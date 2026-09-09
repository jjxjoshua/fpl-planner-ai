# Model: Cards / Discipline

Phase 2 / E5, session `s004` — the **last model before the Phase 2 (E5)
calibration gate**. Blueprint §4's sixth model row: *"Cards / discipline —
Yellow/red probabilities. Referee assignment is a real feature."* Code:
`src/fplai/models/cards.py`. Tests: `tests/test_cards.py` (53 as of the
isotonic-saturation fix below). Fit/report script: `scripts/fit_cards.py`.

> **Consolidation note, later session `s005`:** `IsotonicCalibrator`/
> `_fit_isotonic_calibrator` — described below as this module's own code —
> were moved to the shared `fplai.calibration` module once the identical
> mechanism (and the identical Jeffreys-smoothing fix described below)
> turned up independently in `fplai.models.minutes` and `fplai.models.saves`
> too. Proven bit-identical (three copies diffed line-by-line first, then
> re-run against the real store before/after: pooled log-loss 0.3959, NONE
> slope 0.875, YELLOW slope 0.931, RED slope 0.115 raw==calibrated,
> unchanged) — every number, test name and line reference below is
> unchanged in behaviour; only the import source moved.
> `fplai.models.cards` still exposes `IsotonicCalibrator`/
> `_fit_isotonic_calibrator` as re-exports for backward compatibility, so
> every reference below still resolves.

**A second, separate `s005` story landed after the nested-calibration one
below: the L2-scaling bug fix.** See "L2 scaling bug fixed — session
`s005`" near the end of this page. Short version: NONE/YELLOW improve
modestly (~0.6% log-loss, both raw and calibrated); the isotonic
calibrator is measured to still be pulling essentially the same weight it
was before the fix, not made redundant by it; RED's slope moves from
clearly-unusable to marginally-distinguishable-from-zero-but-still-far-
from-1, which **stays BASE-RATE-ONLY on Architect ruling** — immaterial in
points (~0.414% base rate, ~-0.012 expected points/appearance total
contribution) and boundary-sensitive across the swept `l2` grid, not a
robust new signal.

**A third, separate `s005` story landed after that: the isotonic-
saturation bug fix.** See "Isotonic-saturation bug fixed — session `s005`"
near the end of this page. Short version: this module's own copy of
`fplai.models.minutes`'s saturation defect DID bite YELLOW (121/58,461
predictions, 0.207%, exactly 0.0) but NOT NONE (zero saturation) — an
asymmetry, not assumed. The Jeffreys fix moves both calibrated slopes
further toward 1.0 (NONE 0.845→0.875, YELLOW 0.896→0.931) at a small pooled
cost/benefit (log-loss 0.3967→0.3959, Brier a wash) — **the s004/s005
"still earning its place" verdict is RE-CONFIRMED on corrected numbers, not
assumed to survive.** RED is bit-identical before and after (never
touched, as designed).

**Session `s005` update — the E5 blocking condition is closed.** E5 passed
with one rejected acceptance (Architect ruling, `docs/HANDOFF.md` §2): cards
NONE/YELLOW were **overconfident** (slopes 0.592/0.630) — the dangerous
direction for an optimiser to inherit, unlike bonus's accepted
underconfidence. A nested, out-of-sample isotonic calibration layer for
NONE/YELLOW (the same mechanism `fplai.models.minutes` proved: slope
1.114→0.972, ECE 0.0507→0.0131) has been built, gated, and shipped as the
new default (`fit_cards_model(calibrate=True)`, `walk_forward_validate
(calibrate=True)`, both defaults). **RED is never calibrated** — it is
base-rate-only (slope −0.081, no usable ranking) and there is nothing to
correct. See "Nested out-of-sample NONE/YELLOW calibration — session s005"
below for the full before/after numbers; everything above this note
describes the s004 state and is otherwise unchanged and still accurate
(the multinomial fit itself, the feature set, the referee measurement, the
outcome-space verification were not touched this session).

## The design problem

Discipline looks like two correlated binary events (yellow, red) but is
actually **one complete, mutually-exclusive 3-class outcome** — verified
live against the entire archive, not assumed:

```
rows checked:                  179,950 (7 seasons)
yellow_cards AND red_cards both 1:   0
yellow_cards outside {0, 1}:         0
red_cards outside {0, 1}:            0
HOLDS: True
```

FPL's own `yellow_cards` field never reports 2 even for a second-yellow
dismissal — it resolves that case as `red_cards=1, yellow_cards=0`. This
means `OUTCOMES = (0, 1, 2)` (NONE/YELLOW/RED) is a genuine, complete
partition — a single multinomial target, never two independently-fit
binaries that could jointly assign mass to an impossible cell (the same
class of failure bonus's own docstring names for a naive per-player bonus
classifier). `verify_yellow_red_mutually_exclusive_against_archive` is this
check as a real, callable, break-first function, re-run against the raw
`effective_at()` rows (never this module's own derived `outcome` column) by
`tests/test_cards.py`'s real-store group.

A card without a minute is real, not a data error: **12 rows** across the 7
seasons carry `minutes=0` with a card (all yellow) — bench/technical-area
cautions (e.g. Jamaal Lascelles 2022-23 rounds 16/19, Ashley Barnes 2025-26
round 22). This module follows DC/attacking's `minutes > 0` fit-time filter
(not bonus's zero-inclusion — cards has no fixture-wide rank coupling that
would need the full squad population), and states the 0.007%-of-rows
exclusion explicitly.

## Cards are per-player, not fixture-coupled

Unlike bonus's rank statistic over a shared BPS pool, a card has no
mechanical effect on a teammate's or opponent's own card risk. This module
fits and predicts **one player's own PMF independently**, the same
per-player signature DC/attacking use, never bonus's whole-fixture Monte
Carlo.

## Minutes as an exposure offset, not an ordinary feature

Following **DC**, not bonus: a card count is structurally non-negative and
scales with time on the pitch, so the multinomial log-odds for YELLOW/RED
(relative to NONE) each carry `offset = log(minutes / 90)` at a fixed
coefficient of 1 — DC's own `offset` convention, generalised from an NB2
mean to a softmax log-odds. Bonus's alternative (`minutes_frac` as an
ordinary feature) was rejected here because it was forced by BPS's
sign-unconstrained range, which does not apply to cards.

## Referee: a real feature, deliberately not deployed

**The interesting part of this task.** `fplai.backfill._valid_at_for`
anchors `match.officials@match` at **kickoff** — leak-safe, but it means a
bitemporal query resolved as of the FPL deadline (~1.5h before the first
kickoff) cannot see the referee, even though the PL genuinely publishes
appointments days ahead. This store has never observed the announcement
instant, only the kickoff-anchored settlement — inventing an offset would
fabricate a fact nobody observed. `KNOWN_ABSENT_FEATURES = ("referee_identity",)`.

**What was measured instead**: `measure_referee_value_NOT_DEPLOYABLE` fits
the SAME multinomial model twice, walk-forward, over the identical row
population — once on `NUMERIC_FEATURE_COLUMNS_CARDS` (deployable) and once
with one additional feature added, `referee_trailing_cards_per_match_10`
(this referee's own trailing mean total cards shown per match he has
officiated, `shift(1)`-leakage-safe). The join (verified live this session,
matching the brief's cited numbers):

```
vaastav_player_gameweek_stats (was_home=True rows)
  -> vaastav_team_identity (season, opponent_team) -> away_team_code
  -> pl_match_fixtures (kickoff date, away_team_code) -> match_id
  -> pl_match_officials (match_id, is_referee=True) -> official_name

match_id resolved:        2,274 / 2,280 fixtures (99.74%)
referee row present:      2,266 / 2,280 fixtures (99.39%)
  (the 8-fixture gap: 2022-23 matches with assistants/VAR but no
  Referee role row — a known, already-recorded data gap)
```

### Result — the size of the prize

Real store, 2020-21 through 2025-26, `l2_penalty=0.001`, `min_train_rows=2000`,
n_rows_compared = 64,111 (the population both variants can evaluate):

| variant | log-loss | Brier |
|---|---|---|
| **deployable** (no referee) | 0.397645 | 0.221768 |
| **referee-inclusive — NOT DEPLOYABLE** | 0.397445 | 0.221782 |
| delta (deployable − referee) | **+0.000200** | **−0.0000139** |

Per-outcome ECE/slope are likewise indistinguishable between the two
variants (e.g. YELLOW: ECE 0.00981 vs. 0.00786; RED slope −0.081 vs.
−0.082). **Verdict: referee identity, encoded as a single trailing
strictness rate and even given full omniscient access to it, buys
essentially nothing over the deployable feature set on this store** — the
delta is two to three orders of magnitude smaller than the model's own
ECE. This does not prove a referee signal could never help (a per-referee
fixed effect, or a richer feature than one trailing rate, might behave
differently — the 38-referee/2,274-match sample is thin either way), but on
the feature construction actually measured, **it is not worth building an
announcement-time capability for this alone.** That is the number the
brief asked for, reported honestly rather than assumed in either direction.

**Structural guarantee that the referee feature cannot reach a real
prediction**: `fit_cards_model`/`predict_cards_pmf` hard-code
`NUMERIC_FEATURE_COLUMNS_CARDS` — no parameter selects the referee-inclusive
set. Attacked directly (`tests/test_cards.py::
test_predict_cards_pmf_structurally_cannot_be_influenced_by_a_referee_feature_smuggled_into_the_feature_row`):
smuggling `referee_trailing_cards_per_match_10` into `feature_row` at
predict time produces a bit-identical PMF, because there is no coefficient
that could ever read it.

## What match-level coupling was rejected — and why, with evidence

`pl_team_match_stats` carries team-level discipline stats (`totalYelCard`,
`fkFoulLost`, 246 distinct `stat_key` values, 2,274 matches) — a genuine,
directly-observed team-tackling-style signal. **Rejected**, for a concrete,
verified reason: building a round-grain feature from it needs
`pl_match_fixtures.matchweek` as a stand-in for FPL's own `round`, and that
substitution is **measurably unsafe** — checked live over all 2,274 matches
this module's own referee join resolves, `round != matchweek` on **101 of
2,274 (4.4%)**, with divergences as large as round 22 vs. matchweek 8 (a
postponed-and-rescheduled fixture). Using `matchweek` would silently
misalign a team's trailing discipline history for 4.4% of matches — a
join-drops-the-label failure one level more subtle than usual (the join
succeeds; the value it attaches is wrong). This module instead builds its
own team-style feature (`team_trailing_cards_mean_5`) entirely from
`vaastav_player_gameweek_stats`'s own `round` column — zero new joins.
**Named seam for a future session**: resolving `matchweek` <-> `round`
properly (via `kickoff` matching, not either numbering directly) would
unlock `pl_team_match_stats` for this and every other match-grain model.

## Multinomial logit — closed-form gradient, deterministic L-BFGS-B

Reference class 0 (NONE); two log-odds (YELLOW, RED), each
`X @ beta_k + offset`. Standard softmax cross-entropy + analytic gradient,
cross-checked against finite differences. Every numeric feature
standardised (frozen at fit time), same discipline attacking/bonus/DC all
use.

## §7.1 walk-forward gate — real numbers, real store

Full 6-season window (2020-21 through 2025-26), `min_train_rows=2000`,
`l2_penalty=0.001`, 219 folds, 64,531 out-of-sample eval rows:

| method | log-loss | Brier |
|---|---|---|
| **model (multinomial logit)** | **0.3973** | **0.2215** |
| baseline: position base rate | 0.4006 | 0.2249 |
| baseline: player trailing rate | 2.1801 | 0.2618 |

**§7.1 GATE: PASSED** — strictly beats both baselines on both metrics (true
3-class log-loss and multiclass Brier). The player-trailing baseline's own
huge log-loss is a real property of an unsmoothed empirical rate over a
rare outcome (RED, 0.4% base rate): whenever a player's own trailing window
never saw the true outcome, that baseline assigns it ~0 probability,
producing a `-log(eps)` spike — not a bug in this module's own baseline
construction, the honest cost of a naive, un-Laplace-smoothed trailing
rate against a rare class.

### `l2_penalty` sweep (the choice, with evidence)

`min_train_rows=3000` (a stricter, more conservative fold-count check than
the headline table above, run separately for the sweep):

| l2 | model log-loss | model Brier | beats gate |
|---|---|---|---|
| 1.0 | 0.8388 | 0.4878 | NO |
| 0.3 | 0.7401 | 0.4197 | NO |
| 0.1 | 0.6037 | 0.3259 | NO |
| 0.05 | 0.5260 | 0.2764 | NO |
| 0.02 | 0.4576 | 0.2402 | NO |
| 0.01 | 0.4287 | 0.2290 | NO |
| 0.005 | 0.4123 | 0.2244 | NO (baseline 0.4012) |
| **0.001** | **0.3980** | **0.2220** | **YES — first pass** |
| 0.0005 | 0.3965 | — | YES |
| 0.0001 | 0.3957 | — | YES |

`0.001` chosen: first value that passes, with real headroom before the
sweep's most aggressive (least-tested) end, matching every sibling module's
own "not a claimed optimum, a reasoned starting point" posture.

### Reliability, per outcome (required, not optional — this task's brief)

| outcome | n | log-loss | Brier | ECE | slope | intercept |
|---|---|---|---|---|---|---|
| NONE | 64,531 | 0.3764 | 0.1103 | 0.0099 | 0.603 | 0.699 |
| YELLOW | 64,531 | 0.3674 | 0.1071 | 0.0096 | 0.643 | -0.599 |
| RED | 64,531 | 0.0304 | 0.0042 | 0.0060 | -0.081 | -5.892 |

**Decision, stated per the brief's explicit requirement**: ECE
(0.0060-0.0099) sits in the same low range bonus's own shipped numbers do
(0.0002-0.0091). **The Cox calibration slope is a real, stated defect**:
well below 1.0 for NONE/YELLOW (genuinely overconfident) and near 0 for
RED. **[Updated `s005`, after the L2 scaling fix below]: RED's slope moves
from -0.081 to a small positive number (~0.07-0.12 depending on `l2`) and
its CI narrowly excludes zero at the shipped default — see "L2 scaling bug
fixed" near the end of this page for the full grid and the Architect
ruling that RED nonetheless stays BASE-RATE-ONLY** (still far from 1.0,
boundary-sensitive across `l2`, and immaterial in points). **Checked NOT
to be a code bug**: a synthetic dataset drawn from a
KNOWN multinomial-logit-with-offset generating process, fit through this
exact walk-forward path, recovers the textbook-expected slope > 1
(underconfident) when regularised ABOVE its own true generating scale (see
`tests/test_cards.py::
test_walk_forward_calibration_slope_recovers_correctly_on_a_known_synthetic_generative_process`
— l2=0.01 against a true beta std of 0.5 gives slope 1.78/1.45/3.72 for
NONE/YELLOW/RED, all comfortably above 1). The real-data slope<1 finding is
therefore a genuine property of this fit against real, non-stationary
football data (referee/season-level shifts, unmodelled heterogeneity), not
an artefact of this module's own multinomial/gradient/calibration
machinery. **Session s004 posture (superseded by s005, next section):** no
calibration layer was built that session — "measured, reported, flagged,
not silently accepted or hidden", with a per-outcome isotonic/Platt layer
(the `fplai.models.minutes` precedent) named as a real seam for a future
session. **That future session is s005 — see below.**

## Nested out-of-sample NONE/YELLOW calibration — session s005

**Design.** `IsotonicCalibrator`/`_fit_isotonic_calibrator`/`_inner_
calibration_split` are duplicated (not imported) from `fplai.models.
minutes` — a sibling model, so this module's own "no cross-model import"
convention applies (unlike `fplai.calibration`, which this module now DOES
import for the shared slope-CI/quantile-ECE math, since that module exists
specifically so five-going-on-six independent copies of the same pure math
converge into one). Two ONE-VS-REST isotonic calibrators are fit — one for
NONE, one for YELLOW — each on (that class's own raw predicted probability,
`1{outcome==that class}`) pairs from a **nested, out-of-sample holdout**:
`inner_train`/`calib_holdout` split from the fold's own `train` slice (or,
for the production fit, from the `as_of` window), a model refit on
`inner_train` alone, scored OOS on `calib_holdout`, and the calibrator
learned from THOSE pairs — never from the data it will later be asked to
predict. **RED is never touched** — no calibrator is fit for it (base-rate-
only, no usable ranking to correct).

**Renormalisation — the real design question.** Two independently-fit
one-vs-rest calibrators do not sum to anything meaningful with each other or
with RED, but `CardsPMF.probabilities` must still sum to exactly 1.0. The
choice made: hold RED's raw probability **completely fixed** (this module
never learns anything about RED's calibration at all), then rescale the two
calibrated values `(cal_NONE, cal_YELLOW)` **proportionally** so they sum to
exactly `1 - p_RED_raw` — preserving the ratio the two calibrators produced,
spending the renormalisation entirely on their shared scale. This is the
same mechanism `predict_minutes_pmf` already uses one dimension down (there,
one calibrated value and the remaining SUB+UNUSED mass rescaled to make room
for it). **Stated cost**: the final NONE/YELLOW are `cal * scale`, not `cal`
itself — a further multiplicative perturbation to satisfy the sum-to-1
constraint. Measured (not just argued) to be small: the pooled calibrated
metrics below are strictly better than raw, not merely "not worse", so
whatever perturbation this introduces did not erase the correction. The
untaken alternative — a single binary calibrator on the NONE-vs-YELLOW
conditional split (zero renormalisation cost, but cannot correct NONE's and
YELLOW's slopes to different targets independently) — is a real, cheaper
seam for a future session, not tested head-to-head this session.

**Attacked, not just asserted** (this task's standing rule): `tests/
test_cards.py::test_cards_calibrator_fit_directly_on_eval_data_would_leak_
and_look_suspiciously_good` constructs the FORBIDDEN path by hand (fit the
calibrator on the eval fold's own outcomes) and shows it produces a strictly
better log-loss than the honest nested version on the same synthetic data —
the leak this design exists to prevent is real and measurable, not
hypothetical. `tests/test_cards.py::
test_walk_forward_validate_calibrate_cannot_see_a_folds_own_or_future_outcomes`
perturbs a LATER round's outcomes and shows every EARLIER fold's `p_model`
entries are bit-identical before and after.

### Before / after — real store, `as_of` 2026-08-29, `l2_penalty=0.001`

197 folds, 58,461 out-of-sample eval rows, 197/197 folds received a real
nested calibrator (`n_folds_calibrated`):

| method | log-loss | Brier |
|---|---|---|
| model RAW | 0.4017 | 0.2247 |
| model CALIBRATED | **0.3991** | **0.2245** |
| baseline: group rate | 0.4048 | 0.2283 |
| baseline: player trailing | 2.2084 | 0.2659 |

**§7.1 gate: PASSED** on the calibrated (shipped) series. **Calibrated model
strictly beats raw on both pooled metrics** — not a wash, a real
improvement (this task's brief's "must not be worse than raw" bar is
cleared with margin, not just satisfied at the boundary).

| outcome | series | n | log-loss | Brier | ECE(w) | ECE(q) | slope | 95% CI | intercept |
|---|---|---|---|---|---|---|---|---|---|
| NONE | raw | 58,461 | 0.3808 | 0.1119 | 0.0099 | 0.0124 | 0.592 | [0.554, 0.629] | 0.708 |
| NONE | **calibrated** | 58,461 | **0.3780** | **0.1118** | **0.0085** | **0.0085** | **0.845** | **[0.794, 0.896]** | 0.328 |
| YELLOW | raw | 58,461 | 0.3718 | 0.1087 | 0.0105 | 0.0124 | 0.630 | [0.591, 0.669] | -0.613 |
| YELLOW | **calibrated** | 58,461 | **0.3692** | **0.1086** | **0.0019** | **0.0044** | **0.901** | **[0.849, 0.954]** | -0.185 |
| RED | raw | 58,461 | 0.0304 | 0.0041 | 0.0060 | 0.0076 | -0.064 | [-0.183, 0.054] | -5.812 |
| RED | calibrated | 58,461 | 0.0304 | 0.0041 | 0.0060 | 0.0076 | -0.064 | [-0.183, 0.054] | -5.812 |

RED's row is bit-identical raw vs. calibrated — direct, live confirmation
that the module never touches it (not just a code-reading claim).

**Verdict, stated per the brief's explicit standard.** Both slopes moved
**substantially** toward 1.0 (NONE 0.592→0.845, YELLOW 0.630→0.901) and
both ECE bindings improved on both outcomes (NONE's quantile ECE nearly
halved, 0.0124→0.0085; YELLOW's more than halved on both bindings). Neither
CI includes 1.0 yet (NONE's upper bound 0.896, YELLOW's 0.954) — a residual,
statistically real departure remains, at this sample size (n=58,461) even a
small remaining gap is detectable. **This is the same shape `fplai.models.
minutes`' own post-calibration series carries** (slope 0.972, CI [0.959,
0.984] — also excludes 1.0) and accepted there for the same reason: the
point estimate is close enough that a second calibration layer's marginal
gain is judged small relative to what has already been banked (here: ECE
roughly halved, slope departure roughly two-thirds closed, log-loss/Brier
both improved on the pooled metric too). **No second-stage calibrator built
this session** — accepted, not silently ignored; a further layer (e.g.
tightening `n_bins`, or the untaken conditional-split design above) is a
real, named seam if a future session's gate needs the CI to fully close.

### What changed in the persisted shape

`CardsPMF.calibration_method` (`"raw_uncalibrated"` / `"isotonic_v1"`) is a
new field, carried through `to_polars()` into the persisted derived row —
same structural-provenance convention `MinutesPMF.calibration_method`
establishes. `player.cards_distribution@gameweek`'s registered `value_
fields` grew from `("probability", "outcome_label")` to `("probability",
"outcome_label", "calibration_method")` — a schema-shape change, owned and
registered through `fplai.models.cards._register_cards_capability` per
CLAUDE.md's registration-surface rule (no `fplai/schemas.py` edit was
needed: `register_derived_capability` is fully generic over `value_fields`,
so the change lives entirely in this module's own registration call).

## Feature set

8 standardised numeric features + position one-hot (no team-name dummy,
same promoted-team reasoning DC establishes): `player_trailing_card_rate_
{3,5,10}` (any card, not split by class — RED's 0.17% base rate would make
a class-specific trailing feature mostly zero/high-variance),
`team_trailing_cards_mean_5`, `games_played_this_season`, `cold_start`,
`team_cold_start`, `was_home`. Known-absent: `referee_identity` (measured
retrospectively above, never deployed).

## What this module deliberately did not do

- No referee identity in the deployable feature set (measured its value
  instead — see above).
- No `pl_team_match_stats`-derived team feature (the `matchweek`/`round`
  misalignment, measured and rejected above).
- ~~No calibration layer~~ **Superseded s005** — a nested NONE/YELLOW
  isotonic layer now ships by default; see "Nested out-of-sample NONE/
  YELLOW calibration — session s005" above. RED remains uncalibrated,
  deliberately (base-rate-only, nothing to correct).
- No per-referee categorical dummy (38 referees, ~60 matches each — a
  trailing rate degrades gracefully to an unseen referee; a dummy does not).
- No fixture-level coupling between players' card risk (a real phenomenon —
  a match turning feisty — this module does not model; a named, stated
  scope boundary, not a hidden one).

## L2 scaling bug fixed — session `s005` (second `s005` story, after the nested-calibration one above)

`_multinomial_neg_log_lik_and_grad`'s ridge term was `l2 * (sum(beta1**2)
+ sum(beta2**2))` — unscaled against the per-row-averaged likelihood, the
same defect every sibling model found and fixed this session
(`docs/wiki/model-minutes.md` §13 has the fullest derivation). This
module's own `l2_penalty` default (`0.001`) was chosen specifically to
compensate for it — `CardsModelConfig.l2_penalty`'s own docstring records
a sweep, under the OLD formula, where every `l2 >= 0.005` FAILED the §7.1
gate outright. **Because this session's nested NONE/YELLOW isotonic
calibrator (above) was fitted on the OLD formula's raw predictions, this
task's brief required measuring raw and calibrated separately and asking
whether the calibrator is still earning its place** — it is possible the
calibrator was partly compensating for this bug rather than for a genuine
model-shape problem.

### Arm A — the shipped baseline, reproduced at the OLD formula, `l2=0.001`, `min_train_rows=8000`, 197 folds / 58,461 eval rows

Exactly reproduces the "Nested out-of-sample NONE/YELLOW calibration"
section's own table above — trusted, not re-derived:

| method | log-loss | Brier |
|---|---|---|
| model RAW | 0.4017 | 0.2247 |
| model CALIBRATED | 0.3991 | 0.2245 |

| outcome | series | slope | 95% CI |
|---|---|---|---|
| NONE | raw | 0.592 | [0.554, 0.629] |
| NONE | calibrated | 0.845 | [0.794, 0.896] |
| YELLOW | raw | 0.630 | [0.591, 0.669] |
| YELLOW | calibrated | 0.901 | [0.849, 0.954] |
| RED | raw = calibrated | -0.064 | [-0.183, 0.054] |

### Corrected formula, swept `l2` from `0.0001` through `1.0`, same setup, RAW and CALIBRATED both measured at every point

| l2 | log-loss RAW | log-loss CAL | Brier RAW | Brier CAL | cal not worse than raw |
|---|---|---|---|---|---|
| 0.0001 | 0.3994 | 0.3967 | 0.2246 | 0.2242 | True |
| 0.001 (shipped) | 0.3994 | 0.3967 | 0.2246 | 0.2242 | True |
| 0.005 | 0.3994 | 0.3967 | 0.2246 | 0.2242 | True |
| 0.01 | 0.3994 | 0.3967 | 0.2246 | 0.2243 | True |
| 0.1 | 0.3994 | 0.3967 | 0.2246 | 0.2243 | True |
| 1.0 | 0.3993 | 0.3963 | 0.2246 | 0.2243 | True |

| l2 | NONE slope RAW [CI] | NONE slope CAL [CI] | YELLOW slope RAW [CI] | YELLOW slope CAL [CI] | RED slope (raw=cal) [CI] |
|---|---|---|---|---|---|
| 0.0001 | 0.587 [0.552,0.623] | 0.844 [0.794,0.894] | 0.618 [0.581,0.656] | 0.896 [0.844,0.948] | 0.115 [0.007,0.223] |
| 0.001 (shipped) | 0.587 [0.552,0.623] | 0.845 [0.795,0.895] | 0.618 [0.581,0.656] | 0.896 [0.844,0.948] | 0.115 [0.007,0.224] |
| 0.005 | 0.587 [0.552,0.623] | 0.844 [0.794,0.894] | 0.618 [0.581,0.656] | 0.896 [0.844,0.948] | 0.115 [0.006,0.223] |
| 0.01 | 0.587 [0.552,0.623] | 0.844 [0.795,0.894] | 0.618 [0.581,0.656] | 0.896 [0.843,0.948] | 0.115 [0.005,0.224] |
| 0.1 | 0.588 [0.552,0.623] | 0.845 [0.795,0.895] | 0.618 [0.581,0.656] | 0.895 [0.843,0.947] | 0.109 [-0.005,0.222] |
| 1.0 | 0.590 [0.554,0.626] | 0.850 [0.800,0.900] | 0.620 [0.582,0.657] | 0.901 [0.849,0.953] | 0.073 [-0.049,0.196] |

### Verdict 1 — NONE/YELLOW: a real but modest improvement, same shape as bonus, smaller than attacking's

Pooled log-loss falls 0.4017 -> 0.3994 RAW (-0.6%) and 0.3991 -> 0.3967
CALIBRATED (-0.6%) at the shipped default, and the improvement is stable
across the entire swept grid, not concentrated at one point. **This is a
real, measured improvement, but modest — say so plainly rather than
inflating it**, the same posture bonus's own section on this page takes.
Per-outcome NONE/YELLOW slopes are essentially UNCHANGED by the fix (raw
0.587/0.618 corrected vs 0.592/0.630 under the old formula; calibrated
0.845/0.896 corrected vs 0.845/0.901 old) — within noise of each other at
every `l2` tested.

### Verdict 2 — is the isotonic calibrator still pulling its weight? Yes, essentially unchanged

**This was a real, named possible outcome per this task's brief — that the
calibrator was partly compensating for the L2 bug — and the measurement
says it was not.** Comparing raw-to-calibrated slope movement, old formula
vs corrected formula, at the shipped `l2=0.001`:

| outcome | raw slope, OLD formula | cal slope, OLD formula | raw slope, CORRECTED | cal slope, CORRECTED |
|---|---|---|---|---|
| NONE | 0.592 | 0.845 | 0.587 | 0.845 |
| YELLOW | 0.630 | 0.901 | 0.618 | 0.896 |

The calibrator closes essentially the SAME gap under both formulas (NONE:
+0.253 old, +0.258 corrected; YELLOW: +0.271 old, +0.278 corrected) — the
raw miscalibration this layer corrects is a property of the multinomial
fit's own shape against real football data (module docstring, "checked NOT
to be a code bug" — the synthetic-generative-process test), not an
artefact of the L2 scaling defect. **The calibrator is still necessary and
still earning its place**, unchanged by this fix. `calibrated_not_worse_
than_raw()` is `True` at every point of the swept grid.

### Verdict 3 — RED: ARCHITECT RULING, stays BASE-RATE-ONLY

Under the OLD formula RED's slope was -0.064, CI [-0.183, 0.054] —
comfortably includes 0, unambiguously unusable. Under the corrected
formula RED's slope is POSITIVE at every point of the swept grid (0.115
down to 0.073 as `l2` climbs from `0.0001` to `1.0`), and at the shipped
default (`l2=0.001`) the CI is `[0.007, 0.224]` — technically excludes
zero.

**Reported to the Architect rather than decided here, because a gate
verdict was on the line (this task's brief, explicit stop condition); the
Architect's ruling is recorded here rather than left as an open
question**: RED **remains BASE-RATE-ONLY**. No calibrator is built for it,
no default changes on its account, and `usable=True` at this one point is
not treated as a new capability. Three reasons:

1. **The point estimate (0.115) is not the same claim as `usable=True`.** A
   slope statistically distinguishable from zero is not a slope near 1.0
   — the model has a whisper of ranking power for RED, not a working one.
2. **The verdict is boundary-sensitive.** The CI's lower bound sits at
   0.005-0.007 for `l2 <= 0.01` and flips back to including zero at `l2 =
   0.1` and `1.0` (own table above) — a verdict that depends on which side
   of an arbitrary hyperparameter it happens to land on is not a robust
   finding.
3. **It is immaterial in points.** Red cards run a measured 0.414% base
   rate across 74,564 real appearances, worth -3 points — RED's entire
   contribution is roughly -0.012 expected points per appearance. Compare:
   cards NONE/YELLOW's own accepted residual (§ above) is 0.038
   pts/appearance, and the GK saves model was ordered built at a 0.21
   pts/appearance DIFFERENTIAL. RED's whole signal, even taken at face
   value, sits below the residual this project has already accepted as
   immaterial elsewhere.

**The honest characterisation, updated from the OLD formula's wording**:
RED's calibration slope is now marginally distinguishable from zero at the
shipped default, but remains far from 1.0, is sensitive to `l2` across the
range this session tested, and is immaterial in points terms regardless. A
future session must not read the pre-`s005` wording ("slope indistinguishable
from zero"), re-measure, find a positive CI, and conclude something has been
newly discovered — it has not; the underlying signal is, and remains,
too weak to act on.

### What changed, what did not

- **Fixed**: `_multinomial_neg_log_lik_and_grad`'s ridge term, in both loss
  and gradient, for BOTH `beta1`/`beta2`, scaled by the same `1/n` the
  likelihood already carries.
- **Unchanged**: `CardsModelConfig.l2_penalty` default (`0.001`) — clears
  the §7.1 gate at every point of the swept grid; no retuning indicated.
- **Unchanged**: NONE/YELLOW's isotonic calibrator — still necessary,
  still closing essentially the same gap, not made redundant by this fix.
- **Unchanged, per Architect ruling**: RED stays BASE-RATE-ONLY. No
  calibrator built. Wording in "Reliability, per outcome" above updated to
  reflect the corrected-formula measurement without overstating it.
- **Unchanged**: the §7.1 gate verdict (`PASS`) at every point of the
  swept grid.
- **Verified against the real store**: `scripts/fit_cards.py`'s own
  walk-forward path (both `calibrate=True`/`False`), `tests/test_cards.py::
  test_multinomial_l2_penalty_is_scaled_by_inverse_n` (a formula-
  discriminating pinning test, hand-verified to FAIL against a temporarily
  reverted copy of the old formula before being trusted, CLAUDE.md lesson
  5), full suite `uv run pytest tests/test_cards.py -q` — 51 passed
  (631s — this module's real-store-gated group, including the synthetic
  slope-recovery test and the referee-value measurement, is the slowest in
  the suite by design).

## Isotonic-saturation bug fixed — session `s005` (third `s005` story, after nested-calibration and L2)

`fplai.models.minutes`'s own session-`s005` investigation found that its
`_fit_isotonic_calibrator`'s `bin_y[b]` — a plain sample mean — is exactly
`0.0`/`1.0` whenever a calibration-holdout bin is outcome-homogeneous, and
`IsotonicCalibrator.apply()`'s flat extrapolation then hands that exact
value to every more-extreme eval prediction with unearned full confidence.
This module's own `_fit_isotonic_calibrator` (line 1179 pre-fix) is a
DUPLICATED copy of the same function (module docstring, "Nested
out-of-sample NONE/YELLOW calibration") and carried the identical
uncorrected line — checked directly, not assumed, and found present.

### Saturation measured BEFORE the fix — real store, `l2=0.001`, `min_train_rows=8000`, 197 folds, 58,461 OOS rows

A genuine, measured **asymmetry between NONE and YELLOW** — neither
assumed to behave like the other, nor like minutes' single-series case:

| outcome | exact-0.0 predictions | exact-1.0 | of exact-0.0, true label WAS this class | saturated bins / bins checked | saturated bin weights | all bin weights |
|---|---|---|---|---|---|---|
| NONE | 0 (0.000%) | 0 | — | 0 / 3,940 | — | 75–684 (median 392) |
| YELLOW | 121 (0.207%) | 0 | 2 | 12 / 3,940 (0.30%) | 84–168 (median 96) | 75–684 (median 392) |

YELLOW's saturated rows are a real, if small, drag: summed over just those
121 rows, calibrated log-loss was **+56.03 WORSE** than raw for those same
rows, even though the pooled YELLOW delta (calibrated − raw, summed) was
**−143.53** (net improvement) — i.e. the calibrator was already net-useful
for YELLOW despite the bug, just leaving real log-loss on the table. NONE
never saturated at all (0 of 3,940 bins, at any point in the walk-forward)
— a real negative for that one series, not a defect this fix needed to
address there.

### Before / after — the isotonic fix in isolation (L2 fix already applied on both sides; only `_fit_isotonic_calibrator` changes)

"Before" reuses the "L2 scaling bug fixed" section's own "Corrected
formula" row at `l2=0.001` (the buggy isotonic layer applied to the
already-L2-fixed raw predictions — the correct baseline for isolating
THIS fix, not the pre-L2-fix numbers higher up this page) — re-run live
this session to confirm, not merely quoted:

| method | log-loss | Brier |
|---|---|---|
| model RAW (unaffected by this fix, either way) | 0.3994 | 0.2246 |
| model CALIBRATED, before this fix | 0.3967 | 0.2242 |
| model CALIBRATED, **after this fix** | **0.3959** | **0.2243** |

| outcome | series | ECE(w) before → after | ECE(q) before → after | slope before → after | 95% CI before | 95% CI after |
|---|---|---|---|---|---|---|
| NONE | raw | 0.0139 | 0.0162 | 0.587 (unaffected) | [0.552,0.623] | [0.552,0.623] |
| NONE | calibrated | 0.0039 → 0.0041 | 0.0064 → 0.0060 | **0.845 → 0.875** | [0.795,0.895] | **[0.824,0.925]** |
| YELLOW | raw | 0.0132 | 0.0141 | 0.618 (unaffected) | [0.581,0.656] | [0.581,0.656] |
| YELLOW | calibrated | 0.0020 → 0.0028 | 0.0047 → 0.0044 | **0.896 → 0.931** | [0.844,0.948] | **[0.878,0.984]** |
| RED | raw = calibrated | 0.0001 / 0.0026 (unaffected) | — | 0.115 (unaffected) | [0.007,0.224] | [0.007,0.224] |

RED's row is **bit-identical** before and after this fix, live-confirmed —
this module never fits or applies a calibrator for it (module docstring,
"RED is never calibrated"), so a defect in `_fit_isotonic_calibrator`
structurally cannot reach it.

### Verdict — re-confirmed on corrected numbers, not assumed to survive

**Both slopes moved materially further toward 1.0** (NONE 0.845→0.875,
YELLOW 0.896→0.931), on a real but SMALL pooled cost/benefit (log-loss
0.3967→0.3959 — better; Brier 0.2242→0.2243 — a wash; ECE moved by a few
thousandths in either direction, consistent with only 121/58,461 rows
(0.2%) actually changing, an order of magnitude smaller than minutes' own
3.0%). §7.1 GATE: still PASSED. `calibrated_not_worse_than_raw()`: still
`True`. **The "still earning its place" verdict from the L2-fix section
above is RE-CONFIRMED here, on numbers taken with the saturation bug
actually fixed — it was not simply assumed to survive.** Neither slope's
CI includes 1.0 yet (NONE upper bound 0.925, YELLOW upper bound 0.984) —
narrower gaps than before the fix, but the same residual, statistically
real departure named in the "Nested out-of-sample NONE/YELLOW calibration"
section above still holds, and for the same stated reason (no second-stage
calibrator built this session either).

**RED's Architect ruling is unaffected and was not re-opened** — the
brief's own explicit instruction. RED's numbers are bit-identical to the
L2-fix section's own table, live-confirmed this session, because this
defect could not reach a series that is never calibrated in the first
place.

### What changed, what did not

- **Fixed**: `_fit_isotonic_calibrator`'s `bin_y` — a Jeffreys
  `Beta(0.5, 0.5)` continuity correction, `(successes + 0.5) / (bin_w +
  1.0)`, applied per-bin before the PAVA fit — ported verbatim from
  `fplai.models.minutes._fit_isotonic_calibrator`, not reinvented.
- **Unchanged**: NONE/YELLOW's isotonic calibrator still ships
  (`calibrate=True` default) — the verdict moved in the RIGHT direction
  (closer to 1.0), not away from it, so there was no case for reconsidering
  the default.
- **Unchanged, explicitly not re-opened per this task's brief**: RED stays
  BASE-RATE-ONLY, Architect ruling. Bit-identical numbers, confirmed live.
- **Unchanged**: the §7.1 gate verdict (`PASS`).
- **New tests**: `tests/test_cards.py::
  test_isotonic_calibrator_never_saturates_to_exact_zero_or_one` (hand-
  verified to FAIL against a temporarily reverted copy of this module,
  producing `cal.y == (0.0, 1.0)`, before being trusted — CLAUDE.md lesson
  5) and `::test_isotonic_calibrator_smoothing_converges_to_raw_rate_at_
  scale` (non-regression: the correction must not distort a well-populated
  bin).
- **Verified against the real store**: `scripts/fit_cards.py`'s own
  walk-forward path, run twice (once with the fix temporarily reverted to
  confirm the "before" numbers above, once restored — `diff -q` confirmed
  byte-identical restoration both times, never `git`), full suite
  `uv run pytest tests/test_cards.py -q` — 53 passed.
