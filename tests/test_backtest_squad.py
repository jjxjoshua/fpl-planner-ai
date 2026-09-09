"""Tests for fplai.backtest.squad — constraint validation and the
greedy-plus-local-search squad builder."""

from __future__ import annotations

import pytest

from fplai.backtest.rules import SquadRules
from fplai.backtest.squad import (
    PlayerCandidate,
    Squad,
    SquadError,
    build_squad,
    choose_xi_bench_captain,
    validate_squad,
)

RULES = SquadRules(
    budget_tenths=1000,
    squad_size=15,
    squad_composition=(("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)),
    xi_bounds=(("GK", (1, 1)), ("DEF", (3, 5)), ("MID", (2, 5)), ("FWD", (1, 3))),
    max_per_club=3,
)


def _make_pool(n_per_position: int = 6, n_clubs: int = 7, base_price: int = 45) -> list[PlayerCandidate]:
    """A generous synthetic pool: several clubs, several players per
    position, deterministic ascending value so build_squad's local search
    has real swap decisions to make (not a degenerate "only one legal
    squad" case)."""
    candidates = []
    pid = 0
    for position in ("GK", "DEF", "MID", "FWD"):
        for i in range(n_per_position):
            pid += 1
            candidates.append(
                PlayerCandidate(
                    id=pid,
                    name=f"{position}{i}",
                    position=position,
                    team=f"Club{i % n_clubs}",
                    price=base_price + (i % 5) * 5,
                    value=float(i),  # higher i => higher value => preferred by hill-climb
                )
            )
    return candidates


def test_build_squad_respects_composition_budget_and_club_cap():
    pool = _make_pool()
    squad = build_squad(pool, RULES)
    validate_squad(squad, RULES)  # must not raise
    assert len(squad.players) == 15
    assert len(squad.by_position("GK")) == 2
    assert len(squad.by_position("DEF")) == 5
    assert len(squad.by_position("MID")) == 5
    assert len(squad.by_position("FWD")) == 3
    assert squad.total_price() <= RULES.budget_tenths


def test_build_squad_prefers_higher_value_within_budget():
    pool = _make_pool()
    squad = build_squad(pool, RULES)
    # The highest-value MID (i=5) should have been swapped in over the
    # cheapest-first skeleton's low-value picks, since budget easily allows it.
    mid_ids = {p.id for p in squad.by_position("MID")}
    highest_value_mid = max((p for p in pool if p.position == "MID"), key=lambda c: c.value)
    assert highest_value_mid.id in mid_ids


def test_build_squad_raises_when_not_enough_candidates():
    pool = [
        PlayerCandidate(id=1, name="only gk", position="GK", team="A", price=45, value=1.0)
    ]
    with pytest.raises(SquadError):
        build_squad(pool, RULES)


def test_build_squad_raises_when_cheapest_skeleton_over_budget():
    pool = _make_pool(base_price=200)  # every player unaffordable
    with pytest.raises(SquadError):
        build_squad(pool, RULES)


def test_build_squad_raises_when_candidates_have_duplicate_ids():
    pool = _make_pool()
    # Inject a duplicate id into the middle of the pool
    dup_candidate = pool[0]  # copy the first candidate
    pool.append(dup_candidate)  # add it again with the same id
    with pytest.raises(SquadError, match="candidates contain duplicate ids: 1"):
        build_squad(pool, RULES)


def test_build_squad_raises_when_multiple_duplicate_ids():
    pool = _make_pool()
    # Inject multiple duplicates
    pool.append(PlayerCandidate(id=pool[0].id, name="dup1", position="GK", team="X", price=45, value=1.0))
    pool.append(PlayerCandidate(id=pool[1].id, name="dup2", position="DEF", team="Y", price=45, value=1.0))
    with pytest.raises(SquadError, match="candidates contain duplicate ids"):
        build_squad(pool, RULES)


def test_validate_squad_catches_wrong_size():
    pool = _make_pool()
    squad = build_squad(pool, RULES)
    truncated = Squad(players=squad.players[:14])
    with pytest.raises(SquadError):
        validate_squad(truncated, RULES)


def test_validate_squad_catches_club_cap_violation():
    same_club = tuple(
        PlayerCandidate(id=i, name=f"p{i}", position=pos, team="OneClub", price=45, value=0.0)
        for i, pos in enumerate(
            ["GK", "GK", "DEF", "DEF", "DEF", "DEF", "DEF", "MID", "MID", "MID", "MID", "MID", "FWD", "FWD", "FWD"]
        )
    )
    squad = Squad(players=same_club)
    with pytest.raises(SquadError):
        validate_squad(squad, RULES)


def test_choose_xi_respects_formation_bounds():
    pool = _make_pool()
    squad = build_squad(pool, RULES)
    xi, bench, captain, vice = choose_xi_bench_captain(squad, RULES)
    assert len(xi) == 11
    assert len(bench) == 4
    positions = [p.position for p in xi]
    assert positions.count("GK") == 1
    assert 3 <= positions.count("DEF") <= 5
    assert 2 <= positions.count("MID") <= 5
    assert 1 <= positions.count("FWD") <= 3
    assert captain in xi
    assert vice in xi
    assert captain.id != vice.id


def test_captain_is_highest_value_in_xi():
    pool = _make_pool()
    squad = build_squad(pool, RULES)
    xi, _bench, captain, _vice = choose_xi_bench_captain(squad, RULES)
    assert captain.value == max(p.value for p in xi)


# -- determinism (blueprint §7.2 gate-repair session, s003; CLAUDE.md rule 7) --
#
# scripts/run_baselines.py --seed 42 produced 1907/1909/1887 across three
# identical runs. Root cause traced to THIS module's hill-climb: the
# feasibility skeleton (phase 1) already sorts on (price, id) and is
# order-invariant, but phase 2's swap search picked the FIRST candidate in
# `by_position[position]`'s raw, un-sorted order whose gain strictly beat
# the running best -- when two or more candidates tie on `value` (routine
# for greedy_form, where many players share a trailing-window total of 0),
# the winner was whichever one the CALLER happened to list first. The
# caller's list order, in turn, came from `current_attributes.iter_rows()`
# (fplai.backtest.replay._build_view), fed by a polars `.unique(...)` call
# with `maintain_order=False` (its default) -- verified live against
# data/store/: five repeated `.unique()` calls on the same 692-row real
# gameweek-1 slice returned five COMPLETELY different row orders, not a
# rare edge case. Fixing replay.py alone would still leave squad.py's own
# swap-selection order-dependent by construction (the brief: "I want the
# baseline reproducible regardless of row order") -- so build_squad's
# swap search now picks a globally deterministic winner (highest gain;
# ties broken by lowest incoming id, then lowest outgoing id) instead of
# "first found", independent of both the outer candidate-list order AND
# `selected`'s dict-iteration order.


def _tied_swap_candidates(def_order: list[int]) -> list[PlayerCandidate]:
    """One forced GK, one forced FWD, and a DEF slot with one cheap
    skeleton pick (id=10, value=0) plus three candidates (ids 20/21/22,
    caller-supplied order) that ALL tie on value -- the exact shape that
    exposes an order-dependent "first found" tie-break."""
    candidates = [
        PlayerCandidate(id=1, name="gk", position="GK", team="A", price=40, value=0.0),
        PlayerCandidate(id=2, name="fwd", position="FWD", team="B", price=40, value=0.0),
        PlayerCandidate(id=10, name="def-skeleton", position="DEF", team="C", price=40, value=0.0),
    ]
    for def_id in def_order:
        candidates.append(
            PlayerCandidate(id=def_id, name=f"def-{def_id}", position="DEF", team=f"T{def_id}", price=40, value=100.0)
        )
    return candidates


_TIED_RULES = SquadRules(
    budget_tenths=1000,
    squad_size=3,
    squad_composition=(("GK", 1), ("DEF", 1), ("FWD", 1)),
    xi_bounds=(("GK", (1, 1)), ("DEF", (1, 1)), ("FWD", (1, 1))),
    max_per_club=99,
)


def test_build_squad_hill_climb_tie_break_is_independent_of_candidate_list_order():
    order_a = _tied_swap_candidates([20, 21, 22])
    order_b = _tied_swap_candidates([22, 21, 20])
    winner_a = build_squad(order_a, _TIED_RULES).by_position("DEF")[0].id
    winner_b = build_squad(order_b, _TIED_RULES).by_position("DEF")[0].id
    assert winner_a == winner_b
