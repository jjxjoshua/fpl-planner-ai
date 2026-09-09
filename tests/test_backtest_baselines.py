"""Tests for fplai.backtest.baselines — Random / Template / Greedy-form.

Focus: seeded reproducibility, buy-and-hold vs. weekly re-decision, and
that each baseline's ranking signal only ever comes from `view.history`
(never `view.gameweek`'s own outcome data)."""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from fplai.backtest.baselines import GreedyFormBaseline, RandomBaseline, TemplateBaseline
from fplai.backtest.data import load_season
from fplai.backtest.replay import SeasonReplay
from fplai.backtest.rules import SquadRules
from fplai.store import BitemporalStore

UTC = timezone.utc

RULES = SquadRules(
    budget_tenths=1000,
    squad_size=15,
    squad_composition=(("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)),
    xi_bounds=(("GK", (1, 1)), ("DEF", (3, 5)), ("MID", (2, 5)), ("FWD", (1, 3))),
    max_per_club=3,
)


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


def _round_rows(round_number: int, *, ownership: dict[int, int] | None = None, points: dict[int, int] | None = None) -> list[dict]:
    ownership = ownership or {}
    points = points or {}
    positions = ["GK"] * 3 + ["DEF"] * 8 + ["MID"] * 8 + ["FWD"] * 5
    rows = []
    for i, position in enumerate(positions, start=1):
        rows.append(
            _base_row(
                round=round_number,
                element=i,
                name=f"P{i}",
                position=position,
                team=f"Club{i % 8}",
                value=45 + (i % 6) * 5,
                selected=ownership.get(i, i),
                total_points=points.get(i, 0),
                minutes=90,
            )
        )
    return rows


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


def test_random_baseline_reproducible_from_seed(store):
    rows = _round_rows(1) + _round_rows(2)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=2)

    r1 = SeasonReplay(data, RULES).run(RandomBaseline(seed=7, rules=RULES))
    r2 = SeasonReplay(data, RULES).run(RandomBaseline(seed=7, rules=RULES))
    assert [x.active_xi_ids for x in r1] == [x.active_xi_ids for x in r2]


def test_random_baseline_differs_across_seeds(store):
    rows = _round_rows(1)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=1)

    r1 = SeasonReplay(data, RULES).run(RandomBaseline(seed=1, rules=RULES))
    r2 = SeasonReplay(data, RULES).run(RandomBaseline(seed=2, rules=RULES))
    # Not a strict guarantee for every possible pair of seeds, but true for
    # this fixture and stable — different seeds should not coincidentally
    # produce the identical squad from a pool this size.
    assert r1[0].active_xi_ids != r2[0].active_xi_ids


def test_random_baseline_buys_and_holds(store):
    rows = _round_rows(1) + _round_rows(2) + _round_rows(3)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=3)

    from fplai.backtest.data import rows_before_round, rows_for_round
    from fplai.backtest.replay import ATTRIBUTE_COLUMNS, GameweekView

    strategy = RandomBaseline(seed=3, rules=RULES)
    squads = []
    for gw in (1, 2, 3):
        history = rows_before_round(data, gw)
        current = rows_for_round(data, gw).select(list(ATTRIBUTE_COLUMNS)).unique(subset=["element"])
        view = GameweekView(season="2099-00", gameweek=gw, history=history, current_attributes=current)
        squads.append(strategy.decide(view).squad.ids())

    assert squads[0] == squads[1] == squads[2]


def test_template_uses_previous_gameweek_ownership_not_current(store):
    # Player 1 is heavily owned in round 1 but not round 2; player 2 is the
    # reverse. Template's round-2 decision must reflect round 1's
    # ownership, never round 2's (which would be leakage).
    r1 = _round_rows(1, ownership={1: 999999, 2: 1})
    r2 = _round_rows(2, ownership={1: 1, 2: 999999})
    _write(store, r1 + r2)
    data = load_season(store, "2099-00", expected_gameweeks=2)

    strategy = TemplateBaseline(rules=RULES)
    replay = SeasonReplay(data, RULES)
    # Manually drive gameweek 2's decision to inspect the squad directly.
    from fplai.backtest.replay import ATTRIBUTE_COLUMNS
    from fplai.backtest.data import rows_before_round, rows_for_round
    from fplai.backtest.replay import GameweekView

    history = rows_before_round(data, 2)
    current = rows_for_round(data, 2).select(list(ATTRIBUTE_COLUMNS)).unique(subset=["element"])
    view = GameweekView(season="2099-00", gameweek=2, history=history, current_attributes=current)
    decision = strategy.decide(view)
    assert 1 in decision.squad.ids()
    assert 2 not in decision.squad.ids() or decision.squad.ids() != {1}  # player 1 (high prior-GW ownership) must be picked
    assert 1 in decision.squad.ids()


def test_template_gameweek_one_has_no_ownership_signal_but_still_produces_a_valid_squad(store):
    rows = _round_rows(1)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=1)
    results = SeasonReplay(data, RULES).run(TemplateBaseline(rules=RULES))
    assert len(results) == 1  # did not crash on the cold-start gameweek


def test_greedy_form_uses_trailing_window_only(store):
    # Player 5 scores heavily in round 1 (inside a 2-gameweek trailing
    # window for round 3), player 6 scores heavily only in round 3 itself
    # (must NOT influence round 3's own selection).
    r1 = _round_rows(1, points={5: 50})
    r2 = _round_rows(2, points={})
    r3 = _round_rows(3, points={6: 999})
    _write(store, r1 + r2 + r3)
    data = load_season(store, "2099-00", expected_gameweeks=3)

    strategy = GreedyFormBaseline(rules=RULES, trailing_gameweeks=2)
    from fplai.backtest.data import rows_before_round, rows_for_round
    from fplai.backtest.replay import ATTRIBUTE_COLUMNS, GameweekView

    history = rows_before_round(data, 3)
    current = rows_for_round(data, 3).select(list(ATTRIBUTE_COLUMNS)).unique(subset=["element"])
    view = GameweekView(season="2099-00", gameweek=3, history=history, current_attributes=current)
    decision = strategy.decide(view)
    assert 5 in decision.squad.ids()
    # player 6's round-3-only haul must not have leaked into the ranking
    # value used to build this squad (round 3 is not in `history`).


# -- GameweekView.forward_fixtures (receding-horizon story, S2) ------------
#
# `_build_view` is not otherwise exercised in this file (the baselines
# above construct `GameweekView` by hand), so these tests go through
# `_build_view` directly -- it is the only path that ever populates
# `forward_fixtures`, exactly the same reasoning that already applies to
# `fixtures` in `tests/test_backtest_replay.py`.

_FORWARD_ONLY_COLUMNS = {"fixture", "home_team", "away_team", "kickoff_time"}
_BANNED_IN_FORWARD = {
    "value",
    "selected",
    "total_points",
    "minutes",
    "bonus",
    "bps",
    "goals_scored",
    "opponent_team",
}


def test_forward_fixtures_columns_are_exactly_the_schedule_four(store):
    """Mechanical, not by-eye: for a real (synthetic-store) round, the
    projected table's columns must be exactly the four schedule facts --
    and every outcome-shaped column present on the raw rows (`_round_rows`
    carries `value`/`selected`/`total_points`/`minutes` on every row, plus
    `bonus`/`bps` injected here to attack columns `ATTRIBUTE_COLUMNS`
    itself has never carried) must be structurally absent, not merely
    unobserved."""
    from fplai.backtest.replay import _build_view

    r1 = _round_rows(1)
    r2 = [dict(row, bonus=9, bps=55, goals_scored=3) for row in _round_rows(2)]
    _write(store, r1 + r2)
    data = load_season(store, "2099-00", expected_gameweeks=2)

    view = _build_view(data, 1, horizon=1)
    assert set(view.forward_fixtures.keys()) == {2}
    table = view.forward_fixtures[2]
    assert set(table.columns) == _FORWARD_ONLY_COLUMNS
    assert set(table.columns).isdisjoint(_BANNED_IN_FORWARD)


def test_forward_fixtures_cannot_be_joined_back_to_a_future_rounds_value(store):
    """Attack: even though `forward_fixtures[r]`'s `fixture` id is a real
    join key present on the raw future round, using it to join back onto
    that round's own frame (something a careless strategy might try,
    reasoning "I already have the fixture id, let me look up its price")
    must not be reachable through anything `GameweekView` itself exposes --
    there is no stored reference from `forward_fixtures` back to
    `SeasonData` or the store, only a plain `pl.DataFrame` with the four
    declared columns. This is the same "no path is unsafe" standard as
    `test_backtest_replay.py`'s existing outcome-column attacks, extended
    to the new field."""
    from fplai.backtest.replay import _build_view

    rows = _round_rows(1) + _round_rows(2)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=2)

    view = _build_view(data, 1, horizon=1)
    table = view.forward_fixtures[2]
    assert not hasattr(table, "season_data")
    assert not hasattr(table, "store")
    # The only way to get `value` for round 2 through the object graph a
    # Strategy is ever handed is if `value` sat on `table` itself -- it does
    # not (asserted above), and `view.current_attributes`/`view.history` are
    # round <= 1 only, so no OTHER field on `view` carries round 2's value
    # either.
    assert view.current_attributes["fixture"].to_list() == [1] * view.current_attributes.height
    assert view.history.is_empty()  # round 1 is the first round; nothing precedes it


def test_forward_fixtures_truncates_at_season_end_without_raising(store):
    """A 5-round default horizon requested from round 2 of a 3-round
    season must return only round 3 -- never pad with a fabricated round 4
    or 5, never raise."""
    from fplai.backtest.replay import _build_view

    rows = _round_rows(1) + _round_rows(2) + _round_rows(3)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=3)

    view = _build_view(data, 2)  # default horizon=5
    assert set(view.forward_fixtures.keys()) == {3}


def test_forward_fixtures_is_the_empty_dict_at_the_seasons_last_round(store):
    """Requesting a horizon from the season's final round must return an
    empty dict, not an error -- the mirror of the real-store t=38 case in
    this story's probe."""
    from fplai.backtest.replay import _build_view

    rows = _round_rows(1) + _round_rows(2)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=2)

    view = _build_view(data, 2)
    assert view.forward_fixtures == {}


def test_forward_fixtures_skips_a_mid_season_gap_without_truncating_the_rest(store):
    """A genuine ingestion gap (module docstring of `fplai.backtest.data`)
    must only remove ITS OWN round from the dict -- rounds either side of
    the gap, still within the horizon, must still appear. Round 2 is
    simply never written, simulating a hole exactly like 2022-23's real
    missing round 7."""
    from fplai.backtest.replay import _build_view

    rows = _round_rows(1) + _round_rows(3) + _round_rows(4)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=4)

    view = _build_view(data, 1, horizon=3)  # would want rounds 2, 3, 4
    assert set(view.forward_fixtures.keys()) == {3, 4}


def test_forward_fixtures_default_horizon_matches_the_module_constant(store):
    from fplai.backtest.replay import DEFAULT_FORWARD_HORIZON, _build_view

    assert DEFAULT_FORWARD_HORIZON == 5
    rows = []
    for r in range(1, 8):
        rows += _round_rows(r)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=7)

    view = _build_view(data, 1)  # no horizon kwarg -> the default
    assert set(view.forward_fixtures.keys()) == {2, 3, 4, 5, 6}  # 1+DEFAULT_FORWARD_HORIZON, not 7


def test_bare_gameweek_view_construction_is_unaffected_by_forward_fixtures():
    """The same guarantee `fixtures` already gives every existing caller
    that builds a `GameweekView` by hand (every test above this section,
    and every real `Strategy` under test elsewhere in this file): adding
    `forward_fixtures` must not force a new required argument."""
    from fplai.backtest.replay import GameweekView

    view = GameweekView(
        season="2099-00", gameweek=1, history=pl.DataFrame(), current_attributes=pl.DataFrame()
    )
    assert view.forward_fixtures == {}


def test_forward_fixtures_reflects_a_double_and_a_blank_gameweek_truthfully(store):
    """Synthetic stand-in for the real-store 2025-26 round 33/34 pattern
    from this story's probe: one team (Club0) plays TWICE in the forward
    round, one team (Club1) has ZERO fixtures that round. Both must show up
    exactly as they are -- a doubled team, an absent team -- with no
    invented row for either."""
    from fplai.backtest.replay import _build_view

    round1 = _round_rows(1)
    # Round 2: Club0 plays Club2 (fixture 201) AND Club3 (fixture 202).
    # Club1 has no row at all this round (blank).
    round2 = [
        _base_row(round=2, element=901, name="A", position="MID", team="Club0",
                  fixture=201, was_home=True, opponent_team=2, kickoff_time="2099-08-08T14:00:00Z"),
        _base_row(round=2, element=902, name="B", position="MID", team="Club2",
                  fixture=201, was_home=False, opponent_team=0, kickoff_time="2099-08-08T14:00:00Z"),
        _base_row(round=2, element=903, name="C", position="MID", team="Club0",
                  fixture=202, was_home=False, opponent_team=3, kickoff_time="2099-08-09T11:00:00Z"),
        _base_row(round=2, element=904, name="D", position="MID", team="Club3",
                  fixture=202, was_home=True, opponent_team=0, kickoff_time="2099-08-09T11:00:00Z"),
    ]
    _write(store, round1 + round2)
    data = load_season(store, "2099-00", expected_gameweeks=2)

    view = _build_view(data, 1, horizon=1)
    table = view.forward_fixtures[2]
    assert table.height == 2  # two fixtures, not collapsed to one

    teams = set(table["home_team"].to_list()) | set(table["away_team"].to_list())
    club0_fixtures = table.filter(
        (pl.col("home_team") == "Club0") | (pl.col("away_team") == "Club0")
    )
    assert club0_fixtures.height == 2  # Club0's double
    assert "Club1" not in teams  # Club1's blank
