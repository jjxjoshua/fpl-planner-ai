"""Tests for fplai.backtest.report — distribution summaries."""

from __future__ import annotations

import pytest

from fplai.backtest.replay import GameweekResult
from fplai.backtest.report import summarise


def _result(gw: int, points: int) -> GameweekResult:
    return GameweekResult(
        season="2099-00",
        gameweek=gw,
        points=points,
        active_xi_ids=frozenset(),
        autosubs=(),
        effective_captain_id=None,
    )


def test_summarise_totals_and_mean():
    results = [_result(1, 10), _result(2, 20), _result(3, 30)]
    summary = summarise("2099-00", "test", results)
    assert summary.total_points == 60
    assert summary.mean == pytest.approx(20.0)
    assert summary.median == pytest.approx(20.0)
    assert summary.minimum == 10
    assert summary.maximum == 30
    assert summary.n_gameweeks == 3


def test_summarise_counts_zero_point_gameweeks():
    results = [_result(1, 0), _result(2, 5), _result(3, 0)]
    summary = summarise("2099-00", "test", results)
    assert summary.zero_point_gameweeks == 2


def test_summarise_empty_raises():
    with pytest.raises(ValueError):
        summarise("2099-00", "test", [])
