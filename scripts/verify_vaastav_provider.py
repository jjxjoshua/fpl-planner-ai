#!/usr/bin/env python
"""One-off manual verification of the vaastav archive adapter (E2b story
10) AND the season-scoped identity fix (blueprint §12.5) against LIVE data
— not a scheduled entry point, not run in CI, not part of the test suite
(all network access there is mocked). Run by hand to re-confirm the
adapter and the identity fix still work against the real archive.

**This is the story's acceptance test, run live.** `scripts/
verify_pl_provider.py` (E2b stories 6-7) demonstrated that resolving a
2025/26 match's lineups against the CURRENT (2026/27) FPL elements
snapshot correctly RAISES `IdentityError` on players who have since left
the league — correct behaviour against the wrong snapshot (blueprint
§12.5). This script demonstrates the fix: the SAME match, resolved against
a `player.identity@season` snapshot for the match's OWN season (2025-26,
fetched from the vaastav archive), succeeds.

Live requests made (all cached under cache/vaastav/ and cache/pl_api/,
both gitignored — a second run makes zero live requests):
  1. FPL bootstrap-static/ (current elements+teams, for TEAM identity only
     — team identity was never broken for this match; see inline comments)
  2. PL API matchweek-1 fixtures, identity_season (team-identity bootstrap)
  3. vaastav data/2025-26/players_raw.csv (season-scoped PLAYER identity)
  4. PL API /v3/matches/{id}/lineups (the match under test)
  5. vaastav data/2025-26/gws/gw1.csv (player.gameweek_stats@gameweek,
     sanity-checking the OTHER capability this story adds, live)

Usage:
    python scripts/verify_vaastav_provider.py [--match-id ID] [--season 2025-26] [--pl-season 2025] [--identity-season 2026]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.client import FPLClient  # noqa: E402
from fplai.identity import IdentityError, IdentityResolver, build_player_identity_map_for_season  # noqa: E402
from fplai.providers.fpl import FPLProvider  # noqa: E402
from fplai.providers.pl import PLProvider  # noqa: E402
from fplai.providers.vaastav import VaastavProvider  # noqa: E402
from fplai.schemas import (  # noqa: E402
    MATCH_LINEUPS_MATCH,
    PLAYER_ATTRIBUTES_CURRENT,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    PLAYER_IDENTITY_SEASON,
    TEAM_ATTRIBUTES_CURRENT,
)
from fplai.transport import BulkFilePolicy, FileTransport, HttpTransport, RatePolicy  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("verify_vaastav_provider")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PL_CACHE_DIR = _PROJECT_ROOT / "cache" / "pl_api"
_VAASTAV_CACHE_DIR = _PROJECT_ROOT / "cache" / "vaastav"

# CLAUDE.md rule 4 note: this IS the live-season identifier (what season is
# "current"), not a scoring/price/rule value — same category of constant as
# providers/pl.py's PL_COMPETITION_ID. Still passed as a default that can
# be overridden, never assumed deep inside a function.
CURRENT_SEASON_LABEL = "2026-27"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--match-id", default="2562265", help="a completed 2025/26 match id (default: Brighton 0-3 Man Utd, GW38 — same match as verify_pl_provider.py's default)")
    parser.add_argument("--season", default="2025-26", help="vaastav/FPL-convention season string of the MATCH being resolved")
    parser.add_argument("--pl-season", default="2025", help="PL API starting-year season id of the same match")
    parser.add_argument("--identity-season", default="2026", help="PL API season id whose matchweek-1 fixtures verify TEAM identity (must match the CURRENT live elements/teams snapshot)")
    args = parser.parse_args()

    # -- 1. vaastav archive provider ---------------------------------------
    file_transport = FileTransport(cache_dir=_VAASTAV_CACHE_DIR, policy=BulkFilePolicy())
    vaastav = VaastavProvider(file_transport)

    # player.gameweek_stats@gameweek — the other capability this story
    # adds, fetched live for its own sake (not needed for the identity
    # acceptance test below, but this is the only live proof it works
    # end-to-end against the real archive rather than mocked bytes).
    gw_result = vaastav.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, season=args.season, gameweek=1)
    logger.info(
        "player.gameweek_stats@gameweek: season=%s gw=1 -> %d rows, endpoint=%s, content_hash=%s",
        args.season, gw_result.rows.height, gw_result.endpoint, gw_result.content_hash[:12],
    )
    top_selected = gw_result.rows.sort("selected", descending=True).head(1).to_dicts()[0]
    logger.info(
        "  most-selected player GW1 %s: %s, selected=%s (raw count), value=%s (x10)",
        args.season, top_selected["name"], top_selected["selected"], top_selected["value"],
    )

    # -- 2. current live FPL snapshot (TEAM identity only) -----------------
    # Team identity for Brighton (36) and Man Utd (1) was NEVER the problem
    # for this match (docs/wiki/provider-framework.md §9.4 — both are
    # current-season clubs) — reusing the CURRENT snapshot for team
    # identity here is correct, not an oversight. Only PLAYER identity
    # needs the season-scoped fix, demonstrated in step 3.
    fpl_provider = FPLProvider(client=FPLClient())
    current_elements = fpl_provider.fetch(PLAYER_ATTRIBUTES_CURRENT).rows
    current_teams = fpl_provider.fetch(TEAM_ATTRIBUTES_CURRENT).rows
    logger.info("current (%s) FPL snapshot: %d elements, %d teams", CURRENT_SEASON_LABEL, current_elements.height, current_teams.height)

    pl_transport = HttpTransport(
        base_url="https://sdp-prem-prod.premier-league-prod.pulselive.com/api/",
        policy=RatePolicy(requests_per_second=2.0, jitter_fraction=0.20),
        cache_dir=_PL_CACHE_DIR,
    )
    team_bootstrap_provider = PLProvider(
        pl_transport, season=args.pl_season, identity_season=args.identity_season,
        elements=current_elements, teams=current_teams,
    )
    team_map = team_bootstrap_provider.identity().teams
    logger.info("team identity (unaffected by this story): %d/%d FPL teams resolved", len(team_map.fpl_code_to_pl_id), current_teams.height)

    # -- 3. THE FIX: season-scoped PLAYER identity, from the archive -------
    season_player_map = build_player_identity_map_for_season(
        args.season,
        current_season=CURRENT_SEASON_LABEL,
        archive_elements_fetch=lambda season: vaastav.fetch(PLAYER_IDENTITY_SEASON, season=season).rows,
    )
    logger.info("season-scoped (%s) player identity: %d players resolved from the ARCHIVE, not today's snapshot", args.season, len(season_player_map.code_to_element_id))

    season_scoped_resolver = IdentityResolver(players=season_player_map, teams=team_map)
    fixed_provider = PLProvider(pl_transport, season=args.pl_season, identity_season=args.identity_season, identity=season_scoped_resolver)

    # -- 4. BEFORE: same match, CURRENT-season identity — must still raise -
    before_provider = PLProvider(
        pl_transport, season=args.pl_season, identity_season=args.identity_season,
        elements=current_elements, teams=current_teams,
    )
    try:
        before_provider.fetch(MATCH_LINEUPS_MATCH, match_id=args.match_id)
        logger.error("BEFORE case unexpectedly succeeded — expected IdentityError against CURRENT identity")
        return 1
    except IdentityError as exc:
        logger.info("BEFORE (current-season identity, as verify_pl_provider.py always did): EXPECTED IdentityError — %s", str(exc)[:200])

    # -- 5. AFTER: THIS is the story's acceptance test ----------------------
    after = fixed_provider.fetch(MATCH_LINEUPS_MATCH, match_id=args.match_id)
    n_start = after.rows.filter(after.rows["role"] == "start").height
    n_bench = after.rows.filter(after.rows["role"] == "bench").height
    logger.info(
        "AFTER (season-scoped %s identity, from vaastav): SUCCEEDED — %d rows (%d starters, %d bench), 0 unresolved",
        args.season, after.rows.height, n_start, n_bench,
    )
    logger.info("ACCEPTANCE TEST PASSED: a 2025/26 match now resolves because it resolves against 2025/26 identity.")
    logger.info("VERIFICATION RUN COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
