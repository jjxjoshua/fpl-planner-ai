# Model: Goalkeeper Saves

Phase 3 prerequisite, session `s005` — the **seventh, and last, outcome
model**: "the last gap in goalkeeper points." Blueprint §4's goalkeeper
points row. Code: `src/fplai/models/saves.py`. Tests: `tests/test_saves.py`
(36 as of the isotonic-saturation fix below, includes a real-store-gated
group). Fit/report script: `scripts/fit_saves.py`.

> **Consolidation note, later session `s005`:** `IsotonicCalibrator`/
> `_fit_isotonic_calibrator` — described below as this module's own code —
> were moved to the shared `fplai.calibration` module once the identical
> mechanism (and the identical Jeffreys-smoothing fix described below)
> turned up independently in `fplai.models.minutes` and `fplai.models.cards`
> too. Proven bit-identical (three copies diffed line-by-line first, then
> re-run against the real store before/after: `calibrate=False` still ships,
> RAW still beats CALIBRATED on every pooled metric, unchanged) — every
> number, test name and line reference below is unchanged in behaviour;
> only the import source moved. `fplai.models.saves` still exposes
> `IsotonicCalibrator`/`_fit_isotonic_calibrator` as re-exports for backward
> compatibility, so every reference below still resolves.

## Why this model was built — the evidence, measured before the work was
## authorised

Saves are **18.9% of all GK points** (2,999 of 15,893, across 4,587 GK
appearances, 6 seasons). Ex-post, rank correlation of GK season totals with
vs without saves is 0.944–0.985 and the top GK is usually unchanged — **not
the reason this was built**, because the optimiser ranks on *predicted*
points, not realised ones. The deciding measurement: pooled
`corr(saves/app, clean_sheets/app) = -0.527`, negative in all six seasons
individually. Omitting saves removes **0.762 pts/app from bad-defence
keepers but only 0.552 from good-defence keepers** — a systematic
**~0.21 pts/app differential**, ~8 pts/season, always favouring premium
keepers on strong defences, which distorts budget allocation across the
other 14 squad slots.

## Data verified live, this session

`saves` is non-null for every row in all 7 seasons (179,950 rows). This
module trains on 2020-21..2025-26 (`position`/`team` absent for 2019-20,
same gap every sibling model documents), `position == "GK"`,
`minutes > 0`: **4,587 real GK appearances**.

```
mean saves/app:   2.965
variance:         3.792
var/mean:         1.279   (Poisson implies exactly 1.0)
skewness:         +0.807  (right-skewed)
max observed:     13
```

var/mean = 1.279 is a real, modest departure from Poisson, positive in
every one of the six seasons checked individually (1.24–1.45), never
crossing below 1 — the reason this module is a **Negative Binomial (NB2)**,
measured directly rather than assumed (the DC model's own near-Gaussian
placeholder was assumed and turned out measurably wrong — this session
checked rather than inherited that lesson).

**A real, checked, non-blocking anomaly**: 22 of 145,317 non-GK rows carry
`saves > 0` (0.015%) — almost certainly an outfield player finishing a
match in goal. `build_training_table` filters to `position == "GK"` only.

## Opponent attacking strength — consumed from `team_strength`, not
## rebuilt as a parallel feature

Unlike `defensive_contribution`/`cards`, which each build their own
team-trailing-style feature, this task's brief was explicit: `fplai.
models.team_strength.predict_scoreline` already emits a fixture's full
scoreline PMF, and shots-faced follows from opponent attacking strength net
of this team's own defensive strength (Dixon-Coles's `mu = exp(attack
[opponent] + defence[this_team])` already blends both). Building a second,
independent team-quality proxy here would repeat the exact "re-derive team
scoring rates from player data" mistake `fplai.models.attacking`'s own
docstring was built to avoid, one level removed.

**Composition, not import** (the `fplai.models.attacking` discipline,
applied here): this module imports neither `team_strength` nor `minutes`.
`predict_saves_pmf` takes `opponent_goals_marginal: Sequence[tuple[int,
float]]` — e.g. `list(enumerate(scoreline_pmf.away_goals_marginal()))` if
this GK's team is home — alongside the usual `minute_exposure` mixture.

At **FIT time**, real historical opponent goals are already known — this
module builds `opponent_goals_this_fixture` from `team_a_score`/
`team_h_score` gated on `was_home` (a FIXTURE-level fact, not a personal
per-appearance one). Checked live against `goals_conceded` for every
full-90-minute GK appearance: **4,488 of 4,489 match exactly** (one named
discrepancy, 2023-24 round 21 fixture 210, not investigated further —
0.02%, not blocking). Measured `corr(opponent_goals_this_fixture, saves)
= +0.125` among the 4,587 real appearances — positive, real, and modest
(expected: a goal conceded is one realised outcome of many shots faced,
most of which don't score).

## What this model does NOT claim — the joint structure

`SavesPMF` is a **marginal**, integrated over whatever `opponent_goals_
marginal` the caller supplied — not a joint distribution over (saves,
goals conceded). Saves and goals conceded are NOT independent (both rise
with shots faced — the entire reason `opponent_goals_this_fixture` is a
feature at all), and a consumer that independently draws a clean-sheet
forecast and this module's marginal `SavesPMF`, then treats them as
independent, will **understate** the real coupling. A caller that needs
the coupling can recover it without a new interface: call
`predict_saves_pmf` once per opponent-goals scenario with a degenerate
one-point marginal `[(g, 1.0)]`, using the SAME `g` draw the caller's own
Monte Carlo uses for that scenario's clean-sheet outcome.

## A real bug found and fixed this session: the shared NB2 L2 scaling

`fplai.models.defensive_contribution`'s own `_nb_neg_log_lik_and_grad`
divides the log-likelihood by `n` (a per-row average) but adds the L2
penalty `l2 * sum(beta**2)` **unscaled** — for `n` in the thousands, the
penalty's effective strength relative to the per-row-averaged likelihood is
roughly `n` times the caller's stated `l2`, silently crushing every
coefficient (including the intercept) toward 0.

Measured directly on this module's own duplicated copy of that formula,
before the fix, at `l2=1.0` (the shared sibling default): fitted intercept
0.4259, implied mean count at the population's average features 1.531 —
against a **real population mean of 2.965**. A factor-of-~2 bias. This was
**invisible to DC's own gate** (a binary threshold-crossing reduction of
its PMF, far less sensitive to getting the mean right) and became visible
here because this module's own gate scores the **full multiclass count
PMF**, which is much more sensitive.

**Fixed in this module's own copy** (not in `defensive_contribution.py`,
which is READ-ONLY in this task's owned paths — reported as an Architect-
level finding, not silently routed around) by scaling the L2 term by the
same `1/n` the likelihood already carries:

```
l2 (fixed, l2/n scaling)   intercept   mu_at_means   true mean = 2.9653
10.0                       1.0879      2.9680
1.0                        1.0896      2.9731
0.1                        1.0898      2.9736
0.01                       1.0898      2.9737
```

Every value in this range is within 0.3% of the true mean — the fixed
formula is NOT sensitive to `l2` the way cards' own genuinely sensitive
sweep was. `l2_penalty=1.0` (default) is a real, modest regulariser with no
measured downside.

**Escalated**: `fplai.models.defensive_contribution` and `fplai.models.
cards` (which duplicates the SAME formula for its own multinomial fit,
though the additive-penalty-on-a-different-likelihood-shape mechanics
differ) may carry this same bias — it did not block either module's own
BINARY gate, but is worth a future session's attention if either model's
own full-multiclass numbers are ever needed for something more sensitive
than a binary threshold reduction.

## Gate — proper scoring rules for an ordinal count, justified

The outcome space (0..20, truncated/renormalised) is genuinely ORDINAL.
Plain multiclass log-loss/Brier score the full PMF but treat every wrong
class equally regardless of distance, so this module also reports the
**ranked probability score (RPS)** — already implemented in `fplai.
calibration`, reduction-tested against Brier at K=2, and explicitly named
there as a legitimate ordinal-count use. The §7.1 gate itself requires
strictly lower multiclass log-loss AND Brier than BOTH baselines (RPS
reported alongside, not separately gating — no proper scoring rule gates
alone anywhere in this codebase, per the §7.1 amendment).

**Two baselines, both full count PMFs**: `group_rate` (pooled empirical
histogram of `count` over each fold's own training rows) and
`player_trailing` — a discretised **Poisson** PMF with `lambda = player_
trailing_saves_mean_5` (the "just use the trailing mean directly" posture
the E5 report already established `greedy_form`/the player-trailing
baseline represents, generalised from a scalar rate to a full PMF).

### Real numbers (real store, `min_train_rows=1500`, 152 folds, 3,104 eval
### rows, `scripts/fit_saves.py`)

```
method                          log-loss       brier         rps
model RAW (SHIPS)                 1.9930      0.8409      0.0521
model CALIBRATED (research)       1.9990      0.8432      0.0523
baseline: group rate              2.0343      0.8468      0.0535
baseline: player trailing         4.1452      0.9627      0.0685
```

**§7.1 GATE on the shipped (raw) series vs both baselines: PASSED**
(strictly lower log-loss AND Brier than both). Re-verified at
`min_train_rows=2500` (103 folds, 2,092 eval rows): raw 1.9898/0.8397 vs
group-rate 2.0367/0.8448 and player-trailing 4.1163/0.9635 — same
conclusion, not an artefact of one fold-count choice.

### Per-threshold reliability

FPL awards `floor(saves/3)` points (`game_config`'s `scoring.saves = 1` is
the live points-per-unit VALUE; the divisor 3 is **not present anywhere in
`game_config`'s payload** — the same unpublished-constant situation
`fplai.models.minutes`'s own 60-minute appearance cliff documents;
`SAVES_POINTS_DIVISOR = 3` is a stated, checked-against-public-rules
constant, not a live-config read).

```
threshold        series          n    log-loss   brier   ece(w)  ece(q)  slope         95% CI      usable
ge_points(>=3)    raw          3104     0.6748    0.2411  0.0113  0.0147  0.786  [0.606,0.966]      True
ge_points(>=3)    calibrated   3104     0.6808    0.2437  0.0217  0.0250  0.739  [0.550,0.929]      True
ge_any(>=1)       raw          3104     0.2523    0.0656  0.0056  0.0123  0.818  [0.604,1.032]      True
ge_any(>=1)       calibrated   3104     0.2527    0.0656  0.0040  0.0125  0.986  [0.734,1.238]      True
```

## Calibration — measured, built, tested, and NOT shipped by default

The raw model's own `P(saves >= 3)` slope (0.786, CI `[0.606, 0.966]`)
excludes 1 in the **overconfident** direction — the dangerous direction
for an optimiser to inherit, per the Architect's cards ruling. So a nested
out-of-sample isotonic calibrator was built and tested exactly as `fplai.
models.minutes`/`fplai.models.cards` establish (fit on a fold's own
`inner_train`/`calib_holdout` split, never the eval fold itself; a
two-block renormalisation generalising cards' "hold one class fixed,
rescale the rest proportionally" mechanism to a split point).

**It measurably does not help.** Both `min_train_rows` settings above show
the calibrated series is WORSE, not better, than raw on every pooled
metric, and the per-threshold slope moves FURTHER from 1.0 (0.786 → 0.739),
not closer — the opposite of what a working calibrator should do.
`SavesWalkForwardResult.calibrated_not_worse_than_raw()` returns `False` on
the real store (`tests/test_saves.py::
test_calibration_measurably_does_not_help_on_the_real_store`).

**Most likely cause, stated rather than silently worked around**: this
module's population (4,587–4,611 rows) is 15–40x smaller than every
sibling nested-calibration precedent (minutes: 113,592 rows; cards:
179,950 rows pre-filter). A per-fold `calib_holdout` here is a few hundred
rows — thin enough that a nested isotonic fit plausibly captures fold-level
noise rather than a stable miscalibration curve.

**Decision**: `fit_saves_model`'s default is `calibrate=False` — the
opposite of `fit_cards_model`'s own `calibrate=True` default, a deliberate,
evidenced difference, not an oversight (a third variant of the already-
documented "`walk_forward_validate` calibrate default differs across
sibling models" seam, `docs/HANDOFF.md` §3). The mechanism is fully
implemented, break-first tested (leak-vs-honest proof, fold-isolation
proof), and available via `calibrate=True` for any future session with
more data or a different design. A negative result is the deliverable here
(CLAUDE.md lesson 9) — the raw, uncalibrated model already passes its own
gate with low ECE (0.0056–0.0217 equal-width, 0.0123–0.0250 quantile) and
a usable, if imperfect, slope.

## Isotonic-saturation bug re-opened this decision, then re-confirmed it — session `s005`

`fplai.models.minutes`'s own session-`s005` investigation found a real
saturation defect in `_fit_isotonic_calibrator`'s `bin_y` (a plain sample
mean, exactly `0.0`/`1.0` whenever a calibration-holdout bin is
outcome-homogeneous — see that module's own docstring for the full
mechanism). This module's `_fit_isotonic_calibrator` (line 849 pre-fix) is
a DUPLICATED copy of the same function (module docstring, "Nested
out-of-sample calibration") and carried the identical uncorrected line.
**Because the `calibrate=False` decision above was measured WITH that bug
present, and this module's population (4,587–4,611 rows) is 15–40x smaller
than every sibling precedent — precisely the regime where homogeneous
bins are most likely — the decision was re-opened, not assumed to
survive.**

### Saturation measured BEFORE the fix — real store, `min_train_rows=1500`, 152 folds, 3,104 OOS rows

**A genuine, reportable negative — this model does not saturate, at all,
even under the bug.** Checked at both the eval level (the final calibrated
`P(saves>=3)` per prediction) and the bin level (every fold's own
calibration-holdout bins, before the isotonic fit):

| | exact-0.0 predictions | exact-1.0 | saturated bins / bins checked | bin weights (all) |
|---|---|---|---|---|
| eval-level census | 0 / 3,104 (0.000%) | 0 | — | — |
| bin-level census | — | — | 0 / 3,040 (0.00%) | 16–46 (median 31) |

This module's bins are genuinely SMALLER than cards' YELLOW calibrator's
own saturated ones (16–46 here vs. 84–168 for cards' saturated bins,
75–684 for cards' bins overall) — smaller bins, on priors, saturate MORE
easily, not less — yet zero of 3,040 bins across all 152 folds were ever
outcome-homogeneous. The likely reason, stated rather than assumed: `P
(saves>=3)`'s raw predicted probabilities cluster in a moderate range
rather than the extreme near-0/near-1 tails cards' YELLOW (a genuinely
rare, ~12.5% base-rate class) and minutes' START (a near-bimodal 0/1
outcome) both populate — a bin needs BOTH a small weight AND a raw
prediction near 0 or 1 to have any real chance of drawing an all-one-class
sample; this module's raw predictions apparently never combine both
conditions in this dataset. Not proven as a general property of count-PMF
threshold calibrators, only measured on this store, this threshold, this
session — stated as a finding, not generalised past what was checked.

### Re-measurement after the fix — same setup, same two `min_train_rows` settings the original decision used

| min_train_rows | metric | RAW (ships) | CAL before fix | CAL after fix |
|---|---|---|---|---|
| 1500 | log-loss | 1.9930 | 1.9990 | 1.9987 |
| 1500 | Brier | 0.8409 | 0.8432 | 0.8432 |
| 1500 | RPS | 0.0521 | 0.0523 | 0.0523 |
| 1500 | slope, `P(saves>=3)` | 0.786 [0.606,0.966] | 0.739 [0.550,0.929] | 0.769 [0.572,0.966] |

The post-fix slope (0.769) sits BETWEEN the pre-fix figure (0.739) and
raw's own (0.786), inside the overlap of both CIs — not a real reversal,
consistent with the saturation census above showing nothing for the fix to
correct here. `calibrated_not_worse_than_raw()`: **still `False`** —
pooled log-loss/Brier/RPS are unchanged to 3-4 significant figures (within
walk-forward fold-composition noise of the pre-fix numbers, not a
meaningful movement in either direction).

### Re-decision: `calibrate=False` STAYS — on sound footing now, not merely re-confirmed by coincidence

All three outcomes named in this task's brief as legitimate were live on
the table; the evidence resolves it as the same conclusion the original
(buggy) measurement reached, for the reason the original measurement's own
"most likely cause" paragraph already named (fold-level noise from a
population 15–40x smaller than every sibling precedent) — **not** because
of the saturation bug, which is now measured to have never been present in
this module's own calibrator. This is the meaningful part of the
re-opening: the ORIGINAL conclusion survives, but it now rests on a
measurement that has actually been checked against the specific defect
that invalidated three sibling verdicts' worth of confidence this session,
rather than on one that happened to be correct despite carrying an
unexamined bug.

### What changed, what did not

- **Fixed**: `_fit_isotonic_calibrator`'s `bin_y` — the same Jeffreys
  `Beta(0.5, 0.5)` correction `fplai.models.minutes`/`fplai.models.cards`
  now both carry, ported verbatim, not reinvented — applies even though
  this module's own calibrator never actually saturated, because the
  mechanism must be correct regardless of whether THIS module's specific
  data happens to trigger the bug (the two new tests below are
  constructed, not real-store-derived, for exactly this reason).
- **Unchanged**: `fit_saves_model`'s `calibrate=False` default.
- **Unchanged**: the §7.1 gate verdict on the shipped (raw) series (PASS).
- **New tests**: `tests/test_saves.py::
  test_isotonic_calibrator_never_saturates_to_exact_zero_or_one` (hand-
  verified to FAIL against a temporarily reverted copy of this module,
  producing `cal.y == (0.0, 1.0)`, before being trusted — CLAUDE.md lesson
  5) and `::test_isotonic_calibrator_smoothing_converges_to_raw_rate_at_
  scale`.
- **Verified against the real store**: `scripts/fit_saves.py`'s own
  walk-forward path, run before and after the fix (temporarily reverted,
  `diff -q` confirmed byte-identical restoration, never `git`), full suite
  `uv run pytest tests/test_saves.py -q` — 36 passed.

## Feature design

`NUMERIC_FEATURE_COLUMNS_SAVES` (7, all standardised at fit time):

- `player_trailing_saves_mean_{3,5,10}` — this GK's own trailing per-round
  mean save count.
- `games_played_this_season`, `cold_start` — standard cold-start handling.
- `was_home` — a plain fixture feature, no asserted sign.
- `opponent_goals_this_fixture` — the team-quality signal (see above);
  excluded from the caller's `feature_row` at predict time (supplied via
  `opponent_goals_marginal`'s mixture instead — `predict_saves_pmf` raises
  if a caller smuggles it in directly).

No position one-hot — single population (`GK` only).

## Persistence

`derived_player_saves_distribution`, capability `player.saves_distribution
@gameweek`, entity key `(season, round, element, fixture, count)` — long
format, one row per count with its probability. `calibration_method` in
(`raw_uncalibrated`, `isotonic_v1`) is structural provenance on every
persisted row, same convention `MinutesPMF.calibration_method`/`CardsPMF`
establish.

## What was NOT done this session

- The measured L2-scaling finding was fixed only in this module's own
  duplicated copy — `defensive_contribution.py`/`cards.py` were not
  touched (READ-ONLY in this task's owned paths) and may carry the same
  bias; escalated above, not silently routed around.
- `scripts/calibration_report.py` has no `run_saves` hook — running it
  does not fold this model's numbers into the generated report (a known
  defect the brief anticipated for a different reason; here it is a
  missing hook entirely, not a stale-decision-text bug). Reported, not
  worked around (READ-ONLY, forbidden to edit).
- `points.py`/the player-GW points PMF composing this model with the other
  six is explicitly out of scope — another XL-Coder's parallel task.
