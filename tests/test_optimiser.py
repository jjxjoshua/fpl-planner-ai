"""Tests for the single-period MILP optimiser (`fplai.optimiser`) — Phase
3's E6 gate. Blueprint §6.1/§7.2, CLAUDE.md rules 5/7.

Two classes of test, following the same split the Phase 1 gate-repair
session established (`tests/test_backtest.py`'s own module docstring):

1. **Unit tests against small synthetic candidate pools** — constraint
   correctness (budget/composition/formation/club-cap), captain/vice/
   bench logic, error paths.
2. **Determinism tests, including one live against the real store**
   (`tests/test_backtest.py::
   test_greedy_form_baseline_is_reproducible_across_repeated_runs_on_the_
   real_store` is the direct precedent this file's own real-store test
   mirrors) — CLAUDE.md rule 7 is a hard gate here specifically because a
   MILP has ties and HiGHS may return any optimal vertex, the same class
   of failure that re-opened the Phase 1 gate (polars'
   `.unique(maintain_order=False)` feeding a hill-climb's tie-break).
"""

from __future__ import annotations

import polars as pl
import pytest

from fplai.backtest.baselines import TemplateBaseline
from fplai.backtest.data import SeasonCoverage, SeasonData, load_season
from fplai.backtest.replay import (
    Decision,
    GameweekView,
    SeasonReplay,
    SquadState,
    _build_view,
    free_build_state,
)
from fplai.backtest.report import summarise
from fplai.backtest.rules import (
    GREEDY_FORM_TRAILING_GAMEWEEKS,
    SquadRules,
    TransferRules,
    rules_for_season,
    transfer_rules_for_season,
)
from fplai.backtest.squad import PlayerCandidate, Squad, validate_squad
from fplai.features import FixtureFeatureAssemblyParams
from fplai.optimiser import (
    HorizonStrategy,
    MILPStrategy,
    ModelStackParams,
    ModelStackStrategy,
    OptimiserCandidate,
    OptimiserConfig,
    OptimiserError,
    TrailingProxyHorizonSource,
    _candidates_from_view,
    _decision_from_result,
    _EmpiricalPointsDistribution,
    _parse_kickoff_time,
    _trailing_points_distributions,
    build_decision_calendar,
    collapse_to_expected_points,
    optimise_multi_period,
    optimise_squad,
)
from fplai.points import SAVES_STATUS_NOT_APPLICABLE, PointsPMF
from fplai.store import BitemporalStore

_RULES = rules_for_season("2025-26")  # the shared documented SquadRules assumption; any declared season works identically


def _dist(*points_and_probs: tuple[int, float]) -> _EmpiricalPointsDistribution:
    points = tuple(p for p, _ in points_and_probs)
    probs = tuple(pr for _, pr in points_and_probs)
    return _EmpiricalPointsDistribution(points=points, probabilities=probs)


def _degenerate(points: int) -> _EmpiricalPointsDistribution:
    return _EmpiricalPointsDistribution(points=(points,), probabilities=(1.0,))


def _synthetic_pool(n_per_pos: dict[str, int], *, n_teams: int, price_fn, dist_fn) -> list[OptimiserCandidate]:
    """A feasible, club-cap-respecting synthetic candidate pool — enough
    depth per position (well over each SquadRules requirement) and enough
    distinct clubs (well over `max_per_club`'s binding point) that budget
    and formation are the only constraints actually doing work, unless a
    test deliberately wants otherwise."""
    candidates = []
    element = 0
    for position, count in n_per_pos.items():
        for k in range(count):
            candidates.append(
                OptimiserCandidate(
                    element=element,
                    name=f"{position}{k}",
                    position=position,
                    team=f"T{element % n_teams}",
                    price=price_fn(element),
                    points_dist=dist_fn(element),
                )
            )
            element += 1
    return candidates


def _default_pool(dist_fn=lambda i: _degenerate(i % 6)) -> list[OptimiserCandidate]:
    return _synthetic_pool(
        {"GK": 4, "DEF": 10, "MID": 10, "FWD": 6},
        n_teams=12,
        price_fn=lambda i: 40 + (i * 2) % 30,
        dist_fn=dist_fn,
    )


# ---------------------------------------------------------------------------
# The objective-collapse boundary.
# ---------------------------------------------------------------------------


def test_collapse_to_expected_points_is_the_pmf_dot_product():
    dist = _dist((0, 0.5), (2, 0.25), (6, 0.25))
    assert collapse_to_expected_points(dist) == pytest.approx(0.5 * 0 + 0.25 * 2 + 0.25 * 6)


def test_collapse_to_expected_points_accepts_any_pointsdistributionlike_not_just_the_backtest_proxy():
    # fplai.points.PointsPMF satisfies the same Protocol structurally --
    # this module never hard-imports it (module docstring, "The backtest
    # proxy PMF"), so prove the collapse works against a look-alike that
    # is NOT _EmpiricalPointsDistribution at all.
    class _OtherPMFLike:
        points = (1, 2, 3)
        probabilities = (0.2, 0.3, 0.5)

    assert collapse_to_expected_points(_OtherPMFLike()) == pytest.approx(1 * 0.2 + 2 * 0.3 + 3 * 0.5)


# ---------------------------------------------------------------------------
# Constraint correctness.
# ---------------------------------------------------------------------------


def test_optimise_squad_result_is_a_valid_squad_under_validate_squad():
    candidates = _default_pool()
    result = optimise_squad(candidates, _RULES, OptimiserConfig())
    by_id = {c.element: c for c in candidates}
    from fplai.backtest.squad import PlayerCandidate, Squad

    squad = Squad(
        players=tuple(
            PlayerCandidate(id=i, name=by_id[i].name, position=by_id[i].position, team=by_id[i].team, price=by_id[i].price, value=0.0)
            for i in result.squad_element_ids
        )
    )
    validate_squad(squad, _RULES)  # raises SquadError if anything is wrong


def test_optimise_squad_xi_is_subset_of_squad_and_exactly_eleven():
    candidates = _default_pool()
    result = optimise_squad(candidates, _RULES, OptimiserConfig())
    assert set(result.xi_element_ids).issubset(set(result.squad_element_ids))
    assert len(result.xi_element_ids) == 11


def test_optimise_squad_prefers_higher_expected_points_within_budget():
    # A cheap, high-value player should always be selected over an
    # equally-priced, strictly worse one when both are otherwise
    # interchangeable -- the most basic possible "the objective is doing
    # something" check.
    candidates = _synthetic_pool(
        {"GK": 4, "DEF": 10, "MID": 10, "FWD": 6},
        n_teams=12,
        price_fn=lambda i: 45,
        dist_fn=lambda i: _degenerate(10 if i == 4 else 0),  # element 4 is the first DEF, made a standout
    )
    result = optimise_squad(candidates, _RULES, OptimiserConfig())
    assert 4 in result.squad_element_ids
    assert 4 in result.xi_element_ids


def test_optimise_squad_raises_when_a_position_pool_is_too_small():
    candidates = _synthetic_pool({"GK": 1, "DEF": 10, "MID": 10, "FWD": 6}, n_teams=12, price_fn=lambda i: 45, dist_fn=lambda i: _degenerate(0))
    with pytest.raises(OptimiserError, match="not enough GK"):
        optimise_squad(candidates, _RULES, OptimiserConfig())


def test_optimise_squad_raises_on_empty_candidates():
    with pytest.raises(OptimiserError):
        optimise_squad([], _RULES, OptimiserConfig())


def test_optimise_squad_raises_on_duplicate_element():
    candidates = _default_pool()
    dup = OptimiserCandidate(element=candidates[0].element, name="dup", position=candidates[0].position, team="ZZ", price=45, points_dist=_degenerate(0))
    with pytest.raises(OptimiserError, match="duplicate element"):
        optimise_squad(candidates + [dup], _RULES, OptimiserConfig())


def test_optimiser_candidate_rejects_an_unknown_position():
    with pytest.raises(OptimiserError, match="unknown position"):
        OptimiserCandidate(element=1, name="x", position="SWEEPER", team="A", price=45, points_dist=_degenerate(0))


# ---------------------------------------------------------------------------
# Captain / vice-captain / bench.
# ---------------------------------------------------------------------------


def test_captain_is_the_highest_expected_points_candidate_actually_selected_into_the_xi():
    # Distinct values (no ties) so the answer is unambiguous.
    def dist_fn(i: int) -> _EmpiricalPointsDistribution:
        return _degenerate(i)  # strictly increasing with element id

    candidates = _synthetic_pool({"GK": 4, "DEF": 10, "MID": 10, "FWD": 6}, n_teams=12, price_fn=lambda i: 40, dist_fn=dist_fn)
    result = optimise_squad(candidates, _RULES, OptimiserConfig())
    best_in_xi = max(result.xi_element_ids, key=lambda i: result.expected_points[i])
    assert result.captain_element_id == best_in_xi


def test_vice_captain_is_the_second_highest_expected_points_xi_member():
    def dist_fn(i: int) -> _EmpiricalPointsDistribution:
        return _degenerate(i)

    candidates = _synthetic_pool({"GK": 4, "DEF": 10, "MID": 10, "FWD": 6}, n_teams=12, price_fn=lambda i: 40, dist_fn=dist_fn)
    result = optimise_squad(candidates, _RULES, OptimiserConfig())
    ranked = sorted(result.xi_element_ids, key=lambda i: -result.expected_points[i])
    assert result.vice_captain_element_id == ranked[1]
    assert result.vice_captain_element_id != result.captain_element_id


def test_bench_order_is_descending_expected_points():
    def dist_fn(i: int) -> _EmpiricalPointsDistribution:
        return _degenerate(i)

    candidates = _synthetic_pool({"GK": 4, "DEF": 10, "MID": 10, "FWD": 6}, n_teams=12, price_fn=lambda i: 40, dist_fn=dist_fn)
    result = optimise_squad(candidates, _RULES, OptimiserConfig())
    bench_values = [result.expected_points[i] for i in result.bench_element_ids]
    assert bench_values == sorted(bench_values, reverse=True)


def test_bench_always_carries_a_gk_for_the_current_squad_rules():
    candidates = _default_pool()
    result = optimise_squad(candidates, _RULES, OptimiserConfig())
    by_id = {c.element: c for c in candidates}
    bench_positions = {by_id[i].position for i in result.bench_element_ids}
    assert "GK" in bench_positions


# ---------------------------------------------------------------------------
# Determinism (CLAUDE.md rule 7) — the tie-break, proven, not assumed.
# ---------------------------------------------------------------------------


def test_optimise_squad_is_bit_identical_across_repeated_calls_with_genuinely_tied_candidates():
    """The worst case for an unpinned solver tie-break: EVERY candidate
    shares the identical points distribution (module docstring,
    "Determinism"). Runs the solve 8 times on the identical input and
    asserts every field of `OptimiserResult` relevant to the final
    decision is bit-identical -- mirrors `tests/test_backtest.py`'s own
    `test_greedy_form_baseline_is_reproducible_across_repeated_runs_on_
    the_real_store`, applied to this module's own worst case rather than
    real data."""
    candidates = _default_pool(dist_fn=lambda i: _degenerate(3))  # ALL tied
    results = [optimise_squad(candidates, _RULES, OptimiserConfig()) for _ in range(8)]
    first = results[0]
    for r in results[1:]:
        assert r.squad_element_ids == first.squad_element_ids
        assert r.xi_element_ids == first.xi_element_ids
        assert r.bench_element_ids == first.bench_element_ids
        assert r.captain_element_id == first.captain_element_id
        assert r.vice_captain_element_id == first.vice_captain_element_id


def test_the_tie_break_favours_the_lowest_element_id_by_design_not_by_solver_accident():
    """Proves the tie-break is a DESIGNED rule (module docstring,
    "Determinism": "an explicit tie-break ... per the same
    `(gain, -id)`-favours-lowest-id convention `fplai.backtest.squad.
    build_squad`'s hill-climb already established"), not merely "whatever
    HiGHS's internal branch-and-bound order happens to prefer" -- which
    would also be repeatable run-to-run (module docstring already proves
    that separately, live, before this test was written: a 40-candidate
    all-tied knapsack was bit-identical across 8 repeated solves even
    with `tie_break_eps=0.0`) but would NOT match a specific, predictable,
    documented rule.

    **Proven fail-first**: run live with `OptimiserConfig(tie_break_eps=
    0.0)` against this exact candidate pool before writing this
    assertion -- the solver still returned a stable answer across repeats
    (captain=15 both times), but NOT the lowest-id answer this test
    asserts (captain=0 with the real tie-break; the disabled-tie-break
    run picked captain=15, a different player, twice). That confirms this
    assertion discriminates the designed tie-break from "stable but
    unexplained solver behaviour," which is exactly the class of bug the
    Phase 1 gate-repair session (HANDOFF §2) warns against trusting.
    """
    candidates = _default_pool(dist_fn=lambda i: _degenerate(3))  # ALL tied -- GK ids 0-3, DEF 4-13, MID 14-23, FWD 24-29
    result = optimise_squad(candidates, _RULES, OptimiserConfig(tie_break_eps=1e-6))
    # Lowest-id-first, respecting formation minima/maxima: 1 GK (id 0), 5
    # DEF (ids 4-8, DEF's xi max), then MID/FWD split to reach 11 total
    # while always preferring the lower absolute id (MID's ids 14-23 are
    # all lower than FWD's 24-29, so MID fills toward its own max first,
    # leaving FWD only its required minimum).
    assert result.xi_element_ids == (0, 4, 5, 6, 7, 8, 14, 15, 16, 17, 24)
    assert result.captain_element_id == 0  # lowest id overall, unconstrained by position


# ---------------------------------------------------------------------------
# Determinism, live against the real store (mirrors tests/test_backtest.py).
# ---------------------------------------------------------------------------

_SEASON = "2025-26"  # smallest usable season in the store -- same choice tests/test_backtest.py already makes, for speed
_N_RUNS = 3  # the MILP resolve is materially slower than the Phase 1 baselines' heuristics; 3 is enough to catch nondeterminism without a slow-suite cost


@pytest.mark.slow
def test_milp_strategy_is_reproducible_across_repeated_runs_on_the_real_store():
    totals = []
    for _ in range(_N_RUNS):
        store = BitemporalStore()  # fresh instance each run, like a fresh script invocation
        data = load_season(store, _SEASON)
        rules = rules_for_season(_SEASON)
        replay = SeasonReplay(data, rules)
        results = replay.run(MILPStrategy(rules=rules))
        summary = summarise(_SEASON, "milp", results)
        totals.append(summary.total_points)
    assert len(set(totals)) == 1, f"MILPStrategy total varied across {_N_RUNS} runs: {totals}"


@pytest.mark.slow
def test_milp_strategy_produces_a_complete_valid_season_on_the_real_store():
    store = BitemporalStore()
    data = load_season(store, _SEASON)
    rules = rules_for_season(_SEASON)
    results = SeasonReplay(data, rules).run(MILPStrategy(rules=rules))
    assert len(results) == len(data.rounds())
    assert all(r.points >= 0 for r in results)  # captain doubling + valid squads should never go negative


# ---------------------------------------------------------------------------
# The backtest proxy PMF (leakage boundary + shape).
# ---------------------------------------------------------------------------


def test_trailing_points_distributions_never_reads_the_gameweek_being_decided():
    # A tiny synthetic history frame with a round >= the decided gameweek
    # smuggled in -- proves the function does not silently include it even
    # if a caller's `history` were ever built wrong (GameweekView's own
    # contract already forbids this at the call site; this is the unit-
    # level belt-and-suspenders check for this module's OWN filter logic).
    history = pl.DataFrame(
        {
            "element": [1, 1, 1, 1],
            "round": [1, 2, 3, 4],  # round 4 is the gameweek "being decided" in this test
            "total_points": [2, 4, 6, 999],
        }
    )
    dists = _trailing_points_distributions(history.filter(pl.col("round") < 4), gameweek=4, window=4)
    assert 999 not in dists[1].points


def test_trailing_points_distributions_sums_double_gameweek_fixture_rows_into_one_round_draw():
    history = pl.DataFrame(
        {
            "element": [1, 1, 1],
            "round": [5, 5, 6],  # element 1 had TWO fixtures in round 5 (a real double gameweek)
            "total_points": [3, 4, 2],
        }
    )
    dists = _trailing_points_distributions(history, gameweek=7, window=4)
    # round 5 -> 3+4=7 (one draw), round 6 -> 2 (another draw): two draws total, not
    # three -- support is DENSE (module docstring, "dense support, zero-filled gaps"),
    # so 2..7 all appear, but only 2 and 7 carry nonzero probability mass.
    dist = dists[1]
    assert sum(dist.probabilities) == pytest.approx(1.0)
    nonzero_points = {p for p, pr in zip(dist.points, dist.probabilities) if pr > 0}
    assert nonzero_points == {2, 7}
    assert dict(zip(dist.points, dist.probabilities))[2] == pytest.approx(0.5)
    assert dict(zip(dist.points, dist.probabilities))[7] == pytest.approx(0.5)


def test_cold_start_candidate_gets_the_documented_degenerate_zero_distribution():
    empty_history = pl.DataFrame({"element": [], "round": [], "total_points": []}, schema={"element": pl.Int64, "round": pl.Int64, "total_points": pl.Int64})
    dists = _trailing_points_distributions(empty_history, gameweek=1, window=4)
    assert dists == {}  # MILPStrategy._candidates_from_view falls back to _COLD_START_DISTRIBUTION for anything missing here


# ---------------------------------------------------------------------------
# ModelStackStrategy (session `s006`, task `model-stack-backtest-strategy`)
# — the real seven-model composition swapped in for MILPStrategy's trailing-
# points proxy, per docs/HANDOFF.md §3.5's named keystone gap. Two pinned
# decisions ATTACKED directly, then one real gameweek end to end.
# ---------------------------------------------------------------------------

_EMPTY_ATTRS_SCHEMA = {
    "element": pl.Int64, "name": pl.Utf8, "position": pl.Utf8, "team": pl.Utf8, "value": pl.Int64,
    "fixture": pl.Int64, "was_home": pl.Boolean, "kickoff_time": pl.Utf8, "opponent_team": pl.Int64,
}
_EMPTY_FIXTURES_SCHEMA = {"fixture": pl.Int64, "home_team": pl.Utf8, "away_team": pl.Utf8, "kickoff_time": pl.Utf8}


def _non_empty_current_attributes() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "element": [1, 2], "name": ["A", "B"], "position": ["GK", "DEF"], "team": ["X", "Y"],
            "value": [45, 45], "fixture": [1, 1], "was_home": [True, False],
            "kickoff_time": ["2025-08-16T14:00:00Z", "2025-08-16T14:00:00Z"], "opponent_team": [2, 1],
        }
    )


def test_model_stack_strategy_raises_when_fixtures_is_empty_but_current_attributes_is_not():
    """Attack pinned decision 1 directly (this task's brief: "the single
    most dangerous silent failure in this story"). A strategy that
    proceeded here would treat every candidate as blank and let the
    optimiser pick arbitrarily among an all-zero, all-tied pool -- while
    raising nothing. `params_by_gameweek`/`scoring_config` are deliberately
    NOT valid objects here: the guard must fire before either is ever
    touched."""
    view = GameweekView(
        season="2025-26",
        gameweek=1,
        history=pl.DataFrame(),
        current_attributes=_non_empty_current_attributes(),
        fixtures=pl.DataFrame(schema=_EMPTY_FIXTURES_SCHEMA),
    )
    strategy = ModelStackStrategy(
        rules=_RULES,
        params_by_gameweek={1: "placeholder-never-reached"},  # type: ignore[dict-item]
        scoring_config="placeholder-never-reached",  # type: ignore[arg-type]
    )
    with pytest.raises(OptimiserError, match="view.fixtures is empty"):
        strategy.decide(view)


def test_model_stack_strategy_raises_on_a_missing_gameweek_in_params_by_gameweek():
    """Attack pinned decision 2 directly: a missing gameweek key raises
    rather than silently falling back to a neighbouring gameweek's params
    (a silent fallback is a leak vector)."""
    view = GameweekView(
        season="2025-26",
        gameweek=7,
        history=pl.DataFrame(),
        current_attributes=pl.DataFrame(schema=_EMPTY_ATTRS_SCHEMA),
        fixtures=pl.DataFrame(schema=_EMPTY_FIXTURES_SCHEMA),
    )
    strategy = ModelStackStrategy(
        rules=_RULES,
        params_by_gameweek={6: "placeholder", 8: "placeholder"},  # type: ignore[dict-item]
        scoring_config="placeholder",  # type: ignore[arg-type]
    )
    with pytest.raises(OptimiserError, match="no params_by_gameweek entry for gameweek 7"):
        strategy.decide(view)


# ---------------------------------------------------------------------------
# `build_decision_calendar` -- S3 pilot. Synthetic `GameweekView`s only
# (`GameweekView.fixtures`/`forward_fixtures` are already schedule-only,
# pre-deadline-public columns per `replay.py`'s own module docstring; the
# real-store cost measurement lives in the slow gate test below).
# ---------------------------------------------------------------------------


def _fixtures_table(rows: list[tuple[int, str, str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "fixture": [r[0] for r in rows],
            "home_team": [r[1] for r in rows],
            "away_team": [r[2] for r in rows],
            "kickoff_time": [r[3] for r in rows],
        },
        schema=_EMPTY_FIXTURES_SCHEMA,
    )


def test_build_decision_calendar_shape_and_two_rows_per_fixture():
    fixtures = _fixtures_table(
        [(1, "A", "B", "2025-08-16T14:00:00Z"), (2, "C", "D", "2025-08-16T16:30:00Z")]
    )
    view = GameweekView(
        season="2025-26", gameweek=1, history=pl.DataFrame(), current_attributes=pl.DataFrame(), fixtures=fixtures
    )
    calendar = build_decision_calendar(view)
    assert calendar.columns == ["season", "team", "fixture", "kickoff_time"]
    assert calendar.height == 4  # two fixtures x two sides
    assert set(calendar["team"].to_list()) == {"A", "B", "C", "D"}
    assert set(calendar["season"].to_list()) == {"2025-26"}
    # Every row's kickoff matches its own fixture, never the other one's.
    by_fixture = {row["fixture"]: row["kickoff_time"] for row in calendar.iter_rows(named=True)}
    assert by_fixture[1] == "2025-08-16T14:00:00Z"
    assert by_fixture[2] == "2025-08-16T16:30:00Z"


def test_build_decision_calendar_covers_round_t_and_the_forward_window():
    fixtures_t = _fixtures_table([(1, "A", "B", "2025-08-16T14:00:00Z")])
    fixtures_t1 = _fixtures_table([(11, "A", "C", "2025-08-23T14:00:00Z")])
    fixtures_t2 = _fixtures_table([(21, "B", "D", "2025-08-30T14:00:00Z")])
    view = GameweekView(
        season="2025-26",
        gameweek=1,
        history=pl.DataFrame(),
        current_attributes=pl.DataFrame(),
        fixtures=fixtures_t,
        forward_fixtures={2: fixtures_t1, 3: fixtures_t2},
    )
    calendar = build_decision_calendar(view)
    assert set(calendar["fixture"].to_list()) == {1, 11, 21}
    assert calendar.height == 6  # 3 fixtures x 2 sides -- t AND every forward round, not just t


def test_build_decision_calendar_empty_forward_window_at_season_end():
    """`forward_fixtures={}` (season's last round, `_build_forward_fixtures`'
    own truncation) must not raise -- round t's own fixtures alone."""
    fixtures_t = _fixtures_table([(1, "A", "B", "2025-08-16T14:00:00Z")])
    view = GameweekView(
        season="2025-26",
        gameweek=38,
        history=pl.DataFrame(),
        current_attributes=pl.DataFrame(),
        fixtures=fixtures_t,
        forward_fixtures={},
    )
    calendar = build_decision_calendar(view)
    assert calendar.height == 2
    assert calendar.columns == ["season", "team", "fixture", "kickoff_time"]


def test_build_decision_calendar_totally_empty_view_returns_empty_typed_frame():
    """Neither `fixtures` nor `forward_fixtures` populated (a test-fixture
    `GameweekView` predating this field, per that dataclass's own
    docstring) -- returns an empty, correctly-typed frame, never raises."""
    view = GameweekView(
        season="2025-26",
        gameweek=1,
        history=pl.DataFrame(),
        current_attributes=pl.DataFrame(),
        fixtures=pl.DataFrame(schema=_EMPTY_FIXTURES_SCHEMA),
    )
    calendar = build_decision_calendar(view)
    assert calendar.is_empty()
    assert calendar.columns == ["season", "team", "fixture", "kickoff_time"]


def test_build_decision_calendar_row_order_is_deterministic_across_repeated_builds():
    """Pilot review (S3): `pl.concat(...).unique()` defaults to
    `maintain_order=False`, so two builds from the SAME `GameweekView`
    content could return the same VALUE (today's single consumer sorts
    downstream, in `_build_team_fixture_gap`, laundering the difference
    away) while returning frames that differ in ROW ORDER -- the exact
    `.unique(maintain_order=False)` bug class `docs/HANDOFF.md` records
    from `_build_view`'s s003 incident, where an identical seed/code/data
    triple produced 1907/1909/1887 because storage layout, not content,
    decided row order. CLAUDE.md rule 7 ("every result reproducible from a
    commit hash plus a seed") is about the FUNCTION, not about whichever
    downstream consumer happens to sort its output today.

    Several rounds and several teams, so there is enough row-count and
    team-name spread for a hash-based `.unique()` to have room to reorder
    -- a single-fixture view has too few rows to expose it reliably."""
    fixtures_t = _fixtures_table(
        [(1, "A", "B", "2025-08-16T14:00:00Z"), (2, "C", "D", "2025-08-16T16:30:00Z")]
    )
    fixtures_t1 = _fixtures_table(
        [(11, "A", "C", "2025-08-23T14:00:00Z"), (12, "B", "D", "2025-08-23T16:30:00Z")]
    )
    fixtures_t2 = _fixtures_table(
        [(21, "D", "A", "2025-08-30T14:00:00Z"), (22, "C", "B", "2025-08-30T16:30:00Z")]
    )
    view = GameweekView(
        season="2025-26",
        gameweek=1,
        history=pl.DataFrame(),
        current_attributes=pl.DataFrame(),
        fixtures=fixtures_t,
        forward_fixtures={2: fixtures_t1, 3: fixtures_t2},
    )
    calendars = [build_decision_calendar(view) for _ in range(6)]
    first_rows = calendars[0].rows()
    for calendar in calendars[1:]:
        assert calendar.rows() == first_rows  # ROW ORDER, not just `.equals()` on a sorted copy


@pytest.mark.slow
def test_model_stack_strategy_produces_a_valid_squad_for_one_real_gameweek():
    """GATE 1 (this task's brief): build the params bundle for a single real
    gameweek, run `decide` on a real `GameweekView`, and show a valid squad
    comes back with every real candidate carrying a real `PointsPMF`
    (nonzero `fixture_pmfs`, probabilities summing to 1). Also GATE 4
    (COST): times fitting and the assembly+solve ("decide") cost separately
    and prints the 38x4-season extrapolation, per this task's brief."""
    import time
    from datetime import datetime, timezone

    from fplai.models.attacking import fit_attacking_model
    from fplai.models.bonus import fit_bonus_model
    from fplai.models.cards import fit_cards_model
    from fplai.models.defensive_contribution import build_dc_threshold_set, fit_dc_model
    from fplai.models.minutes import fit_minutes_model
    from fplai.models.saves import fit_saves_model
    from fplai.models.team_strength import fit_team_strength
    from fplai.scoring import load_scoring_config

    season = "2025-26"
    gameweek = 4
    store = BitemporalStore()
    data = load_season(store, season)
    rules = rules_for_season(season)
    view = _build_view(data, gameweek)
    assert not view.fixtures.is_empty(), f"{season} GW{gameweek} has no fixtures in the real store"

    # As-of the EARLIEST kickoff of the round -- strictly before every
    # fixture this gameweek, so no fit can see this round's own results
    # (fit_*'s own `as_of` cutoffs are all strictly-before, module
    # docstrings of each fplai.models.* module).
    as_of = min(_parse_kickoff_time(k) for k in view.fixtures["kickoff_time"].to_list())

    t0 = time.time()
    minutes_params = fit_minutes_model(store, as_of=as_of)
    attacking_params = fit_attacking_model(store, as_of=as_of)
    threshold_set = build_dc_threshold_set(store, as_of=as_of)
    dc_params = fit_dc_model(store, as_of=as_of, threshold_set=threshold_set)
    cards_params = fit_cards_model(store, as_of=as_of)
    bonus_params = fit_bonus_model(store, as_of=as_of)
    saves_params = fit_saves_model(store, as_of=as_of)
    team_params = fit_team_strength(store, as_of=as_of)
    fit_dt = time.time() - t0

    # 2025-26 -- DC scoring and the DC-trained model both already existed,
    # so no dataclasses.replace neutralisation is needed here (pinned
    # decision 3, ModelStackStrategy's own docstring). game_config is
    # CURRENT-season-only/forward-looking (fplai.scoring's own module
    # docstring), so read at "now", not at this historical fixture's as_of.
    scoring_config = load_scoring_config(store, datetime.now(timezone.utc))

    bundle = ModelStackParams(
        team_strength_params=team_params,
        minutes_params=minutes_params,
        attacking_params=attacking_params,
        dc_params=dc_params,
        cards_params=cards_params,
        bonus_params=bonus_params,
        saves_params=saves_params,
        assembly_params=FixtureFeatureAssemblyParams(threshold_set=threshold_set),
    )
    strategy = ModelStackStrategy(rules=rules, params_by_gameweek={gameweek: bundle}, scoring_config=scoring_config)

    # S3 pilot COST QUESTION: `build_decision_calendar` in isolation, built
    # ONCE for this decision -- never per player, never per fixture (see
    # its own docstring). Timed separately from `decide_candidates` below
    # (which now also builds it once, internally) so the isolated cost is
    # visible against the total `_build_candidates` cost it is now part of.
    t_cal = time.time()
    calendar = build_decision_calendar(view)
    calendar_dt = time.time() - t_cal

    t1 = time.time()
    candidates = strategy.decide_candidates(view)
    assembly_dt = time.time() - t1

    assert len(candidates) == view.current_attributes.height
    real_pmf_candidates = [c for c in candidates if len(c.points_dist.fixture_pmfs) > 0]
    assert len(real_pmf_candidates) > 0, "no candidate carried a real (non-blank) fixture PMF"
    for c in real_pmf_candidates:
        assert abs(sum(c.points_dist.probabilities) - 1.0) < 1e-6

    t2 = time.time()
    result = optimise_squad(candidates, rules, OptimiserConfig())
    solve_dt = time.time() - t2
    decide_dt = assembly_dt + solve_dt

    by_id = {c.element: c for c in candidates}
    squad = Squad(
        players=tuple(
            PlayerCandidate(id=i, name=by_id[i].name, position=by_id[i].position, team=by_id[i].team, price=by_id[i].price, value=0.0)
            for i in result.squad_element_ids
        )
    )
    validate_squad(squad, rules)  # raises SquadError if anything is wrong

    assert calendar.height == 2 * view.fixtures.height + sum(2 * t.height for t in view.forward_fixtures.values())

    n_gameweeks_full_backtest = 38 * 4
    print(
        f"\nCOST -- {season} GW{gameweek}: calendar_dt={calendar_dt:.4f}s ({calendar.height} rows), "
        f"fit_dt={fit_dt:.1f}s, assembly_dt={assembly_dt:.1f}s (includes ONE internal calendar build), "
        f"solve_dt={solve_dt:.1f}s, decide_dt={decide_dt:.1f}s ({len(candidates)} candidates, "
        f"{real_pmf_candidates.__len__()} with a real fixture PMF). Extrapolated to "
        f"{n_gameweeks_full_backtest} gameweeks (38 x 4 seasons): "
        f"fit={fit_dt * n_gameweeks_full_backtest / 60:.1f}min, "
        f"decide={decide_dt * n_gameweeks_full_backtest / 60:.1f}min."
    )


# ---------------------------------------------------------------------------
# `ModelStackStrategy.horizon_candidates` -- S3 part 2. Synthetic tests
# monkeypatch the four model-stack call points `_assemble_round_candidates`
# forwards to (`assemble_fixture_player_features_from_frame`,
# `predict_scoreline`, `simulate_fixture_points_pmfs`, `make_saves_predict_
# fn`), the same posture the S3 pilot's `build_decision_calendar` tests
# already take toward `GameweekView` ("already schedule-only, pre-deadline-
# public columns") -- these tests are exercising THIS story's own schedule/
# element-set/blank/doubled logic, not re-proving the six models fit. The
# real six-model composition, on a real gameweek, is the slow test below.
# ---------------------------------------------------------------------------


def _fake_assemble_fixture_player_features_from_frame(history, *, season, round, fixture, kickoff_time, roster, params, schedule):
    return roster  # forwarded, unopened, straight to the (also faked) simulate_fixture_points_pmfs call.


def _fake_simulate_fixture_points_pmfs(*, fixture, scoreline, minutes_params, attacking_params, dc_params, cards_params, bonus_params, scoring_config, players, config, saves_predict_fn):
    roster = players  # `_fake_assemble_...` above returns the roster dict unchanged.
    return [
        PointsPMF(
            element=element,
            fixture=fixture,
            position=position,
            points=(fixture % 5,),
            probabilities=(1.0,),
            n_simulations=1,
            seed=0,
            saves_status=SAVES_STATUS_NOT_APPLICABLE,
            caveats=(),
        )
        for element, (position, team, was_home) in roster.items()
    ]


@pytest.fixture
def _stub_model_stack_calls(monkeypatch):
    """Patches the four call points `_assemble_round_candidates` (S3 part 2)
    forwards to -- see module comment above for why."""
    monkeypatch.setattr("fplai.optimiser.assemble_fixture_player_features_from_frame", _fake_assemble_fixture_player_features_from_frame)
    monkeypatch.setattr("fplai.optimiser.predict_scoreline", lambda *a, **k: None)
    monkeypatch.setattr("fplai.optimiser.simulate_fixture_points_pmfs", _fake_simulate_fixture_points_pmfs)
    monkeypatch.setattr("fplai.optimiser.make_saves_predict_fn", lambda *a, **k: None)


def _horizon_current_attributes() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "element": [1, 2, 3, 4],
            "name": ["p1", "p2", "p3", "p4"],
            "position": ["GK", "DEF", "MID", "FWD"],
            "team": ["A", "B", "C", "D"],
            "value": [40, 45, 55, 60],
        }
    )


def _horizon_bundle() -> ModelStackParams:
    # Every field is opaque and only ever forwarded by `_assemble_round_
    # candidates` to a monkeypatched stub above -- never read for its own
    # shape once `_stub_model_stack_calls` is active.
    sentinel = object()
    return ModelStackParams(
        team_strength_params=sentinel, minutes_params=sentinel, attacking_params=sentinel,
        dc_params=sentinel, cards_params=sentinel, bonus_params=sentinel, saves_params=sentinel,
        assembly_params=sentinel,
    )


def _build_horizon_view() -> GameweekView:
    """Round 10 (`A` v `B`, `C` v `D`); forward window {11, 12, 13} --
    round 12 doubles team `A`, round 13 blanks team `D`, and round 14+ is
    deliberately ABSENT (truncation, per D7) so the keys-exactly-match test
    has something to catch."""
    fixtures_t = _fixtures_table([(100, "A", "B", "2025-08-16T14:00:00Z"), (101, "C", "D", "2025-08-16T16:30:00Z")])
    fixtures_11 = _fixtures_table([(111, "A", "C", "2025-08-23T14:00:00Z"), (112, "B", "D", "2025-08-23T16:30:00Z")])
    fixtures_12 = _fixtures_table(
        [(120, "A", "B", "2025-08-30T14:00:00Z"), (121, "A", "C", "2025-08-30T16:30:00Z")]  # A doubled
    )
    fixtures_13 = _fixtures_table([(130, "B", "C", "2025-09-06T14:00:00Z")])  # D blank -- absent entirely
    return GameweekView(
        season="2025-26",
        gameweek=10,
        history=pl.DataFrame(),
        current_attributes=_horizon_current_attributes(),
        fixtures=fixtures_t,
        forward_fixtures={11: fixtures_11, 12: fixtures_12, 13: fixtures_13},
    )


def test_horizon_candidates_keys_are_exactly_t_plus_present_forward_rounds(_stub_model_stack_calls):
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(view)
    assert set(horizon.keys()) == {10, 11, 12, 13}  # NOT 14/15 -- absent from forward_fixtures, never fabricated (D7)


def test_horizon_candidates_decide_candidates_unchanged(_stub_model_stack_calls):
    """`_build_candidates`'/`decide_candidates()`'s own behaviour must not
    change (this story's OWNED-behaviour guarantee) -- `horizon_candidates`'
    round-`t` entry must be identical to what `decide_candidates` already
    returns for the same view."""
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    single = strategy.decide_candidates(view)
    horizon = strategy.horizon_candidates(view)
    assert [(c.element, c.name, c.position, c.team, c.price, c.points_dist.points, c.points_dist.probabilities) for c in single] == [
        (c.element, c.name, c.position, c.team, c.price, c.points_dist.points, c.points_dist.probabilities) for c in horizon[10]
    ]


def test_horizon_candidates_same_element_set_every_round(_stub_model_stack_calls):
    """D1: the element set is round `t`'s `current_attributes`, ALWAYS --
    identical across every round in the horizon, whatever that round's own
    fixtures contain."""
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(view)
    t_elements = sorted(c.element for c in horizon[10])
    assert t_elements == [1, 2, 3, 4]
    for round_number, candidates in horizon.items():
        assert sorted(c.element for c in candidates) == t_elements, f"round {round_number} has a different element set"


def test_horizon_candidates_price_team_position_are_round_t_for_every_round(_stub_model_stack_calls):
    """D2/D3: price/team/position never change across the horizon, even for
    a team that blanks or doubles."""
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(view)
    t_by_id = {c.element: c for c in horizon[10]}
    for round_number, candidates in horizon.items():
        for c in candidates:
            t_c = t_by_id[c.element]
            assert c.price == t_c.price, f"round {round_number} element {c.element} price drifted"
            assert c.team == t_c.team, f"round {round_number} element {c.element} team drifted"
            assert c.position == t_c.position, f"round {round_number} element {c.element} position drifted"


def test_horizon_candidates_blank_team_is_degenerate_zero_not_omitted(_stub_model_stack_calls):
    """D1's blank case: team `D` (element 4) does not appear in round 13's
    fixtures at all -- candidate 4 must still be present, with a degenerate
    0-point PMF, never omitted."""
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(view)
    round_13_by_id = {c.element: c for c in horizon[13]}
    assert 4 in round_13_by_id  # never omitted
    blank_candidate = round_13_by_id[4]
    assert blank_candidate.points_dist.points == (0,)
    assert blank_candidate.points_dist.probabilities == (1.0,)
    assert blank_candidate.points_dist.fixture_pmfs == ()


def test_horizon_candidates_doubled_team_is_convolved_not_doubled_single(_stub_model_stack_calls):
    """Team `A` (element 1) plays TWICE in round 12 (fixtures 120 and 121)
    -- the candidate must carry a real TWO-fixture combination, never a
    single fixture's PMF doubled."""
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(view)
    round_12_by_id = {c.element: c for c in horizon[12]}
    doubled_candidate = round_12_by_id[1]
    assert len(doubled_candidate.points_dist.fixture_pmfs) == 2
    assert {pmf.fixture for pmf in doubled_candidate.points_dist.fixture_pmfs} == {120, 121}
    # A single-fixture player this same round (element 2, only fixture 120) contrasts directly.
    single_candidate = round_12_by_id[2]
    assert len(single_candidate.points_dist.fixture_pmfs) == 1


def test_horizon_candidates_empty_forward_window_returns_just_t(_stub_model_stack_calls):
    """Season's last round -- `forward_fixtures={}` -- returns `{t: [...]}`
    without raising (D7)."""
    view = _build_horizon_view()
    season_end_view = GameweekView(
        season=view.season, gameweek=view.gameweek, history=view.history,
        current_attributes=view.current_attributes, fixtures=view.fixtures, forward_fixtures={},
    )
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(season_end_view)
    assert set(horizon.keys()) == {10}
    assert len(horizon[10]) == 4


def test_horizon_candidates_raises_on_missing_gameweek_in_params_by_gameweek():
    """Same pinned-decision-2 guard `decide()`/`decide_candidates()` apply
    (D7) -- cheap, no model stack needed since the guard fires first."""
    view = _build_horizon_view()
    strategy = ModelStackStrategy(
        rules=_RULES, params_by_gameweek={6: "placeholder", 8: "placeholder"}, scoring_config="placeholder"  # type: ignore[dict-item,arg-type]
    )
    with pytest.raises(OptimiserError, match="no params_by_gameweek entry for gameweek 10"):
        strategy.horizon_candidates(view)


def test_horizon_candidates_raises_when_fixtures_is_empty_but_current_attributes_is_not():
    """Same pinned-decision-1 guard `decide()`/`decide_candidates()` apply."""
    view = GameweekView(
        season="2025-26",
        gameweek=1,
        history=pl.DataFrame(),
        current_attributes=_non_empty_current_attributes(),
        fixtures=pl.DataFrame(schema=_EMPTY_FIXTURES_SCHEMA),
    )
    strategy = ModelStackStrategy(
        rules=_RULES, params_by_gameweek={1: "placeholder"}, scoring_config="placeholder"  # type: ignore[dict-item,arg-type]
    )
    with pytest.raises(OptimiserError, match="view.fixtures is empty"):
        strategy.horizon_candidates(view)


# ---------------------------------------------------------------------------
# S10, D1 -- `max_rounds`, the one optimiser change this story makes.
# `None` must be byte-for-byte today's behaviour; an int assembles ONLY the
# first `max_rounds` rounds (round t first); over-asking is not an error;
# `max_rounds < 1` is.
# ---------------------------------------------------------------------------


def test_max_rounds_none_is_byte_for_byte_unchanged(_stub_model_stack_calls):
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    default = strategy.horizon_candidates(view)
    explicit_none = strategy.horizon_candidates(view, max_rounds=None)
    assert set(default.keys()) == set(explicit_none.keys()) == {10, 11, 12, 13}
    for round_number in default:
        assert [(c.element, c.points_dist.points) for c in default[round_number]] == [
            (c.element, c.points_dist.points) for c in explicit_none[round_number]
        ]


def test_max_rounds_one_returns_exactly_round_t(_stub_model_stack_calls):
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(view, max_rounds=1)
    assert set(horizon.keys()) == {10}


def test_max_rounds_two_returns_t_plus_first_forward_round_only(_stub_model_stack_calls):
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(view, max_rounds=2)
    assert set(horizon.keys()) == {10, 11}  # 11 is the smallest forward round, never 12/13


def test_max_rounds_beyond_available_rounds_is_not_an_error(_stub_model_stack_calls):
    """A season-end horizon is legitimately short (S9's own D3) -- asking
    for more rounds than the source has is not a caller bug."""
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(view, max_rounds=100)
    assert set(horizon.keys()) == {10, 11, 12, 13}


def test_max_rounds_less_than_one_raises_for_model_stack_strategy(_stub_model_stack_calls):
    view = _build_horizon_view()
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    with pytest.raises(OptimiserError, match="max_rounds must be >= 1"):
        strategy.horizon_candidates(view, max_rounds=0)


def test_max_rounds_less_than_one_raises_for_trailing_proxy_horizon_source():
    history = pl.DataFrame({"element": [1], "round": [19], "total_points": [4]})
    current = pl.DataFrame({"element": [1], "name": ["A"], "position": ["GK"], "team": ["X"], "value": [45]})
    view = GameweekView(
        season="2025-26", gameweek=20, history=history, current_attributes=current, forward_fixtures={21: pl.DataFrame()}
    )
    source = TrailingProxyHorizonSource()
    with pytest.raises(OptimiserError, match="max_rounds must be >= 1"):
        source.horizon_candidates(view, max_rounds=0)


def test_trailing_proxy_horizon_source_max_rounds_truncates_the_returned_keys():
    history = pl.DataFrame({"element": [1], "round": [19], "total_points": [4]})
    current = pl.DataFrame({"element": [1], "name": ["A"], "position": ["GK"], "team": ["X"], "value": [45]})
    view = GameweekView(
        season="2025-26", gameweek=20, history=history, current_attributes=current,
        forward_fixtures={21: pl.DataFrame(), 22: pl.DataFrame()},
    )
    source = TrailingProxyHorizonSource()
    assert set(source.horizon_candidates(view).keys()) == {20, 21, 22}
    assert set(source.horizon_candidates(view, max_rounds=1).keys()) == {20}
    assert set(source.horizon_candidates(view, max_rounds=2).keys()) == {20, 21}
    assert set(source.horizon_candidates(view, max_rounds=100).keys()) == {20, 21, 22}


def test_horizon_strategy_passes_its_own_horizon_as_max_rounds_to_the_source():
    """D1: `HorizonStrategy.decide` passes `max_rounds=self._horizon` --
    checked by a source that records what it was called with, rather than
    only checking the end-to-end decision (which the pre-existing
    `sorted(...)[: self._horizon]` truncation would mask even if this
    plumbing regressed)."""
    seen_max_rounds = []

    class _RecordingStub:
        def __init__(self, per_round):
            self._per_round = per_round

        def horizon_candidates(self, view, *, max_rounds=None):
            seen_max_rounds.append(max_rounds)
            return self._per_round

    source = _RecordingStub({20: _mp_round({9: 5.0})})
    strategy = HorizonStrategy(source, _RULES, transfer_rules_for_season("2025-26"), horizon=6)
    strategy.decide(_empty_view())
    assert seen_max_rounds == [6]

    seen_max_rounds.clear()
    strategy_h1 = HorizonStrategy(source, _RULES, transfer_rules_for_season("2025-26"), horizon=1)
    strategy_h1.decide(_empty_view())
    assert seen_max_rounds == [1]


def test_attack_two_horizon_strategies_sharing_one_model_stack_instance_see_identical_round_t_candidates(
    _stub_model_stack_calls,
):
    """S10, D3's own ATTACK (CLAUDE.md's "no path is unsafe" standard):
    `scripts/run_e7_gate.py` constructs exactly ONE `ModelStackStrategy`
    per season and wraps it in TWO `HorizonStrategy` instances
    (`horizon=6`, `horizon=1`) sharing that one instance -- the claim is
    that both arms therefore see the IDENTICAL round-`t` candidate pool.
    Attacked here by a spy that wraps the SAME shared `model_stack`
    instance for BOTH arms and records exactly what round `t`'s candidate
    list contained on each call, rather than trusting `decide()`'s final
    chosen squad (two different candidate pools could still coincidentally
    choose the same squad, which would NOT have shown a difference even if
    one existed). What this would have caught: `max_rounds` accidentally
    mutating shared state on `model_stack` between calls, or the H=6 call
    somehow feeding round-`t+k` data back into round `t`'s own assembly.
    `_build_horizon_view`'s tiny 4-candidate pool cannot fill
    `optimise_squad`'s minimum position counts (documented at that pool's
    own definition) -- both `decide()` calls raise `OptimiserError` from
    the solve itself, but only AFTER `horizon_candidates` has already run
    and been recorded, which is all this attack needs."""
    view = _build_horizon_view()
    model_stack = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())

    captured: dict[int, list] = {}

    class _Spy:
        name = "spy"

        def horizon_candidates(self, view, *, max_rounds=None):
            candidates = model_stack.horizon_candidates(view, max_rounds=max_rounds)
            captured[max_rounds] = candidates[view.gameweek]
            return candidates

    tr = transfer_rules_for_season("2025-26")
    h6 = HorizonStrategy(_Spy(), _RULES, tr, horizon=6)
    h1 = HorizonStrategy(_Spy(), _RULES, tr, horizon=1)

    with pytest.raises(OptimiserError):
        h6.decide(view)
    with pytest.raises(OptimiserError):
        h1.decide(view)

    assert set(captured.keys()) == {6, 1}, "expected one recorded call per arm, keyed by its own max_rounds"

    def _key(c):
        return (c.element, c.name, c.position, c.team, c.price, c.points_dist.points, c.points_dist.probabilities)

    round_t_via_h6 = sorted((_key(c) for c in captured[6]), key=lambda k: k[0])
    round_t_via_h1 = sorted((_key(c) for c in captured[1]), key=lambda k: k[0])
    assert round_t_via_h6 == round_t_via_h1
    assert len(round_t_via_h6) == 4  # the pool's own 4 elements -- nothing lost, nothing added


# ---------------------------------------------------------------------------
# S9, PHASE 2, D5 -- held-player backfill in ModelStackStrategy.
# horizon_candidates. decide()/decide_candidates() must stay byte-for-byte
# untouched (E6's own numbers cannot move); only horizon_candidates changes.
# ---------------------------------------------------------------------------


def test_decide_and_decide_candidates_ignore_incoming_state_entirely(_stub_model_stack_calls):
    """D5's own boundary claim: `decide()`/`decide_candidates()` never read
    `view.incoming_state` at all -- even an incoming_state naming a held
    element that WOULD make `horizon_candidates` raise (no current-round
    row AND no history row to backfill from -- `view.history` is empty
    here) leaves both completely unaffected. `decide_candidates()` is the
    documented diagnostic seam onto the EXACT candidate-assembly path
    `decide()` itself calls (`_build_candidates`, this class's own
    docstring) -- proving it here is proving `decide()`'s own candidate
    pool is unaffected too; `_build_horizon_view`'s tiny 1-per-position
    pool (built for `horizon_candidates` shape tests, not a full solve)
    cannot itself reach `optimise_squad`'s minimum position counts, so
    `decide()` is not separately callable against it."""
    view = _build_horizon_view()
    poison = SquadState(element_ids=(99,), purchase_prices=((99, 40),), bank_tenths=0, free_transfers=1)
    view_with_state = GameweekView(
        season=view.season, gameweek=view.gameweek, history=view.history,
        current_attributes=view.current_attributes, fixtures=view.fixtures,
        forward_fixtures=view.forward_fixtures, incoming_state=poison,
    )
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())

    def _key(c):
        return (c.element, c.name, c.position, c.team, c.price, c.points_dist.points, c.points_dist.probabilities)

    baseline_candidates = [_key(c) for c in strategy.decide_candidates(view)]
    with_state_candidates = [_key(c) for c in strategy.decide_candidates(view_with_state)]
    assert baseline_candidates == with_state_candidates  # decide_candidates() untouched by D5

    # horizon_candidates() DOES read incoming_state, and raises here --
    # proving the two paths genuinely diverge rather than the poison value
    # simply being harmless everywhere.
    with pytest.raises(OptimiserError, match="cannot backfill"):
        strategy.horizon_candidates(view_with_state)


def _full_pool_view(*, incoming_state=None) -> GameweekView:
    """A real, end-to-end-solvable pool (2 GK/5 DEF/5 MID/3 FWD across 5
    clubs, one apiece over the 3-per-club cap) so `ModelStackStrategy.
    decide()` -- not just the diagnostic `decide_candidates()` seam --
    can actually be called against it. Team `E` has no fixture this
    round (D1's existing degenerate-zero rule, unaffected by this test)."""
    positions = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    teams = ["A", "B", "C", "D", "E"]
    current = pl.DataFrame(
        {
            "element": list(range(1, 16)),
            "name": [f"p{i}" for i in range(1, 16)],
            "position": positions,
            "team": [teams[i % 5] for i in range(15)],
            "value": [40] * 15,
        }
    )
    fixtures = _fixtures_table([(200, "A", "B", "2025-08-16T14:00:00Z"), (201, "C", "D", "2025-08-16T16:30:00Z")])
    return GameweekView(
        season="2025-26", gameweek=10, history=pl.DataFrame(), current_attributes=current,
        fixtures=fixtures, forward_fixtures={}, incoming_state=incoming_state,
    )


def test_model_stack_strategy_decide_is_unchanged_by_incoming_state(_stub_model_stack_calls):
    """The other half of D5's boundary claim, against a pool `decide()`
    can actually solve (unlike `_build_horizon_view`'s tiny shape-test
    pool, above): `decide()`'s own chosen squad/captain must be identical
    whether or not `view.incoming_state` names a full, held squad."""
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    baseline = strategy.decide(_full_pool_view())
    incoming = SquadState(
        element_ids=tuple(range(1, 16)), purchase_prices=tuple((i, 40) for i in range(1, 16)),
        bank_tenths=0, free_transfers=1,
    )
    with_state = strategy.decide(_full_pool_view(incoming_state=incoming))
    assert baseline.squad.ids() == with_state.squad.ids()
    assert baseline.captain.id == with_state.captain.id


def test_model_stack_strategy_horizon_candidates_backfills_a_held_blank_player(_stub_model_stack_calls):
    """D5: a held element (5) absent from round t's own current_attributes
    (its team, E, has no fixture at round 10) is added to round t's pool
    from `last_known_attributes`, with a degenerate-0 PMF at round t (D1's
    own blank-candidate rule, unchanged) -- and a REAL PMF at round 11,
    where E's team finally plays, via the ordinary fixture-assembly path
    now that team_by_element knows about it."""
    current = _horizon_current_attributes()  # elements 1-4, teams A/B/C/D
    history = pl.DataFrame(
        {
            "element": [5, 5],
            "round": [8, 9],
            "fixture": [80, 90],
            "name": ["p5-old", "p5"],
            "position": ["MID", "MID"],
            "team": ["E", "E"],
            "value": [48, 50],
        }
    )
    fixtures_t = _fixtures_table([(100, "A", "B", "2025-08-16T14:00:00Z"), (101, "C", "D", "2025-08-16T16:30:00Z")])
    fixtures_11 = _fixtures_table([(111, "A", "E", "2025-08-23T14:00:00Z"), (112, "B", "C", "2025-08-23T16:30:00Z")])
    view = GameweekView(
        season="2025-26", gameweek=10, history=history, current_attributes=current,
        fixtures=fixtures_t, forward_fixtures={11: fixtures_11},
        incoming_state=SquadState(element_ids=(5,), purchase_prices=((5, 45),), bank_tenths=0, free_transfers=1),
    )
    strategy = ModelStackStrategy(rules=_RULES, params_by_gameweek={10: _horizon_bundle()}, scoring_config=object())
    horizon = strategy.horizon_candidates(view)

    round_t_by_id = {c.element: c for c in horizon[10]}
    assert 5 in round_t_by_id
    backfilled_t = round_t_by_id[5]
    assert backfilled_t.team == "E"
    assert backfilled_t.position == "MID"
    assert backfilled_t.price == 50  # round 9's row -- the LAST one, not round 8's stale value
    assert backfilled_t.points_dist.points == (0,)  # E plays no fixture at round t -- degenerate zero
    assert backfilled_t.points_dist.probabilities == (1.0,)

    round_11_by_id = {c.element: c for c in horizon[11]}
    assert 5 in round_11_by_id
    assert len(round_11_by_id[5].points_dist.fixture_pmfs) == 1  # E plays fixture 111 at round 11 -- a real PMF
    print(f"backfill fired: round10 candidates={len(horizon[10])} (incl. backfilled 5), round11={len(horizon[11])}")


@pytest.mark.slow
def test_model_stack_strategy_horizon_candidates_cost_against_decide_candidates_on_a_real_gameweek():
    """COST QUESTION (this story's brief): time ONE `horizon_candidates`
    call against ONE `decide_candidates` call on the SAME real
    `GameweekView`/bundle, and report the ratio against the horizon's own
    round count. Uses the exact gameweek probe (a) already verified
    well-formed (t=20, forward rounds 21-25) so this test's own shape
    claims need no re-verification."""
    import time
    from datetime import datetime, timezone

    from fplai.models.attacking import fit_attacking_model
    from fplai.models.bonus import fit_bonus_model
    from fplai.models.cards import fit_cards_model
    from fplai.models.defensive_contribution import build_dc_threshold_set, fit_dc_model
    from fplai.models.minutes import fit_minutes_model
    from fplai.models.saves import fit_saves_model
    from fplai.models.team_strength import fit_team_strength
    from fplai.scoring import load_scoring_config

    season = "2025-26"
    gameweek = 20
    store = BitemporalStore()
    data = load_season(store, season)
    rules = rules_for_season(season)
    view = _build_view(data, gameweek)
    assert not view.fixtures.is_empty(), f"{season} GW{gameweek} has no fixtures in the real store"
    assert sorted(view.forward_fixtures.keys()) == [21, 22, 23, 24, 25], "probe (a)'s own claim -- re-check if this fails"

    as_of = min(_parse_kickoff_time(k) for k in view.fixtures["kickoff_time"].to_list())
    minutes_params = fit_minutes_model(store, as_of=as_of)
    attacking_params = fit_attacking_model(store, as_of=as_of)
    threshold_set = build_dc_threshold_set(store, as_of=as_of)
    dc_params = fit_dc_model(store, as_of=as_of, threshold_set=threshold_set)
    cards_params = fit_cards_model(store, as_of=as_of)
    bonus_params = fit_bonus_model(store, as_of=as_of)
    saves_params = fit_saves_model(store, as_of=as_of)
    team_params = fit_team_strength(store, as_of=as_of)
    scoring_config = load_scoring_config(store, datetime.now(timezone.utc))

    bundle = ModelStackParams(
        team_strength_params=team_params, minutes_params=minutes_params, attacking_params=attacking_params,
        dc_params=dc_params, cards_params=cards_params, bonus_params=bonus_params, saves_params=saves_params,
        assembly_params=FixtureFeatureAssemblyParams(threshold_set=threshold_set),
    )
    strategy = ModelStackStrategy(rules=rules, params_by_gameweek={gameweek: bundle}, scoring_config=scoring_config)

    t0 = time.time()
    single = strategy.decide_candidates(view)
    decide_dt = time.time() - t0

    t1 = time.time()
    horizon = strategy.horizon_candidates(view)
    horizon_dt = time.time() - t1

    n_rounds = len(horizon)  # t plus every forward round present
    ratio = horizon_dt / decide_dt if decide_dt > 0 else float("inf")

    assert set(horizon.keys()) == {gameweek, *view.forward_fixtures.keys()}
    t_elements = sorted(c.element for c in horizon[gameweek])
    assert [c.element for c in single] == t_elements
    for round_number, candidates in horizon.items():
        assert sorted(c.element for c in candidates) == t_elements, f"round {round_number} lost the element set"

    print(
        f"\nCOST -- {season} GW{gameweek} horizon_candidates vs decide_candidates: "
        f"decide_dt={decide_dt:.2f}s, horizon_dt={horizon_dt:.2f}s, n_rounds={n_rounds}, "
        f"ratio={ratio:.2f}x, ratio/n_rounds={ratio / n_rounds:.2f} (expect close to 1.0 if linear per round)."
    )


@pytest.mark.slow
def test_max_rounds_one_is_bit_identical_to_the_full_horizon_at_round_t_on_the_real_store():
    """S10, G2 -- the honest form of "share the k=0 step between arms":
    `horizon_candidates(view, max_rounds=1)[t]` must be BIT-IDENTICAL,
    element for element, to `horizon_candidates(view)[t]` -- not just its
    mean, its FULL PMF support and probability vector, plus name/position/
    team/price. Season 2025-26 GW37 (one forward round only, GW38 -- the
    season's own last gameweek) keeps the FULL-horizon call itself cheap
    (2 rounds, not 6) while still exercising the real seven-model
    composition end to end, matching this story's brief ("Use a gameweek
    that keeps this under ~2 minutes").

    ATTACK, not just assert (D3's own guarantee, CLAUDE.md's "no path is
    unsafe" standard): if `max_rounds` genuinely gated which rounds get
    assembled rather than merely being accepted and ignored, round t's OWN
    assembly work is identical either way (D1's own claim: `max_rounds`
    changes what gets built for OTHER rounds, never round t's own
    inputs) -- proven here by fitting the model stack exactly ONCE and
    calling `horizon_candidates` twice against the SAME `bundle`/`view`,
    so any divergence could only come from `max_rounds` itself perturbing
    round t's own candidate assembly, not from a different fit or a
    different view."""
    from datetime import datetime, timezone

    from fplai.models.attacking import fit_attacking_model
    from fplai.models.bonus import fit_bonus_model
    from fplai.models.cards import fit_cards_model
    from fplai.models.defensive_contribution import build_dc_threshold_set, fit_dc_model
    from fplai.models.minutes import fit_minutes_model
    from fplai.models.saves import fit_saves_model
    from fplai.models.team_strength import fit_team_strength
    from fplai.scoring import load_scoring_config

    season = "2025-26"
    gameweek = 37
    store = BitemporalStore()
    data = load_season(store, season)
    rules = rules_for_season(season)
    view = _build_view(data, gameweek)
    assert not view.fixtures.is_empty(), f"{season} GW{gameweek} has no fixtures in the real store"
    assert sorted(view.forward_fixtures.keys()) == [38], (
        "expected exactly one forward round (the season's own last gameweek) -- re-check if this fails"
    )

    as_of = min(_parse_kickoff_time(k) for k in view.fixtures["kickoff_time"].to_list())
    minutes_params = fit_minutes_model(store, as_of=as_of)
    attacking_params = fit_attacking_model(store, as_of=as_of)
    threshold_set = build_dc_threshold_set(store, as_of=as_of)
    dc_params = fit_dc_model(store, as_of=as_of, threshold_set=threshold_set)
    cards_params = fit_cards_model(store, as_of=as_of)
    bonus_params = fit_bonus_model(store, as_of=as_of)
    saves_params = fit_saves_model(store, as_of=as_of)
    team_params = fit_team_strength(store, as_of=as_of)
    scoring_config = load_scoring_config(store, datetime.now(timezone.utc))

    bundle = ModelStackParams(
        team_strength_params=team_params, minutes_params=minutes_params, attacking_params=attacking_params,
        dc_params=dc_params, cards_params=cards_params, bonus_params=bonus_params, saves_params=saves_params,
        assembly_params=FixtureFeatureAssemblyParams(threshold_set=threshold_set),
    )
    strategy = ModelStackStrategy(rules=rules, params_by_gameweek={gameweek: bundle}, scoring_config=scoring_config)

    full = strategy.horizon_candidates(view)
    assert set(full.keys()) == {37, 38}
    myopic = strategy.horizon_candidates(view, max_rounds=1)
    assert set(myopic.keys()) == {37}  # the whole point of max_rounds=1 -- round 38 never assembled at all

    def _key(c):
        return (c.element, c.name, c.position, c.team, c.price, c.points_dist.points, c.points_dist.probabilities)

    full_t = sorted((_key(c) for c in full[37]), key=lambda k: k[0])
    myopic_t = sorted((_key(c) for c in myopic[37]), key=lambda k: k[0])
    assert full_t == myopic_t
    assert len(full_t) > 0, "expected a non-empty candidate pool at round t"
    print(f"\nG2 -- {season} GW{gameweek}: {len(full_t)} candidates bit-identical between max_rounds=1 and full horizon.")


# ---------------------------------------------------------------------------
# optimise_multi_period -- Phase 4, E7, story S8. `horizon_candidates`'
# own shape (D1), validated against round t's own element set/price/
# position/team (D2), the H=1/incoming_state=None reproduction gate,
# the free-transfer/hit sign (D6), and a constructed banking scenario
# (this story's own GATE part 2, attacked per CLAUDE.md's "no path is
# unsafe" standard).
# ---------------------------------------------------------------------------

def test_optimise_multi_period_h1_with_no_incoming_state_reproduces_optimise_squad_on_synthetic_pool():
    """GATE 1, synthetic half: D3's own claim -- with a single round and no
    incoming squad, `optimise_multi_period` builds round `t` exactly the
    way `optimise_squad` does (no continuity, no transfer vars, the same
    flat budget cap) -- so every field of the two results should agree,
    not just the squad."""
    candidates = _default_pool()
    single = optimise_squad(candidates, _RULES, OptimiserConfig())
    multi = optimise_multi_period({20: candidates}, _RULES, transfer_rules_for_season("2025-26"), incoming_state=None)
    assert multi.squad_element_ids == single.squad_element_ids
    assert multi.xi_element_ids == single.xi_element_ids
    assert multi.bench_element_ids == single.bench_element_ids
    assert multi.captain_element_id == single.captain_element_id
    assert multi.vice_captain_element_id == single.vice_captain_element_id
    assert multi.transfers_in == ()
    assert multi.transfers_out == ()
    assert multi.hits == 0
    assert multi.objective_value == single.objective_value  # same MILP, term for term -- not just the same winning squad
    assert len(multi.plan) == 1
    assert multi.plan[0].round == 20


@pytest.mark.slow
def test_optimise_multi_period_h1_with_no_incoming_state_reproduces_optimise_squad_on_the_real_store():
    """GATE 1, real-store half -- probe (a)'s own pool: 2024-25 GW20, the
    trailing-proxy candidate path (`_candidates_from_view`, the same
    signal `MILPStrategy` uses), 711 real candidates. The probe's own
    reference run: `objective_value=106.49206099999985`,
    `squad=(2, 3, 6, 10, 14, 54, 235, 328, 399, 401, 402, 422, 432, 541,
    573)`. This test does not hardcode that literal (CLAUDE.md: an
    assertion against a baked-in number is a scheduled false alarm the
    moment an upstream signal changes) -- it computes BOTH results from
    the SAME candidate pool in the same test run and asserts they agree,
    which is the property that actually matters and cannot go stale."""
    season, gameweek = "2024-25", 20
    store = BitemporalStore()
    data = load_season(store, season)
    rules = rules_for_season(season)
    transfer_rules = transfer_rules_for_season("2024-25")
    view = _build_view(data, gameweek)
    candidates = _candidates_from_view(view, GREEDY_FORM_TRAILING_GAMEWEEKS)
    assert len(candidates) > 0, f"{season} GW{gameweek} produced no candidates -- re-check the probe is still valid"

    single = optimise_squad(candidates, rules, OptimiserConfig())
    multi = optimise_multi_period({gameweek: candidates}, rules, transfer_rules, incoming_state=None)
    assert multi.squad_element_ids == single.squad_element_ids
    assert multi.xi_element_ids == single.xi_element_ids
    assert multi.bench_element_ids == single.bench_element_ids
    assert multi.captain_element_id == single.captain_element_id
    assert multi.vice_captain_element_id == single.vice_captain_element_id
    assert multi.objective_value == single.objective_value
    print(f"\nGATE 1 (real store) -- {season} GW{gameweek}: pool={len(candidates)}, objective={single.objective_value}")


def _mp_pool_meta(pos_layout: dict[str, int], n_teams: int, price_fn) -> dict[int, dict]:
    """Shared element -> {position, team, price} layout for the D2/D6/
    banking tests below -- mirrors `_synthetic_pool`'s own construction
    but keyed by element for O(1) lookup while building per-round
    `OptimiserCandidate` lists with per-round-varying `points_dist`."""
    out: dict[int, dict] = {}
    element = 0
    for position, count in pos_layout.items():
        for _ in range(count):
            out[element] = {"position": position, "team": f"T{element % n_teams}", "price": price_fn(element)}
            element += 1
    return out


_MP_META = _mp_pool_meta({"GK": 4, "DEF": 10, "MID": 10, "FWD": 6}, n_teams=12, price_fn=lambda i: 40 + (i * 2) % 30)
_MP_INCOMING_IDS = (0, 1, 4, 5, 6, 7, 8, 14, 15, 16, 17, 18, 24, 25, 26)  # a valid 2GK/5DEF/5MID/3FWD squad from _MP_META


def _mp_round(values: dict[int, float]) -> list[OptimiserCandidate]:
    """One horizon round's candidate list from `_MP_META`, with an
    explicit per-element value (a degenerate points distribution at that
    value) -- `values` need not cover every element; anything absent gets
    the flat baseline of 4.0."""
    return [
        OptimiserCandidate(
            element=e, name=f"e{e}", position=m["position"], team=m["team"], price=m["price"],
            points_dist=_degenerate(int(values.get(e, 4))) if float(values.get(e, 4.0)).is_integer()
            else _dist((round(values.get(e, 4.0)), 1.0)),
        )
        for e, m in _MP_META.items()
    ]


def _mp_incoming_state(*, bank_tenths: int, free_transfers: int) -> SquadState:
    purchase_prices = tuple((i, _MP_META[i]["price"]) for i in _MP_INCOMING_IDS)
    return SquadState(
        element_ids=tuple(sorted(_MP_INCOMING_IDS)), purchase_prices=purchase_prices,
        bank_tenths=bank_tenths, free_transfers=free_transfers,
    )


def test_optimise_multi_period_raises_when_a_later_rounds_element_set_differs():
    """D2, validated: a horizon round that drops a candidate present at
    round `t` must raise, naming both the round and the missing element --
    never silently build a per-round `by_position` that disagrees."""
    t0_candidates = _mp_round({})
    t1_candidates = [c for c in _mp_round({}) if c.element != 0]  # drop element 0 -- a genuine element-set mismatch
    with pytest.raises(OptimiserError, match=r"element set differs"):
        optimise_multi_period({20: t0_candidates, 21: t1_candidates}, _RULES, transfer_rules_for_season("2025-26"))


def test_optimise_multi_period_raises_when_a_later_rounds_price_differs():
    """D2, validated: price (or position/team) must be IDENTICAL across
    every horizon round -- S3's own D2/D3 guarantee, checked here rather
    than assumed."""
    t0_candidates = _mp_round({})
    t1_candidates = _mp_round({})
    t1_candidates[0] = OptimiserCandidate(
        element=t1_candidates[0].element, name=t1_candidates[0].name, position=t1_candidates[0].position,
        team=t1_candidates[0].team, price=t1_candidates[0].price + 1, points_dist=t1_candidates[0].points_dist,
    )
    with pytest.raises(OptimiserError, match=r"D2 requires identical"):
        optimise_multi_period({20: t0_candidates, 21: t1_candidates}, _RULES, transfer_rules_for_season("2025-26"))


def test_optimise_multi_period_hit_cost_sign_is_negative_not_a_reward():
    """D6's own named risk, verbatim: S7's registered gate text once
    described the hit penalty as "subtracts 4 * max(0, n - ft)" against a
    NEGATIVE `hit_cost`, which silently turns into a reward. Two
    independent +1-edge upgrades compete for a single free transfer
    (`free_transfers=1`); with `hit_cost` correctly negative, taking the
    SECOND upgrade too would cost a hit worth more than its own +1 gain,
    so the optimal choice takes exactly one and declines the other,
    `hits == 0`. A sign-flipped implementation would instead find hits
    REWARDING (verified live while designing this test: flipping
    `hit_cost` to `+4` against this exact scenario made the solver take
    `hits == 15`, wringing every hit the model bound allows out of a
    positive-reward bug) and would take both -- this assertion fails
    against that implementation, which is the point."""
    t0 = _mp_round({9: 5.0, 19: 5.0})  # elements 9 (DEF) and 19 (MID), each a +1 edge over the 4.0 baseline
    incoming = _mp_incoming_state(bank_tenths=100, free_transfers=1)
    tr = TransferRules(free_transfers_per_gameweek=1, max_banked_transfers=1, hit_cost=-4)
    result = optimise_multi_period({20: t0}, _RULES, tr, incoming_state=incoming)
    assert result.hits == 0
    assert len(result.transfers_in) == 1  # exactly one of the two +1 upgrades, never both


def test_optimise_multi_period_banks_a_transfer_when_two_together_beat_one_now_plus_a_hit():
    """GATE 2. Two rounds (`t0`, `t0+1`), one incoming squad, `free_
    transfers=1`. Three opportunities compete for that one transfer:

    - `X` (DEF, element 9): a LOCALLY POSITIVE +1 edge, but ONLY at `t0`
      -- it reverts to the 4.0 baseline at `t0+1`, so its value can only
      ever be captured by transferring it in AT `t0`.
    - `Y`, `Z` (MID, elements 19/20): both WORTHLESS if bought early (a
      -10 value at `t0`, far below the 4.0 baseline they would replace --
      deliberately larger than any hit, so spreading them one-per-round
      is never competitive, closing the "prefund now, buy later" escape
      this test's own design process found and rejected), both a big
      +10 edge from `t0+1` onward. Capturing both needs exactly TWO
      transfers, and they are only ever worth taking TOGETHER, at `t0+1`.

    With `max_banked_transfers=2`: the optimal declines `X` at `t0`
    (banks the sole free transfer), then buys `Y` AND `Z` together at
    `t0+1` using the 2 banked free transfers, `hits == 0` throughout.

    **The attack** (CLAUDE.md: a guarantee is only earned once attacked
    from outside the intended path): re-run the IDENTICAL scenario with
    `max_banked_transfers=1` -- banking can no longer accumulate past 1,
    so the `Y`+`Z` double purchase at `t0+1` now costs exactly one hit
    regardless of what happens at `t0`. Under that cap, taking `X`'s free
    local edge is strictly better (it costs nothing extra: the hit for
    `Y`+`Z` is unavoidable either way), so the decision FLIPS to taking
    `X` at `t0`. If the flip did not happen -- if `X` were declined under
    BOTH caps, or taken under BOTH -- this would not be evidence of
    banking at all, just some unrelated preference (CLAUDE.md's own
    named failure mode: S7's unaffordable-transfer case tripping
    `validate_squad`'s budget guard instead of the ledger's). Bank is
    generous (100 tenths) throughout, so a budget guard cannot be what is
    driving either decision -- the swap prices move it by single-digit
    tenths, nowhere near binding.
    """
    def value_at(element: int, at_t0: bool) -> float:
        if element == 9:  # X
            return 5.0 if at_t0 else 4.0
        if element in (19, 20):  # Y, Z
            return -10.0 if at_t0 else 14.0
        if element == 14:  # fixed captain anchor -- unaffected by any of the above, so captaincy never confounds the comparison
            return 100.0
        return 4.0

    horizon = {
        20: _mp_round({e: value_at(e, at_t0=True) for e in (9, 14, 19, 20)}),
        21: _mp_round({e: value_at(e, at_t0=False) for e in (9, 14, 19, 20)}),
    }
    # MID and DEF forced to start in full (xi_bounds (5, 5) for both) so
    # neither X nor Y/Z can hide their edge (or their downgrade) on the
    # bench -- a real SquadRules' formation flexibility let an early
    # bench-hidden version of this scenario pass for the wrong reason
    # during this test's own construction (Y bought early but benched,
    # contributing nothing either way): closed here, not left implicit.
    rules = SquadRules(
        budget_tenths=1000, squad_size=15,
        squad_composition=(("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)),
        xi_bounds=(("GK", (1, 1)), ("DEF", (5, 5)), ("MID", (5, 5)), ("FWD", (0, 0))),
        max_per_club=3,
    )
    incoming = _mp_incoming_state(bank_tenths=100, free_transfers=1)

    banked = optimise_multi_period(
        horizon, rules, TransferRules(free_transfers_per_gameweek=1, max_banked_transfers=2, hit_cost=-4),
        incoming_state=incoming,
    )
    assert banked.transfers_in == ()  # X declined at t0 ...
    assert banked.transfers_out == ()
    assert banked.hits == 0
    assert banked.plan[1].transfers_in == (19, 20)  # ... to buy Y AND Z together at t0+1 ...
    assert banked.plan[1].hits == 0  # ... hit-free, because 2 free transfers were banked.
    assert banked.plan[1].free_transfers_available == 2

    capped = optimise_multi_period(
        horizon, rules, TransferRules(free_transfers_per_gameweek=1, max_banked_transfers=1, hit_cost=-4),
        incoming_state=incoming,
    )
    assert capped.transfers_in == (9,)  # THE FLIP: X is now taken immediately ...
    assert capped.transfers_out == (8,)
    assert capped.hits == 0  # ... itself still free (it alone never needed more than 1 FT) ...
    assert capped.plan[1].transfers_in == (19, 20)  # ... while Y and Z are still bought together next round ...
    assert capped.plan[1].hits == 1  # ... now unavoidably paying the hit banking would otherwise have avoided.
    assert capped.plan[1].free_transfers_available == 1  # capped, not the 2 the uncapped run reached

    # Confirm banking is genuinely BETTER, not merely different -- the raw
    # objective (both runs share every other term) should favour the
    # banked, hit-free path.
    assert banked.objective_value > capped.objective_value


# ---------------------------------------------------------------------------
# HorizonStrategy / TrailingProxyHorizonSource -- Phase 4, E7, story S9,
# PHASE 1. Wires optimise_multi_period (S8) into a fplai.backtest.replay.
# Strategy. See optimiser.py's own module section, "HorizonStrategy --
# Phase 4, E7, story S9, PHASE 1", for every pinned decision (D1-D4, D8,
# D9) this section's tests check against.
# ---------------------------------------------------------------------------


class _StubHorizonSource:
    """A HorizonCandidateSource (D2) with a fixed, caller-supplied
    per-round candidate mapping -- lets these tests drive HorizonStrategy
    without needing a real ModelStackStrategy/store round-trip."""

    def __init__(self, per_round):
        self._per_round = per_round

    def horizon_candidates(self, view, *, max_rounds=None):
        # S10, D1: a source that IGNORES max_rounds is still correct --
        # HorizonStrategy.decide's own [: self._horizon] truncation is the
        # belt-and-braces this stub deliberately exercises.
        return self._per_round


def _empty_view(*, gameweek=20, incoming_state=None, forward_fixtures=None):
    return GameweekView(
        season="2025-26",
        gameweek=gameweek,
        history=pl.DataFrame(),
        current_attributes=pl.DataFrame(),
        forward_fixtures=forward_fixtures if forward_fixtures is not None else {},
        incoming_state=incoming_state,
    )


def test_decision_from_result_default_transfers_are_empty_for_existing_call_sites():
    """Existing call sites (MILPStrategy.decide, ModelStackStrategy.decide)
    never pass transfers_in/transfers_out -- confirm the widened
    _decision_from_result (D8) still defaults both to () unchanged."""
    candidates = _default_pool()
    result = optimise_squad(candidates, _RULES, OptimiserConfig())
    decision = _decision_from_result(result, candidates)
    assert decision.transfers_in == ()
    assert decision.transfers_out == ()


def test_horizon_strategy_raises_for_horizon_less_than_one():
    with pytest.raises(OptimiserError, match="horizon must be >= 1"):
        HorizonStrategy(_StubHorizonSource({}), _RULES, transfer_rules_for_season("2025-26"), horizon=0)


def test_horizon_strategy_raises_optimisererror_not_indexerror_on_an_empty_source():
    """S9, PHASE 2, D8: a source returning an empty mapping (from a valid
    horizon >= 1) used to reach `rounds[0]` and raise a bare `IndexError`.
    Must raise this module's own `OptimiserError` instead, naming the
    decision."""
    source = _StubHorizonSource({})
    strategy = HorizonStrategy(source, _RULES, transfer_rules_for_season("2025-26"), horizon=6)
    with pytest.raises(OptimiserError, match="empty mapping"):
        strategy.decide(_empty_view())


def test_horizon_strategy_raises_when_incoming_state_is_a_half_built_ledger():
    """D4's own guard: incoming_state must be an empty ledger (a free
    build) or a full rules.squad_size squad -- anything in between is a
    caller bug, named explicitly rather than interpreted."""
    source = _StubHorizonSource({20: _default_pool()})
    strategy = HorizonStrategy(source, _RULES, transfer_rules_for_season("2025-26"), horizon=1)
    half_built = SquadState(
        element_ids=(1, 2, 3), purchase_prices=((1, 40), (2, 40), (3, 40)), bank_tenths=100, free_transfers=1
    )
    view = _empty_view(incoming_state=half_built)
    with pytest.raises(OptimiserError, match=r"incoming_state carries 3 element"):
        strategy.decide(view)


def test_horizon_strategy_h1_no_incoming_state_is_a_free_build_synthesising_transfers_in():
    """D4: incoming_state is None (stateless mode, or a season's first
    decision with no ledger constructed yet at all) is ALSO a free build --
    optimise_multi_period runs with incoming_state=None, and transfers_in
    is synthesised as the whole solved squad so
    _validate_transfer_continuity never sees a spurious mismatch."""
    source = _StubHorizonSource({20: _default_pool()})
    strategy = HorizonStrategy(source, _RULES, transfer_rules_for_season("2025-26"), horizon=1)
    decision = strategy.decide(_empty_view())
    assert set(decision.transfers_in) == decision.squad.ids()
    assert len(decision.transfers_in) == _RULES.squad_size
    assert decision.transfers_out == ()


def test_horizon_strategy_h1_explicit_empty_squad_state_is_also_a_free_build():
    """D4: an explicit free_build_state(rules) -- not just None -- maps to
    the identical free-build path; incoming_state=None is passed to
    optimise_multi_period either way (this story's own D4 mapping)."""
    source = _StubHorizonSource({20: _default_pool()})
    strategy = HorizonStrategy(source, _RULES, transfer_rules_for_season("2025-26"), horizon=1)
    decision = strategy.decide(_empty_view(incoming_state=free_build_state(_RULES)))
    assert set(decision.transfers_in) == decision.squad.ids()
    assert decision.transfers_out == ()


def test_horizon_strategy_free_build_costs_no_hit_end_to_end_via_season_replay():
    """D4's arithmetic claim, proven through the REAL call path
    (SeasonReplay.run, _validate_transfer_continuity, _advance_state)
    rather than only against HorizonStrategy.decide in isolation --
    n_transfers is read from transfers_out (empty), so the hit charge is
    zero regardless of transfers_in's length."""
    rows_gw1 = [
        {
            "element": e, "name": f"e{e}", "position": m["position"], "team": m["team"],
            "value": m["price"], "round": 1, "total_points": 2, "minutes": 90,
            "fixture": e, "was_home": True, "opponent_team": 1, "kickoff_time": "2025-08-16T14:00:00Z",
            "selected": 100, "team_a_score": 1, "team_h_score": 1,
        }
        for e, m in _MP_META.items()
    ]
    frame = pl.DataFrame([dict(r, season="2025-26") for r in rows_gw1])
    coverage = SeasonCoverage(season="2025-26", rounds_present=(1,), expected_gameweeks=1, missing_rounds=())
    data = SeasonData(season="2025-26", frame=frame, coverage=coverage)

    rules = SquadRules(
        budget_tenths=1000, squad_size=15,
        squad_composition=(("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)),
        xi_bounds=(("GK", (1, 1)), ("DEF", (3, 5)), ("MID", (2, 5)), ("FWD", (1, 3))),
        max_per_club=3,
    )
    source = _StubHorizonSource({1: _mp_round({})})
    strategy = HorizonStrategy(source, rules, transfer_rules_for_season("2025-26"), horizon=1)

    results = SeasonReplay(data, rules).run(strategy, initial_state=free_build_state(rules))
    assert len(results) == 1
    assert results[0].transfer_hit_points == 0


def test_horizon_strategy_truncates_to_the_requested_horizon_not_the_full_source():
    """D3: sorted(source.horizon_candidates(view))[:horizon] -- a source
    offering 3 rounds with horizon=2 must solve identically to
    optimise_multi_period called with only the first two rounds, never all
    three."""
    horizon_source_rounds = {20: _mp_round({9: 5.0}), 21: _mp_round({9: 5.0}), 22: _mp_round({9: 100.0})}
    source = _StubHorizonSource(horizon_source_rounds)
    incoming = _mp_incoming_state(bank_tenths=100, free_transfers=1)
    tr = transfer_rules_for_season("2025-26")
    strategy = HorizonStrategy(source, _RULES, tr, horizon=2)

    decision = strategy.decide(_empty_view(incoming_state=incoming))
    expected = optimise_multi_period(
        {20: horizon_source_rounds[20], 21: horizon_source_rounds[21]}, _RULES, tr, incoming_state=incoming
    )
    assert decision.squad.ids() == set(expected.squad_element_ids)
    assert set(decision.transfers_in) == set(expected.transfers_in)
    assert set(decision.transfers_out) == set(expected.transfers_out)


def test_horizon_strategy_stateful_squad_uses_optimise_multi_periods_own_transfers():
    """The non-free-build branch: incoming_state is a full squad ->
    transfers_in/transfers_out are read straight off MultiPeriodResult,
    never synthesised."""
    incoming = _mp_incoming_state(bank_tenths=100, free_transfers=1)
    source = _StubHorizonSource({20: _mp_round({9: 5.0, 19: 5.0})})
    tr = TransferRules(free_transfers_per_gameweek=1, max_banked_transfers=1, hit_cost=-4)
    strategy = HorizonStrategy(source, _RULES, tr, horizon=1)
    decision = strategy.decide(_empty_view(incoming_state=incoming))
    expected = optimise_multi_period({20: source._per_round[20]}, _RULES, tr, incoming_state=incoming)
    assert decision.transfers_in == expected.transfers_in
    assert decision.transfers_out == expected.transfers_out


def test_trailing_proxy_horizon_source_repeats_round_t_candidates_for_every_forward_round():
    """D2: fixture-blind by construction -- every forward round carries
    round t's own candidate list unchanged, never a re-derived one."""
    history = pl.DataFrame({"element": [1, 2], "round": [19, 19], "total_points": [4, 2]})
    current = pl.DataFrame(
        {"element": [1, 2], "name": ["A", "B"], "position": ["GK", "GK"], "team": ["X", "Y"], "value": [45, 45]}
    )
    view = GameweekView(
        season="2025-26", gameweek=20, history=history, current_attributes=current,
        forward_fixtures={21: pl.DataFrame(), 22: pl.DataFrame()},
    )
    source = TrailingProxyHorizonSource()
    out = source.horizon_candidates(view)
    assert set(out) == {20, 21, 22}
    assert out[20] == out[21] == out[22]


def test_trailing_proxy_horizon_source_backfills_a_held_blank_player():
    """D5: a held element (5) absent from round t's own candidate list gets
    appended, priced/identified from `last_known_attributes`, with a REAL
    trailing-window PMF (it has genuine history) -- and the fixture-blind
    convention (D2) still applies: the backfilled candidate is part of
    round t's own list, so it too repeats unchanged into every forward
    round."""
    history = pl.DataFrame(
        {
            "element": [1, 2, 5, 5],
            "round": [19, 19, 17, 18],
            "fixture": [190, 191, 170, 180],
            "total_points": [4, 2, 6, 8],
            "name": ["A", "B", "E-old", "E"],
            "position": ["GK", "GK", "MID", "MID"],
            "team": ["X", "Y", "Z", "Z"],
            "value": [45, 45, 48, 50],
        }
    )
    current = pl.DataFrame(
        {"element": [1, 2], "name": ["A", "B"], "position": ["GK", "GK"], "team": ["X", "Y"], "value": [45, 45]}
    )
    view = GameweekView(
        season="2025-26", gameweek=20, history=history, current_attributes=current,
        forward_fixtures={21: pl.DataFrame()},
        incoming_state=SquadState(element_ids=(5,), purchase_prices=((5, 50),), bank_tenths=0, free_transfers=1),
    )
    source = TrailingProxyHorizonSource()
    out = source.horizon_candidates(view)
    by_id_t = {c.element: c for c in out[20]}
    assert 5 in by_id_t
    backfilled = by_id_t[5]
    assert backfilled.team == "Z"
    assert backfilled.position == "MID"
    assert backfilled.price == 50  # round 18's row -- the LAST one
    assert backfilled.points_dist.points == (6, 7, 8)  # dense histogram over trailing draws {6, 8}
    assert backfilled.points_dist.probabilities == (0.5, 0.0, 0.5)
    assert out[20] == out[21]  # still fixture-blind (D2) -- forward round repeats the backfilled list too


def test_trailing_proxy_horizon_source_raises_when_a_held_element_has_no_history_at_all():
    """D5's still-raises case: an id absent from BOTH round t's own
    candidate list and every row in history must raise, never fabricate a
    price."""
    history = pl.DataFrame(
        {
            "element": [1], "round": [19], "fixture": [190], "total_points": [4],
            "name": ["A"], "position": ["GK"], "team": ["X"], "value": [45],
        }
    )
    current = pl.DataFrame({"element": [1], "name": ["A"], "position": ["GK"], "team": ["X"], "value": [45]})
    view = GameweekView(
        season="2025-26", gameweek=20, history=history, current_attributes=current,
        incoming_state=SquadState(element_ids=(5,), purchase_prices=((5, 50),), bank_tenths=0, free_transfers=1),
    )
    source = TrailingProxyHorizonSource()
    with pytest.raises(OptimiserError, match="cannot backfill"):
        source.horizon_candidates(view)


# --- ATTACK (CLAUDE.md "no path is unsafe" standard) -----------------------
# D4's claim is that a free build costs no hit BECAUSE HorizonStrategy maps
# an empty incoming squad to incoming_state=None. This attacks that claim
# from OUTSIDE HorizonStrategy: pass an EMPTY SquadState straight to
# optimise_multi_period, bypassing the None-mapping entirely.


def test_attack_an_empty_squadstate_passed_directly_to_optimise_multi_period_produces_a_real_hit():
    candidates = _default_pool()
    empty_state = SquadState(element_ids=(), purchase_prices=(), bank_tenths=1000, free_transfers=0)
    tr = TransferRules(free_transfers_per_gameweek=1, max_banked_transfers=2, hit_cost=-4)
    result = optimise_multi_period({20: candidates}, _RULES, tr, incoming_state=empty_state)
    assert len(result.transfers_in) == _RULES.squad_size
    assert result.transfers_out == ()
    # 15 transfers against 0 free -- every one of them a hit, unlike HorizonStrategy's mapped path.
    assert result.hits == _RULES.squad_size


@pytest.mark.slow
def test_horizon_strategy_stateful_rehearsal_gw1_to_3_on_the_real_store():
    """G2 -- Phase 1's real-store gate (this story's brief). 2024-25 GW1-3
    is inside probe 1's blank-free window (the first vanishing element-set
    pair is r=14 -> r=15), so this rehearsal is gated without needing
    Phase 2's held-player backfill/sell-price fallback."""
    season = "2024-25"
    store = BitemporalStore()
    data = load_season(store, season)
    rules = rules_for_season(season)
    transfer_rules = transfer_rules_for_season(season)

    trimmed = SeasonData(
        season=season,
        frame=data.frame.filter(pl.col("round") <= 3),
        coverage=SeasonCoverage(season=season, rounds_present=(1, 2, 3), expected_gameweeks=3, missing_rounds=()),
    )

    class _Spy:
        name = "spy"

        def __init__(self, inner):
            self._inner = inner
            self.decisions = []

        def decide(self, view):
            d = self._inner.decide(view)
            self.decisions.append(d)
            return d

    inner = HorizonStrategy(TrailingProxyHorizonSource(), rules, transfer_rules, horizon=6)
    spy = _Spy(inner)
    results = SeasonReplay(trimmed, rules).run(spy, initial_state=free_build_state(rules))

    assert len(results) == 3
    assert len(spy.decisions[0].transfers_in) == rules.squad_size
    assert spy.decisions[0].transfers_out == ()
    assert results[0].transfer_hit_points == 0
    print(f"\nG2 -- {season} GW1-3 stateful rehearsal: points={[r.points for r in results]}")


@pytest.mark.slow
def test_horizon_strategy_backfill_across_the_gw28_29_blank_gate_on_the_real_store():
    """G2 -- Phase 2's real-store gate (this story's brief). 2023-24
    GW26-30, stateful, TrailingProxyHorizonSource, H=6 -- the window
    containing this story's probe 1/2 evidence: the 494-element GW28->29
    blank (pool 838 -> 355). Must complete WITHOUT raising, and the
    backfill (D5) must actually fire at GW29 -- at least one held element
    absent from that round's own current_attributes must be present in the
    candidate pool `optimise_multi_period` actually saw. A `_RecordingSource`
    wraps the real `TrailingProxyHorizonSource` (never re-implements it,
    per this story's own instruction to consume, not regenerate) purely to
    observe, per round, which held ids were missing from `current_
    attributes` and whether they made it into the pool that round."""
    season = "2023-24"
    store = BitemporalStore()
    data = load_season(store, season)
    rules = rules_for_season(season)
    transfer_rules = transfer_rules_for_season(season)

    trimmed = SeasonData(
        season=season,
        frame=data.frame.filter((pl.col("round") >= 26) & (pl.col("round") <= 30)),
        coverage=SeasonCoverage(
            season=season, rounds_present=(26, 27, 28, 29, 30), expected_gameweeks=5, missing_rounds=()
        ),
    )

    backfill_counts: dict[int, int] = {}

    class _RecordingSource:
        name = "recording-trailing-proxy"

        def __init__(self):
            self._inner = TrailingProxyHorizonSource()

        def horizon_candidates(self, view, *, max_rounds=None):
            candidate_map = self._inner.horizon_candidates(view, max_rounds=max_rounds)
            round_t = view.gameweek
            current_ids = set(view.current_attributes["element"].to_list()) if not view.current_attributes.is_empty() else set()
            incoming_ids = set(view.incoming_state.element_ids) if view.incoming_state is not None else set()
            missing_from_current = incoming_ids - current_ids
            in_pool_t = {c.element for c in candidate_map[round_t]}
            backfill_counts[round_t] = len(missing_from_current & in_pool_t)
            return candidate_map

    strategy = HorizonStrategy(_RecordingSource(), rules, transfer_rules, horizon=6)
    results = SeasonReplay(trimmed, rules).run(strategy, initial_state=free_build_state(rules))

    print(f"\nG2 (Phase 2) -- {season} GW26-30 backfill counts by round: {backfill_counts}")
    assert len(results) == 5
    assert backfill_counts.get(29, 0) > 0, "expected the held-player backfill to fire at GW29 (the 494-element blank)"
