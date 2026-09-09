"""Squad representation, constraint validation, and the squad-selection
heuristic shared by all three Phase 1 baselines.

**No solver dependency.** Blueprint §9/§6 puts HiGHS in the optimiser
(Phase 3); this task's constraint list explicitly allows only
`requests`/`polars`/`duckdb`/`tzdata`. `build_squad` is therefore a
deterministic greedy-plus-local-search heuristic, not a certified-optimal
ILP solve. That is the correct level of rigour for a *baseline* — Random is
definitionally not optimal, Template approximates community wisdom (not a
knapsack solve real managers perform either), and Greedy-form's own name
says "greedy." Phase 3's MILP is what "beat the template" is measured
against; these are the floor, not a preview of the optimiser.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from fplai.backtest.rules import SquadRules

_TIE_BREAK_EPS = 1e-9


class SquadError(ValueError):
    """Raised when a valid squad cannot be built from the candidate pool
    under the given rules — never silently return an invalid or partial
    squad."""


@dataclass(frozen=True)
class PlayerCandidate:
    """One selectable player at one decision point. `value` is whatever
    scalar a specific baseline ranks candidates by (ownership count,
    trailing points, price, or a seeded random draw) — it is a ranking key
    for squad SELECTION, not a modelled quantity, and is never itself used
    as a score (blueprint §7.2: scoring sums stored `total_points` only)."""

    id: int
    name: str
    position: str
    team: str
    price: int  # FPL's x10 convention, e.g. 55 == £5.5m
    value: float


@dataclass(frozen=True)
class Squad:
    players: tuple[PlayerCandidate, ...]

    def total_price(self) -> int:
        return sum(p.price for p in self.players)

    def by_position(self, position: str) -> list[PlayerCandidate]:
        return [p for p in self.players if p.position == position]

    def ids(self) -> set[int]:
        return {p.id for p in self.players}


def validate_squad(squad: Squad, rules: SquadRules, *, check_budget: bool = True) -> None:
    """Raise SquadError naming exactly what's wrong, rather than let an
    invalid squad silently enter a backtest.

    `check_budget` (S9, PHASE 2, D7) defaults to `True`, so every existing
    call site (`build_squad`, every pre-S9 test) behaves exactly as before.
    `total_price() > rules.budget_tenths` is a STATELESS-mode invariant — a
    fresh `rules.budget_tenths` build every gameweek; it does not hold for
    a squad genuinely HELD across a stateful season, which can legitimately
    appreciate past the nominal budget as prices drift (see `fplai.
    backtest.replay.score_gameweek`'s own docstring for the measured
    figures and for why overspend is still impossible in that mode by two
    OTHER mechanisms). Every other check below — size, duplicate ids,
    composition, club cap — always runs, regardless of this flag."""
    if len(squad.players) != rules.squad_size:
        raise SquadError(f"squad has {len(squad.players)} players, need {rules.squad_size}")
    if len(squad.ids()) != len(squad.players):
        raise SquadError("squad contains duplicate player ids")
    if check_budget and squad.total_price() > rules.budget_tenths:
        raise SquadError(
            f"squad costs {squad.total_price() / 10:.1f}m, over budget "
            f"{rules.budget_tenths / 10:.1f}m"
        )
    comp = rules.squad_composition_dict
    for position, count in comp.items():
        actual = len(squad.by_position(position))
        if actual != count:
            raise SquadError(f"squad has {actual} {position}, needs exactly {count}")
    club_counts = Counter(p.team for p in squad.players)
    over = {team: n for team, n in club_counts.items() if n > rules.max_per_club}
    if over:
        raise SquadError(f"club cap ({rules.max_per_club}) exceeded: {over}")


def build_squad(
    candidates: list[PlayerCandidate],
    rules: SquadRules,
    *,
    max_iterations: int = 300,
) -> Squad:
    """Build a valid, budget-respecting squad that greedily maximises total
    `value` subject to formation, budget, and club-cap constraints.

    Two phases, both deterministic given the candidate list's `value`/`id`
    ordering (so the caller controls "randomness" entirely by what `value`
    it assigns each candidate — see `baselines.py`'s RandomBaseline, which
    assigns a seeded-RNG value rather than a football-relevant one):

    1. **Feasibility skeleton** — cheapest-first fill per position,
       respecting the club cap. Guarantees a budget-feasible starting point
       (raises `SquadError` if even the cheapest legal skeleton is over
       budget — a genuine, reportable failure, not something to paper over).
    2. **Value hill-climb** — repeatedly apply the single best-improving
       swap (highest `value` gain, respecting budget and club cap) until no
       improving swap exists or `max_iterations` is hit. Not a certified
       optimum; see module docstring.

       **Tie-break is explicit and stable, independent of candidate-list
       order (blueprint §7.2 gate-repair session, s003).** `scripts/
       run_baselines.py --seed 42` produced 1907/1909/1887 across three
       identical runs; before this fix, a tie among several candidates'
       `gain` was resolved by "whichever the caller happened to list
       first" — the caller's list order in turn traced back to a polars
       `.unique(maintain_order=False)` call in `replay.py` with genuinely
       random output order on every call (verified live). Fixed at BOTH
       ends (replay.py now sorts its output; this method no longer needs
       an ordered input to begin with) per the brief: "I want the baseline
       reproducible regardless of row order" — resting correctness on
       upstream ordering is the same fragility that produced the bug.
       Proven fail-first: `tests/test_backtest_squad.py::
       test_build_squad_hill_climb_tie_break_is_independent_of_candidate_
       list_order` feeds the identical tied candidates in two different
       list orders and asserts the same winner.
    """
    # Validate candidate-id uniqueness upfront before any position bucketing,
    # so the error points at the input rather than at a downstream symptom
    # (e.g., "could not fill POSITION" or "squad has 14 players, need 15").
    seen_ids: set[int] = set()
    duplicates: set[int] = set()
    for c in candidates:
        if c.id in seen_ids:
            duplicates.add(c.id)
        seen_ids.add(c.id)
    if duplicates:
        dup_str = ", ".join(str(d) for d in sorted(duplicates))
        raise SquadError(f"candidates contain duplicate ids: {dup_str}")

    by_position: dict[str, list[PlayerCandidate]] = {}
    for c in candidates:
        by_position.setdefault(c.position, []).append(c)

    composition = rules.squad_composition_dict
    for position, count in composition.items():
        available = len(by_position.get(position, []))
        if available < count:
            raise SquadError(
                f"not enough {position} candidates: need {count}, have {available}"
            )

    selected: dict[int, PlayerCandidate] = {}
    club_count: Counter[str] = Counter()

    for position, count in composition.items():
        pool = sorted(by_position[position], key=lambda c: (c.price, c.id))
        chosen = 0
        for c in pool:
            if chosen == count:
                break
            if club_count[c.team] >= rules.max_per_club:
                continue
            selected[c.id] = c
            club_count[c.team] += 1
            chosen += 1
        if chosen < count:
            raise SquadError(
                f"could not fill {position} ({chosen}/{count}) within the "
                f"{rules.max_per_club}-per-club cap even at minimum cost"
            )

    total_price = sum(c.price for c in selected.values())
    if total_price > rules.budget_tenths:
        raise SquadError(
            f"cheapest possible valid squad costs {total_price / 10:.1f}m, over budget "
            f"{rules.budget_tenths / 10:.1f}m for this season's prices"
        )

    selected_ids = set(selected)
    for _ in range(max_iterations):
        # Deterministic total order over every legal swap this iteration:
        # (gain, -in_c.id, -out_c.id) so the winner is a pure function of
        # the swap's own content, never of `selected`'s or `by_position`'s
        # iteration order. `max()` over the whole candidate set (not
        # "first found strictly better") is what makes this invariant to
        # input order — see build_squad's docstring, "Tie-break is
        # explicit and stable".
        best_swap: tuple[PlayerCandidate, PlayerCandidate] | None = None
        best_key: tuple[float, int, int] | None = None
        remaining_budget = rules.budget_tenths - total_price
        for out_c in selected.values():
            pool = by_position[out_c.position]
            for in_c in pool:
                if in_c.id in selected_ids:
                    continue
                price_delta = in_c.price - out_c.price
                if price_delta > remaining_budget:
                    continue
                if in_c.team != out_c.team and club_count[in_c.team] >= rules.max_per_club:
                    continue
                gain = in_c.value - out_c.value
                if gain <= _TIE_BREAK_EPS:
                    continue
                key = (gain, -in_c.id, -out_c.id)
                if best_key is None or key > best_key:
                    best_key = key
                    best_swap = (out_c, in_c)
        if best_swap is None:
            break
        out_c, in_c = best_swap
        del selected[out_c.id]
        selected_ids.discard(out_c.id)
        club_count[out_c.team] -= 1
        selected[in_c.id] = in_c
        selected_ids.add(in_c.id)
        club_count[in_c.team] += 1
        total_price += in_c.price - out_c.price

    squad = Squad(players=tuple(selected.values()))
    validate_squad(squad, rules)
    return squad


def choose_xi_bench_captain(
    squad: Squad,
    rules: SquadRules,
) -> tuple[tuple[PlayerCandidate, ...], tuple[PlayerCandidate, ...], PlayerCandidate, PlayerCandidate]:
    """Pick a valid starting XI (by `value`, respecting formation bounds),
    a bench order (the rest, highest `value` first == first sub priority),
    a captain (highest-value XI player), and a vice-captain (second
    highest) — a fixed, deterministic, pre-deadline rule using only each
    player's `value`, never anything from the gameweek being decided for.

    Formation: fill each position's minimum first (so a legal formation is
    always reachable), then fill remaining slots up to 11 by value,
    respecting each position's maximum.
    """
    xi_bounds = rules.xi_bounds_dict
    by_position: dict[str, list[PlayerCandidate]] = {}
    for p in squad.players:
        by_position.setdefault(p.position, []).append(p)
    for position in by_position:
        by_position[position].sort(key=lambda c: (-c.value, c.id))

    xi_ids: set[int] = set()
    counts: dict[str, int] = {pos: 0 for pos in xi_bounds}

    # Fill minimums first, by value, so the required floor is always met.
    for position, (min_n, _max_n) in xi_bounds.items():
        for c in by_position.get(position, []):
            if counts[position] >= min_n:
                break
            xi_ids.add(c.id)
            counts[position] += 1

    # Fill remaining slots up to 11, best value first, respecting maxima.
    remaining_slots = 11 - len(xi_ids)
    pool = sorted(
        (c for c in squad.players if c.id not in xi_ids),
        key=lambda c: (-c.value, c.id),
    )
    for c in pool:
        if remaining_slots == 0:
            break
        max_n = xi_bounds[c.position][1]
        if counts[c.position] >= max_n:
            continue
        xi_ids.add(c.id)
        counts[c.position] += 1
        remaining_slots -= 1

    if len(xi_ids) != 11:
        raise SquadError(
            f"could not fill an 11-player XI within formation bounds {xi_bounds} "
            f"from squad {[(p.position, p.id) for p in squad.players]}"
        )

    xi = tuple(sorted((p for p in squad.players if p.id in xi_ids), key=lambda c: (-c.value, c.id)))
    bench = tuple(sorted((p for p in squad.players if p.id not in xi_ids), key=lambda c: (-c.value, c.id)))
    captain = xi[0]
    vice_captain = xi[1]
    return xi, bench, captain, vice_captain
