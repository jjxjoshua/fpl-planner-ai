"""Tests for fplai.schemas — canonical capability keys and fact-table
schemas. Pure, no I/O, no network."""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from fplai.schemas import (
    CANONICAL_SCHEMAS,
    DATASET_ENTITY_KEYS,
    MANAGER_PICKS_SELECTION_GAMEWEEK,
    CapabilityKey,
    FactTableSchema,
    SchemaError,
)


def test_capability_key_is_structurally_hashable_and_equal():
    a = CapabilityKey("player", "defensive_actions", "match")
    b = CapabilityKey("player", "defensive_actions", "match")
    assert a == b
    assert hash(a) == hash(b)
    assert {a: 1}[b] == 1


def test_capability_key_grain_is_part_of_the_key():
    # blueprint §12.1: same entity+measure, different grain -> different key.
    match_grain = CapabilityKey("player", "defensive_actions", "match")
    season_grain = CapabilityKey("player", "defensive_actions", "season")
    assert match_grain != season_grain
    assert hash(match_grain) != hash(season_grain)


def test_capability_key_str_matches_blueprint_notation():
    key = CapabilityKey("player", "defensive_actions", "match")
    assert str(key) == "player.defensive_actions@match"


def test_fact_table_schema_rejects_entity_key_not_in_required_fields():
    with pytest.raises(SchemaError):
        FactTableSchema(
            capability=CapabilityKey("x", "y", "z"),
            entity_key=("id",),
            required_fields=("name",),  # "id" missing
        )


def test_fact_table_schema_validate_passes_when_fields_present():
    schema = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"),
        entity_key=("id",),
        required_fields=("id", "name"),
    )
    df = pl.DataFrame({"id": [1], "name": ["a"], "extra": [True]})
    schema.validate(df)  # must not raise — extra columns are fine


def test_fact_table_schema_validate_raises_on_missing_field():
    schema = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"),
        entity_key=("id",),
        required_fields=("id", "name"),
    )
    df = pl.DataFrame({"id": [1]})
    with pytest.raises(SchemaError):
        schema.validate(df)


# -- valid_time_column / valid_time_format (blueprint §3.2, "Valid time is
#    per ROW, not per batch", decision 2026-08-22) --------------------------


def test_fact_table_schema_defaults_to_no_declared_valid_time_column():
    schema = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"),
        entity_key=("id",),
        required_fields=("id",),
    )
    assert schema.valid_time_column is None
    assert schema.valid_time_format is None


def test_fact_table_schema_rejects_valid_time_column_not_in_required_fields():
    # Same rule entity_key already follows -- a declared valid-time column
    # that isn't guaranteed present can't be resolved against.
    with pytest.raises(SchemaError):
        FactTableSchema(
            capability=CapabilityKey("x", "y", "z"),
            entity_key=("id",),
            required_fields=("id",),  # "kickoff" missing
            valid_time_column="kickoff",
        )


def test_fact_table_schema_rejects_a_format_without_a_declared_column():
    with pytest.raises(SchemaError):
        FactTableSchema(
            capability=CapabilityKey("x", "y", "z"),
            entity_key=("id",),
            required_fields=("id",),
            valid_time_format="%Y-%m-%d",  # valid_time_column is None
        )


def test_fact_table_schema_accepts_a_declared_valid_time_column_with_format():
    schema = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"),
        entity_key=("id",),
        required_fields=("id", "kickoff"),
        valid_time_column="kickoff",
        valid_time_format="%Y-%m-%dT%H:%M:%SZ",
    )
    assert schema.valid_time_column == "kickoff"
    assert schema.valid_time_format == "%Y-%m-%dT%H:%M:%SZ"


def test_fact_table_schema_accepts_a_declared_valid_time_column_with_no_format():
    # None means "already a native (naive UTC) Datetime column" — a
    # legitimate declaration on its own, no format required.
    schema = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"),
        entity_key=("id",),
        required_fields=("id", "as_of_marker"),
        valid_time_column="as_of_marker",
    )
    assert schema.valid_time_column == "as_of_marker"
    assert schema.valid_time_format is None


def test_player_gameweek_stats_declares_kickoff_time_as_its_valid_time_column():
    from fplai.schemas import PLAYER_GAMEWEEK_STATS_GAMEWEEK

    schema = CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK]
    assert schema.valid_time_column == "kickoff_time"
    assert schema.valid_time_format == "%Y-%m-%dT%H:%M:%SZ"


def test_dataset_valid_time_columns_contains_exactly_the_declaring_datasets():
    # A dataset appears in DATASET_VALID_TIME_COLUMNS if and only if its
    # schema declares valid_time_column — no dataset should be present with
    # a None entry (that would defeat store._resolve_valid_time_column's
    # "raise, don't guess" contract for the absent case) and no declaring
    # dataset should be missing.
    from fplai.schemas import DATASET_VALID_TIME_COLUMNS, _DATASET_TO_CAPABILITY

    expected = {
        dataset
        for dataset, capability in _DATASET_TO_CAPABILITY.items()
        if CANONICAL_SCHEMAS[capability].valid_time_column is not None
    }
    assert set(DATASET_VALID_TIME_COLUMNS.keys()) == expected
    # Session s005 -- fpl_api_player_gameweek_stats is a SECOND dataset
    # backing the SAME PLAYER_GAMEWEEK_STATS_GAMEWEEK capability (see
    # fplai.providers.fpl's module docstring), so it declares the same
    # valid_time_column ("kickoff_time") the vaastav dataset already does.
    assert expected == {"vaastav_player_gameweek_stats", "fpl_api_player_gameweek_stats"}
    for column, fmt in DATASET_VALID_TIME_COLUMNS.values():
        assert column is not None


def test_fact_table_schema_singleton_allows_empty_entity_key():
    schema = FactTableSchema(
        capability=CapabilityKey("game", "config", "current"),
        entity_key=(),
        required_fields=("payload",),
    )
    schema.validate(pl.DataFrame({"payload": ["{}"]}))


def test_is_modelled_defaults_false_and_is_settable():
    observed = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"), entity_key=("id",), required_fields=("id",)
    )
    derived = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"), entity_key=("id",), required_fields=("id",), is_modelled=True
    )
    assert observed.is_modelled is False
    assert derived.is_modelled is True


def test_canonical_schemas_cover_all_fpl_pl_vaastav_olbauday_and_odds_capabilities():
    # The seven FPL-provider writes (E2b story 1), the five PL-provider
    # capabilities (E2b stories 6-7), the three vaastav-archive capabilities
    # (story 10's two plus story 7b's `team.identity@season`, closing the
    # player/team asymmetry §12.5 flags), the two olbauday-archive
    # capabilities from E2b story 10b, and the two Odds-API capabilities
    # from E2b story 8. `player.defensive_actions@match` is deliberately
    # absent — no adapter serves it (blueprint §12.1). `player.shots_on_
    # target@fixture` is deliberately absent too — deferred, not built
    # (blueprint §3.3, providers/odds.py's module docstring).
    #
    # OBSERVED capabilities only (is_modelled=False) — Architect ruling,
    # 2026-08-22 (session s003): this assertion is partitioned from the
    # DERIVED side (see test_derived_capabilities_registered_at_import_time_
    # cover_exactly_the_registered_model_modules below) rather than either
    # loosened to "contains at least" (which would stop catching an
    # accidental derived dataset landing among these) or made to also cover
    # derived capabilities (which would make this assertion's result depend
    # on which model modules happened to be imported elsewhere in the same
    # session — CLAUDE.md rule 7). `fplai.schemas.observed_capabilities()`
    # is the one place this partition is computed.
    #
    # `job.heartbeat@run` (session s003, PROGRESS.md E2) is also here even
    # though it is not from a provider adapter: it is a genuine observation
    # (a job DID run and DID see these payload hashes), never a statistical
    # inference, so it belongs on the observed side of the partition, not
    # is_modelled=True.
    from fplai.schemas import observed_capabilities

    expected = {
        "player.attributes@current",
        "team.attributes@current",
        "gameweek.attributes@season",
        "chip.window@season",
        "game.config@current",
        "game.settings@current",
        "manager_picks.selection@gameweek",
        "manager_picks.automatic_subs@gameweek",
        "match.lineups@match",
        "match.substitutions@match",
        "team.match_stats@match",
        "player.season_stats@season",
        "match.fixtures@matchweek",
        # Session s004 — PL API match officials/referee. Observed, not
        # derived: it is a name+role reported by the source, no fitted
        # parameter, no residual.
        "match.officials@match",
        "player.gameweek_stats@gameweek",
        "player.identity@season",
        "team.identity@season",
        "player.attributes@gameweek",
        "gameweek.field_summary@gameweek",
        "match.odds@fixture",
        "player.goal_odds@fixture",
        "job.heartbeat@run",
        # Session s004 — DC threshold pins observed from event/{gw}/live/.
        # Observed, not derived (Architect ruling 2026-08-27): a pin is a
        # deterministic bounds-meeting computation over real (count, points)
        # pairs, with no fitted parameter and no residual, so §12.2's
        # derived-provenance contract would be meaningless for it. Same
        # reasoning as job.heartbeat@run directly above.
        "game.dc_threshold_observation@gameweek",
    }
    assert {str(k) for k in observed_capabilities()} == expected


def test_derived_capabilities_registered_at_import_time_cover_exactly_the_registered_model_modules():
    # The DERIVED-side exact-set assertion this partition strengthens over
    # (nothing enforced this before the Architect's 2026-08-22 ruling — see
    # docs/wiki/model-team-strength.md §10). A derived capability registers
    # at its owning model module's IMPORT time (blueprint §15.4's stated
    # intention, finally honoured without corrupting this test's exact-set
    # check — see fplai.schemas' "Phase 2, E5" section for the full
    # account), so this test imports every model module whose derived
    # capability it expects to see, making its own result a pure function
    # of which modules were imported — not of test-execution order or
    # which OTHER test files pytest happened to collect alongside this one.
    # Grown to include fplai.models.minutes (Phase 2, E5, session s003) —
    # the second real derived-capability module to import-time-register,
    # exactly the generalisation model-team-strength.md §10 anticipated.
    # Grown again to include fplai.models.defensive_contribution (same
    # session, third module) — same convention.
    # Grown again to include fplai.models.attacking (session s004, FOURTH
    # module) — same convention.
    # Grown again to include fplai.models.bonus (session s004, FIFTH
    # module) — same convention.
    # Grown again to include fplai.models.cards (session s004, SIXTH
    # module) — same convention.
    # Grown again to include fplai.models.saves (session s005, SEVENTH
    # module, the last gap in goalkeeper points) — same convention.
    import fplai.models.minutes  # noqa: F401  registers player.minutes_distribution@gameweek
    import fplai.models.team_strength  # noqa: F401  registers team.strength_rating@gameweek
    import fplai.models.defensive_contribution  # noqa: F401  registers player.defensive_contribution_distribution@gameweek
    import fplai.models.attacking  # noqa: F401  registers player.attacking_involvement_distribution@gameweek
    import fplai.models.bonus  # noqa: F401  registers player.bonus_distribution@gameweek
    import fplai.models.cards  # noqa: F401  registers player.cards_distribution@gameweek
    import fplai.models.saves  # noqa: F401  registers player.saves_distribution@gameweek

    from fplai.schemas import derived_capabilities

    assert {str(k) for k in derived_capabilities()} == {
        "team.strength_rating@gameweek",
        "player.defensive_contribution_distribution@gameweek",
        "player.minutes_distribution@gameweek",
        "player.attacking_involvement_distribution@gameweek",
        "player.bonus_distribution@gameweek",
        "player.cards_distribution@gameweek",
        "player.saves_distribution@gameweek",
    }


def test_canonical_schemas_are_all_internally_consistent():
    # Every registered schema's own entity_key/required_fields invariant
    # holds (this would already be enforced by __post_init__ at import
    # time, but assert it explicitly so a future edit that breaks it fails
    # here rather than only via a downstream adapter test).
    for capability, schema in CANONICAL_SCHEMAS.items():
        assert schema.capability == capability
        assert all(k in schema.required_fields for k in schema.entity_key)


# -- DATASET_ENTITY_KEYS (blueprint §3.2, decision 2026-08-20) -------------


def test_manager_picks_entity_key_includes_event_not_just_entry_and_element():
    # A pick row's identity is (entry, GAMEWEEK, element) -- omitting the
    # gameweek would make BitemporalStore.as_of()'s state-collapse merge
    # different gameweeks' picks for the same entry+element into one row,
    # silently losing history the moment as_of() started using declared
    # entity keys instead of returning the raw stream.
    schema = CANONICAL_SCHEMAS[MANAGER_PICKS_SELECTION_GAMEWEEK]
    assert schema.entity_key == ("entry_id", "event", "element")


# -- automatic_subs, blueprint §3.4 "Blocker 3", session s003 -------------


def test_automatic_subs_is_a_different_capability_and_dataset_from_picks():
    # The grain-mismatch ruling this story makes: automatic_subs[] is a
    # TOP-LEVEL list in the picks/ payload, a genuinely different entity
    # from a picks[] row — it must never be registered under the same
    # capability key or flattened into the picks entity key (CLAUDE.md
    # lesson 2's third instance would otherwise repeat: picks omitting
    # `event`, vaastav_player_gameweek_stats omitting `fixture`).
    from fplai.schemas import MANAGER_AUTOMATIC_SUBS_GAMEWEEK

    assert MANAGER_AUTOMATIC_SUBS_GAMEWEEK != MANAGER_PICKS_SELECTION_GAMEWEEK
    assert DATASET_ENTITY_KEYS["automatic_subs"] != DATASET_ENTITY_KEYS["picks"]


def test_automatic_subs_entity_key_is_entry_event_element_out():
    from fplai.schemas import MANAGER_AUTOMATIC_SUBS_GAMEWEEK

    schema = CANONICAL_SCHEMAS[MANAGER_AUTOMATIC_SUBS_GAMEWEEK]
    assert schema.entity_key == ("entry_id", "event", "element_out")
    assert "element_in" in schema.required_fields  # present, even though not part of the key


def test_automatic_subs_dataset_registered():
    from fplai.schemas import MANAGER_AUTOMATIC_SUBS_GAMEWEEK

    assert DATASET_ENTITY_KEYS["automatic_subs"] == ("entry_id", "event", "element_out")
    assert _dataset_for(MANAGER_AUTOMATIC_SUBS_GAMEWEEK) == "automatic_subs"


def _dataset_for(capability):
    from fplai.schemas import capability_dataset

    return capability_dataset(capability)


def test_automatic_subs_validates_a_real_shaped_row():
    from fplai.schemas import MANAGER_AUTOMATIC_SUBS_GAMEWEEK

    df = pl.DataFrame({"entry_id": [3434577], "event": [1], "element_in": [200], "element_out": [100]})
    CANONICAL_SCHEMAS[MANAGER_AUTOMATIC_SUBS_GAMEWEEK].validate(df)  # must not raise


# -- picks widening, blueprint §3.4 "Blocker 3", session s003 --------------


def test_picks_overall_rank_is_now_required_and_accepts_all_null():
    # overall_rank was already produced by every existing writer (both
    # sample_picks.py's own row-builder and providers/fpl.py's parallel
    # implementation) but never declared required. Promoted to required at
    # zero risk; still legitimately all-null pre-scoring (blueprint §3.4).
    schema = CANONICAL_SCHEMAS[MANAGER_PICKS_SELECTION_GAMEWEEK]
    assert "overall_rank" in schema.required_fields
    df = pl.DataFrame(
        {
            "event": [1],
            "entry_id": [1],
            "element": [1],
            "multiplier": [1],
            "is_captain": [False],
            "is_vice_captain": [False],
            "overall_rank": [None],
        }
    )
    schema.validate(df)  # must not raise -- null family is accepted


def test_dataset_entity_keys_covers_every_store_dataset_this_slice_writes():
    # OBSERVED datasets only — see the partition rationale on
    # test_canonical_schemas_cover_all_fpl_pl_vaastav_olbauday_and_odds_
    # capabilities above; fplai.schemas.observed_dataset_entity_keys() is
    # the dataset-name-keyed mirror of observed_capabilities().
    from fplai.schemas import observed_dataset_entity_keys

    expected_datasets = {
        "elements",
        "teams",
        "events",
        "chips",
        "game_config",
        "game_settings",
        "picks",
        "automatic_subs",
        "pl_match_lineups",
        "pl_match_substitutions",
        "pl_team_match_stats",
        "pl_player_season_stats",
        "pl_match_fixtures",
        # Session s004 — see the capability-side note above.
        "pl_match_officials",
        "vaastav_player_gameweek_stats",
        # Session s005 -- FPL API's own second provider of the SAME
        # player.gameweek_stats@gameweek capability (see fplai.providers.
        # fpl's module docstring for the fixture-attribution design).
        "fpl_api_player_gameweek_stats",
        "vaastav_player_identity",
        "vaastav_team_identity",
        "olbauday_player_attributes",
        "olbauday_gameweek_field_summary",
        "odds_match_odds",
        "odds_player_goal_odds",
        "heartbeat",
        # Session s004 — see the capability-side note in
        # test_canonical_schemas_cover_all_fpl_pl_vaastav_olbauday_and_odds_
        # capabilities above.
        "dc_threshold_observations",
    }
    assert set(observed_dataset_entity_keys()) == expected_datasets


def test_derived_dataset_entity_keys_registered_at_import_time_cover_exactly_the_registered_model_modules():
    # DERIVED-side mirror of test_derived_capabilities_registered_at_import_
    # time_cover_exactly_the_registered_model_modules above — same reason
    # this test imports the model module itself rather than relying on
    # collection order.
    # Grown again to include fplai.models.saves (session s005, SEVENTH
    # module) — same convention.
    import fplai.models.minutes  # noqa: F401  registers derived_player_minutes_distribution
    import fplai.models.team_strength  # noqa: F401  registers derived_team_strength_rating
    import fplai.models.defensive_contribution  # noqa: F401  registers derived_player_defensive_contribution_distribution
    import fplai.models.attacking  # noqa: F401  registers derived_player_attacking_involvement_distribution
    import fplai.models.bonus  # noqa: F401  registers derived_player_bonus_distribution
    import fplai.models.cards  # noqa: F401  registers derived_player_cards_distribution
    import fplai.models.saves  # noqa: F401  registers derived_player_saves_distribution

    from fplai.schemas import derived_dataset_entity_keys

    assert set(derived_dataset_entity_keys()) == {
        "derived_team_strength_rating",
        "derived_player_minutes_distribution",
        "derived_player_defensive_contribution_distribution",
        "derived_player_attacking_involvement_distribution",
        "derived_player_bonus_distribution",
        "derived_player_cards_distribution",
        "derived_player_saves_distribution",
    }


def test_dataset_entity_keys_singletons_are_empty_tuples():
    assert DATASET_ENTITY_KEYS["game_config"] == ()
    assert DATASET_ENTITY_KEYS["game_settings"] == ()


def test_dataset_entity_keys_matches_canonical_schema_entity_key():
    assert DATASET_ENTITY_KEYS["elements"] == ("id",)
    assert DATASET_ENTITY_KEYS["teams"] == ("id",)
    assert DATASET_ENTITY_KEYS["events"] == ("id",)
    assert DATASET_ENTITY_KEYS["chips"] == ("id",)
    assert DATASET_ENTITY_KEYS["picks"] == ("entry_id", "event", "element")


# -- PL API capabilities, E2b stories 6-7 -----------------------------------


def test_player_defensive_actions_at_match_is_deliberately_unregistered():
    # blueprint §12.1's protection, exercised for real: no source exists
    # (docs/wiki/provider-evaluation.md, "P2 verdict: NEGATIVE"), so a query
    # for this capability must fail loudly via registry.RegistryError rather
    # than being silently answered by a season-grain provider.
    assert CapabilityKey("player", "defensive_actions", "match") not in CANONICAL_SCHEMAS


def test_team_match_stats_and_player_season_stats_are_long_format():
    # Both store one row per (entity..., stat_key) -> value, never one
    # column per Opta stat key (Architect decision for team.match_stats;
    # applied by XL-Coder to player.season_stats for the same reason).
    from fplai.schemas import PLAYER_SEASON_STATS_SEASON, TEAM_MATCH_STATS_MATCH

    assert "stat_key" in CANONICAL_SCHEMAS[TEAM_MATCH_STATS_MATCH].entity_key
    assert "value" in CANONICAL_SCHEMAS[TEAM_MATCH_STATS_MATCH].required_fields
    assert "stat_key" in CANONICAL_SCHEMAS[PLAYER_SEASON_STATS_SEASON].entity_key
    assert "value" in CANONICAL_SCHEMAS[PLAYER_SEASON_STATS_SEASON].required_fields


def test_match_lineups_entity_key_resolves_to_fpl_element_not_raw_pl_id():
    # blueprint §12.6: no provider-specific id may leak past the adapter —
    # the entity key is the FPL-native element id, not the raw PL player id.
    from fplai.schemas import MATCH_LINEUPS_MATCH

    assert CANONICAL_SCHEMAS[MATCH_LINEUPS_MATCH].entity_key == ("match_id", "player_element_id")


def test_match_fixtures_dataset_entity_key_is_match_id():
    assert DATASET_ENTITY_KEYS["pl_match_fixtures"] == ("match_id",)


# -- vaastav archive capabilities, E2b story 10 ------------------------------


def test_player_gameweek_stats_entity_key_includes_season_and_round():
    from fplai.schemas import PLAYER_GAMEWEEK_STATS_GAMEWEEK

    schema = CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK]
    # Entity key includes fixture to handle double/triple gameweeks where a player
    # plays multiple times in one round. Blueprint §3.2, fix 2026-08-21.
    assert schema.entity_key == ("season", "round", "element", "fixture")
    assert "fixture" in schema.required_fields  # part of the entity key
    assert "selected" in schema.required_fields  # the archived ownership, blueprint §3.4
    assert "value" in schema.required_fields  # point-in-time price


def test_player_identity_season_entity_key_is_season_and_id():
    from fplai.schemas import PLAYER_IDENTITY_SEASON

    schema = CANONICAL_SCHEMAS[PLAYER_IDENTITY_SEASON]
    assert schema.entity_key == ("season", "id")


def test_player_identity_season_does_not_require_opta_code():
    # blueprint §12.5 / schemas.py's module comment: opta_code is verified
    # ABSENT in 2016-17 and present in 2025-26 — required_fields must not
    # demand a column that a real, in-scope archive season lacks. The
    # missing-join failure belongs to fplai.identity.PlayerIdentityMap.build,
    # not to this schema's presence check.
    from fplai.schemas import PLAYER_IDENTITY_SEASON

    assert "opta_code" not in CANONICAL_SCHEMAS[PLAYER_IDENTITY_SEASON].required_fields
    df = pl.DataFrame(
        {
            "season": ["2016-17"],
            "id": [1],
            "code": [12345],
            "element_type": [1],
            "team": [1],
            "team_code": [3],
            "web_name": ["Test"],
            "first_name": ["T"],
            "second_name": ["Est"],
            "now_cost": [50],
        }
    )
    CANONICAL_SCHEMAS[PLAYER_IDENTITY_SEASON].validate(df)  # must not raise


# -- team.identity@season, E2b story 7b (blueprint §12.5's player/team asymmetry) --


def test_team_identity_season_entity_key_is_season_and_id():
    from fplai.schemas import TEAM_IDENTITY_SEASON

    schema = CANONICAL_SCHEMAS[TEAM_IDENTITY_SEASON]
    assert schema.entity_key == ("season", "id")


def test_team_identity_season_requires_code_unlike_player_identitys_opta_code():
    # Unlike PLAYER_IDENTITY_SEASON's opta_code (legitimately absent for
    # pre-opta seasons), `code` is present in every season vaastav's
    # teams.csv exists for at all (verified 2019-20 -> 2025-26) — it is the
    # actual join field fplai.identity.TeamIdentityMap uses (PL API team id
    # == FPL legacy `code`, not opta_code, which is always None for teams).
    from fplai.schemas import TEAM_IDENTITY_SEASON

    schema = CANONICAL_SCHEMAS[TEAM_IDENTITY_SEASON]
    assert "code" in schema.required_fields
    assert "name" in schema.required_fields
    assert "short_name" in schema.required_fields


def test_team_identity_season_dataset_entity_key():
    assert DATASET_ENTITY_KEYS["vaastav_team_identity"] == ("season", "id")


# -- olbauday archive capabilities, E2b story 10b ----------------------------


def test_player_attributes_gameweek_entity_key_is_season_gw_id():
    from fplai.schemas import PLAYER_ATTRIBUTES_GAMEWEEK

    schema = CANONICAL_SCHEMAS[PLAYER_ATTRIBUTES_GAMEWEEK]
    assert schema.entity_key == ("season", "gw", "id")
    assert "selected_by_percent" in schema.required_fields  # a PERCENTAGE, not vaastav's raw count


def test_player_attributes_gameweek_is_not_registered_against_vaastav_gameweek_stats():
    # blueprint §12.1's ruling for this story: an attribute SNAPSHOT
    # (price, ownership%, injury flag) must never share a capability key
    # with match PERFORMANCE (vaastav's total_points/minutes/fixture) —
    # doing so would let CapabilityRegistry silently substitute one for the
    # other on a priority tiebreak.
    from fplai.schemas import PLAYER_ATTRIBUTES_GAMEWEEK, PLAYER_GAMEWEEK_STATS_GAMEWEEK

    assert PLAYER_ATTRIBUTES_GAMEWEEK != PLAYER_GAMEWEEK_STATS_GAMEWEEK
    assert PLAYER_ATTRIBUTES_GAMEWEEK.measure != PLAYER_GAMEWEEK_STATS_GAMEWEEK.measure


def test_player_attributes_gameweek_does_not_require_2025_26_only_columns():
    # Real, live-verified schema drift (schemas.py's module comment):
    # 2024-2025's playerstats.csv has 58 columns, 2025-2026/2026-2027 have
    # 87 — web_name, news, defensive_contribution, tackles, recoveries,
    # minutes, goals_scored are all ABSENT for 2024-2025 and must not be
    # required, same drift philosophy as vaastav's opta_code.
    from fplai.schemas import PLAYER_ATTRIBUTES_GAMEWEEK

    schema = CANONICAL_SCHEMAS[PLAYER_ATTRIBUTES_GAMEWEEK]
    for absent_2024_25 in ("web_name", "news", "news_added", "defensive_contribution", "tackles", "recoveries", "minutes", "goals_scored"):
        assert absent_2024_25 not in schema.required_fields


def test_gameweek_field_summary_entity_key_is_season_id():
    from fplai.schemas import GAMEWEEK_FIELD_SUMMARY_GAMEWEEK

    schema = CANONICAL_SCHEMAS[GAMEWEEK_FIELD_SUMMARY_GAMEWEEK]
    assert schema.entity_key == ("season", "id")
    assert "chip_plays" in schema.required_fields
    assert "ranked_count" in schema.required_fields
    # snapshot_time is deliberately NOT required (corrected 2026-08-21):
    # verified live to be a stale file-generation stamp, not a genuine
    # per-gameweek observation time — see providers/olbauday.py's module
    # docstring. observed_at_source/observed_at_imputed are the CLAUDE.md
    # rule 3 label for the row's IMPUTED observed_at (from deadline_time),
    # which replaced it.
    assert "snapshot_time" not in schema.required_fields
    assert "observed_at_source" in schema.required_fields
    assert "observed_at_imputed" in schema.required_fields


def test_olbauday_dataset_entity_keys():
    assert DATASET_ENTITY_KEYS["olbauday_player_attributes"] == ("season", "gw", "id")
    assert DATASET_ENTITY_KEYS["olbauday_gameweek_field_summary"] == ("season", "id")


# -- dtype contract — closes the "presence, not dtype" gap ------------------
# docs/wiki/provider-framework.md §14.6: a genuinely tz-aware `commence_time`/
# `market_last_update` column passed validate() (presence-only) and reached
# the real store, only failing three layers downstream at a DuckDB read
# (needs pytz, not a project dependency). These tests reconstruct that
# incident directly against FactTableSchema.validate() and confirm the dtype
# contract now closes it, without breaking the legitimate cross-provider/
# cross-era dtype drift already documented above (Int32 vs Int64, opta_code
# present/absent, ...).


def _naive_odds_row(**overrides: object) -> dict:
    naive = datetime(2026, 8, 22, 15, 0)
    row = {
        "season": ["2026-27"],
        "provider_event_id": ["e1"],
        "home_team_code": [1],
        "away_team_code": [2],
        "commence_time": [naive],
        "bookmaker_key": ["x"],
        "bookmaker_title": ["X"],
        "market_key": ["h2h"],
        "market_last_update": [naive],
        "outcome_name": ["Home"],
        "outcome_price": [1.5],
        "outcome_point": [None],
    }
    row.update(overrides)
    return row


def test_validate_rejects_a_timezone_aware_datetime_column():
    # The exact incident, reconstructed: providers/odds.py originally
    # produced genuinely tz-aware commence_time/market_last_update before
    # the §14.6 fix. Before this dtype contract existed, this call did not
    # raise (proven live, 2026-08-21) — it must raise now.
    from fplai.schemas import MATCH_ODDS_FIXTURE

    aware = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)
    df = pl.DataFrame(_naive_odds_row(commence_time=[aware], market_last_update=[aware]))
    with pytest.raises(SchemaError, match="timezone-AWARE"):
        CANONICAL_SCHEMAS[MATCH_ODDS_FIXTURE].validate(df)


def test_validate_checks_every_column_for_timezone_awareness_not_only_required_ones():
    # The tz check applies to the WHOLE payload, not just required_fields —
    # an optional/extra tz-aware column would reach the store just as
    # unreadably as a required one.
    schema = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"), entity_key=("id",), required_fields=("id",)
    )
    aware = datetime(2026, 8, 22, tzinfo=timezone.utc)
    df = pl.DataFrame({"id": [1], "extra_not_required": [aware]})
    with pytest.raises(SchemaError, match="timezone-AWARE"):
        schema.validate(df)


def test_validate_accepts_naive_datetime_columns():
    from fplai.schemas import MATCH_ODDS_FIXTURE

    df = pl.DataFrame(_naive_odds_row())
    CANONICAL_SCHEMAS[MATCH_ODDS_FIXTURE].validate(df)  # must not raise


def test_validate_rejects_nested_dtype_on_a_required_field():
    # A Struct/List landing where a scalar is expected is the "genuinely
    # broken/reshaped response" class this module's docstring already
    # names as the reason required_fields validation exists at all.
    schema = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"), entity_key=("id",), required_fields=("id", "payload")
    )
    df = pl.DataFrame({"id": [1], "payload": [{"nested": 1}]})
    with pytest.raises(SchemaError, match="fits none of this store's six accepted families"):
        schema.validate(df)


def test_validate_accepts_an_all_null_required_field():
    # Real, already-documented case, not hypothetical: MATCH_ODDS_FIXTURE's
    # outcome_point is required and legitimately NULL for every row of an
    # h2h/h2h_lay outcome (this capability's own schema description).
    # Rejecting an all-null required column would fail a schema-conformant
    # capture for a reason unrelated to the presence-not-dtype gap.
    from fplai.schemas import MATCH_ODDS_FIXTURE

    df = pl.DataFrame(_naive_odds_row(outcome_point=[None]))
    assert df.schema["outcome_point"] == pl.Null
    CANONICAL_SCHEMAS[MATCH_ODDS_FIXTURE].validate(df)  # must not raise


def test_validate_accepts_int32_vs_int64_drift_across_a_required_field():
    # Story 10b's whole record is legitimate cross-season/cross-provider
    # dtype drift for the same logical field — a CLASS check, not exact
    # equality, must tolerate it.
    schema = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"), entity_key=("id",), required_fields=("id",)
    )
    df32 = pl.DataFrame({"id": pl.Series([1], dtype=pl.Int32)})
    df64 = pl.DataFrame({"id": pl.Series([1], dtype=pl.Int64)})
    schema.validate(df32)  # must not raise
    schema.validate(df64)  # must not raise


def test_validate_accepts_string_vs_float_drift_for_the_same_logical_field():
    # elements.selected_by_percent is stored VARCHAR (raw API string);
    # vaastav_player_identity's equivalent field is DOUBLE (parsed) —
    # verified against the real store, both legitimate for their own
    # capability. The class check must accept either, per capability.
    schema = FactTableSchema(
        capability=CapabilityKey("x", "y", "z"), entity_key=("id",), required_fields=("id", "pct")
    )
    schema.validate(pl.DataFrame({"id": [1], "pct": ["14.9"]}))  # must not raise
    schema.validate(pl.DataFrame({"id": [1], "pct": [14.9]}))  # must not raise


# -- job.heartbeat@run, session s003 (PROGRESS.md E2) ----------------------


def test_heartbeat_entity_key_includes_job_and_run_ts_not_just_target_dataset():
    # Event-stream grain (module comment above JOB_HEARTBEAT_RUN): omitting
    # run_ts would let as_of()'s state-collapse merge every run this job has
    # ever made into one row per target_dataset, defeating the whole point
    # of check_heartbeat.py needing the full run history.
    from fplai.schemas import JOB_HEARTBEAT_RUN

    schema = CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN]
    assert schema.entity_key == ("job", "run_ts", "target_dataset")
    assert schema.is_modelled is False  # a real observation, not a derived fact


def test_heartbeat_dataset_registered_as_heartbeat():
    from fplai.schemas import JOB_HEARTBEAT_RUN

    assert DATASET_ENTITY_KEYS["heartbeat"] == ("job", "run_ts", "target_dataset")
    assert _dataset_for(JOB_HEARTBEAT_RUN) == "heartbeat"


def test_heartbeat_required_fields_cover_outcome_hash_and_error():
    from fplai.schemas import JOB_HEARTBEAT_RUN

    schema = CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN]
    for field in ("job", "run_ts", "target_dataset", "outcome", "payload_hash", "n_rows", "error"):
        assert field in schema.required_fields


def test_heartbeat_entity_key_is_unique_within_one_realistic_run_batch():
    # CLAUDE.md lesson 2 -- verify uniqueness against a realistic batch
    # shape (one job, one run_ts, six target datasets, matching
    # snapshot_bootstrap.py's own DATASET_CAPABILITIES), not just assert it
    # in a comment.
    from fplai.schemas import JOB_HEARTBEAT_RUN

    schema = CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN]
    run_ts = datetime(2026, 8, 22, 12, 0)
    rows = [
        {
            "job": "snapshot_bootstrap",
            "run_ts": run_ts,
            "target_dataset": name,
            "outcome": "written",
            "payload_hash": f"hash-{name}",
            "n_rows": 1,
            "error": None,
        }
        for name in ("elements", "teams", "events", "chips", "game_config", "game_settings")
    ]
    df = pl.DataFrame(rows)
    schema.validate(df)  # must not raise
    keys = list(zip(df["job"], df["run_ts"], df["target_dataset"]))
    assert len(keys) == len(set(keys))


def test_heartbeat_validates_a_failed_outcome_row_with_null_hash_and_rows():
    # A "failed" outcome legitimately has no content hash and no row count
    # (nothing was fetched/written) -- must still validate, same null-family
    # precedent as MATCH_ODDS_FIXTURE.outcome_point.
    from fplai.schemas import JOB_HEARTBEAT_RUN

    schema = CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN]
    df = pl.DataFrame(
        {
            "job": ["snapshot_bootstrap"],
            "run_ts": [datetime(2026, 8, 22, 12, 0)],
            "target_dataset": ["elements"],
            "outcome": ["failed"],
            "payload_hash": [None],
            "n_rows": [None],
            "error": ["ProviderError: bootstrap-static timed out"],
        }
    )
    schema.validate(df)  # must not raise


def test_validate_error_message_reports_the_dataset_and_offending_columns():
    from fplai.schemas import MATCH_ODDS_FIXTURE

    aware = datetime(2026, 8, 22, tzinfo=timezone.utc)
    df = pl.DataFrame(_naive_odds_row(commence_time=[aware]))
    with pytest.raises(SchemaError) as exc_info:
        CANONICAL_SCHEMAS[MATCH_ODDS_FIXTURE].validate(df)
    assert "commence_time" in str(exc_info.value)
