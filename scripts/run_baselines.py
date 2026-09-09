#!/usr/bin/env python
"""Phase 1 gate — run the three baselines across every usable season in the
real store and report totals + distributions (blueprint §7.2).

Read-only against `data/store/` (never writes). Deterministic and seeded:
the random baseline's seed is a CLI argument, defaulted and logged, so a
result is reproducible from a commit hash plus that seed (CLAUDE.md rule 7).

Usage:
    python scripts/run_baselines.py
    python scripts/run_baselines.py --seed 7 --seasons 2024-25,2025-26
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.backtest.baselines import (  # noqa: E402
    GreedyFormBaseline,
    RandomBaseline,
    TemplateBaseline,
)
from fplai.backtest.data import SeasonDataError, load_season  # noqa: E402
from fplai.backtest.replay import SeasonReplay  # noqa: E402
from fplai.backtest.report import summarise  # noqa: E402
from fplai.backtest.rules import rules_for_season  # noqa: E402
from fplai.store import BitemporalStore  # noqa: E402

# 2019-20 is excluded outright: only 29/38 gameweeks are in the store (a
# verified ingestion gap, not the season's real disruption — see this
# session's punch-card finding and docs/wiki/phase1-baselines.md). Reporting
# a "season total" over 76% of a season would misrepresent it next to the
# other six, which are complete or near-complete.
ALL_SEASONS = ("2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26")

# The human reference point (task brief / blueprint §7.2). 2025-26 is
# in-progress in the store (partial), so no full-season figure to compare
# to yet for it; the other two are complete historical seasons.
USER_REFERENCE_SCORES = {
    "2025-26": 2019,
    "2024-25": 2251,
    "2023-24": 2169,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42, help="Random baseline seed (default 42)")
    parser.add_argument(
        "--seasons",
        type=str,
        default=",".join(ALL_SEASONS),
        help=f"Comma-separated seasons to run (default: {','.join(ALL_SEASONS)})",
    )
    args = parser.parse_args()
    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]

    store = BitemporalStore()

    print(f"Phase 1 baselines — seed={args.seed}")
    print(f"Excluded: 2019-20 (store holds only 29/38 gameweeks — ingestion gap, see finding)")
    print()

    all_summaries = []
    for season in seasons:
        try:
            season_data = load_season(store, season)
        except SeasonDataError as exc:
            print(f"{season}: SKIPPED — {exc}")
            continue

        cov = season_data.coverage
        print(f"=== {season} — {cov.describe()} ===")
        rules = rules_for_season(season)
        replay = SeasonReplay(season_data, rules)

        strategies = [
            RandomBaseline(seed=args.seed, rules=rules),
            TemplateBaseline(rules=rules),
            GreedyFormBaseline(rules=rules),
        ]
        for strategy in strategies:
            results = replay.run(strategy)
            summary = summarise(season, strategy.name, results)
            all_summaries.append(summary)
            print("  " + summary.describe())

        if season in USER_REFERENCE_SCORES:
            print(f"  human reference (real recorded season score): {USER_REFERENCE_SCORES[season]}")
        print()

    print("=== Summary table (total points) ===")
    header = f"{'season':10} {'strategy':22} {'total':>7} {'GWs':>5} {'mean':>7} {'stdev':>7}"
    print(header)
    for s in all_summaries:
        print(f"{s.season:10} {s.strategy_name:22} {s.total_points:7d} {s.n_gameweeks:5d} {s.mean:7.1f} {s.stdev:7.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
