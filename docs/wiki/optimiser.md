# Phase 3 — the single-period MILP optimiser (E6)

> Author: XL-Coder · session `s005` · 2026-08-30/31
> Implements blueprint §6.1 (single-period slice) and the Phase 3 deliverable in §7 ("beat the
> template over 2+ backtested seasons"). Code: `src/fplai/optimiser.py`. Tests:
> `tests/test_optimiser.py` (23, measured `09-03` via `--collect-only`). Script: `scripts/run_optimiser.py`.
> Consumes S-Coder's graphify where relevant — none existed for this area at pickup time.

## 0. Gate status — PASSED `09-03` (honest re-run, real model stack)

**CORRECTED `09-03` (session s006).** This section originally reported the E6 gate as *not
met*, and that was wrong about the **outcome**, not just imprecise like §4 was about cost — a
reader who stops here, which is most readers, walked away believing the opposite of what
`PROGRESS.md` now records. The gate has since been re-run with the real six-model stack (via
the new `ModelStackStrategy`, §4) rather than the trailing-`total_points` proxy the table
below used, and it **passes**:

| season | model_stack | MILP proxy | template | margin |
|---|---:|---:|---:|---:|
| 2023-24 | 2221 | 1948 | 2092 | **+129** |
| 2024-25 | 2392 | 2033 | 1906 | **+486** |
| 2025-26 | 2228 | 1945 | 1951 | **+277** |

Mean **2,280.3** vs template **1,983.0**, +297/season, seed 0, commit `495a168`
(`PROGRESS.md`, `data/gate/e6_gate_results.jsonl`). Lowest season (2,221) clears both the
~2,100 bar and the 2,146 career-average reference this page's original standard cared about.

**Four caveats travel with this number and must not be dropped or softened if this section is
read in isolation:** the backtest re-decides weekly and **pays no transfer costs** — the
template is measured identically, so the *gate* is sound, but 2,280 is **not** a real-season
forecast; the window is **three seasons, not four** (2022-23 is unfittable — the `starts`
label begins *in* it, not season-picking); **2025-26 GW1 ran with DC forced ineligible**
(`dc_cold_start_gameweeks=[1]`); and the objective is **E[points], not the rank-aware
objective** blueprint §10 needs (Phase 5).

**The proxy-era reasoning directly below was honestly obtained and its central call was
right, not wrong — preserved for that reason, not deleted.** "Very likely a signal-quality
ceiling, not a solver defect" (§0.1, unchanged) is now a measured fact rather than a hedge:
fed the identical trailing-`total_points` proxy PMF, `model_stack` beats `MILPStrategy`'s own
proxy result by **+273 to +359 points per season, in every one of the three re-run seasons**
(2221−1948=273, 2392−2033=359, 2228−1945=283) — same optimiser, same constraints, same
solver, same tie-break, only the input distribution changed. That is exactly the ceiling this
section originally predicted, confirmed once the real signal became available to test it
against.

### 0.1 Original assessment (pre-swap, proxy PMF only) — preserved, not deleted

**Do not read this as "E6 passed."** Read literally ("beat the template over 2+ backtested
seasons," counting individual season wins), the gate is met: MILP beats `TemplateBaseline` in
**3 of 6** backtested seasons. Read against the standard the rest of this document (and
`docs/HANDOFF.md` §2) actually cares about — the ~2,100/2,146 bar, and the seasons the user
can be directly compared against — **it is not met**: over the three seasons with a real
human reference score, MILP totals **5,926** against template's **5,949** (MILP behind by
23), and never gets within 130 points of the 2,146 career average in any of them.

| Season | MILP | Template | Greedy-form | MILP beats template | Human reference |
|---|---:|---:|---:|---|---:|
| 2020-21 | 1,989 | 1,895 | 1,945 | **True** (+94) | — |
| 2021-22 | 2,107 | 2,060 | 2,122 | **True** (+47) | — |
| 2022-23 | 1,992 | 2,078 | 2,011 | False (−86) | — |
| 2023-24 | 1,948 | 2,092 | 2,043 | False (−144) | 2,169 (MILP −221) |
| 2024-25 | 2,033 | 1,906 | 1,954 | **True** (+127) | 2,251 (MILP −218) |
| 2025-26 | 1,945 | 1,951 | 1,889 | False (−6) | 2,019 (MILP −74) |

Reproduce with `uv run python scripts/run_optimiser.py`. Live-verified against the real
`data/store/`, not a fixture. This table is the six-season proxy-only run this page was
originally built on and is superseded by the three-season honest re-run above — kept for the
record and because the reasoning it fed into (below) turned out correct.

**This is very likely a signal-quality ceiling, not a solver defect** — see §3.

---

## 1. Scope — single-period only

Blueprint §6.1 describes a receding-horizon MILP over gameweeks *t..t+5*, with transfers,
hits, and chips. **That is Phase 4 (E7), not this story.** This module solves ONE gameweek in
isolation: 15 players, a valid XI, bench order, and a captain — no transfer variables, no
free-transfer/hit accounting, no chip logic, no continuity constraint linking gameweek *t* to
*t-1*. §5 below states exactly what a multi-period extension needs and why the constraint
model here was shaped so that extension is additive.

## 2. The constraint model

Three binary decision-variable families, one per candidate `i` (the FPL `element` id, used
directly as the `highspy` variable key):

- `squad_i` — one of the 15.
- `xi_i <= squad_i` — one of the starting 11.
- `captain_i <= xi_i` — the captain.

```
sum(price_i * squad_i)                     <= budget_tenths
sum(squad_i for i in position p)           == squad_composition[p]      for every position p
sum(squad_i for i in club c)               <= max_per_club              for every club c
sum(xi_i)                                  == 11
xi_bounds[p][0] <= sum(xi_i for i in p)    <= xi_bounds[p][1]           for every position p
sum(captain_i)                             == 1
```

Every constant (`budget_tenths`, `squad_composition`, `xi_bounds`, `max_per_club`) is read
from the caller's `fplai.backtest.rules.SquadRules` (CLAUDE.md rule 4) — for the backtest,
`rules_for_season`'s own declared-assumption historical rules (its docstring explains at
length why there is no live `game_config` for a closed season to read instead); a live weekly
caller should prefer `fplai.scoring.load_scoring_config`-style live config the same way
`scoring.py` already does. This module is agnostic to which `SquadRules` it is handed.

## 3. The objective — where the PMF collapses, and how Phase 5 replaces it

`collapse_to_expected_points(dist: PointsDistributionLike) -> float` is the **entire**
collapse: `sum(p * prob for p, prob in zip(dist.points, dist.probabilities))`. It is called
exactly once per candidate, at the very end of `optimise_squad`, immediately before the
objective coefficients are assembled — nowhere else in the module reads `.points`/
`.probabilities`. Every constraint above is built from price/position/team only.

```
maximize  sum(xi_i * e_i)  +  sum(captain_i * e_i)  -  tie-break terms
```

`e_i = collapse_to_expected_points(candidate_i.points_dist)`. The captain's extra `e_i` is a
genuine second term, gated by `captain_i <= xi_i`, so captaincy can only ever double a player
already in the XI — exactly FPL's own rule. **Captain is a decision variable, not "solve for
XI then pick the highest scorer"**: both are decided by the same solve, over the same
constraints, so a marginal player whose captaincy upside is exceptional can tip which 11-of-15
gets chosen whenever budget/formation binds — the naive two-step shortcut cannot see that.

**Phase 5's rank-aware objective (E8) replaces exactly one thing**: `collapse_to_
expected_points` (and its one call site), with something reading a different property of the
same `PointsDistributionLike` — a percentile, an EO-weighted rank value, a downside-adjusted
CVaR. No variable, no constraint, no solver option changes, because none of them ever look at
the distribution shape — only at the scalar this function hands them.

`PointsDistributionLike` is a duck-typed `Protocol` (`.points`, `.probabilities` — the exact
two field names `fplai.points.PointsPMF` already carries), not a hard dependency on that
concrete dataclass — see §4.

## 4. The backtest proxy PMF — the load-bearing limitation

**This is the single most important thing to understand about why §0.1's numbers look the way
they do.** `fplai.points.simulate_fixture_points_pmfs` (the real six-model composition) is
the intended production input, and it satisfies `PointsDistributionLike` structurally with no
adapter needed. But wiring it into a backtest needs a live/historical **feature-row
pipeline** — building each of the six models' own `numeric_columns` feature vocabulary, as of
every historical gameweek's own deadline, for every player — and that pipeline does not exist
(`docs/HANDOFF.md` §3, "Phase 3 has two unwritten prerequisites"; `fplai.points`'s own module
docstring, "What this module does NOT do"). Building it was explicitly out of this task's
OWNED PATHS (model files are READ-ONLY) and is a project on the same order of size as the six
models themselves — not attempted here.

**What `MILPStrategy` uses instead**: a genuine empirical points distribution built from
`view.history`'s trailing `GREEDY_FORM_TRAILING_GAMEWEEKS` (4) gameweeks of the player's own
stored `total_points` — the SAME leakage-free signal `fplai.backtest.baselines.
GreedyFormBaseline` already uses, histogrammed into a real, dense-support `_EmpiricalPoints
Distribution` (never a scalar dressed up as a PMF — rule 5). Cold start (no trailing
history) falls back to the same documented degenerate-zero value `baselines.py` already uses.

**This is a stated, argued proxy, not a silent stand-in.** It exists so the OPTIMISER itself
— the constraint model, the objective-collapse step, captain-as-a-variable, deterministic
tie-breaking — could be built and gated against real backtested seasons without waiting on
the (much larger) live-pipeline story. §0.1's mixed result is consistent with this: MILP and
`greedy_form` are fed the **identical** signal, and they swap which is ahead season to season
by amounts (see the table in §0.1) that look like noise in a shared weak signal, not a
systematic solver edge — `greedy_form` beats MILP in 2022-23 and 2023-24 despite being a
heuristic, not a certified-optimal solve, which is exactly what you'd expect if the signal,
not the algorithm, is the bottleneck. **CORRECTED `09-03` (session s006) — the paragraph below, as originally written, was
half true and half falsified by this session's own record.** What is still true, and worth
keeping emphatically: nothing in `optimise_squad`, `collapse_to_expected_points`, the
constraints, or the tie-break changed when the swap actually happened — the diff to
`optimiser.py` has zero removed lines, purely additive, and the `PointsDistributionLike`
Protocol's central claim held exactly as designed. What was false: the implication that the
swap was therefore *small*. It reads that way only from `optimise_squad`'s own interface;
two things the interface could not see turned out to gate the swap, and *reaching* the point
where the one call could be made cost **~13 story points across four dispatches, not the ~3
this framing implied** (`PROGRESS.md`, "MILP swap RE-GROOMED `09-03` — it is ~13, not 3, and
probing is what showed it"), because:

1. **`MILPStrategy.decide(view)` has no store.** `replay.py` states outright that a strategy
   has no attribute through which it could reach `SeasonData` or the store directly — that is
   the structural leakage guarantee this whole harness is built around, not an oversight.
   Feeding `simulate_fixture_points_pmfs` therefore needed frame-based feature assembly (seven
   `*_from_frame` entry points in `fplai.features`) built entirely from `view.history`, not a
   deadline-bounded store handle threaded in for convenience.
2. **`GameweekView` could not say which fixture a player is in.** `ATTRIBUTE_COLUMNS` was
   `(element, name, position, team, value)` — no `fixture`, `was_home`, `kickoff_time`, or
   opponent — while `simulate_fixture_points_pmfs` works per fixture. That required extending
   the leakage boundary itself (a dedicated dispatch, attacked both for outcome-column leakage
   and for deterministic double-gameweek dedup) and adding a separate `GameweekView.fixtures`
   field rather than duplicating rows in `current_attributes`.
3. **Double-gameweek handling**, which the old trailing-`total_points` proxy got for free
   (it is already per-gameweek and includes both fixtures) but the real six-model stack does
   not — needed its own convolution step (`combine_gameweek_points_pmfs`) and its own dispatch
   to land before the gate could be trusted.

The original one-line framing was quoted in a brief and led to an estimate roughly half the
actual cost. §0.1's table (this page's original gate table) was run against the trailing-
`total_points` proxy, before the swap; the swap has since happened, and the honest re-run
gate result, using the real six-model stack, is:

| season | model_stack | proxy | template | margin |
|---|---:|---:|---:|---:|
| 2023-24 | 2221 | 1948 | 2092 | +129 |
| 2024-25 | 2392 | 2033 | 1906 | +486 |
| 2025-26 | 2228 | 1945 | 1951 | +277 |

Mean **2,280.3** vs template **1,983.0**, seed 0, commit `495a168` (`PROGRESS.md`,
`data/gate/e6_gate_results.jsonl`). Four caveats travel with this number and must not be
dropped or paraphrased into something weaker: **no transfer costs** (re-decided weekly, so
this is not a real-season forecast — the template is measured identically, so the gate itself
is sound); **three seasons, not four** (2022-23 is unfittable — the `starts` label begins
*in* it, not season-picking); **2025-26 GW1 ran with DC forced ineligible**
(`dc_cold_start_gameweeks=[1]`); and the objective is **E[points], not the rank-aware
objective** blueprint §10 needs, which is Phase 5's job. This same outcome table also now
opens §0, since the honest gate result is the page's headline and most readers stop there —
see §0 for the outcome-focused version and the pre-swap table preserved as §0.1; this section
stays focused on what the swap actually cost to reach, not on restating the result twice in
identical form.

## 5. Captain, vice-captain, bench — what is modelled and what is stated as a simplification

- **Captain**: a real MILP decision variable (§3). Verified in `tests/test_optimiser.py::
  test_captain_is_the_highest_expected_points_candidate_actually_selected_into_the_xi`.
- **Vice-captain is NOT a decision variable.** FPL's vice only pays out if the captain records
  zero minutes — resolving that properly needs the minutes model's own `P(state=UNUSED)`,
  which the trailing-points proxy cannot separate from "played and scored 0." Vice is chosen
  post-hoc: highest-`e_i` XI member other than the captain, ties broken by lowest `element`
  id — reusing `fplai.backtest.squad.choose_xi_bench_captain`'s own `(-value, id)` convention
  rather than inventing a second one.
- **Bench order is a stated simplification, not an optimised decision.** Bench players
  contribute nothing to this module's objective (only `xi_i`/`captain_i` carry `e_i`) —
  properly valuing a bench slot needs `P(a specific starter records 0 minutes) *
  (bench player's expected points | subbed on)`, out of scope per this task's brief. The 4
  non-XI squad members are ordered by `(-e_i, element_id)` — highest expected points first,
  matching `choose_xi_bench_captain`'s own bench-order convention. "At least one GK sits on
  the bench" is not a separate constraint — it falls out of the existing composition/formation
  constraints for every `SquadRules` this codebase declares today (2 GK in squad, 1 in XI)
  — but `optimise_squad` asserts it explicitly after solving and raises `OptimiserError`
  rather than silently shipping a GK-less bench if a future ruleset ever broke the implication.

## 6. Determinism (CLAUDE.md rule 7) — how a tied MILP was made deterministic, and how it was proven

**HiGHS alone, single-threaded with fixed options, is already deterministic given identical
input** — verified live before writing production code: a 40-candidate all-tied synthetic
knapsack (`threads=1`) returned the identical chosen set across 8 repeated solves. But
"stable run to run" is not the same as "an explicable, designed tie-break" — without one, the
winning vertex among true ties is whatever HiGHS's internal branch-and-bound order happens to
prefer, an implementation detail this project should not depend on (the exact "stable on the
current layout, would silently break on a change" fragility that reopened the Phase 1 gate,
`docs/HANDOFF.md` §2, on a different layer).

**The fix: an explicit tie-break**, `- tie_break_eps * element_id` added to every objective
coefficient at all three variable layers (`squad_i`/`xi_i`/`captain_i` independently — a
squad-level tie needs breaking independently of an XI-level tie, independently of a
captain-level tie, even though the three are linked by `<=`), favouring the lowest `element`
id — the same `(gain, -id)`-favours-lowest-id convention `fplai.backtest.squad.build_squad`'s
hill-climb already established for an analogous bug. `tie_break_eps` defaults to `1e-6`; the
largest possible perturbation to any one candidate (`3 * tie_break_eps * max(element_id)`,
worst case all three layers apply) is several orders of magnitude below the smallest
resolvable `e_i` gap either distribution source in this codebase can produce. `threads` is
pinned to `1` (never left at HiGHS's own `0`/"choose" — verified live that the default is
hardware-dependent auto-selection) and `mip_rel_gap`/`mip_abs_gap` are tightened to `1e-9`.

**Proven, not assumed** (`tests/test_optimiser.py`):

- `test_optimise_squad_is_bit_identical_across_repeated_calls_with_genuinely_tied_candidates`
  — every candidate sharing an IDENTICAL distribution (the worst case for a solver's own
  internal tie-break), 8 repeated solves, every field of the result bit-identical.
- `test_the_tie_break_favours_the_lowest_element_id_by_design_not_by_solver_accident` — the
  discriminating test. Asserts the SPECIFIC winner (not just repeatability), and was proven
  fail-first live before being written: with `tie_break_eps=0.0` on the identical candidate
  pool, the solver still returned a stable answer across repeats (captain id 15, both times)
  but NOT the lowest-id answer this test asserts (captain id 0 with the real tie-break) —
  proving the assertion discriminates "the designed rule" from "stable but unexplained solver
  behaviour."
- `test_milp_strategy_is_reproducible_across_repeated_runs_on_the_real_store` — mirrors
  `tests/test_backtest.py::test_greedy_form_baseline_is_reproducible_across_repeated_runs_on_
  the_real_store` exactly, live against `data/store/`, 3 fresh-store runs, bit-identical
  totals.

## 7. What a multi-period extension (Phase 4, E7) needs

Stated in the module docstring in full; summarised here:

- Index every variable by gameweek: `squad_{i,t}`, `xi_{i,t}`, `captain_{i,t}`.
- A squad-continuity link: `squad_{i,t} = squad_{i,t-1} + in_{i,t} - out_{i,t}`, with new
  transfer binaries and `sum(in) == sum(out)` per gameweek.
- A rolling budget carrying banked money forward.
- A free-transfer/hit accounting layer — a running `free_transfers_t` state and a
  `-4 * max(0, transfers_t - free_transfers_t)` penalty (a genuine `max`, needs the standard
  MILP linearisation — an auxiliary variable plus two inequalities, not a new idea).
- Chip binaries (wildcard/bench-boost/triple-captain/free-hit, read from live config, never
  hardcoded — blueprint §11) that relax or modify specific constraints for exactly one `t`.
- The objective sums `collapse_to_expected_points` over every `(i, t)` pair the horizon
  touches — this module's collapse function is already the right shape; only the summation
  index grows.
- This module's existing single-`t` constraint block becomes exactly the `t`-th slice of the
  multi-period model — it does not change shape, only gets repeated and linked.

## 8. What was NOT done, and why

- **The real six-model `PointsPMF` was not wired into the backtest.** §4 explains why at
  length — the live/historical feature-row pipeline is a separate, much larger prerequisite,
  explicitly out of this task's owned paths (model files are READ-ONLY).
- **No tuning against the test seasons.** The trailing window (`GREEDY_FORM_TRAILING_
  GAMEWEEKS = 4`) is the SAME already-documented assumption `greedy_form` uses, imported
  rather than re-invented or adjusted after seeing §0.1's numbers — per the task brief's
  explicit instruction and the same discipline `docs/wiki/phase1-baselines.md` §5 already
  states for the Phase 1 baselines ("nothing here was adjusted after seeing the numbers").
- **Multi-period, transfers, hits, chips** — Phase 4/E7, see §7.
- **`src/fplai/points.py`, `src/fplai/calibration.py`, and every model file** were read-only
  for this task and were not touched.

## 9. Test suite

```
$ uv run pytest tests/test_optimiser.py -q
23 passed   # measured 09-03; the +3 are ModelStackStrategy's own tests
            # (the "26 passed" in PROGRESS.md is test_optimiser.py AND
            #  test_backtest.py together, not this file alone)
```

Full-suite run before/after this story: see `docs/HANDOFF.md`/punch-card for the exact
before/after count — this story only ADDS `tests/test_optimiser.py`; no existing test file
was modified.
