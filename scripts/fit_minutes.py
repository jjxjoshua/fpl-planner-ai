#!/usr/bin/env python
"""Fit the minutes model (blueprint §4.1, E5) against the REAL store,
read-only, and run the full walk-forward calibration gate (§7.1) — not a
scheduled entry point, not part of the test suite (`tests/test_minutes.py`
mocks/fabricates everything it needs, plus one BOUNDED live sanity check).
Run by hand to see the model's real out-of-sample numbers.

**Read-only against `data/store/` by design.** This task's OWNED PATHS
forbid `data/**` — this script never calls `write_minutes_pmfs` against the
real store. `--demo-write` exercises the derived-capability write path
(blueprint §12.2) end-to-end, but ONLY against an isolated temp directory
(`tempfile.mkdtemp()`, never under `data/`), same convention
`scripts/fit_team_strength.py --demo-write` already established.

Usage:
    python scripts/fit_minutes.py [--eval-seasons 2024-25 2025-26] [--demo-write]
    python scripts/fit_minutes.py --calibrate   # session s003, blueprint §7.1

The full walk-forward (refitting a softmax at every evaluated gameweek) is
the reason this is a script, not a test: ~40s per evaluated season against
the real store (measured live, this session) — too slow for the regular
suite to pay on every run, but this IS the live verification the brief
requires; see docs/wiki/model-minutes.md for the numbers this script
produces, captured once.

`--calibrate` (session s003) additionally reports the NESTED out-of-sample
P(start) calibration numbers — see `fplai.models.minutes`'s "Nested
out-of-sample P(start) calibration" section for the mechanism and its
leakage boundary. Prints raw vs. calibrated log-loss/Brier/ECE/calibration-
slope-intercept, pooled AND per position, plus both reliability tables —
this is the authoritative report `docs/wiki/model-minutes.md` is built
from. Roughly doubles the walk-forward's own runtime (an extra inner
fit+score per fold).
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.derived import CalibrationReference  # noqa: E402
from fplai.models.minutes import (  # noqa: E402
    MinutesModelConfig,
    build_training_table,
    fit_minutes_model,
    predict_minutes_pmf,
    walk_forward_validate,
    write_minutes_pmfs,
)
from fplai.store import BitemporalStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fit_minutes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        default=datetime.now(timezone.utc).isoformat(),
        help="ISO-8601 UTC deadline for the headline fit (default: now).",
    )
    parser.add_argument(
        "--eval-seasons",
        nargs="+",
        default=["2024-25", "2025-26"],
        help="Seasons to walk-forward evaluate the §7.1 gate over (default: the last two "
        "labelled seasons). Each evaluated season costs ~roughly one softmax refit per "
        "gameweek against all prior data -- ~40s/season measured live.",
    )
    parser.add_argument(
        "--demo-write",
        action="store_true",
        help="Also demonstrate fplai.derived.write_derived end-to-end -- against an isolated "
        "temp store (tempfile.mkdtemp(), NEVER data/store/), not the real one.",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Session s003, blueprint §7.1: also run the (expensive, ~2x walk-forward "
        "runtime) nested out-of-sample P(start) calibration REPORT -- raw vs. calibrated, "
        "pooled and per position. The headline fit's own calibrator (fit_minutes_model's "
        "calibrate=True default, evidence from this session) is ALWAYS attached regardless "
        "of this flag -- see fit_minutes_model's docstring. This flag controls only whether "
        "the diagnostic comparison is also computed and printed.",
    )
    parser.add_argument("--allow-live-season", action="store_true", default=False)
    args = parser.parse_args()

    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    store = BitemporalStore()  # default path = data/store/, read-only in this script

    logger.info("building the leakage-safe training table (all labelled seasons), as_of=%s", as_of.isoformat())
    t0 = time.time()
    table = build_training_table(store, as_of=as_of, allow_live_season=args.allow_live_season)
    logger.info("training table: %d rows, %.2fs", table.height, time.time() - t0)

    print("\n=== Minutes model: headline fit (in-sample feature construction, out-of-sample gate below) ===")
    t0 = time.time()
    # fit_minutes_model's own calibrate=True DEFAULT applies here -- the
    # headline fit always carries a nested calibrator when the training
    # window supports one (session s003 evidence, see that function's
    # docstring), independent of --calibrate below (which controls only
    # the separate, expensive walk-forward diagnostic REPORT).
    params = fit_minutes_model(store, as_of=as_of)
    print(f"fit time: {time.time() - t0:.2f}s")
    print(f"n_rows_used: {params.n_rows_used}  seasons_used: {', '.join(params.seasons_used)}")
    print(f"n_features: {params.feature_spec.n_features}")
    print(f"p_start_calibrator attached: {params.p_start_calibrator is not None}")

    print("\n=== §7.1 gate: walk-forward P(start) calibration ===")
    print(f"eval_seasons={args.eval_seasons}  calibrate={args.calibrate}")
    t0 = time.time()
    result = walk_forward_validate(
        table, eval_seasons=args.eval_seasons, min_train_rows=200, calibrate=args.calibrate
    )
    print(f"walk-forward time: {time.time() - t0:.2f}s")
    print(f"n_folds={result.n_folds}  n_eval_rows={len(result.y_true)}")
    if args.calibrate:
        print(f"n_folds_calibrated={result.n_folds_calibrated}")
    print(f"\n{'method':<28}{'log-loss':>12}{'brier':>12}")
    print(f"{'model':<28}{result.model_log_loss():>12.4f}{result.model_brier():>12.4f}")
    print(
        f"{'baseline: started last GW':<28}{result.baseline_last_gw_log_loss():>12.4f}"
        f"{result.baseline_last_gw_brier():>12.4f}"
    )
    print(
        f"{'baseline: base rate by pos':<28}{result.baseline_position_rate_log_loss():>12.4f}"
        f"{result.baseline_position_rate_brier():>12.4f}"
    )
    verdict = "PASS" if result.beats_both_baselines() else "FAIL"
    print(f"\nGATE: {verdict} (model must beat BOTH baselines on BOTH metrics)")

    print("\n=== Reliability diagram (model P(start), 10 bins) ===")
    for b in result.reliability_diagram(10):
        print(f"  [{b.bin_lo:.1f}, {b.bin_hi:.1f}) n={b.n:6d}  mean_predicted={b.mean_predicted:.3f}  observed_rate={b.observed_rate:.3f}")

    if args.calibrate:
        print("\n=== Session s003: nested out-of-sample calibration report (blueprint §7.1) ===")

        def _print_metrics_block(title: str, m) -> None:
            print(f"\n-- {title} (n={m.n}) --")
            print(f"  log-loss={m.log_loss:.4f}  brier={m.brier:.4f}  ece={m.ece:.4f}  "
                  f"slope={m.calibration_slope:.3f}  intercept={m.calibration_intercept:.3f}")
            for b in m.reliability:
                print(f"    [{b.bin_lo:.1f}, {b.bin_hi:.1f}) n={b.n:6d}  mean_predicted={b.mean_predicted:.3f}  observed_rate={b.observed_rate:.3f}")

        raw = result.raw_metrics()
        calibrated = result.calibrated_metrics()
        _print_metrics_block("POOLED -- raw (uncalibrated)", raw)
        _print_metrics_block("POOLED -- calibrated (isotonic_v1)", calibrated)

        print(f"\nPOOLED delta: log-loss {calibrated.log_loss - raw.log_loss:+.4f}  "
              f"brier {calibrated.brier - raw.brier:+.4f}  ece {calibrated.ece - raw.ece:+.4f}")

        raw_by_pos = result.raw_metrics_by_position()
        calibrated_by_pos = result.calibrated_metrics_by_position()
        print(f"\n{'position':<10}{'n':>8}{'raw_ll':>10}{'cal_ll':>10}{'raw_brier':>11}{'cal_brier':>11}{'raw_ece':>10}{'cal_ece':>10}")
        for pos in sorted(raw_by_pos):
            r, c = raw_by_pos[pos], calibrated_by_pos[pos]
            print(f"{pos:<10}{r.n:>8}{r.log_loss:>10.4f}{c.log_loss:>10.4f}{r.brier:>11.4f}{c.brier:>11.4f}{r.ece:>10.4f}{c.ece:>10.4f}")

    # A handful of example predictions from the headline fit, for a sanity
    # read -- not part of the gate.
    print("\n=== Example predictions (most recent gameweek in the training window) ===")
    latest_season = params.seasons_used[-1]
    sample = table.filter(table["season"] == latest_season).sort("round", descending=True).head(5).to_dicts()
    for row in sample:
        pmf = predict_minutes_pmf(params, row, element=row["element"], fixture=row["fixture"])
        print(
            f"  element={row['element']:>6} pos={row['position']:<3} team={row['team']:<20} "
            f"p_start={pmf.p_start():.3f} E[min]={pmf.expected_minutes():.1f} p(60+)={pmf.p_appearance_60_plus():.3f} "
            f"(actual: state={row['state']} minutes={row['minutes']})"
        )

    if args.demo_write:
        scratch = Path(tempfile.mkdtemp(prefix="fplai_minutes_demo_"))
        logger.info("demo-write: isolated temp store at %s (never data/store/)", scratch)
        demo_store = BitemporalStore(base_path=scratch / "store")
        latest_round_rows = table.filter(table["season"] == latest_season).sort("round", descending=True).head(5).to_dicts()
        pmfs = [
            predict_minutes_pmf(params, row, element=row["element"], fixture=row["fixture"])
            for row in latest_round_rows
        ]
        calibration = CalibrationReference(
            reference=(
                f"walk-forward out-of-sample P(start) over eval_seasons={args.eval_seasons} "
                f"({result.n_folds} folds, {len(result.y_true)} eval rows): "
                f"log-loss={result.model_log_loss():.4f}, brier={result.model_brier():.4f}"
            ),
            residual_mean=0.0,
            residual_std=result.model_brier() ** 0.5,
        )
        write_result = write_minutes_pmfs(
            demo_store,
            pmfs,
            season=latest_season,
            round_=int(latest_round_rows[0]["round"]),
            valid_at=as_of,
            calibration=calibration,
            source="scripts/fit_minutes.py --demo-write",
        )
        print(f"\n=== derived-capability write demo (temp store, not data/store/) ===")
        print(f"written={write_result.written}  n_rows={write_result.n_rows}  dataset={write_result.dataset}")
        readback = demo_store.as_of("derived_player_minutes_distribution", datetime.now(timezone.utc))
        print(f"read back: {readback.height} rows, is_modelled all True: {bool(readback['is_modelled'].all())}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
