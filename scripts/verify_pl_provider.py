#!/usr/bin/env python
"""One-off manual verification of the PL API provider (E2b stories 6-7)
against LIVE data — not a scheduled entry point, not run in CI, not part
of the test suite (all network access there is mocked). Run by hand when
you need to re-confirm the adapter still works against the real API.

Deliberately gentle (blueprint §3.6): one identity-bootstrap request, one
lineups fetch, one substitutions fetch, one current-season fixtures fetch,
one team-match-stats fetch, one player-season-stats fetch, one officials
fetch (added session s004) — seven live requests total, all cached under
cache/pl_api/ (gitignored) so a second run makes zero live requests.

By default fetches a completed 2025/26 match against the CURRENT (2026/27)
identity snapshot. Expect match.lineups@match and match.substitutions@match
to raise IdentityError for that combination — that is real, expected
behaviour (blueprint §12.5), not a bug; see the inline comments below for
why, and docs/wiki/provider-framework.md for the live record.

Usage:
    python scripts/verify_pl_provider.py [--match-id ID] [--season YYYY] [--identity-season YYYY]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.client import FPLClient  # noqa: E402
from fplai.identity import IdentityError  # noqa: E402
from fplai.providers.fpl import FPLProvider  # noqa: E402
from fplai.providers.pl import PLProvider  # noqa: E402
from fplai.schemas import (  # noqa: E402
    MATCH_FIXTURES_MATCHWEEK,
    MATCH_LINEUPS_MATCH,
    MATCH_OFFICIALS_MATCH,
    MATCH_SUBSTITUTIONS_MATCH,
    PLAYER_ATTRIBUTES_CURRENT,
    PLAYER_SEASON_STATS_SEASON,
    TEAM_ATTRIBUTES_CURRENT,
    TEAM_MATCH_STATS_MATCH,
)
from fplai.transport import HttpTransport, RatePolicy  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("verify_pl_provider")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PL_CACHE_DIR = _PROJECT_ROOT / "cache" / "pl_api"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--match-id", default="2562265", help="a completed 2025/26 match id (default: Brighton 0-3 Man Utd, GW38)")
    parser.add_argument("--season", default="2025", help="PL API season id of the MATCH being fetched, starting-year convention (default: 2025 = 2025/26)")
    parser.add_argument(
        "--identity-season",
        default="2026",
        help=(
            "PL API season id to verify TEAM identity against — MUST match "
            "the season the live 'elements'/'teams' FPL snapshot describes "
            "(the CURRENT season, not necessarily --season). Update this "
            "each season; see PLProvider's docstring for why the two are "
            "kept separate."
        ),
    )
    args = parser.parse_args()

    # Identity needs a real, current FPL elements/teams snapshot — fetch it
    # through the existing, tested FPL provider (live=True, cached).
    fpl_provider = FPLProvider(client=FPLClient())
    elements = fpl_provider.fetch(PLAYER_ATTRIBUTES_CURRENT).rows
    teams = fpl_provider.fetch(TEAM_ATTRIBUTES_CURRENT).rows
    logger.info("FPL snapshot: %d elements, %d teams", elements.height, teams.height)

    transport = HttpTransport(
        base_url="https://sdp-prem-prod.premier-league-prod.pulselive.com/api/",
        policy=RatePolicy(requests_per_second=2.0, jitter_fraction=0.20),
        cache_dir=_PL_CACHE_DIR,
    )
    provider = PLProvider(
        transport, season=args.season, identity_season=args.identity_season, elements=elements, teams=teams
    )

    resolver = provider.identity()
    logger.info(
        "identity resolved: %d players, %d teams (both directions)",
        len(resolver.players.code_to_element_id),
        len(resolver.teams.fpl_code_to_pl_id),
    )

    # match.lineups@match and match.substitutions@match need EVERY player in
    # the match to resolve against the CURRENT elements snapshot. For a
    # 2025/26 archival match against a 2026/27 pre-season snapshot this
    # routinely — not rarely — hits a player who has left the Premier
    # League since (confirmed live across 3 different GW38 fixtures while
    # building this script). That is blueprint §12.5 working exactly as
    # specified, not a bug; caught and logged here rather than aborting the
    # whole verification run, so the other three capabilities (which do not
    # need full-squad resolution) still get their own live proof below.
    try:
        lineups = provider.fetch(MATCH_LINEUPS_MATCH, match_id=args.match_id)
        logger.info(
            "match.lineups@match: %d rows, %d starters, %d bench, provenance=%s",
            lineups.rows.height,
            lineups.rows.filter(lineups.rows["role"] == "start").height,
            lineups.rows.filter(lineups.rows["role"] == "bench").height,
            (lineups.provider_id, lineups.endpoint, lineups.content_hash[:12]),
        )
    except IdentityError as exc:
        logger.warning("match.lineups@match: EXPECTED IdentityError for a historical match — %s", exc)

    try:
        subs = provider.fetch(MATCH_SUBSTITUTIONS_MATCH, match_id=args.match_id)
        logger.info("match.substitutions@match: %d rows", subs.rows.height)
    except IdentityError as exc:
        logger.warning("match.substitutions@match: EXPECTED IdentityError for a historical match — %s", exc)

    # match.officials@match needs NO identity resolution at all (schema
    # comment — no numeric id exists for an official anywhere in this
    # payload), so unlike lineups/substitutions above this is never expected
    # to raise IdentityError for a historical match — a real failure here is
    # a real bug, not blueprint §12.5 working as intended.
    officials = provider.fetch(MATCH_OFFICIALS_MATCH, match_id=args.match_id)
    logger.info(
        "match.officials@match: %d rows, provenance=%s",
        officials.rows.height,
        (officials.provider_id, officials.endpoint, officials.content_hash[:12]),
    )
    for row in officials.rows.to_dicts():
        logger.info("  role=%s is_referee=%s name=%s", row["role"], row["is_referee"], row["official_name"])

    # match.fixtures@matchweek resolves BOTH teams of EVERY fixture in the
    # batch eagerly — a historical matchweek (e.g. GW38 2025/26) routinely
    # contains a fixture between two clubs since relegated, which raises for
    # the WHOLE matchweek even though --match-id's own two teams (Brighton,
    # Man Utd — both still in the Premier League) would resolve fine on
    # their own. Confirmed live while building this script (Burnley v Wolves
    # elsewhere in the same GW38). This is §12.5 working as specified, but
    # it is a real "no partial batch" design tension worth flagging for
    # story 11 (backfill orchestrator) — see this run's report. Demonstrated
    # here instead against --identity-season's OWN matchweek 1 (all 20
    # current clubs, guaranteed to resolve) — which is also the actually
    # representative near-term use case: capturing THIS season's fixtures
    # as they are played, not backfilling relegated clubs.
    fixtures = provider.fetch(MATCH_FIXTURES_MATCHWEEK, season=args.identity_season, matchweek=1)
    logger.info(
        "match.fixtures@matchweek: %d fixtures in season %s GW1 (all 20 current clubs)",
        fixtures.rows.height,
        args.identity_season,
    )
    for row in fixtures.rows.head(3).to_dicts():
        logger.info(
            "  match_id=%s kickoff=%s home_code=%s away_code=%s (%s v %s)",
            row["match_id"], row["kickoff"], row["home_team_code"], row["away_team_code"],
            row["home_team_name_pl"], row["away_team_name_pl"],
        )

    # Brighton=36, Man Utd=1 — both current-season clubs, known to resolve
    # (unlike the matchweek batch above); passed straight through rather
    # than re-deriving from a fixtures fetch of --match-id's own (historical
    # and partially-unresolvable) matchweek.
    stats = provider.fetch(TEAM_MATCH_STATS_MATCH, match_id=args.match_id, home_team_code=36, away_team_code=1)
    logger.info("team.match_stats@match: %d rows (long format), meta=%s", stats.rows.height, stats.meta)
    xg_rows = stats.rows.filter(stats.rows["stat_key"] == "expectedGoals")
    for row in xg_rows.to_dicts():
        logger.info("  expectedGoals side=%s team_code=%s value=%.4f", row["side"], row["team_code"], row["value"])

    # Deliberately independent of the lineups fetch above (which may have
    # raised) — any CURRENT element resolves for player.season_stats, since
    # this capability doesn't require the player to have appeared in
    # --match-id at all.
    a_player_row = elements.row(0, named=True)
    season_stats = provider.fetch(
        PLAYER_SEASON_STATS_SEASON, player_element_id=a_player_row["id"], season=args.season
    )
    logger.info(
        "player.season_stats@season: element_id=%d (%s), %d stat rows",
        a_player_row["id"],
        a_player_row.get("web_name"),
        season_stats.rows.height,
    )

    logger.info("VERIFICATION RUN COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
