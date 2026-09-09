"""Tests for fplai.providers.pl — the Premier League API provider adapter
(E2b stories 6-7). `HttpTransport` is replaced with a fake `.get()` that
returns canned (status, text) pairs keyed by endpoint path — no network, no
dependency on transport.py's own internals (those are covered by
test_transport.py)."""

from __future__ import annotations

import json

import polars as pl
import pytest

from fplai.identity import IdentityResolver
from fplai.providers.base import ProviderError
from fplai.providers.pl import PLProvider, register
from fplai.registry import CapabilityRegistry
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    MATCH_FIXTURES_MATCHWEEK,
    MATCH_LINEUPS_MATCH,
    MATCH_OFFICIALS_MATCH,
    MATCH_SUBSTITUTIONS_MATCH,
    PLAYER_SEASON_STATS_SEASON,
    TEAM_MATCH_STATS_MATCH,
    CapabilityKey,
)


class FakeTransport:
    """Stands in for transport.HttpTransport. `responses` maps an endpoint
    PATH (exactly what PLProvider.fetch builds, e.g.
    'v3/matches/123/lineups') to a (status, body_dict_or_str)."""

    def __init__(self, responses: dict[str, tuple[int, object]]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, path: str, *, force_refresh: bool = False, cost: int = 1):
        self.calls.append(path)
        if path not in self.responses:
            raise AssertionError(f"unexpected request: {path}")
        status, body = self.responses[path]
        text = body if isinstance(body, str) else json.dumps(body)
        return status, text


# -- fixtures shared across tests --------------------------------------------

ELEMENTS = pl.DataFrame(
    [
        {"id": 100, "code": 489639, "opta_code": "p489639"},  # Verbruggen
        {"id": 101, "code": 83299, "opta_code": "p83299"},  # Dunk
        {"id": 102, "code": 465247, "opta_code": "p465247"},  # Man Utd GK stand-in
    ]
)
TEAMS = pl.DataFrame(
    [
        {"code": 36, "name": "Brighton"},
        {"code": 1, "name": "Man Utd"},
    ]
)
MATCHWEEK_PAYLOAD = {
    "data": [
        {
            "matchId": "2562265",
            "kickoff": "2026-05-24 16:00:00",
            "homeTeam": {"id": "36", "name": "Brighton and Hove Albion", "shortName": "Brighton"},
            "awayTeam": {"id": "1", "name": "Manchester United", "shortName": "Man Utd"},
            "ground": "Amex",
            "period": "FullTime",
        }
    ]
}

LINEUPS_PAYLOAD = {
    "home_team": {
        "teamId": "36",
        "players": [
            {"id": "489639", "firstName": "Bart", "lastName": "Verbruggen", "position": "Goalkeeper", "shirtNum": "1", "isCaptain": False},
            {"id": "83299", "firstName": "Lewis", "lastName": "Dunk", "position": "Defender", "shirtNum": "5", "isCaptain": True},
        ],
    },
    "away_team": {
        "teamId": "1",
        "players": [
            {"id": "465247", "firstName": "Man Utd", "lastName": "GK", "position": "Substitute", "subPosition": "Goalkeeper", "shirtNum": "13", "isCaptain": False},
        ],
    },
}

EVENTS_PAYLOAD = {
    "homeTeam": {"id": "36", "name": "Brighton", "subs": [{"period": "SecondHalf", "time": "59", "playerOnId": "83299", "playerOffId": "489639"}]},
    "awayTeam": {"id": "1", "name": "Man Utd", "subs": [{"period": "SecondHalf", "time": "74", "playerOnId": "489639", "playerOffId": "465247"}]},
}

TEAM_STATS_PAYLOAD = [
    {"side": "Home", "stats": {"expectedGoals": 1.23, "totalTackle": 12.0}},
    {"side": "Away", "stats": {"expectedGoals": 0.98, "totalTackle": 9.0}},
]

PLAYER_SEASON_STATS_PAYLOAD = {"stats": {"expectedAssists": 0.57, "totalTackles": 35.0}}

OFFICIALS_PAYLOAD = {
    "matchId": "2562265",
    "matchOfficials": [
        {"official": {"firstName": "Samuel", "lastName": "Barrott", "name": "Samuel Barrott"}, "type": "Referee"},
        {"official": {"firstName": "Simon", "lastName": "Bennett", "name": "Simon Bennett"}, "type": "Assistant Referee#1"},
        {"official": {"firstName": "Blake", "lastName": "Antrobus", "name": "Blake Antrobus"}, "type": "Assistant Referee#2"},
        {"official": {"firstName": "Ruebyn", "lastName": "Ricardo", "name": "Ruebyn Ricardo"}, "type": "Fourth official"},
        {"official": {"firstName": "Stuart", "lastName": "Attwell", "name": "Stuart Attwell"}, "type": "Video Assistant Referee"},
        {"official": {"firstName": "Steven", "lastName": "Meredith", "name": "Steven Meredith"}, "type": "Assistant VAR Official"},
    ],
}  # verified live shape, real match id 2562265, 2026-08-28


def make_provider(extra_responses=None, season="2025", identity=None, elements=ELEMENTS, teams=TEAMS):
    responses = {
        "v1/competitions/8/seasons/2025/matchweeks/1/matches": (200, MATCHWEEK_PAYLOAD),
        "v3/matches/2562265/lineups": (200, LINEUPS_PAYLOAD),
        "v1/matches/2562265/events": (200, EVENTS_PAYLOAD),
        "v3/matches/2562265/stats": (200, TEAM_STATS_PAYLOAD),
        "v2/competitions/8/seasons/2025/players/489639/stats": (200, PLAYER_SEASON_STATS_PAYLOAD),
        "v1/matches/2562265/officials": (200, OFFICIALS_PAYLOAD),
    }
    if extra_responses:
        responses.update(extra_responses)
    transport = FakeTransport(responses)
    provider = PLProvider(transport, season=season, elements=elements, teams=teams, identity=identity)
    return provider, transport


# -- capability coverage ------------------------------------------------------


def test_capabilities_lists_exactly_the_six_served():
    provider, _ = make_provider()
    assert set(provider.capabilities()) == {
        MATCH_LINEUPS_MATCH,
        MATCH_SUBSTITUTIONS_MATCH,
        TEAM_MATCH_STATS_MATCH,
        PLAYER_SEASON_STATS_SEASON,
        MATCH_FIXTURES_MATCHWEEK,
        MATCH_OFFICIALS_MATCH,
    }


def test_defensive_actions_at_match_is_not_supported():
    provider, _ = make_provider()
    assert provider.supports(CapabilityKey("player", "defensive_actions", "match")) is False


def test_supports_false_for_non_pl_competition():
    provider, _ = make_provider()
    assert provider.supports(MATCH_LINEUPS_MATCH, competition="UCL") is False


def test_constructor_requires_identity_or_both_snapshots():
    from fplai.transport import HttpTransport  # unused directly, just needs a placeholder

    with pytest.raises(ProviderError):
        PLProvider(FakeTransport({}), season="2025")


# -- identity bootstrap -------------------------------------------------------


def test_identity_is_built_lazily_with_exactly_one_request():
    provider, transport = make_provider()
    assert transport.calls == []
    resolver = provider.identity()
    assert isinstance(resolver, IdentityResolver)
    assert transport.calls == ["v1/competitions/8/seasons/2025/matchweeks/1/matches"]
    provider.identity()  # second call must not re-fetch
    assert transport.calls == ["v1/competitions/8/seasons/2025/matchweeks/1/matches"]


def test_identity_bootstrap_raises_if_a_team_is_unresolved():
    bad_teams = pl.DataFrame([{"code": 999, "name": "Nowhere FC"}])
    provider, _ = make_provider(teams=bad_teams)
    with pytest.raises(Exception):  # IdentityError
        provider.identity()


def test_identity_season_defaults_to_season_but_is_independently_overridable():
    # Live finding, 2026-08-20: identity MUST be verified against the
    # season whose fixture list matches the current elements/teams
    # snapshot, which need not be the season of the match being fetched.
    responses = {
        "v1/competitions/8/seasons/2026/matchweeks/1/matches": (200, MATCHWEEK_PAYLOAD),
    }
    transport = FakeTransport(responses)
    provider = PLProvider(
        transport, season="2025", identity_season="2026", elements=ELEMENTS, teams=TEAMS
    )
    provider.identity()
    assert transport.calls == ["v1/competitions/8/seasons/2026/matchweeks/1/matches"]


# -- lineups -------------------------------------------------------------


def test_fetch_lineups_resolves_identity_and_marks_bench():
    provider, _ = make_provider()
    result = provider.fetch(MATCH_LINEUPS_MATCH, match_id=2562265)
    CANONICAL_SCHEMAS[MATCH_LINEUPS_MATCH].validate(result.rows)
    assert result.rows.height == 3
    by_code = {row["player_code"]: row for row in result.rows.to_dicts()}
    assert by_code[489639]["role"] == "start"
    assert by_code[489639]["player_element_id"] == 100
    assert by_code[489639]["team_code"] == 36
    assert by_code[465247]["role"] == "bench"
    assert by_code[465247]["team_code"] == 1
    assert by_code[83299]["is_captain"] is True


def test_fetch_lineups_raises_on_unresolved_player():
    payload = json.loads(json.dumps(LINEUPS_PAYLOAD))
    payload["home_team"]["players"].append(
        {"id": "999999999", "position": "Defender", "shirtNum": "99", "isCaptain": False}
    )
    provider, _ = make_provider(extra_responses={"v3/matches/2562265/lineups": (200, payload)})
    with pytest.raises(Exception):  # IdentityError, propagated
        provider.fetch(MATCH_LINEUPS_MATCH, match_id=2562265)


def test_fetch_lineups_raises_on_non_200():
    provider, _ = make_provider(extra_responses={"v3/matches/2562265/lineups": (404, "not found")})
    with pytest.raises(ProviderError):
        provider.fetch(MATCH_LINEUPS_MATCH, match_id=2562265)


# -- substitutions ---------------------------------------------------------


def test_fetch_substitutions_normalises_rows():
    provider, _ = make_provider()
    result = provider.fetch(MATCH_SUBSTITUTIONS_MATCH, match_id=2562265)
    CANONICAL_SCHEMAS[MATCH_SUBSTITUTIONS_MATCH].validate(result.rows)
    assert result.rows.height == 2
    rows = result.rows.to_dicts()
    home_sub = next(r for r in rows if r["team_code"] == 36)
    assert home_sub["minute"] == 59
    assert home_sub["player_on_element_id"] == 101  # Dunk
    assert home_sub["player_off_element_id"] == 100  # Verbruggen


def test_fetch_substitutions_handles_a_null_player_on_id():
    # Live finding, session s004, 2026-08-28: Crystal Palace v Aston Villa
    # 2025-26 (match_id 2561915) has a real substitution with
    # playerOnId=None (subbed off, no one came on) — this crashed
    # PlayerIdentityMap.resolve(None) with an unhandled TypeError before
    # this fix (identity.py's resolve() is READ-ONLY; the fix belongs here,
    # treating a null on-side as a real, kept fact, not an identity miss).
    payload = json.loads(json.dumps(EVENTS_PAYLOAD))
    payload["homeTeam"]["subs"].append({"period": "SecondHalf", "time": "90", "playerOnId": None, "playerOffId": "83299"})
    provider, _ = make_provider(extra_responses={"v1/matches/2562265/events": (200, payload)})
    result = provider.fetch(MATCH_SUBSTITUTIONS_MATCH, match_id=2562265)
    CANONICAL_SCHEMAS[MATCH_SUBSTITUTIONS_MATCH].validate(result.rows)
    assert result.rows.height == 3
    row = next(r for r in result.rows.to_dicts() if r["minute"] == 90)
    assert row["player_on_element_id"] is None
    assert row["player_on_code"] is None
    assert row["player_off_element_id"] == 101  # Dunk


def test_fetch_substitutions_raises_on_null_player_off_id():
    payload = json.loads(json.dumps(EVENTS_PAYLOAD))
    payload["homeTeam"]["subs"].append({"period": "SecondHalf", "time": "90", "playerOnId": "83299", "playerOffId": None})
    provider, _ = make_provider(extra_responses={"v1/matches/2562265/events": (200, payload)})
    with pytest.raises(ProviderError):
        provider.fetch(MATCH_SUBSTITUTIONS_MATCH, match_id=2562265)


def test_fetch_substitutions_raises_on_zero_subs():
    empty = {"homeTeam": {"id": "36", "subs": []}, "awayTeam": {"id": "1", "subs": []}}
    provider, _ = make_provider(extra_responses={"v1/matches/2562265/events": (200, empty)})
    with pytest.raises(ProviderError):
        provider.fetch(MATCH_SUBSTITUTIONS_MATCH, match_id=2562265)


# -- team match stats (long format) -----------------------------------------


def test_fetch_team_match_stats_is_long_format_with_all_keys():
    provider, _ = make_provider()
    result = provider.fetch(
        TEAM_MATCH_STATS_MATCH, match_id=2562265, home_team_code=36, away_team_code=1
    )
    CANONICAL_SCHEMAS[TEAM_MATCH_STATS_MATCH].validate(result.rows)
    assert result.rows.height == 4  # 2 stat keys x 2 sides
    rows = result.rows.to_dicts()
    home_xg = next(r for r in rows if r["side"] == "Home" and r["stat_key"] == "expectedGoals")
    assert home_xg["value"] == pytest.approx(1.23)
    assert home_xg["team_code"] == 36


def test_fetch_team_match_stats_team_code_optional():
    provider, _ = make_provider()
    result = provider.fetch(TEAM_MATCH_STATS_MATCH, match_id=2562265)
    assert all(r["team_code"] is None for r in result.rows.to_dicts())
    CANONICAL_SCHEMAS[TEAM_MATCH_STATS_MATCH].validate(result.rows)  # still valid — not required


def test_fetch_team_match_stats_handles_a_non_scalar_stat_value():
    # Live finding: 'fastestPlayer' is {"topSpeed": 35.53, "playerId": "..."},
    # not a number. Must survive as value_raw (JSON), never silently dropped,
    # and must not blow up float()'ing every other, genuinely scalar, key.
    payload = [
        {"side": "Home", "stats": {"expectedGoals": 1.23, "fastestPlayer": {"topSpeed": 35.53, "playerId": "592031"}}},
        {"side": "Away", "stats": {"expectedGoals": 0.98}},
    ]
    provider, _ = make_provider(extra_responses={"v3/matches/2562265/stats": (200, payload)})
    result = provider.fetch(TEAM_MATCH_STATS_MATCH, match_id=2562265)
    rows = {r["stat_key"]: r for r in result.rows.to_dicts() if r["side"] == "Home"}
    assert rows["expectedGoals"]["value"] == pytest.approx(1.23)
    assert rows["expectedGoals"]["value_raw"] is None
    assert rows["fastestPlayer"]["value"] is None
    assert json.loads(rows["fastestPlayer"]["value_raw"]) == {"topSpeed": 35.53, "playerId": "592031"}


# -- player season stats (long format) --------------------------------------


def test_fetch_player_season_stats_resolves_reverse_identity():
    provider, _ = make_provider()
    result = provider.fetch(PLAYER_SEASON_STATS_SEASON, player_element_id=100)  # Verbruggen
    CANONICAL_SCHEMAS[PLAYER_SEASON_STATS_SEASON].validate(result.rows)
    assert result.rows.height == 2
    rows = result.rows.to_dicts()
    assert all(r["player_code"] == 489639 for r in rows)
    assert all(r["season"] == "2025" for r in rows)


# -- fixtures ---------------------------------------------------------------


def test_fetch_fixtures_resolves_team_codes():
    provider, _ = make_provider()
    result = provider.fetch(MATCH_FIXTURES_MATCHWEEK, matchweek=1)
    CANONICAL_SCHEMAS[MATCH_FIXTURES_MATCHWEEK].validate(result.rows)
    row = result.rows.to_dicts()[0]
    assert row["match_id"] == "2562265"
    assert row["home_team_code"] == 36
    assert row["away_team_code"] == 1


def test_fetch_unknown_capability_raises_provider_error():
    provider, _ = make_provider()
    with pytest.raises(ProviderError):
        provider.fetch(CapabilityKey("player", "defensive_actions", "match"))


# -- officials ----------------------------------------------------------------


def test_fetch_officials_returns_one_row_per_official_with_role_and_is_referee():
    provider, transport = make_provider()
    result = provider.fetch(MATCH_OFFICIALS_MATCH, match_id=2562265)
    CANONICAL_SCHEMAS[MATCH_OFFICIALS_MATCH].validate(result.rows)
    assert result.rows.height == 6
    # a SEPARATE request from lineups/substitutions — proves this capability
    # is not silently answered from an already-cached payload for either.
    assert transport.calls == ["v1/matches/2562265/officials"]
    by_role = {row["role"]: row for row in result.rows.to_dicts()}
    ref = by_role["Referee"]
    assert ref["is_referee"] is True
    assert ref["official_name"] == "Samuel Barrott"
    assert ref["official_first_name"] == "Samuel"
    assert ref["official_last_name"] == "Barrott"
    assert by_role["Assistant Referee#1"]["is_referee"] is False
    assert by_role["Video Assistant Referee"]["is_referee"] is False
    # entity key uniqueness within one real batch (CLAUDE.md lesson 2) —
    # every role distinct, no collision.
    assert len({row["role"] for row in result.rows.to_dicts()}) == 6


def test_fetch_officials_does_not_call_identity_at_all():
    # No player/team numeric id exists in this payload (schema comment) —
    # unlike lineups/substitutions, this fetch must not touch self._identity
    # or make the identity-bootstrap request at all.
    provider, transport = make_provider()
    provider.fetch(MATCH_OFFICIALS_MATCH, match_id=2562265)
    assert "v1/competitions/8/seasons/2025/matchweeks/1/matches" not in transport.calls
    assert provider._identity is None


def test_fetch_officials_raises_on_non_200():
    provider, _ = make_provider(extra_responses={"v1/matches/2562265/officials": (404, "not found")})
    with pytest.raises(ProviderError):
        provider.fetch(MATCH_OFFICIALS_MATCH, match_id=2562265)


def test_fetch_officials_raises_on_zero_officials():
    empty = {"matchId": "2562265", "matchOfficials": []}
    provider, _ = make_provider(extra_responses={"v1/matches/2562265/officials": (200, empty)})
    with pytest.raises(ProviderError):
        provider.fetch(MATCH_OFFICIALS_MATCH, match_id=2562265)


def test_fetch_officials_raises_on_missing_type():
    payload = {"matchId": "2562265", "matchOfficials": [{"official": {"name": "No Role"}}]}
    provider, _ = make_provider(extra_responses={"v1/matches/2562265/officials": (200, payload)})
    with pytest.raises(ProviderError):
        provider.fetch(MATCH_OFFICIALS_MATCH, match_id=2562265)


# -- registration -------------------------------------------------------------


def test_register_adds_every_capability_to_the_registry():
    registry = CapabilityRegistry()
    transport = FakeTransport(
        {"v1/competitions/8/seasons/2025/matchweeks/1/matches": (200, MATCHWEEK_PAYLOAD)}
    )
    provider = register(registry, transport, season="2025", elements=ELEMENTS, teams=TEAMS)
    for capability in provider.capabilities():
        assert registry.resolve(capability, season="2025") is provider


def test_gate_new_provider_is_adapter_plus_registry_entry():
    registry = CapabilityRegistry()
    transport = FakeTransport(
        {
            "v1/competitions/8/seasons/2025/matchweeks/1/matches": (200, MATCHWEEK_PAYLOAD),
            "v3/matches/2562265/lineups": (200, LINEUPS_PAYLOAD),
        }
    )
    register(registry, transport, season="2025", elements=ELEMENTS, teams=TEAMS)
    provider = registry.resolve(MATCH_LINEUPS_MATCH, season="2025")
    result = provider.fetch(MATCH_LINEUPS_MATCH, match_id=2562265)
    assert result.rows.height == 3


# --- PL kickoff timezone (session s004, 2026-08-29) --------------------------


def test_pl_kickoff_is_europe_london_wall_clock_converted_to_utc():
    """The PL API sends a bare wall-clock string with no offset, and it is
    Europe/London, not UTC. This adapter treated it as UTC until 2026-08-29,
    putting every British-Summer-Time fixture an hour early.

    Found by measurement, not review: joining PL fixtures to vaastav's own
    FPL-sourced `kickoff_time` (explicitly Z) matched 223 of 375 fixtures in
    2025-26, and the delta on the remainder was EXACTLY -60 minutes, split
    perfectly on the calendar — 0 in Nov-Mar, -60 in Apr/May/Aug/Sep.

    It mattered beyond the join: `_valid_at_for` anchors lineups,
    substitutions, team-match-stats and officials on this same kickoff, so
    the error put their valid_at an hour EARLY — the leakage direction under
    blueprint §3.2.
    """
    from datetime import datetime

    from fplai.providers.pl import _parse_pl_kickoff

    # BST: 20:00 London == 19:00 UTC
    assert _parse_pl_kickoff("2025-08-15 20:00:00") == datetime(2025, 8, 15, 19, 0)
    # GMT: unchanged
    assert _parse_pl_kickoff("2026-01-01 17:30:00") == datetime(2026, 1, 1, 17, 30)
    # either side of the autumn changeover, which is where an off-by-one-rule
    # implementation (e.g. "subtract an hour from April to October") breaks
    assert _parse_pl_kickoff("2025-10-25 15:00:00") == datetime(2025, 10, 25, 14, 0)
    assert _parse_pl_kickoff("2025-11-01 15:00:00") == datetime(2025, 11, 1, 15, 0)
    # stored tz-naive, per CLAUDE.md's storage boundary
    assert _parse_pl_kickoff("2025-08-15 20:00:00").tzinfo is None

