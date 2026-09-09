"""Single-period MILP optimiser — Phase 3's deliverable, the E6 gate
(blueprint §6.1/§7.2, CLAUDE.md rules 1/4/5/7). Session `s005`, task
`single-period-milp`.

## What this module is

Given one gameweek's candidate pool (identity/price/position/club, plus a
points DISTRIBUTION per candidate — never a scalar, rule 5), pick the best
valid 15, the starting XI, the bench order, the captain and the vice-
captain, certified optimal (to a tight, configured gap) by HiGHS
(`highspy`) rather than a greedy heuristic. `fplai.backtest.squad.
build_squad`'s own module docstring says this explicitly: "Phase 3's MILP
is what 'beat the template' is measured against" — this module is that
MILP.

**Single-period only, by this task's explicit scope.** Blueprint §6.1
describes a receding-horizon MILP over gameweeks *t..t+5* with transfers,
hits, and chips — that is Phase 4 (E7). This module solves ONE gameweek in
isolation: no transfer variables, no free-transfer/hit accounting, no chip
logic, no squad-continuity constraint linking gameweek *t* to *t-1*. See
"What a multi-period extension needs" below for how this constraint model
was shaped so that extension is additive, not a rewrite.

## The constraint model

Three families of binary decision variables, one per candidate `i`
(`i` is the FPL `element` id, used directly as the HiGHS variable key —
`highspy.Highs.addVariables` accepts arbitrary hashable keys, so there is
no separate index-remapping layer to keep in sync with `element` ids):

- `squad_i ∈ {0,1}` — is `i` one of the 15.
- `xi_i ∈ {0,1}` — is `i` one of the starting 11. Constrained `xi_i <=
  squad_i` (an XI member must be a squad member — without this, the
  objective could reward an "XI" player who was never actually bought).
- `captain_i ∈ {0,1}` — is `i` the captain. Constrained `captain_i <=
  xi_i` (captain must start).

Constraints, every value read from the caller's `SquadRules` (CLAUDE.md
rule 4 — this module never hardcodes budget, composition, formation
bounds or the club cap; see `fplai.backtest.rules.rules_for_season`'s own
docstring for why a *historical* season's rules are a declared, cited
assumption rather than a live-config read, and prefer a live-config read
for a *current*-season solve — this module is agnostic to which `rules`
its caller passes):

```
sum(price_i * squad_i)                     <= budget_tenths
sum(squad_i for i in position p)           == squad_composition[p]      for every position p
sum(squad_i for i in club c)               <= max_per_club              for every club c
xi_i                                       <= squad_i                   for every i
sum(xi_i)                                  == 11
xi_bounds[p][0] <= sum(xi_i for i in p)    <= xi_bounds[p][1]           for every position p
captain_i                                  <= xi_i                      for every i
sum(captain_i)                             == 1
```

No formation constraint is expressed for the 4 bench slots beyond "squad
minus XI" — see "Bench order is a stated simplification" below for why.

## The objective — where and how the PMF collapses to a scalar

`collapse_to_expected_points(dist: PointsDistributionLike) -> float` is
the ENTIRE collapse. It is:

- **A single, named, documented function** — nowhere else in this module
  reads `.points`/`.probabilities` off a distribution object. Every
  constraint above is built from `price`/`position`/`team` only, never
  from the distribution — the distribution's only consumer is this one
  function.
- **The last thing that happens before the MILP is built.** `optimise_
  squad` calls it exactly once per candidate, immediately assembles the
  resulting `dict[element, float]`, and every objective coefficient below
  is drawn from that dict — the full PMF is never touched again after
  this point, but it survives, untouched, everywhere upstream of it
  (`OptimiserCandidate.points_dist` carries the whole distribution, not
  a pre-collapsed number, all the way to the solver boundary).
- **Replaceable.** Phase 5 (E8)'s rank-aware objective reads *A DIFFERENT*
  function of the same `PointsDistributionLike` — e.g. an EO-weighted
  percentile, or a CVaR-style downside-adjusted value — and only this one
  function plus its call site in `optimise_squad` change. No constraint,
  no variable, no solver option changes, because none of them ever look
  at the distribution shape.

`E[points]` (`sum(p * prob for p, prob in zip(dist.points, dist.
probabilities))`) is the Phase 3 choice, per this task's brief: "the
obvious and defensible Phase 3 choice." Nothing here claims it is
rank-aware — blueprint §10's target (top 10% overall) needs the
percentile-aware version, and that is explicitly Phase 5's job, not this
one's.

## Captain — a decision variable, not a post-hoc pick

The objective is
```
maximize  sum(xi_i * e_i)  +  sum(captain_i * e_i)  -  tie_break_terms
```
where `e_i = collapse_to_expected_points(candidate_i.points_dist)`. The
captain's extra `e_i` is a genuine second term over the SAME `e_i`, added
because `captain_i <= xi_i` — captaincy can only ever double a player who
is already in the XI, exactly FPL's own rule. This is deliberately NOT
"solve for XI, then pick the highest scorer as captain" as a separate
step: both decisions are made by the same solve, over the same
constraints, simultaneously, so a marginal player whose OWN expected
points are merely decent but whose captaincy upside is exceptional can
still tip which 11-of-15 the solver prefers whenever the two decisions
interact (they are independent for a strictly optimal solve when `e_i` is
unique-maximal in the XI as usual, but the brief is explicit that the two
"differ whenever bench/XI constraints bind" — modelling captaincy as a
constrained variable makes the solver find the true joint optimum in
every case, including a bench/XI/budget three-way squeeze, without this
module having to reason about when the naive shortcut would go wrong).

**Vice-captain is NOT a decision variable.** FPL's vice only pays out
if the captain records zero minutes — a single-period expected-value
objective over "did the captain play" is exactly the sort of scenario
this module's proxy expected-points signal (see "The backtest proxy PMF"
below) cannot resolve on its own (an empirical points histogram does not
separate "played and scored 0" from "did not play"), and building that
properly needs the minutes model's own `P(state=UNUSED)`, which is a
Phase 5/live-pipeline concern, not this module's. Vice-captain is chosen
POST-HOC: the highest-`e_i` XI member other than the captain, ties broken
by lowest `element` id — the exact convention `fplai.backtest.squad.
choose_xi_bench_captain` already uses (`captain = xi[0], vice = xi[1]`
under a `(-value, id)` sort) — reused here rather than invented, so the
two callers of this codebase's squad-selection logic agree on what
"second-best" means.

## Bench order is a stated simplification, not an optimised decision

Bench players score points only via automatic substitution — a
0-objective event in this module's own formulation (only `xi_i`/
`captain_i` carry `e_i`; a `squad_i` that lost the XI competition
contributes nothing to the objective). Properly valuing a bench slot
needs `P(this specific starter records 0 minutes) * (this bench player's
own expected points | subbed on)`, which needs the minutes model's own
per-player start/sub/unused split — not available from this module's
proxy signal (see below), and out of scope for Phase 3 per this task's
brief ("For Phase 3 a defensible simplification is to order the bench by
expected points"). **What this module actually does**: the 4 non-XI
squad members are ordered by `(-e_i, element_id)` — highest expected
points first, i.e. first sub priority — the same sort key `fplai.backtest.
squad.choose_xi_bench_captain` already uses for its own bench ordering.
**"At least one GK sits on the bench"** is not a separate constraint this
module adds; it falls out of the EXISTING composition/formation
constraints for every `SquadRules` this codebase declares today (2 GK in
the squad, exactly 1 in the XI => exactly 1 on the bench) — but that is a
consequence of THIS SEASON'S numbers, not a law of the constraint model,
so `optimise_squad` asserts it explicitly after solving and raises
`OptimiserError` rather than silently shipping a GK-less bench if some
future `SquadRules` ever changed the GK count/formation bound in a way
that broke the implication.

## The backtest proxy PMF, and why it is not `fplai.points.PointsPMF`

**This module's PRODUCTION distribution input is a duck-typed
`PointsDistributionLike` Protocol** (`.points`, `.probabilities` — the
exact two field names `fplai.points.PointsPMF` already carries), never a
hard dependency on the concrete `PointsPMF` dataclass. `fplai.points.
simulate_fixture_points_pmfs`'s real six-model composition satisfies this
protocol structurally and is the intended live input — but wiring it in
needs a live/historical FEATURE-ROW pipeline (`docs/HANDOFF.md` §3,
"Phase 3 has two unwritten prerequisites"; `fplai.points`'s own module
docstring, "What this module does NOT do" — "Building a LIVE feature-row
pipeline ... is a separate, not-yet-built prerequisite"). That pipeline
does not exist for either the live 2026/27 season OR for the seven
historical seasons this backtest runs against: it would mean re-deriving
each of the six models' own `numeric_columns` feature vocabulary, as of
every historical gameweek's own deadline, for every player — a project
of comparable size to the six models themselves, and explicitly not this
task's OWNED PATHS (model files are READ-ONLY, not to be extended).

**What this module ships instead, for the backtest strategy only**
(`MILPStrategy`, `_trailing_points_distributions`): a genuine, dense-
support EMPIRICAL points distribution built from the SAME leakage-free
signal `fplai.backtest.baselines.GreedyFormBaseline` already uses —
`view.history`'s trailing `GREEDY_FORM_TRAILING_GAMEWEEKS` gameweeks of a
player's OWN stored `total_points`, one real historical value per
gameweek, histogrammed — never a scalar dressed up as a distribution
(rule 5 forbids exactly that), and never anything from `round >=
view.gameweek` (the same leakage boundary every baseline already
respects; `GameweekView.history` structurally cannot carry the gameweek
being decided — see `fplai.backtest.replay`'s own module docstring).
**This IS a real distribution** — it carries genuine dispersion from the
player's own trailing scores, not a point mass — but it is a materially
coarser thing than the six-model composition (no fixture-level scoreline,
no minutes-band conditioning, no captured cross-model correlation), which
is exactly why it is a SEPARATE, clearly-named type
(`_EmpiricalPointsDistribution`), not `fplai.points.PointsPMF` itself —
giving it the concrete production type would misrepresent what produced
it. **This is a stated, argued proxy, not a silent stand-in**: the
E6 gate this module is built to pass tests the OPTIMISER — the constraint
model, the objective-collapse step, captain-as-a-variable, and
deterministic tie-breaking — end to end, against real backtested seasons,
without waiting on the (much larger) six-model live-pipeline story. When
that pipeline lands, `MILPStrategy` swaps its ONE call
(`_trailing_points_distributions` -> a per-fixture `simulate_fixture_
points_pmfs` call) for a caller supplying real `PointsPMF` objects — no
change anywhere else in this file, because both satisfy the same
`PointsDistributionLike` Protocol `optimise_squad` actually consumes.

**Cold start** (no trailing history — the season's first gameweek, or a
player with zero rows in the window): a degenerate distribution at 0
points, probability 1 — the same documented cold-start value `fplai.
backtest.baselines`' own module docstring uses ("value = 0.0 for every
candidate for that gameweek only ... a real, labelled degenerate case,
not a modelled decision"), reused rather than a new undocumented rule
invented for this module.

## Determinism (CLAUDE.md rule 7) — the tie-break, and how it was proven

**HiGHS itself, run single-threaded with fixed options, is deterministic
given identical input** — verified live before writing this module's
production code: a synthetic 40-candidate all-tied knapsack (`threads=1`)
returned the IDENTICAL chosen set across 8 repeated solves. But
"deterministic" is not the same as "a chosen, explicable tie-break" —
without one, the winning vertex among true ties is whatever HiGHS's
internal branch-and-bound order happens to prefer, which is stable
run-to-run on one solver build but is an implementation detail this
project should not depend on (the exact "stable on the CURRENT file
layout, would silently break on a change" fragility blueprint §7.2's
gate-repair session (HANDOFF §2, "polars' `.unique(maintain_order=
False)`") already cost real debugging time once, on a different layer).
**This module therefore pins an EXPLICIT tie-break, per the same
`(gain, -id)`-favours-lowest-id convention `fplai.backtest.squad.
build_squad`'s hill-climb already established**: every objective
coefficient carries an additional `- OptimiserConfig.tie_break_eps *
element_id` term, at every one of the three variable layers
(`squad_i`/`xi_i`/`captain_i` each get their own copy — a squad-level tie
needs breaking independently of an XI-level tie, which needs breaking
independently of a captain-level tie, even though `captain_i <= xi_i <=
squad_i` already links them). `tie_break_eps` defaults to `1e-6`; the
largest possible perturbation to any one candidate's true objective
contribution is `3 * tie_break_eps * max(element_id)` (three layers,
worst case all three apply) — with real FPL element ids topping out in
the low thousands, that is on the order of `1e-2`, several orders of
magnitude below the smallest resolvable `e_i` gap either signal in this
codebase can produce (an empirical histogram of integer points has a
resolution floor of `1/n_draws` per unique value, and a Monte-Carlo
`PointsPMF` from `fplai.points` is bounded below by
`1/n_simulations`, both `>= ~1e-3` at any realistic sample size) — chosen
deliberately below that floor so the tie-break can only ever move a
genuinely-tied decision, never a real preference.

**Verified, not just argued**: `tests/test_optimiser.py::
test_optimise_squad_is_bit_identical_across_repeated_calls_with_
genuinely_tied_candidates` builds a candidate pool where every player
shares an IDENTICAL `points_dist` (a true tie across the entire pool,
the worst case for a solver's internal tie-break) and asserts the chosen
squad/XI/bench/captain are bit-identical across N repeated solves AND
match the tie-break's own stated rule (lowest `element_id` wins, subject
to the constraints) — proving the design, not merely the reproducibility.
`tests/test_backtest.py`'s own precedent
(`test_greedy_form_baseline_is_reproducible_across_repeated_runs_on_the_
real_store`) is mirrored here for `MILPStrategy` against the real store,
per this task's brief.

`OptimiserConfig` also pins `threads=1` (never left at HiGHS's own
`0`/"choose" default — verified live that the default is hardware-
dependent auto-selection, a second, independent source of run-to-run
variance this module refuses to depend on) and a fixed `random_seed`
(HiGHS's own MIP heuristics can consult one; `0` unless the caller
overrides it) — belt and suspenders alongside the explicit tie-break,
matching `fplai.backtest.squad.build_squad`'s own "fixed at BOTH ends"
precedent for the identical class of bug on a different layer.

## What a multi-period extension (Phase 4, E7) needs

Stated here because this constraint model was shaped for it, not because
it is built:

- Index every variable by gameweek: `squad_{i,t}`, `xi_{i,t}`,
  `captain_{i,t}`, over `t = 0..horizon`.
- A squad-continuity link between periods: `squad_{i,t} = squad_{i,t-1} +
  in_{i,t} - out_{i,t}`, with `in_{i,t}`/`out_{i,t}` new binary transfer
  variables, plus `sum(in_{i,t}) == sum(out_{i,t})` per gameweek (transfers
  balance).
- A rolling budget that carries banked money forward:
  `budget_t = budget_{t-1} - sum(price_i * in_{i,t}) + sum(price_i *
  out_{i,t})`.
- A free-transfer/hit accounting layer: a running `free_transfers_t`
  state variable (capped, per blueprint §11's live rules) and a
  `-4 * max(0, transfers_t - free_transfers_t)` penalty term in the
  objective — genuinely nonlinear (a `max`) and needs the standard
  MILP linearisation (an auxiliary variable + two inequalities), not a
  new idea, just not built here.
- Chip binaries (`wildcard_t`, `bench_boost_t`, `triple_captain_t`,
  `free_hit_t` — blueprint §11's current live inventory, never
  hardcoded) that RELAX or MODIFY specific constraints for exactly one
  `t`: wildcard suspends the continuity link for that gameweek's
  transfers (no hit, unlimited moves), bench boost adds `sum(squad_{i,t}
  - xi_{i,t}) * e_{i,t}` to that gameweek's objective term, triple
  captain changes the captain multiplier from 2 to 3 for that `t` only.
- The objective sums `collapse_to_expected_points` over EVERY `(i, t)`
  pair the horizon touches — this module's collapse function is already
  the right shape for that; only the summation index grows.
- This module's existing single-`t` constraint block (composition,
  budget, formation, captain) becomes exactly the `t`-th slice of the
  multi-period model — nothing here needs to change shape, only be
  repeated and linked.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable

import highspy
import polars as pl

from fplai.backtest.replay import Decision, GameweekView, SquadState, _sell_price, last_known_attributes
from fplai.backtest.rules import GREEDY_FORM_TRAILING_GAMEWEEKS, SquadRules, TransferRules
from fplai.backtest.squad import PlayerCandidate, Squad
from fplai.features import (
    FixtureFeatureAssemblyParams,
    Roster,
    assemble_fixture_player_features_from_frame,
    combine_gameweek_points_pmfs,
    make_saves_predict_fn,
)
from fplai.models.attacking import AttackingModelParams
from fplai.models.bonus import BonusModelParams
from fplai.models.cards import CardsModelParams
from fplai.models.defensive_contribution import DCModelParams
from fplai.models.minutes import MinutesModelParams
from fplai.models.saves import SavesModelParams
from fplai.models.team_strength import TeamStrengthParams, predict_scoreline
from fplai.points import PointsPMF, PointsSimulationConfig, simulate_fixture_points_pmfs
from fplai.scoring import ScoringConfig

# House vocabulary, declared independently rather than imported — the same
# convention every fplai.models.* module and fplai.points already follow
# for this exact four-tuple (see fplai.points module docstring,
# "Composition, not import").
POSITIONS: tuple[str, ...] = ("GK", "DEF", "MID", "FWD")


class OptimiserError(ValueError):
    """Misuse, an infeasible constraint set, or a genuine solver failure
    this module refuses to paper over — an under-filled position pool, a
    non-optimal solver status, a bench that lost its structurally-implied
    GK slot. Never silently return a partial or invalid decision."""


# ---------------------------------------------------------------------------
# The objective-collapse boundary (module docstring, "The objective").
# ---------------------------------------------------------------------------


@runtime_checkable
class PointsDistributionLike(Protocol):
    """The minimal duck-typed shape this module needs from a points
    distribution — `fplai.points.PointsPMF`'s own two field names,
    matched structurally (module docstring, "The backtest proxy PMF") so
    this module never hard-imports that concrete dataclass. Any object
    carrying a dense integer `points` support and a matching
    `probabilities` sequence that sums to 1.0 satisfies this."""

    points: tuple[int, ...]
    probabilities: tuple[float, ...]


def collapse_to_expected_points(dist: PointsDistributionLike) -> float:
    """THE single, named, last-step collapse from a full points
    distribution to the scalar a linear MILP objective needs (CLAUDE.md
    rule 5; module docstring, "The objective"). Phase 5's rank-aware
    objective replaces just this function (and its call site in
    `optimise_squad`) with something reading a different property of the
    same `PointsDistributionLike` — a percentile, an EO-weighted rank
    value, a downside-adjusted CVaR — with no other change anywhere in
    this module, because nothing else here ever reads `.points`/
    `.probabilities`."""
    return sum(p * prob for p, prob in zip(dist.points, dist.probabilities))


# ---------------------------------------------------------------------------
# Inputs / outputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptimiserCandidate:
    """One selectable player at one single-period decision point. `price`
    is FPL's own x10 convention (matches `fplai.backtest.squad.
    PlayerCandidate.price`). `points_dist` carries the WHOLE distribution
    into this module — collapsed to a scalar exactly once, inside
    `optimise_squad`, never earlier (module docstring)."""

    element: int
    name: str
    position: str
    team: str
    price: int
    points_dist: PointsDistributionLike

    def __post_init__(self) -> None:
        if self.position not in POSITIONS:
            raise OptimiserError(f"unknown position {self.position!r} for element={self.element} — expected one of {POSITIONS}")


@dataclass(frozen=True)
class OptimiserConfig:
    """Every solver/tie-break hyperparameter, in one place — same
    convention `fplai.points.PointsSimulationConfig` already establishes
    for its own module (CLAUDE.md rule 4's spirit, applied to solver
    behaviour rather than a scoring/threshold value). See module
    docstring, "Determinism", for why every one of these defaults is
    pinned rather than left at HiGHS's own defaults."""

    tie_break_eps: float = 1e-6
    """Per-`element_id` objective perturbation favouring the lowest id on
    an exact tie, applied independently at the squad/xi/captain layers
    (module docstring, "Determinism"). Deliberately far below the
    smallest resolvable `e_i` gap either distribution source in this
    codebase can produce."""

    mip_rel_gap: float = 1e-9
    mip_abs_gap: float = 1e-9
    """HiGHS's own optimality-gap tolerances, tightened from its defaults
    (1e-4 relative) — this module's problems are small (a few thousand
    binaries at most), so proving near-exact optimality costs negligible
    extra time and removes a second source of "close enough, but which
    close-enough vertex" nondeterminism."""

    threads: int = 1
    """Never left at HiGHS's own `0` ("choose", hardware-dependent —
    verified live) — module docstring, "Determinism"."""

    random_seed: int = 0
    time_limit_seconds: float | None = None


@dataclass(frozen=True)
class OptimiserResult:
    """The solved decision, as element ids — deliberately NOT
    `fplai.backtest.replay.Decision` (which needs `fplai.backtest.squad.
    PlayerCandidate`/`Squad` objects this module's own inputs do not
    carry enough of — `name` is optional metadata here, not load-bearing
    to the solve). `MILPStrategy` adapts this into a `Decision` for the
    backtest harness; a live weekly caller can use this directly."""

    squad_element_ids: tuple[int, ...]
    xi_element_ids: tuple[int, ...]
    bench_element_ids: tuple[int, ...]  # sub-priority order, highest e_i first (module docstring, "Bench order")
    captain_element_id: int
    vice_captain_element_id: int
    expected_points: dict[int, float] = field(default_factory=dict)  # every candidate considered, not just chosen — diagnostic
    objective_value: float = 0.0  # HiGHS's raw objective, INCLUDING tie-break terms — diagnostic only
    solver_status: str = ""


# ---------------------------------------------------------------------------
# The core MILP.
# ---------------------------------------------------------------------------


def optimise_squad(
    candidates: Sequence[OptimiserCandidate],
    rules: SquadRules,
    config: OptimiserConfig = OptimiserConfig(),
) -> OptimiserResult:
    """Build and solve the single-period MILP (module docstring, "The
    constraint model"/"The objective"). Deterministic given identical
    `candidates`/`rules`/`config` (module docstring, "Determinism").

    Raises `OptimiserError` if a position pool cannot fill its required
    squad count, if the solve does not reach `kOptimal`, or if the solved
    bench structurally lost its implied GK slot (see module docstring,
    "Bench order is a stated simplification").
    """
    if not candidates:
        raise OptimiserError("optimise_squad called with an empty candidate list")

    # Canonical order: never trust the caller's own list order (the same
    # "do not trust upstream ordering" discipline `fplai.points.
    # simulate_fixture_points_pmfs` already applies, traced back to Phase
    # 1's polars `.unique(maintain_order=False)` bug — HANDOFF §2).
    ordered = sorted(candidates, key=lambda c: c.element)
    seen: set[int] = set()
    for c in ordered:
        if c.element in seen:
            raise OptimiserError(f"duplicate element={c.element} in candidates")
        seen.add(c.element)

    ids = [c.element for c in ordered]
    price = {c.element: c.price for c in ordered}
    team = {c.element: c.team for c in ordered}
    e: dict[int, float] = {c.element: collapse_to_expected_points(c.points_dist) for c in ordered}  # THE collapse, once.

    by_position: dict[str, list[int]] = {}
    for c in ordered:
        by_position.setdefault(c.position, []).append(c.element)
    by_club: dict[str, list[int]] = {}
    for c in ordered:
        by_club.setdefault(c.team, []).append(c.element)

    comp = rules.squad_composition_dict
    xi_bounds = rules.xi_bounds_dict

    for position, count in comp.items():
        available = len(by_position.get(position, []))
        if available < count:
            raise OptimiserError(f"not enough {position} candidates: need {count}, have {available}")

    h = highspy.Highs()
    h.silent()
    h.setOptionValue("threads", config.threads)
    h.setOptionValue("random_seed", config.random_seed)
    h.setOptionValue("mip_rel_gap", config.mip_rel_gap)
    h.setOptionValue("mip_abs_gap", config.mip_abs_gap)
    if config.time_limit_seconds is not None:
        h.setOptionValue("time_limit", config.time_limit_seconds)

    squad_vars = h.addVariables(ids, type=highspy.HighsVarType.kInteger, lb=0, ub=1, name=[f"squad_{i}" for i in ids])
    xi_vars = h.addVariables(ids, type=highspy.HighsVarType.kInteger, lb=0, ub=1, name=[f"xi_{i}" for i in ids])
    captain_vars = h.addVariables(ids, type=highspy.HighsVarType.kInteger, lb=0, ub=1, name=[f"captain_{i}" for i in ids])

    # --- squad composition, budget, club cap ------------------------------
    for position, count in comp.items():
        pool = by_position[position]
        h.addConstr(h.qsum(squad_vars[i] for i in pool) == count, name=f"comp_{position}")

    h.addConstr(h.qsum(price[i] * squad_vars[i] for i in ids) <= rules.budget_tenths, name="budget")

    for club, pool in by_club.items():
        h.addConstr(h.qsum(squad_vars[i] for i in pool) <= rules.max_per_club, name=f"club_{club}")

    # --- XI: subset of squad, exactly 11, formation bounds -----------------
    for i in ids:
        h.addConstr(xi_vars[i] <= squad_vars[i], name=f"xi_subset_{i}")
    h.addConstr(h.qsum(xi_vars[i] for i in ids) == 11, name="xi_size")
    for position, (lo, hi) in xi_bounds.items():
        pool = by_position.get(position, [])
        h.addConstr(h.qsum(xi_vars[i] for i in pool) >= lo, name=f"xi_min_{position}")
        h.addConstr(h.qsum(xi_vars[i] for i in pool) <= hi, name=f"xi_max_{position}")

    # --- captain: subset of XI, exactly one --------------------------------
    for i in ids:
        h.addConstr(captain_vars[i] <= xi_vars[i], name=f"captain_subset_{i}")
    h.addConstr(h.qsum(captain_vars[i] for i in ids) == 1, name="one_captain")

    # --- objective: E[points] over XI + captain's doubled share, plus a
    # deterministic tie-break at every layer (module docstring,
    # "Determinism"). ------------------------------------------------------
    tie = config.tie_break_eps
    objective_expr = h.qsum(
        xi_vars[i] * e[i] + captain_vars[i] * e[i] - tie * i * (squad_vars[i] + xi_vars[i] + captain_vars[i])
        for i in ids
    )
    h.maximize(objective_expr)

    status = h.getModelStatus()
    if status != highspy.HighsModelStatus.kOptimal:
        raise OptimiserError(
            f"MILP did not solve to optimality: status={status.name}, n_candidates={len(ordered)}, "
            f"budget_tenths={rules.budget_tenths}"
        )

    squad_ids = tuple(sorted(i for i in ids if h.val(squad_vars[i]) > 0.5))
    xi_id_set = {i for i in ids if h.val(xi_vars[i]) > 0.5}
    captain_ids = [i for i in ids if h.val(captain_vars[i]) > 0.5]
    if len(captain_ids) != 1:
        raise OptimiserError(f"solver returned {len(captain_ids)} captains, expected exactly 1")
    captain_id = captain_ids[0]

    # XI/bench ordering (module docstring, "Bench order is a stated
    # simplification") — highest e_i first, lowest element id breaks ties,
    # mirroring fplai.backtest.squad.choose_xi_bench_captain's own
    # (-value, id) convention exactly.
    xi_sorted = tuple(sorted(xi_id_set, key=lambda i: (-e[i], i)))
    bench_sorted = tuple(sorted((i for i in squad_ids if i not in xi_id_set), key=lambda i: (-e[i], i)))

    element_to_position = {c.element: c.position for c in ordered}
    if "GK" not in {element_to_position[i] for i in bench_sorted}:
        raise OptimiserError(
            "solved bench has no GK — the structural implication (2 GK in squad, 1 in XI => 1 on "
            "bench) that every SquadRules this codebase declares today satisfies no longer holds "
            "for this rules object; see module docstring, 'Bench order is a stated simplification'."
        )

    vice_candidates = [i for i in xi_sorted if i != captain_id]
    if not vice_candidates:
        raise OptimiserError("XI has fewer than 2 players — cannot select a vice-captain")
    vice_captain_id = vice_candidates[0]

    return OptimiserResult(
        squad_element_ids=squad_ids,
        xi_element_ids=xi_sorted,
        bench_element_ids=bench_sorted,
        captain_element_id=captain_id,
        vice_captain_element_id=vice_captain_id,
        expected_points=e,
        objective_value=float(h.getObjectiveValue()),
        solver_status=status.name,
    )


# ---------------------------------------------------------------------------
# Multi-period MILP (Phase 4, E7, story S8) — the extension the module
# docstring's "What a multi-period extension (Phase 4, E7) needs" section
# sketched before it was built. That section is left UNCHANGED above (it is
# the historical design note, and `optimise_squad` itself is byte-untouched
# by this story per its own brief); this section documents what was
# actually built and exactly where it diverges from the sketch.
#
# ## Shape of the input — D1/D2/D3 (this story's pinned decisions)
#
# `optimise_multi_period` takes `horizon_candidates: Mapping[int,
# Sequence[OptimiserCandidate]]` — precisely `ModelStackStrategy.
# horizon_candidates(view)`'s own return shape (`optimiser.py`, S3 part 2),
# never regenerated here. Rounds are the mapping's own keys, sorted; `t`
# (the round actually executed, D9) is the smallest key. The mapping may be
# SHORTER than the nominal horizon (season end, an ingestion gap) — not an
# error, `H` is simply `len(horizon_candidates)`.
#
# D2 is VALIDATED, not assumed: every round's element set, and every
# element's price/position/team, must equal round `t`'s — an `OptimiserError`
# names the first mismatch found, round and element together, rather than
# silently building a per-round `by_position`/`by_club` that could disagree
# gameweek to gameweek. This validation is also what licenses treating
# `price`/`position`/`team`/`by_position`/`by_club` as ROUND-INVARIANT
# dictionaries below, built once from round `t`'s own candidates and reused
# unchanged for every later round's constraints — exactly the same
# single-collection-then-reuse discipline `optimise_squad` already applies
# within one round, just widened across the horizon.
#
# ## The variable families
#
# `squad_{i,t}`/`xi_{i,t}`/`captain_{i,t}` — one COPY of `optimise_squad`'s
# own three binary families per round `t` in the horizon, under the
# IDENTICAL per-round constraint block (composition/budget-shape/formation/
# captain-subset) `optimise_squad` already builds — this is the "existing
# single-`t` constraint block ... becomes exactly the `t`-th slice" promise
# the module docstring's sketch made, now literally true: the per-round loop
# below emits the same six constraint families, once per round, differing
# only in variable/constraint NAME suffixes.
#
# `in_{i,t}`/`out_{i,t}` — new binary transfer variables, created for every
# round where transfer accounting genuinely applies. That is EVERY round
# when `incoming_state is not None`; it is every round EXCEPT `t` itself
# when `incoming_state is None` (D3: the first round is a FREE build with no
# transfer concept at all — see "The `incoming_state=None` case" below).
# `in_{i,t} + out_{i,t} <= 1` per player per round is added even though the
# brief does not name it explicitly: without it, a player could be marked
# simultaneously bought and sold in the same round at zero net squad change,
# which is a no-op that COSTS a transfer (and therefore FT/hits) for
# nothing — the continuity equation alone does not forbid it, since
# `in - out` can equal 0 with `in = out = 1` just as validly as `in = out =
# 0`. Nothing in the objective ever wants this (it can only make hits_t
# larger for no benefit), but "the solver would never choose it" is not the
# same guarantee as "the solver cannot choose it" (CLAUDE.md's "a guarantee
# is only earned once attacked" standard, applied here structurally rather
# than by a specific test — an alternate optimal vertex with a phantom
# in/out pair would still report a WRONG `transfers_in`/`transfers_out` even
# though the squad/points would be identical, which is exactly the
# diagnostic-correctness class of bug this module refuses to ship).
#
# ## D4 — the sell-price constant, exploited exactly as pinned
#
# Because D2 pins price identical across every round, a player bought
# INSIDE the horizon is sold inside it at exactly what was paid — proceeds
# `out_{i,t}`'s coefficient is simply `price[i]`, a plain Python int, no
# variable. Only an INCOMING-squad player (`i in incoming_state.
# element_ids`) carries a purchase-vs-current gap; its proceeds are
# `fplai.backtest.replay._sell_price(purchase_price, price[i])` — imported,
# never reimplemented (this story's brief, citing story S7's own tests for
# that function). Both cases collapse to a CONSTANT per `i`, computed once
# in `_sell_proceeds_tenths` below and reused for every round `i` could be
# sold in — including a second sale after a rebuy, which is D4's named
# residual edge case: using the ORIGINAL haircut price for every sale of an
# incoming element UNDERSTATES available cash on a sell-rebuy-resell
# (a second sale at the then-current price could realise more), which is
# the conservative direction and is stated here rather than modelled, per
# the brief.
#
# ## D5 — the rolling budget
#
# One continuous `bank_t >= 0` HiGHS variable per round where transfer
# accounting applies, tied to the previous round's bank (a Python constant
# for the very first such round, a HiGHS variable/expression thereafter) by
# an EQUALITY constraint: `bank_t == bank_{t-1} + sum(sell_i * out_{i,t}) -
# sum(price_i * in_{i,t})`. Chosen over an accumulated inequality because a
# variable-per-round is what lets the FT/hits chain (D6) and the `plan`
# diagnostic (D9) read back "opening bank for round t" directly via `h.
# val(...)`, without re-deriving it from every prior round's transfers.
# `bank_t`'s own `lb=0` IS the "never go negative" guarantee (D5's own
# wording) — no separate inequality duplicates it.
#
# There is NO separate `sum(price_i * squad_{i,t}) <= rules.budget_tenths`
# constraint for a round with an incoming state. This is deliberate, not an
# omission: `budget_tenths` bounds what a squad can be BUILT for, not what
# an already-owned squad's current value may drift to as prices move —
# `fplai.backtest.replay._advance_state` (read-only, this story's own
# citation) never checks `budget_tenths` either, only that `bank_tenths`
# stays non-negative. The ONLY round that uses the flat `budget_tenths`
# cap is a `t == t0` FREE build under `incoming_state=None` (D3) — exactly
# `optimise_squad`'s own constraint, because that IS what that round is.
#
# ## D6 — free transfers and hits, and the identity that avoids a second
# `max(0, ...)` linearisation
#
# `hits_t >= transfers_t - free_transfers_t`, `hits_t >= 0`, both pinned by
# the brief, both DEC INTEGER, and — because the objective's `hit_cost *
# hits_t` term (negative `hit_cost`, D6's sign) always rewards a SMALLER
# `hits_t` — HiGHS's own pressure to maximise the objective pushes `hits_t`
# down to exactly `max(0, transfers_t - free_transfers_t)` at any optimum.
# That is the textbook `max(0, x)` trick, exactly as the brief states it.
#
# The BRIEF's own recurrence is `free_transfers_t = min(max_banked,
# free_transfers_{t-1} - spent_{t-1} + per_gameweek)`. Read literally, that
# formula can go negative when `spent_{t-1} > free_transfers_{t-1}` (a hit
# was taken) — FPL's real rule clamps the SPENT-DOWN amount at zero before
# adding next gameweek's allowance (`fplai.backtest.replay._advance_state`:
# `remaining = max(0, previous.free_transfers - n_transfers)`), which is a
# SECOND `max(0, ...)` the brief's one-line recurrence elides. Building that
# second `max` the naive way (an auxiliary binary + big-M) would be exactly
# the "correctness surface" this story's probe evidence said not to spend
# effort on for solve-SPEED reasons — but this one is not a speed concern,
# it is a genuine second nonlinearity the brief's stated formula does not
# actually avoid.
#
# It IS avoidable, without a second binary, by reusing `hits_t` itself.
# Once `hits_t` is pinned to its true value (the paragraph above), the
# identity
#
#     leftover_t := free_transfers_t - transfers_t + hits_t
#
# equals EXACTLY `max(0, free_transfers_t - transfers_t)`: if `transfers_t
# <= free_transfers_t` then `hits_t = 0` and `leftover_t = free_transfers_t
# - transfers_t`, the genuine unused balance; if `transfers_t >
# free_transfers_t` then `hits_t = transfers_t - free_transfers_t` exactly,
# so `leftover_t` collapses to `0` — a hit was taken, nothing carries over.
# `leftover_t` is therefore a plain LINEAR expression in already-defined
# variables, needing no new binary, and it is the CORRECT input to the
# brief's `min(max_banked, ... + per_gameweek)` step — restoring the clamp
# the brief's one-liner omitted, for free.
#
# The `min` itself is then linearised the same way the brief says the `max`
# already is, but mirrored: `free_transfers_{t+1} <= max_banked` (the
# variable's own `ub`) and `free_transfers_{t+1} <= leftover_t +
# per_gameweek`, both upper bounds, plus a small `+ ft_settle_eps *
# free_transfers_{t+1}` term in the objective — analogous to the tie-break
# below, NOT a preference for more transfers, but a nudge (`tie_break_eps`
# again, several orders of magnitude below the smallest resolvable `e_i`
# gap) that makes the reported `free_transfers_available` diagnostic settle
# at its true tight value in the (rare, harmless-to-the-real-decision) case
# where a round makes zero transfers and the LP relaxation would otherwise
# leave the variable anywhere satisfying the two upper bounds. Without this
# nudge the CHOSEN squad/transfers/hits are unaffected (nothing else ever
# reads `free_transfers_{t}` except the two constraints that already pin it
# whenever it matters), only a diagnostic-only field could read low; the
# nudge closes that gap at negligible cost, the same "belt and suspenders"
# posture the module docstring's "Determinism" section already argues for
# `tie_break_eps` itself.
#
# `free_transfer_overrides_dict` (a sparse per-gameweek TOP-UP, `fplai.
# backtest.rules.TransferRules`, read-only) SETS `free_transfers_t` to the
# override value for an overridden round — an equality, not an additional
# upper bound — per this story's D6: "does not add". This governs ROUNDS
# AFTER `t` only; an override landing exactly on `t` is already resolved by
# whichever `SquadState.free_transfers` the caller built for `t`'s own
# `incoming_state` (this module never re-derives that value, the same
# "caller's ledger, this module only threads it forward" posture `fplai.
# backtest.replay`'s own module docstring takes for `SquadState`).
#
# ## D8 — the tie-break, per round, and why it is enough
#
# Every `(squad_{i,t}, xi_{i,t}, captain_{i,t})` triple carries its OWN
# copy of `optimise_squad`'s exact tie-break term, `- tie_break_eps * i *
# (squad_{i,t} + xi_{i,t} + captain_{i,t})`, independently at EVERY round —
# a round-`t+2` tie needs breaking exactly as much as round-`t`'s does; a
# single global tie-break term would not distinguish two DIFFERENT rounds'
# otherwise-tied choices. `in_{i,t}`/`out_{i,t}` carry NO tie-break term of
# their own: once `squad_{i,t}` and `squad_{i,t-1}` are pinned by their own
# tie-broken objective terms, the continuity equality `squad_{i,t} =
# squad_{i,t-1} + in_{i,t} - out_{i,t}` together with `in_{i,t} + out_{i,t}
# <= 1` and both binary FORCES a unique `(in_{i,t}, out_{i,t})` pair for
# every `i` — there is no remaining tie left to break. At `T=1`,
# `incoming_state=None` (this story's H=1 reproduction gate), `in`/`out`
# variables for `t` do not even exist (D3), so this question is moot for
# the gate itself, and the squad/xi/captain tie-break is, term for term,
# `optimise_squad`'s own — which is exactly what the gate requires.
#
# ## The `incoming_state=None` case, beyond `H=1`
#
# D3 is explicit about `H=1`: a free build, `optimise_squad` in every
# respect. For `H>1` with no incoming state (a wildcard-shaped START to a
# multi-round horizon — not this story's gate, but a shape the type allows
# and therefore must not silently misbehave), round `t`'s squad is built
# free under the flat budget cap exactly as `H=1` does, and rounds `t+1..`
# thread continuity from round `t`'s OWN solved-for variables (never a
# concrete resolved value substituted mid-model — `squad_{i,t}` stays a
# genuine decision variable throughout, exactly as it does in the
# `incoming_state is not None` case). Two starting values the type has no
# other source for are BOOTSTRAPPED, and flagged here rather than silently
# assumed:
#
# - **Bank entering round `t+1`** = `budget_tenths - sum(price_i *
#   squad_{i,t})` — an EXPRESSION (round `t`'s squad cost is a decision, not
#   a constant), realised via the same `bank_t` equality-constraint pattern
#   every other round uses. This is exactly "what is left in the bank after
#   building the free squad", the only value a `None` incoming state can
#   honestly imply.
# - **Free transfers entering round `t+1`** = `0`, not `transfer_rules.
#   free_transfers_per_gameweek` — a genuinely fresh squad (no prior
#   transfer market existed to have banked anything into) gets exactly ONE
#   free transfer at the FIRST gameweek that follows it (the normal
#   `per_gameweek` accrual applied once, from a zero base), matching FPL's
#   own "GW1 squad, GW2's transfer window opens with 1 free transfer"
#   behaviour — seeding the bootstrap at `per_gameweek` itself would double
#   that to 2 by the same recurrence, which is wrong.
#
# Neither bootstrap is pinned by this story's brief (D1-D9 all describe the
# `incoming_state is not None` case, or `H=1`); both are argued here rather
# than left implicit, per this story's own checkpoint instruction ("if
# something in the pinned decisions turns out to be wrong ... stop and
# report" — this is not that; it is a gap the brief's own decisions do not
# reach, not a contradiction of one).
# ---------------------------------------------------------------------------


def _sell_proceeds_tenths(
    element: int, price_tenths: int, *, incoming_ids: frozenset[int], purchase_prices: dict[int, int]
) -> int:
    """D4's constant sell-price coefficient for `out_{element,t}`, valid for
    EVERY round `t` an element could be sold in (D2 pins `price_tenths` as
    round `t`'s == every round's, so there is exactly one value to compute,
    not one per round). An incoming-squad element sells via FPL's own
    haircut rule (`fplai.backtest.replay._sell_price`, imported not
    reimplemented); an element bought INSIDE the horizon sells at exactly
    what was paid — no haircut ever applies to a same-window round trip
    (D4's own reasoning: buy and sell both happen at round `t`'s pinned
    price)."""
    if element in incoming_ids:
        return _sell_price(purchase_prices[element], price_tenths)
    return price_tenths


@dataclass(frozen=True)
class MultiPeriodRoundPlan:
    """One horizon round's OWN solved state — diagnostic only, per D9's
    "forward plan for diagnostics, clearly labelled plan, not commitment".
    A caller executes round `t`'s decision (`MultiPeriodResult`'s own
    top-level fields, which duplicate `plan[0]`'s content) and NOTHING
    else here — every later round is what THIS solve currently expects to
    do next, entirely superseded the moment the next real gameweek's
    `optimise_multi_period` call runs against fresh `horizon_candidates`
    (a fresh `GameweekView`, a fresh incoming `SquadState`). Blueprint
    §6.1: "solve over GW t..t+5, execute only week t" — this type is what
    makes the other four/five weeks visible without ever being mistaken
    for a commitment."""

    round: int
    squad_element_ids: tuple[int, ...]
    xi_element_ids: tuple[int, ...]
    bench_element_ids: tuple[int, ...]
    captain_element_id: int
    vice_captain_element_id: int
    transfers_in: tuple[int, ...]
    transfers_out: tuple[int, ...]
    hits: int
    """Transfers beyond this round's free allowance, `>= 0` — always `0`
    for a `t == t0` round under `incoming_state=None` (D3: no transfer
    concept applies to a free build)."""
    free_transfers_available: int
    """This round's free-transfer count BEFORE this round's own transfers
    spend any of it (D6) — `0`, meaninglessly, for a `t == t0` round under
    `incoming_state=None` (see module section above, "The
    `incoming_state=None` case")."""
    expected_points: float
    """`sum(e_{i,t} for i in xi) + e_{captain,t}` for THIS round only — the
    same two-term shape `optimise_squad`'s own objective uses for one
    round, with no tie-break and no hit cost folded in (those live only in
    `MultiPeriodResult.objective_value`, diagnostic, module docstring
    convention)."""


@dataclass(frozen=True)
class ForcedTransfer:
    """One exact round-t transfer used by the Phase 4 what-if engine.

    The pair is deliberately expressed as an OUT and an IN rather than as
    final-squad membership. That makes the existing transfer continuity,
    bank, free-transfer and hit equations do the accounting unchanged.
    """

    transfer_out: int
    transfer_in: int


@dataclass(frozen=True)
class WhatIfScenario:
    """A current-round counterfactual evaluated against the unconstrained
    optimum. Later rounds remain the solver's diagnostic plan, not forced
    commitments (blueprint §6.1: execute only week t)."""

    forced_transfers: tuple[ForcedTransfer, ...]


@dataclass(frozen=True)
class MultiPeriodResult:
    """`optimise_multi_period`'s solved decision (D9). Round `t`'s (the
    SMALLEST key in the caller's `horizon_candidates`) full decision, in
    EXACTLY `OptimiserResult`'s own shape — same five identity fields, same
    ordering conventions (bench sub-priority order, vice-captain
    convention), same GK-on-bench structural check — plus round `t`'s own
    transfers/hits, plus `plan`: every horizon round's OWN solved state
    (`MultiPeriodRoundPlan`), `plan[0]` always round `t` itself so this
    type's own top-level fields are never the only place to find it."""

    squad_element_ids: tuple[int, ...]
    xi_element_ids: tuple[int, ...]
    bench_element_ids: tuple[int, ...]
    captain_element_id: int
    vice_captain_element_id: int
    transfers_in: tuple[int, ...]  # round t only
    transfers_out: tuple[int, ...]  # round t only
    hits: int  # round t only
    expected_points: dict[int, float] = field(default_factory=dict)  # round t's e_i for every candidate — diagnostic
    objective_value: float = 0.0  # HiGHS's raw objective, including every tie-break/settle term — diagnostic only
    solver_status: str = ""
    plan: tuple[MultiPeriodRoundPlan, ...] = ()  # every horizon round, t first — PLAN, NOT a commitment


@dataclass(frozen=True)
class WhatIfComparison:
    """Unconstrained optimum versus one forced current-round scenario.

    The `*_horizon_*` values intentionally exclude HiGHS tie-break and
    free-transfer settle nudges. They are football-value diagnostics only:
    gross expected points plus the real (negative) transfer hit points.
    """

    optimum: MultiPeriodResult
    scenario: MultiPeriodResult
    optimum_horizon_gross_expected_points: float
    scenario_horizon_gross_expected_points: float
    optimum_horizon_hit_points: int
    scenario_horizon_hit_points: int
    optimum_horizon_net_expected_points: float
    scenario_horizon_net_expected_points: float
    net_expected_points_delta: float  # scenario - optimum; negative means the forced scenario is worse


def _canonical_forced_transfers(
    forced_transfers: Sequence[ForcedTransfer], *, require_nonempty: bool = False
) -> tuple[ForcedTransfer, ...]:
    transfers = tuple(forced_transfers)
    if require_nonempty and not transfers:
        raise OptimiserError("what-if scenario must force at least one current-round transfer")

    outs: set[int] = set()
    ins: set[int] = set()
    for forced in transfers:
        if forced.transfer_out == forced.transfer_in:
            raise OptimiserError(
                f"forced transfer cannot buy and sell the same element={forced.transfer_out}"
            )
        if forced.transfer_out in outs:
            raise OptimiserError(f"duplicate forced transfer_out={forced.transfer_out}")
        if forced.transfer_in in ins:
            raise OptimiserError(f"duplicate forced transfer_in={forced.transfer_in}")
        outs.add(forced.transfer_out)
        ins.add(forced.transfer_in)

    overlap = sorted(outs & ins)
    if overlap:
        raise OptimiserError(
            f"forced transfer element(s) appear in both transfer_out and transfer_in: {overlap}"
        )
    return tuple(sorted(transfers, key=lambda forced: (forced.transfer_out, forced.transfer_in)))


def _validate_forced_current_round_transfers(
    forced_transfers: Sequence[ForcedTransfer],
    *,
    t0: int,
    candidate_ids: Sequence[int],
    incoming_state: SquadState | None,
) -> None:
    if not forced_transfers:
        return
    if incoming_state is None:
        raise OptimiserError(
            f"round {t0}: forced transfers require incoming_state; a free build has no round-t transfer variables"
        )

    candidates = set(candidate_ids)
    held = set(incoming_state.element_ids)
    for forced in forced_transfers:
        if forced.transfer_out not in candidates:
            raise OptimiserError(
                f"round {t0}: forced transfer_out={forced.transfer_out} is not in the candidate pool"
            )
        if forced.transfer_in not in candidates:
            raise OptimiserError(
                f"round {t0}: forced transfer_in={forced.transfer_in} is not in the candidate pool"
            )
        if forced.transfer_out not in held:
            raise OptimiserError(
                f"round {t0}: forced transfer_out={forced.transfer_out} is not held in incoming_state"
            )
        if forced.transfer_in in held:
            raise OptimiserError(
                f"round {t0}: forced transfer_in={forced.transfer_in} is already held in incoming_state"
            )


def optimise_multi_period(
    horizon_candidates: Mapping[int, Sequence[OptimiserCandidate]],
    rules: SquadRules,
    transfer_rules: TransferRules,
    incoming_state: SquadState | None = None,
    config: OptimiserConfig = OptimiserConfig(),
    *,
    forced_transfers: Sequence[ForcedTransfer] = (),
    max_current_round_hits: int | None = None,
) -> MultiPeriodResult:
    """Build and solve the receding-horizon MILP over every round key in
    `horizon_candidates` (D1) — squad/XI/captain per round, transfers/hits/
    free-transfer chain linking consecutive rounds (D4-D6), a rolling
    budget (D5), executing round `t` (the smallest key) only (D9). See the
    module section directly above this function, "Multi-period MILP (Phase
    4, E7, story S8)", for the full constraint-model writeup and every
    design decision this docstring only summarises.

    `incoming_state=None` builds round `t` FREE (D3) — no incoming squad,
    no transfer accounting for that round at all; this is what makes `H=1`
    reproduce `optimise_squad` byte for byte. `incoming_state` given links
    round `t`'s own continuity/budget/free-transfers to that ledger.

    Raises `OptimiserError` on: an empty `horizon_candidates`, an empty or
    duplicate-carrying candidate list for any round, a round whose element
    set or price/position/team disagrees with round `t`'s (D2), an
    under-filled position pool, a non-`kOptimal` solve, or any round's
    solved bench losing its structurally-implied GK slot (mirrors
    `optimise_squad`'s own checks, applied per round)."""
    canonical_forced_transfers = _canonical_forced_transfers(forced_transfers)
    if max_current_round_hits is not None and max_current_round_hits < 0:
        raise OptimiserError(
            f"max_current_round_hits must be >= 0, got {max_current_round_hits}"
        )
    if max_current_round_hits is not None and incoming_state is None:
        raise OptimiserError(
            "current-round hit cap cannot be applied to a free build: "
            "incoming_state=None has no round-t transfer/hit variables"
        )
    if not horizon_candidates:
        raise OptimiserError("optimise_multi_period called with an empty horizon_candidates mapping")

    rounds = sorted(horizon_candidates)
    t0 = rounds[0]

    def _indexed(round_number: int, candidates: Sequence[OptimiserCandidate]) -> dict[int, OptimiserCandidate]:
        ordered = sorted(candidates, key=lambda c: c.element)
        seen: set[int] = set()
        out: dict[int, OptimiserCandidate] = {}
        for c in ordered:
            if c.element in seen:
                raise OptimiserError(f"round {round_number}: duplicate element={c.element} in candidates")
            seen.add(c.element)
            out[c.element] = c
        return out

    by_round: dict[int, dict[int, OptimiserCandidate]] = {
        r: _indexed(r, horizon_candidates[r]) for r in rounds
    }
    base = by_round[t0]
    if not base:
        raise OptimiserError(f"round {t0}'s candidate list is empty")
    ids = sorted(base)

    _validate_forced_current_round_transfers(
        canonical_forced_transfers,
        t0=t0,
        candidate_ids=ids,
        incoming_state=incoming_state,
    )

    # D2, VALIDATED: identical element set and identical price/position/team
    # in every round, against round t0's — see module section above.
    for r in rounds[1:]:
        cur = by_round[r]
        if set(cur) != set(ids):
            missing = sorted(set(ids) - set(cur))
            extra = sorted(set(cur) - set(ids))
            raise OptimiserError(
                f"round {r}'s element set differs from round {t0}'s (D2 requires identical): "
                f"missing={missing}, extra={extra}"
            )
        for i in ids:
            b, c = base[i], cur[i]
            if b.price != c.price or b.position != c.position or b.team != c.team:
                raise OptimiserError(
                    f"element={i}: round {r}'s (price={c.price}, position={c.position!r}, team={c.team!r}) "
                    f"differs from round {t0}'s (price={b.price}, position={b.position!r}, team={b.team!r}) "
                    "— D2 requires identical price/position/team in every horizon round"
                )

    price = {i: base[i].price for i in ids}
    position = {i: base[i].position for i in ids}
    team = {i: base[i].team for i in ids}
    e: dict[tuple[int, int], float] = {
        (i, r): collapse_to_expected_points(by_round[r][i].points_dist) for r in rounds for i in ids
    }  # THE collapse, once per (element, round) — module docstring, "The objective".

    by_position: dict[str, list[int]] = {}
    by_club: dict[str, list[int]] = {}
    for i in ids:
        by_position.setdefault(position[i], []).append(i)
        by_club.setdefault(team[i], []).append(i)

    comp = rules.squad_composition_dict
    xi_bounds = rules.xi_bounds_dict
    for pos, count in comp.items():
        available = len(by_position.get(pos, []))
        if available < count:
            raise OptimiserError(f"not enough {pos} candidates: need {count}, have {available}")

    incoming_ids = frozenset(incoming_state.element_ids) if incoming_state is not None else frozenset()
    purchase_prices = incoming_state.purchase_prices_dict if incoming_state is not None else {}
    sell_proceeds = {
        i: _sell_proceeds_tenths(i, price[i], incoming_ids=incoming_ids, purchase_prices=purchase_prices)
        for i in ids
    }

    h = highspy.Highs()
    h.silent()
    h.setOptionValue("threads", config.threads)
    h.setOptionValue("random_seed", config.random_seed)
    h.setOptionValue("mip_rel_gap", config.mip_rel_gap)
    h.setOptionValue("mip_abs_gap", config.mip_abs_gap)
    if config.time_limit_seconds is not None:
        h.setOptionValue("time_limit", config.time_limit_seconds)

    squad_vars: dict[int, dict] = {}
    xi_vars: dict[int, dict] = {}
    captain_vars: dict[int, dict] = {}
    for r in rounds:
        squad_vars[r] = h.addVariables(ids, type=highspy.HighsVarType.kInteger, lb=0, ub=1, name=[f"squad_{i}_{r}" for i in ids])
        xi_vars[r] = h.addVariables(ids, type=highspy.HighsVarType.kInteger, lb=0, ub=1, name=[f"xi_{i}_{r}" for i in ids])
        captain_vars[r] = h.addVariables(ids, type=highspy.HighsVarType.kInteger, lb=0, ub=1, name=[f"captain_{i}_{r}" for i in ids])

    # D3: with no incoming state, round t0 is a FREE build -- no in/out
    # variables at all for that one round. Every other round (and every
    # round when incoming_state IS given) gets a transfer pair per element.
    transfer_rounds = rounds if incoming_state is not None else rounds[1:]
    in_vars: dict[int, dict] = {}
    out_vars: dict[int, dict] = {}
    for r in transfer_rounds:
        in_vars[r] = h.addVariables(ids, type=highspy.HighsVarType.kInteger, lb=0, ub=1, name=[f"in_{i}_{r}" for i in ids])
        out_vars[r] = h.addVariables(ids, type=highspy.HighsVarType.kInteger, lb=0, ub=1, name=[f"out_{i}_{r}" for i in ids])

    # S11 what-if seam: force the ACTUAL executed-round transfer variables,
    # not squad membership. Every downstream legality/accounting mechanism
    # (continuity, rolling bank, FT spending and hits) therefore remains the
    # exact same model as the unconstrained solve. Forces are t0-only;
    # forward rounds stay diagnostic plans and are re-optimised normally.
    for forced in canonical_forced_transfers:
        h.addConstr(out_vars[t0][forced.transfer_out] == 1, name=f"what_if_out_{forced.transfer_out}_{t0}")
        h.addConstr(in_vars[t0][forced.transfer_in] == 1, name=f"what_if_in_{forced.transfer_in}_{t0}")

    # --- per-round squad composition / formation / captain block --------
    # (module section above: "exactly the t-th slice" of optimise_squad's
    # own single-period block, round-invariant by_position/by_club/comp/
    # xi_bounds reused unchanged for every round, licensed by D2.)
    for r in rounds:
        sv, xv, cv = squad_vars[r], xi_vars[r], captain_vars[r]
        for pos, count in comp.items():
            pool = by_position[pos]
            h.addConstr(h.qsum(sv[i] for i in pool) == count, name=f"comp_{pos}_{r}")
        for club, pool in by_club.items():
            h.addConstr(h.qsum(sv[i] for i in pool) <= rules.max_per_club, name=f"club_{club}_{r}")
        for i in ids:
            h.addConstr(xv[i] <= sv[i], name=f"xi_subset_{i}_{r}")
        h.addConstr(h.qsum(xv[i] for i in ids) == 11, name=f"xi_size_{r}")
        for pos, (lo, hi) in xi_bounds.items():
            pool = by_position.get(pos, [])
            h.addConstr(h.qsum(xv[i] for i in pool) >= lo, name=f"xi_min_{pos}_{r}")
            h.addConstr(h.qsum(xv[i] for i in pool) <= hi, name=f"xi_max_{pos}_{r}")
        for i in ids:
            h.addConstr(cv[i] <= xv[i], name=f"captain_subset_{i}_{r}")
        h.addConstr(h.qsum(cv[i] for i in ids) == 1, name=f"one_captain_{r}")

    # --- continuity, rolling budget (D5), free-transfer/hits chain (D6) --
    hits_vars: dict[int, object] = {}
    ft_available: dict[int, object] = {}  # round -> ft AVAILABLE at that round, before that round's own transfers (constant int or HiGHS var)
    bank_after: dict[int, object] = {}  # round -> bank AFTER that round's own transfers, i.e. opening bank for round+1 (constant int or HiGHS var)
    ft_settle_vars: list = []  # every genuinely-created (non-override) free_transfers_{t+1} variable, for the settle nudge below

    prev_squad = None  # dict[element] -> constant 0/1 or HiGHS var, squad ENTERING the round about to be processed
    for idx, r in enumerate(rounds):
        sv = squad_vars[r]
        has_next = idx + 1 < len(rounds)

        if incoming_state is None and idx == 0:
            # D3: free build. No continuity, no transfers, flat budget cap
            # -- exactly optimise_squad's own single constraint, which is
            # what makes H=1 reproduce it byte for byte.
            h.addConstr(h.qsum(price[i] * sv[i] for i in ids) <= rules.budget_tenths, name=f"budget_{r}")
            prev_squad = sv
            if has_next:
                # Bootstrap for a wildcard-shaped multi-round start beyond
                # H=1 (module section above, "The incoming_state=None case,
                # beyond H=1") -- not this story's gate, but the type must
                # not silently misbehave here. Keyed by `next_r` (rounds[1]),
                # matching every other populate-site's convention below: a
                # dict entry keyed `k` always means "ft available AT round
                # k", never "AT the round that computed it".
                next_r = rounds[idx + 1]
                bank_r = h.addVariable(lb=0, ub=highspy.kHighsInf, type=highspy.HighsVarType.kContinuous, name=f"bank_{r}")
                h.addConstr(bank_r == rules.budget_tenths - h.qsum(price[i] * sv[i] for i in ids), name=f"bank_eq_{r}")
                bank_after[r] = bank_r
                ft_available[next_r] = 0  # bootstrap: nothing was ever banked before a genuinely free build.
            continue

        iv, ov = in_vars[r], out_vars[r]
        for i in ids:
            h.addConstr(iv[i] + ov[i] <= 1, name=f"in_out_exclusive_{i}_{r}")
            prev_i = (1 if i in incoming_ids else 0) if prev_squad is None else prev_squad[i]
            h.addConstr(sv[i] == prev_i + iv[i] - ov[i], name=f"continuity_{i}_{r}")

        transfers_r = h.qsum(iv[i] for i in ids)

        if idx == 0:
            # incoming_state is not None here (the idx==0-and-None case was
            # handled, and `continue`d, above).
            bank_prev = incoming_state.bank_tenths
            ft_prev = incoming_state.free_transfers
        else:
            bank_prev = bank_after[rounds[idx - 1]]
            # `ft_available` is keyed by "the round the value is available
            # AT" (every populate-site below writes under that round's own
            # key, e.g. `ft_available[next_r] = ...`) -- so the value
            # entering THIS round `r` was already populated, under `r`
            # itself, by the PRECEDING iteration's own processing. This is
            # a different indexing convention from `bank_after` just above
            # (keyed by "the round whose transactions produced this closing
            # value", hence looked up via the PREVIOUS round's key) -- found
            # and fixed via this story's own attack test (a constructed
            # banking scenario, forced to decline every t0 transfer, still
            # reported a spurious hit at t0+1 with the original `ft_available
            # [rounds[idx - 1]]` lookup, because that read round t0's OWN
            # incoming free-transfer count a second time instead of the
            # freshly computed value for round t0+1).
            ft_prev = ft_available[r]

        # D5: rolling budget. lb=0 on bank_r IS the "never go negative"
        # guarantee -- no separate inequality duplicates it.
        bank_r = h.addVariable(lb=0, ub=highspy.kHighsInf, type=highspy.HighsVarType.kContinuous, name=f"bank_{r}")
        h.addConstr(
            bank_r == bank_prev + h.qsum(sell_proceeds[i] * ov[i] for i in ids) - h.qsum(price[i] * iv[i] for i in ids),
            name=f"bank_eq_{r}",
        )
        bank_after[r] = bank_r
        ft_available[r] = ft_prev

        # D6: hits_r >= max(0, transfers_r - ft_prev), pinned tight by the
        # objective's own hit_cost pressure (see module section above).
        hits_r = h.addVariable(lb=0, ub=float(rules.squad_size), type=highspy.HighsVarType.kInteger, name=f"hits_{r}")
        h.addConstr(hits_r >= transfers_r - ft_prev, name=f"hits_lb_{r}")
        hits_vars[r] = hits_r

        if has_next:
            next_r = rounds[idx + 1]
            override = transfer_rules.free_transfer_overrides_dict.get(next_r)
            if override is not None:
                # D6: an override SETS next round's free transfers, it does
                # not add -- a plain constant, no variable needed.
                ft_available[next_r] = override
            else:
                # leftover_r = free_transfers_t - transfers_t + hits_t is
                # EXACTLY max(0, free_transfers_t - transfers_t) once hits_t
                # is pinned (module section above) -- no second binary.
                leftover_r = ft_prev - transfers_r + hits_r
                ft_next = h.addVariable(
                    lb=0, ub=float(transfer_rules.max_banked_transfers), type=highspy.HighsVarType.kInteger, name=f"ft_{next_r}"
                )
                h.addConstr(ft_next <= leftover_r + transfer_rules.free_transfers_per_gameweek, name=f"ft_cap_{next_r}")
                ft_available[next_r] = ft_next
                ft_settle_vars.append(ft_next)

        prev_squad = sv

    # S10b diagnostic-only constraint seam. This does NOT change any
    # objective coefficient or hit arithmetic: it only restricts the
    # already-existing current-round hits variable so an audit can compare
    # the chosen plan with the best plan that takes no paid hit. The normal
    # production path leaves this as None and therefore adds no constraint.
    if max_current_round_hits is not None:
        h.addConstr(
            hits_vars[t0] <= max_current_round_hits,
            name=f"diagnostic_current_hits_cap_{t0}",
        )

    # --- objective: E[points] over XI + captain, per round, plus each
    # round's tie-break (D8), plus the FT/hits penalty (D6, sign pinned:
    # hit_cost is NEGATIVE, so `+ hit_cost * hits_r` IS the penalty), plus
    # the tiny ft-settle nudge (module section above). ---------------------
    tie = config.tie_break_eps
    obj_terms = []
    for r in rounds:
        sv, xv, cv = squad_vars[r], xi_vars[r], captain_vars[r]
        for i in ids:
            e_ir = e[(i, r)]
            obj_terms.append(xv[i] * e_ir + cv[i] * e_ir - tie * i * (sv[i] + xv[i] + cv[i]))
    for r in transfer_rounds:
        obj_terms.append(transfer_rules.hit_cost * hits_vars[r])  # hit_cost < 0: a genuine subtraction under maximisation.
    for ft_var in ft_settle_vars:
        obj_terms.append(tie * ft_var)
    h.maximize(h.qsum(obj_terms))

    status = h.getModelStatus()
    if status != highspy.HighsModelStatus.kOptimal:
        raise OptimiserError(
            f"multi-period MILP did not solve to optimality: status={status.name}, "
            f"n_candidates={len(ids)}, horizon_rounds={rounds}"
        )

    def _val(x: object) -> float:
        return float(x) if isinstance(x, (int, float)) else float(h.val(x))

    def _extract_round(r: int) -> MultiPeriodRoundPlan:
        sv, xv, cv = squad_vars[r], xi_vars[r], captain_vars[r]
        squad_ids = tuple(sorted(i for i in ids if h.val(sv[i]) > 0.5))
        xi_id_set = {i for i in ids if h.val(xv[i]) > 0.5}
        captain_ids = [i for i in ids if h.val(cv[i]) > 0.5]
        if len(captain_ids) != 1:
            raise OptimiserError(f"round {r}: solver returned {len(captain_ids)} captains, expected exactly 1")
        captain_id = captain_ids[0]

        e_r = {i: e[(i, r)] for i in ids}
        xi_sorted = tuple(sorted(xi_id_set, key=lambda i: (-e_r[i], i)))
        bench_sorted = tuple(sorted((i for i in squad_ids if i not in xi_id_set), key=lambda i: (-e_r[i], i)))
        if "GK" not in {position[i] for i in bench_sorted}:
            raise OptimiserError(
                f"round {r}: solved bench has no GK -- the structural implication (2 GK in squad, 1 in "
                "XI => 1 on bench) that every SquadRules this codebase declares today satisfies no "
                "longer holds for this rules object (mirrors optimise_squad's own check)."
            )
        vice_candidates = [i for i in xi_sorted if i != captain_id]
        if not vice_candidates:
            raise OptimiserError(f"round {r}: XI has fewer than 2 players -- cannot select a vice-captain")
        vice_id = vice_candidates[0]

        if r in transfer_rounds:
            transfers_in_r = tuple(sorted(i for i in ids if h.val(in_vars[r][i]) > 0.5))
            transfers_out_r = tuple(sorted(i for i in ids if h.val(out_vars[r][i]) > 0.5))
            hits_r_val = int(round(_val(hits_vars[r])))
            ft_avail_val = int(round(_val(ft_available[r])))
        else:
            transfers_in_r = ()
            transfers_out_r = ()
            hits_r_val = 0
            ft_avail_val = 0

        round_points = sum(e_r[i] for i in xi_id_set) + e_r[captain_id]

        return MultiPeriodRoundPlan(
            round=r,
            squad_element_ids=squad_ids,
            xi_element_ids=xi_sorted,
            bench_element_ids=bench_sorted,
            captain_element_id=captain_id,
            vice_captain_element_id=vice_id,
            transfers_in=transfers_in_r,
            transfers_out=transfers_out_r,
            hits=hits_r_val,
            free_transfers_available=ft_avail_val,
            expected_points=round_points,
        )

    plan = tuple(_extract_round(r) for r in rounds)
    current = plan[0]

    return MultiPeriodResult(
        squad_element_ids=current.squad_element_ids,
        xi_element_ids=current.xi_element_ids,
        bench_element_ids=current.bench_element_ids,
        captain_element_id=current.captain_element_id,
        vice_captain_element_id=current.vice_captain_element_id,
        transfers_in=current.transfers_in,
        transfers_out=current.transfers_out,
        hits=current.hits,
        expected_points={i: e[(i, t0)] for i in ids},
        objective_value=float(h.getObjectiveValue()),
        solver_status=status.name,
        plan=plan,
    )


def _horizon_football_value(
    result: MultiPeriodResult, transfer_rules: TransferRules
) -> tuple[float, int, float]:
    """Return (gross expected points, hit points, net expected points).

    This is deliberately not `result.objective_value`: the raw solver
    objective also contains tiny deterministic tie-break / FT-settle terms
    that are useful to HiGHS but meaningless in a user-facing what-if.
    """
    gross = float(sum(round_plan.expected_points for round_plan in result.plan))
    hit_points = int(sum(transfer_rules.hit_cost * round_plan.hits for round_plan in result.plan))
    return gross, hit_points, gross + hit_points


def evaluate_what_if(
    horizon_candidates: Mapping[int, Sequence[OptimiserCandidate]],
    rules: SquadRules,
    transfer_rules: TransferRules,
    *,
    incoming_state: SquadState | None,
    scenario: WhatIfScenario,
    config: OptimiserConfig = OptimiserConfig(),
) -> WhatIfComparison:
    """Compare a forced round-t transfer scenario with the same MILP's
    unconstrained optimum.

    No points or hit arithmetic is reimplemented here. Both arms call
    `optimise_multi_period`; the constrained arm merely pins selected t0
    `in`/`out` binaries to 1. A `-4` therefore appears only when the
    existing free-transfer equations say the forced move actually costs a
    hit. A structurally valid but impossible scenario raises rather than
    relaxing the force.
    """
    forced = _canonical_forced_transfers(scenario.forced_transfers, require_nonempty=True)

    # Reject a bad force before paying for the unconstrained baseline solve.
    # Full horizon/D2 validation still belongs to optimise_multi_period.
    if horizon_candidates:
        t0 = min(horizon_candidates)
        _validate_forced_current_round_transfers(
            forced,
            t0=t0,
            candidate_ids=[candidate.element for candidate in horizon_candidates[t0]],
            incoming_state=incoming_state,
        )

    optimum = optimise_multi_period(
        horizon_candidates,
        rules,
        transfer_rules,
        incoming_state=incoming_state,
        config=config,
    )
    try:
        constrained = optimise_multi_period(
            horizon_candidates,
            rules,
            transfer_rules,
            incoming_state=incoming_state,
            config=config,
            forced_transfers=forced,
        )
    except OptimiserError as exc:
        raise OptimiserError(f"what-if scenario could not be solved: {exc}") from exc

    optimum_gross, optimum_hit_points, optimum_net = _horizon_football_value(optimum, transfer_rules)
    scenario_gross, scenario_hit_points, scenario_net = _horizon_football_value(constrained, transfer_rules)
    return WhatIfComparison(
        optimum=optimum,
        scenario=constrained,
        optimum_horizon_gross_expected_points=optimum_gross,
        scenario_horizon_gross_expected_points=scenario_gross,
        optimum_horizon_hit_points=optimum_hit_points,
        scenario_horizon_hit_points=scenario_hit_points,
        optimum_horizon_net_expected_points=optimum_net,
        scenario_horizon_net_expected_points=scenario_net,
        net_expected_points_delta=scenario_net - optimum_net,
    )


# ---------------------------------------------------------------------------
# Backtest proxy PMF (module docstring, "The backtest proxy PMF, and why").
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EmpiricalPointsDistribution:
    """Backtest-only proxy satisfying `PointsDistributionLike`
    structurally — NOT `fplai.points.PointsPMF` (module docstring, "The
    backtest proxy PMF"): that dataclass requires a `fixture` id this
    module does not have at decision time (`GameweekView.
    current_attributes` carries no `fixture` column — `fplai.backtest.
    replay.ATTRIBUTE_COLUMNS`), and this proxy is a materially coarser
    thing than a six-model composition; giving it the same concrete type
    would misrepresent what produced it."""

    points: tuple[int, ...]
    probabilities: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.points) != len(self.probabilities):
            raise OptimiserError("_EmpiricalPointsDistribution points/probabilities length mismatch")
        total = sum(self.probabilities)
        if abs(total - 1.0) > 1e-6:
            raise OptimiserError(f"_EmpiricalPointsDistribution probabilities sum to {total}, not 1.0")


# Cold-start value (module docstring, "The backtest proxy PMF") — reuses
# fplai.backtest.baselines' own documented degenerate-zero convention
# rather than inventing a new one.
_COLD_START_DISTRIBUTION = _EmpiricalPointsDistribution(points=(0,), probabilities=(1.0,))


def _dense_histogram(draws: Sequence[int]) -> _EmpiricalPointsDistribution:
    """Dense integer-support histogram over real historical per-gameweek
    point totals — same "dense support, zero-filled gaps" convention
    `fplai.points._histogram_to_pmf` uses for its own (differently
    sourced) PMFs, re-declared here rather than imported (that helper is
    private to `fplai.points`, module docstring, "Composition, not
    import"). Precondition: `draws` is non-empty (callers route the empty
    case to `_COLD_START_DISTRIBUTION` before reaching here)."""
    lo, hi = min(draws), max(draws)
    counts = Counter(draws)
    support = tuple(range(lo, hi + 1))
    n = len(draws)
    probs = tuple(counts.get(v, 0) / n for v in support)
    return _EmpiricalPointsDistribution(points=support, probabilities=probs)


def _trailing_points_distributions(
    history: pl.DataFrame, gameweek: int, window: int
) -> dict[int, _EmpiricalPointsDistribution]:
    """One `_EmpiricalPointsDistribution` per element with at least one row
    in the trailing `window` gameweeks of `history` — `fplai.backtest.
    baselines.GreedyFormBaseline`'s own trailing-window filter
    (`round >= gameweek - window`), reused rather than re-derived. Trusts
    `history`'s own leakage guarantee (`fplai.backtest.replay.
    GameweekView.history` is structurally `round < gameweek` — its own
    module docstring establishes this; re-filtering it here would be
    redundant, not safer) exactly the way `GreedyFormBaseline.decide`
    already does. A double/triple-gameweek player's several fixture rows
    within one round are SUMMED to one per-round total first (FPL scores
    per gameweek, not per fixture) — the distribution is over gameweek
    outcomes, one real historical draw per round, not per fixture-row.

    One grouped polars pass over the whole window, not one filter call per
    candidate (this module's own "canonical order" discipline extends to
    performance here too — a per-candidate `.filter()` loop would be
    O(n_candidates) separate scans of a frame that grows every gameweek).
    `maintain_order=True` on the final group-by is the same defensive
    discipline `fplai.points` and this module's own `optimise_squad`
    apply everywhere — Phase 1's `.unique(maintain_order=False)` bug
    (HANDOFF §2) is the reason this project no longer trusts an
    unmaintained group-by order, even where a subsequent `.sort()` would
    likely make it moot.
    """
    window_start = gameweek - window
    window_df = history.filter(pl.col("round") >= window_start)
    if window_df.is_empty():
        return {}
    per_round = (
        window_df.group_by(["element", "round"])
        .agg(pl.col("total_points").sum().alias("total_points"))
        .sort(["element", "round"])
    )
    grouped = per_round.group_by("element", maintain_order=True).agg(pl.col("total_points"))
    out: dict[int, _EmpiricalPointsDistribution] = {}
    for row in grouped.iter_rows(named=True):
        out[int(row["element"])] = _dense_histogram(row["total_points"])
    return out


def _candidates_from_view(view: GameweekView, window: int) -> list[OptimiserCandidate]:
    dists = _trailing_points_distributions(view.history, view.gameweek, window)
    current = view.current_attributes.sort("element")  # defensive re-sort; never trust upstream ordering
    candidates = []
    for row in current.iter_rows(named=True):
        element = int(row["element"])
        candidates.append(
            OptimiserCandidate(
                element=element,
                name=row["name"],
                position=row["position"],
                team=row["team"],
                price=int(row["value"]),
                points_dist=dists.get(element, _COLD_START_DISTRIBUTION),
            )
        )
    return candidates


class _DecisionResultLike(Protocol):
    """Structural shape `_decision_from_result` actually needs. Both
    `OptimiserResult` (single-period) and `MultiPeriodResult` (S8) carry
    every one of these fields under the identical name — widened here
    (S9, D8) so `HorizonStrategy` can hand this function a
    `MultiPeriodResult` directly rather than the caller building a
    throwaway `OptimiserResult` just to satisfy the old, narrower type."""

    squad_element_ids: tuple[int, ...]
    xi_element_ids: tuple[int, ...]
    bench_element_ids: tuple[int, ...]
    captain_element_id: int
    vice_captain_element_id: int
    expected_points: dict[int, float]


def _decision_from_result(
    result: _DecisionResultLike,
    candidates: Sequence[OptimiserCandidate],
    *,
    transfers_in: tuple[int, ...] = (),
    transfers_out: tuple[int, ...] = (),
) -> Decision:
    """Builds a `Decision` from a solved result plus the candidate pool
    identity/price/position is read from — round `t`'s own list for a
    multi-period result (S9, D8), since `Decision.squad` must carry prices
    as of the gameweek actually being decided, never a later horizon
    round's. `transfers_in`/`transfers_out` default to `()`, so every
    pre-S9 call site (`MILPStrategy.decide`, `ModelStackStrategy.decide`)
    constructs unchanged — only `HorizonStrategy` (S9) passes them."""
    by_id = {c.element: c for c in candidates}

    def to_pc(element: int) -> PlayerCandidate:
        c = by_id[element]
        return PlayerCandidate(
            id=c.element, name=c.name, position=c.position, team=c.team, price=c.price,
            value=result.expected_points[c.element],
        )

    squad = Squad(players=tuple(to_pc(i) for i in result.squad_element_ids))
    xi = tuple(to_pc(i) for i in result.xi_element_ids)
    bench = tuple(to_pc(i) for i in result.bench_element_ids)
    captain = to_pc(result.captain_element_id)
    vice_captain = to_pc(result.vice_captain_element_id)
    return Decision(
        squad=squad, xi=xi, bench=bench, captain=captain, vice_captain=vice_captain,
        transfers_in=transfers_in, transfers_out=transfers_out,
    )


class MILPStrategy:
    """Single-period MILP as a `fplai.backtest.replay.Strategy`
    (structural typing — `Strategy` is a `Protocol`; this class is never
    registered against it, matching every baseline in `fplai.backtest.
    baselines`). Re-decided every gameweek, exactly like Template/
    Greedy-form (module docstring, "The backtest proxy PMF") — this is a
    re-optimised squad each week, not buy-and-hold; Phase 4's receding
    horizon is what adds transfer continuity between weeks."""

    def __init__(
        self,
        rules: SquadRules,
        config: OptimiserConfig | None = None,
        trailing_window: int = GREEDY_FORM_TRAILING_GAMEWEEKS,
    ):
        self.name = f"milp(window={trailing_window})"
        self._rules = rules
        self._config = config if config is not None else OptimiserConfig()
        self._window = trailing_window

    def decide(self, view: GameweekView) -> Decision:
        candidates = _candidates_from_view(view, self._window)
        try:
            result = optimise_squad(candidates, self._rules, self._config)
        except OptimiserError as exc:
            raise OptimiserError(f"{view.season} GW{view.gameweek}: {exc}") from exc
        return _decision_from_result(result, candidates)


# ---------------------------------------------------------------------------
# ModelStackStrategy — the real six/seven-model composition (session `s006`,
# task `model-stack-backtest-strategy`). `MILPStrategy` above is UNCHANGED
# and stays the control: same constraint model, same objective-collapse,
# same tie-break, fed a DIFFERENT distribution source. docs/HANDOFF.md §3.5
# names the swap this class makes as the E6 keystone: "MILPStrategy moves
# `_trailing_points_distributions` -> `simulate_fixture_points_pmfs` and
# nothing else in the optimiser changes, because both satisfy the same
# `PointsDistributionLike` Protocol." Verified true here: `optimise_squad`,
# `collapse_to_expected_points`, every constraint, and the tie-break are
# untouched -- this class only builds a DIFFERENT `dict[element,
# PointsDistributionLike]` and hands it to the same `optimise_squad`.
# ---------------------------------------------------------------------------

# FPL's one ISO-8601-with-literal-Z kickoff convention -- the same value
# `fplai.features._KICKOFF_TIME_FORMAT` declares privately, declared here BY
# VALUE rather than imported (this module's own "house vocabulary, declared
# independently" convention, module docstring — POSITIONS above is the same
# posture for a different constant).
_KICKOFF_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_kickoff_time(raw: str) -> datetime:
    return datetime.strptime(raw, _KICKOFF_TIME_FORMAT).replace(tzinfo=timezone.utc)


# `fplai.features._GAP_INPUT_COLUMNS` — declared here BY VALUE, the same
# "house vocabulary, declared independently" posture `_KICKOFF_TIME_FORMAT`
# above already takes, not imported from a private name in another module.
_GAP_INPUT_COLUMN_ORDER = ("season", "team", "fixture", "kickoff_time")

_DECISION_CALENDAR_SCHEMA = {"season": pl.Utf8, "team": pl.Utf8, "fixture": pl.Int64, "kickoff_time": pl.Utf8}


def build_decision_calendar(view: GameweekView) -> pl.DataFrame:
    """S3 pilot (docs/HANDOFF.md, "the forward schedule crosses the
    leakage boundary, and nothing else does"). Fixes the defect this
    story's brief reproduces directly: `fplai.models.minutes._build_team_
    fixture_gap` measures a team's rest gap from whatever kickoffs the
    frame it is handed carries, and `GameweekView.history` alone is frozen
    at `round < t` — for a team whose actual previous fixture was only 1-2
    rounds back, that leaves the gap calculation reading a stale, several-
    rounds-earlier kickoff instead. `_build_team_fixture_gap`'s own
    docstring sanctions supplying more: "kickoff times are public
    pre-deadline, so this is not a leakage boundary".

    Built from `view.fixtures` (round `t`) and every table in `view.
    forward_fixtures` (rounds `t+1..t+H`) — TWO rows per fixture, one per
    side, each carrying that side's OWN team name against the fixture's
    `kickoff_time`. Shaped exactly like `fplai.features._GAP_INPUT_
    COLUMNS` (`season`, `team`, `fixture`, `kickoff_time`) so it can be
    concatenated straight into `_assemble_minutes_feature_row_from_raw`'s
    `gap_input` alongside `raw`/`history` and the target fixture's own
    placeholder row — `kickoff_time` stays the raw FPL string, unparsed;
    `_build_team_fixture_gap` parses it itself, the same convention every
    other caller of that function already follows.

    **Includes rounds AFTER any given target fixture, deliberately**
    (pinned decision D1, this story's brief) — `_build_team_fixture_gap`
    sorts by kickoff and takes `shift(1)` per `(season, team)`, so a LATER
    fixture can never change an EARLIER one's own gap. This is what lets
    ONE calendar, built ONCE per decision, serve every horizon step
    (`_build_candidates`' per-fixture loop) instead of rebuilding it once
    per step or once per player — see that function's own call site for
    the "once per decision" invariant this exists to make cheap enough to
    hold.

    Reads only `view.fixtures` and `view.forward_fixtures` — both already
    schedule-only, pre-deadline-public columns (`replay.py`'s own module
    docstring); this function adds no leakage surface `GameweekView`
    itself does not already carry. Returns an empty, correctly-typed frame
    (never raises) when both are empty — the season-end case, `view.
    forward_fixtures` truncating rather than padding past the season's
    last round (`_build_forward_fixtures`'s own docstring)."""
    tables = [view.fixtures, *view.forward_fixtures.values()]
    sides: list[pl.DataFrame] = []
    for table in tables:
        if table.is_empty():
            continue
        sides.append(table.select(["fixture", "kickoff_time", pl.col("home_team").alias("team")]))
        sides.append(table.select(["fixture", "kickoff_time", pl.col("away_team").alias("team")]))
    if not sides:
        return pl.DataFrame(schema=_DECISION_CALENDAR_SCHEMA)
    # No `.unique()`: every (fixture, side) pair is already unique by
    # construction -- a fixture belongs to exactly one round and appears in
    # exactly one table (`view.fixtures` or one `view.forward_fixtures`
    # round), so a fixture/team pair can never repeat across `tables`.
    # Measured directly across all three gate seasons: max rows removed by
    # `.unique()` across every round = 0 (2023-24, 2024-25, 2025-26). Pilot
    # review found `.unique()` here non-deterministic in row order
    # (`maintain_order` defaults to False) with zero dedup benefit -- the
    # exact `.unique(maintain_order=False)` bug class `docs/HANDOFF.md`
    # records from `_build_view`'s own s003 incident, fixed there the same
    # way: drop the unneeded dedup and make row order a pure function of
    # content via an explicit sort, so correctness does not rest on
    # `pl.concat`'s internal ordering staying accidentally stable.
    combined = pl.concat(sides, how="vertical_relaxed")
    return (
        combined.with_columns(pl.lit(view.season).alias("season"))
        .select(list(_GAP_INPUT_COLUMN_ORDER))
        .sort(["fixture", "team"])
    )


@dataclass(frozen=True)
class ModelStackParams:
    """Everything `ModelStackStrategy` needs to predict ONE gameweek: the
    seven models' own fitted params objects, plus the `DCThresholdSet`
    every DC-eligibility check needs (module docstring's OWN "bundle of 7
    fitted params + threshold_set") — the latter carried on
    `assembly_params.threshold_set` rather than as an eighth top-level
    field, because `fplai.features.assemble_fixture_player_features_
    from_frame` already requires exactly a `FixtureFeatureAssemblyParams`
    (which itself bundles `threshold_set` with the six models' rollup-
    window `*ModelConfig`s) — duplicating `threshold_set` as a second,
    separate field here would only invite the two copies to drift.

    **Fitted entirely by the CALLER, outside this module, as-of that
    gameweek's OWN deadline** (CLAUDE.md rule 2) — this module never calls
    a store, never fits anything, never reads a clock. `ModelStackStrategy`
    trusts `params_by_gameweek` completely; the bitemporal discipline of
    "fit only on data strictly before this gameweek's deadline" is the
    caller's to uphold when building one of these per gameweek, the same
    trust `fplai.backtest.replay.GameweekView.history` already places in
    `SeasonReplay`/`_build_view`'s own leakage boundary rather than
    re-deriving it here."""

    team_strength_params: TeamStrengthParams
    minutes_params: MinutesModelParams
    attacking_params: AttackingModelParams
    dc_params: DCModelParams
    cards_params: CardsModelParams
    bonus_params: BonusModelParams
    saves_params: SavesModelParams
    assembly_params: FixtureFeatureAssemblyParams


def _round_t_attribute_maps(
    current: pl.DataFrame,
) -> tuple[dict[int, str], dict[int, str], dict[int, str], dict[int, int]]:
    """Team/position/name/price, keyed by `element`, read from ONE round's
    `current_attributes` frame — always round `t`'s, per `horizon_candidates`'
    pinned decisions D2/D3 (price/team/position are round `t`'s for every
    horizon round, never projected). Factored out of `ModelStackStrategy.
    _build_candidates` (S3 part 2) so `horizon_candidates` builds these maps
    ONCE, from round `t`, and reuses them for every later round rather than
    re-reading a forward round's `current_attributes` that does not exist."""
    team_by_element: dict[int, str] = {}
    position_by_element: dict[int, str] = {}
    name_by_element: dict[int, str] = {}
    price_by_element: dict[int, int] = {}
    for row in current.iter_rows(named=True):
        element = int(row["element"])
        team_by_element[element] = row["team"]
        position_by_element[element] = row["position"]
        name_by_element[element] = row["name"]
        price_by_element[element] = int(row["value"])
    return team_by_element, position_by_element, name_by_element, price_by_element


def _assemble_round_candidates(
    fixtures: pl.DataFrame,
    *,
    view: GameweekView,
    round: int,
    bundle: ModelStackParams,
    schedule: pl.DataFrame,
    scoring_config: ScoringConfig,
    points_config: PointsSimulationConfig,
    team_by_element: dict[int, str],
    position_by_element: dict[int, str],
    name_by_element: dict[int, str],
    price_by_element: dict[int, int],
) -> list[OptimiserCandidate]:
    """The per-round candidate assembly `ModelStackStrategy._build_candidates`
    used to do inline, factored out (S3 part 2) so `horizon_candidates` can
    call it once per round in `t..t+H` instead of pasting a second copy of
    the fixture loop. `fixtures` is round `round`'s own fixtures table
    (`view.fixtures` for round `t`, `view.forward_fixtures[round]` for a
    forward round) — everything ELSE (`team_by_element` and friends, `bundle`,
    `schedule`) is the CALLER's to hold fixed across rounds per D1/D2/D3/D5/D8;
    this function never reads `view.current_attributes` or `view.
    forward_fixtures` itself, so it cannot silently re-derive a per-round
    element set or a per-round schedule by accident.

    One candidate per element in `team_by_element`, always (pinned decision
    D1) — a player whose team does not appear in `fixtures` (a blank) gets
    a degenerate-0 PMF via `combine_gameweek_points_pmfs`' own empty-list
    branch below, never an omitted candidate; a player whose team appears
    twice (a double gameweek) gets every one of `fixtures`' rows for that
    team folded into one list before the same convolution call, never a
    doubled single-fixture PMF."""
    fixture_pmfs_by_element: dict[int, list[PointsPMF]] = {}
    for frow in fixtures.sort("fixture").iter_rows(named=True):
        fixture_id = int(frow["fixture"])
        home_team = frow["home_team"]
        away_team = frow["away_team"]
        kickoff_time = _parse_kickoff_time(frow["kickoff_time"])

        roster: Roster = {}
        for element, team in team_by_element.items():
            if team == home_team:
                roster[element] = (position_by_element[element], team, True)
            elif team == away_team:
                roster[element] = (position_by_element[element], team, False)
        if len(roster) < 2:
            # No (or only one) rostered candidate on either side of this
            # fixture -- fplai.points.simulate_fixture_points_pmfs' own
            # >= 2 floor; nothing to simulate for this fixture.
            continue

        players = assemble_fixture_player_features_from_frame(
            view.history,
            season=view.season,
            round=round,
            fixture=fixture_id,
            kickoff_time=kickoff_time,
            roster=roster,
            params=bundle.assembly_params,
            schedule=schedule,
        )
        scoreline = predict_scoreline(bundle.team_strength_params, home_team, away_team)
        pmfs = simulate_fixture_points_pmfs(
            fixture=fixture_id,
            scoreline=scoreline,
            minutes_params=bundle.minutes_params,
            attacking_params=bundle.attacking_params,
            dc_params=bundle.dc_params,
            cards_params=bundle.cards_params,
            bonus_params=bundle.bonus_params,
            scoring_config=scoring_config,
            players=players,
            config=points_config,
            saves_predict_fn=make_saves_predict_fn(bundle.saves_params),
        )
        for pmf in pmfs:
            fixture_pmfs_by_element.setdefault(pmf.element, []).append(pmf)

    candidates: list[OptimiserCandidate] = []
    for element in sorted(team_by_element):
        position = position_by_element[element]
        gw_pmf = combine_gameweek_points_pmfs(
            fixture_pmfs_by_element.get(element, ()), element=element, position=position
        )
        candidates.append(
            OptimiserCandidate(
                element=element,
                name=name_by_element[element],
                position=position,
                team=team_by_element[element],
                price=price_by_element[element],
                points_dist=gw_pmf,
            )
        )
    return candidates


class ModelStackStrategy:
    """Single-period MILP fed by the REAL model composition
    (`fplai.points.simulate_fixture_points_pmfs`, via `fplai.features`'
    frame-based assemblers) rather than `MILPStrategy`'s trailing-4-
    gameweek empirical-points proxy — the swap docs/HANDOFF.md §3.5 names
    as the single most valuable next step for the E6 gate ("the backtest
    measured the optimiser using greedy's eyes; it did not measure the
    system"). `MILPStrategy` is left untouched alongside this class, as a
    control: the proxy remains a useful comparison point, per this task's
    brief ("do not delete or alter MILPStrategy").

    **Scoring config is the CALLER's to season-adjust, not this class's**
    (this task's pinned decision 3, resolved): DC training data and the DC
    scoring rule both only exist from 2025-26 onward, so a pre-2025-26
    backtest must be scored with a `ScoringConfig` whose
    `defensive_contribution` mapping is all zeros (`dataclasses.replace`)
    — applying a 2025-26-trained DC model's predictions against a scoring
    rule that could not have paid them out that season. This class accepts
    one `scoring_config` at construction and applies it UNCHANGED to every
    gameweek it decides, deliberately: a `ModelStackStrategy` instance is
    already single-season-scoped by construction (`fplai.backtest.rules.
    SquadRules` is per-season, and `scripts/run_optimiser.py`'s own loop
    already constructs one strategy instance per season) — the caller
    already knows the season at construction time and is in the best
    position to decide whether to neutralise DC, exactly the way it
    already decides `rules_for_season(season)`. Building that decision
    into `decide()` itself would mean this class silently guessing a
    season-to-DC-existence cutoff CLAUDE.md rule 4 says belongs in a
    caller's config, not a hardcoded date comparison here."""

    def __init__(
        self,
        rules: SquadRules,
        params_by_gameweek: dict[int, ModelStackParams],
        scoring_config: ScoringConfig,
        config: OptimiserConfig | None = None,
        points_config: PointsSimulationConfig | None = None,
    ):
        self.name = "model_stack_milp"
        self._rules = rules
        self._params_by_gameweek = params_by_gameweek
        self._scoring_config = scoring_config
        self._config = config if config is not None else OptimiserConfig()
        self._points_config = points_config if points_config is not None else PointsSimulationConfig()

    def decide(self, view: GameweekView) -> Decision:
        # Pinned decision 2: a missing gameweek key RAISES rather than
        # silently falling back to a neighbouring gameweek's params (a
        # silent fallback is a leak vector -- a GW5 decision must never be
        # made on params fitted as-of GW4's or GW6's deadline).
        if view.gameweek not in self._params_by_gameweek:
            raise OptimiserError(
                f"{view.season} GW{view.gameweek}: no params_by_gameweek entry for gameweek "
                f"{view.gameweek} (have {sorted(self._params_by_gameweek)}) -- refusing to fall "
                "back to a neighbouring gameweek's params. See ModelStackStrategy's own "
                "docstring, pinned decision 2."
            )
        # Pinned decision 1 -- THE dangerous case this task's brief names
        # directly: `current_attributes` defaults to an empty frame for a
        # test file outside this task's scope, but `view.fixtures` being
        # empty while `current_attributes` is NOT means the fixture-schedule
        # side of this GameweekView is missing or malformed. Proceeding
        # would treat every candidate as blank (zero fixtures this
        # gameweek), route every one of them through `combine_gameweek_
        # points_pmfs`'s own degenerate-zero branch, and let `optimise_
        # squad` pick arbitrarily among a pool that is genuinely all-tied
        # at 0 -- while raising nothing. Refuse instead.
        if view.fixtures.is_empty() and not view.current_attributes.is_empty():
            raise OptimiserError(
                f"{view.season} GW{view.gameweek}: view.fixtures is empty but current_attributes "
                f"has {view.current_attributes.height} row(s) -- refusing to treat every candidate "
                "as blank. See ModelStackStrategy's own docstring, pinned decision 1."
            )

        candidates = self._build_candidates(view)
        try:
            result = optimise_squad(candidates, self._rules, self._config)
        except OptimiserError as exc:
            raise OptimiserError(f"{view.season} GW{view.gameweek}: {exc}") from exc
        return _decision_from_result(result, candidates)

    def _build_candidates(self, view: GameweekView) -> list[OptimiserCandidate]:
        bundle = self._params_by_gameweek[view.gameweek]
        current = view.current_attributes
        if current.is_empty():
            return []

        team_by_element, position_by_element, name_by_element, price_by_element = _round_t_attribute_maps(current)

        # Built ONCE per decision, never per fixture and never per player
        # (S3 pilot) -- `build_decision_calendar`'s own docstring, "once
        # per decision" invariant. This is what makes a receding-horizon
        # arm's H=1 and H=6 candidates carry identical rest-gap signal for
        # any fixture the two horizons share.
        schedule = build_decision_calendar(view)

        # S3 part 2: the fixture-loop/candidate-assembly body itself now
        # lives in the module-level `_assemble_round_candidates`, shared
        # with `horizon_candidates` below -- a second CALL, never a second
        # implementation. This method's own behaviour is unchanged: round
        # `t`'s fixtures, round `t`, this instance's `scoring_config`/
        # `points_config`.
        return _assemble_round_candidates(
            view.fixtures,
            view=view,
            round=view.gameweek,
            bundle=bundle,
            schedule=schedule,
            scoring_config=self._scoring_config,
            points_config=self._points_config,
            team_by_element=team_by_element,
            position_by_element=position_by_element,
            name_by_element=name_by_element,
            price_by_element=price_by_element,
        )

    def decide_candidates(self, view: GameweekView) -> list[OptimiserCandidate]:
        """Test/diagnostic seam onto the assembled candidate pool, before
        the MILP solve -- lets a caller inspect the real `PointsPMF`/
        `GameweekPointsPMF` objects `decide()` actually optimises over,
        without re-implementing this class's own fixture-grouping and
        leakage-safe assembly. Applies the same pinned-decision-1/2 guards
        `decide()` itself applies, so it never returns a silently-wrong
        pool for a caller that skipped straight to it."""
        if view.gameweek not in self._params_by_gameweek:
            raise OptimiserError(
                f"{view.season} GW{view.gameweek}: no params_by_gameweek entry for gameweek "
                f"{view.gameweek} (have {sorted(self._params_by_gameweek)})."
            )
        if view.fixtures.is_empty() and not view.current_attributes.is_empty():
            raise OptimiserError(
                f"{view.season} GW{view.gameweek}: view.fixtures is empty but current_attributes "
                f"has {view.current_attributes.height} row(s)."
            )
        return self._build_candidates(view)

    def horizon_candidates(
        self, view: GameweekView, *, max_rounds: int | None = None
    ) -> dict[int, list[OptimiserCandidate]]:
        """S3 part 2: one candidate list per round across the receding
        horizon, `t` plus every round present in `view.forward_fixtures`
        (`t+1..t+H`, `H` owned entirely by `_build_view`'s own
        `DEFAULT_FORWARD_HORIZON` -- this method takes no horizon
        parameter of its own). `decide()`/`decide_candidates()` stay
        single-period and unchanged (D6); this is a strict addition, not
        yet consumed by any `Strategy` -- that is story S9's job.

        Every pinned decision below is this method's brief
        (`ModelStackStrategy.horizon_candidates`, S3 part 2), not
        re-litigated here:

        - D1: the ELEMENT SET is identical in every round -- round `t`'s
          `current_attributes`, always. A player blank in a forward round
          gets a degenerate-0 PMF (`combine_gameweek_points_pmfs`' own
          empty-list branch, inside `_assemble_round_candidates`), never
          an omitted candidate.
        - D2/D3: price/team/position are round `t`'s for every round --
          `team_by_element`/`position_by_element`/`name_by_element`/
          `price_by_element` are built ONCE, from `view.current_attributes`
          (round `t`), and reused unchanged for every forward round.
        - D4: `round=round_number` is passed to the assemblers for each
          round -- the honest value; probe (c) showed it is numerically
          inert for a fixed history frame, so this costs nothing.
        - D5: ONE decision calendar, `build_decision_calendar(view)`,
          built once and passed to every round's assembly.
        - D7: the same pinned-decision-1/2 guards `decide()`/
          `decide_candidates()` already apply. A round simply ABSENT from
          `view.forward_fixtures` (season end, an ingestion gap) is not an
          error -- the returned dict is just shorter; never fabricated.
        - D8: `bundle = self._params_by_gameweek[view.gameweek]` -- the
          SAME fitted-as-of-`t` bundle for every horizon round; a
          `params_by_gameweek[t+k]` lookup would leak.

        **S10, D1 (`max_rounds`)**: `max_rounds=None` (the default)
        assembles every round in `view.forward_fixtures`, byte-for-byte
        today's behaviour -- every existing caller keeps working
        unchanged. An int assembles round `t` plus only the FIRST
        `max_rounds - 1` forward rounds in sorted order, skipping the
        `_assemble_round_candidates` call (the expensive step this
        method's own cost-question test measured) for every later round
        entirely, rather than building all of them and truncating
        afterwards. This is what makes a myopic `HorizonStrategy(horizon=
        1)` arm assemble ONE round instead of `H`, at the ~1.0
        ratio-per-round `_assemble_round_candidates` cost this story's
        probe 1 measured. Raises `OptimiserError` for `max_rounds < 1`;
        `max_rounds` larger than the rounds actually available is not an
        error (a season-end horizon is legitimately short, S9's own D3).

        **S9, PHASE 2, D5 (held-player backfill)**: when `view.
        incoming_state` is not `None`, a held element missing from round
        t's own `current_attributes` (a genuine blank -- their club has no
        fixture this round) is added to every attribute map from
        `fplai.backtest.replay.last_known_attributes` before the rest of
        this method runs. `decide()`/`decide_candidates()` never pass
        through this branch and stay byte-for-byte unchanged; S3's
        stateless-mode bit-identity proof for `horizon_candidates(view)[t]`
        vs. `decide_candidates(view)` still holds exactly as proven -- in
        stateful mode round t's list becomes a strict SUPERSET (this
        method's own block above has the full reasoning).
        """
        if max_rounds is not None and max_rounds < 1:
            raise OptimiserError(f"{view.season} GW{view.gameweek}: max_rounds must be >= 1, got {max_rounds}")
        if view.gameweek not in self._params_by_gameweek:
            raise OptimiserError(
                f"{view.season} GW{view.gameweek}: no params_by_gameweek entry for gameweek "
                f"{view.gameweek} (have {sorted(self._params_by_gameweek)})."
            )
        if view.fixtures.is_empty() and not view.current_attributes.is_empty():
            raise OptimiserError(
                f"{view.season} GW{view.gameweek}: view.fixtures is empty but current_attributes "
                f"has {view.current_attributes.height} row(s)."
            )

        bundle = self._params_by_gameweek[view.gameweek]  # D8: one bundle, every round.
        team_by_element, position_by_element, name_by_element, price_by_element = _round_t_attribute_maps(
            view.current_attributes
        )

        # S9, PHASE 2, D5: held-player backfill across a blank gameweek.
        # `ModelStackStrategy.decide`/`decide_candidates` are UNTOUCHED —
        # this block runs only here, and only when `view.incoming_state`
        # is not None (every E6 backtest run was stateless, so E6's own
        # numbers cannot move). A held element missing from round t's own
        # `current_attributes` (their club has no fixture this round — a
        # genuine blank, not an ingestion gap) is added to all four maps
        # from `last_known_attributes` — THE single source of truth this
        # module and SeasonReplay's D6 sell-price fallback both read.
        # Nothing downstream changes: `_assemble_round_candidates` already
        # emits one candidate per element in `team_by_element` for every
        # round, so a backfilled player gets a degenerate-0 PMF at round t
        # (their team plays no fixture there) and a REAL PMF in any forward
        # round their club does play — the same "one candidate per element,
        # always" treatment D1 already gives a blank in a FORWARD round,
        # just now extended to round t itself for a HELD player.
        #
        # S3's proof that `horizon_candidates(view)[t]` is bit-identical to
        # `decide_candidates(view)` remains true in STATELESS mode
        # (`incoming_state is None`, where this block never runs at all).
        # In STATEFUL mode, round t's list becomes a strict SUPERSET of
        # `decide_candidates(view)`'s own pool — a held-but-blank element
        # `decide_candidates` never had reason to include. S10's plan to
        # share the k=0 step between the H=1 and H=6 arms is unaffected:
        # both arms receive the identical `GameweekView` and therefore the
        # identical backfill.
        if view.incoming_state is not None:
            held_missing = frozenset(view.incoming_state.element_ids) - set(team_by_element)
            if held_missing:
                backfilled = last_known_attributes(view.history, held_missing)
                not_found = held_missing - set(backfilled)
                if not_found:
                    raise OptimiserError(
                        f"{view.season} GW{view.gameweek}: held element(s) {sorted(not_found)} are "
                        "absent from round t's current_attributes AND carry no prior history row -- "
                        "cannot backfill (S9, PHASE 2, D5)."
                    )
                for element, attrs in backfilled.items():
                    team_by_element[element] = attrs.team
                    position_by_element[element] = attrs.position
                    name_by_element[element] = attrs.name
                    price_by_element[element] = attrs.price_tenths

        schedule = build_decision_calendar(view)  # D5: one calendar, every round.

        out: dict[int, list[OptimiserCandidate]] = {}
        out[view.gameweek] = _assemble_round_candidates(
            view.fixtures,
            view=view,
            round=view.gameweek,
            bundle=bundle,
            schedule=schedule,
            scoring_config=self._scoring_config,
            points_config=self._points_config,
            team_by_element=team_by_element,
            position_by_element=position_by_element,
            name_by_element=name_by_element,
            price_by_element=price_by_element,
        )
        forward_rounds = sorted(view.forward_fixtures)  # D7: truncated, never fabricated.
        if max_rounds is not None:
            # S10, D1: round t (above) already counts as one of max_rounds
            # -- only the first (max_rounds - 1) forward rounds get an
            # `_assemble_round_candidates` call at all; the rest are simply
            # never built, not built-then-discarded.
            forward_rounds = forward_rounds[: max_rounds - 1]
        for round_number in forward_rounds:
            out[round_number] = _assemble_round_candidates(
                view.forward_fixtures[round_number],
                view=view,
                round=round_number,
                bundle=bundle,
                schedule=schedule,
                scoring_config=self._scoring_config,
                points_config=self._points_config,
                team_by_element=team_by_element,
                position_by_element=position_by_element,
                name_by_element=name_by_element,
                price_by_element=price_by_element,
            )
        return out


# ---------------------------------------------------------------------------
# HorizonStrategy — Phase 4, E7, story S9, PHASE 1. Wires S3's per-round
# candidates (`ModelStackStrategy.horizon_candidates`, or the mechanical
# proxy below), S7's stateful `SquadState` ledger, and S8's receding-
# horizon `optimise_multi_period` together into a `fplai.backtest.replay.
# Strategy` any `SeasonReplay` can run — executing round `t` only (D9),
# `MultiPeriodResult.plan`'s later rounds are diagnostic, never surfaced.
#
# **PHASE 1 landed the wiring; PHASE 2 (this section, as it now stands)
# closes the blank-gameweek continuity gap Phase 1 explicitly deferred.**
# Phase 1's own note here read: "this class assumes, and does NOT itself
# guarantee, that every element `view.incoming_state` holds is present in
# round `t`'s own candidate pool" — Probes 1-2 (S9's brief) showed that
# assumption FALSE across a genuine blank-gameweek boundary
# (`optimise_multi_period` went `kInfeasible` there, 494 elements vanishing
# GW28->29 in 2023-24). Phase 2 closes it with three changes, none of which
# touch `optimise_multi_period`/`optimise_squad` themselves:
#
# - **D5 — held-player backfill**: `fplai.backtest.replay.
#   last_known_attributes` is THE single source of truth for a held
#   player's last-known identity/price; both `HorizonCandidateSource`
#   implementations below (`ModelStackStrategy.horizon_candidates`,
#   `TrailingProxyHorizonSource.horizon_candidates`) call it to add a held-
#   but-blank element to round t's own pool before `optimise_multi_period`
#   ever sees it. `ModelStackStrategy.decide`/`decide_candidates` are
#   UNTOUCHED — every E6 backtest run was stateless, so E6's own numbers
#   cannot move (see that method's own docstring for the full argument,
#   including what this does to S3's stateless-mode bit-identity proof).
# - **D6 — sell-price fallback**: `SeasonReplay.run`'s own
#   `outgoing_current_prices` lookup now falls back to the identical
#   `last_known_attributes` call when a transferred-out id has no row in
#   round t's `current_attributes` (a blanked outgoing player) — still
#   raises, naming the id, if that fallback has nothing either.
# - **D7 — `validate_squad`'s flat budget check is now STATELESS-mode
#   only** (`check_budget`/`validate_budget` parameters, default `True`
#   everywhere so every pre-Phase-2 call site is unaffected): a genuinely
#   held stateful squad can legitimately appreciate past the nominal
#   budget as prices drift. Overspend is still impossible in stateful
#   mode, by two mechanisms this flag never touches — `_advance_state`'s
#   own negative-bank raise and `optimise_multi_period`'s `bank_r`
#   variable's own `lb=0`.
# - **D8 — a Phase 1 robustness gap**: `HorizonStrategy.decide` used to
#   index `rounds[0]` before `optimise_multi_period` got a chance to
#   reject an empty mapping, raising a bare `IndexError` instead of this
#   module's own `OptimiserError`. Fixed at the point of use below.
# ---------------------------------------------------------------------------


class HorizonCandidateSource(Protocol):
    """D2: the candidate source `HorizonStrategy` runs against is INJECTED,
    never hardwired to one concrete provider. `ModelStackStrategy` (above)
    already satisfies this structurally, with no change to its own
    signature — `horizon_candidates` was S3 part 2's own addition, built
    for exactly this consumer. `TrailingProxyHorizonSource` below is the
    second, cheaper implementation this story ships alongside it.

    **S10, D1**: `max_rounds`, keyword-only, added to this Protocol and
    both implementations. `None` (the default) means every round present
    in the source's own view — today's behaviour, byte-for-byte, so every
    pre-S10 caller of either implementation is unaffected. An int means
    assemble only the first `max_rounds` rounds in sorted order (round `t`
    first); a source that ignores it is still correct (`HorizonStrategy.
    decide` truncates the result afterwards regardless), only slower."""

    def horizon_candidates(
        self, view: GameweekView, *, max_rounds: int | None = None
    ) -> dict[int, list[OptimiserCandidate]]: ...


@dataclass(frozen=True)
class TrailingProxyHorizonSource:
    """D2's MECHANICAL HARNESS, not a forecast. Its distribution is
    history-only (`_trailing_points_distributions`, the same leakage-free
    trailing-`window`-gameweek empirical signal `MILPStrategy`/
    `_candidates_from_view` already use) and therefore FIXTURE-BLIND: it
    has no notion of who a team plays in a forward round, so every horizon
    round after `t` simply carries round `t`'s OWN candidate list,
    unchanged — a real fixture could be a blank or a double for a given
    team at `t+2`, and this source would neither know nor care.

    That is a deliberate, stated simplification, not an oversight: the E7
    gate needs a full 38-round STATEFUL season run to prove the wiring
    (`SeasonReplay` + `HorizonStrategy` + `optimise_multi_period`) end to
    end, and a `ModelStackStrategy`-fed season costs ~4.1 hours against
    this source's ~10 minutes (this story's brief, D2) — cheap enough to
    run repeatedly while `HorizonStrategy` itself is being proven, where
    `ModelStackStrategy` is reserved for the real E7 gate (S10). Never
    swap this in for a live decision or a gate-measuring run; it exists to
    make the WIRING cheap to test, not to forecast anything.

    **S9, PHASE 2, D5 (held-player backfill)**: when `view.incoming_state`
    is not `None`, every held element missing from round t's own candidate
    list (a genuine blank — their club has no fixture this round) is
    appended, priced/identified via `fplai.backtest.replay.
    last_known_attributes` (THE single source of truth this source shares
    with `ModelStackStrategy`'s own backfill and `SeasonReplay`'s D6
    sell-price fallback), with a REAL trailing-window PMF from the same
    `_trailing_points_distributions` every other candidate here uses — a
    backfilled player has history, so this is a genuine distribution, not
    a fabricated one; a player with none even in the trailing window falls
    back to the same cold-start convention every other candidate does. The
    appended elements are kept sorted by `element`. This source is
    FIXTURE-BLIND for every player, blanked or not (the paragraph above) —
    the backfill does not change that; it only ensures a held element is
    never simply absent from the pool `optimise_multi_period` sees."""

    window: int = GREEDY_FORM_TRAILING_GAMEWEEKS

    def horizon_candidates(
        self, view: GameweekView, *, max_rounds: int | None = None
    ) -> dict[int, list[OptimiserCandidate]]:
        if max_rounds is not None and max_rounds < 1:
            raise OptimiserError(f"{view.season} GW{view.gameweek}: max_rounds must be >= 1, got {max_rounds}")
        candidates_t0 = _candidates_from_view(view, self.window)
        if view.incoming_state is not None:
            held_missing = frozenset(view.incoming_state.element_ids) - {c.element for c in candidates_t0}
            if held_missing:
                backfilled = last_known_attributes(view.history, held_missing)
                not_found = held_missing - set(backfilled)
                if not_found:
                    raise OptimiserError(
                        f"{view.season} GW{view.gameweek}: held element(s) {sorted(not_found)} are "
                        "absent from round t's own candidate list AND carry no prior history row -- "
                        "cannot backfill (S9, PHASE 2, D5)."
                    )
                dists = _trailing_points_distributions(view.history, view.gameweek, self.window)
                extra = [
                    OptimiserCandidate(
                        element=element,
                        name=attrs.name,
                        position=attrs.position,
                        team=attrs.team,
                        price=attrs.price_tenths,
                        points_dist=dists.get(element, _COLD_START_DISTRIBUTION),
                    )
                    for element, attrs in sorted(backfilled.items())
                ]
                candidates_t0 = sorted(list(candidates_t0) + extra, key=lambda c: c.element)
        out: dict[int, list[OptimiserCandidate]] = {view.gameweek: candidates_t0}
        forward_rounds = sorted(view.forward_fixtures)
        if max_rounds is not None:
            forward_rounds = forward_rounds[: max_rounds - 1]  # S10, D1 -- see class docstring.
        for round_number in forward_rounds:
            out[round_number] = candidates_t0  # D2: fixture-blind — t's own list, unchanged, every round.
        return out


class HorizonStrategy:
    """D1: the `fplai.backtest.replay.Strategy` (structural — `Strategy`
    is a `Protocol`, this class is never registered against it, matching
    `MILPStrategy`/`ModelStackStrategy`'s own convention) that wires S3's
    per-round candidates, S7's stateful ledger, and S8's receding-horizon
    MILP together.

    `horizon` counts rounds INCLUSIVE of `t` (D3): `H=1` is the myopic arm
    (round `t` alone; with an empty/`None` incoming squad this reproduces
    `optimise_squad` byte for byte, per `optimise_multi_period`'s own D3
    gate); `H=6` is `t..t+5`, blueprint §6.1's own horizon length. A
    season-end truncation (`source.horizon_candidates(view)` simply
    returning fewer than `horizon` rounds) is never an error —
    `sorted(...)[:horizon]` degrades gracefully to whatever the source
    actually returned. Raises `OptimiserError` for `horizon < 1`.

    Executes round `t` only (D9) — nothing in `MultiPeriodResult.plan`
    beyond `t` ever reaches the returned `Decision`.

    See module section above (S9, PHASE 2) for the held-player backfill
    (D5), sell-price fallback (D6), stateless-only budget check (D7), and
    the empty-source raise (D8) this class now relies on/exhibits — this
    class's own body needed no change beyond D8's raise, because D5/D6/D7
    all live in its collaborators (`HorizonCandidateSource` implementations
    and `SeasonReplay`)."""

    def __init__(
        self,
        source: HorizonCandidateSource,
        rules: SquadRules,
        transfer_rules: TransferRules,
        horizon: int,
        config: OptimiserConfig | None = None,
        diagnostic_observer: Callable[
            [
                GameweekView,
                Mapping[int, Sequence[OptimiserCandidate]],
                SquadState | None,
                MultiPeriodResult,
            ],
            None,
        ]
        | None = None,
    ):
        if horizon < 1:
            raise OptimiserError(f"HorizonStrategy: horizon must be >= 1, got {horizon}")
        self.name = f"horizon(H={horizon})"
        self._source = source
        self._rules = rules
        self._transfer_rules = transfer_rules
        self._horizon = horizon
        self._config = config if config is not None else OptimiserConfig()
        self._diagnostic_observer = diagnostic_observer

    def decide(self, view: GameweekView) -> Decision:
        incoming = view.incoming_state
        if incoming is not None and len(incoming.element_ids) not in (0, self._rules.squad_size):
            raise OptimiserError(
                f"{view.season} GW{view.gameweek}: incoming_state carries "
                f"{len(incoming.element_ids)} element(s) — expected 0 (a free build) or "
                f"{self._rules.squad_size} (a full squad); a half-built ledger is a caller "
                "bug, not something this strategy interprets."
            )

        # S10, D1: max_rounds=self._horizon lets a source assemble only
        # what this strategy will ever use (the myopic H=1 arm's own
        # motivation) -- the `sorted(...)[: self._horizon]` truncation
        # below is KEPT regardless, defensive belt-and-braces, so a source
        # that ignores max_rounds (or over-returns) can never change what
        # this method actually decides.
        all_candidates = self._source.horizon_candidates(view, max_rounds=self._horizon)
        rounds = sorted(all_candidates)[: self._horizon]  # D3: truncate, never pad past what the source returned.
        if not rounds:
            # S9, PHASE 2, D8: a source returning an empty mapping used to
            # reach `rounds[0]` below and raise a bare IndexError. Raise the
            # module's own OptimiserError instead, naming the decision this
            # source failed to supply anything for.
            raise OptimiserError(
                f"{view.season} GW{view.gameweek}: horizon_candidates source returned an empty "
                "mapping -- nothing to solve."
            )
        truncated = {r: all_candidates[r] for r in rounds}
        t0 = rounds[0]
        candidates_t0 = all_candidates[t0]

        # D4: an empty (or wholly absent) incoming squad IS the free build
        # — mapped to `incoming_state=None` so `optimise_multi_period`
        # builds round t0 with NO continuity/transfer accounting at all
        # (the flat budget cap only), rather than fifteen phantom `in`
        # transfers against a zero-element ledger at ft=0 (fifteen hits,
        # -60 points).
        free_build = incoming is None or not incoming.element_ids
        mp_incoming = None if free_build else incoming

        try:
            result = optimise_multi_period(
                truncated, self._rules, self._transfer_rules, incoming_state=mp_incoming, config=self._config
            )
        except OptimiserError as exc:
            raise OptimiserError(f"{view.season} GW{view.gameweek}: {exc}") from exc

        if self._diagnostic_observer is not None:
            # Expose the exact primary solve AFTER it has completed but
            # BEFORE it is narrowed to Decision. Freeze the mapping and each
            # candidate sequence so diagnostic code cannot mutate the inputs
            # later used to construct the executable decision. Any observer
            # error is intentionally loud: a requested audit must not silently
            # drop evidence and pretend it ran.
            diagnostic_horizon = MappingProxyType(
                {round_number: tuple(truncated[round_number]) for round_number in rounds}
            )
            self._diagnostic_observer(view, diagnostic_horizon, mp_incoming, result)

        if free_build:
            # D4: optimise_multi_period reports transfers_in=() for a free
            # t0 (no transfer variables exist for that round at all under
            # incoming_state=None) — synthesise the transfers this really
            # was, or _validate_transfer_continuity raises (SeasonReplay.
            # run requires decision.squad == previous.element_ids - out +
            # in exactly; a free build's "previous" is empty, so `in` must
            # be the whole squad).
            transfers_in = result.squad_element_ids
            transfers_out: tuple[int, ...] = ()
        else:
            transfers_in = result.transfers_in
            transfers_out = result.transfers_out

        return _decision_from_result(result, candidates_t0, transfers_in=transfers_in, transfers_out=transfers_out)
