#!/usr/bin/env python
"""Fit the bonus (BPS) model (blueprint §4, §7.1, E5, session s004) against
the REAL store, read-only, and run the archive-pin verification plus the
§7.1 walk-forward calibration gate — not a scheduled entry point, not part
of the test suite (`tests/test_bonus.py` mocks/fabricates everything it
needs, plus bounded real-store checks at a reduced simulation count). Run
by hand to see the model's real out-of-sample numbers. Same read-only-by-
design convention as `scripts/fit_attacking.py`/`scripts/fit_defensive_
contribution.py`/`scripts/fit_minutes.py`/`scripts/fit_team_strength.py`.

**Read-only against `data/store/` by design.** This task's OWNED PATHS
forbid `data/**` — this script never calls `write_bonus_pmfs` against the
real store. `--demo-write` exercises the derived-capability write path end
to end, but ONLY against an isolated temp directory (`tempfile.mkdtemp()`,
never under `data/`).

Usage:
    python scripts/fit_bonus.py [--as-of 2026-08-28T00:00:00Z] [--min-train-rows 2000]
                                 [--n-simulations 4000] [--demo-write]
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
from fplai.models.bonus import (  # noqa: E402
    NUMERIC_FEATURE_COLUMNS_BONUS,
    OUTCOMES,
    BonusPlayerInput,
    build_training_table,
    fit_bonus_model,
    predict_bonus_pmfs_for_fixture,
    verify_bonus_award_rule_against_archive,
    walk_forward_validate,
    write_bonus_pmfs,
)
from fplai.store import BitemporalStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fit_bonus")


def _parse_as_of(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--min-train-rows", type=int, default=2000)
    parser.add_argument("--n-simulations", type=int, default=4000)
    parser.add_argument("--demo-write", action="store_true")
    parser.add_argument("--allow-live-season", action="store_true", default=False)
    args = parser.parse_args()

    as_of = _parse_as_of(args.as_of)
    store = BitemporalStore()  # default path = data/store/, read-only in this script

    print("\n=== Award-rule archive verification (module docstring, 'pinned from the archive') ===")
    t0 = time.time()
    verification = verify_bonus_award_rule_against_archive(store, as_of=as_of)
    print(f"({time.time() - t0:.2f}s)")
    print(f"  fixtures checked: {verification.n_fixtures}")
    print(f"  player rows checked: {verification.n_player_rows}")
    print(f"  mismatched fixtures: {verification.n_mismatched_fixtures}")
    print(f"  match rate: {verification.match_rate:.4%}")
    if verification.mismatch_examples:
        print(f"  examples (season, round, fixture): {verification.mismatch_examples}")

    logger.info("building the leakage-safe training table, as_of=%s", as_of.isoformat())
    t0 = time.time()
    table = build_training_table(store, as_of=as_of, allow_live_season=args.allow_live_season)
    logger.info(
        "training table: %d rows, seasons=%s, %.2fs",
        table.height, sorted(table["season"].unique().to_list()), time.time() - t0,
    )

    print("\n=== Bonus model: headline fit ===")
    t0 = time.time()
    params = fit_bonus_model(store, as_of=as_of)
    print(f"fit time: {time.time() - t0:.2f}s, n_rows_used={params.n_rows_used}")
    print(f"feature names: {params.feature_spec.feature_names}")
    print(f"beta: {params.beta}")
    for pos in ("GK", "DEF", "MID", "FWD"):
        pool = params.residual_pool_by_position.get(pos, ())
        if pool:
            import numpy as np

            arr = np.array(pool)
            print(f"  residual pool [{pos}]: n={len(pool)}, mean={arr.mean():.3f}, std={arr.std():.3f}")

    print(f"\n=== §7.1 gate: walk-forward 4-class calibration (min_train_rows={args.min_train_rows}, n_simulations={args.n_simulations}) ===")
    t0 = time.time()
    result = walk_forward_validate(table, min_train_rows=args.min_train_rows, n_simulations=args.n_simulations)
    print(f"({time.time() - t0:.2f}s, n_folds={result.n_folds}, n_eval={result.n_eval_rows})")
    print(f"{'method':<28}{'log-loss':>12}{'brier':>12}")
    print(f"{'model (ridge + MC sim)':<28}{result.model_log_loss():>12.4f}{result.model_brier():>12.4f}")
    print(f"{'baseline: group rate':<28}{result.baseline_group_rate_log_loss():>12.4f}{result.baseline_group_rate_brier():>12.4f}")
    print(f"{'baseline: player trailing':<28}{result.baseline_player_trailing_log_loss():>12.4f}{result.baseline_player_trailing_brier():>12.4f}")
    gate_passed = result.beats_both_baselines()
    print(f"§7.1 GATE: {'PASSED' if gate_passed else 'FAILED'} (must beat both baselines on both metrics)")

    print("\n=== Reliability, per outcome (module docstring, 'required per-outcome') ===")
    print(f"{'outcome':<10}{'n':>8}{'log-loss':>12}{'brier':>10}{'ece':>10}{'slope':>10}{'intercept':>12}")
    residual_means: list[float] = []
    residual_stds: list[float] = []
    for k in OUTCOMES:
        m = result.reliability_for_outcome(k)
        print(f"{k:<10}{m.n:>8}{m.log_loss:>12.4f}{m.brier:>10.4f}{m.ece:>10.4f}{m.calibration_slope:>10.3f}{m.calibration_intercept:>12.3f}")

    calibration = CalibrationReference(
        reference=(
            f"OUT-OF-SAMPLE walk-forward 4-class Brier/log-loss over {result.n_eval_rows} eval rows, "
            f"{result.n_folds} folds, 2020-21 onward (excludes 2019-20, no position/team upstream) -- "
            "blueprint §7.1's Phase 2 gate. Per-outcome reliability reported separately, not folded "
            "into this single reference (module docstring, 'Reliability — required per-outcome')."
        ),
        residual_mean=0.0,
        residual_std=1.0,
    )

    if args.demo_write:
        tmp_dir = tempfile.mkdtemp()
        logger.info("demo-write: isolated temp store at %s (NEVER data/store/)", tmp_dir)
        tmp_store = BitemporalStore(base_path=Path(tmp_dir))
        last_round = int(table["round"].max())
        fixture_ids = table.filter(pl.col("round") == last_round)["fixture"].unique().to_list()[:1]
        pmfs = []
        for fixture_id in fixture_ids:
            rows = table.filter((pl.col("round") == last_round) & (pl.col("fixture") == fixture_id)).to_dicts()
            players = []
            for row in rows:
                fr = {c: row[c] for c in NUMERIC_FEATURE_COLUMNS_BONUS if c != "minutes_frac"}
                exposure = [(float(row["minutes"]), 1.0)] if row["minutes"] > 0 else [(0.0, 1.0)]
                players.append(BonusPlayerInput(element=row["element"], position=row["position"], feature_row=fr, minute_exposure=exposure))
            if len(players) < 2:
                continue
            pmfs.extend(predict_bonus_pmfs_for_fixture(params, players, fixture=fixture_id, n_simulations=args.n_simulations))
        if pmfs:
            season = str(table.filter(pl.col("round") == last_round)["season"][0])
            write_result = write_bonus_pmfs(
                tmp_store, pmfs, season=season, round_=last_round, valid_at=as_of, calibration=calibration,
            )
            print(f"\ndemo-write: written={write_result.written} n_rows={write_result.n_rows}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
