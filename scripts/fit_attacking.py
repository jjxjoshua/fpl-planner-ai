#!/usr/bin/env python
"""Fit the attacking-involvement model (blueprint §4, §7.1, E5, session
s004) against the REAL store, read-only, and run the §7.1 walk-forward
calibration gate for both stats (goals, assists) — not a scheduled entry
point, not part of the test suite (`tests/test_attacking.py` mocks/
fabricates everything it needs, plus bounded real-store checks). Run by
hand to see the model's real out-of-sample numbers. Same read-only-by-
design convention as `scripts/fit_defensive_contribution.py`/`scripts/
fit_minutes.py`/`scripts/fit_team_strength.py`.

**Read-only against `data/store/` by design.** This task's OWNED PATHS
forbid `data/**` — this script never calls `write_attacking_pmfs` against
the real store. `--demo-write` exercises the derived-capability write path
end to end, but ONLY against an isolated temp directory
(`tempfile.mkdtemp()`, never under `data/`).

`--demo-write`'s `team_goals_marginal` is a small hand-built distribution
around each row's own REAL historical `team_goals` (not read from a fitted
`team_strength.ScorelinePMF` — this script never imports `fplai.models.
team_strength`, per this module's own "composition, not import" discipline;
see `fplai.models.attacking`'s module docstring) — good enough to exercise
the write path end to end, not a live team-strength prediction.

Usage:
    python scripts/fit_attacking.py [--as-of 2026-08-22T00:00:00Z] [--min-train-rows 2000] [--demo-write]
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
from fplai.models.attacking import (  # noqa: E402
    NUMERIC_FEATURE_COLUMNS_ATTACKING,
    STATS,
    build_training_table,
    fit_attacking_model,
    predict_attacking_pmf,
    walk_forward_validate,
    write_attacking_pmfs,
)
from fplai.store import BitemporalStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fit_attacking")


def _parse_as_of(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--min-train-rows", type=int, default=2000)
    parser.add_argument("--demo-write", action="store_true")
    parser.add_argument("--allow-live-season", action="store_true", default=False)
    args = parser.parse_args()

    as_of = _parse_as_of(args.as_of)
    store = BitemporalStore()  # default path = data/store/, read-only in this script

    logger.info("building the leakage-safe training table, as_of=%s", as_of.isoformat())
    t0 = time.time()
    table = build_training_table(store, as_of=as_of, allow_live_season=args.allow_live_season)
    logger.info("training table: %d rows, seasons=%s, %.2fs", table.height, sorted(table["season"].unique().to_list()), time.time() - t0)

    print("\n=== Attacking-involvement model: headline fit ===")
    t0 = time.time()
    params = fit_attacking_model(store, as_of=as_of)
    print(f"fit time: {time.time() - t0:.2f}s")
    for stat in STATS:
        print(f"  {stat}: n_rows_used={params.stats[stat].n_rows_used}")

    calibration_by_stat: dict[str, CalibrationReference] = {}
    print("\n=== §7.1 gate: walk-forward P(registers >= 1) calibration (2022-23+) ===")
    for stat in STATS:
        t0 = time.time()
        result = walk_forward_validate(table, stat=stat, min_train_rows=args.min_train_rows)
        print(f"\n--- {stat} --- ({time.time() - t0:.2f}s, n_folds={result.n_folds}, n_eval={len(result.y_true)})")
        print(f"{'method':<24}{'log-loss':>12}{'brier':>12}")
        print(f"{'model (Binomial)':<24}{result.model_log_loss():>12.4f}{result.model_brier():>12.4f}")
        print(f"{'baseline: position rate':<24}{result.baseline_position_rate_log_loss():>12.4f}{result.baseline_position_rate_brier():>12.4f}")
        print(f"{'baseline: player trail':<24}{result.baseline_player_trailing_log_loss():>12.4f}{result.baseline_player_trailing_brier():>12.4f}")
        gate_passed = result.beats_both_baselines()
        print(f"§7.1 GATE: {'PASSED' if gate_passed else 'FAILED'} (must beat both baselines on both metrics)")
        print(f"OOS P(involved) residual: mean={result.residual_mean():.4f}  std={result.residual_std():.4f}")

        calibration_by_stat[stat] = CalibrationReference(
            reference=(
                f"OUT-OF-SAMPLE walk-forward residual (actual >=1 indicator minus fitted "
                f"P(involved)) over {len(result.y_true)} minutes>0 eval rows, {result.n_folds} "
                f"folds, 2022-23 onward (the xG/xA-covered era) -- blueprint §7.1's Phase 2 gate."
            ),
            residual_mean=result.residual_mean(),
            residual_std=result.residual_std(),
        )

    if args.demo_write:
        tmp_dir = tempfile.mkdtemp()
        logger.info("demo-write: isolated temp store at %s (NEVER data/store/)", tmp_dir)
        tmp_store = BitemporalStore(base_path=Path(tmp_dir))
        last_round = int(table["round"].max())
        last_round_rows = table.filter(pl.col("round") == last_round)
        pmfs = []
        for row in last_round_rows.head(20).to_dicts():
            fr = {c: row[c] for c in NUMERIC_FEATURE_COLUMNS_ATTACKING}
            fr["position"] = row["position"]
            exposure = [(float(row["minutes"]), 1.0)] if row["minutes"] > 0 else [(0.0, 1.0)]
            # Small hand-built spread around this row's OWN real historical
            # team_goals -- not a fitted team_strength prediction (module
            # docstring's own script header note).
            g0 = int(row["team_goals"])
            marginal = [(max(g0 - 1, 0), 0.25), (g0, 0.5), (g0 + 1, 0.25)]
            # collapse duplicate goal values (e.g. g0=0) into one weighted entry
            merged: dict[int, float] = {}
            for g, w in marginal:
                merged[g] = merged.get(g, 0.0) + w
            marginal = list(merged.items())
            for stat in STATS:
                pmf = predict_attacking_pmf(
                    params, fr, element=row["element"], fixture=row["fixture"], stat=stat,
                    team_goals_marginal=marginal, minute_exposure=exposure,
                )
                pmfs.append(pmf)
        result = write_attacking_pmfs(
            tmp_store, pmfs, season=str(last_round_rows["season"][0]), round_=last_round, valid_at=as_of,
            calibration_by_stat=calibration_by_stat,
        )
        for stat, write_result in result.items():
            print(f"\ndemo-write [{stat}]: written={write_result.written} n_rows={write_result.n_rows}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
