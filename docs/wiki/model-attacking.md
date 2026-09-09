# Attacking-involvement model — Phase 2, E5 (XL-Coder, session `s004`)

> `src/fplai/models/attacking.py` (new, then the L2 scaling fix — session
> `s005`, §12), `tests/test_attacking.py` (new, 34 tests; **35 as of the L2
> fix**, §12), `scripts/fit_attacking.py` (new), `src/fplai/schemas.py`
> (append-only: `PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK`,
> `PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_DATASET` constants — no
> existing entry touched). Implements blueprint §4 ("Attacking" row: "Player
> share of team goals / assists. Minutes-weighted; set-piece and penalty
> responsibility as explicit features."), §7.1 (calibration before points),
> §12.2 (derived-fact labelling). Fourth real derived-capability module,
> fourth real consumer of import-time registration — and the first to
> **compose** two other models' outputs (`team_strength.ScorelinePMF`,
> `MinutesPMF`) rather than fitting standalone.

## 1. What this is

A player's own goal count in one fixture is a **Binomial thinning** of his
team's goal count: if the team scored `g` goals and this player's per-goal
involvement probability is `p`, his own goal count is genuinely
`Binomial(g, p)` — bounded by `g`, unlike an independently-fit rate that can
exceed it. This module fits `p` (the share) directly, then **composes** the
fitted share with a caller-supplied team-goal distribution and minutes
distribution at predict time — it never fits or infers a team scoring rate
itself, and it never imports `fplai.models.team_strength` or
`fplai.models.minutes`:

```python
from fplai.models.attacking import fit_attacking_model, predict_attacking_pmf
from fplai.store import BitemporalStore
from datetime import datetime, timezone

store = BitemporalStore()
params = fit_attacking_model(store, as_of=datetime(2026, 8, 22, tzinfo=timezone.utc))

# team_goals_marginal: e.g. list(enumerate(scoreline_pmf.home_goals_marginal()))
#   from a fitted fplai.models.team_strength.ScorelinePMF
# minute_exposure: e.g. the six MinutesPMF band midpoints and their weight
pmf = predict_attacking_pmf(
    params, feature_row, element=..., fixture=..., stat="goals",
    team_goals_marginal=[(0, 0.30), (1, 0.40), (2, 0.20), (3, 0.10)],
    minute_exposure=[(0.0, 0.05), (15.0, 0.05), (45.0, 0.05), (67.0, 0.10), (82.0, 0.15), (90.0, 0.60)],
)
pmf.p_involved(), pmf.expected_count()
```

`AttackingInvolvementPMF.p_involved()`/`expected_count()` are convenience
properties **computed from** the full count PMF — never a substitute for it
(CLAUDE.md rule 5). Both `goals` and `assists` are fitted independently
(`STATS = ("goals", "assists")`) — a creative player's assist share and a
poacher's goal share are genuinely different profiles.

> **Reliability measured 2026-08-29** — this module shipped without it, which was a logged
> defect against it. `docs/wiki/calibration-report.md` closed the gap using the shared
> `fplai.calibration` machinery rather than adding a fifth per-model implementation:
> **goals slope 0.969, CI [0.930, 1.008] — a clean PASS**; **assists slope 0.847,
> CI [0.804, 0.889] — PASS-with-accepted-departure.** ECE 0.0286 / 0.0305 equal-width.
> `attacking.py` itself needed no change: `WalkForwardResult` already exposed `y_true` and
> `p_model` publicly — the gap was that nobody had scored them, not that they were unreachable.


## 2. Data — verified live, 2026-08-28, not re-derived from the brief

`expected_goals`/`expected_assists` are 100% NULL for 2019-20/2020-21/
2021-22 and 100% populated 2022-23 onward — a hard boundary, same shape the
minutes model hit for `starts`. Per counts: 26,505 / 29,725 / 27,605 /
29,747 (2022-23 → 2025-26). This module excludes the three earliest seasons
**entirely** (features and target, not just target) — the same "let the
data say which seasons qualify" discipline `minutes`/`defensive_
contribution` both already establish.

**`y <= n` verified over every 2022-23+ row before this module's design was
chosen, not assumed**: zero rows have `goals_scored > team_goals_this_
fixture`; zero have `assists > team_goals_this_fixture`. Max `team_goals`
in a single fixture: 9. Max `goals_scored`/`assists` for one player in one
fixture: 4 each. `build_training_table` re-checks this on every window it
is called with and raises rather than silently proceeding if a future data
revision ever breaks it.

Training table: **113,260 rows**, 2022-23 → 2025-26 (`build_training_
table(store, as_of=datetime(2026,8,22,tzinfo=UTC))`); **45,787** of those
have `minutes > 0` and are what the Binomial-share regression actually
fits on.

## 3. Why Binomial regression, not the NB2 count regression `defensive_
   contribution` uses

DC fits a raw count (`count ~ NegBinom(mu, alpha)`) because there is no
natural "number of trials" for a defensive action — a player can rack up
an unbounded count of tackles/blocks/interceptions in a match. A goal is
different: the team's own goal total is a genuine, observable trial count,
and a player's own goals in that match cannot exceed it (barring an own
goal, which credits no scorer on that team at all). Modelling
`Binomial(n=team_goals, p=share)` is therefore not an approximation of
"share of team goals" — it **is** that quantity, and it is automatically
sum-consistent in a way an independently-fit rate is not.

## 4. Minutes-weighted, without folding minutes into the rate

`p = (minutes_this_fixture / 90) * sigmoid(X @ beta)` at **fit** time
(real observed minutes — this is what lets `sigmoid(X @ beta)` be read as
"this player's per-team-goal involvement probability if he played the full
90"). At **predict** time, `minute_exposure` supplies a full mixture over
minute values (the same `MinutesPMF`-shaped `Sequence[(minutes, weight)]`
`defensive_contribution.predict_dc_pmf` already established) — the model
never collapses a minutes forecast into one scalar before computing
involvement from it. A `minute_exposure` entry of `(0.0, w)` naturally
produces a `Binomial(g, 0)` — a spike at 0 — with no special-casing needed
(`scipy.stats.binom.pmf` handles `p=0` directly).

## 5. Set-piece and penalty responsibility — declared known-absent

Per this task's brief and the minutes model's own precedent
(`KNOWN_ABSENT_FEATURES`), this module declares rather than proxies:

```python
KNOWN_ABSENT_FEATURES = ("primary_set_piece_taker", "penalty_taker_duty")
```

No capability in this store's registry carries current set-piece order.
FPL's own `penalties_missed`/`penalties_saved` fields exist (64 and 49
nonzero rows respectively over 2022-23+, verified live) but are a biased,
sparse proxy for **duty**, not a measurement of it — a never-assigned
penalty-taker and a perfect one both read `0`. Neither field is in
`NUMERIC_FEATURE_COLUMNS_ATTACKING` (`tests/test_attacking.py` asserts
this by name). What the model does implicitly carry: a recognised
penalty-taker's real historical goal tally already flows into his own
trailing goals/xG rate. What it genuinely lacks: a forward-looking flag for
a player who has *just* been handed duty (transfer, incumbent injury,
managerial reshuffle) — the same "regime change" gap `fplai.models.minutes`
names for its own rotation-prior features.

## 6. A real diagnostic this session had to fix — feature standardisation

An unstandardised design matrix, fit at this module's DC/minutes-inherited
default `l2_penalty=1.0`, converges L-BFGS-B to a **genuine** stationary
point (gradient norm ~1e-5, not a premature stop) that is nonetheless
football-nonsensical: `games_played_this_season` (raw range 0..37) reads as
the single largest-magnitude coefficient, every `player_trailing_{xg,
goals}_*` coefficient sits under 0.002, and `position=FWD` reads *negative*
for the goals model. Diagnosed directly (not guessed): the fixed
`l2_penalty` charges the same quadratic cost per unit of `beta` regardless
of what a unit buys on that feature's own scale, and a two-orders-of-
magnitude scale gap (0..37 vs 0..2) means the large-scale, less-informative
feature moves `eta` cheaply while the small-scale, genuinely informative
trailing features get crushed toward zero.

**Fix**: every numeric column is standardised (z-score, `std` floored at
`1e-8`) inside `AttackingFeatureSpec`, fitted once from the training table
and frozen (never recomputed at predict time — `AttackingFeatureSpec.
numeric_means`/`numeric_stds`). Position dummies and the intercept are left
un-standardised (already 0/1-scaled). Standardising alone was not enough at
`l2_penalty=1.0` — the gate still failed outright (§7 below) — because this
Binomial-share likelihood's natural per-parameter curvature is weak (a
genuinely rare per-team-goal event, unlike DC's 3.6-8.4% threshold-hit rate
or minutes' ~50%+ start rate) and a ridge penalty tuned for those
likelihoods over-shrinks this one. Swept `{1.0, 0.2, 0.05, 0.03, 0.015,
0.01, 0.005}` against the real store; the gate first passes at `0.015` and
keeps improving through `0.005`. **`l2_penalty=0.01` is the shipped
default** — clears both baselines with a real margin without sitting at
the sweep's most aggressive, least-tested end. Still a stated, reasoned
starting point per this task's brief, not a claimed optimum.

## 7. §7.1 gate — walk-forward, real numbers

`scripts/fit_attacking.py --as-of 2026-08-22T00:00:00Z --min-train-rows 2000`,
against the real store, scoring the derived quantity `P(player registers
>= 1 {goal|assist} this fixture) = 1 - (1 - p_share)^n` (`n` = that
fixture's real team-goal count, out-of-sample; `predict_attacking_pmf`
never sees a real `n`, only the caller's own `team_goals_marginal`) against
two §7.1-minimum baselines:

| goals | log-loss | Brier |
|---|---|---|
| model (Binomial) | **0.2347** | **0.0673** |
| baseline: position rate | 0.2687 | 0.0746 |
| baseline: player trailing rate | 1.3268 | 0.0840 |

| assists | log-loss | Brier |
|---|---|---|
| model (Binomial) | **0.2370** | **0.0676** |
| baseline: position rate | 0.2689 | 0.0719 |
| baseline: player trailing rate | 1.4229 | 0.0829 |

**§7.1 GATE: PASSED for both stats, on both metrics.** 143 folds, 43,505
out-of-sample evaluation rows each. The raw player-trailing-rate baseline's
huge log-loss (1.33/1.42) is a real property of that baseline, not a bug —
a 5-round empirical rate can legitimately hit an extreme (0 or ~1), and
being confidently wrong once is expensive under log-loss; it is still a
required §7.1 minimum baseline and the model beats it regardless.

OOS `P(involved)` residual (model minus actual 0/1 outcome): goals
mean=-0.0286, std=0.2578; assists mean=-0.0305, std=0.2583 — both carried
into the persisted `calibration_reference`/`calibration_residual_mean`/
`calibration_residual_std` via `write_attacking_pmfs`, never discarded
(blueprint §11's residual-as-uncertainty discipline, generalised here as
`fplai.derived.CalibrationReference` already generalises it beyond DC).

## 8. Known limitation, stated rather than hidden

Small-sample noise in the position ordering for goalkeepers specifically:
at the shipped `l2_penalty=0.01`, the fitted `position=GK` coefficient for
the goals model sits *above* `position=DEF`'s (i.e. the fit reads GK as a
slightly less negative goal-share than DEF), which is football-implausible
— GKs essentially never score. This is very likely a small-sample artefact
(goalkeepers registering a goal is such a rare event that the regularised
MLE has little to pin the coefficient on either side of zero) rather than
a real signal, and it does not affect the §7.1 gate (which scores the
pooled `P(involved)` quantity, not per-position coefficient ordering).
Reported rather than smoothed away — the same posture `defensive_
contribution`'s own module docstring takes for Elliot Anderson's round-38
reversal.

## 9. No calibration layer (isotonic) built here

`fplai.models.minutes` added a nested isotonic P(start) calibrator in
session s003; `fplai.models.defensive_contribution` did not add an
analogous layer. This module follows DC's precedent: the §7.1 gate is
"beat both naive baselines" and it does, honestly, without a further
calibration pass. A real calibration layer is a genuine, separate design
decision this task's brief never asked for — left as a named seam for a
future session.

## 10. Persistence — `stat` is part of the entity key, not a value field

`entity_key=("season", "round", "element", "fixture", "stat", "count")`.
`stat` (`"goals"`/`"assists"`) MUST be part of the key: a single (season,
round, element, fixture, count) would otherwise collide between the two
independently-fitted PMFs — unlike DC's `group`, which is a VALUE field
because a player only ever belongs to one DC group at a time (determined
by position), a single player-fixture genuinely carries TWO PMFs here
simultaneously.

## 11. Findings for the blueprint

- **The team-style feature seam DC left for a future session (PL API
  `team.match_stats@match`, zero rows in this store) also applies here** —
  this module deliberately carries no team-level feature at all (§ "Why
  this is NOT re-deriving team scoring rates"), so the seam does not block
  it, but a future session adding that capability would let DC's team-style
  proxy stop leaning on DC's own counters.
- **Feature standardisation is now a real, load-bearing design question for
  any FUTURE Phase 2 model that mixes count features (0..37 range) with
  rate features (0..2 range) under a shared `l2_penalty` convention** — DC
  and minutes happened not to hit this because their own feature sets are
  more homogeneously scaled; the next model built on this pattern should
  check its own feature-scale spread before assuming DC/minutes'
  `l2_penalty=1.0` transfers.
- **olbauday's `By Gameweek/GW{N}/shots.csv` (2024-25+, FPL-element-aligned
  shot-level data, deferred from E2b story 10b) would materially improve
  this specific model** if it were available — genuine shot-by-shot
  attribution would let a future revision fit the ACTUAL per-goal scorer
  identity directly (a true multinomial over on-field players) rather than
  the Binomial-share approximation this module uses, and would give a real,
  observable basis for a penalty/set-piece duty feature instead of leaving
  it declared known-absent. Not reached for in this session — genuinely
  deferred, per this task's brief.

## 12. L2 scaling bug fixed — session `s005`, measured before touching a default

`_binomial_share_neg_log_lik_and_grad` divided the log-likelihood by
`n_rows` (a per-row average) but added the ridge penalty `l2 *
sum(beta**2)` **unscaled** — for `n_rows` in the tens of thousands,
effectively `l2*n_rows` against the averaged likelihood. Same defect
`fplai.models.saves`/`fplai.models.minutes`/`fplai.models.defensive_
contribution` each independently found and fixed this session
(`docs/wiki/model-minutes.md` §13 has the fullest derivation). **This
module already knew it had a scaling problem and had worked around it by
shrinking `l2_penalty` to `0.01`** (§6 above) — §6's own sweep, run at
the OLD unscaled formula, found `l2_penalty=1.0` (DC/minutes' shared
default) failed the §7.1 gate outright. The corrected formula changes what
`l2_penalty=0.01` effectively means, so it was re-measured against the
real store, RAW and swept across a grid spanning both the shrunken default
and the unshrunk `1.0`, before touching anything.

### 12.1 Arm A — the shipped baseline, reproduced at the OLD formula before touching it

`min_train_rows=2000`, real store, both stats, `l2_penalty=0.01` (the
shipped default, unchanged): this reproduces §7's exact table (§7's
0.2347/0.0673 and 0.2370/0.0676) exactly — the trusted starting point, not
re-derived from scratch.

| metric | goals | assists |
|---|---|---|
| log-loss | 0.2347 | 0.2370 |
| Brier | 0.0673 | 0.0676 |
| ECE (equal-width) | 0.0286 | 0.0305 |
| ECE (quantile) | 0.0308 | 0.0332 |
| calibration slope (95% CI) | 0.969 [0.930, 1.008] | 0.847 [0.804, 0.889] |
| mean(p_model) vs mean(y_true) | 0.1138 vs 0.0852 (+0.0286) | 0.1096 vs 0.0791 (+0.0305) |
| beats both §7.1 baselines | True | True |

### 12.2 Corrected formula at the SAME unchanged default (`l2=0.01`) — the honest, non-obvious comparison

| metric | goals | assists |
|---|---|---|
| log-loss | 0.2187 (-6.8%) | 0.2254 (-4.9%) |
| Brier | 0.0639 (-5.1%) | 0.0647 (-4.3%) |
| ECE (equal-width) | 0.0071 (-75.2%) | 0.0085 (-72.1%) |
| ECE (quantile) | 0.0079 (-74.4%) | 0.0085 (-74.4%) |
| calibration slope (95% CI) | 0.870 [0.838, 0.903] | 0.820 [0.783, 0.857] |
| mean(p_model) vs mean(y_true) | 0.0837 vs 0.0852 (-0.0015) | 0.0792 vs 0.0791 (+0.0001) |
| beats both §7.1 baselines | True | True |

**Read this table carefully — it is not a clean sweep in every column, and
the one column that looks like a regression is the misleading one.** The
Cox calibration slope moves AWAY from 1.0 at the unchanged default (goals
0.969 → 0.870; assists 0.847 → 0.820) — read naively, that looks like the
fix made calibration worse. It did not. **The mean-level diagnostic shows
why**: the OLD formula's mean prediction OVERSHOT the true rate by a large
margin (goals +0.0286, a 34% relative overestimate of a true 8.5% rate;
assists +0.0305, a 39% relative overestimate of a true 7.9% rate) while its
slope happened to read close to 1 anyway — Cox slope measures whether
predicted LOG-ODDS *spread* scales correctly against true log-odds spread,
which is a different question from whether the overall LEVEL is right, and
the two can point opposite directions. The corrected formula gets the mean
right (both within 0.0015 of the true rate) but under-spreads the log-odds
slightly (slope <1). **ECE — which scores both level and spread together,
empirically, via binned deviation — is unambiguous: it fell 72-75% at the
unchanged default.** This is the single clearest evidence the fix is a
genuine improvement here, not a metric trade dressed up; report the whole
table, not the one column that looks worst.

### 12.3 Swept `l2` under the corrected formula — the curve, not just the winner

Goals:

| l2 | log-loss | Brier | ECE (w) | ECE (q) | slope [95% CI] | mean(p) vs 0.0852 |
|---|---|---|---|---|---|---|
| 0.001 | 0.2200 | 0.0639 | 0.0072 | 0.0080 | 0.853 [0.821, 0.885] | 0.0835 |
| 0.005 | 0.2190 | 0.0639 | 0.0073 | 0.0080 | 0.866 [0.834, 0.898] | 0.0836 |
| 0.01 (shipped) | 0.2187 | 0.0639 | 0.0071 | 0.0079 | 0.870 [0.838, 0.903] | 0.0837 |
| 0.05 | 0.2183 | 0.0638 | 0.0068 | 0.0076 | 0.880 [0.847, 0.912] | 0.0838 |
| 0.1 | 0.2182 | 0.0638 | 0.0066 | 0.0074 | 0.884 [0.851, 0.916] | 0.0839 |
| 0.5 | 0.2181 | 0.0637 | 0.0064 | 0.0071 | 0.895 [0.863, 0.927] | 0.0842 |
| 1.0 | 0.2182 | 0.0637 | 0.0060 | 0.0068 | 0.903 [0.870, 0.935] | 0.0845 |
| 5.0 | 0.2189 | 0.0637 | 0.0048 | 0.0050 | 0.929 [0.896, 0.962] | 0.0858 |
| 10.0 | 0.2196 | 0.0638 | 0.0042 | 0.0049 | 0.944 [0.910, 0.977] | 0.0872 |

Assists:

| l2 | log-loss | Brier | ECE (w) | ECE (q) | slope [95% CI] | mean(p) vs 0.0791 |
|---|---|---|---|---|---|---|
| 0.001 | 0.2268 | 0.0647 | 0.0087 | 0.0087 | 0.797 [0.760, 0.833] | 0.0791 |
| 0.005 | 0.2257 | 0.0647 | 0.0086 | 0.0086 | 0.815 [0.778, 0.852] | 0.0792 |
| 0.01 (shipped) | 0.2254 | 0.0647 | 0.0085 | 0.0085 | 0.820 [0.783, 0.857] | 0.0792 |
| 0.05 | 0.2250 | 0.0647 | 0.0085 | 0.0085 | 0.829 [0.792, 0.867] | 0.0794 |
| 0.1 | 0.2249 | 0.0647 | 0.0085 | 0.0083 | 0.832 [0.795, 0.870] | 0.0795 |
| 0.5 | 0.2248 | 0.0646 | 0.0080 | 0.0080 | 0.840 [0.803, 0.878] | 0.0798 |
| 1.0 | 0.2248 | 0.0646 | 0.0078 | 0.0079 | 0.845 [0.808, 0.883] | 0.0800 |
| 5.0 | 0.2252 | 0.0647 | 0.0070 | 0.0076 | 0.863 [0.824, 0.901] | 0.0811 |
| 10.0 | 0.2257 | 0.0647 | 0.0071 | 0.0080 | 0.869 [0.830, 0.908] | 0.0824 |

**Unlike DC, this is not a real either/or tradeoff across most of the
grid.** Log-loss/Brier/ECE and the calibration slope all move in the SAME
(improving) direction as `l2` increases from `0.001` through roughly `1.0`
— there is no point in that range where improving the slope costs
log-loss. Only past `l2≈1` does a shallow tradeoff appear: log-loss/Brier
creep back up very slightly (0.2181→0.2196 goals, a 0.7% relative move)
while the slope keeps climbing toward 1.0 (0.903→0.944), still short of it
even at `l2=10`, the most aggressive point tested. `beats_both_baselines()`
is `True` at every point of both grids.

### 12.4 What changed, what did not

- **Fixed**: `_binomial_share_neg_log_lik_and_grad`'s ridge term, in both
  loss and gradient, scaled by the same `1/n_rows` the likelihood already
  carries.
- **Unchanged**: `AttackingModelConfig.l2_penalty` default (`0.01`) —
  clears the §7.1 gate with a wide margin at every point of the swept
  grid, and sits inside the region that already captures most of the
  log-loss/Brier/ECE improvement (§12.2). **Not** retuned toward `l2=0.5`
  or `1.0` even though that region shows a further, real slope
  improvement with essentially no log-loss cost — chasing the
  calibration-optimal point in the grid is a Phase-7 calibration-tuning
  decision, the same posture `fplai.models.defensive_contribution`'s own
  §9.3 takes for its own (real, unlike this one) tradeoff, not a scaling-
  bug fix. Flagged for the Architect / a future calibration pass.
- **Unchanged**: the §7.1 gate verdict (`PASS`, both stats) at every point
  of both swept grids — a real performance fix that leaves the gate
  intact, with a genuine, better-characterised calibration picture than
  §7/§9's own numbers (crossed-out §9 slope figures 0.969/0.847 were valid
  under the formula that produced them, but §12.2 shows they were flattering
  a badly-biased mean, not a well-calibrated model).
- **Verified against the real store**: `scripts/fit_attacking.py`'s own
  walk-forward path, `tests/test_attacking.py::
  test_binomial_share_l2_penalty_is_scaled_by_inverse_n` (a formula-
  discriminating pinning test, hand-verified to FAIL against a temporarily
  reverted copy of the old formula before being trusted, CLAUDE.md lesson
  5), full suite `uv run pytest tests/test_attacking.py -q` — 35 passed.
