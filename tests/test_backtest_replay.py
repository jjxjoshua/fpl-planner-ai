"""Tests for fplai.backtest.replay — the leakage boundary, autosubs,
captain/vice-captain doubling, and scoring from stored total_points.

The leakage tests are the point of this module (task brief: "the leakage
rule is the whole point of this task"). They assert, mechanically, that a
Strategy can never see a gameweek's own outcome data — not "the code looks
like it wouldn't leak," but an executable check against the actual object
handed to `decide()`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from fplai.backtest.data import SeasonCoverage, SeasonData, load_season
from fplai.backtest.replay import (
    ATTRIBUTE_COLUMNS,
    Decision,
    GameweekView,
    HeldPlayerAttributes,
    SeasonReplay,
    SquadState,
    _build_view,
    last_known_attributes,
    score_gameweek,
)
from fplai.backtest.rules import SquadRules, transfer_rules_for_season
from fplai.backtest.squad import PlayerCandidate, Squad, SquadError, choose_xi_bench_captain, validate_squad
from fplai.features import FeatureAssemblyError, GameweekPointsPMF, combine_gameweek_points_pmfs
from fplai.points import PointsPMF
from fplai.store import BitemporalStore

UTC = timezone.utc

RULES = SquadRules(
    budget_tenths=1000,
    squad_size=15,
    squad_composition=(("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)),
    xi_bounds=(("GK", (1, 1)), ("DEF", (3, 5)), ("MID", (2, 5)), ("FWD", (1, 3))),
    max_per_club=3,
)

OUTCOME_ONLY_COLUMNS = {
    "total_points",
    "minutes",
    "selected",
    "team_a_score",
    "team_h_score",
}
# `was_home`/`opponent_team` moved OUT of this set this story: they are
# published-schedule facts (fixed weeks before kickoff), not settled by
# playing the fixture, and now belong in ATTRIBUTE_COLUMNS — see
# replay.py's module docstring for the verification. `team_a_score`/
# `team_h_score` (the actual final score) remain here precisely because
# they ARE settled only by playing the fixture, unlike who's playing whom.


# -- leakage: GameweekView structurally excludes outcome data --------------


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


def _small_squad_rows(round_number: int, points_by_element: dict[int, int] | None = None) -> list[dict]:
    """A minimal legal 15-player pool (2 GK / 5 DEF / 5 MID / 3 FWD)
    spread across enough clubs to satisfy the 3-per-club cap, for one
    round."""
    points_by_element = points_by_element or {}
    positions = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    rows = []
    for i, position in enumerate(positions, start=1):
        rows.append(
            _base_row(
                round=round_number,
                element=i,
                name=f"P{i}",
                position=position,
                team=f"Club{i % 6}",
                value=45 + (i % 5) * 5,
                selected=100 * i,
                total_points=points_by_element.get(i, i % 4),
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


class SpyStrategy:
    """Records every GameweekView it is handed, then returns a trivial
    valid Decision built from whatever is legally visible — used to
    inspect exactly what a Strategy could see, never to try to cheat."""

    name = "spy"

    def __init__(self, rules: SquadRules):
        self._rules = rules
        self.views: list[GameweekView] = []

    def decide(self, view: GameweekView) -> Decision:
        from fplai.backtest.baselines import _candidates_from_attributes
        from fplai.backtest.squad import build_squad, choose_xi_bench_captain

        self.views.append(view)
        candidates = _candidates_from_attributes(view.current_attributes, {})
        squad = build_squad(candidates, self._rules)
        xi, bench, captain, vice = choose_xi_bench_captain(squad, self._rules)
        return Decision(squad=squad, xi=xi, bench=bench, captain=captain, vice_captain=vice)


def test_current_attributes_never_carries_outcome_columns(store):
    rows = _small_squad_rows(1) + _small_squad_rows(2) + _small_squad_rows(3)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=3)
    spy = SpyStrategy(RULES)
    SeasonReplay(data, RULES).run(spy)

    for view in spy.views:
        cols = set(view.current_attributes.columns)
        assert cols == set(ATTRIBUTE_COLUMNS), cols
        assert cols.isdisjoint(OUTCOME_ONLY_COLUMNS)


def test_history_never_includes_the_gameweek_being_decided(store):
    rows = _small_squad_rows(1) + _small_squad_rows(2) + _small_squad_rows(3)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=3)
    spy = SpyStrategy(RULES)
    SeasonReplay(data, RULES).run(spy)

    assert len(spy.views) == 3
    for view in spy.views:
        if view.history.is_empty():
            continue
        max_round_seen = view.history["round"].max()
        assert max_round_seen < view.gameweek


def test_decide_is_called_before_scoring(store, monkeypatch):
    """Structural proof, not just an assertion about the resulting data:
    `_build_view` legitimately reads round==t's ATTRIBUTE_COLUMNS (price/
    position/team — pre-deadline facts) to build the view a Strategy sees;
    that is not the leakage boundary. The leakage boundary is scoring —
    `score_gameweek` receives the FULL round==t outcome frame
    (total_points/minutes/...), and this test proves that call happens only
    after `decide()` has already returned for that gameweek, by patching
    `score_gameweek` itself to assert it."""
    rows = _small_squad_rows(1)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=1)

    decided = {"done": False}
    import fplai.backtest.replay as replay_module

    real_score_gameweek = replay_module.score_gameweek

    def guarded_score_gameweek(*args, **kwargs):
        assert decided["done"], "score_gameweek was called before decide() returned"
        return real_score_gameweek(*args, **kwargs)

    class Spy2Strategy:
        name = "spy2"

        def decide(self, view):
            from fplai.backtest.baselines import _candidates_from_attributes
            from fplai.backtest.squad import build_squad, choose_xi_bench_captain

            candidates = _candidates_from_attributes(view.current_attributes, {})
            squad = build_squad(candidates, RULES)
            xi, bench, captain, vice = choose_xi_bench_captain(squad, RULES)
            decided["done"] = True
            return Decision(squad=squad, xi=xi, bench=bench, captain=captain, vice_captain=vice)

    monkeypatch.setattr(replay_module, "score_gameweek", guarded_score_gameweek)
    replay_module.SeasonReplay(data, RULES).run(Spy2Strategy())


# -- schedule facts: fixture/was_home/kickoff_time/opponent_team -----------


def test_outcome_columns_do_not_survive_construction_even_when_present_upstream(store):
    """Attack, not observation, on the outcome boundary: add outcome-shaped
    columns (`bonus`, `bps`) upstream that were never part of
    `ATTRIBUTE_COLUMNS` and confirm the SELECTION mechanism excludes them
    -- not merely their absence from the source data. If `_build_view` ever
    grew a `.select(...)` that silently widened past `ATTRIBUTE_COLUMNS`
    (a stray `**overrides` passthrough, e.g.), this is what would catch it."""
    rows = [dict(r, bonus=7, bps=42) for r in _small_squad_rows(1)]
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=1)
    view = _build_view(data, 1)
    assert set(view.current_attributes.columns) == set(ATTRIBUTE_COLUMNS)
    assert "bonus" not in view.current_attributes.columns
    assert "bps" not in view.current_attributes.columns


def test_schedule_columns_present_and_correct_for_the_gameweek_being_decided(store):
    """The four columns this story adds must actually reach a strategy,
    with the right per-player values, for round == gameweek only."""
    round_1 = _small_squad_rows(1)
    round_2 = _small_squad_rows(2)
    target_element = round_2[5]["element"]
    round_2[5] = dict(round_2[5], was_home=False, opponent_team=17, kickoff_time="2099-08-09T11:30:00Z", fixture=99)
    _write(store, round_1 + round_2)
    data = load_season(store, "2099-00", expected_gameweeks=2)
    view = _build_view(data, 2)

    target = view.current_attributes.filter(pl.col("element") == target_element)
    assert target.height == 1
    row = target.row(0, named=True)
    assert row["was_home"] is False
    assert row["opponent_team"] == 17
    assert row["kickoff_time"] == "2099-08-09T11:30:00Z"
    assert row["fixture"] == 99
    # round 1's own schedule facts must never leak into round 2's view.
    assert set(view.history["round"].unique().to_list()) == {1}


def test_schedule_columns_unchanged_whether_or_not_later_rows_exist(store, tmp_path):
    """The same attack shape as test_features.py's
    `..._unchanged_whether_or_not_later_rows_exist`: build the round-2 view
    twice -- once from a store that only has rounds 1-2, once from a store
    that also has round 3 for the same players -- and show the schedule
    columns for round 2 are byte-for-byte identical either way. If a
    reconstruction ever crept in (joining against a later round's data
    instead of reading round 2's own stored row), this is what would move."""
    round_1_2 = _small_squad_rows(1) + _small_squad_rows(2)
    round_3 = _small_squad_rows(3)

    store_no_future = BitemporalStore(base_path=tmp_path / "no_future")
    _write(store_no_future, round_1_2)
    data_no_future = load_season(store_no_future, "2099-00", expected_gameweeks=2)

    store_with_future = BitemporalStore(base_path=tmp_path / "with_future")
    _write(store_with_future, round_1_2 + round_3)
    data_with_future = load_season(store_with_future, "2099-00", expected_gameweeks=3)

    view_no_future = _build_view(data_no_future, 2)
    view_with_future = _build_view(data_with_future, 2)

    cols = ["element", "fixture", "was_home", "kickoff_time", "opponent_team"]
    assert view_no_future.current_attributes.select(cols).equals(
        view_with_future.current_attributes.select(cols)
    )


def test_double_gameweek_current_attributes_keeps_lowest_fixture_deterministically():
    """A DGW player's two fixture rows this round must collapse to exactly
    one row in `current_attributes` (the `baselines.py` one-row-per-element
    assumption this story deliberately does not change -- see module
    docstring), and WHICH one survives must be deterministic: the lower
    `fixture` id, regardless of the rows' order in the underlying frame.

    Bypasses the store round-trip deliberately (`SeasonData` built
    in-process) so the frame's row order is under this test's direct
    control rather than at the mercy of a read path that may or may not
    happen to preserve write order at this small a scale -- the exact
    non-determinism `_build_view`'s own comment documents as real at
    production scale (verified live, blueprint §7.2 gate-repair session
    s003: five repeated real-store reads of one 692-row gameweek returned
    five different row orders)."""
    base = _small_squad_rows(1)
    dgw_element = base[0]["element"]
    lower_fixture_row = dict(base[0], fixture=1)
    higher_fixture_row = dict(base[0], fixture=501, was_home=not base[0]["was_home"], opponent_team=77)
    # Deliberately construct the frame with the HIGHER fixture id first, so
    # a naive "keep whatever comes first in the frame" dedup would pick the
    # wrong (later) fixture.
    rows = [higher_fixture_row, lower_fixture_row] + base[1:]
    frame = pl.DataFrame(rows)
    coverage = SeasonCoverage(season="2099-00", rounds_present=(1,), expected_gameweeks=1, missing_rounds=())
    data = SeasonData(season="2099-00", frame=frame, coverage=coverage)
    view = _build_view(data, 1)

    target = view.current_attributes.filter(pl.col("element") == dgw_element)
    assert target.height == 1
    assert target.row(0, named=True)["fixture"] == 1


# -- autosubs, captain/vice-captain, double-gameweek scoring ---------------


def _candidate(id_, position, team=None, price=45, value=0.0) -> PlayerCandidate:
    # Spread across enough clubs that a 15-player squad never breaches the
    # 3-per-club cap in validate_squad().
    team = team if team is not None else f"C{id_ % 6}"
    return PlayerCandidate(id=id_, name=f"p{id_}", position=position, team=team, price=price, value=value)


def _decision_from(xi_positions: list[str], bench_positions: list[str]) -> Decision:
    xi = tuple(_candidate(i, pos) for i, pos in enumerate(xi_positions, start=1))
    bench = tuple(_candidate(100 + i, pos) for i, pos in enumerate(bench_positions, start=1))
    squad = Squad(players=xi + bench)
    return Decision(squad=squad, xi=xi, bench=bench, captain=xi[0], vice_captain=xi[1])


def test_autosub_replaces_non_playing_starter_with_eligible_bench():
    decision = _decision_from(
        xi_positions=["GK", "DEF", "DEF", "DEF", "MID", "MID", "MID", "MID", "MID", "FWD", "FWD"],
        bench_positions=["GK", "DEF", "DEF", "FWD"],
    )
    starter_out = decision.xi[1]  # a DEF, id=2
    bench_in = decision.bench[1]  # a DEF, id=102
    points = {p.id: 3 for p in decision.xi}
    minutes = {p.id: 90 for p in decision.xi}
    minutes[starter_out.id] = 0  # did not play
    minutes[bench_in.id] = 90
    points[bench_in.id] = 7

    outcome_rows = pl.DataFrame(
        {
            "element": list(points.keys()),
            "total_points": list(points.values()),
            "minutes": [minutes.get(k, 0) for k in points.keys()],
        }
    )
    result = score_gameweek(decision, outcome_rows, RULES, season="2099-00", gameweek=1)
    assert bench_in.id in result.active_xi_ids
    assert starter_out.id not in result.active_xi_ids
    assert (starter_out.id, bench_in.id) in result.autosubs


def test_captain_fallback_to_vice_when_captain_did_not_play():
    decision = _decision_from(
        xi_positions=["GK", "DEF", "DEF", "DEF", "MID", "MID", "MID", "MID", "MID", "FWD", "FWD"],
        bench_positions=["GK", "DEF", "DEF", "FWD"],
    )
    captain, vice = decision.captain, decision.vice_captain
    points = {p.id: 2 for p in decision.xi}
    minutes = {p.id: 90 for p in decision.xi}
    minutes[captain.id] = 0
    points[captain.id] = 0
    points[vice.id] = 5

    outcome_rows = pl.DataFrame(
        {
            "element": list(points.keys()),
            "total_points": list(points.values()),
            "minutes": [minutes.get(k, 0) for k in points.keys()],
        }
    )
    result = score_gameweek(decision, outcome_rows, RULES, season="2099-00", gameweek=1)
    assert result.effective_captain_id == vice.id
    # base points (captain scored 0, all others 2 except vice=5) + vice doubled once more
    base = sum(points[p.id] for p in decision.xi if p.id in result.active_xi_ids)
    assert result.points == base + points[vice.id]


def test_no_captain_bonus_when_neither_captain_nor_vice_played():
    decision = _decision_from(
        xi_positions=["GK", "DEF", "DEF", "DEF", "MID", "MID", "MID", "MID", "MID", "FWD", "FWD"],
        bench_positions=["GK", "DEF", "DEF", "FWD"],
    )
    captain, vice = decision.captain, decision.vice_captain
    points = {p.id: 2 for p in decision.xi}
    minutes = {p.id: 90 for p in decision.xi}
    minutes[captain.id] = 0
    minutes[vice.id] = 0
    outcome_rows = pl.DataFrame(
        {
            "element": list(points.keys()),
            "total_points": list(points.values()),
            "minutes": [minutes.get(k, 0) for k in points.keys()],
        }
    )
    result = score_gameweek(decision, outcome_rows, RULES, season="2099-00", gameweek=1)
    assert result.effective_captain_id is None


def test_double_gameweek_points_are_summed_not_overwritten():
    decision = _decision_from(
        xi_positions=["GK", "DEF", "DEF", "DEF", "MID", "MID", "MID", "MID", "MID", "FWD", "FWD"],
        bench_positions=["GK", "DEF", "DEF", "FWD"],
    )
    target = decision.xi[-1]
    outcome_rows = pl.DataFrame(
        {
            "element": [p.id for p in decision.xi] + [target.id],
            "total_points": [1] * len(decision.xi) + [9],
            "minutes": [90] * len(decision.xi) + [90],
        }
    )
    result = score_gameweek(decision, outcome_rows, RULES, season="2099-00", gameweek=1)
    # target appears twice: 1 (first fixture) + 9 (second fixture) = 10,
    # doubled if captain, else counted once at 10. Here target is not
    # captain/vice, so just verify the sum landed, not a double-count of a
    # single fixture nor a silent drop of the second row.
    expected_base = sum(1 for _ in decision.xi[:-1]) + 10
    assert result.points >= expected_base  # captain bonus may add more


# -- GameweekView.fixtures + combine_gameweek_points_pmfs (double-gameweek
# handling story) --------------------------------------------------------


def _points_pmf(element: int, fixture: int, position: str, points: list[int], probs: list[float]) -> PointsPMF:
    return PointsPMF(
        element=element,
        fixture=fixture,
        position=position,
        points=tuple(points),
        probabilities=tuple(probs),
        n_simulations=1000,
        seed=0,
        saves_status="NOT_APPLICABLE",
        caveats=(),
    )


def _direct_enumeration_convolution(
    points_a: list[int], probs_a: list[float], points_b: list[int], probs_b: list[float]
) -> dict[int, float]:
    """The independent gate proof: enumerate every (a, b) pair by hand and
    accumulate P(a)*P(b) under a+b, with no shared code path with
    `combine_gameweek_points_pmfs`/`_convolve_dense_pmfs` at all."""
    out: dict[int, float] = {}
    for a, pa in zip(points_a, probs_a):
        for b, pb in zip(points_b, probs_b):
            out[a + b] = out.get(a + b, 0.0) + pa * pb
    return out


def test_convolution_matches_direct_enumeration_on_a_small_hand_built_case():
    pmf1 = _points_pmf(1, 10, "MID", [0, 1, 2], [0.5, 0.3, 0.2])
    pmf2 = _points_pmf(1, 20, "MID", [0, 1, 2, 3], [0.4, 0.0, 0.0, 0.6])

    result = combine_gameweek_points_pmfs([pmf1, pmf2], element=1, position="MID")

    expected = _direct_enumeration_convolution([0, 1, 2], [0.5, 0.3, 0.2], [0, 1, 2, 3], [0.4, 0.0, 0.0, 0.6])
    got = dict(zip(result.points, result.probabilities))
    assert set(got.keys()) == set(expected.keys())
    for total, prob in expected.items():
        assert got[total] == pytest.approx(prob, abs=1e-9)

    assert sum(result.probabilities) == pytest.approx(1.0, abs=1e-9)
    # Support and mass match a direct enumeration; the mean must equal the
    # sum of the two individual means exactly (to float tolerance) -- a
    # property that holds for a sum of two random variables regardless of
    # independence, and therefore must hold here too.
    mean1 = sum(p * w for p, w in zip(pmf1.points, pmf1.probabilities))
    mean2 = sum(p * w for p, w in zip(pmf2.points, pmf2.probabilities))
    assert result.expected_points() == pytest.approx(mean1 + mean2, abs=1e-9)


def test_convolution_support_is_dense_and_starts_at_the_sum_of_minima():
    pmf1 = _points_pmf(1, 10, "FWD", [-1, 0, 1], [0.2, 0.5, 0.3])
    pmf2 = _points_pmf(1, 20, "FWD", [2, 3, 4, 5], [0.1, 0.2, 0.3, 0.4])
    result = combine_gameweek_points_pmfs([pmf1, pmf2], element=1, position="FWD")
    assert result.points[0] == -1 + 2
    assert result.points[-1] == 1 + 5
    assert list(result.points) == list(range(result.points[0], result.points[-1] + 1))


def test_single_gameweek_path_is_unchanged_by_combination():
    """Regression, not just absence of error: a player with exactly one
    fixture must get exactly the same PMF they got before this story --
    the identity, not a near-identical recomputation."""
    pmf = _points_pmf(7, 42, "DEF", [0, 1, 2, 3], [0.1, 0.4, 0.3, 0.2])
    result = combine_gameweek_points_pmfs([pmf], element=7, position="DEF")
    assert result.points == pmf.points
    assert result.probabilities == pmf.probabilities
    assert result.fixture_pmfs == (pmf,)


def test_blank_gameweek_is_a_degenerate_pmf_at_zero_not_an_omitted_candidate():
    result = combine_gameweek_points_pmfs([], element=99, position="MID")
    assert result.points == (0,)
    assert result.probabilities == (1.0,)
    assert result.fixture_pmfs == ()
    assert result.expected_points() == 0.0


def test_combine_rejects_a_fixture_pmf_for_the_wrong_element():
    pmf = _points_pmf(1, 10, "MID", [0, 1], [0.5, 0.5])
    with pytest.raises(FeatureAssemblyError):
        combine_gameweek_points_pmfs([pmf], element=2, position="MID")


def test_combine_rejects_a_fixture_pmf_for_the_wrong_position():
    pmf = _points_pmf(1, 10, "MID", [0, 1], [0.5, 0.5])
    with pytest.raises(FeatureAssemblyError):
        combine_gameweek_points_pmfs([pmf], element=1, position="FWD")


def test_gameweek_points_pmf_input_order_does_not_affect_the_result():
    """CLAUDE.md rule 7 (deterministic, seeded) -- the caller's own list
    order must never matter, only which fixtures a player has."""
    pmf_a = _points_pmf(1, 10, "MID", [0, 1, 2], [0.5, 0.3, 0.2])
    pmf_b = _points_pmf(1, 20, "MID", [0, 1, 2, 3], [0.4, 0.0, 0.0, 0.6])
    forward = combine_gameweek_points_pmfs([pmf_a, pmf_b], element=1, position="MID")
    backward = combine_gameweek_points_pmfs([pmf_b, pmf_a], element=1, position="MID")
    assert forward.points == backward.points
    assert forward.probabilities == backward.probabilities


def test_fixtures_field_empty_when_current_attributes_empty():
    """No fixtures at all this round (an entirely uncovered round) ->
    `fixtures` is an empty frame with the declared schema, never a crash."""
    # Build a SeasonData with a round that has zero rows directly, the same
    # in-process construction `test_double_gameweek_current_attributes_
    # keeps_lowest_fixture_deterministically` uses above.
    frame = pl.DataFrame(_small_squad_rows(1))
    coverage = SeasonCoverage(season="2099-00", rounds_present=(1, 2), expected_gameweeks=2, missing_rounds=())
    season_data = SeasonData(season="2099-00", frame=frame, coverage=coverage)
    view = _build_view(season_data, 2)
    assert view.current_attributes.is_empty()
    assert view.fixtures.is_empty()
    assert set(view.fixtures.columns) == {"fixture", "home_team", "away_team", "kickoff_time"}


def test_fixtures_field_lists_home_and_away_team_for_a_single_fixture(store):
    rows = _small_squad_rows(1)
    _write(store, rows)
    data = load_season(store, "2099-00", expected_gameweeks=1)
    view = _build_view(data, 1)

    assert view.fixtures.height == 1
    row = view.fixtures.row(0, named=True)
    assert row["fixture"] == 1
    # _base_row: was_home=True, team="Testton" (overridden per-row to
    # f"Club{i%6}"), opponent_team=2 -- home_team must be the player's OWN
    # team name, never the numeric opponent_team code.
    assert row["home_team"] == rows[0]["team"]
    assert isinstance(row["away_team"], str) or row["away_team"] is None


@pytest.mark.slow
def test_real_double_gameweek_round_gives_a_dgw_player_two_fixtures_and_a_convolved_view():
    """Gate 2: a real double gameweek from the archive, end to end.
    Season 2022-23, round 19, element 2 (Bernd Leno) has two fixtures
    (64, 188) -- verified live this story via a direct scan of every
    round's per-element row counts, not assumed from memory. Element 457
    (Aaron Cresswell) has exactly one fixture the same round, as a
    same-round contrast."""
    from fplai.backtest.data import load_season as _load_season

    real_store = BitemporalStore()
    data = _load_season(real_store, "2022-23")
    view = _build_view(data, 19)

    # Gate 4: current_attributes stays one row per element across the WHOLE
    # frame, not just for this one DGW element -- baselines.py's
    # one-candidate-per-element assumption must hold for every row.
    assert view.current_attributes["element"].n_unique() == view.current_attributes.height

    dgw_row = view.current_attributes.filter(pl.col("element") == 2)
    assert dgw_row.height == 1  # current_attributes stays one row per element -- untouched
    dgw_team = dgw_row.row(0, named=True)["team"]

    dgw_fixtures = view.fixtures.filter(
        (pl.col("home_team") == dgw_team) | (pl.col("away_team") == dgw_team)
    )
    assert dgw_fixtures.height == 2
    assert set(dgw_fixtures["fixture"].to_list()) == {64, 188}

    single_row = view.current_attributes.filter(pl.col("element") == 457)
    assert single_row.height == 1
    single_team = single_row.row(0, named=True)["team"]
    single_fixtures = view.fixtures.filter(
        (pl.col("home_team") == single_team) | (pl.col("away_team") == single_team)
    )
    assert single_fixtures.height == 1

    # A convolved GameweekPointsPMF actually differs from a single-fixture
    # one -- build two small stand-in PointsPMFs (one per real fixture id)
    # for the DGW player and show the combination widens the support past
    # what either fixture alone carries, while the single-fixture player's
    # combination is the untouched identity.
    dgw_pmf_a = _points_pmf(2, 64, "GK", [0, 1, 2], [0.5, 0.3, 0.2])
    dgw_pmf_b = _points_pmf(2, 188, "GK", [0, 1, 2], [0.5, 0.3, 0.2])
    dgw_combined = combine_gameweek_points_pmfs([dgw_pmf_a, dgw_pmf_b], element=2, position="GK")
    assert dgw_combined.points[-1] == 4  # 2 + 2, only reachable via both fixtures

    single_pmf = _points_pmf(457, list(single_fixtures["fixture"])[0], "DEF", [0, 1, 2], [0.5, 0.3, 0.2])
    single_combined = combine_gameweek_points_pmfs([single_pmf], element=457, position="DEF")
    assert single_combined.points == single_pmf.points
    assert single_combined.probabilities == single_pmf.probabilities


# -- SquadState + stateful threading (story S6) -----------------------------


def _hold_decision(view: GameweekView, element_ids: tuple[int, ...], rules: SquadRules) -> Decision:
    """Buy-and-hold: build a `Decision` for exactly `element_ids`, pricing
    each candidate at THIS gameweek's own `current_attributes` price (the
    price the manager would see today — not the ledger's purchase price,
    which is a separate concern `SquadState` tracks). `value` is a fixed 0.0
    for every candidate; the id-order tiebreak in `choose_xi_bench_captain`
    is deterministic and irrelevant to what these tests check."""
    rows = view.current_attributes.filter(pl.col("element").is_in(list(element_ids)))
    candidates = [
        PlayerCandidate(
            id=int(r["element"]),
            name=r["name"],
            position=r["position"],
            team=r["team"],
            price=int(r["value"]),
            value=0.0,
        )
        for r in rows.iter_rows(named=True)
    ]
    squad = Squad(players=tuple(candidates))
    xi, bench, captain, vice = choose_xi_bench_captain(squad, rules)
    return Decision(squad=squad, xi=xi, bench=bench, captain=captain, vice_captain=vice)


class HoldStrategy:
    """Records every view it is handed. Buy-and-hold the same 15 ids all
    season, ignoring `view.incoming_state` entirely for squad SELECTION
    (that is deliberately not what this story tests -- see D2/D3: no
    Strategy is required to consume `incoming_state` yet). Used purely to
    drive `SeasonReplay.run`'s state-threading machinery with a Decision
    whose squad membership is known and fixed in advance."""

    name = "hold"

    def __init__(self, element_ids: tuple[int, ...], rules: SquadRules):
        self._element_ids = element_ids
        self._rules = rules
        self.views: list[GameweekView] = []

    def decide(self, view: GameweekView) -> Decision:
        self.views.append(view)
        return _hold_decision(view, self._element_ids, self._rules)


def _season_data_2_rounds(season: str, round_1: list[dict], round_2: list[dict]) -> SeasonData:
    frame = pl.DataFrame([dict(r, season=season) for r in round_1 + round_2])
    coverage = SeasonCoverage(season=season, rounds_present=(1, 2), expected_gameweeks=2, missing_rounds=())
    return SeasonData(season=season, frame=frame, coverage=coverage)


def _season_data_n_rounds(season: str, rounds: list[list[dict]]) -> SeasonData:
    all_rows = [row for round_rows in rounds for row in round_rows]
    frame = pl.DataFrame([dict(r, season=season) for r in all_rows])
    rounds_present = tuple(range(1, len(rounds) + 1))
    coverage = SeasonCoverage(
        season=season, rounds_present=rounds_present, expected_gameweeks=len(rounds), missing_rounds=()
    )
    return SeasonData(season=season, frame=frame, coverage=coverage)


def _initial_state_from_rows(rows: list[dict], *, free_transfers: int = 1, bank_tenths: int = 0) -> SquadState:
    prices = tuple(sorted((int(r["element"]), int(r["value"])) for r in rows))
    ids = tuple(sorted(int(r["element"]) for r in rows))
    return SquadState(
        element_ids=ids, purchase_prices=prices, bank_tenths=bank_tenths, free_transfers=free_transfers
    )


def test_stateless_mode_every_view_has_no_incoming_state_and_run_is_unaffected():
    """D1: `initial_state=None` (the default) -- every view's
    `incoming_state` is `None`, and calling `run` with vs. without
    explicitly passing `initial_state=None` is identical."""
    round_1 = _small_squad_rows(1)
    round_2 = _small_squad_rows(2)
    data = _season_data_2_rounds("2023-24", round_1, round_2)
    ids = tuple(sorted(r["element"] for r in round_1))

    spy_default = HoldStrategy(ids, RULES)
    results_default = SeasonReplay(data, RULES).run(spy_default)
    spy_explicit = HoldStrategy(ids, RULES)
    results_explicit = SeasonReplay(data, RULES).run(spy_explicit, initial_state=None)

    assert len(spy_default.views) == 2
    for view in spy_default.views + spy_explicit.views:
        assert view.incoming_state is None
    assert [r.points for r in results_default] == [r.points for r in results_explicit]


def test_stateful_mode_threads_state_gameweek_to_gameweek():
    """Gameweek t+1's `incoming_state` must be exactly what gameweek t's
    `Decision` produced -- not a fresh rebuild, not the caller's original
    `initial_state` unchanged."""
    round_1 = _small_squad_rows(1)
    round_2 = _small_squad_rows(2)
    data = _season_data_2_rounds("2023-24", round_1, round_2)
    ids = tuple(sorted(r["element"] for r in round_1))
    initial_state = _initial_state_from_rows(round_1, free_transfers=1)

    spy = HoldStrategy(ids, RULES)
    SeasonReplay(data, RULES).run(spy, initial_state=initial_state)

    assert len(spy.views) == 2
    assert spy.views[0].incoming_state == initial_state
    gw2_state = spy.views[1].incoming_state
    assert gw2_state is not None
    assert gw2_state != initial_state  # it moved -- free transfers advanced (see below)
    assert gw2_state.element_ids == ids


def test_held_player_purchase_price_carried_forward_not_reset_to_later_price():
    """The one that actually matters for S7: a held player's price rising
    at gameweek 2 must NOT change the ledger's recorded purchase price for
    that player once it is threaded into gameweek 3's incoming state.

    Three rounds, deliberately -- the price rise happens IN round 2's own
    data, so it is round 2's `Decision` (built from round 2's raised-price
    `current_attributes`) whose squad `PlayerCandidate.price` would already
    be wrong if the carry-forward logic ever used it directly instead of
    consulting the INCOMING state's own record. Checking round 2's own
    incoming state (threaded only from round 1, which never saw the raise)
    cannot distinguish correct from broken -- see this story's punch-card
    finding, this exact mistake was made and caught by running the test
    against a deliberately broken implementation first."""
    round_1 = _small_squad_rows(1)
    round_2 = _small_squad_rows(2)
    round_3 = _small_squad_rows(3)
    raised_element = round_1[0]["element"]
    original_price = round_1[0]["value"]
    round_2 = [dict(r, value=r["value"] + 5) if r["element"] == raised_element else r for r in round_2]
    data = _season_data_n_rounds("2023-24", [round_1, round_2, round_3])
    ids = tuple(sorted(r["element"] for r in round_1))
    initial_state = _initial_state_from_rows(round_1, free_transfers=1)

    spy = HoldStrategy(ids, RULES)
    SeasonReplay(data, RULES).run(spy, initial_state=initial_state)

    assert len(spy.views) == 3
    # the raised price genuinely reached round 2's current_attributes --
    # otherwise this test would pass vacuously because nothing changed.
    raised_row_gw2 = spy.views[1].current_attributes.filter(pl.col("element") == raised_element)
    assert raised_row_gw2.row(0, named=True)["value"] == original_price + 5

    gw3_state = spy.views[2].incoming_state  # threaded through round 2's (raised-price) decision
    assert gw3_state is not None
    assert gw3_state.purchase_prices_dict[raised_element] == original_price


def test_free_transfers_accumulate_and_cap_at_max_banked_transfers():
    rules_2023 = transfer_rules_for_season("2023-24")  # free=1/gw, max_banked=2
    round_1 = _small_squad_rows(1)
    round_2 = _small_squad_rows(2)
    data = _season_data_2_rounds("2023-24", round_1, round_2)
    ids = tuple(sorted(r["element"] for r in round_1))

    # Start already at the cap -- advancing must NOT exceed it.
    initial_state = _initial_state_from_rows(round_1, free_transfers=rules_2023.max_banked_transfers)
    spy = HoldStrategy(ids, RULES)
    SeasonReplay(data, RULES).run(spy, initial_state=initial_state)
    gw2_state = spy.views[1].incoming_state
    assert gw2_state.free_transfers == rules_2023.max_banked_transfers

    # Start below the cap -- advancing must add exactly free_transfers_per_gameweek.
    initial_state_low = _initial_state_from_rows(round_1, free_transfers=0)
    spy2 = HoldStrategy(ids, RULES)
    SeasonReplay(data, RULES).run(spy2, initial_state=initial_state_low)
    gw2_state_low = spy2.views[1].incoming_state
    assert gw2_state_low.free_transfers == rules_2023.free_transfers_per_gameweek


def test_2025_26_gw16_override_tops_up_to_five_not_adds_five():
    """docs/wiki/transfer-rules.md: the AFCON top-up set the bank TO five,
    it did not ADD five to whatever was already banked."""
    rules_2025 = transfer_rules_for_season("2025-26")
    assert rules_2025.free_transfer_overrides_dict[16] == 5

    round_15 = [dict(r, round=15) for r in _small_squad_rows(1)]
    round_16 = [dict(r, round=16) for r in _small_squad_rows(2)]
    frame = pl.DataFrame([dict(r, season="2025-26") for r in round_15 + round_16])
    coverage = SeasonCoverage(season="2025-26", rounds_present=(15, 16), expected_gameweeks=38, missing_rounds=())
    data = SeasonData(season="2025-26", frame=frame, coverage=coverage)
    ids = tuple(sorted(r["element"] for r in round_15))

    # Already-banked transfers (2) plus the normal +1/gw advance would give 3
    # -- well short of 5. The override must still land on exactly 5, proving
    # it is a top-up-TO, not a max()-with-the-normal-advance coincidence,
    # and not a +5 addition (which would give 7).
    initial_state = _initial_state_from_rows(round_15, free_transfers=2)
    spy = HoldStrategy(ids, RULES)
    SeasonReplay(data, RULES).run(spy, initial_state=initial_state)
    gw16_state = spy.views[1].incoming_state
    assert gw16_state is not None
    assert gw16_state.free_transfers == 5


def test_stateless_mode_still_works_for_a_season_with_no_sourced_transfer_rules():
    """D5: a 2021-22-style season has no entry in `SEASON_TRANSFER_RULES` --
    `transfer_rules_for_season` raises for it. Stateless mode must never
    call that function at all, so running stateless for such a season must
    succeed exactly as for any sourced season."""
    with pytest.raises(KeyError):
        transfer_rules_for_season("2021-22")  # confirms the season really is unsourced

    round_1 = _small_squad_rows(1)
    round_2 = _small_squad_rows(2)
    data = _season_data_2_rounds("2021-22", round_1, round_2)
    ids = tuple(sorted(r["element"] for r in round_1))

    spy = HoldStrategy(ids, RULES)
    results = SeasonReplay(data, RULES).run(spy)  # initial_state=None, stateless

    assert len(results) == 2
    assert all(view.incoming_state is None for view in spy.views)

    # Attack: passing a real SquadState (stateful mode) for this same
    # unsourced season must raise -- not silently degrade to stateless.
    initial_state = _initial_state_from_rows(round_1)
    spy2 = HoldStrategy(ids, RULES)
    with pytest.raises(KeyError):
        SeasonReplay(data, RULES).run(spy2, initial_state=initial_state)


# -- transfer execution, selling-price rules, hit accounting (story S7) ----


def _s7_row(element: int, position: str, team: str, value: int, round_number: int, points: int = 2) -> dict:
    return _base_row(
        round=round_number,
        element=element,
        name=f"P{element}",
        position=position,
        team=team,
        value=value,
        total_points=points,
        minutes=90,
    )


def _decision_for_squad(
    view: GameweekView,
    squad_ids: tuple[int, ...],
    rules: SquadRules,
    *,
    transfers_in: tuple[int, ...] = (),
    transfers_out: tuple[int, ...] = (),
) -> Decision:
    """Like `_hold_decision` above, but the target squad is given
    explicitly (rather than always equal to the ids passed at construction
    time) and `transfers_in`/`transfers_out` are threaded onto the
    `Decision` -- what a real transfer-making `Strategy` would build."""
    rows = view.current_attributes.filter(pl.col("element").is_in(list(squad_ids)))
    candidates = [
        PlayerCandidate(
            id=int(r["element"]),
            name=r["name"],
            position=r["position"],
            team=r["team"],
            price=int(r["value"]),
            value=0.0,
        )
        for r in rows.iter_rows(named=True)
    ]
    squad = Squad(players=tuple(candidates))
    xi, bench, captain, vice = choose_xi_bench_captain(squad, rules)
    return Decision(
        squad=squad,
        xi=xi,
        bench=bench,
        captain=captain,
        vice_captain=vice,
        transfers_in=transfers_in,
        transfers_out=transfers_out,
    )


class ScriptedTransferStrategy:
    """Plays back a fixed, hand-authored plan: one `(squad_ids,
    transfers_in, transfers_out)` triple per gameweek, in order. Used only
    by this story's hand-computed-arithmetic test, where every squad,
    transfer and price is a literal chosen so the ledger can be checked by
    hand, not to exercise any football judgement."""

    name = "scripted"

    def __init__(
        self,
        plan: list[tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]],
        rules: SquadRules,
    ):
        self._plan = plan
        self._rules = rules
        self.views: list[GameweekView] = []

    def decide(self, view: GameweekView) -> Decision:
        self.views.append(view)
        squad_ids, transfers_in, transfers_out = self._plan[len(self.views) - 1]
        return _decision_for_squad(
            view, squad_ids, self._rules, transfers_in=transfers_in, transfers_out=transfers_out
        )


def test_hand_computed_multiweek_transfer_scenario():
    """The story's own gate: bank after a transfer, sell price on a risen
    player, sell price on a dropped player, FT count across weeks, and a
    points total that includes a hit -- all worked out BY HAND below and
    checked against the real replay's output, not the other way around.

    Squad: 15 players, ids 1-15 (2 GK / 5 DEF / 5 MID / 3 FWD), every
    held player's purchase price is a flat 50 (5.0m) so only the four
    transferred players' prices need tracking by hand. `2023-24`
    TransferRules: 1 free transfer/gameweek, capped at 2 banked, hit_cost
    -4 (transfer_rules_for_season("2023-24")).

    GW1 (initial free_transfers=0): sell id 5 (purchase 50, current-price
    row value 53 -- RISEN) and id 6 (purchase 50, current-price row value
    47 -- DROPPED), buy id 16 (price 52) and id 17 (price 45).
      sell(5)  = 50 + (53-50)//2 = 50 + 1 = 51   (D2's own worked example)
      sell(6)  = 47                              (drop borne in full)
      bank     = 20 (initial) + 51 + 47 - 52 - 45 = 21
      n_transfers=2, free_transfers=0 -> paid=2 -> hit = -4*2 = -8
      remaining FT = max(0, 0-2) = 0 -> advanced = min(0+1, 2) = 1
      base points (15 players x 2 pts, 11 in the XI + captain double,
      everyone plays 90) = 11*2 + 2 = 24 -> scored points = 24 - 8 = 16

    GW2: hold (no transfer). bank unchanged (21); FT: remaining=max(0,1-0)=1
      -> advanced=min(1+1,2)=2. points = 24 (no hit).

    GW3: sell id 17 (purchase 45, current-price row value 40 -- DROPPED
      again), buy id 19 (price 44).
      sell(17) = 40 (drop borne in full)
      bank     = 21 (carried from GW2) + 40 - 44 = 17
      n_transfers=1, free_transfers=2 -> paid=max(0,1-2)=0 -> hit=0
      remaining FT = max(0, 2-1) = 1 -> advanced = min(1+1,2) = 2
      points = 24 (no hit).

    GW4: hold (no transfer) -- exists purely so GW4's OWN `incoming_state`
      (spy.views[3].incoming_state) exposes the ledger exactly as GW3 left
      it, without needing a 5th round. points = 24 (no hit).

    Total across the 4 gameweeks: 16 + 24 + 24 + 24 = 88 (96 base, -8 hit).
    """
    transfer_rules = transfer_rules_for_season("2023-24")
    assert transfer_rules.free_transfers_per_gameweek == 1
    assert transfer_rules.max_banked_transfers == 2
    assert transfer_rules.hit_cost == -4

    held_positions = {
        1: ("GK", "Club1"), 2: ("GK", "Club2"),
        3: ("DEF", "Club3"), 4: ("DEF", "Club4"), 5: ("DEF", "Club5"),
        6: ("DEF", "Club0"), 7: ("DEF", "Club1"),
        8: ("MID", "Club2"), 9: ("MID", "Club3"), 10: ("MID", "Club4"),
        11: ("MID", "Club5"), 12: ("MID", "Club0"),
        13: ("FWD", "Club1"), 14: ("FWD", "Club2"), 15: ("FWD", "Club3"),
    }
    always_held = [1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13, 14, 15]  # never transferred

    round_1 = (
        [_s7_row(i, *held_positions[i], 50, 1) for i in always_held]
        + [_s7_row(5, "DEF", "Club5", 53, 1)]  # riser, sold this gw
        + [_s7_row(6, "DEF", "Club0", 47, 1)]  # dropper, sold this gw
        + [_s7_row(16, "DEF", "Club5", 52, 1)]  # bought this gw
        + [_s7_row(17, "DEF", "Club0", 45, 1)]  # bought this gw
    )
    round_2 = [_s7_row(i, *held_positions[i], 50, 2) for i in always_held] + [
        _s7_row(16, "DEF", "Club5", 52, 2),
        _s7_row(17, "DEF", "Club0", 45, 2),
    ]
    round_3 = (
        [_s7_row(i, *held_positions[i], 50, 3) for i in always_held]
        + [_s7_row(16, "DEF", "Club5", 52, 3)]
        + [_s7_row(17, "DEF", "Club0", 40, 3)]  # dropped again, sold this gw
        + [_s7_row(19, "DEF", "Club0", 44, 3)]  # bought this gw
    )
    round_4 = [_s7_row(i, *held_positions[i], 50, 4) for i in always_held] + [
        _s7_row(16, "DEF", "Club5", 52, 4),
        _s7_row(19, "DEF", "Club0", 44, 4),
    ]

    data = _season_data_n_rounds("2023-24", [round_1, round_2, round_3, round_4])

    initial_state = SquadState(
        element_ids=tuple(sorted(always_held + [5, 6])),
        purchase_prices=tuple(sorted((i, 50) for i in always_held + [5, 6])),
        bank_tenths=20,
        free_transfers=0,
    )

    gw1_squad = tuple(sorted(always_held + [16, 17]))
    gw2_squad = gw1_squad
    gw3_squad = tuple(sorted(always_held + [16, 19]))
    gw4_squad = gw3_squad

    plan = [
        (gw1_squad, (16, 17), (5, 6)),
        (gw2_squad, (), ()),
        (gw3_squad, (19,), (17,)),
        (gw4_squad, (), ()),
    ]
    strategy = ScriptedTransferStrategy(plan, RULES)
    results = SeasonReplay(data, RULES).run(strategy, initial_state=initial_state)

    assert len(results) == 4
    assert [r.points for r in results] == [16, 24, 24, 24]
    assert [r.transfer_hit_points for r in results] == [-8, 0, 0, 0]
    assert sum(r.points for r in results) == 88

    gw2_state = strategy.views[1].incoming_state
    assert gw2_state.bank_tenths == 21
    assert gw2_state.free_transfers == 1
    assert gw2_state.purchase_prices_dict[16] == 52
    assert gw2_state.purchase_prices_dict[17] == 45

    gw3_state = strategy.views[2].incoming_state
    assert gw3_state.bank_tenths == 21  # GW2 held -- bank untouched
    assert gw3_state.free_transfers == 2

    gw4_state = strategy.views[3].incoming_state
    assert gw4_state.bank_tenths == 17
    assert gw4_state.free_transfers == 2
    assert gw4_state.element_ids == gw3_squad
    assert gw4_state.purchase_prices_dict[19] == 44
    assert 17 not in gw4_state.purchase_prices_dict


def test_hit_makes_the_total_lower_not_higher():
    """D4's own sign attack: `TransferRules.hit_cost` is -4, NEGATIVE. This
    proves `score_gameweek` ADDS an already-negative `hit_points`, so a
    scenario with a hit scores strictly LOWER than the identical scenario
    scored with `hit_points=0` -- if the sign were ever inverted (adding
    `-hit_cost` or subtracting a negative), this assertion flips and fails."""
    decision = _decision_from(
        xi_positions=["GK", "DEF", "DEF", "DEF", "MID", "MID", "MID", "MID", "MID", "FWD", "FWD"],
        bench_positions=["GK", "DEF", "DEF", "FWD"],
    )
    points = {p.id: 3 for p in decision.xi}
    minutes = {p.id: 90 for p in decision.xi}
    outcome_rows = pl.DataFrame(
        {
            "element": list(points.keys()),
            "total_points": list(points.values()),
            "minutes": [minutes[k] for k in points.keys()],
        }
    )
    transfer_rules = transfer_rules_for_season("2023-24")
    paid = 2
    hit_points = transfer_rules.hit_cost * paid  # -4 * 2 = -8
    assert hit_points == -8

    without_hit = score_gameweek(decision, outcome_rows, RULES, season="2099-00", gameweek=1, hit_points=0)
    with_hit = score_gameweek(decision, outcome_rows, RULES, season="2099-00", gameweek=1, hit_points=hit_points)

    assert with_hit.points < without_hit.points
    assert with_hit.points == without_hit.points + hit_points
    assert with_hit.transfer_hit_points == -8


def test_transferring_an_unowned_player_raises_naming_the_id():
    """D6's attack surface: a `Decision` claiming `transfers_out` for a
    player the incoming ledger does not actually hold must raise, and the
    message must name the offending id -- proven by actually driving it
    through `SeasonReplay.run`, not by unit-testing a helper in isolation."""
    always_held = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    held_positions = {
        1: "GK", 2: "GK", 3: "DEF", 4: "DEF", 5: "DEF", 6: "DEF", 7: "DEF",
        8: "MID", 9: "MID", 10: "MID", 11: "MID", 12: "MID",
        13: "FWD", 14: "FWD", 15: "FWD",
    }
    round_1 = [
        _s7_row(i, held_positions[i], f"Club{i % 6}", 50, 1) for i in always_held
    ] + [_s7_row(999, "DEF", "ClubX", 50, 1)]  # exists in the store, never owned
    data = _season_data_n_rounds("2023-24", [round_1])
    initial_state = SquadState(
        element_ids=tuple(sorted(always_held)),
        purchase_prices=tuple(sorted((i, 50) for i in always_held)),
        bank_tenths=0,
        free_transfers=1,
    )

    class CheatingStrategy:
        name = "cheat"

        def decide(self, view: GameweekView) -> Decision:
            # Claims to sell id 999, which the incoming ledger never held.
            squad_ids = tuple(sorted(set(always_held) - {5} | {999}))
            return _decision_for_squad(
                view, squad_ids, RULES, transfers_in=(999,), transfers_out=(999,)
            )

    with pytest.raises(ValueError, match=r"999"):
        SeasonReplay(data, RULES).run(CheatingStrategy(), initial_state=initial_state)


def test_unaffordable_transfer_raises_rather_than_going_negative():
    """D3: a transfer whose incoming price exceeds bank plus sale proceeds
    must raise, never silently clamp the bank to zero -- that would let a
    strategy buy a squad it cannot pay for."""
    always_held = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    held_positions = {
        1: "GK", 2: "GK", 3: "DEF", 4: "DEF", 5: "DEF", 6: "DEF", 7: "DEF",
        8: "MID", 9: "MID", 10: "MID", 11: "MID", 12: "MID",
        13: "FWD", 14: "FWD", 15: "FWD",
    }
    round_1 = [_s7_row(i, held_positions[i], f"Club{i % 6}", 50, 1) for i in always_held if i != 5] + [
        _s7_row(5, "DEF", "Club5", 50, 1),  # sold at exactly purchase price -> sell 50
        # 200 keeps the SQUAD's total price (700 + 200 = 900) safely under the
        # 1000 budget -- validate_squad must pass so the failure this test is
        # after (an unaffordable TRANSFER, D3) is not masked by a different,
        # unrelated squad-cost failure (as an earlier draft of this test hit
        # with an even more expensive buy).
        _s7_row(16, "DEF", "Club5", 200, 1),
    ]
    data = _season_data_n_rounds("2023-24", [round_1])
    initial_state = SquadState(
        element_ids=tuple(sorted(always_held)),
        purchase_prices=tuple(sorted((i, 50) for i in always_held)),
        bank_tenths=0,  # no cash buffer at all
        free_transfers=1,
    )

    class OverspendStrategy:
        name = "overspend"

        def decide(self, view: GameweekView) -> Decision:
            squad_ids = tuple(sorted(set(always_held) - {5} | {16}))
            return _decision_for_squad(view, squad_ids, RULES, transfers_in=(16,), transfers_out=(5,))

    with pytest.raises(ValueError, match=r"unaffordable"):
        SeasonReplay(data, RULES).run(OverspendStrategy(), initial_state=initial_state)


# -- S9, PHASE 2 -- held-player backfill (D5), sell-price fallback (D6), --
# -- stateless-only budget check (D7) --------------------------------------


def test_last_known_attributes_returns_the_most_recent_pre_round_row():
    """D5: sorted by [element, round, fixture], the LAST row per element
    wins -- round 8's row, not round 5's."""
    history = pl.DataFrame(
        {
            "element": [7, 7, 7],
            "round": [5, 8, 8],
            "fixture": [50, 80, 81],
            "name": ["old", "mid", "new"],
            "position": ["MID", "MID", "MID"],
            "team": ["Old FC", "Mid FC", "New FC"],
            "value": [40, 45, 48],
        }
    )
    out = last_known_attributes(history, [7])
    assert out[7] == HeldPlayerAttributes(name="new", position="MID", team="New FC", price_tenths=48)


def test_last_known_attributes_omits_an_element_with_no_history():
    """D5: never fabricated -- an id with zero rows anywhere in `history`
    is simply absent from the returned dict."""
    history = pl.DataFrame(
        {"element": [7], "round": [5], "fixture": [50], "name": ["a"], "position": ["MID"], "team": ["T"], "value": [40]}
    )
    out = last_known_attributes(history, [7, 999])
    assert set(out) == {7}
    assert 999 not in out


def test_last_known_attributes_empty_history_returns_empty_dict():
    out = last_known_attributes(pl.DataFrame(), [1, 2])
    assert out == {}


def test_d6_sell_price_fallback_used_when_a_blanked_outgoing_player_has_no_current_row():
    """D6 (S9, PHASE 2): a transferred-out player whose club blanks this
    round has no row in `current_attributes` at all -- `SeasonReplay.run`
    must fall back to `last_known_attributes` rather than raising, and the
    sell price used must be exactly `_sell_price(purchase_price, that
    fallback price)` -- not the purchase price, not zero, not the sell
    price of some other round."""
    held_positions = {
        1: "GK", 2: "GK", 3: "DEF", 4: "DEF", 5: "DEF", 6: "DEF", 7: "DEF",
        8: "MID", 9: "MID", 10: "MID", 11: "MID", 12: "MID",
        13: "FWD", 14: "FWD", 15: "FWD",
    }
    always_held = list(held_positions)
    round_1 = [_s7_row(i, held_positions[i], f"Club{i % 6}", 50, 1) for i in always_held]
    # round 2: element 5's club blanks -- its row is simply absent, unlike
    # every other held player's.
    round_2 = [_s7_row(i, held_positions[i], f"Club{i % 6}", 50, 2) for i in always_held if i != 5] + [
        _s7_row(16, "DEF", "Club5", 50, 2)
    ]
    new_squad_ids = tuple(sorted(set(always_held) - {5} | {16}))
    round_3 = [
        _s7_row(i, held_positions.get(i, "DEF"), f"Club{i % 6}" if i != 16 else "Club5", 50, 3)
        for i in new_squad_ids
    ]
    data = _season_data_n_rounds("2023-24", [round_1, round_2, round_3])

    initial_state = SquadState(
        element_ids=tuple(sorted(always_held)),
        purchase_prices=tuple((i, 40) if i == 5 else (i, 50) for i in sorted(always_held)),  # element 5 bought at 40
        bank_tenths=1000,
        free_transfers=1,
    )
    plan = [
        (tuple(sorted(always_held)), (), ()),  # round 1: hold
        (new_squad_ids, (16,), (5,)),  # round 2: sell blanked element 5, buy 16
        (new_squad_ids, (), ()),  # round 3: hold the new squad
    ]
    strategy = ScriptedTransferStrategy(plan, RULES)
    SeasonReplay(data, RULES).run(strategy, initial_state=initial_state)

    gw3_state = strategy.views[2].incoming_state
    assert gw3_state is not None
    # purchase price 40, LAST-KNOWN price (round 1's own row, since round 2
    # has none) 50 -> profit 10, half 5 -> sell 45. Buy element 16 at 50.
    assert gw3_state.bank_tenths == 1000 + 45 - 50


def test_d6_sell_price_fallback_raises_when_neither_current_row_nor_history_exists():
    """D6's still-raises case: an id absent from BOTH this round's
    `current_attributes` and every row in `history` must still raise,
    naming the id -- never fabricated."""
    held_positions = {
        1: "GK", 2: "GK", 3: "DEF", 4: "DEF", 6: "DEF", 7: "DEF",
        8: "MID", 9: "MID", 10: "MID", 11: "MID", 12: "MID",
        13: "FWD", 14: "FWD", 15: "FWD",
    }
    phantom = 999
    always_held = list(held_positions) + [phantom]
    new_squad_ids = tuple(sorted(set(held_positions) | {16}))
    round_1 = [_s7_row(i, held_positions[i], f"Club{i % 6}", 50, 1) for i in held_positions] + [
        _s7_row(16, "DEF", "Club5", 50, 1)
    ]
    data = _season_data_n_rounds("2023-24", [round_1])
    initial_state = SquadState(
        element_ids=tuple(sorted(always_held)),
        purchase_prices=tuple(sorted((i, 50) for i in always_held)),
        bank_tenths=1000,
        free_transfers=1,
    )

    class SellPhantomStrategy:
        name = "sell-phantom"

        def decide(self, view: GameweekView) -> Decision:
            return _decision_for_squad(view, new_squad_ids, RULES, transfers_in=(16,), transfers_out=(phantom,))

    with pytest.raises(ValueError, match=rf"{phantom}"):
        SeasonReplay(data, RULES).run(SellPhantomStrategy(), initial_state=initial_state)


def test_validate_squad_check_budget_false_skips_only_the_budget_check():
    """D7: `check_budget=False` skips ONLY the flat budget check -- size,
    duplicate-id, composition and club-cap checks all still fire."""
    positions = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3

    def candidates(price: int) -> list[PlayerCandidate]:
        return [
            PlayerCandidate(id=i, name=f"P{i}", position=pos, team=f"Club{i % 6}", price=price, value=0.0)
            for i, pos in enumerate(positions, start=1)
        ]

    over_budget = Squad(players=tuple(candidates(price=100)))  # 15 * 100 = 1500, well over RULES' 1000
    with pytest.raises(SquadError, match="over budget"):
        validate_squad(over_budget, RULES)
    validate_squad(over_budget, RULES, check_budget=False)  # must NOT raise

    wrong_size = Squad(players=tuple(candidates(price=40)[:14]))
    with pytest.raises(SquadError, match="need 15"):
        validate_squad(wrong_size, RULES, check_budget=False)

    dup = list(candidates(price=40))
    dup[1] = PlayerCandidate(id=dup[0].id, name="dup", position=dup[0].position, team=dup[0].team, price=40, value=0.0)
    with pytest.raises(SquadError, match="duplicate"):
        validate_squad(Squad(players=tuple(dup)), RULES, check_budget=False)

    wrong_comp = list(candidates(price=40))
    wrong_comp[0] = PlayerCandidate(id=wrong_comp[0].id, name="x", position="DEF", team=wrong_comp[0].team, price=40, value=0.0)
    with pytest.raises(SquadError, match="needs exactly"):
        validate_squad(Squad(players=tuple(wrong_comp)), RULES, check_budget=False)

    over_club_cap = [
        PlayerCandidate(id=i, name=f"P{i}", position=pos, team="SameClub", price=40, value=0.0)
        for i, pos in enumerate(positions, start=1)
    ]
    with pytest.raises(SquadError, match="club cap"):
        validate_squad(Squad(players=tuple(over_club_cap)), RULES, check_budget=False)


def test_d7_stateful_mode_tolerates_a_held_squad_appreciated_past_budget_but_stateless_still_catches_it():
    """D7: the flat budget check is a STATELESS-mode invariant. A HELD
    squad (no transfers at all) that appreciates past `rules.
    budget_tenths` purely from price drift must not fail a STATEFUL
    replay -- the identical squad/price data, run STATELESS, still raises,
    proving the check really did (and still does) bind for the mode it is
    meant for."""
    positions = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    prices_gw1 = [50, 50, 60, 60, 60, 60, 60, 80, 80, 80, 80, 80, 66, 67, 67]
    assert sum(prices_gw1) == RULES.budget_tenths  # exactly at the cap -- both modes pass GW1

    round_1 = [
        _s7_row(i, pos, f"Club{i % 6}", price, 1) for i, (pos, price) in enumerate(zip(positions, prices_gw1), start=1)
    ]
    # GW2: every price rises by 10 (real drift, no transfer) -- squad value
    # becomes 1150, 150 over the 1000 budget.
    round_2 = [
        _s7_row(i, pos, f"Club{i % 6}", price + 10, 2)
        for i, (pos, price) in enumerate(zip(positions, prices_gw1), start=1)
    ]
    data = _season_data_n_rounds("2023-24", [round_1, round_2])
    ids = tuple(range(1, 16))
    initial_state = _initial_state_from_rows(round_1, free_transfers=1)

    stateful_results = SeasonReplay(data, RULES).run(HoldStrategy(ids, RULES), initial_state=initial_state)
    assert len(stateful_results) == 2  # did not raise

    with pytest.raises(SquadError, match="over budget"):
        SeasonReplay(data, RULES).run(HoldStrategy(ids, RULES))  # initial_state=None -- stateless, D7 does not apply


# --- ATTACK (CLAUDE.md "no path is unsafe" standard) -----------------------
# D7 removed validate_squad's flat budget check from stateful mode. Attack
# the claim that overspending is still impossible there: a squad whose
# TOTAL price stays comfortably under rules.budget_tenths (so even the OLD
# check would never have caught this) is nonetheless unaffordable per the
# bank LEDGER -- proving the two checks were never the same guarantee, and
# that removing one leaves the other (a wholly independent mechanism)
# still standing.


def test_attack_d7_removing_the_flat_budget_check_does_not_open_an_overspend_hole():
    """The squad's own total price (560 + 100 = 660) is nowhere near
    `RULES.budget_tenths` (1000) -- `validate_squad`'s flat check, even if
    it still ran in stateful mode, would never have caught this transfer.
    It is unaffordable anyway, because the incoming bank (50) plus the
    sale proceeds (element 5 sells at exactly 40, no rise) cannot cover a
    100-cost buy. This must still raise, and by `_advance_state`'s own
    negative-bank check (D3) -- not by `validate_squad`, which D7 has
    already taken out of this mode entirely."""
    held_positions = {
        1: "GK", 2: "GK", 3: "DEF", 4: "DEF", 5: "DEF", 6: "DEF", 7: "DEF",
        8: "MID", 9: "MID", 10: "MID", 11: "MID", 12: "MID",
        13: "FWD", 14: "FWD", 15: "FWD",
    }
    always_held = list(held_positions)
    round_1 = [_s7_row(i, held_positions[i], f"Club{i % 6}", 40, 1) for i in always_held if i != 5] + [
        _s7_row(5, "DEF", "Club5", 40, 1),
        _s7_row(16, "DEF", "Club5", 100, 1),
    ]
    data = _season_data_n_rounds("2023-24", [round_1])
    initial_state = SquadState(
        element_ids=tuple(sorted(always_held)),
        purchase_prices=tuple(sorted((i, 40) for i in always_held)),
        bank_tenths=50,
        free_transfers=1,
    )

    class OverspendStrategy:
        name = "overspend-d7-attack"

        def decide(self, view: GameweekView) -> Decision:
            squad_ids = tuple(sorted(set(always_held) - {5} | {16}))
            return _decision_for_squad(view, squad_ids, RULES, transfers_in=(16,), transfers_out=(5,))

    with pytest.raises(ValueError, match=r"unaffordable"):
        SeasonReplay(data, RULES).run(OverspendStrategy(), initial_state=initial_state)
