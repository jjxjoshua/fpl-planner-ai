# Model: Bonus (BPS)

Phase 2 / E5, session `s004`; L2 scaling fix, session `s005` (see near the
end of this page — a negative result, the fix does not materially move
this model). Blueprint §4's fifth model row: *"Bonus (BPS) — Expected
bonus distribution. Modelled from BPS components, not from historical
bonus alone."* Code: `src/fplai/models/bonus.py`. Tests:
`tests/test_bonus.py` (43 as of the L2 fix). Fit/report script:
`scripts/fit_bonus.py`.

## The design problem

Bonus is a **rank-within-fixture** phenomenon, not an independent
per-player draw: a player earns bonus by finishing in the top 3 on BPS
among every player in his own fixture (both squads, played or not). A
per-player logistic classifier on "did this player get bonus" cannot keep
a fixture's total bonus internally consistent — this module instead fits a
per-player **BPS distribution** (the richer quantity) and **derives** the
bonus PMF by coupling every player in a fixture through a seeded Monte
Carlo simulation of the real rank/tie structure.

## The award rule is pinned from the archive, not press-sourced

Unlike defensive contribution's brand-new 2026/27 count thresholds (which
had no historical archive to check and needed a live-API observation),
bonus scoring mechanics are unchanged FPL rule-book logic, and `bps`/
`bonus` are both complete (zero nulls) across every row this store holds.
The archive itself is the ground truth here.

**What was attacked, live, this session**: the competition-ranking rule
below (`assign_bonus_points`) was applied to every real `(season, round,
fixture)` group's own `bps` values in the store and compared against that
same group's real `bonus` column, across **all 7 seasons**:

```
fixtures checked:      2,569
player rows checked:   179,950
mismatched fixtures:   2
match rate:            99.92%
```

The two exceptions:

1. `2019-20` round 29, fixture 275 — all 59 rows carry `bps=0` but
   `bonus=3`; a known data-quality artefact of the season already flagged
   incomplete elsewhere in this store (no `position`/`team` at all).
2. `2021-22` round 1, fixture 8 — one player at bps=28 (tied with another
   player also at 28) received `bonus=1` where competition ranking
   predicts 0. Not explained by tie logic alone — most likely a genuine
   FPL `overrides` application on that specific fixture (blueprint §11
   already flags this possibility for DC; evidently real for bonus too, at
   least once in 7 seasons). Recorded honestly as an unexplained residual,
   not argued away.

`verify_bonus_award_rule_against_archive(store, as_of=...)` is this check
as a real, callable function, re-run by `tests/test_bonus.py`'s real-store
group and printed by `scripts/fit_bonus.py` — the 99.92% figure is
reproducible from a commit hash, not a stale docstring claim.

**Consequence**: this is BETTER evidence than DC's press citation (drawn
from the archive's own settled scoring, not a rule-book paraphrase). The
award rule's provenance is essentially closed; the only genuinely
uncertain quantity left is the BPS regression + residual distribution that
feeds the simulation, which is where this module's calibration effort
goes.

### The rule itself — competition ranking

```
rank[player] = 1 + count(players with a STRICTLY GREATER bps value)
points = 3 if rank == 1, 2 if rank == 2, 1 if rank == 3, else 0
```

Handles every tie shape without a special case: a 2-way tie for 1st both
score 3 and whoever is next scores 1 (never 2); a 2-way tie for 2nd both
score 2 (no 3rd-place point awarded); a 2-way tie for 3rd both score 1; a
3-way tie for 1st gives all three 3 points (fixture total 9, matching the
brief's observed "9 -> 6 fixtures" figure).

## Why `predict_bonus_pmfs_for_fixture` takes a whole fixture

A deliberate, documented deviation from every sibling module's per-player
`predict_*_pmf(...)` signature. `predict_bonus_pmfs_for_fixture(params,
players: Sequence[BonusPlayerInput], *, fixture, n_simulations, seed)`
returns one `BonusPMF` per player, all derived from the SAME joint Monte
Carlo draw — never independent calls that happen to share a name.

## Composition with `MinutesPMF` — same shape, one layer earlier

No import of `fplai.models.minutes` (composition, not import, the same
discipline attacking/DC establish). `BonusPlayerInput.minute_exposure` is
the same `[(minutes_value, probability), ...]` shape those two modules use.
Unlike DC's multiplicative `log(minutes/90)` offset or attacking's
`minutes_frac * sigmoid(...)`, this module puts `minutes_frac` directly
into the standardised feature vector as an ordinary regression feature —
BPS is not sign-constrained (real range in this store: **-25 to 128**), so
a multiplicative minutes term would force `bps -> 0` unconditionally as
minutes shrinks, which is wrong in principle even though it is correct in
the modal case.

**Consequence**: zero-minute rows are INCLUDED in both the fit and the
fixture-membership universe (a deliberate deviation from DC/attacking's
`minutes > 0` filter) — necessary both because there is no `log(0)`
constraint to avoid, and because the joint simulation genuinely needs the
full competitive universe of a fixture (verified: 96,717 of 96,729
zero-minute rows carry `bps=0`, i.e. 99.99%).

## Feature set

9 standardised numeric features + position one-hot (no team-name dummy,
same promoted-team reasoning DC's own team-style feature uses):
`player_trailing_bps_{3,5,10}`, `team_trailing_bps_mean_5`,
`games_played_this_season`, `cold_start`, `team_cold_start`, `was_home`,
`minutes_frac`. Known-absent: `primary_set_piece_taker`,
`penalty_taker_duty` (same as attacking, for the same reason), plus
`bps_component_breakdown` — this store carries only the aggregate `bps`
number, never the Premier League's own per-action point breakdown.

## Fitting — closed-form ridge, not L-BFGS

`fplai.models.attacking` diagnosed a real bug: `l2_penalty=1.0` on an
unstandardised design matrix converges an ITERATIVE optimiser to a
genuine-but-nonsensical stationary point. This module sidesteps the
convergence half of that risk entirely: minimising `(1/n)||Xβ - y||² +
l2‖β‖²` over a real-valued target (BPS, not a count or a probability) is a
**convex quadratic with a unique closed-form minimiser** — no iteration,
no convergence warning path. `_fit_ridge` is `β = (XᵀX/n + l2·I)⁻¹ · Xᵀy/n`,
cross-checked in `tests/test_bonus.py` both against its own zero-gradient
condition and against an independent `scipy.optimize.minimize` run.

**`l2_penalty=0.01`** (same value attacking settled on) — swept against
the real store, `{1.0, 0.1, 0.03, 0.01, 0.003, 0.001}`, all pass the gate;
0.01 sits within 0.0004 log-loss of the sweep's best (0.001), a safe,
non-minimal choice that avoids the smallest-tested-value risk.

## Residual distribution — empirical bootstrap, bucketed by position

No parametric family assumed. Every training row's real `bps - Xβ`
residual is kept, bucketed by position (verified real spread: GK std 4.46,
DEF 5.60, MID 6.05, FWD 8.97 — materially heteroskedastic, confirming the
bucketing is worth doing), and sampled with replacement (seeded) at
predict/walk-forward time. A position missing from a fitting window falls
back to the pooled cross-position residuals.

## Monte Carlo simulation — vectorised, seeded

`_assign_bonus_points_batch` computes competition rank via one vectorised
pairwise-comparison broadcast over `(n_sims, n_players)` — benchmarked
~14ms for `(2000, 70)` on this machine, comfortably inside this store's
real fixture sizes (mean 71.7, max 115 players/fixture, 2020-21 through
2025-26). Every random draw goes through one `numpy.random.default_rng
(seed)` instance per call — CLAUDE.md rule 7, deterministic and seeded.
BPS is rounded to the nearest integer before ranking (un-rounded floats
would essentially never tie, silently understating the real ~28%
tie-fixture rate this task's brief cites).

## §7.1 walk-forward gate — real numbers, real store, 2026-08-28

Full 6-season window (2020-21 through 2025-26; 2019-20 excluded, no
`position`/`team` upstream), `min_train_rows=2000`, `n_simulations=4000`,
223 folds, 160,992 out-of-sample eval rows, ~86s runtime:

| method | log-loss | Brier |
|---|---|---|
| **model (ridge + MC simulation)** | **0.1881** | **0.0807** |
| baseline: position base rate | 0.2293 | 0.0861 |
| baseline: player trailing rate | 1.0390 | 0.0992 |

**§7.1 GATE: PASSED** — strictly beats both baselines on both metrics
(true 4-class log-loss and multiclass Brier, never a binarised proxy —
CLAUDE.md rule 5).

### Reliability, per outcome (required, not optional — this task's brief)

| outcome (bonus=k) | n | log-loss | Brier | ECE | slope | intercept |
|---|---|---|---|---|---|---|
| 0 | 160,992 | 0.1388 | 0.0382 | 0.0091 | 1.417 | -0.797 |
| 1 | 160,992 | 0.0640 | 0.0142 | 0.0031 | 1.091 | 0.090 |
| 2 | 160,992 | 0.0635 | 0.0141 | 0.0014 | 1.400 | 1.245 |
| 3 | 160,992 | 0.0629 | 0.0141 | 0.0002 | 2.218 | 4.109 |

**Decision, stated per the brief's explicit requirement**: ECE per outcome
(0.0002–0.0091) is well BELOW the minutes-model precedent (pooled ECE
0.0470) that triggered a nested isotonic calibration layer there — on ECE
alone this module would not need one. However, the **Cox calibration
slope departs materially from 1.0** for outcomes 0 and especially 3
(1.417, 2.218) — a real, stated residual, not papered over by the small
ECE. Given the effort budget for this task and that ECE (the brief's own
headline reliability statistic) does not independently corroborate a large
practical miscalibration, **no calibration layer was built this session**
— the same stated-scope-boundary decision `fplai.models.attacking` makes,
not an oversight. ~~**Flagged for a future session**: re-check reliability with quantile-based
bins before deciding whether a calibrator is warranted.~~

**ANSWERED 2026-08-29 by `docs/wiki/calibration-report.md`, and the suspicion above was
right.** Quantile ECE is materially larger than equal-width ECE on **every** bonus outcome
(outcome 0: 0.0091 → 0.0163; outcome 3: 0.0002 → 0.0070). The departure is real, not a binning
artefact: **the slope's signal was correct and the equal-width ECE was the misleading one.**

The same check also corrected the characterisation above — **outcome 2 departs by a material
margin too** (slope 1.398, CI [1.347, 1.450], comparable to outcome 0's 1.415), so this is not
a two-outcome edge effect; it affects at least 3 of the 4 classes.

**Still no calibrator, and now with a stated reason rather than a deferral**: bonus's departure
is *underconfident*, which is the safer direction for an optimiser to inherit than an
overconfident one — a PMF under-claiming its own certainty costs less than one over-claiming
it — and the model clears both baselines by a wide margin (log-loss 0.19 vs 0.23 / 1.04).
Architect ruling: this may ride into Phase 3. **Cards' overconfident departure may not.**

The general lesson outlived this model and is now HANDOFF §4 lesson 10: equal-width ECE
concentrates a rare class's rows into low-probability bins and under-reports a departure living
in a sparse high-probability tail. Report both binnings.

## What this module deliberately did not do

- No isotonic/other calibration layer (see "Reliability" above — measured,
  decided, stated, not silently declined).
- No correlation across DIFFERENT fixtures/players beyond the one fixture
  being simulated (squad-level correlated Monte Carlo across an entire
  gameweek is Phase 5's job, same scope boundary DC's own module docstring
  states).
- No use of `team_strength`'s scoreline PMF — bonus does not need a team
  goals marginal the way attacking's Binomial-thinning does; BPS accrues
  from match events more directly than from goals alone.

## L2 scaling bug fixed — session `s005`, and a negative result worth stating plainly

`_ridge_loss_and_grad`'s loss was `mean(resid**2) + l2 * sum(beta**2)` —
the same defect every sibling model found and fixed this session: an
unscaled penalty against a per-row-averaged loss, effectively `l2*n` for
`n` in the hundreds of thousands. `_fit_ridge`'s closed-form solver
carried the matching bug (`a = xtx + l2*I`, where `xtx` is already
`X^TX/n`). Fixed the same way: both terms now carry `1/n`, and
`_fit_ridge`'s closed form simplifies algebraically to the textbook ridge
solution `(X^T X + l2*I)^-1 X^T y` — see that function's own docstring for
the derivation.

**Unlike attacking, this is a genuine no-op at the shipped default —
report it plainly, not dressed up as a bigger finding than it is.**

### Arm A — the shipped baseline, reproduced at the OLD formula, `l2=0.01`, 223 folds / 160,992 eval rows

| method | log-loss | Brier |
|---|---|---|
| model (OLD formula, shipped) | 0.1882 | 0.0807 |
| baseline: group rate | 0.2293 | 0.0861 |
| baseline: player trailing | 1.0390 | 0.0992 |

(Matches §7.1's shipped 0.1881/0.0807 within float noise — trusted, not
re-derived.) Per-outcome slopes: 0/1/2/3 = 1.415/1.087/1.392/2.243 — match
the shipped 1.417/1.091/1.400/2.218 to within the same noise.

### Corrected formula, swept `l2` from the shrunken default through `1.0`

| l2 | log-loss | Brier | outcome-0 slope | outcome-1 slope | outcome-2 slope | outcome-3 slope |
|---|---|---|---|---|---|---|
| 0.001 | 0.1880 | 0.0807 | 1.407 | 1.083 | 1.388 | 2.196 |
| 0.005 | 0.1879 | 0.0806 | 1.407 | 1.085 | 1.386 | 2.227 |
| 0.01 (shipped) | 0.1879 | 0.0807 | 1.407 | 1.084 | 1.385 | 2.205 |
| 0.05 | 0.1879 | 0.0806 | 1.408 | 1.084 | 1.385 | 2.225 |
| 0.1 | 0.1879 | 0.0807 | 1.406 | 1.086 | 1.387 | 2.222 |
| 0.5 | 0.1878 | 0.0806 | 1.410 | 1.086 | 1.396 | 2.215 |
| 1.0 | 0.1879 | 0.0807 | 1.408 | 1.085 | 1.383 | 2.224 |

Every cell across the entire grid sits within 0.1-0.2% of Arm A's already-
shipped number, with no consistent direction — this is noise-level
movement, not a curve. `beats_both_baselines()` is `True` at every point.
`log_loss`/`Brier`/every outcome's slope are, for practical purposes,
**unchanged by this fix**.

### Why — the loss-scale reasoning, not assumed, worked out

Every OTHER model this session (attacking's Binomial-share, cards'
multinomial, plus minutes/DC before them) fits a LOG-LIKELIHOOD-scale
loss, where `-mean(ll)` sits in a bounded, roughly unit range regardless
of the target — so an unscaled `l2` of a similar order of magnitude
becomes comparable to, or dominates, the averaged likelihood once `n`
grows large, which is exactly the mechanism that crushed those models'
coefficients. This module's ridge loss is different in kind:
`mean(resid**2)` fits BPS directly, a raw-valued target with real,
measured residual variance in the tens (position-bucketed std 4.46-8.97,
§"Residual distribution" above — variance roughly 20-80). At `l2=0.01`,
the OLD unscaled penalty term was never large enough, relative to a loss
already sitting at that scale, to meaningfully distort the fit — the same
architectural defect (an `l2` term missing the `1/n` its loss-mate
carries) was present, but its PRACTICAL severity depends on how the
loss's own natural scale compares to `l2`, not on the architecture alone.
`fplai.models.attacking`'s Binomial-share log-likelihood and `fplai.
models.cards`' multinomial log-likelihood both sit near unit scale, where
`l2=0.01`/`0.001` is directly comparable — that is why the identical bug
mattered there and did not here.

### What changed, what did not

- **Fixed**: `_ridge_loss_and_grad`'s ridge term and `_fit_ridge`'s
  closed-form solution, both now correctly `1/n`-scaled.
- **Unchanged**: `BonusModelConfig.l2_penalty` default (`0.01`) — no
  retuning needed or indicated; the corrected formula's own sweep is flat
  across the entire tested range.
- **Unchanged**: the §7.1 gate verdict (`PASS`) at every point of the
  swept grid.
- **A negative result, stated as a deliverable, not a disappointment**
  (CLAUDE.md lesson 9): this fix, real and correctly applied, simply does
  not matter for this specific model at this specific scale. Measuring
  that and saying so plainly is the honest output of this task for this
  module.
- **Verified against the real store**: `scripts/fit_bonus.py`'s own
  walk-forward path: `tests/test_bonus.py::
  test_ridge_l2_penalty_is_scaled_by_inverse_n` and `::
  test_fit_ridge_closed_form_matches_standard_textbook_ridge_solution`
  (two independent pinning tests, both hand-verified to FAIL against a
  temporarily reverted copy of the old formula before being trusted,
  CLAUDE.md lesson 5), full suite `uv run pytest tests/test_bonus.py -q`
  — 43 passed.
