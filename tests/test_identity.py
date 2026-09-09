"""Tests for fplai.identity — cross-provider identity resolution (E2b story
7, blueprint §12.5). No network — everything here works off small, literal
DataFrames/dicts standing in for FPL and PL API snapshots."""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from fplai.identity import (
    IdentityError,
    IdentityResolver,
    PlayerIdentityMap,
    TeamIdentityMap,
    TeamNameCanonicalisationMap,
    build_player_identity_map_for_season,
    build_team_identity_map_for_season,
    resolve_match,
)


def _elements_df(rows=None):
    rows = rows if rows is not None else [
        {"id": 1, "code": 223094, "opta_code": "p223094"},
        {"id": 2, "code": 489639, "opta_code": "p489639"},
    ]
    return pl.DataFrame(rows)


def _teams_df(rows=None):
    rows = rows if rows is not None else [
        {"code": 3, "name": "Arsenal"},
        {"code": 7, "name": "Aston Villa"},
        {"code": 91, "name": "Bournemouth"},
        {"code": 94, "name": "Brentford"},
    ]
    return pl.DataFrame(rows)


def _pl_teams(rows=None):
    return rows if rows is not None else [
        {"id": "3", "name": "Arsenal", "shortName": "Arsenal"},
        {"id": "7", "name": "Aston Villa", "shortName": "Aston Villa"},
        {"id": "91", "name": "Bournemouth", "shortName": "Bournemouth"},
        {"id": "94", "name": "Brentford", "shortName": "Brentford"},
    ]


# -- PlayerIdentityMap ------------------------------------------------------


def test_player_identity_map_builds_from_valid_elements():
    m = PlayerIdentityMap.build(_elements_df())
    assert m.resolve(223094) == 1
    assert m.resolve("489639") == 2  # string input, e.g. straight from JSON
    assert m.resolve_reverse(1) == 223094


def test_player_identity_map_raises_on_opta_code_mismatch():
    bad = _elements_df([{"id": 1, "code": 223094, "opta_code": "p999999"}])
    with pytest.raises(IdentityError):
        PlayerIdentityMap.build(bad)


def test_player_identity_map_raises_on_missing_opta_code():
    bad = _elements_df([{"id": 1, "code": 223094, "opta_code": None}])
    with pytest.raises(IdentityError):
        PlayerIdentityMap.build(bad)


def test_player_identity_map_raises_on_duplicate_code():
    dup = _elements_df(
        [
            {"id": 1, "code": 223094, "opta_code": "p223094"},
            {"id": 2, "code": 223094, "opta_code": "p223094"},
        ]
    )
    with pytest.raises(IdentityError):
        PlayerIdentityMap.build(dup)


def test_player_identity_map_missing_columns_raises():
    with pytest.raises(IdentityError):
        PlayerIdentityMap.build(pl.DataFrame({"id": [1]}))


def test_player_identity_map_resolve_raises_on_unresolved_player():
    # EXPECTED case: a PL API player id from an old match, not in the
    # current elements snapshot this map was built from.
    m = PlayerIdentityMap.build(_elements_df())
    with pytest.raises(IdentityError):
        m.resolve(999999999)


def test_player_identity_map_resolve_reverse_raises_on_unresolved_element():
    m = PlayerIdentityMap.build(_elements_df())
    with pytest.raises(IdentityError):
        m.resolve_reverse(9999)


# -- TeamIdentityMap ---------------------------------------------------------


def test_team_identity_map_builds_and_resolves_both_directions():
    m = TeamIdentityMap.build(_teams_df(), _pl_teams())
    assert m.resolve_fpl_to_pl(3) == "3"
    assert m.resolve_pl_to_fpl("3") == 3
    assert m.resolve_pl_to_fpl(94) == 94  # int input tolerated too


def test_team_identity_map_raises_naming_every_unresolved_team():
    pl_teams = _pl_teams()[:-1]  # drop Brentford (code 94)
    with pytest.raises(IdentityError, match="94"):
        TeamIdentityMap.build(_teams_df(), pl_teams)


def test_team_identity_map_resolve_raises_on_unknown_code():
    m = TeamIdentityMap.build(_teams_df(), _pl_teams())
    with pytest.raises(IdentityError):
        m.resolve_fpl_to_pl(999)


def test_team_identity_map_resolve_pl_to_fpl_raises_on_unknown_id():
    m = TeamIdentityMap.build(_teams_df(), _pl_teams())
    with pytest.raises(IdentityError):
        m.resolve_pl_to_fpl("999")


def test_team_identity_map_missing_columns_raises():
    with pytest.raises(IdentityError):
        TeamIdentityMap.build(pl.DataFrame({"code": [3]}), _pl_teams())


def test_team_identity_map_tolerates_naming_convention_differences():
    # 'Spurs' (FPL short name) vs 'Tottenham Hotspur' (PL API) must NOT
    # raise — the numeric id match is authoritative, name is a sanity
    # signal only (logged, never fatal on its own).
    fpl_teams = pl.DataFrame([{"code": 6, "name": "Spurs"}])
    pl_teams = [{"id": "6", "name": "Tottenham Hotspur", "shortName": "Spurs"}]
    m = TeamIdentityMap.build(fpl_teams, pl_teams)
    assert m.resolve_fpl_to_pl(6) == "6"


# -- resolve_match ------------------------------------------------------------


def _fixtures_df():
    return pl.DataFrame(
        {
            "match_id": ["2645195", "2645196"],
            "kickoff": [datetime(2026, 8, 21, 20, 0), datetime(2026, 8, 22, 17, 30)],
            "home_team_code": [3, 94],
            "away_team_code": [9, 6],
        }
    )


def test_resolve_match_finds_the_unique_fixture():
    match_id = resolve_match(
        _fixtures_df(), kickoff_date=date(2026, 8, 21), home_team_code=3, away_team_code=9
    )
    assert match_id == "2645195"


def test_resolve_match_raises_on_zero_matches():
    with pytest.raises(IdentityError):
        resolve_match(_fixtures_df(), kickoff_date=date(2026, 8, 21), home_team_code=999, away_team_code=9)


def test_resolve_match_raises_on_ambiguous_matches():
    dup = pl.concat([_fixtures_df(), _fixtures_df().head(1)])
    with pytest.raises(IdentityError):
        resolve_match(dup, kickoff_date=date(2026, 8, 21), home_team_code=3, away_team_code=9)


def test_resolve_match_missing_columns_raises():
    with pytest.raises(IdentityError):
        resolve_match(pl.DataFrame({"match_id": ["1"]}), kickoff_date=date(2026, 8, 21), home_team_code=3, away_team_code=9)


# -- IdentityResolver ---------------------------------------------------------


def test_identity_resolver_from_fpl_snapshots_bundles_both_maps():
    resolver = IdentityResolver.from_fpl_snapshots(_elements_df(), _teams_df(), _pl_teams())
    assert resolver.players.resolve(223094) == 1
    assert resolver.teams.resolve_fpl_to_pl(3) == "3"


# -- build_player_identity_map_for_season, E2b story 10 (blueprint §12.5) ---


def test_build_for_current_season_uses_live_elements():
    m = build_player_identity_map_for_season(
        "2026-27", current_season="2026-27", live_elements=_elements_df()
    )
    assert m.resolve(223094) == 1


def test_build_for_current_season_without_live_elements_raises():
    with pytest.raises(IdentityError):
        build_player_identity_map_for_season("2026-27", current_season="2026-27")


def test_build_for_historical_season_uses_archive_fetch():
    seen_seasons = []

    def archive_fetch(season: str):
        seen_seasons.append(season)
        return _elements_df([{"id": 9, "code": 111, "opta_code": "p111"}])

    m = build_player_identity_map_for_season(
        "2025-26", current_season="2026-27", archive_elements_fetch=archive_fetch
    )
    assert seen_seasons == ["2025-26"]
    assert m.resolve(111) == 9


def test_build_for_historical_season_without_archive_fetch_raises():
    with pytest.raises(IdentityError):
        build_player_identity_map_for_season("2025-26", current_season="2026-27")


def test_build_for_historical_season_never_touches_live_elements():
    # Even if a caller supplies BOTH, a historical season must resolve
    # against the archive, never silently fall back to today's snapshot —
    # that fallback is exactly the bug blueprint §12.5 was extended to
    # close (a 2025/26 match resolved against CURRENT elements, which
    # raises on any departed player for the WRONG reason).
    def archive_fetch(season: str):
        return _elements_df([{"id": 9, "code": 111, "opta_code": "p111"}])

    live = _elements_df()  # id=1..2, codes 223094/489639 — NOT id=9/code=111
    m = build_player_identity_map_for_season(
        "2025-26", current_season="2026-27", live_elements=live, archive_elements_fetch=archive_fetch
    )
    assert m.resolve(111) == 9
    with pytest.raises(IdentityError):
        m.resolve(223094)  # from `live`, not the archive — must NOT be in this map


# -- build_team_identity_map_for_season, E2b story 7b (blueprint §12.5) -----
#
# Symmetrical to the player tests above. `pl_teams` here stands in for the
# PL API's raw team list for the TARGET season — West Ham (code 21) is the
# real, live-verified example (story 11, docs/wiki/provider-framework.md
# §11.4): present in 2025/26's own team list, absent from the CURRENT
# (2026/27) one.


def _west_ham_pl_teams():
    return [
        {"id": "3", "name": "Arsenal", "shortName": "Arsenal"},
        {"id": "21", "name": "West Ham United", "shortName": "West Ham"},
    ]


def _west_ham_archive_teams():
    return pl.DataFrame(
        [
            {"code": 3, "name": "Arsenal", "short_name": "ARS", "id": 1},
            {"code": 21, "name": "West Ham", "short_name": "WHU", "id": 19},
        ]
    )


def test_build_team_for_current_season_uses_live_teams():
    m = build_team_identity_map_for_season(
        "2026-27", current_season="2026-27", pl_teams=_pl_teams(), live_teams=_teams_df()
    )
    assert m.resolve_fpl_to_pl(3) == "3"


def test_build_team_for_current_season_without_live_teams_raises():
    with pytest.raises(IdentityError):
        build_team_identity_map_for_season("2026-27", current_season="2026-27", pl_teams=_pl_teams())


def test_build_team_for_historical_season_uses_archive_fetch():
    seen_seasons = []

    def archive_fetch(season: str):
        seen_seasons.append(season)
        return _west_ham_archive_teams()

    m = build_team_identity_map_for_season(
        "2025-26",
        current_season="2026-27",
        pl_teams=_west_ham_pl_teams(),
        archive_teams_fetch=archive_fetch,
    )
    assert seen_seasons == ["2025-26"]
    assert m.resolve_fpl_to_pl(21) == "21"  # West Ham resolves from the ARCHIVE list


def test_build_team_for_historical_season_without_archive_fetch_raises():
    with pytest.raises(IdentityError):
        build_team_identity_map_for_season("2025-26", current_season="2026-27", pl_teams=_west_ham_pl_teams())


def test_build_team_for_historical_season_never_touches_live_teams():
    # Even if a caller supplies both, a historical season must resolve
    # against the archive, never silently fall back to today's snapshot —
    # exactly the bug this story closes for teams (players fixed in story
    # 10). Today's (2026/27) teams list does NOT contain West Ham at all —
    # if this silently fell back to `live_teams`, it would raise on West
    # Ham for the WRONG reason (missing from live) instead of resolving it
    # from the archive.
    m = build_team_identity_map_for_season(
        "2025-26",
        current_season="2026-27",
        pl_teams=_west_ham_pl_teams(),
        live_teams=_teams_df(),  # current 2026/27 clubs — no West Ham
        archive_teams_fetch=lambda season: _west_ham_archive_teams(),
    )
    assert m.resolve_fpl_to_pl(21) == "21"


def test_build_team_for_current_season_still_raises_on_pl_teams_season_mismatch():
    # The CURRENT-season branch, proven to still fail loudly (§12.5) when
    # given a pl_teams list from the WRONG season — a live snapshot's own
    # club (Brentford, code 94) with no counterpart in a pl_teams list that
    # only covers Arsenal + West Ham. This is the "correct raise, wrong
    # snapshot" shape story 11 found live (§9.3/§11.4): a season mismatch
    # between the FPL-side snapshot and pl_teams surfaces as SOME club
    # unresolved, not as the extra club being flagged — TeamIdentityMap.
    # build reports the FPL side's misses, which is what a caller actually
    # needs to diagnose "these two snapshots don't agree on a season".
    with pytest.raises(IdentityError, match="94"):
        build_team_identity_map_for_season(
            "2026-27", current_season="2026-27", pl_teams=_west_ham_pl_teams(), live_teams=_teams_df()
        )


# -- TeamNameCanonicalisationMap (session s003, the Ipswich/Ipswich Town fix) -


def _team_identity_archive_rows():
    """Two seasons, standing in for the real 2024-25/2025-26 vaastav
    archive shape: Ipswich's `code` (40) is stable, its `name` is not
    ("Ipswich" 2024-25). A second club (Arsenal, code 3) is included with a
    STABLE name across both seasons, to prove the map leaves an
    already-consistent club alone."""
    return pl.DataFrame(
        [
            {"season": "2023-24", "id": 10, "code": 40, "name": "Ipswich", "short_name": "IPS"},
            {"season": "2024-25", "id": 10, "code": 40, "name": "Ipswich", "short_name": "IPS"},
            {"season": "2023-24", "id": 1, "code": 3, "name": "Arsenal", "short_name": "ARS"},
            {"season": "2024-25", "id": 1, "code": 3, "name": "Arsenal", "short_name": "ARS"},
        ]
    )


def _live_teams_with_renamed_ipswich():
    return pl.DataFrame(
        [
            {"code": 40, "name": "Ipswich Town", "short_name": "IPS"},
            {"code": 3, "name": "Arsenal", "short_name": "ARS"},
        ]
    )


def test_team_name_canonicalisation_maps_both_archive_seasons_to_the_live_name():
    m = TeamNameCanonicalisationMap.build(_team_identity_archive_rows(), live_teams=_live_teams_with_renamed_ipswich())
    assert m.canonicalise("2023-24", "Ipswich") == "Ipswich Town"
    assert m.canonicalise("2024-25", "Ipswich") == "Ipswich Town"
    # A club whose name never drifted is canonicalised to itself.
    assert m.canonicalise("2023-24", "Arsenal") == "Arsenal"


def test_team_name_canonicalisation_falls_back_to_the_most_recent_archive_name_without_live_teams():
    # No live_teams supplied -- e.g. a purely-historical backtest that must
    # never reference today's naming. The most RECENT archived season for
    # a code wins (2024-25 > 2023-24 as plain strings).
    m = TeamNameCanonicalisationMap.build(_team_identity_archive_rows())
    assert m.canonicalise("2023-24", "Ipswich") == "Ipswich"
    assert m.canonicalise("2024-25", "Ipswich") == "Ipswich"


def test_team_name_canonicalisation_degrades_gracefully_for_an_unresolvable_pair():
    # Deliberately NOT §12.5's "unmatched entities raise" -- this is a
    # training-data pooling aid, not an identity guarantee a caller trusts
    # as a match (see the class docstring). A (season, name) this map was
    # never built with returns unchanged, never raises, never drops.
    m = TeamNameCanonicalisationMap.build(_team_identity_archive_rows())
    assert m.canonicalise("2019-20", "Norwich") == "Norwich"


def test_team_name_canonicalisation_raises_on_missing_columns():
    with pytest.raises(IdentityError):
        TeamNameCanonicalisationMap.build(pl.DataFrame([{"season": "2024-25", "name": "Ipswich"}]))


def test_team_name_canonicalisation_raises_on_missing_live_teams_columns():
    with pytest.raises(IdentityError):
        TeamNameCanonicalisationMap.build(
            _team_identity_archive_rows(), live_teams=pl.DataFrame([{"name": "Ipswich Town"}])
        )
