"""Tests for fplai.providers.vaastav — the archive adapter (E2b story 10).
`FileTransport` is replaced with a fake `.fetch()` that returns canned
bytes keyed by URL — no network, no dependency on transport.py's own
internals (those are covered by test_transport.py)."""

from __future__ import annotations

import pytest

from fplai.providers.base import ProviderError
from fplai.providers.vaastav import BASE_URL, VaastavProvider, register
from fplai.registry import CapabilityRegistry
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    PLAYER_IDENTITY_SEASON,
    TEAM_IDENTITY_SEASON,
)


class FakeFileTransport:
    """Stands in for transport.FileTransport. `files` maps a URL to raw
    bytes. Records every URL fetched, so tests can assert exactly one
    fetch happened per file (blueprint §12.4's "download-once" contract)."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.calls: list[str] = []

    def fetch(self, url: str, *, force_refresh: bool = False) -> bytes:
        self.calls.append(url)
        if url not in self.files:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.files[url]


# -- fixtures: real column shapes, trimmed to a couple of rows -------------

_GW1_CSV_2025_26 = (
    b"name,position,team,xP,assists,bonus,bps,clean_sheets,"
    b"clearances_blocks_interceptions,creativity,defensive_contribution,element,"
    b"expected_assists,expected_goal_involvements,expected_goals,"
    b"expected_goals_conceded,fixture,goals_conceded,goals_scored,ict_index,"
    b"influence,kickoff_time,minutes,modified,opponent_team,own_goals,"
    b"penalties_missed,penalties_saved,recoveries,red_cards,round,saves,"
    b"selected,starts,tackles,team_a_score,team_h_score,threat,total_points,"
    b"transfers_balance,transfers_in,transfers_out,value,was_home,yellow_cards\n"
    b"Bukayo Saka,MID,Arsenal,6.5,0,0,20,1,1,15.0,0,1,0.1,0.1,0.0,0.3,1,0,1,"
    b"20.0,50.0,2026-08-21T19:00:00Z,90,False,10,0,0,0,2,0,1,0,"
    b"550561,1,1,0,2,60.0,6,0,0,0,130,True,0\n"
)

_GW1_CSV_2016_17 = (
    b"name,assists,attempted_passes,big_chances_created,big_chances_missed,"
    b"bonus,bps,clean_sheets,clearances_blocks_interceptions,completed_passes,"
    b"creativity,dribbles,ea_index,element,errors_leading_to_goal,"
    b"errors_leading_to_goal_attempt,fixture,fouls,goals_conceded,goals_scored,"
    b"ict_index,id,influence,key_passes,kickoff_time,kickoff_time_formatted,"
    b"loaned_in,loaned_out,minutes,offside,open_play_crosses,opponent_team,"
    b"own_goals,penalties_conceded,penalties_missed,penalties_saved,recoveries,"
    b"red_cards,round,saves,selected,tackled,tackles,target_missed,"
    b"team_a_score,team_h_score,threat,total_points,transfers_balance,"
    b"transfers_in,transfers_out,value,was_home,winning_goals,yellow_cards\n"
    b"Aaron_Cresswell,0,0,0,0,0,0,0,0,0,0.0,0,0,454,0,0,10,0,0,0,0.0,454,0.0,"
    b"0,2016-08-15T19:00:00Z,15 Aug 20:00,0,0,0,0,0,4,0,0,0,0,0,0,1,0,14023,"
    b"0,0,0,1,2,0.0,0,0,0,0,55,False,0,0\n"
)

_PLAYERS_RAW_2025_26_HEADER = (
    "assists,birth_date,bonus,bps,can_select,can_transact,"
    "chance_of_playing_this_round,chance_of_playing_next_round,clean_sheets,"
    "code,element_type,first_name,id,minutes,now_cost,opta_code,second_name,"
    "selected_by_percent,status,team,team_code,total_points,web_name"
)
_PLAYERS_RAW_2025_26_ROW = (
    "0,1995-09-15,11,633,False,True,None,None,19,154561,1,David,1,3330,62,"
    "p154561,Raya Martín,36.4,a,1,3,162,Raya"
)
_PLAYERS_RAW_2025_26 = (_PLAYERS_RAW_2025_26_HEADER + "\n" + _PLAYERS_RAW_2025_26_ROW + "\n").encode("utf-8")

# 2016-17: verified live — NO opta_code column at all.
_PLAYERS_RAW_2016_17_HEADER = (
    "assists,bonus,bps,chance_of_playing_next_round,chance_of_playing_this_round,"
    "clean_sheets,code,element_type,first_name,id,minutes,now_cost,second_name,"
    "selected_by_percent,status,team,team_code,total_points,web_name"
)
_PLAYERS_RAW_2016_17_ROW = "0,0,0,None,None,0,12345,2,Aaron,1,0,55,Cresswell,0.1,a,1,21,0,Cresswell"
_PLAYERS_RAW_2016_17 = (_PLAYERS_RAW_2016_17_HEADER + "\n" + _PLAYERS_RAW_2016_17_ROW + "\n").encode("utf-8")

# teams.csv — real column shapes, verified live 2026-08-21 (E2b story 7b).
# 2025-26's real header, trimmed to two rows: Arsenal (code 3, still in the
# 2026/27 PL) and West Ham (code 21, relegated since — story 11's live
# proof, docs/wiki/provider-framework.md §11.4).
_TEAMS_CSV_2025_26 = (
    b"code,draw,form,id,loss,name,played,points,position,short_name,strength,"
    b"team_division,unavailable,win,link_url,strength_overall_home,"
    b"strength_overall_away,strength_attack_home,strength_attack_away,"
    b"strength_defence_home,strength_defence_away,pulse_id\n"
    b"3,0,,1,0,Arsenal,0,0,1,ARS,5,,False,0,,1305,1355,1340,1390,1270,1320,1\n"
    b"21,0,,19,0,West Ham,0,0,18,WHU,3,,False,0,,1095,1130,1060,1080,1130,1180,25\n"
)


# -- provider ----------------------------------------------------------------


def _provider(files: dict[str, bytes]) -> tuple[VaastavProvider, FakeFileTransport]:
    transport = FakeFileTransport(files)
    return VaastavProvider(transport), transport


def test_provider_id_and_policy():
    provider, _ = _provider({})
    assert provider.provider_id == "vaastav_archive"
    from fplai.transport import BulkFilePolicy

    assert isinstance(provider.policy, BulkFilePolicy)


def test_capabilities_lists_exactly_the_three_served():
    provider, _ = _provider({})
    assert set(provider.capabilities()) == {
        PLAYER_GAMEWEEK_STATS_GAMEWEEK,
        PLAYER_IDENTITY_SEASON,
        TEAM_IDENTITY_SEASON,
    }


def test_supports_true_for_served_capability_false_otherwise():
    provider, _ = _provider({})
    assert provider.supports(PLAYER_GAMEWEEK_STATS_GAMEWEEK) is True
    from fplai.schemas import TEAM_ATTRIBUTES_CURRENT

    assert provider.supports(TEAM_ATTRIBUTES_CURRENT) is False


def test_fetch_gameweek_stats_decodes_and_injects_season():
    url = BASE_URL + "2025-26/gws/gw1.csv"
    provider, transport = _provider({url: _GW1_CSV_2025_26})
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, season="2025-26", gameweek=1)

    assert result.rows.height == 1
    assert result.rows["season"][0] == "2025-26"
    assert result.rows["selected"][0] == 550561  # raw ownership COUNT, not a percent
    assert result.rows["value"][0] == 130  # point-in-time price, x10 convention
    assert result.provider_id == "vaastav_archive"
    assert result.capability == PLAYER_GAMEWEEK_STATS_GAMEWEEK
    assert transport.calls == [url]  # exactly one fetch


def test_fetch_gameweek_stats_validates_against_schema():
    CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK]  # sanity: schema exists
    url = BASE_URL + "2025-26/gws/gw1.csv"
    provider, _ = _provider({url: _GW1_CSV_2025_26})
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, season="2025-26", gameweek=1)
    CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK].validate(result.rows)  # must not raise


def test_fetch_gameweek_stats_2016_17_has_fewer_columns_but_still_validates():
    # Real schema drift, verified live: 2016-17 lacks xP/position/team/
    # defensive_contribution/expected_* — but every REQUIRED field is still
    # present, so this must validate cleanly rather than raise.
    url = BASE_URL + "2016-17/gws/gw1.csv"
    provider, _ = _provider({url: _GW1_CSV_2016_17})
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, season="2016-17", gameweek=1)
    assert "position" not in result.rows.columns  # confirms this is genuinely the old shape
    assert result.rows["selected"][0] == 14023
    CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK].validate(result.rows)


def test_fetch_gameweek_stats_missing_required_field_raises():
    # A file genuinely missing a required column (e.g. no 'selected' at
    # all) must raise SchemaError, not silently pass with a null column.
    bad = b"element,round,total_points,minutes,value,was_home,team_a_score,team_h_score,kickoff_time,name\n1,1,5,90,50,True,1,2,2026-08-21T19:00:00Z,X\n"
    url = BASE_URL + "2099-00/gws/gw1.csv"
    provider, _ = _provider({url: bad})
    from fplai.schemas import SchemaError

    with pytest.raises(SchemaError):
        provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, season="2099-00", gameweek=1)


def test_fetch_player_identity_decodes_and_injects_season():
    url = BASE_URL + "2025-26/players_raw.csv"
    provider, transport = _provider({url: _PLAYERS_RAW_2025_26})
    result = provider.fetch(PLAYER_IDENTITY_SEASON, season="2025-26")

    assert result.rows.height == 1
    assert result.rows["season"][0] == "2025-26"
    assert result.rows["opta_code"][0] == "p154561"
    assert result.meta["has_opta_code"] is True
    assert transport.calls == [url]


def test_fetch_player_identity_2016_17_has_no_opta_code_but_still_validates():
    # The load-bearing schema-drift case: opta_code genuinely absent, and
    # this capability must STILL validate — the join failure belongs to
    # fplai.identity.PlayerIdentityMap.build, not this schema check.
    url = BASE_URL + "2016-17/players_raw.csv"
    provider, _ = _provider({url: _PLAYERS_RAW_2016_17})
    result = provider.fetch(PLAYER_IDENTITY_SEASON, season="2016-17")
    assert "opta_code" not in result.rows.columns
    assert result.meta["has_opta_code"] is False
    CANONICAL_SCHEMAS[PLAYER_IDENTITY_SEASON].validate(result.rows)  # must not raise

    from fplai.identity import IdentityError, PlayerIdentityMap

    with pytest.raises(IdentityError):
        PlayerIdentityMap.build(result.rows)  # THIS is where the missing join raises


def test_fetch_team_identity_decodes_and_injects_season():
    url = BASE_URL + "2025-26/teams.csv"
    provider, transport = _provider({url: _TEAMS_CSV_2025_26})
    result = provider.fetch(TEAM_IDENTITY_SEASON, season="2025-26")

    assert result.rows.height == 2
    assert result.rows["season"][0] == "2025-26"
    assert set(result.rows["code"].to_list()) == {3, 21}  # West Ham (21) present, story 11's finding
    assert result.provider_id == "vaastav_archive"
    assert result.capability == TEAM_IDENTITY_SEASON
    assert transport.calls == [url]


def test_fetch_team_identity_validates_against_schema():
    url = BASE_URL + "2025-26/teams.csv"
    provider, _ = _provider({url: _TEAMS_CSV_2025_26})
    result = provider.fetch(TEAM_IDENTITY_SEASON, season="2025-26")
    CANONICAL_SCHEMAS[TEAM_IDENTITY_SEASON].validate(result.rows)  # must not raise


def test_fetch_team_identity_west_ham_resolvable_from_this_row():
    # The load-bearing case this story exists for: West Ham (code 21) is a
    # real row in the 2025/26 archive, feeding straight into
    # fplai.identity.TeamIdentityMap.build via build_team_identity_map_for_season.
    url = BASE_URL + "2025-26/teams.csv"
    provider, _ = _provider({url: _TEAMS_CSV_2025_26})
    result = provider.fetch(TEAM_IDENTITY_SEASON, season="2025-26")

    from fplai.identity import TeamIdentityMap

    pl_teams = [{"id": "21", "name": "West Ham United", "shortName": "West Ham"}]
    m = TeamIdentityMap.build(result.rows.filter(result.rows["code"] == 21), pl_teams)
    assert m.resolve_fpl_to_pl(21) == "21"


def test_fetch_team_identity_missing_teams_csv_raises_transport_error():
    # teams.csv genuinely does not exist for 2016-17/2017-18/2018-19
    # (verified live, 404 on all three) — a season not in the fake
    # transport's files dict stands in for that 404; FakeFileTransport
    # raises AssertionError on an unexpected fetch, standing in for the
    # real FileTransport's TransportError on a real 404 (see test_transport.py
    # for that raise itself).
    provider, _ = _provider({})
    with pytest.raises(AssertionError, match="2017-18"):
        provider.fetch(TEAM_IDENTITY_SEASON, season="2017-18")


def test_fetch_unsupported_capability_raises():
    from fplai.schemas import TEAM_ATTRIBUTES_CURRENT

    provider, _ = _provider({})
    with pytest.raises(ProviderError):
        provider.fetch(TEAM_ATTRIBUTES_CURRENT)


def test_fetch_empty_decoded_frame_raises():
    url = BASE_URL + "2025-26/gws/gw99.csv"
    header_only = b"name,element,round,total_points,minutes,selected,value,was_home,team_a_score,team_h_score,kickoff_time\n"
    provider, _ = _provider({url: header_only})
    with pytest.raises(ProviderError):
        provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, season="2025-26", gameweek=99)


# -- register() ----------------------------------------------------------


def test_register_adds_provider_to_registry_for_all_three_capabilities():
    registry = CapabilityRegistry()
    transport = FakeFileTransport({})
    provider = register(registry, transport)

    assert isinstance(provider, VaastavProvider)
    resolved = registry.resolve(PLAYER_GAMEWEEK_STATS_GAMEWEEK)
    assert resolved is provider
    resolved2 = registry.resolve(PLAYER_IDENTITY_SEASON)
    assert resolved2 is provider
    resolved3 = registry.resolve(TEAM_IDENTITY_SEASON)
    assert resolved3 is provider


def test_register_priority_defers_to_live_providers():
    # No live provider serves these capabilities today, but the declared
    # priority (20) must still be worse (higher number) than fpl.py/pl.py's
    # 10 — an explicit statement of intent for when overlap exists.
    registry = CapabilityRegistry()
    transport = FakeFileTransport({})
    register(registry, transport)
    entries = registry._entries[PLAYER_GAMEWEEK_STATS_GAMEWEEK]
    assert entries[0].coverage.priority == 20
