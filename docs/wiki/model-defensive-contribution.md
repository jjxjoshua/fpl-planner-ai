# Defensive-contribution (DC) estimator — Phase 2, E5 (XL-Coder, session `s003`)

> `src/fplai/models/defensive_contribution.py` (new), `tests/test_defensive_contribution.py`
> (new, 49 tests), `scripts/fit_defensive_contribution.py` (new),
> `scripts/pin_dc_thresholds.py` (new), `src/fplai/schemas.py` (append-only:
> `PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK`,
> `PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_DATASET` constants — no existing
> entry touched). Implements blueprint §4 ("Defensive" row), §7.1 (calibration
> before points), §11 (DC calibration constraint), §12.2 (derived-fact
> labelling). Third real derived-capability module, third real consumer of
> import-time registration. Sourcing: `docs/wiki/defensive-contribution.md`
> (`fpl-elite`'s report).

## 1. What this is

DC pays a flat 2 points per match if a player's summed defensive actions
clear a THRESHOLD count — never mind by how much. This module fits a
**Negative Binomial (NB2) regression** on the raw per-fixture count (never a
logistic classifier on the pass/fail event directly — see §4), and derives
`P(DC awarded)` from the fitted count distribution:

```python
from fplai.models.defensive_contribution import build_dc_threshold_set, fit_dc_model, predict_dc_pmf
from fplai.store import BitemporalStore
from datetime import datetime, timezone

store = BitemporalStore()
threshold_set = build_dc_threshold_set(store)  # LIVE game_config read + press-sourced counts
params = fit_dc_model(store, as_of=datetime(2026, 8, 22, tzinfo=timezone.utc), threshold_set=threshold_set)
pmf = predict_dc_pmf(
    params, feature_row, element=..., fixture=..., position="MID",
    minute_exposure=[(0.0, 0.05), (15.0, 0.05), (45.0, 0.05), (67.0, 0.10), (82.0, 0.15), (90.0, 0.60)],
)
pmf.p_dc_awarded(), pmf.expected_count(), pmf.expected_dc_points()
```

`DCPMF.p_dc_awarded()`/`expected_count()`/`expected_dc_points()` are
convenience properties **computed from** the full count PMF — never a
substitute for it (CLAUDE.md rule 5). `minute_exposure` is a full
`(minutes_value, probability)` mixture, not a scalar — see §5.

## 2. Data — verified live, 2026-08-22, not re-derived from the brief

All four DC counters (`recoveries`, `tackles`, `clearances_blocks_
interceptions`, `defensive_contribution`) are in-store for **2025-26 only**
— 29,747 rows each after `effective_at()`'s own dedup (the same 10
exact-duplicate rows `model-team-strength.md`/`model-minutes.md` already
document for this dataset; the brief's cited 29,757 is the pre-dedup count).

**Composition, checked over every 2025-26 row, not a sample:**

| Group | Formula | Mismatches |
|---|---|---|
| `DEF_CBIT` | `tackles + clearances_blocks_interceptions` | 0 / 9,733 |
| `MID_FWD_CBIRT` | `tackles + clearances_blocks_interceptions + recoveries` | 0 / 16,587 |

`defensive_contribution` is **always 0 for GK** (3,427/3,427 rows) even
though a keeper's own `recoveries` reaches as high as **18** in this store —
FPL's own field is already position-gated; this module never recomputes a
GK's "would-be" count, because the rules define no CBIT/CBIRT formula for
goalkeepers at all.

`game_config`'s `scoring.defensive_contribution` (live-read 2026-08-22):
`{"DEF": 2, "FWD": 2, "GKP": 0, "MID": 2}`. The string `"threshold"` appears
**zero times** anywhere in the payload — checked directly
(`"threshold" in payload_raw.lower()` → `False`). Base hit rates over every
2025-26 fixture row (including 0-minute rows): DEF 8.44%, MID/FWD 3.59% —
genuinely rare events.

## 3. Threshold provenance — the crux of this task

`docs/BLUEPRINT.md` §11 and CLAUDE.md rule 4 both name DC thresholds as
things that must never be hardcoded — and they are genuinely absent from
the API, a real tension, not an oversight. This module's answer:

- `DCThresholdProvenance` is the injected unit: a group's count threshold,
  points, source, source date, and a `verified: bool`.
- `PRESS_DC_COUNT_THRESHOLDS` (DEF 10, MID/FWD 12) — from
  `docs/wiki/defensive-contribution.md` §1.1, premierleague.com, 20 Jul
  2026, Tier HIGH-but-press. **`verified=False` on both** — this is the
  FALLBACK now, not the normal case; see §"Pinned by observation" below.
- `read_dc_points_by_position(store)` / `build_dc_threshold_set(store,
  as_of=...)` — LIVE-reads the POSITION GATE (GKP=0) from `game_config`,
  `verified=True` by construction (it came off the API), and composes it
  with the count thresholds. **Given an `as_of`, it resolves the latest
  PINNED observation from the store and returns `verified=True`;** with no
  pin visible by that timestamp it falls back to the press value at
  `verified=False`. `as_of=None` keeps the old press-only behaviour.
- Every entry point (`build_training_table`, `fit_dc_model`,
  `predict_dc_pmf`, `walk_forward_validate`) takes `threshold_set` as an
  **explicit parameter** — never a module-level literal, never a hidden
  global cache (an earlier draft of `walk_forward_validate` used one; the
  Architect-facing standard this project holds — "no code path is unsafe,
  not just the sanctioned one" — argued against it and it was removed
  before this task closed).

**Structural labelling, not a docstring promise.** `DCPMF.threshold_
verified`/`count_threshold` and the persisted row columns of the same name
mean a consumer of the derived dataset six weeks from now can filter on
`threshold_verified == False` directly, without reading this module's
source — the same standard `is_modelled`/`derived_from` already set for
every derived capability (blueprint §12.2).

**A genuine asymmetry, worth restating:** the POSITION gate is
machine-verified and effectively permanent. The COUNT threshold could in
principle differ under an `overrides` block on a specific event (blueprint
§11 flags this as unranked-out, not ruled out) — which is why a pin is
recorded per gameweek and resolved as-of, rather than being frozen once
and trusted forever. These are different kinds of uncertainty and are
never blurred into one flag.

### `scripts/pin_dc_thresholds.py` — closing the loop

Reads `event/{gw}/live/` once a gameweek has settled and either CONFIRMS
or CONTRADICTS 10/12 by observation — bounds, not a guess: for each group,
`T <= min(count | DC awarded)` and `T > max(count | DC not awarded)`; if
the bounds meet exactly, T is pinned; if they gap, both bounds are
reported honestly as inconclusive; if they contradict (the same count with
both outcomes), that is raised loudly, never silently resolved.

### PINNED BY OBSERVATION, 2026-08-27 — and the flaw that had to be fixed first

**Result: `DEF_CBIT` = 10 (n=108), `MID_FWD_CBIRT` = 12 (n=182). Both
MATCH the press values.** Bounds met exactly: DEF 9 → 10, MID/FWD 11 → 12.
This closes the last press-only dependency in the rule set.

**The first run against the real settled GW1 returned INCONCLUSIVE for
both groups, and the reason was structural, not a thin gameweek.** FPL's
`explain` blocks list **only identifiers that SCORED**. Across all 610
elements there are 31 `defensive_contribution` entries, every one
`points=2`, and **zero `points=0` entries for any identifier at all**. A
defender with 9 CBIT has no DC entry — not a zero-point one. So
`max(count | points == 0)` was unobservable **by construction**, and this
script could never have converged, on any gameweek, however many settled.

The unscored count lives in **`stats["defensive_contribution"]`**, present
for every element. It is the RAW COUNT, not the awarded points — verified
two ways on the same real payload: it equals the `explain` `value` on
every scoring row, and it ranges 0..21, which points (capped at 2) cannot.

Two exclusions, both deliberate:

- **Zero-minute elements.** A count of 0 for someone who never played is
  true but carries no information. (This is why `n_observations` dropped
  from 202/341 to 108/182 — the smaller number is the honest one.)
- **Multi-fixture elements.** `stats` is per-element-per-GAMEWEEK while
  `explain` is per-FIXTURE, so on a double gameweek an unscored element's
  total cannot be attributed to either fixture — a 14 could be 7+7, and
  treating the total as one fixture's count would manufacture a false
  CONTRADICTION against a real threshold. Excluded and reported as
  `n_unattributable`, never silently folded in. GW1 is a single gameweek
  so this excluded nothing; it is written now precisely so it is not
  discovered on the first DGW.

### Persistence — `game.dc_threshold_observation@gameweek`

Every attempt is written to the store (`dc_threshold_observations`),
including inconclusive and contradictory ones — a gameweek that failed to
pin is evidence about that gameweek, and dropping it would leave the
record silently flattering.

**Registered as OBSERVED, not derived** (Architect ruling 2026-08-27),
same side of the provenance partition as `job.heartbeat@run` and for the
same reason: a pin is a deterministic bounds-meeting computation over real
`(count, points)` pairs, with no fitted parameter and no residual, so
§12.2's derived contract (`calibration_reference` and friends) would be
meaningless for it.

`resolve_dc_threshold_observations(store, as_of=...)` takes the
chronologically latest row **that actually pinned a value**, so an
inconclusive later gameweek can never silently withdraw an earlier real
pin — a different collapse from the one `as_of()` performs, which is why
it reads the raw `observations()` stream and does that step by hand.

**Latent store bug this flushed out:** the entity key is `(season, group,
round)`, and `group` is a reserved SQL word. `as_of()` interpolated entity
-key columns into `PARTITION BY` as bare identifiers, so it emitted
`PARTITION BY season, group, round` and DuckDB rejected it outright —
latent since the beginning, for every dataset. Fixed with `_quote_ident()`
in `fplai.store`. Found by `tests/test_store_invariants.py` running
against the REAL store; fixtures had never used a reserved word, so they
could not have caught it.

No provider persists `event/{gw}/live/` to the store (`fplai.providers.
fpl`'s own module docstring: `event_live`/`event_status` are "reachable on
`FPLClient` directly ... unwrapped here"), so this script imports
`fplai.client.FPLClient` directly — unmodified, used exactly as
`fplai.providers.fpl` itself already uses it, inheriting its 2 req/s
rate-limiting/caching/backoff rather than reimplementing any of it.

**Live-verified this session:** GW1 carries `finished=False,
data_checked=False` in the real store, and the script exits **cleanly,
code 0**, with a clear "not settled yet" message — checked both via the
store's own `events` snapshot (instant, no live call) and, live, via
`event-status/` (`bonus_added: False` for every reported day of GW1) and a
fresh `bootstrap-static()` events entry. `element_type -> position` label
resolution was also checked live: `{1: 'GK', 2: 'DEF', 3: 'MID', 4:
'FWD'}`, read from `bootstrap-static()['element_types']`, never hardcoded.

**NOT live-verified**: the `explain`-block field-name assumptions
(`identifier`/`value`/`points`) — GW1 has not settled, so no real settled
payload exists yet to check the parsing branch against. Verified instead
against a fabricated payload (`tests/test_defensive_contribution.py`'s
group 5) matching the FPL live API's documented general shape. **Check
this against the real payload the first time a gameweek settles**, before
trusting a pinned value.

## 4. Why Negative Binomial, not logistic regression on the threshold event

`docs/wiki/defensive-contribution.md` §3.2: DC is a threshold statistic
governed by per-match **dispersion**, not the mean — two players with
identical season rates but different match-to-match volatility have very
different DC yields, and a classifier that only ever sees the binary
pass/fail outcome cannot express that at all. This module fits the RAW
COUNT instead: `count ~ NegBinom(mu, alpha)`, `log(mu) = X @ beta +
log(minutes/90)` (an exposure offset), `alpha` (dispersion, `var = mu +
alpha*mu^2`) a SEPARATE fitted parameter, not fixed to the Poisson case.
`P(count >= threshold)` is then derived from the fitted distribution, the
same "fit the richer quantity, derive the narrower one" relationship
`MinutesPMF`'s 6-band PMF has to `p_start()`.

Fit via deterministic L-BFGS-B with an **analytic gradient**
(`_nb_neg_log_lik_and_grad`), checked against finite differences
(`tests/test_defensive_contribution.py::
test_nb_neg_log_lik_gradient_matches_finite_differences`) — **proved to
fail first**: a deliberately broken gradient (dropping the `r/(mu+r)`
factor in `dL/d(eta)`) was checked against the same finite-difference
harness and failed (`allclose: False`, analytic `[0.062, 0.167, 0.156,
-0.059, -0.037]` vs numeric `[0.109, 0.133, 0.017, -0.023, -0.037]`) —
confirming the test is a genuine correctness proof, not a vacuous one.

**Not solved by this design**: components (tackles/CBI/recoveries) are
positively correlated WITHIN a match (wiki §3.2), and this module
sidesteps that specific failure by never modelling components separately
— but it does not model correlation ACROSS teammates in the same match
(two defenders on a team under siege both posting high counts the same
game). That is squad-level correlated Monte Carlo, blueprint §4.3/Phase 5,
explicitly out of scope here.

## 5. The team-style feature — Elliot Anderson

`docs/wiki/defensive-contribution.md` §4.3: *"DC is a team-style statistic
wearing a player's name... player identity is the SECOND input, not the
first."* Features, in the stated order:

1. **First — `team_trailing_dc_mean_5`**: the player's TEAM's trailing
   mean total `defensive_contribution` (FPL's own already-position-gated
   field, summed across every rostered player, at ROUND grain — never
   gameweek-aggregated across a double gameweek). A continuous proxy, not
   a team-name dummy (see design rationale in the module docstring: a
   dummy needs a promoted-team-prior mechanism for Hull/Ipswich/Coventry,
   who have zero 2025-26 top-flight matches; a smooth feature degrades
   gracefully via the same cold-start convention `fplai.models.minutes`
   already uses).
2. **Second — `player_trailing_count_{3,5,10}`**: the player's own
   trailing raw count (not per-90 normalised — see module docstring,
   "Feature design notes" for why).

### The demonstration, run live against the real store

`scripts/fit_defensive_contribution.py --show-team-transfer-effect
"Elliot Anderson" "Nott'm Forest" "Man City"`, holding Anderson's own
2025-26 trailing features fixed and swapping only the team-style feature:

| Team-style value used | `team_trailing_dc_mean_5` | `P(DC awarded)` | `E[count]` |
|---|---|---|---|
| Nott'm Forest, **season-mean** | 76.0 | **32.8%** | 9.73 |
| Man City, **season-mean** | 69.0 | **23.1%** | 8.41 |

The mechanism works: holding everything about Anderson himself fixed, the
team-style feature alone drops his predicted `P(DC awarded)` by ~30%
relative (32.8% → 23.1%) when the team context moves from Forest to City
— exactly the direction `docs/wiki/defensive-contribution.md` calls for.

**A genuine finding, reported rather than smoothed over.** The table above
uses each team's SEASON-MEAN trailing value. The literal **end-of-season
(round-38) 5-match snapshot** gives the OPPOSITE ordering for this specific
pair: Man City 89.2 > Nott'm Forest 86.8, which would have shown Anderson
predicted HIGHER at City (52.3%) than at Forest (48.8%) — the wrong
direction. Checked further: City's own `team_trailing_dc_mean_5` swings
from 0 (round 1) to 89.2 (round 38) with a **season mean of 69.0**, so the
round-38 value is itself an outlier within City's own season, not
representative — a genuinely noisy single 5-match window, not evidence
against the underlying possession-share hypothesis. A full ranking of all
20 clubs' round-38 snapshot values also independently supports the
possession-share story everywhere else it can be checked directly: Arsenal
68.4, Liverpool 62.6, Aston Villa 56.2 sit at the bottom (matching "elite
possession = low DC volume"); Leeds 103.2, Bournemouth 101.8, Crystal
Palace 100.8 sit at the top. **Man City is the one exception in this
metric**, and it is City's specific case (not the mechanism) that is
noisy at a 5-match window.

**Escalated finding for the blueprint/HANDOFF**: a single window size
(`team_trailing_window=5`) is doing two different jobs — in-season
week-to-week team-style tracking (where "recent form" is exactly the
right quantity) and cross-season carryover for a preseason prediction
(where a longer or season-level window is more robust, per the Anderson
case above). `predict_dc_pmf`'s caller controls which value is passed in
today (this module does not compute the exposure/team-value itself), but
a future revision should probably expose an explicit "carryover" query
(e.g. `team_season_mean_dc(store, team, season)`) alongside the in-season
rolling one, rather than relying on every caller to remember which window
is appropriate for which use.

## 6. Residual shape — real, not the Gaussian placeholder

`docs/HANDOFF.md` §3 flagged the DC estimator's `residual_mean`/`std` as a
PLACEHOLDER assuming near-Gaussian, "do not let it harden by default."
Real out-of-sample walk-forward numbers (2025-26, within-season, both
groups — see §7):

| Group | residual mean | residual std | **skewness** |
|---|---|---|---|
| `DEF_CBIT` | -0.134 | 3.512 | **+0.438** |
| `MID_FWD_CBIRT` | -0.120 | 3.222 | **+0.401** |

**The residual is NOT near-Gaussian.** Skewness ~0.40-0.44 is a real,
moderate right skew (a Gaussian residual has skewness 0) — consistent with
a count model whose occasional large positive outliers (a player posting
20+ actions in one match, verified max: DEF 23, MID/FWD 29) pull the
residual's tail right, while the count is bounded at 0 on the left. The
`CalibrationReference.residual_mean`/`residual_std` pair persisted by
`write_dc_pmfs` is therefore honest about its own limits — it carries the
first two moments only, per `fplai.derived.CalibrationReference`'s own
documented minimum contract, and this wiki entry is where the fuller shape
(the skewness this session actually measured) is recorded for a future
revision that wants to carry a richer residual.

## 7. §7.1 gate — walk-forward, within 2025-26 (the only season with DC data)

`scripts/fit_defensive_contribution.py`, real store, `min_train_rows=500`:

| Group | n_folds | n_eval | model log-loss | model Brier | group-rate log-loss | group-rate Brier | player-trailing log-loss | player-trailing Brier | **Gate** |
|---|---|---|---|---|---|---|---|---|---|
| `DEF_CBIT` | 33 | 3,425 | **0.4523** | **0.1528** | 0.5147 | 0.1662 | 2.0170 | 0.1688 | **PASSED** |
| `MID_FWD_CBIRT` | 35 | 6,236 | **0.2395** | **0.0730** | 0.3048 | 0.0825 | 1.0256 | 0.0775 | **PASSED** |

Both groups beat BOTH required baselines (per-group base rate, per-player
trailing rate) on BOTH metrics (log-loss, Brier) — the gate as specified,
not tuned to pass it. The player-trailing baseline's very poor log-loss
(2.02 / 1.03) despite a competitive Brier is a real, unsurprising artefact
of a naive short-window rate estimator occasionally predicting exactly 0
or 1 and being penalised hard by log-loss's boundary behaviour when the
outcome disagrees — not a bug in the scoring, and exactly the kind of
naive baseline the NB model is expected to dominate.

Fitted dispersion (both groups genuinely overdispersed relative to
Poisson, `alpha = 1/r`): `DEF_CBIT` alpha=0.179 (r=5.58), `MID_FWD_CBIRT`
alpha=0.202 (r=4.96).

## 8. What was NOT done — declared, not hidden

- **Feature scaling.** The L2 penalty (`l2_penalty=1.0`) applies uniformly
  across features on very different raw scales (`team_trailing_dc_mean_5`
  is O(50-100); `player_trailing_count_*` is O(2-15)) — this shrinks the
  team feature's coefficient less aggressively in absolute terms than it
  might if features were standardised first. The gate still passes
  comfortably at these defaults (both UNCALIBRATED, same provisional
  status `TeamStrengthConfig`/`MinutesModelConfig`'s own defaults carry),
  but standardising features before the L2 penalty is applied is a
  natural Phase 7-calibration-pass improvement, not built here. **See §9
  for a second, separate, now-FIXED issue with this same `l2_penalty`**
  (an `n`-scaling defect, not a feature-scale one) — the two are
  independent and this feature-scale item remains open.
- **PL API possession%** (`team.match_stats@match`) has **zero rows** in
  this store (checked directly: `store.latest("pl_team_match_stats")` →
  `(0, 0)`) — `team_trailing_dc_mean_5` is a proxy built from this
  project's own DC counters, not an independently observed possession
  signal. Ingesting it is a provider-layer task, out of this task's OWNED
  PATHS.
- **No pre-2025-26 backfill** — Elite's recommendation, accepted (wiki
  §3.3): a multi-season backfill would buy rows measured under a
  different provider's definitions for a rule that pays on FPL's own
  counters, unmeasurable except in the season that doesn't need it.
- **Squad-level correlated Monte Carlo** across teammates in the same
  match — Phase 5, not this module (§4 above).

## 9. L2 scaling bug fixed — session `s005`, measured before touching a default

`_nb_neg_log_lik_and_grad` divided the log-likelihood by `n` (a per-row
average) but added the ridge penalty `l2 * sum(beta**2)` **unscaled** —
for `n` in the thousands, effectively `l2*n` against the averaged
likelihood. `fplai.models.saves`'s own duplicated copy of this exact
formula found and fixed this first (`docs/wiki/model-saves.md` — that
module's own real-data measurement: implied mean 1.531 against a true
2.965 at the shared `l2=1.0` default, before its fix). This module's §7
gate PASSED comfortably at the unscaled formula because it scores a
**binary threshold-crossing reduction** of the fitted count PMF, which
(per `saves`'s own escalated finding) is far less sensitive to the fitted
mean being wrong than a full-multiclass count gate would be. Measured
here, not assumed, whether that insensitivity actually held.

### 9.1 Walk-forward, real store, within 2025-26 (§7's exact setup, `min_train_rows=500`)

Arm A (current, unscaled, `l2=1.0`) reproduces §7's exact numbers:

| metric | DEF_CBIT: A (unscaled) | DEF_CBIT: B (l2/n, l2=1.0) | MID_FWD_CBIRT: A (unscaled) | MID_FWD_CBIRT: B (l2/n, l2=1.0) |
|---|---|---|---|---|
| log-loss | 0.4523 | 0.4272 (-5.5%) | 0.2395 | 0.2140 (-10.6%) |
| Brier | 0.1528 | 0.1433 (-6.2%) | 0.0730 | 0.0667 (-8.6%) |
| ECE (width) | 0.0355 | 0.0244 | 0.0218 | 0.0102 |
| ECE (quantile) | 0.0357 | 0.0253 | 0.0219 | 0.0120 |
| calibration slope (95% CI) | 0.677 [0.575, 0.779] | 1.262 [1.086, 1.439] | 0.909 [0.807, 1.010] | 1.189 [1.059, 1.318] |
| calibration intercept | -0.383 | 0.442 | -0.404 | 0.296 |
| mean(mu_model) vs mean(count_true) | 6.322 vs 6.188 (+0.134) | 5.980 vs 6.188 (-0.208) | 5.239 vs 5.119 (+0.120) | 5.021 vs 5.119 (-0.098) |
| beats both §7.1 baselines | True | True | True | True |

**Log-loss and Brier improve cleanly** for both groups at the unchanged
`l2=1.0` default — the gate's own primary metrics get materially better,
consistent with `saves`'s finding. `beats_both_baselines()` stays `True`
in every cell — **the §7.1 PASS verdict never changes.**

**But this is not a clean win on every axis, unlike `fplai.models.
minutes`.** The calibration slope and the implied-mean diagnostic both
**overshoot in the opposite direction**: `DEF_CBIT`'s slope moves from
0.677 (compressed below 1, CI excludes 1 below) to 1.262 (CI excludes 1
above) — worse by the "how far from 1" measure, not better, even though
the CI has tightened along with everything getting more confident.
`mean(mu_model)` similarly flips from overestimating the true count mean
(+0.134) to underestimating it by a LARGER margin (-0.208). Same
direction, smaller magnitude, for `MID_FWD_CBIRT` (+0.120 -> -0.098).

### 9.2 Swept `l2` under the corrected formula — a real tradeoff, not a flat curve

| l2 (corrected) | DEF_CBIT log-loss | DEF_CBIT slope (95% CI) | DEF_CBIT mean_mu diff | MID_FWD_CBIRT log-loss | MID_FWD_CBIRT slope (95% CI) | MID_FWD_CBIRT mean_mu diff |
|---|---|---|---|---|---|---|
| 0.01 | 0.4271 | 1.284 [1.104, 1.463] | -0.215 | 0.2136 | 1.201 [1.070, 1.331] | -0.103 |
| 0.1 | 0.4273 | 1.280 [1.101, 1.460] | -0.211 | 0.2138 | 1.198 [1.067, 1.328] | -0.102 |
| 1.0 | 0.4272 | 1.262 [1.086, 1.439] | -0.208 | 0.2140 | 1.189 [1.059, 1.318] | -0.098 |
| 10.0 | 0.4276 | 1.140 [0.982, 1.298] | -0.160 | 0.2154 | 1.129 [1.007, 1.251] | -0.050 |
| 100.0 | 0.4363 | 0.829 [0.711, 0.947] | -0.036 | 0.2213 | 0.969 [0.868, 1.070] | +0.028 |

Unlike minutes, this curve is **not flat**: log-loss/Brier are best in the
`0.01–1.0` range and degrade monotonically above `l2=10`, while the slope
CI and the mean-accuracy diagnostic move the OTHER way — `l2=10` is the
first point where `DEF_CBIT`'s slope CI includes 1.0 (`[0.982, 1.298]`),
and `l2=100` is the first for `MID_FWD_CBIRT` (`[0.868, 1.070]`), but both
cost real log-loss relative to the `0.01–1.0` region. No single `l2`
value in this grid is simultaneously best on scoring-rule grounds AND on
slope-near-1 grounds for both groups at once.

### 9.3 What changed, what did not, and what is deliberately left open

- **Fixed**: `_nb_neg_log_lik_and_grad`'s ridge term, in both loss and
  gradient, scaled by the same `1/n` the likelihood already carries (same
  form `saves.py:747` ships, and the same fix `fplai.models.minutes`
  received the same session — `docs/wiki/model-minutes.md` §13).
- **Unchanged**: `DCModelConfig.l2_penalty` default (`1.0`) — it clears
  the §7.1 gate with room at every point of the swept grid, and sits
  inside the region that is best on the gate's own primary metrics
  (log-loss/Brier). **Not** re-tuned toward `l2=10` or higher, even though
  that would pull the calibration slope closer to 1.0, because doing so
  on the strength of one model's slope alone — without also revisiting
  §8's already-open feature-standardisation item, which interacts with
  the same penalty — is a calibration-tuning decision, not a scaling-bug
  fix. Flagged for the Architect / a future Phase-7 calibration pass,
  explicitly not decided here.
- **Unchanged**: the §7.1 gate verdict (`PASS` for both groups) at every
  point of the swept grid — this is a real performance fix that leaves
  the existing gate intact, plus a newly-surfaced, NOT-yet-resolved
  calibration-slope question that the unscaled bug had been masking in
  the opposite direction (compressed, not inflated).
- **Verified against the real store**: `tests/test_defensive_
  contribution.py::test_walk_forward_validate_beats_both_baselines_on_
  the_real_store` passes against `data/store/` with the fixed formula in
  place.
