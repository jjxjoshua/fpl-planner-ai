#!/usr/bin/env python
"""One-off manual verification of season-scoped TEAM identity (E2b story 7b,
blueprint §12.5's player/team asymmetry) against LIVE data — not a scheduled
entry point, not run in CI, not part of the test suite (all network access
there is mocked). Run by hand to re-confirm the fix still works against the
real archive and the real PL API.

**This is story 7b's acceptance test, run live.** `scripts/
verify_vaastav_provider.py` (E2b story 10) fixed PLAYER identity —
resolving a 2025/26 match against a season-scoped `player.identity@season`
snapshot instead of today's. It deliberately left TEAM identity alone
because the match it used (Brighton vs Man Utd) involves only clubs still
in the 2026/27 league — team codes persisting across seasons made the OLD
(current-snapshot-only) team identity "happen to work" for that match,
masking the same missing-snapshot bug players had (see
docs/wiki/provider-framework.md §10.4/§11.4 for the two-part paper trail).

Story 11 (the backfill orchestrator) proved the gap live: `--seasons 2025`
(2025/26) halted with `IdentityError` on PL numeric team id 21 — West Ham,
relegated since, absent from the CURRENT (2026/27) 20-club list.

This script reproduces that halt from first principles (step 3, using the
OLD current-snapshot-only construction) and then demonstrates the fix
(step 4, using `fplai.identity.build_team_identity_map_for_season` fed by
`providers/vaastav.py`'s new `team.identity@season`) against the SAME
season and the SAME club.

Live requests made (all cached under cache/vaastav/ and cache/pl_api/,
both gitignored — a second run makes zero live requests):
  1. FPL bootstrap-static/ (current elements+teams — the live branch both
     identities fall back to)
  2. PL API 2025/26 matchweek-1 fixtures (season-MATCHED pl_teams — the
     actual fix; the old code reused step 2's twin from the CURRENT
     season instead)
  3. vaastav data/2025-26/teams.csv (season-scoped FPL-side team list —
     the other half of the fix)
  4. vaastav data/2025-26/players_raw.csv (season-scoped PLAYER identity,
     needed to fetch the lineup in step 5 at all — story 10, unchanged)
  5. PL API /v3/matches/{id}/lineups for West Ham's own MW1 2025/26 fixture

Usage:
    python scripts/verify_team_identity_season.py [--pl-season 2025] [--season 2025-26] [--match-id 2561899]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.client import FPLClient  # noqa: E402
from fplai.identity import (  # noqa: E402
    IdentityError,
    IdentityResolver,
    PlayerIdentityMap,
    build_player_identity_map_for_season,
    build_team_identity_map_for_season,
)
from fplai.providers.fpl import FPLProvider  # noqa: E402
from fplai.providers.pl import PLProvider  # noqa: E402
from fplai.providers.vaastav import VaastavProvider  # noqa: E402
from fplai.schemas import (  # noqa: E402
    MATCH_FIXTURES_MATCHWEEK,
    MATCH_LINEUPS_MATCH,
    PLAYER_ATTRIBUTES_CURRENT,
    PLAYER_IDENTITY_SEASON,
    TEAM_ATTRIBUTES_CURRENT,
    TEAM_IDENTITY_SEASON,
)
from fplai.transport import BulkFilePolicy, FileTransport, HttpTransport, RatePolicy  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("verify_team_identity_season")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PL_CACHE_DIR = _PROJECT_ROOT / "cache" / "pl_api"
_VAASTAV_CACHE_DIR = _PROJECT_ROOT / "cache" / "vaastav"

# CLAUDE.md rule 4 note: this IS the live-season identifier, not a
# scoring/price/rule value — same category of constant as
# providers/pl.py's PL_COMPETITION_ID / verify_vaastav_provider.py's
# CURRENT_SEASON_LABEL. Still a default, always overridable.
CURRENT_SEASON_PL = "2026"
CURRENT_SEASON_ARCHIVE = "2026-27"

# West Ham United — code 21. Verified live, story 11: present in
# vaastav's 2025-26/teams.csv, ABSENT from the current (2026/27) 20-club
# FPL bootstrap-static snapshot (relegated since). This is the club that
# makes the acceptance test real, not a club still in the league.
WEST_HAM_CODE = 21


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pl-season", default="2025", help="PL API starting-year season id (2025 = 2025/26, the season West Ham last played top flight)")
    parser.add_argument("--season", default="2025-26", help="vaastav/FPL-convention season string for the same season")
    parser.add_argument("--match-id", default="2561899", help="West Ham's own 2025/26 MW1 fixture (Sunderland vs West Ham) — must be a match West Ham actually played in, not just any 2025/26 match")
    args = parser.parse_args()

    # -- 1. current live FPL snapshot (the live branch both identities fall back to) --
    fpl_provider = FPLProvider(client=FPLClient())
    current_elements = fpl_provider.fetch(PLAYER_ATTRIBUTES_CURRENT).rows
    current_teams = fpl_provider.fetch(TEAM_ATTRIBUTES_CURRENT).rows
    logger.info("current (%s) FPL snapshot: %d elements, %d teams", CURRENT_SEASON_ARCHIVE, current_elements.height, current_teams.height)
    current_codes = set(current_teams["code"].to_list())
    assert WEST_HAM_CODE not in current_codes, (
        f"West Ham (code {WEST_HAM_CODE}) unexpectedly present in the CURRENT snapshot — "
        "this script needs a genuinely relegated club to be a real test (blueprint §12.5's "
        "own instruction); pick a different club/season if this assertion ever fires."
    )
    logger.info("confirmed: West Ham (code %d) is NOT in the current %s snapshot — a real test.", WEST_HAM_CODE, CURRENT_SEASON_ARCHIVE)

    pl_transport = HttpTransport(
        base_url="https://sdp-prem-prod.premier-league-prod.pulselive.com/api/",
        policy=RatePolicy(requests_per_second=2.0, jitter_fraction=0.20),
        cache_dir=_PL_CACHE_DIR,
    )
    file_transport = FileTransport(cache_dir=_VAASTAV_CACHE_DIR, policy=BulkFilePolicy())
    vaastav = VaastavProvider(file_transport)

    # -- 2. season-MATCHED pl_teams — the actual fix's first half ----------
    # THIS season's (2025/26's) own matchweek-1 fixtures, not the current
    # (2026/27) season's. The pre-fix code always used the CURRENT season
    # here regardless of which season's identity was being built.
    season_matched_probe = PLProvider(
        pl_transport, season=args.pl_season, identity_season=args.pl_season,
        elements=current_elements, teams=current_teams,  # unused placeholders, see scripts/backfill.py's pl_teams_for_season
    )
    season_matched_pl_teams = season_matched_probe._fetch_pl_teams_for_identity()
    assert any(t["id"] == str(WEST_HAM_CODE) for t in season_matched_pl_teams), (
        f"expected PL API team id {WEST_HAM_CODE} in {args.pl_season}'s own matchweek-1 fixtures"
    )
    logger.info("season-matched (%s) pl_teams: %d clubs, including West Ham (PL id %d) — confirmed", args.pl_season, len(season_matched_pl_teams), WEST_HAM_CODE)

    # -- 3. BEFORE: the OLD bug, reproduced byte-for-byte -------------------
    # This is exactly scripts/backfill.py's PRE-fix `_build_pl_factory`
    # shape (also independently reproduced live this session by stashing
    # that file and running the real CLI — see this story's punch-card/
    # wiki record for that transcript): team identity is bootstrapped ONCE
    # against the CURRENT season's own matchweek-1 fixtures (so the BUILD
    # step below succeeds — Coventry/Hull/Ipswich are all there, nothing
    # is missing), then reused to fetch a HISTORICAL (2025/26) season's
    # fixtures — which is where West Ham's PL team id first appears and
    # has no counterpart in that CURRENT-only map.
    old_team_bootstrap = PLProvider(
        pl_transport, season=CURRENT_SEASON_PL, identity_season=CURRENT_SEASON_PL,
        elements=current_elements, teams=current_teams,
    )
    old_team_map = old_team_bootstrap.identity().teams  # succeeds — verified against ITS OWN (current) season
    logger.info("OLD team identity, built against the CURRENT (%s) season only: %d/%d resolved", CURRENT_SEASON_PL, len(old_team_map.fpl_code_to_pl_id), current_teams.height)

    # Player identity is irrelevant to this step (`_fetch_fixtures` never
    # touches it) — the CURRENT elements snapshot is a harmless placeholder,
    # not the thing under test here.
    placeholder_player_map = PlayerIdentityMap.build(current_elements)
    old_style_provider = PLProvider(
        pl_transport, season=args.pl_season, identity_season=CURRENT_SEASON_PL,
        identity=IdentityResolver(players=placeholder_player_map, teams=old_team_map),
    )
    try:
        old_style_provider.fetch(MATCH_FIXTURES_MATCHWEEK, matchweek=1)
        logger.error("BEFORE case unexpectedly succeeded — expected IdentityError (West Ham has no current-season FPL row)")
        return 1
    except IdentityError as exc:
        logger.info("BEFORE (current-season TEAM identity reused for a %s fetch, story 11's exact bug): EXPECTED IdentityError — %s", args.pl_season, str(exc)[:260])

    # -- 4. AFTER: THE FIX — season-scoped FPL-side team list too ----------
    season_team_map = build_team_identity_map_for_season(
        args.season,
        current_season=CURRENT_SEASON_ARCHIVE,
        pl_teams=season_matched_pl_teams,
        archive_teams_fetch=lambda season: vaastav.fetch(TEAM_IDENTITY_SEASON, season=season).rows,
    )
    logger.info(
        "season-scoped (%s) team identity: %d/%d FPL teams resolved from the ARCHIVE, not today's snapshot",
        args.season, len(season_team_map.fpl_code_to_pl_id), len(season_matched_pl_teams),
    )
    west_ham_pl_id = season_team_map.resolve_fpl_to_pl(WEST_HAM_CODE)
    logger.info("AFTER: West Ham (FPL code %d) resolves to PL API team id %s — SUCCEEDED", WEST_HAM_CODE, west_ham_pl_id)

    # -- 5. End-to-end: fetch a real West Ham lineup with the fixed identity --
    season_player_map = build_player_identity_map_for_season(
        args.season,
        current_season=CURRENT_SEASON_ARCHIVE,
        archive_elements_fetch=lambda season: vaastav.fetch(PLAYER_IDENTITY_SEASON, season=season).rows,
    )
    fixed_resolver = IdentityResolver(players=season_player_map, teams=season_team_map)
    fixed_provider = PLProvider(pl_transport, season=args.pl_season, identity_season=args.pl_season, identity=fixed_resolver)

    lineups = fixed_provider.fetch(MATCH_LINEUPS_MATCH, match_id=args.match_id)
    wh_rows = lineups.rows.filter(lineups.rows["team_code"] == WEST_HAM_CODE)
    n_start = wh_rows.filter(wh_rows["role"] == "start").height
    n_bench = wh_rows.filter(wh_rows["role"] == "bench").height
    assert wh_rows.height > 0, f"expected West Ham rows in match {args.match_id}'s lineup — wrong match id?"
    logger.info(
        "END-TO-END: match %s lineup fetched — %d total rows, %d are West Ham (%d starters, %d bench)",
        args.match_id, lineups.rows.height, wh_rows.height, n_start, n_bench,
    )
    logger.info(
        "ACCEPTANCE TEST PASSED: a %s fixture involving a since-relegated club (West Ham, code %d) "
        "now resolves because TEAM identity resolves against %s's own snapshot, not today's.",
        args.season, WEST_HAM_CODE, args.season,
    )
    logger.info("VERIFICATION RUN COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
