"""The three Phase 1 baselines — blueprint §7.2.

All three build candidates from `view.current_attributes` only (never an
outcome column for the gameweek being decided — see `replay.py`'s
docstring) and rank them by a strategy-specific `value` computed from
`view.history` only (rounds strictly before the one being decided).
`squad.build_squad`/`choose_xi_bench_captain` do the rest, identically for
all three — the only thing that differs between baselines is the value
function.

**Random** is the only strictly buy-and-hold baseline (matches the task
brief's explicit wording: "seeded valid squad at GW1, buy-and-hold").
**Template** and **Greedy-form** are re-decided every gameweek — the brief
describes both in terms of a per-gameweek rule ("using the *previous*
gameweek's ownership"; "highest trailing-N-gameweek points"), and a
re-decided-each-week baseline needs no transfer/budget-continuity machinery
that would anticipate Phase 3's optimiser. This is a design choice, stated
plainly because the brief did not spell out buy-and-hold vs. weekly
re-selection for these two — see docs/wiki/phase1-baselines.md for the full
reasoning.

**Both Template and Greedy-form have a genuine cold-start gameweek.**
Template's rule needs a *previous* gameweek's ownership, which does not
exist for the season's first available gameweek — the leakage rule (a
gameweek's own `selected` is not knowable at its own deadline), applied
strictly, means Template has *no* legitimate signal for gameweek 1.
Greedy-form's trailing window is empty for the same reason. Rather than
silently faking a signal or crashing the whole backtest, both fall back to
`value = 0.0` for every candidate for that gameweek only — squad selection
degenerates to a deterministic id-order tiebreak, and this is a real,
labelled degenerate case, not a modelled decision. Reported explicitly, not
hidden — see the module-level `COLD_START_GAMEWEEKS` note below and the
results report.
"""

from __future__ import annotations

import random

import polars as pl

from fplai.backtest.replay import Decision, GameweekView
from fplai.backtest.rules import GREEDY_FORM_TRAILING_GAMEWEEKS, SquadRules
from fplai.backtest.squad import PlayerCandidate, build_squad, choose_xi_bench_captain


def _candidates_from_attributes(
    current_attributes: pl.DataFrame,
    values: dict[int, float],
) -> list[PlayerCandidate]:
    candidates = []
    for row in current_attributes.iter_rows(named=True):
        element_id = int(row["element"])
        candidates.append(
            PlayerCandidate(
                id=element_id,
                name=row["name"],
                position=row["position"],
                team=row["team"],
                price=int(row["value"]),
                value=values.get(element_id, 0.0),
            )
        )
    return candidates


def _decide_from_values(
    view: GameweekView,
    values: dict[int, float],
    rules: SquadRules,
) -> Decision:
    candidates = _candidates_from_attributes(view.current_attributes, values)
    squad = build_squad(candidates, rules)
    xi, bench, captain, vice_captain = choose_xi_bench_captain(squad, rules)
    return Decision(squad=squad, xi=xi, bench=bench, captain=captain, vice_captain=vice_captain)


class RandomBaseline:
    """Seeded valid squad at GW1 (or the season's first available
    gameweek), buy-and-hold all season. XI/captain/bench order are also
    fixed at that first decision, by the same seeded random ranking — "a
    fixed pre-deadline rule" that never looks at any subsequent gameweek.
    Reproducible from `seed` alone (CLAUDE.md rule 7)."""

    def __init__(self, seed: int, rules: SquadRules):
        self.name = f"random(seed={seed})"
        self._rng = random.Random(seed)
        self._rules = rules
        self._decision: Decision | None = None

    def decide(self, view: GameweekView) -> Decision:
        if self._decision is not None:
            return self._decision
        element_ids = [int(v) for v in view.current_attributes["element"].to_list()]
        # Deterministic given `seed`: shuffle a sorted id list (never rely
        # on the store's row order, which is not itself guaranteed stable).
        element_ids_sorted = sorted(element_ids)
        values = {eid: self._rng.random() for eid in element_ids_sorted}
        self._decision = _decide_from_values(view, values, self._rules)
        return self._decision


class TemplateBaseline:
    """Most-owned valid squad, re-decided every gameweek from the
    *previous* gameweek's raw ownership count (`selected`). See module
    docstring for the GW1 cold-start case."""

    name = "template"

    def __init__(self, rules: SquadRules):
        self._rules = rules

    def decide(self, view: GameweekView) -> Decision:
        previous_round = view.gameweek - 1
        prior = view.history.filter(pl.col("round") == previous_round)
        if prior.is_empty():
            values: dict[int, float] = {}
        else:
            deduped = prior.unique(subset=["element"], keep="first")
            values = dict(zip(deduped["element"].to_list(), deduped["selected"].to_list()))
        return _decide_from_values(view, values, self._rules)


class GreedyFormBaseline:
    """Highest trailing-`N`-gameweek total points, re-decided every
    gameweek. `N = GREEDY_FORM_TRAILING_GAMEWEEKS` (documented assumption,
    see rules.py). See module docstring for the early-season cold-start
    case (an empty or partial trailing window)."""

    name = f"greedy_form(n={GREEDY_FORM_TRAILING_GAMEWEEKS})"

    def __init__(self, rules: SquadRules, trailing_gameweeks: int = GREEDY_FORM_TRAILING_GAMEWEEKS):
        self._rules = rules
        self._n = trailing_gameweeks

    def decide(self, view: GameweekView) -> Decision:
        window_start = view.gameweek - self._n
        window = view.history.filter(pl.col("round") >= window_start)
        if window.is_empty():
            values: dict[int, float] = {}
        else:
            totals = window.group_by("element").agg(pl.col("total_points").sum().alias("total_points"))
            values = dict(zip(totals["element"].to_list(), totals["total_points"].to_list()))
        return _decide_from_values(view, values, self._rules)
