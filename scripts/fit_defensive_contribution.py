#!/usr/bin/env python
"""Fit the defensive-contribution (DC) estimator (blueprint §4, §7.1, §11,
E5, session s003) against the REAL store, read-only, and run the §7.1
walk-forward calibration gate for both position groups — not a scheduled
entry point, not part of the test suite (`tests/test_defensive_
contribution.py` mocks/fabricates everything it needs, plus bounded
real-store checks). Run by hand to see the model's real out-of-sample
numbers. Same read-only-by-design convention as `scripts/fit_minutes.py`/
`scripts/fit_team_strength.py`.

**Read-only against `data/store/` by design.** This task's OWNED PATHS
forbid `data/**` — this script never calls `write_dc_pmfs` against the
real store. `--demo-write` exercises the derived-capability write path end
to end, but ONLY against an isolated temp directory (`tempfile.
mkdtemp()`, never under `data/`).

Usage:
    python scripts/fit_defensive_contribution.py [--as-of 2026-08-22T00:00:00Z] [--min-train-rows 500] [--demo-write]
    python scripts/fit_defensive_contribution.py --show-team-transfer-effect "Elliot Anderson" "Nott'm Forest" "Man City"
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
from fplai.models.defensive_contribution import (  # noqa: E402
    DC_GROUPS,
    NUMERIC_FEATURE_COLUMNS_DC,
    build_dc_threshold_set,
    build_training_table,
    fit_dc_model,
    predict_dc_pmf,
    walk_forward_validate,
    write_dc_pmfs,
)
from fplai.store import BitemporalStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fit_defensive_contribution")


def _parse_as_of(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _run_team_transfer_demo(store: BitemporalStore, player_name: str, from_team: str, to_team: str, as_of: datetime) -> None:
    """Module docstring's "What this predicts for Elliot Anderson" demo.
    Uses each team's SEASON-MEAN `team_trailing_dc_mean_5` (not the raw
    last-round snapshot) as the team-style value — see
    `docs/wiki/model-defensive-contribution.md` for why: the literal
    end-of-season 5-match trailing snapshot is noisy at a single point and
    was observed, live, to give the OPPOSITE ordering for Man City vs
    Nottingham Forest specifically (round-38 snapshot: City 89.2 >
    Forest 86.8) versus the season-long average (City 69.0 < Forest 76.0,
    the expected direction) — a genuine finding, reported rather than
    hidden, not cherry-picked to make the demo work."""
    threshold_set = build_dc_threshold_set(store)
    params = fit_dc_model(store, as_of=as_of, threshold_set=threshold_set)
    table = build_training_table(store, as_of=as_of, threshold_set=threshold_set)

    player_rows = table.filter(pl.col("name").str.contains(player_name, literal=True))
    if player_rows.is_empty():
        print(f"No rows found for player name containing {player_name!r} in this store's DC-eligible positions.")
        return
    last_row = player_rows.sort("round").tail(1).to_dicts()[0]
    position = last_row["position"]
    feature_row = {c: last_row[c] for c in NUMERIC_FEATURE_COLUMNS_DC}

    print(f"\n=== Team-transfer effect: {player_name} ({position}), {from_team} -> {to_team} ===")
    print(f"own trailing features held fixed at his real {from_team}-era history: "
          f"player_trailing_count_3={feature_row['player_trailing_count_3']:.2f}, "
          f"_5={feature_row['player_trailing_count_5']:.2f}, _10={feature_row['player_trailing_count_10']:.2f}")

    season_mean_by_team: dict[str, float] = {}
    for team in (from_team, to_team):
        vals = (
            table.filter(pl.col("team") == team)
            .group_by("round")
            .agg(pl.col("team_trailing_dc_mean_5").first())
            .sort("round")["team_trailing_dc_mean_5"]
            .to_list()
        )
        if not vals:
            print(f"  WARNING: no rows found for team={team!r} -- check the exact store spelling.")
            continue
        season_mean_by_team[team] = sum(vals) / len(vals)
        last_round_val = table.filter(pl.col("team") == team).sort("round").tail(1)["team_trailing_dc_mean_5"][0]
        print(f"  {team}: season-mean team_trailing_dc_mean_5={season_mean_by_team[team]:.1f}  "
              f"(last-round-38 snapshot={last_round_val:.1f})")

    exposure = [(90.0, 1.0)]  # isolate the team-feature effect; not a minutes forecast
    for team, team_val in season_mean_by_team.items():
        fr = dict(feature_row)
        fr["team_trailing_dc_mean_5"] = team_val
        pmf = predict_dc_pmf(params, fr, element=999999, fixture=1, position=position, minute_exposure=exposure)
        print(f"  at {team} (season-mean team value {team_val:.1f}): "
              f"P(DC awarded)={pmf.p_dc_awarded():.4f}  E[count]={pmf.expected_count():.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--min-train-rows", type=int, default=500)
    parser.add_argument("--demo-write", action="store_true")
    parser.add_argument("--allow-live-season", action="store_true", default=False)
    parser.add_argument(
        "--show-team-transfer-effect",
        nargs=3,
        metavar=("PLAYER_NAME", "FROM_TEAM", "TO_TEAM"),
        default=None,
        help='e.g. --show-team-transfer-effect "Elliot Anderson" "Nott\'m Forest" "Man City" '
        "(team names must match this store's own vaastav team-name strings exactly).",
    )
    args = parser.parse_args()

    as_of = _parse_as_of(args.as_of)
    store = BitemporalStore()  # default path = data/store/, read-only in this script

    if args.show_team_transfer_effect:
        player_name, from_team, to_team = args.show_team_transfer_effect
        _run_team_transfer_demo(store, player_name, from_team, to_team, as_of)
        return 0

    threshold_set = build_dc_threshold_set(store)
    print("\n=== DC threshold provenance ===")
    for group in DC_GROUPS:
        thr = threshold_set.threshold(group)
        print(f"  {group}: count_threshold={thr.count_threshold}  verified={thr.verified}  source={thr.source[:80]}...")
    print(f"  points_by_position={threshold_set.points_by_position}")

    logger.info("building the leakage-safe training table, as_of=%s", as_of.isoformat())
    t0 = time.time()
    table = build_training_table(store, as_of=as_of, threshold_set=threshold_set, allow_live_season=args.allow_live_season)
    logger.info("training table: %d rows, %.2fs", table.height, time.time() - t0)

    print("\n=== DC model: headline fit ===")
    t0 = time.time()
    params = fit_dc_model(store, as_of=as_of, threshold_set=threshold_set)
    print(f"fit time: {time.time() - t0:.2f}s")
    for group, gp in params.groups.items():
        print(f"  {group}: n_rows_used={gp.n_rows_used}  alpha(1/r)={1.0 / gp.r:.4f}  r={gp.r:.4f}")

    calibration_by_group: dict[str, CalibrationReference] = {}
    print("\n=== §7.1 gate: walk-forward P(DC awarded) calibration (within 2025-26) ===")
    for group in DC_GROUPS:
        t0 = time.time()
        result = walk_forward_validate(table, group=group, threshold_set=threshold_set, min_train_rows=args.min_train_rows)
        print(f"\n--- {group} --- ({time.time() - t0:.2f}s, n_folds={result.n_folds}, n_eval={len(result.y_true)})")
        print(f"{'method':<24}{'log-loss':>12}{'brier':>12}")
        print(f"{'model (NB)':<24}{result.model_log_loss():>12.4f}{result.model_brier():>12.4f}")
        print(f"{'baseline: group rate':<24}{result.baseline_group_rate_log_loss():>12.4f}{result.baseline_group_rate_brier():>12.4f}")
        print(f"{'baseline: player trail':<24}{result.baseline_player_trailing_log_loss():>12.4f}{result.baseline_player_trailing_brier():>12.4f}")
        gate_passed = result.beats_both_baselines()
        print(f"§7.1 GATE: {'PASSED' if gate_passed else 'FAILED'} (must beat both baselines on both metrics)")
        print(f"OOS count residual: mean={result.residual_mean():.4f}  std={result.residual_std():.4f}  skewness={result.residual_skewness():.4f}")
        print(f"  (skewness ~0 would be near-Gaussian; this is NOT that -- see module docstring / wiki)")

        calibration_by_group[group] = CalibrationReference(
            reference=(
                f"OUT-OF-SAMPLE walk-forward residual (actual count minus fitted NB mean) over "
                f"{len(result.y_true)} minutes>0 eval rows, {result.n_folds} folds, within 2025-26 "
                f"(the only season with DC counters) -- blueprint §7.1's Phase 2 gate."
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
            fr = {c: row[c] for c in NUMERIC_FEATURE_COLUMNS_DC}
            pmf = predict_dc_pmf(
                params, fr, element=row["element"], fixture=row["fixture"], position=row["position"],
                minute_exposure=[(float(row["minutes"]), 1.0)] if row["minutes"] > 0 else [(0.0, 1.0)],
            )
            pmfs.append(pmf)
        result = write_dc_pmfs(
            tmp_store, pmfs, season="2025-26", round_=last_round, valid_at=as_of,
            calibration_by_group=calibration_by_group,
        )
        for group, write_result in result.items():
            print(f"\ndemo-write [{group}]: written={write_result.written} n_rows={write_result.n_rows}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
