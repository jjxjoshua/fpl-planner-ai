"""Tests for fplai.backtest.data — season loading, coverage-gap detection,
and the GKP/AM position-drift normalisation found while building the Phase 1
harness. No network; a temp directory stands in for the store."""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from fplai.backtest.data import SeasonDataError, load_season, rows_before_round, rows_for_round
from fplai.store import BitemporalStore

UTC = timezone.utc


def _base_row(**overrides) -> dict:
    row = {
        "season": "2099-00",
        "round": 1,
        "element": 1,
        "name": "Test Player",
        "position": "MID",
        "team": "Testton",
        "value": 55,
        "selected": 1000,
        "total_points": 2,
        "minutes": 90,
        "fixture": 1,
        "was_home": True,
        "opponent_team": 2,
        "kickoff_time": "2099-08-01T14:00:00Z",
        "team_a_score": 1,
        "team_h_score": 1,
    }
    row.update(overrides)
    return row


@pytest.fixture
def store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def _write(store: BitemporalStore, rows: list[dict]) -> None:
    df = pl.DataFrame(rows)
    store.write(
        "vaastav_player_gameweek_stats",
        df,
        valid_at=datetime(2099, 8, 1, tzinfo=UTC),
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
        source="test",
    )


def test_missing_season_raises(store):
    _write(store, [_base_row(season="2099-00")])
    with pytest.raises(SeasonDataError):
        load_season(store, "2000-01")


def test_empty_dataset_raises(store):
    with pytest.raises(SeasonDataError):
        load_season(store, "2099-00")


def test_coverage_detects_missing_round(store):
    rows = [_base_row(round=r, element=1) for r in (1, 2, 4)]
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=4)
    assert data.coverage.missing_rounds == (3,)
    assert not data.coverage.is_complete
    assert data.rounds() == [1, 2, 4]


def test_coverage_complete_when_all_rounds_present(store):
    rows = [_base_row(round=r, element=1) for r in (1, 2, 3)]
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=3)
    assert data.coverage.is_complete
    assert data.coverage.coverage_fraction == 1.0


def test_2019_20_all_null_position_raises(store):
    rows = [_base_row(season="2019-20", round=1, position=None, team=None)]
    _write(store, rows)
    with pytest.raises(SeasonDataError):
        load_season(store, "2019-20")


def test_gkp_normalised_to_gk(store):
    rows = [_base_row(round=1, element=1, position="GKP")]
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=1)
    assert data.frame["position"].to_list() == ["GK"]


def test_am_rows_dropped(store):
    rows = [
        _base_row(round=1, element=1, position="MID"),
        _base_row(round=1, element=2, position="AM", name="Some Manager", team="Testton"),
    ]
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=1)
    assert data.frame["element"].to_list() == [1]
    assert "AM" not in data.frame["position"].to_list()


def test_rows_before_round_excludes_current_and_future(store):
    rows = [_base_row(round=r, element=1, total_points=r) for r in (1, 2, 3)]
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=3)
    history = rows_before_round(data, 3)
    assert sorted(history["round"].to_list()) == [1, 2]

    outcome = rows_for_round(data, 3)
    assert outcome["round"].to_list() == [3]


def test_double_gameweek_rows_both_present(store):
    # Same (season, round, element) key, two different fixtures — must NOT
    # collapse to one row (this is exactly why data.py uses observations(),
    # never as_of() — see module docstring).
    rows = [
        _base_row(round=5, element=9, fixture=101, total_points=6),
        _base_row(round=5, element=9, fixture=102, total_points=4),
    ]
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=5)
    both = rows_for_round(data, 5).filter(pl.col("element") == 9)
    assert both.height == 2
    assert sorted(both["total_points"].to_list()) == [4, 6]
