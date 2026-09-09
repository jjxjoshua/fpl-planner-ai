#!/usr/bin/env python
"""Fit the Dixon-Coles team-strength model (blueprint §4, E5) against the
REAL store, read-only, and print a report — not a scheduled entry point,
not part of the test suite (`tests/test_team_strength.py` mocks/fabricates
everything it needs). Run by hand to see what the fit actually produces on
real data.

**Read-only against `data/store/` by design.** This task's OWNED PATHS
forbid `data/**` — this script never calls `write_team_strength` against
the real store. `--demo-write` exercises the derived-capability write path
(story 9's framework, blueprint §12.2) end-to-end, but ONLY against an
isolated temp directory (`tempfile.mkdtemp()`, never under `data/`),
proving the framework holds against a real fit's real output without
touching the one store this task must leave alone.

Usage:
    python scripts/fit_team_strength.py [--as-of 2026-08-21T17:30:00Z] [--half-life-days 180] [--demo-write]
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.identity import TeamNameCanonicalisationMap  # noqa: E402
from fplai.models.team_strength import (  # noqa: E402
    TeamStrengthConfig,
    fit_team_strength,
    predict_scoreline,
    write_team_strength,
)
from fplai.store import BitemporalStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fit_team_strength")


def _parse_as_of(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        default=datetime.now(timezone.utc).isoformat(),
        help="ISO-8601 UTC deadline -- only fixtures kicked off strictly before this are used "
        "(default: now, i.e. everything the store currently holds).",
    )
    parser.add_argument("--half-life-days", type=float, default=TeamStrengthConfig().half_life_days)
    parser.add_argument(
        "--goals-source-weight",
        type=float,
        default=TeamStrengthConfig().goals_source_weight,
        help="Relative weight of a goals-sourced (pre-2022-23) match vs an xG-sourced one.",
    )
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument(
        "--fixture",
        nargs=2,
        metavar=("HOME_TEAM", "AWAY_TEAM"),
        default=None,
        help="Print the full scoreline PMF for one fixture, e.g. --fixture 'Man City' 'Arsenal'.",
    )
    parser.add_argument(
        "--demo-write",
        action="store_true",
        help="Also demonstrate fplai.derived.write_derived end-to-end -- against an isolated "
        "temp store (tempfile.mkdtemp(), NEVER data/store/), not the real one.",
    )
    parser.add_argument(
        "--no-team-name-canonicalisation",
        action="store_true",
        help="Session s003: disable the Ipswich/Ipswich Town cross-season identity fix and fit "
        "on raw archive team-name strings, exactly as before this session -- for before/after "
        "comparison only, never the intended normal use.",
    )
    parser.add_argument("--allow-live-season", action="store_true", default=False)
    args = parser.parse_args()

    as_of = _parse_as_of(args.as_of)
    config = TeamStrengthConfig(half_life_days=args.half_life_days, goals_source_weight=args.goals_source_weight)

    store = BitemporalStore()  # default path = data/store/, read-only in this script
    live_teams = store.latest("teams")
    teams = live_teams["name"].to_list()
    logger.info("fitting against data/store/ (read-only), as_of=%s, %d current teams as the target universe", as_of.isoformat(), len(teams))

    canonicaliser = None
    if not args.no_team_name_canonicalisation:
        team_identity_rows = store.latest("vaastav_team_identity")
        canonicaliser = TeamNameCanonicalisationMap.build(team_identity_rows, live_teams=live_teams)
        logger.info(
            "team-name canonicalisation ON (session s003 fix): %d archive (season, name) pairs, "
            "%d codes with a canonical name",
            len(canonicaliser.season_name_to_code), len(canonicaliser.code_to_canonical_name),
        )

    params = fit_team_strength(store, as_of=as_of, teams=teams, config=config, team_name_canonicaliser=canonicaliser)

    print(f"\n=== Team strength fit: as_of={as_of.isoformat()} ===")
    print(f"matches used: {params.n_matches_used}  seasons: {', '.join(params.seasons_used)}")
    print(f"home_advantage={params.home_advantage:.4f}  rho={params.rho:.4f}")
    print(f"in-sample residual: mean={params.in_sample_residual_mean:.4f}  std={params.in_sample_residual_std:.4f}")
    if params.teams_with_no_history:
        print(f"promoted-team prior applied to: {', '.join(params.teams_with_no_history)}")

    ranked_attack = sorted(params.attack.items(), key=lambda kv: -kv[1])
    ranked_defence = sorted(params.defence.items(), key=lambda kv: kv[1])  # lower = better defence

    n = args.top_n
    print(f"\nTop {n} attack:")
    for team, value in ranked_attack[:n]:
        print(f"  {team:20s} {value:+.4f}")
    print(f"\nBottom {n} attack:")
    for team, value in ranked_attack[-n:]:
        print(f"  {team:20s} {value:+.4f}")

    print(f"\nBest {n} defence (lower = better):")
    for team, value in ranked_defence[:n]:
        print(f"  {team:20s} {value:+.4f}")
    print(f"\nWorst {n} defence:")
    for team, value in ranked_defence[-n:]:
        print(f"  {team:20s} {value:+.4f}")

    if args.fixture:
        home, away = args.fixture
        pmf = predict_scoreline(params, home, away)
        print(f"\n=== {home} v {away} ===")
        print(f"home_win={pmf.home_win:.3f}  draw={pmf.draw:.3f}  away_win={pmf.away_win:.3f}")
        print(f"E[home goals]={pmf.expected_home_goals():.3f}  E[away goals]={pmf.expected_away_goals():.3f}")
        print(f"most likely scoreline: {pmf.most_likely_scoreline()}")
        print(f"mass captured within max_goals={pmf.max_goals}: {pmf.mass_before_truncation:.6f}")

    if args.demo_write:
        scratch = Path(tempfile.mkdtemp(prefix="fplai_team_strength_demo_"))
        logger.info("demo-write: isolated temp store at %s (never data/store/)", scratch)
        demo_store = BitemporalStore(base_path=scratch / "store")
        result = write_team_strength(demo_store, params, season="2026-27", gameweek=1, source="scripts/fit_team_strength.py --demo-write")
        print(f"\n=== derived-capability write demo (temp store, not data/store/) ===")
        print(f"written={result.written}  n_rows={result.n_rows}  dataset={result.dataset}")
        readback = demo_store.as_of("derived_team_strength_rating", datetime.now(timezone.utc))
        print(f"read back: {readback.height} rows, is_modelled all True: {bool(readback['is_modelled'].all())}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
