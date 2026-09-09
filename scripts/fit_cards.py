#!/usr/bin/env python
"""Fit the cards (discipline) model (blueprint §4, §7.1, E5, session s004;
session s005 added the nested NONE/YELLOW isotonic calibration layer that
closes the E5 blocking condition) against the REAL store, read-only, and
run the outcome-space archive verification plus the §7.1 walk-forward
calibration gate — not a scheduled entry point, not part of the test suite
(`tests/test_cards.py` mocks/fabricates most of what it needs, plus bounded
real-store checks). Run by hand to see the model's real out-of-sample
numbers, RAW and CALIBRATED side by side, plus the referee-value
measurement this task's brief asked for. Same read-only-by-design
convention as `scripts/fit_bonus.py`/`scripts/fit_attacking.py`/
`scripts/fit_defensive_contribution.py`/`scripts/fit_minutes.py`.

**Read-only against `data/store/` by design.** This task's OWNED PATHS
forbid `data/**` — this script never calls `write_cards_pmfs` against the
real store. `--demo-write` exercises the derived-capability write path end
to end, but ONLY against an isolated temp directory (`tempfile.mkdtemp()`,
never under `data/`).

Usage:
    python scripts/fit_cards.py [--as-of 2026-08-29T00:00:00Z]
                                 [--min-train-rows 8000] [--l2-sweep]
                                 [--skip-referee] [--demo-write]
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
from fplai.models.cards import (  # noqa: E402
    NUMERIC_FEATURE_COLUMNS_CARDS,
    OUTCOME_LABELS,
    OUTCOMES,
    CardsModelConfig,
    attach_referee_feature_NOT_DEPLOYABLE,
    build_referee_trailing_feature_table_NOT_DEPLOYABLE,
    build_training_table,
    fit_cards_model,
    measure_referee_value_NOT_DEPLOYABLE,
    predict_cards_pmf,
    read_card_points,
    verify_yellow_red_mutually_exclusive_against_archive,
    walk_forward_validate,
    write_cards_pmfs,
)
from fplai.store import BitemporalStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fit_cards")


def _parse_as_of(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--min-train-rows", type=int, default=8000)
    parser.add_argument("--l2-sweep", action="store_true")
    parser.add_argument("--skip-referee", action="store_true", help="skip the referee-value measurement (requires pl_match_officials)")
    parser.add_argument("--demo-write", action="store_true")
    parser.add_argument("--allow-live-season", action="store_true", default=False)
    args = parser.parse_args()

    as_of = _parse_as_of(args.as_of)
    store = BitemporalStore()  # default path = data/store/, read-only in this script

    print("\n=== Outcome-space archive verification (module docstring, 'What is verified') ===")
    t0 = time.time()
    verification = verify_yellow_red_mutually_exclusive_against_archive(store, as_of=as_of)
    print(f"({time.time() - t0:.2f}s)")
    print(f"  rows checked: {verification.n_rows}")
    print(f"  yellow AND red both 1: {verification.n_yellow_and_red_both_one}")
    print(f"  yellow out of {{0,1}}: {verification.n_yellow_out_of_range}")
    print(f"  red out of {{0,1}}: {verification.n_red_out_of_range}")
    print(f"  HOLDS: {verification.holds}")

    logger.info("building the leakage-safe training table, as_of=%s", as_of.isoformat())
    t0 = time.time()
    table = build_training_table(store, as_of=as_of, allow_live_season=args.allow_live_season)
    logger.info(
        "training table: %d rows, seasons=%s, %.2fs",
        table.height, sorted(table["season"].unique().to_list()), time.time() - t0,
    )
    print(f"\noutcome distribution: {table['outcome'].value_counts().sort('outcome').to_dicts()}")

    if args.l2_sweep:
        # calibrate=False deliberately: the sweep is diagnosing the RAW
        # multinomial fit's own regularisation behaviour, unrelated to the
        # session-s005 NONE/YELLOW isotonic layer (same reasoning as
        # tests/test_cards.py's synthetic-DGP test opting out).
        print("\n=== l2_penalty sweep (walk-forward RAW model, min_train_rows=%d) ===" % args.min_train_rows)
        print(f"{'l2':<10}{'n_folds':<10}{'model_ll':>12}{'grp_ll':>12}{'model_brier':>14}{'beats':>8}")
        for l2 in (1.0, 0.3, 0.1, 0.05, 0.02, 0.01, 0.005, 0.001, 0.0005, 0.0001):
            cfg = CardsModelConfig(l2_penalty=l2)
            res = walk_forward_validate(table, min_train_rows=args.min_train_rows, config=cfg, calibrate=False)
            print(
                f"{l2:<10}{res.n_folds:<10}{res.model_log_loss():>12.4f}"
                f"{res.baseline_group_rate_log_loss():>12.4f}{res.model_brier():>14.4f}{res.beats_both_baselines()!s:>8}"
            )

    print("\n=== Cards model: headline fit ===")
    t0 = time.time()
    params = fit_cards_model(store, as_of=as_of)
    print(f"fit time: {time.time() - t0:.2f}s, n_rows_used={params.n_rows_used}")
    print(f"feature names: {params.feature_spec.feature_names}")
    print(f"beta_yellow: {params.beta_yellow}")
    print(f"beta_red: {params.beta_red}")
    print(
        f"NONE/YELLOW nested isotonic calibrator attached: "
        f"{params.p_none_calibrator is not None and params.p_yellow_calibrator is not None}"
    )

    print(f"\n=== §7.1 gate: walk-forward 3-class calibration (min_train_rows={args.min_train_rows}) ===")
    print("session s005: RAW and CALIBRATED (NONE/YELLOW nested isotonic, RED untouched) computed from the SAME walk-forward run.")
    t0 = time.time()
    result = walk_forward_validate(table, min_train_rows=args.min_train_rows)  # calibrate=True default
    print(
        f"({time.time() - t0:.2f}s, n_folds={result.n_folds}, n_folds_calibrated={result.n_folds_calibrated}, "
        f"n_eval={len(result.y_true_class)})"
    )
    print(f"{'method':<28}{'log-loss':>12}{'brier':>12}")
    print(f"{'model RAW':<28}{result.model_log_loss_raw():>12.4f}{result.model_brier_raw():>12.4f}")
    print(f"{'model CALIBRATED':<28}{result.model_log_loss():>12.4f}{result.model_brier():>12.4f}")
    print(f"{'baseline: group rate':<28}{result.baseline_group_rate_log_loss():>12.4f}{result.baseline_group_rate_brier():>12.4f}")
    print(f"{'baseline: player trailing':<28}{result.baseline_player_trailing_log_loss():>12.4f}{result.baseline_player_trailing_brier():>12.4f}")
    gate_passed = result.beats_both_baselines()
    not_worse = result.calibrated_not_worse_than_raw()
    print(f"§7.1 GATE (calibrated model vs both baselines): {'PASSED' if gate_passed else 'FAILED'}")
    print(f"CALIBRATED NOT WORSE THAN RAW on every pooled metric (this task's brief, explicit): {not_worse}")

    print("\n=== Reliability, per outcome, RAW vs CALIBRATED (module docstring, 'required per-outcome') ===")
    print(f"{'outcome':<8}{'series':<12}{'n':>8}{'log-loss':>10}{'brier':>8}{'ece(w)':>9}{'ece(q)':>9}{'slope':>8}{'95% CI':>18}{'intercept':>10}")
    for k in OUTCOMES:
        for label, m in (("raw", result.reliability_for_outcome_raw(k)), ("calibrated", result.reliability_for_outcome(k))):
            ci = f"[{m.slope_ci_lo:.3f},{m.slope_ci_hi:.3f}]"
            print(
                f"{OUTCOME_LABELS[k]:<8}{label:<12}{m.n:>8}{m.log_loss:>10.4f}{m.brier:>8.4f}{m.ece:>9.4f}"
                f"{m.ece_quantile:>9.4f}{m.calibration_slope:>8.3f}{ci:>18}{m.calibration_intercept:>10.3f}"
            )

    yellow_points, red_points = read_card_points(store)
    print(f"\nlive card points (game_config): yellow={yellow_points}, red={red_points}")

    if not args.skip_referee:
        print("\n=== Referee value measurement — RESEARCH ONLY, NOT DEPLOYABLE (module docstring) ===")
        try:
            t0 = time.time()
            ref_table = build_referee_trailing_feature_table_NOT_DEPLOYABLE(store, as_of=as_of)
            print(f"referee-trailing table: {ref_table.height} fixtures resolved, {time.time() - t0:.2f}s")
            attached = attach_referee_feature_NOT_DEPLOYABLE(table, ref_table)
            print(f"attached (minutes>0) rows: {attached.height} of {table.height} ({attached.height / table.height:.2%})")

            t0 = time.time()
            comparison = measure_referee_value_NOT_DEPLOYABLE(attached, min_train_rows=args.min_train_rows)
            print(f"comparison ({time.time() - t0:.2f}s), n_rows_compared={comparison.n_rows_compared}")
            print(f"  deployable:              log-loss={comparison.deployable.model_log_loss():.4f}  brier={comparison.deployable.model_brier():.4f}")
            print(
                f"  referee-inclusive (NOT DEPLOYABLE): log-loss="
                f"{comparison.referee_inclusive_variant_NOT_DEPLOYABLE.model_log_loss():.4f}  "
                f"brier={comparison.referee_inclusive_variant_NOT_DEPLOYABLE.model_brier():.4f}"
            )
            print(f"  log_loss_delta (deployable - referee, positive=referee would help): {comparison.log_loss_delta():.6f}")
            print(f"  brier_delta: {comparison.brier_delta():.6f}")
            print(
                "  VERDICT: referee identity would buy "
                f"{'a real improvement' if comparison.log_loss_delta() > 0.005 else 'essentially nothing'} "
                "if it were deployable (arbitrary 0.005 log-loss threshold for 'real' — read the raw delta)."
            )
        except Exception as exc:  # pl_match_officials may be absent in some environments
            logger.warning("referee value measurement skipped: %s", exc)

    if args.demo_write:
        tmp_dir = tempfile.mkdtemp()
        logger.info("demo-write: isolated temp store at %s (NEVER data/store/)", tmp_dir)
        tmp_store = BitemporalStore(base_path=Path(tmp_dir))
        last_round = int(table["round"].max())
        rows = table.filter((pl.col("round") == last_round) & (pl.col("minutes") > 0)).head(5).to_dicts()
        pmfs = []
        for row in rows:
            fr = {c: row[c] for c in NUMERIC_FEATURE_COLUMNS_CARDS} | {"position": row["position"]}
            pmfs.append(
                predict_cards_pmf(
                    params, fr, element=row["element"], fixture=row["fixture"], minute_exposure=[(90.0, 0.8), (0.0, 0.2)]
                )
            )
        if pmfs:
            season = str(table.filter(pl.col("round") == last_round)["season"][0])
            calibration = CalibrationReference(
                reference=(
                    f"OUT-OF-SAMPLE walk-forward 3-class Brier/log-loss over {len(result.y_true_class)} eval rows, "
                    f"{result.n_folds} folds, 2020-21 onward -- blueprint §7.1's Phase 2 gate. Fit WITHOUT referee "
                    "identity (not resolvable as of a pre-deadline as_of); per-outcome reliability reported "
                    "separately, not folded into this single reference."
                ),
                residual_mean=0.0,
                residual_std=1.0,
            )
            write_result = write_cards_pmfs(
                tmp_store, pmfs, season=season, round_=last_round, valid_at=as_of, calibration=calibration,
            )
            print(
                f"\ndemo-write: written={write_result.written} n_rows={write_result.n_rows} "
                f"calibration_method={pmfs[0].calibration_method}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
