"""Tests for fplai.providers.fpl — the FPL provider adapter (E2b story 5:
port the existing FPL client onto the interface). `FPLClient` is mocked at
the method level (bootstrap_static / entry_picks) — no network, no
dependency on client.py's own internals being exercised again here (those
are covered by test_client.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import polars as pl
import pytest

from fplai.providers.base import FetchResult, ProviderError
from fplai.providers.fpl import FPLProvider, register
from fplai.registry import CapabilityRegistry
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    CHIP_WINDOW_SEASON,
    GAME_CONFIG_CURRENT,
    GAME_SETTINGS_CURRENT,
    GAMEWEEK_ATTRIBUTES_SEASON,
    MANAGER_PICKS_SELECTION_GAMEWEEK,
    PLAYER_ATTRIBUTES_CURRENT,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    TEAM_ATTRIBUTES_CURRENT,
    SchemaError,
)


def _bootstrap_payload(**overrides):
    payload = {
        "elements": [
            {"id": 1, "web_name": "Player One", "team": 1, "element_type": 3, "now_cost": 80, "selected_by_percent": "12.3", "status": "a"},
            {"id": 2, "web_name": "Player Two", "team": 2, "element_type": 4, "now_cost": 55, "selected_by_percent": "0.4", "status": "a"},
        ],
        "teams": [{"id": 1, "name": "Team A", "short_name": "TMA"}],
        "events": [{"id": 1, "deadline_time": "2026-08-21T17:30:00Z", "finished": True}],
        "chips": [{"id": 1, "name": "wildcard", "start_event": 1, "stop_event": 19}],
        "game_config": {"rules": {"squad_total_spend": 1000}},
        "game_settings": {"league_join_private_max": 20},
    }
    payload.update(overrides)
    return payload


def make_provider(bootstrap_payload=None, entry_picks_side_effect=None):
    client = MagicMock()
    client.bootstrap_static.return_value = bootstrap_payload if bootstrap_payload is not None else _bootstrap_payload()
    if entry_picks_side_effect is not None:
        client.entry_picks.side_effect = entry_picks_side_effect
    return FPLProvider(client=client), client


# -- capability coverage ---------------------------------------------------


def test_capabilities_lists_exactly_the_eight_served():
    # CANONICAL_SCHEMAS now also carries the five PL-provider capabilities
    # (E2b stories 6-7) — FPLProvider serves none of those, so compare
    # against the explicit FPL-only set rather than the whole registry.
    # Session s005 widened this from seven to eight:
    # PLAYER_GAMEWEEK_STATS_GAMEWEEK, a SECOND provider of an existing
    # capability (vaastav's archive is the first) -- see fplai.providers.
    # fpl's module docstring for the fixture-attribution design.
    provider, _ = make_provider()
    expected = {
        PLAYER_ATTRIBUTES_CURRENT,
        TEAM_ATTRIBUTES_CURRENT,
        GAMEWEEK_ATTRIBUTES_SEASON,
        CHIP_WINDOW_SEASON,
        GAME_CONFIG_CURRENT,
        GAME_SETTINGS_CURRENT,
        MANAGER_PICKS_SELECTION_GAMEWEEK,
        PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    }
    assert set(provider.capabilities()) == expected


def test_supports_true_for_served_capability():
    provider, _ = make_provider()
    assert provider.supports(PLAYER_ATTRIBUTES_CURRENT) is True


def test_supports_false_for_unserved_capability():
    from fplai.schemas import CapabilityKey

    provider, _ = make_provider()
    assert provider.supports(CapabilityKey("player", "defensive_actions", "match")) is False


def test_supports_false_for_non_pl_competition():
    provider, _ = make_provider()
    assert provider.supports(PLAYER_ATTRIBUTES_CURRENT, competition="UCL") is False


# -- fetch: bootstrap-backed capabilities -----------------------------------


def test_fetch_player_attributes_returns_canonical_rows_and_provenance():
    provider, client = make_provider()
    result = provider.fetch(PLAYER_ATTRIBUTES_CURRENT, force_refresh=True)
    assert isinstance(result, FetchResult)
    assert result.provider_id == "fpl_api"
    assert result.capability == PLAYER_ATTRIBUTES_CURRENT
    assert result.endpoint == "bootstrap-static/"
    assert result.content_hash
    assert result.rows.height == 2
    CANONICAL_SCHEMAS[PLAYER_ATTRIBUTES_CURRENT].validate(result.rows)  # must not raise
    client.bootstrap_static.assert_called_once_with(force_refresh=True)


def test_fetch_game_config_singleton_wraps_payload_as_json():
    provider, _ = make_provider()
    result = provider.fetch(GAME_CONFIG_CURRENT)
    assert result.rows.columns == ["payload"]
    assert '"squad_total_spend": 1000' in result.rows["payload"][0]


def test_fetch_batch_makes_exactly_one_upstream_call():
    # The regression this test exists to catch: naively calling fetch()
    # once per capability with force_refresh=True would multiply live
    # requests. fetch_batch must make exactly one bootstrap_static() call
    # no matter how many of the six capabilities are requested.
    provider, client = make_provider()
    capabilities = [
        PLAYER_ATTRIBUTES_CURRENT,
        TEAM_ATTRIBUTES_CURRENT,
        GAMEWEEK_ATTRIBUTES_SEASON,
        CHIP_WINDOW_SEASON,
        GAME_CONFIG_CURRENT,
        GAME_SETTINGS_CURRENT,
    ]
    results = provider.fetch_batch(capabilities, force_refresh=True)
    assert client.bootstrap_static.call_count == 1
    assert set(results.keys()) == set(capabilities)
    for capability, result in results.items():
        CANONICAL_SCHEMAS[capability].validate(result.rows)


def test_fetch_batch_rejects_non_bootstrap_capability():
    provider, _ = make_provider()
    with pytest.raises(ProviderError):
        provider.fetch_batch([MANAGER_PICKS_SELECTION_GAMEWEEK])


def test_fetch_batch_skips_a_missing_payload_key_without_failing_the_rest():
    # Mirrors snapshot_bootstrap.py's original per-dataset resilience: one
    # missing/empty dataset must not take the other five down with it.
    payload = _bootstrap_payload()
    payload["chips"] = []  # falsy -> "missing"
    provider, _ = make_provider(bootstrap_payload=payload)
    results = provider.fetch_batch([PLAYER_ATTRIBUTES_CURRENT, CHIP_WINDOW_SEASON])
    assert PLAYER_ATTRIBUTES_CURRENT in results
    assert CHIP_WINDOW_SEASON not in results


def test_fetch_batch_skips_a_capability_that_fails_schema_validation():
    payload = _bootstrap_payload()
    payload["teams"] = [{"id": 1}]  # missing required 'name'/'short_name'
    provider, _ = make_provider(bootstrap_payload=payload)
    results = provider.fetch_batch([PLAYER_ATTRIBUTES_CURRENT, TEAM_ATTRIBUTES_CURRENT])
    assert PLAYER_ATTRIBUTES_CURRENT in results
    assert TEAM_ATTRIBUTES_CURRENT not in results


def test_fetch_unknown_capability_raises_provider_error():
    from fplai.schemas import CapabilityKey

    provider, _ = make_provider()
    with pytest.raises(ProviderError):
        provider.fetch(CapabilityKey("player", "defensive_actions", "match"))


# -- fetch: picks ------------------------------------------------------


def test_fetch_picks_normalises_rows_and_reports_hits_misses():
    def side_effect(entry_id, event, force_refresh=False):
        if entry_id == 2:
            return None  # 404 — expected, non-exceptional
        return {
            "picks": [{"element": 100 + entry_id, "position": 1, "multiplier": 1, "is_captain": False, "is_vice_captain": False}],
            "active_chip": None,
            "entry_history": {"bank": 5, "value": 1005, "event_transfers": 1, "event_transfers_cost": 0, "points_on_bench": 2, "overall_rank": 12345},
        }

    provider, client = make_provider(entry_picks_side_effect=side_effect)
    result = provider.fetch(MANAGER_PICKS_SELECTION_GAMEWEEK, entry_ids=[1, 2, 3], event=1)
    assert result.meta == {"hits": 2, "misses": 1, "n_requested": 3}
    assert result.rows.height == 2
    assert set(result.rows["entry_id"].to_list()) == {1, 3}
    CANONICAL_SCHEMAS[MANAGER_PICKS_SELECTION_GAMEWEEK].validate(result.rows)


def test_fetch_picks_raises_if_every_entry_misses():
    provider, _ = make_provider(entry_picks_side_effect=lambda *a, **k: None)
    with pytest.raises(ProviderError):
        provider.fetch(MANAGER_PICKS_SELECTION_GAMEWEEK, entry_ids=[1, 2], event=1)


def test_fetch_picks_progress_callback_fires_at_the_configured_cadence():
    def side_effect(entry_id, event, force_refresh=False):
        return {"picks": [{"element": entry_id}], "active_chip": None, "entry_history": {}}

    provider, _ = make_provider(entry_picks_side_effect=side_effect)
    progress_calls = []
    provider.fetch(
        MANAGER_PICKS_SELECTION_GAMEWEEK,
        entry_ids=list(range(1, 7)),
        event=1,
        progress_every=2,
        on_progress=lambda i, n, hits, misses: progress_calls.append((i, n, hits, misses)),
    )
    assert progress_calls == [(2, 6, 2, 0), (4, 6, 4, 0), (6, 6, 6, 0)]


# -- registration ------------------------------------------------------


def test_register_adds_every_capability_to_the_registry_except_gameweek_stats_when_season_omitted():
    # Session s005 -- PLAYER_GAMEWEEK_STATS_GAMEWEEK cannot safely share
    # the other seven capabilities' blanket seasons=None coverage (see
    # register()'s own docstring: vaastav's archive also serves this
    # capability, for every OTHER season, and this provider's priority
    # would silently outrank it for a historical-season query too). A
    # caller that omits `season` gets exactly the pre-s005 behaviour for
    # the other seven -- and this one capability is skipped, not raised.
    registry = CapabilityRegistry()
    client = MagicMock()
    client.bootstrap_static.return_value = _bootstrap_payload()
    provider = register(registry, client=client)
    for capability in provider.capabilities():
        if capability == PLAYER_GAMEWEEK_STATS_GAMEWEEK:
            continue
        assert registry.resolve(capability) is provider
    with pytest.raises(Exception):
        registry.resolve(PLAYER_GAMEWEEK_STATS_GAMEWEEK)


def test_register_adds_gameweek_stats_with_season_restricted_coverage_when_season_given():
    registry = CapabilityRegistry()
    client = MagicMock()
    client.bootstrap_static.return_value = _bootstrap_payload()
    provider = register(registry, client=client, season="2026-27")

    # Registered, and resolvable for the season it was registered under.
    assert registry.resolve(PLAYER_GAMEWEEK_STATS_GAMEWEEK, season="2026-27") is provider

    # The critical guarantee this coverage restriction exists for: a
    # DIFFERENT season must NOT resolve to this provider (it would
    # silently mislabel the current live season's data under a historical
    # season's name -- CLAUDE.md rule 2). vaastav_archive is the correct
    # source for a query like this; nothing is registered for it in this
    # test's bare registry, so an unmatched season must raise, not fall
    # through to fpl_api.
    with pytest.raises(Exception):
        registry.resolve(PLAYER_GAMEWEEK_STATS_GAMEWEEK, season="2019-20")

    # Every other capability is still registered exactly as before.
    for capability in provider.capabilities():
        if capability == PLAYER_GAMEWEEK_STATS_GAMEWEEK:
            continue
        assert registry.resolve(capability) is provider


def test_gate_new_provider_is_adapter_plus_registry_entry():
    # E2b's gate, exercised directly: constructing a registry and calling
    # register() is ALL that's needed to make every FPL capability
    # resolvable — no change to schemas.py, registry.py or providers/base.py
    # was required to reach this state.
    registry = CapabilityRegistry()
    client = MagicMock()
    client.bootstrap_static.return_value = _bootstrap_payload()
    register(registry, client=client)
    provider = registry.resolve(PLAYER_ATTRIBUTES_CURRENT)
    result = provider.fetch(PLAYER_ATTRIBUTES_CURRENT)
    assert result.rows.height == 2


# -- fetch: player.gameweek_stats@gameweek (session s005) ------------------


def _gw_bootstrap_payload(**overrides):
    """A bootstrap-static payload shaped for the gameweek-stats tests --
    separate from `_bootstrap_payload()` above (used by the original
    seven-capability tests) so this doesn't disturb their fixed 2-element
    shape. Field names match the real, live-verified payload (session
    s005: `first_name`/`second_name`, `team`, `id`, `finished`/
    `data_checked` on `events`)."""
    payload = {
        "elements": [
            {"id": 1, "first_name": "David", "second_name": "Raya Martin", "team": 1, "element_type": 1},
            {"id": 2, "first_name": "Kepa", "second_name": "Arrizabalaga", "team": 1, "element_type": 1},
            {"id": 3, "first_name": "Bukayo", "second_name": "Saka", "team": 1, "element_type": 3},
        ],
        "events": [{"id": 1, "deadline_time": "2026-08-21T17:30:00Z", "finished": True, "data_checked": True}],
        # session s005 continued -- real live shape, verified against a
        # cached bootstrap-static response (id, singular_name_short only
        # matter to _element_type_labels).
        "element_types": [
            {"id": 1, "singular_name_short": "GKP"},
            {"id": 2, "singular_name_short": "DEF"},
            {"id": 3, "singular_name_short": "MID"},
            {"id": 4, "singular_name_short": "FWD"},
        ],
    }
    payload.update(overrides)
    return payload


# -- position/team resolution, session s005 continued -----------------------


def _elements_as_of_frame(rows: list[dict]) -> pl.DataFrame:
    """A minimal `elements_as_of` frame -- the shape `store.as_of("elements",
    deadline)` would hand back, restricted to the columns this join needs."""
    return pl.DataFrame(rows, infer_schema_length=None)


def _teams_as_of_frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, infer_schema_length=None)


def _gw1_live_payload():
    """One settled, single-fixture gameweek -- field shapes verified live
    against the real FPL API, GW1 2026/27 (session s005): `explain`'s
    `value` agrees exactly with `stats` on every scored identifier,
    `explain` carries NO entry at all for an identifier that scored 0."""
    return {
        "elements": [
            {
                "id": 1,
                "stats": {
                    "minutes": 90, "goals_scored": 0, "assists": 0, "clean_sheets": 1,
                    "goals_conceded": 0, "own_goals": 0, "penalties_saved": 0, "penalties_missed": 0,
                    "yellow_cards": 0, "red_cards": 0, "saves": 1, "bonus": 0, "bps": 24,
                    "clearances_blocks_interceptions": 1, "recoveries": 8, "tackles": 0,
                    "defensive_contribution": 0, "starts": 1,
                    "expected_goals": "0.00", "expected_assists": "0.00",
                    "expected_goal_involvements": "0.00", "expected_goals_conceded": "0.20",
                    "total_points": 6,
                },
                "explain": [
                    {
                        "fixture": 1,
                        "stats": [
                            {"identifier": "minutes", "points": 2, "value": 90},
                            {"identifier": "clean_sheets", "points": 4, "value": 1},
                        ],
                    }
                ],
            },
            {
                # Unused sub -- empty explain, all-zero stats, still gets
                # a row attributed to the team's one fixture this gameweek.
                "id": 2,
                "stats": {
                    "minutes": 0, "goals_scored": 0, "assists": 0, "clean_sheets": 0,
                    "goals_conceded": 0, "own_goals": 0, "penalties_saved": 0, "penalties_missed": 0,
                    "yellow_cards": 0, "red_cards": 0, "saves": 0, "bonus": 0, "bps": 0,
                    "clearances_blocks_interceptions": 0, "recoveries": 0, "tackles": 0,
                    "defensive_contribution": 0, "starts": 0,
                    "expected_goals": "0.00", "expected_assists": "0.00",
                    "expected_goal_involvements": "0.00", "expected_goals_conceded": "0.00",
                    "total_points": 0,
                },
                "explain": [],
            },
        ],
    }


def _gw1_fixtures_payload():
    return [
        {"id": 1, "team_h": 1, "team_a": 7, "team_h_score": 3, "team_a_score": 0, "kickoff_time": "2026-08-21T19:00:00Z"},
    ]


def make_gameweek_stats_provider(bootstrap=None, live=None, fixtures=None):
    client = MagicMock()
    client.bootstrap_static.return_value = bootstrap if bootstrap is not None else _gw_bootstrap_payload()
    client.event_live.return_value = live if live is not None else _gw1_live_payload()
    client.fixtures.return_value = fixtures if fixtures is not None else _gw1_fixtures_payload()
    return FPLProvider(client=client), client


def test_fetch_gameweek_stats_single_fixture_uses_stats_wholesale():
    provider, client = make_gameweek_stats_provider()
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27")
    CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK].validate(result.rows)
    assert result.rows.height == 2
    assert result.meta == {
        "season": "2026-27", "gameweek": 1, "n_rows": 2, "n_elements": 2,
        "n_dgw_elements": 0, "n_blank_elements": 0, "n_position_team_unresolved": 2,
        "n_was_home_opponent_unresolved": 2,
    }

    raya = result.rows.filter(result.rows["element"] == 1).to_dicts()[0]
    assert raya["total_points"] == 6
    assert raya["minutes"] == 90
    assert raya["clean_sheets"] == 1
    assert raya["saves"] == 1  # scored 0 points (floor(1/3)=0) but the FULL stats block is used, not explain alone
    assert raya["attribution_complete"] is True
    assert raya["fixture"] == 1
    assert raya["selected"] is None
    assert raya["value"] is None
    # No elements_as_of/teams_as_of supplied -- pre-fix behaviour, NULL,
    # never guessed from bs_el's own CURRENT element_type/team. Session
    # s005 continued: was_home/opponent_team now follow the SAME
    # discipline -- previously they still derived from the current live
    # team even with no as-of snapshot; now they degrade to None too.
    assert raya["position"] is None
    assert raya["team"] is None
    assert raya["was_home"] is None
    assert raya["opponent_team"] is None

    kepa = result.rows.filter(result.rows["element"] == 2).to_dicts()[0]
    assert kepa["minutes"] == 0
    assert kepa["total_points"] == 0
    assert kepa["fixture"] == 1  # unused sub still attributed to the team's one fixture


def test_fetch_gameweek_stats_entity_key_unique_within_batch():
    # Lesson 2's direct test -- measured, not asserted by construction.
    provider, _ = make_gameweek_stats_provider()
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27")
    key_cols = ["season", "round", "element", "fixture"]
    n_unique = result.rows.select(key_cols).n_unique()
    assert n_unique == result.rows.height


def test_fetch_gameweek_stats_refuses_unsettled_gameweek():
    bootstrap = _gw_bootstrap_payload(events=[{"id": 1, "deadline_time": "2026-08-21T17:30:00Z", "finished": False, "data_checked": False}])
    provider, client = make_gameweek_stats_provider(bootstrap=bootstrap)
    with pytest.raises(ProviderError, match="not settled"):
        provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27")
    client.event_live.assert_not_called()  # never even attempted once unsettled


def test_fetch_gameweek_stats_blank_gameweek_element_produces_no_row():
    bootstrap = _gw_bootstrap_payload()
    bootstrap["elements"].append({"id": 99, "first_name": "Blank", "second_name": "Gw", "team": 99, "element_type": 3})
    live = _gw1_live_payload()
    live["elements"].append({"id": 99, "stats": {"minutes": 0, "total_points": 0}, "explain": []})
    provider, _ = make_gameweek_stats_provider(bootstrap=bootstrap, live=live)
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27")
    assert 99 not in result.rows["element"].to_list()
    assert result.meta["n_blank_elements"] == 1
    # A blank-gameweek element produces no row -- it must not also be
    # counted as "position/team unresolved" (there is no row to attach a
    # position/team to).
    assert result.meta["n_position_team_unresolved"] == 2  # raya (1), kepa (2) -- neither snapshot supplied


# -- position/team resolution, session s005 continued -----------------------


def test_fetch_gameweek_stats_resolves_position_team_when_snapshots_supplied():
    elements_as_of = _elements_as_of_frame(
        [
            {"id": 1, "element_type": 1, "team": 1},  # Raya -- GKP, team 1
            {"id": 2, "element_type": 1, "team": 1},  # Kepa -- GKP, team 1
        ]
    )
    teams_as_of = _teams_as_of_frame([{"id": 1, "name": "Arsenal"}])
    provider, _ = make_gameweek_stats_provider()
    result = provider.fetch(
        PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27",
        elements_as_of=elements_as_of, teams_as_of=teams_as_of,
    )
    CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK].validate(result.rows)
    assert result.meta["n_position_team_unresolved"] == 0
    raya = result.rows.filter(result.rows["element"] == 1).to_dicts()[0]
    assert raya["position"] == "GKP"
    assert raya["team"] == "Arsenal"


def test_fetch_gameweek_stats_unrecognised_element_type_code_never_maps_to_a_position():
    # An element_type code with no entry in bootstrap's own element_types
    # list (e.g. a future season's Assistant-Manager-only type) must
    # resolve to None, never be silently mapped onto an outfield position.
    elements_as_of = _elements_as_of_frame([{"id": 1, "element_type": 5, "team": 1}])
    teams_as_of = _teams_as_of_frame([{"id": 1, "name": "Arsenal"}])
    provider, _ = make_gameweek_stats_provider()
    result = provider.fetch(
        PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27",
        elements_as_of=elements_as_of, teams_as_of=teams_as_of,
    )
    raya = result.rows.filter(result.rows["element"] == 1).to_dicts()[0]
    assert raya["position"] is None
    assert raya["team"] == "Arsenal"  # team side still resolves independently
    assert result.meta["n_position_team_unresolved"] >= 1


def test_fetch_gameweek_stats_missing_element_in_as_of_snapshot_resolves_to_none_not_a_raise():
    # A snapshot that plainly doesn't cover this element (e.g. a debutant
    # registered after every pre-deadline elements capture) is a genuine,
    # honest miss -- degrade to None, do not take the whole ingest down.
    elements_as_of = _elements_as_of_frame([{"id": 999, "element_type": 1, "team": 1}])
    teams_as_of = _teams_as_of_frame([{"id": 1, "name": "Arsenal"}])
    provider, _ = make_gameweek_stats_provider()
    result = provider.fetch(
        PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27",
        elements_as_of=elements_as_of, teams_as_of=teams_as_of,
    )
    raya = result.rows.filter(result.rows["element"] == 1).to_dicts()[0]
    assert raya["position"] is None
    assert raya["team"] is None


def test_fetch_gameweek_stats_elements_as_of_missing_required_column_raises():
    bad_frame = pl.DataFrame({"id": [1]})  # missing element_type/team
    provider, _ = make_gameweek_stats_provider()
    with pytest.raises(ProviderError, match="elements_as_of"):
        provider.fetch(
            PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27",
            elements_as_of=bad_frame, teams_as_of=_teams_as_of_frame([{"id": 1, "name": "Arsenal"}]),
        )


# -- the leakage attack: MUST use the AS-OF snapshot, never the CURRENT ------
# live bootstrap-static team/element_type, even though `bs_el` (bootstrap's
# own live 'elements' list, already in hand for the name lookup) carries a
# DIFFERENT value here on purpose. Attacked directly, not assumed -- see
# CLAUDE.md: "a guarantee is only earned once someone has attacked it from
# outside the intended call path".


def test_fetch_gameweek_stats_uses_as_of_snapshot_not_the_current_live_bootstrap_team():
    # The live/current bootstrap payload says Raya is at team 1 ("Current
    # FC" if it were resolved -- it never is, this is exactly the point).
    # The AS-OF snapshot -- what elements/teams looked like at the
    # gameweek's own deadline -- says team 77 ("Old FC"), simulating a
    # player who has since transferred. If the code took the shortcut of
    # reading bs_el/team (both are right there, already fetched, and
    # trivially reachable), this test fails.
    bootstrap = _gw_bootstrap_payload()
    assert bootstrap["elements"][0]["team"] == 1  # sanity: live team is 1, NOT 77

    elements_as_of = _elements_as_of_frame([{"id": 1, "element_type": 1, "team": 77}])
    teams_as_of = _teams_as_of_frame([{"id": 1, "name": "Current FC"}, {"id": 77, "name": "Old FC"}])

    provider, _ = make_gameweek_stats_provider(bootstrap=bootstrap)
    result = provider.fetch(
        PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27",
        elements_as_of=elements_as_of, teams_as_of=teams_as_of,
    )
    raya = result.rows.filter(result.rows["element"] == 1).to_dicts()[0]
    assert raya["team"] == "Old FC"       # the AS-OF value
    assert raya["team"] != "Current FC"   # never the live/current value
    # was_home/opponent_team, session s005 continued -- the SAME
    # as-of-deadline team (77) now drives these two columns too (a
    # SEPARATE, documented gap before this fix). Team 77 is neither side
    # of _gw1_fixtures_payload()'s one real fixture (team_h=1, team_a=7)
    # -- a genuinely inconsistent fabricated scenario (this test only
    # cares that team 1, the LIVE/current team, is never consulted) -- so
    # `_fixture_home_away`'s "not the home side" fallback applies: False,
    # opponent=team_h=1. The load-bearing assertion is the second one:
    # opponent_team must NOT be 7, which is what a current-team-based read
    # would produce (team 1 correctly matches team_h under the OLD code).
    assert raya["was_home"] is False
    assert raya["opponent_team"] == 1
    assert raya["opponent_team"] != 7


def test_fetch_gameweek_stats_was_home_opponent_team_uses_deadline_resolved_team_not_current():
    # THE ATTACK, direct: the current/live bootstrap team (1) and the
    # deadline-resolved team (7) are BOTH real sides of the one fixture in
    # _gw1_fixtures_payload() (team_h=1, team_a=7) -- so a code path that
    # (incorrectly) reads the CURRENT team produces a DIFFERENT, WRONG
    # answer than the fix, rather than both happening to agree. Simulates
    # a player who was at team 7 (the away side) as of the GW1 deadline
    # and has since transferred to team 1 (the home side) -- the exact
    # shape of the real GW1 2026/27 store leak found live 2026-08-30
    # (Ethan Pinnock: resolved Brentford at the deadline, shows Coventry
    # City in a later live call).
    bootstrap = _gw_bootstrap_payload()
    assert bootstrap["elements"][0]["team"] == 1  # sanity: CURRENT/live team is 1 (team_h)

    elements_as_of = _elements_as_of_frame([{"id": 1, "element_type": 1, "team": 7}])  # DEADLINE team is 7 (team_a)
    teams_as_of = _teams_as_of_frame([{"id": 1, "name": "New Club"}, {"id": 7, "name": "Old Club"}])

    provider, _ = make_gameweek_stats_provider(bootstrap=bootstrap)
    result = provider.fetch(
        PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=1, season="2026-27",
        elements_as_of=elements_as_of, teams_as_of=teams_as_of,
    )
    raya = result.rows.filter(result.rows["element"] == 1).to_dicts()[0]
    # Correct answer, from the DEADLINE-resolved team (7 = team_a, away):
    assert raya["was_home"] is False
    assert raya["opponent_team"] == 1
    # The WRONG answer a current-team-based read would have produced (1
    # matches team_h) -- explicitly proven NOT what this row carries:
    assert raya["was_home"] is not True
    assert raya["opponent_team"] != 7


# -- fetch: double-gameweek degraded path (UNVERIFIED against real data --
# no double gameweek has occurred in 2026/27 as of this session; this is a
# synthetic, hand-built payload mirroring the verified real single-fixture
# JSON shape, doubled) --------------------------------------------------


def _dgw_bootstrap_payload():
    return _gw_bootstrap_payload(
        events=[{"id": 5, "deadline_time": "2026-09-25T17:30:00Z", "finished": True, "data_checked": True}]
    )


def _dgw_live_payload():
    return {
        "elements": [
            {
                "id": 1,
                # Gameweek-aggregate stats -- present, but per this
                # module's design NOT used for the degraded rows below
                # (only total_points/minutes/linear identifiers come from
                # explain; saves/goals_conceded/defensive_contribution
                # come from explain too, or are NULL).
                "stats": {
                    "minutes": 180, "goals_scored": 1, "assists": 0, "clean_sheets": 0,
                    "goals_conceded": 1, "own_goals": 0, "penalties_saved": 0, "penalties_missed": 0,
                    "yellow_cards": 1, "red_cards": 0, "saves": 0, "bonus": 0, "bps": 40,
                    "defensive_contribution": 4, "total_points": 8,
                },
                "explain": [
                    {
                        "fixture": 10,
                        "stats": [
                            {"identifier": "minutes", "points": 2, "value": 90},
                            {"identifier": "goals_scored", "points": 4, "value": 1},
                        ],
                    },
                    {
                        "fixture": 11,
                        "stats": [
                            {"identifier": "minutes", "points": 2, "value": 90},
                            {"identifier": "yellow_cards", "points": -1, "value": 1},
                        ],
                    },
                ],
            }
        ]
    }


def _dgw_fixtures_payload():
    return [
        {"id": 10, "team_h": 1, "team_a": 8, "team_h_score": 1, "team_a_score": 0, "kickoff_time": "2026-09-23T19:00:00Z"},
        {"id": 11, "team_h": 9, "team_a": 1, "team_h_score": 0, "team_a_score": 0, "kickoff_time": "2026-09-26T19:00:00Z"},
    ]


def test_fetch_gameweek_stats_double_gameweek_emits_one_row_per_fixture_never_drops():
    # Lesson 2, restated for THIS provider: vaastav's own entity key once
    # omitted `fixture` and silently dropped 7,141 real double-gameweek
    # rows. This provider must never repeat that -- both fixtures get a
    # row, always.
    provider, _ = make_gameweek_stats_provider(
        bootstrap=_dgw_bootstrap_payload(), live=_dgw_live_payload(), fixtures=_dgw_fixtures_payload()
    )
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=5, season="2026-27")
    CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK].validate(result.rows)
    assert result.rows.height == 2
    assert result.meta["n_dgw_elements"] == 1
    assert set(result.rows["fixture"].to_list()) == {10, 11}

    key_cols = ["season", "round", "element", "fixture"]
    assert result.rows.select(key_cols).n_unique() == result.rows.height


def test_fetch_gameweek_stats_double_gameweek_row_marks_attribution_incomplete():
    provider, _ = make_gameweek_stats_provider(
        bootstrap=_dgw_bootstrap_payload(), live=_dgw_live_payload(), fixtures=_dgw_fixtures_payload()
    )
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=5, season="2026-27")
    rows = {r["fixture"]: r for r in result.rows.to_dicts()}
    assert rows[10]["attribution_complete"] is False
    assert rows[11]["attribution_complete"] is False


def test_fetch_gameweek_stats_double_gameweek_total_points_and_minutes_are_exact_per_fixture():
    # total_points is the sum of explain's own per-fixture points -- ALWAYS
    # exact, never reconstructed. minutes is a linear identifier -- exact
    # whenever it appears in that fixture's explain block.
    provider, _ = make_gameweek_stats_provider(
        bootstrap=_dgw_bootstrap_payload(), live=_dgw_live_payload(), fixtures=_dgw_fixtures_payload()
    )
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=5, season="2026-27")
    rows = {r["fixture"]: r for r in result.rows.to_dicts()}
    assert rows[10]["total_points"] == 6   # 2 (minutes) + 4 (goals_scored)
    assert rows[11]["total_points"] == 1   # 2 (minutes) + -1 (yellow_cards)
    assert rows[10]["minutes"] == 90
    assert rows[11]["minutes"] == 90
    assert rows[10]["goals_scored"] == 1
    assert rows[11]["goals_scored"] == 0   # absent from fixture 11's explain -- a LINEAR identifier, correctly 0
    assert rows[10]["yellow_cards"] == 0   # absent from fixture 10's explain -- correctly 0
    assert rows[11]["yellow_cards"] == 1


def test_fetch_gameweek_stats_double_gameweek_divisor_threshold_identifiers_are_null_not_zero():
    # The core of this story's design: saves/goals_conceded/defensive_
    # contribution CANNOT be safely inferred as 0 from explain's absence
    # (a nonzero raw count can still score 0 points via a floor division
    # or a threshold) -- must be NULL, never guessed.
    provider, _ = make_gameweek_stats_provider(
        bootstrap=_dgw_bootstrap_payload(), live=_dgw_live_payload(), fixtures=_dgw_fixtures_payload()
    )
    result = provider.fetch(PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=5, season="2026-27")
    rows = {r["fixture"]: r for r in result.rows.to_dicts()}
    # Neither fixture's explain block carries saves/goals_conceded/
    # defensive_contribution (both score 0 points that leg) -- genuinely
    # unknown per fixture, even though the GAMEWEEK-aggregate stats block
    # shows goals_conceded=1 and defensive_contribution=4.
    for fid in (10, 11):
        assert rows[fid]["saves"] is None
        assert rows[fid]["goals_conceded"] is None
        assert rows[fid]["defensive_contribution"] is None
        assert rows[fid]["bps"] is None
        assert rows[fid]["clearances_blocks_interceptions"] is None
        assert rows[fid]["starts"] is None
        assert rows[fid]["expected_goals"] is None


def test_fetch_gameweek_stats_double_gameweek_position_team_resolved_identically_on_both_rows():
    # position/team are a per-ELEMENT-per-GAMEWEEK fact, not a per-fixture
    # one -- both of this element's degraded rows must carry the SAME
    # resolved value, from the SAME as-of snapshot.
    elements_as_of = _elements_as_of_frame([{"id": 1, "element_type": 3, "team": 1}])
    teams_as_of = _teams_as_of_frame([{"id": 1, "name": "Arsenal"}])
    provider, _ = make_gameweek_stats_provider(
        bootstrap=_dgw_bootstrap_payload(), live=_dgw_live_payload(), fixtures=_dgw_fixtures_payload()
    )
    result = provider.fetch(
        PLAYER_GAMEWEEK_STATS_GAMEWEEK, gameweek=5, season="2026-27",
        elements_as_of=elements_as_of, teams_as_of=teams_as_of,
    )
    rows = {r["fixture"]: r for r in result.rows.to_dicts()}
    assert rows[10]["position"] == rows[11]["position"] == "MID"
    assert rows[10]["team"] == rows[11]["team"] == "Arsenal"
    assert result.meta["n_position_team_unresolved"] == 0
