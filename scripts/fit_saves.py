#!/usr/bin/env python
"""Fit the GK saves model (blueprint §4, §7.1, E5, Phase 3 prerequisite,
session s005) against the REAL store, read-only, and run the data-shape
verification plus the §7.1 walk-forward calibration gate — not a scheduled
entry point, not part of the test suite (`tests/test_saves.py` fabricates
most of what it needs, plus bounded real-store checks). Run by hand to see
the model's real out-of-sample numbers. Same read-only-by-design
convention as `scripts/fit_cards.py`/`scripts/fit_defensive_contribution.py`.

**Read-only against `data/store/` by design.** This task's OWNED PATHS
forbid `data/**` — this script never calls `write_saves_pmfs` against the
real store. `--demo-write` exercises the derived-capability write path end
to end, but ONLY against an isolated temp directory (`tempfile.mkdtemp()`,
never under `data/`).

Usage:
    python scripts/fit_saves.py [--as-of 2026-08-30T00:00:00Z]
                                 [--min-train-rows 1500] [--demo-write]
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

import polars as pl  # noqa: E402

from fplai.derived import CalibrationReference  # noqa: E402
from fplai.models.saves import (  # noqa: E402
    PREDICT_FEATURE_ROW_COLUMNS,
    SavesModelConfig,
    build_training_table,
    fit_saves_model,
    predict_saves_pmf,
    read_saves_points_per_unit,
    verify_saves_data_shape_against_archive,
    walk_forward_validate,
    write_saves_pmfs,
)
from fplai.store import BitemporalStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fit_saves")


def _parse_as_of(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--min-train-rows", type=int, default=1500)
    parser.add_argument("--demo-write", action="store_true")
    parser.add_argument("--allow-live-season", action="store_true", default=False)
    args = parser.parse_args()

    as_of = _parse_as_of(args.as_of)
    store = BitemporalStore()  # default path = data/store/, read-only in this script

    print("\n=== Data-shape verification (module docstring, 'Data verified live') ===")
    t0 = time.time()
    v = verify_saves_data_shape_against_archive(store, as_of=as_of)
    print(f"({time.time() - t0:.2f}s)")
    print(f"  GK appearances (minutes>0): {v.n_gk_appearances}")
    print(f"  non-GK rows with saves>0 (checked anomaly): {v.n_non_gk_rows_with_saves_positive}")
    print(f"  full-90 GK rows checked (opponent_goals vs goals_conceded): {v.n_full90_gk_rows_checked}")
    print(f"  mismatches: {v.n_opponent_goals_vs_goals_conceded_mismatches}")
    print(f"  corr(opponent_goals_this_fixture, saves): {v.corr_opponent_goals_saves:.4f}")

    logger.info("building the leakage-safe training table, as_of=%s", as_of.isoformat())
    t0 = time.time()
    table = build_training_table(store, as_of=as_of, allow_live_season=args.allow_live_season)
    logger.info(
        "training table: %d rows, seasons=%s, %.2fs",
        table.height, sorted(table["season"].unique().to_list()), time.time() - t0,
    )
    counts = table["saves"].cast(pl.Float64)
    print(
        f"\nsaves: n={table.height} mean={counts.mean():.4f} var={counts.var(ddof=0):.4f} "
        f"var/mean={counts.var(ddof=0) / counts.mean():.4f} max={int(counts.max())}"
    )

    print("\n=== Saves model: headline fit (calibrate=False, the SHIPPED default) ===")
    t0 = time.time()
    params = fit_saves_model(store, as_of=as_of)
    print(f"fit time: {time.time() - t0:.2f}s, n_rows_used={params.n_rows_used}, r={params.r:.4f}")
    print(f"feature names: {params.feature_spec.feature_names}")
    print(f"beta: {params.beta}")
    print(f"p_ge3 nested isotonic calibrator attached: {params.p_ge3_calibrator is not None}")

    print(f"\n=== §7.1 gate: walk-forward count-PMF calibration (min_train_rows={args.min_train_rows}) ===")
    print("computed with calibrate=True so RAW (== what ships) and CALIBRATED (research only, see fit_saves_model's own docstring for why it does not ship) are both visible from one run.")
    t0 = time.time()
    result = walk_forward_validate(table, min_train_rows=args.min_train_rows)  # calibrate=True default, for the comparison
    print(f"({time.time() - t0:.2f}s, n_folds={result.n_folds}, n_folds_calibrated={result.n_folds_calibrated}, n_eval={len(result.y_true)})")
    print(f"{'method':<28}{'log-loss':>12}{'brier':>12}{'rps':>12}")
    print(f"{'model RAW (SHIPS)':<28}{result.model_log_loss_raw():>12.4f}{result.model_brier_raw():>12.4f}{result.model_rps_raw():>12.4f}")
    print(f"{'model CALIBRATED (research)':<28}{result.model_log_loss():>12.4f}{result.model_brier():>12.4f}{result.model_rps():>12.4f}")
    print(
        f"{'baseline: group rate':<28}{result.baseline_group_rate_log_loss():>12.4f}"
        f"{result.baseline_group_rate_brier():>12.4f}{result.baseline_group_rate_rps():>12.4f}"
    )
    print(
        f"{'baseline: player trailing':<28}{result.baseline_player_trailing_log_loss():>12.4f}"
        f"{result.baseline_player_trailing_brier():>12.4f}{result.baseline_player_trailing_rps():>12.4f}"
    )
    gate_passed_raw = (
        result.model_log_loss_raw() < result.baseline_group_rate_log_loss()
        and result.model_log_loss_raw() < result.baseline_player_trailing_log_loss()
        and result.model_brier_raw() < result.baseline_group_rate_brier()
        and result.model_brier_raw() < result.baseline_player_trailing_brier()
    )
    not_worse = result.calibrated_not_worse_than_raw()
    print(f"§7.1 GATE on the SHIPPED (raw) series vs both baselines, log-loss+Brier: {'PASSED' if gate_passed_raw else 'FAILED'}")
    print(f"CALIBRATED NOT WORSE THAN RAW on every pooled metric (it is NOT -- this is why calibrate=False ships): {not_worse}")

    print("\n=== Reliability, per threshold, RAW vs CALIBRATED ===")
    print(f"{'threshold':<12}{'series':<12}{'n':>8}{'log-loss':>10}{'brier':>8}{'ece(w)':>9}{'ece(q)':>9}{'slope':>8}{'95% CI':>18}{'usable':>8}")
    for label, thr in (("ge_points(>=3)", 3), ("ge_any(>=1)", 1)):
        for series_label, m in (("raw", result.reliability_for_threshold(thr, raw=True)), ("calibrated", result.reliability_for_threshold(thr, raw=False))):
            ci = f"[{m.slope_ci_lo:.3f},{m.slope_ci_hi:.3f}]"
            print(
                f"{label:<12}{series_label:<12}{m.n:>8}{m.log_loss:>10.4f}{m.brier:>8.4f}{m.ece:>9.4f}"
                f"{m.ece_quantile:>9.4f}{m.calibration_slope:>8.3f}{ci:>18}{str(m.slope_usable):>8}"
            )

    points_per_unit = read_saves_points_per_unit(store)
    print(f"\nlive saves points-per-unit (game_config): {points_per_unit} (divisor, unpublished, checked: 3)")

    if args.demo_write:
        tmp_dir = tempfile.mkdtemp()
        logger.info("demo-write: isolated temp store at %s (NEVER data/store/)", tmp_dir)
        tmp_store = BitemporalStore(base_path=Path(tmp_dir))
        last_round = int(table["round"].max())
        rows = table.filter((pl.col("round") == last_round) & (pl.col("minutes") > 0)).head(5).to_dicts()
        pmfs = []
        for row in rows:
            fr = {c: row[c] for c in PREDICT_FEATURE_ROW_COLUMNS}
            pmfs.append(
                predict_saves_pmf(
                    params, fr, element=row["element"], fixture=row["fixture"],
                    minute_exposure=[(90.0, 0.8), (0.0, 0.2)],
                    opponent_goals_marginal=[(0, 0.3), (1, 0.35), (2, 0.2), (3, 0.1), (4, 0.05)],
                )
            )
        if pmfs:
            season = str(table.filter(pl.col("round") == last_round)["season"][0])
            calibration = CalibrationReference(
                reference=(
                    f"OUT-OF-SAMPLE walk-forward count-PMF log-loss/Brier/RPS over {len(result.y_true)} "
                    f"eval rows, {result.n_folds} folds, 2020-21 onward -- blueprint §7.1's Phase 2/3 gate."
                ),
                residual_mean=0.0,
                residual_std=1.0,
            )
            write_result = write_saves_pmfs(
                tmp_store, pmfs, season=season, round_=last_round, valid_at=as_of, calibration=calibration,
            )
            print(f"\ndemo-write: written={write_result.written} n_rows={write_result.n_rows} calibration_method={pmfs[0].calibration_method}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
