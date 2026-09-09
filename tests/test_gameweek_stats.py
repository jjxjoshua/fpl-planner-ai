"""Tests for fplai.gameweek_stats — the capability-level union reader for
player.gameweek_stats@gameweek (session s005). Temp store (tmp_path), no
network — rows are written directly via BitemporalStore.write() with the
minimal shape each provider's own schema requires, rather than going
through either provider adapter (those are exercised by tests/
test_provider_vaastav.py and tests/test_provider_fpl.py respectively);
this file is only about the UNION/bitemporal-resolution behaviour on top.
"""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from fplai.gameweek_stats import (
    FPL_API_DATASET,
    VAASTAV_DATASET,
    read_player_gameweek_stats,
    resolve_elements_and_teams_as_of_deadline,
)
from fplai.store import BitemporalStore

UTC = timezone.utc


@pytest.fixture
def store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def dt(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 8, day, hour, 0, tzinfo=UTC)


def _vaastav_row(*, season="2025-26", round_=1, element=1, fixture=1, total_points=6, xp=4.2, **overrides):
    row = {
        "season": season, "round": round_, "element": element, "fixture": fixture,
        "name": "Test Player", "total_points": total_points, "minutes": 90,
        "selected": 550000, "value": 55, "was_home": True,
        "team_a_score": 0, "team_h_score": 3,
        "kickoff_time": f"2025-08-{17 + round_:02d}T15:30:00Z",
        "xP": xp,
    }
    row.update(overrides)
    return row


def _fpl_api_row(*, season="2026-27", round_=1, element=1, fixture=1, total_points=6, attribution_complete=True, **overrides):
    row = {
        "season": season, "round": round_, "element": element, "fixture": fixture,
        "name": "Test Player", "total_points": total_points, "minutes": 90,
        "selected": None, "value": None, "was_home": True,
        "team_a_score": 0, "team_h_score": 3,
        "kickoff_time": f"2026-08-{21 + round_:02d}T19:00:00Z",
        "attribution_complete": attribution_complete,
    }
    row.update(overrides)
    return row


def _write_vaastav(store: BitemporalStore, rows: list[dict], *, valid_at: datetime, observed_at: datetime) -> None:
    df = pl.DataFrame(rows, infer_schema_length=None).with_columns(
        pl.col("selected").cast(pl.Int64), pl.col("value").cast(pl.Int64)
    )
    store.write(VAASTAV_DATASET, df, valid_at=valid_at, observed_at=observed_at, source="test:vaastav")


def _write_fpl_api(store: BitemporalStore, rows: list[dict], *, valid_at: datetime, observed_at: datetime) -> None:
    df = pl.DataFrame(rows, infer_schema_length=None).with_columns(
        pl.col("selected").cast(pl.Int64), pl.col("value").cast(pl.Int64),
        pl.col("attribution_complete").cast(pl.Boolean),
    )
    store.write(FPL_API_DATASET, df, valid_at=valid_at, observed_at=observed_at, source="test:fpl_api")


# -- empty store -------------------------------------------------------------


def test_both_sources_empty_returns_empty_frame(store):
    result = read_player_gameweek_stats(store, as_of=dt(25))
    assert result.is_empty()


# -- single-source reads ------------------------------------------------------


def test_vaastav_only_gets_provenance_and_attribution_complete_true(store):
    _write_vaastav(store, [_vaastav_row()], valid_at=dt(17), observed_at=dt(20))
    result = read_player_gameweek_stats(store, as_of=dt(25))
    assert result.height == 1
    row = result.to_dicts()[0]
    assert row["source_provider"] == "vaastav_archive"
    assert row["attribution_complete"] is True
    assert row["xP"] == 4.2


def test_fpl_api_only_gets_provenance_and_preserves_its_own_attribution_complete(store):
    _write_fpl_api(store, [_fpl_api_row(attribution_complete=False)], valid_at=dt(23), observed_at=dt(24))
    result = read_player_gameweek_stats(store, as_of=dt(25))
    assert result.height == 1
    row = result.to_dicts()[0]
    assert row["source_provider"] == "fpl_api"
    # NOT overwritten to True by the reader -- the provider's own degraded-
    # row flag (double gameweek) must survive the union unchanged.
    assert row["attribution_complete"] is False
    assert row["selected"] is None
    assert row["value"] is None


# -- union across both sources ------------------------------------------------


def test_both_sources_present_unions_with_correct_counts(store):
    _write_vaastav(store, [_vaastav_row(season="2025-26", element=1), _vaastav_row(season="2025-26", element=2)],
                    valid_at=dt(17), observed_at=dt(20))
    _write_fpl_api(store, [_fpl_api_row(season="2026-27", element=1)], valid_at=dt(23), observed_at=dt(24))

    result = read_player_gameweek_stats(store, as_of=dt(25))
    assert result.height == 3
    counts = dict(zip(*result["source_provider"].value_counts().to_dict(as_series=False).values()))
    assert counts == {"vaastav_archive": 2, "fpl_api": 1}


def test_season_filter_applies_to_both_sources_independently(store):
    _write_vaastav(
        store,
        [_vaastav_row(season="2024-25", element=1), _vaastav_row(season="2025-26", element=1)],
        valid_at=dt(17), observed_at=dt(20),
    )
    _write_fpl_api(store, [_fpl_api_row(season="2026-27", element=1)], valid_at=dt(23), observed_at=dt(24))

    result = read_player_gameweek_stats(store, as_of=dt(25), season="2025-26")
    assert result.height == 1
    assert result["season"].to_list() == ["2025-26"]
    assert result["source_provider"].to_list() == ["vaastav_archive"]


def test_season_filter_excluding_everything_returns_empty(store):
    _write_vaastav(store, [_vaastav_row(season="2025-26")], valid_at=dt(17), observed_at=dt(20))
    result = read_player_gameweek_stats(store, as_of=dt(25), season="2099-00")
    assert result.is_empty()


# -- column alignment ----------------------------------------------------------


def test_vaastav_only_column_is_null_for_fpl_sourced_rows_after_union(store):
    # xP exists on vaastav's rows and has no FPL-API-provider counterpart
    # at all -- this reader must not invent a value for it.
    _write_vaastav(store, [_vaastav_row(season="2025-26", xp=7.3)], valid_at=dt(17), observed_at=dt(20))
    _write_fpl_api(store, [_fpl_api_row(season="2026-27")], valid_at=dt(23), observed_at=dt(24))

    result = read_player_gameweek_stats(store, as_of=dt(25))
    by_provider = {r["source_provider"]: r for r in result.to_dicts()}
    assert by_provider["vaastav_archive"]["xP"] == 7.3
    assert by_provider["fpl_api"]["xP"] is None


def test_selected_and_value_are_null_only_on_fpl_sourced_rows(store):
    _write_vaastav(store, [_vaastav_row(season="2025-26")], valid_at=dt(17), observed_at=dt(20))
    _write_fpl_api(store, [_fpl_api_row(season="2026-27")], valid_at=dt(23), observed_at=dt(24))
    result = read_player_gameweek_stats(store, as_of=dt(25))
    by_provider = {r["source_provider"]: r for r in result.to_dicts()}
    assert by_provider["vaastav_archive"]["selected"] == 550000
    assert by_provider["vaastav_archive"]["value"] == 55
    assert by_provider["fpl_api"]["selected"] is None
    assert by_provider["fpl_api"]["value"] is None


# -- bitemporal correctness: effective_at(), not a trivial passthrough --------


def test_as_of_before_kickoff_excludes_the_row_from_both_sources(store):
    # Proves this reader actually resolves VALID time per row (kickoff_
    # time), not just "return everything ever written" -- effective_at()'s
    # own boundary is STRICTLY before kickoff.
    _write_vaastav(store, [_vaastav_row(season="2025-26")], valid_at=dt(17), observed_at=dt(20))
    _write_fpl_api(store, [_fpl_api_row(season="2026-27")], valid_at=dt(23), observed_at=dt(24))

    # vaastav row's kickoff_time is 2025-08-18T15:30:00Z; fpl_api row's is
    # 2026-08-22T19:00:00Z. A cutoff before EITHER kickoff must exclude both.
    result = read_player_gameweek_stats(store, as_of=datetime(2020, 1, 1, tzinfo=UTC))
    assert result.is_empty()


def test_as_of_between_the_two_kickoffs_returns_only_the_earlier_one(store):
    _write_vaastav(store, [_vaastav_row(season="2025-26", round_=1)], valid_at=dt(17), observed_at=dt(20))
    _write_fpl_api(store, [_fpl_api_row(season="2026-27", round_=1)], valid_at=dt(23), observed_at=dt(24))

    # Between the vaastav fixture's kickoff (2025-08-18) and the fpl_api
    # fixture's kickoff (2026-08-22).
    result = read_player_gameweek_stats(store, as_of=datetime(2026, 1, 1, tzinfo=UTC))
    assert result.height == 1
    assert result["source_provider"].to_list() == ["vaastav_archive"]


# -- no cross-source deduplication (documented behaviour, proven not assumed) --


def test_overlapping_entity_key_across_sources_returns_both_rows_undeduplicated(store):
    # Not a real scenario today (vaastav stops at 2025-26; the FPL API
    # provider is season-restricted at registration) but the reader itself
    # enforces no such disjointness -- see this module's docstring, "No
    # cross-source deduplication on the entity key". Contrived here to
    # prove the documented behaviour is real, not aspirational.
    shared = dict(season="2026-27", round_=1, element=1, fixture=1)
    _write_vaastav(store, [_vaastav_row(**shared)], valid_at=dt(17), observed_at=dt(20))
    _write_fpl_api(store, [_fpl_api_row(**shared)], valid_at=dt(23), observed_at=dt(24))

    result = read_player_gameweek_stats(store, as_of=dt(25))
    assert result.height == 2
    key_cols = ["season", "round", "element", "fixture"]
    assert result.select(key_cols).n_unique() == 1  # same key, both rows kept -- not deduplicated


# -- resolve_elements_and_teams_as_of_deadline: the ingest-time position/team
# resolution, and its leakage attack (session s005, continued) ---------------


def _write_elements(store: BitemporalStore, rows: list[dict], *, observed_at: datetime) -> None:
    # Minimal shape -- store.write() itself does not run FactTableSchema
    # validation (only providers/callers that opt into CANONICAL_SCHEMAS
    # do), so only the entity-key column ("id") is structurally required
    # for as_of() to collapse on. observed_at doubles as valid_at here --
    # this dataset carries no declared valid_time_column (see this
    # module's own comment block), so only observed_at matters.
    df = pl.DataFrame(rows, infer_schema_length=None)
    store.write("elements", df, valid_at=observed_at, observed_at=observed_at, source="test:elements")


def _write_teams(store: BitemporalStore, rows: list[dict], *, observed_at: datetime) -> None:
    df = pl.DataFrame(rows, infer_schema_length=None)
    store.write("teams", df, valid_at=observed_at, observed_at=observed_at, source="test:teams")


def test_resolve_elements_and_teams_as_of_deadline_empty_store_returns_empty_frames(store):
    resolved = resolve_elements_and_teams_as_of_deadline(store, "2026-08-21T17:30:00Z")
    assert resolved.elements.is_empty()
    assert resolved.teams.is_empty()
    assert resolved.deadline == datetime(2026, 8, 21, 17, 30, tzinfo=UTC)


def test_resolve_elements_and_teams_as_of_deadline_leakage_attack_picks_the_pre_deadline_team(store):
    # THE ATTACK: element 1 is at team 1 in every snapshot taken BEFORE the
    # gameweek's deadline, then transfers to team 2 in a snapshot taken
    # AFTER it (simulating a same-season mid-flight transfer -- fabricated
    # here, per the brief, since no real player in the store has changed
    # team since 2026-08-19 as of this session). A caller that reads
    # store.latest("elements") instead of resolving AS OF the deadline
    # would silently pick up the POST-transfer team on a HISTORICAL
    # gameweek's row -- exactly the leakage CLAUDE.md rule 2 names.
    _write_elements(store, [{"id": 1, "element_type": 2, "team": 1}], observed_at=dt(19))
    _write_elements(store, [{"id": 1, "element_type": 2, "team": 2}], observed_at=dt(23))
    _write_teams(store, [{"id": 1, "name": "Old Team"}, {"id": 2, "name": "New Team"}], observed_at=dt(19))

    deadline_between = "2026-08-21T00:00:00Z"  # strictly between the two elements batches
    resolved = resolve_elements_and_teams_as_of_deadline(store, deadline_between)
    row = resolved.elements.to_dicts()[0]
    assert row["team"] == 1  # the PRE-transfer team, never the post-transfer one
    assert row["team"] != 2

    # Symmetric check: a deadline AFTER the transfer correctly picks up
    # the NEW team -- this is not "always returns the earliest row", it
    # genuinely resolves the state that was true at the given instant.
    deadline_after = "2026-08-25T00:00:00Z"
    resolved_after = resolve_elements_and_teams_as_of_deadline(store, deadline_after)
    assert resolved_after.elements.to_dicts()[0]["team"] == 2


def test_resolve_elements_and_teams_as_of_deadline_before_any_observation_is_empty(store):
    _write_elements(store, [{"id": 1, "element_type": 2, "team": 1}], observed_at=dt(19))
    resolved = resolve_elements_and_teams_as_of_deadline(store, "2026-08-01T00:00:00Z")
    assert resolved.elements.is_empty()
