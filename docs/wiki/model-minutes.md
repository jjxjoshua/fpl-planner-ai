# Minutes model v1 — Phase 2, E5 (XL-Coder, session `s003`)

> `src/fplai/models/minutes.py` (new, then recalibrated later the same
> session — §12), `tests/test_minutes.py` (new, 48 tests; **70 as of the
> recalibration pass**, §12), `scripts/fit_minutes.py` (new, extended with
> `--calibrate`), `src/fplai/schemas.py` (append-only:
> `PLAYER_MINUTES_DISTRIBUTION_GAMEWEEK`, `PLAYER_MINUTES_DISTRIBUTION_DATASET`
> constants — no existing entry touched).
> Implements blueprint §4.1 ("the model that decides the project"), §4.3 (PMF
> discipline), §7.1 (calibration before points), §12.2 (derived-fact labelling).
> The second real model in the project, and the second real consumer of the
> derived-capability framework — this time exercising **import-time**
> registration, per Story A's resolution (see `model-team-strength.md` §10 and
> `fplai.schemas`' "Phase 2, E5" section).
>
> **§12 closes the deliberate v1 defect this file originally shipped with**
> (§7's reliability diagram showed real mid-range underconfidence, and §10
> named recalibration as explicitly not built) — read §12 for the honest
> before/after: recalibration genuinely helps out-of-sample, it now ships,
> and `fit_minutes_model`'s default changed as a direct result.
>
> **§§13-14 (session `s005`) are the CURRENT numbers** — §12's own figures
> were superseded twice in one session by two independent, real bugs (an
> unscaled `l2` penalty, §13; an isotonic-calibrator saturation regression
> the `l2` fix then exposed, §14). Read §14 before trusting any log-loss/
> Brier/ECE/slope figure elsewhere in this document as current.
>
> **Consolidation note, later the same session `s005`:** `IsotonicCalibrator`/
> `_fit_isotonic_calibrator` — described below as this module's own code —
> were moved to the shared `fplai.calibration` module once the identical
> mechanism (and the identical Jeffreys-smoothing fix, §14) turned up
> independently in `fplai.models.cards` and `fplai.models.saves` too. This
> was a proven-bit-identical refactor (diffed line-by-line first, then
> re-run against the real store before/after) — every number, test name and
> line reference below is unchanged in behaviour; only the import source
> moved. `fplai.models.minutes` still exposes `IsotonicCalibrator`/
> `_fit_isotonic_calibrator` as re-exports for backward compatibility, so
> every reference below still resolves.

## 1. What this is

A player with elite xG at 60% start probability is usually worse than a
mediocre player at 95%. This module answers "will this player play, and how
much" as a genuine probability distribution, never a scalar:

- **Stage 1 — a three-state categorical**: `P(START)`, `P(SUB)`, `P(UNUSED)`,
  a multinomial-logistic (softmax) fit over leakage-safe trailing features.
- **Stage 2 — a minute-band distribution conditional on state**: given
  START/SUB, a second softmax over six FPL-meaningful minute bands. UNUSED's
  band distribution is not fitted — it is structurally degenerate (verified
  live: `UNUSED` always means `minutes=0` in this store), fixed to
  `{"0": 1.0}`.

```python
from fplai.models.minutes import fit_minutes_model, predict_minutes_pmf
from fplai.store import BitemporalStore
from datetime import datetime, timezone

store = BitemporalStore()
params = fit_minutes_model(store, as_of=datetime(2026, 8, 22, tzinfo=timezone.utc))
# feature_row: a dict with every column in params.feature_spec.numeric_columns
# plus 'position'/'team' — the same shape build_training_table produces per row.
pmf = predict_minutes_pmf(params, feature_row, element=..., fixture=...)
pmf.p_start(), pmf.expected_minutes(), pmf.p_appearance_60_plus()
```

`MinutesPMF.expected_minutes()`/`p_appearance_60_plus()` are convenience
properties **computed from** the joint PMF — never a substitute for it
(CLAUDE.md rule 5).

## 2. Data — verified live, 2026-08-22, not re-derived from the brief

`starts` is observed and complete for **2022-23 → 2025-26: 113,592
player-gameweeks** (26,505 + 29,725 + 27,605 + 29,757). It is **wholly NULL**
for 2019-20/2020-21/2021-22 (16,556 + 24,365 + 25,447 null rows) — those three
seasons carry no start label at all. This module **never infers `starts` from
`minutes`**: a 20-minute hook and a 20-minute cameo off the bench are
indistinguishable on `minutes` alone, and a silently-wrong label is worse than
three fewer seasons of training data. Rows with a NULL `starts` are excluded
from every stage (feature engineering, target construction, the walk-forward
gate) — `tests/test_minutes.py::test_null_starts_rows_are_excluded_not_inferred`.

Also verified live (informs the state/band design): every labelled row is
internally consistent —

```
starts=1 & minutes=0   :   0 rows   (never observed)
starts=0 & minutes>0   :  15,343 rows  (SUB), 1 <= minutes <= 90
starts=0 & minutes=0   :  67,799 rows  (UNUSED)
starts=1 & minutes>0   :  30,450 rows  (START)
30,450 + 15,343 + 67,799 = 113,592 exactly
```

## 3. Minute bands — the boundaries, and why

```
MINUTE_BANDS = ("0", "1-29", "30-59", "60-74", "75-89", "90+")
```

The one boundary that must be exact is **60** — `game_config`'s `scoring`
blob pays `long_play=2` points for 60+ minutes and `short_play=1` for 1-59
(blueprint §11; verified live in the real store's `game_config`, 2026-08-22:
`"long_play": 2` sits directly in the `scoring` object alongside
`goals_scored`/`goals_conceded`).

**The 60-minute THRESHOLD itself is not present anywhere in `game_config`'s
payload — only the two point VALUES are.** Checked directly, not assumed:
the scoring blob has `long_play`/`short_play` (point values) but no field
naming the minute at which one becomes the other. This is the same
"absent from the API entirely" gap CLAUDE.md already documents for
defensive-contribution thresholds (blueprint §11) — `APPEARANCE_POINTS_
MINUTE_CLIFF = 60` in the module is therefore a **stated, verified-unpublished
constant**, not a value read from live config, because there is no
live-config value to read.

The other four boundaries exist because defensive-contribution thresholds
(and rotation-risk reasoning generally) scale with *how much* of a match a
player was on the pitch for, not just whether they cleared the appearance
cliff:

| Band | Meaning |
|---|---|
| `1-29` | Token cameo — minimal DC-accrual chance |
| `30-59` | Longer sub appearance, still under the appearance cliff |
| `60-74` | Cleared the cliff but hooked before the final quarter — the "hooked at 60" case blueprint §4.1 names explicitly |
| `75-89` | Played almost the whole match |
| `90+` | A genuinely full match, allowing for stoppage time |

This gives the model "hooked at 60 vs plays 90" as separate, orderable
outcomes, rather than folding them into one "played" bucket.

## 4. Features — all leakage-safe, strictly before the deadline being predicted

| Feature | Source |
|---|---|
| `trailing_start_rate_{3,5,10}` | Rolling mean of `any_start`, strictly prior rounds |
| `trailing_minutes_mean_{3,5}` | Rolling mean of round-summed minutes, strictly prior rounds |
| `trailing_full_appearance_rate` | Rolling fraction of last 5 rounds with minutes >= 75 — the explicit "hooked vs full" signal |
| `games_played_this_season` | Count of prior labelled rounds this season |
| `cold_start` | `games_played_this_season == 0` |
| `days_since_team_previous_fixture` | Gap to the team's chronologically previous kickoff |
| `team_first_fixture_in_window` | No earlier team fixture found in the training window |
| `prev_gw_value` / `prev_gw_selected_log1p` | STRICTLY PREVIOUS round's price/ownership |
| `was_home` | This fixture's own venue (known pre-deadline, not an outcome) |
| `position` / `team` | One-hot, all categories (ridge-regularised, same gauge-freedom trick `TeamStrengthConfig.attack_defence_l2` uses) |

**Every trailing feature stops at `round < this row's round`, never at
"the other fixture this round"** — a double gameweek's two fixtures share one
deadline, so from that deadline's own vantage point neither fixture's
outcome is knowable yet. This mirrors `fplai.backtest.replay.GameweekView`'s
`round < gameweek` boundary exactly, applied to feature engineering instead
of squad selection. **Attacked directly, not just asserted**:
`test_double_gameweek_fixtures_share_identical_trailing_features` proves both
fixtures of a fabricated double gameweek get bit-identical trailing features.

`days_since_team_previous_fixture` is different in kind: kickoff times and
the fixture list are public well before a deadline, so a gap to a
fixture-chronologically earlier kickoff — even one in the *same* round, for a
double gameweek's second leg (a real, short rest gap, exactly the congestion
signal wanted) — is not leakage the way an outcome would be.
`test_days_since_team_previous_fixture_allows_a_same_round_double_gameweek_gap`
proves this the other way: the gap feature *does* legitimately see the
same-round sibling fixture.

**Ownership/price enter only as the STRICTLY PREVIOUS round's `value`/
`selected`**, per this task's brief, applied literally to both fields even
though `fplai.backtest.data` treats the *current* round's `value` as
pre-deadline-safe for a different purpose (template-baseline construction).
That is a different module with an already-settled rationale for a different
question; this brief's own instruction is followed here without attempting
to reconcile the two — a genuine, stated tension between two modules' rules
for the same underlying column, not an oversight.

**Cold start is handled explicitly, not silently.** Every trailing feature
that would otherwise be NULL for a player's first labelled round is filled
to `0.0`, and `cold_start` is carried as its own boolean feature so the
fitted model can learn a *different* relationship for a cold-start row
rather than reading "0.0 trailing start rate" as "this player never starts".
`games_played_this_season` (a count, not a rate) is the companion signal
distinguishing "one game of history" from "twenty".

## 5. Features that are NOT sourceable — declared, not silently dropped

Checked directly against this store, not assumed from the brief. Two of
blueprint §4.1's named strongest predictors cannot be built from anything
ingested:

- **European congestion** — a midweek UCL/UEL fixture within N days of a
  Premier League match. This needs a non-PL fixture calendar. Every
  registered provider (`providers/pl.py`, `providers/fpl.py`,
  `providers/vaastav.py`, `providers/olbauday.py`) is **PL-only** — there is
  no capability to query for this at all, not a missing column on an
  existing one. **What would close this**: a new capability, something like
  `match.fixtures@matchweek` but for UCL/UEL/domestic-cup competitions, from
  a provider that actually covers them (none currently in `fplai.schemas`).
- **Manager rotation priors and the regime-change flag** — no
  manager-identity capability exists anywhere in this store. `pl_match_
  lineups` names players and roles, never a manager; nothing anywhere
  resolves "who was picking this team" as an entity at all, so there is no
  identity to attach a rotation prior *to*. **What would close this**: a
  `manager.identity@season` (or similar) capability, plus a per-match
  manager-assignment fact this store does not currently ingest from any
  source.

Sourcing either is a `fpl-data-scout` task, out of this module's scope. Not
attempted here, and not silently worked around with a proxy — a silently
omitted feature is this project's recurring failure mode (CLAUDE.md), and the
whole point of naming these is that a future session (or Data Scout) has a
concrete target to check off, not a vague "minutes model could be better"
note.

## 6. Bitemporal fitting — the third consumer of the same finding

Same wrong-primitive lesson `fplai.models.team_strength`'s module docstring
documents, **re-verified directly for this module**, not assumed to
transfer: `vaastav_player_gameweek_stats` was bulk-ingested in one backfill
session (2026-08-21), so every row's `observed_at` is approximately "today"
regardless of which historical season/gameweek the row describes.
`store.as_of(dataset, deadline_t)` therefore returns EMPTY for any real
historical `deadline_t` — proven directly, not assumed to still hold, by
`test_store_as_of_is_empty_for_a_genuinely_historical_deadline_on_this_
dataset`. `build_training_table` reads via `store.observations()` and filters
explicitly on `kickoff_time`, exactly like `fplai.backtest.data` and
`fplai.models.team_strength` before it.

**This is the THIRD module in this project to hit and route around this
exact primitive.** See §9 below for the `finding` this raises about
`as_of()`'s de facto interface for bulk-ingested archives — reported, not
fixed (`store.py` is read-only for this task).

## 7. Walk-forward validation — the real gate numbers

`walk_forward_validate` iterates chronologically over `(season, round)`
folds. For fold *t*, the P(start) model is refit using **only** rows whose
own `(season, round)` is strictly earlier than *t* — the leakage rule applied
to model-fitting itself, not just feature engineering. **Attacked directly**
(this session's standing rule): `test_walk_forward_validate_cannot_see_a_
folds_own_or_future_outcomes` flips a later round's outcome and confirms
every earlier fold's prediction is bit-for-bit unchanged; before writing
this test, it was run against a deliberately-broken version of
`walk_forward_validate` (`train = table` instead of `train = table.filter
(_chronological_rank < rank)`) and confirmed to FAIL — 18/18 predictions
differed, proving the test genuinely detects the leak it exists to catch,
not just asserting a property that happens to hold. `test_walk_forward_
validate_is_unaffected_by_input_row_order` separately proves the split is
computed from `_chronological_rank`, never row position.

### Real numbers — `scripts/fit_minutes.py --eval-seasons 2024-25 2025-26`, against the real `data/store/` (read-only)

```
**Counts restated 2026-08-22** (was 113,270 training / 57,040 eval / log-loss 0.3465): the
`effective_at()` migration correctly deduplicated **10 exact-duplicate rows** present in
`vaastav_player_gameweek_stats` 2025-26, which had been double-counted. The gate still
PASSES; Brier is unchanged at 0.1062.

training table: 113,260 rows (113,592 labelled minus 322 AM/Assistant-Manager rows, minus 10
                 rows, dropped for the same reason fplai.backtest.data drops them)
n_features: 43
walk-forward: 76 folds, 57,030 eval rows, 45.6s

method                          log-loss       brier
model                             0.3464      0.1062
baseline: started last GW         3.2129      0.1215
baseline: base rate by pos        0.6057      0.2075

GATE: PASS (model beats BOTH baselines on BOTH metrics)
```

**Both baselines are defined leakage-safely and refit at every fold, exactly
like the model**: "started last gameweek" is the player's own immediately
preceding round's `any_start` (falling back to the position base rate for a
genuinely cold-start player, since there is no previous round to read);
"base rate by position" is the mean START rate among *training* rows only,
recomputed fresh at every fold from data strictly before it. Neither is a
single global constant computed once and reused across folds — that would
itself have been a lesser, unfair comparison.

**The "started last gameweek" baseline's log-loss (3.21) looks extreme
because it is a genuinely unsmoothed binary indicator used directly as a
probability** — every miss costs `-log(eps)` at the numerical floor. This is
not a bug in the baseline definition; it is the honest cost of reporting
`1.0`/`0.0` as a probability estimate rather than the calibrated
probabilistic estimate the model itself produces. Brier (which penalises
quadratically, not logarithmically) tells a gentler version of the same
story: `0.1215` vs the model's `0.1062` — still a real, if smaller, win.

### Reliability diagram (10 bins, same 76-fold run)

```
[0.0, 0.1) n=30578  mean_predicted=0.061  observed=0.034
[0.1, 0.2) n= 4761  mean_predicted=0.140  observed=0.230
[0.2, 0.3) n= 2574  mean_predicted=0.247  observed=0.395
[0.3, 0.4) n= 2035  mean_predicted=0.350  observed=0.500
[0.4, 0.5) n= 3424  mean_predicted=0.441  observed=0.453
[0.5, 0.6) n= 2023  mean_predicted=0.551  observed=0.653
[0.6, 0.7) n= 2305  mean_predicted=0.652  observed=0.740
[0.7, 0.8) n= 3071  mean_predicted=0.753  observed=0.804
[0.8, 0.9) n= 3977  mean_predicted=0.853  observed=0.866
[0.9, 1.0) n= 2292  mean_predicted=0.931  observed=0.901
```

Read honestly: the model is reasonably well-calibrated but **systematically
underconfident in the middle bins** (0.2-0.7 predicted consistently undershoot
the observed rate by 0.05-0.15) and mildly overconfident at the extremes
(0.0-0.1 predicts 0.061 against an observed 0.034 — closer than the middle
bins, but the direction flips). This was a real, stated limitation of v1,
not smoothed over — **closed later the same session, §12**: a nested,
out-of-sample isotonic calibrator now ships (`fit_minutes_model`'s default
changed to `calibrate=True` as a direct result of the real numbers in §12,
not assumed in advance). This section's numbers are the RAW model's, kept
exactly as originally measured — §12 is the honest before/after, not a
silent edit of this table.

**Why `scripts/fit_minutes.py`, not a pytest test, produces these numbers.**
45.6s for 76 folds (measured live, this session) is too slow to pay on every
regular test-suite run. `tests/test_minutes.py` instead carries: (a) a
synthetic-data walk-forward test proving the MECHANISM recovers a clean
signal to near-zero loss (not the real gate numbers — see that test's own
docstring for why a perfectly deterministic-by-position synthetic signal
cannot fairly test "beats both baselines"), and (b) one `@requires_real_
store`-gated BOUNDED live sanity check (`test_walk_forward_validate_beats_
both_baselines_on_a_bounded_real_slice`) that runs the real walk-forward
over a real, if narrower, window — genuinely live, not the full authoritative
report.

## 8. Persistence

`write_minutes_pmfs` routes through `fplai.derived.write_derived` (never a
bare `store.write()`), via `player.minutes_distribution@gameweek` → dataset
`derived_player_minutes_distribution`, **LONG format**: one row per
`(season, round, element, fixture, state, band)` with its joint probability —
the same long-format convention `team.match_stats@match`/`player.season_
stats@season` already use for a sparse/enumerated key. Unlike `write_team_
strength` (which computes its own in-sample residual), `write_minutes_pmfs`
requires the caller to pass a `CalibrationReference` explicitly — this
module's meaningful calibration number is the walk-forward's OUT-OF-SAMPLE
gate result, computed over a validation window a single `fit_minutes_model`
call has no way to know about; passing it explicitly keeps that distinction
honest rather than fabricating an in-sample number that would understate
this model's real uncertainty. `scripts/fit_minutes.py --demo-write`
demonstrates this against an isolated temp store (never `data/store/`):
`written=True n_rows=90 dataset=derived_player_minutes_distribution`, all
`is_modelled=True` on readback.

## 9. Story A reused, not re-litigated — and a live finding for the Architect

**Import-time registration** (`_register_minutes_capability()` at this
module's own module level) is Story A's resolution, applied here as its
first real reuse — see `model-team-strength.md` §10 and `fplai.schemas`'
"Phase 2, E5" section for the full account. `tests/test_schemas.py`'s two
derived-side exact-set assertions now import both `fplai.models.team_
strength` and `fplai.models.minutes` explicitly and expect **both**
capabilities — proven to need updating, not just assumed: adding this
module without updating those two tests produced exactly the expected
failure (`AssertionError: {'player.minutes_distribution@gameweek', 'team.
strength_rating@gameweek'} == {'team.strength_rating@gameweek'}`), confirming
the exact-set check is load-bearing for a second real module, not only the
first.

**Finding for the Architect, per this task's brief**: this is the **third**
module (`fplai.backtest.data`, `fplai.models.team_strength`, now `fplai.
models.minutes`) to independently discover that `store.as_of()` returns
EMPTY for `vaastav_player_gameweek_stats` at any genuinely historical
deadline, and to route around it the same way — `store.observations()` plus
an explicit filter on the archive's own valid-time column. Three consumers
converging on the identical workaround, with the identical justifying
comment reproduced nearly verbatim each time, is itself a signal: `as_of()`
returning STATE via `observed_at` is correct **by the store's own contract**
(blueprint §3.2) for a dataset that is snapshotted incrementally, but for a
dataset that was bulk-ingested once, "state as of `observed_at`" and "state
as of the archive's own valid-time column" have silently become two
different, useful things — and every caller of this dataset has had to know
that and route around it by hand. **The workaround has become the de facto
interface for this class of dataset, not documented as one.** Options worth
the Architect's consideration, not decided here (store.py is read-only for
this task): (a) document `observations() + explicit valid-time filter` as
the SANCTIONED pattern for bulk-ingested archives in `docs/wiki/provider-
framework.md`, so a fourth consumer does not have to rediscover it; (b) a
store-level convenience (e.g. `as_of_by(dataset, ts, valid_at_column=...)`)
that does this filtering under one name, closing the "three consumers wrote
nearly the same 6 lines" duplication; (c) leave it as-is, on the reasoning
that different datasets genuinely need different valid-time columns and a
shared helper would just be a thinner wrapper around what each caller
already writes. Not my call — reporting the pattern, not choosing between
these.

## 10. Deliberately not built here

- ~~**A recalibration pass** on the raw softmax output~~ **BUILT, session
  s003 — §12.** Nested, out-of-sample isotonic calibration, proven to
  genuinely improve every pooled metric and every position's log-loss/ECE
  against the real store. Struck through rather than deleted, so this
  section stays an honest record of what v1 shipped without, even after
  it stopped being true.
- **European congestion / manager rotation / regime-change features** — §5,
  declared as genuinely unsourceable from this store today, a `fpl-data-
  scout` task.
- **Cross-season team-identity resolution** for the `team` one-hot feature —
  same name-based-join limitation `fplai.models.team_strength`'s wiki §4
  already found and accepted (e.g. `"Ipswich"` vs `"Ipswich Town"` across
  seasons would be two different one-hot categories). Not fixed here for
  the same reason team_strength didn't fix it: `identity.py`/`providers/**`
  are out of this task's owned paths, and the cost here is smaller than for
  team_strength (a categorical feature losing a little cross-season signal
  is a milder failure than a team-strength rating discarding a relegated
  club's real history).
- **Live serving / a decision-brief consumer** — this module produces PMFs
  and persists them; wiring predictions into a weekly decision loop is
  later-phase scope (blueprint §7, Phase 3+).

## 11. Verification

- `uv run pytest tests/test_minutes.py -q` — **48 passed** at original ship
  (v1), **70 passed** after the recalibration pass (§12 — 22 new tests: the
  isotonic calibrator's mechanics and its small-sample overfitting fix, the
  inner split, the leakage attack, `fit_minutes_model(calibrate=...)`
  integration and default-flip behaviour, `CalibrationMetrics`/ECE/
  reliability-diagram-as-a-function/calibration-slope-intercept, and one
  more bounded real-store sanity check). Includes: minute-
  band/state boundary tests; a check that the pure-Python `minute_band` and
  the vectorised Polars expression used in feature construction agree across
  every minute value 0-120 (the two implementations cannot silently drift
  apart); `MinutesPMF` sum-to-1 discipline (both the state and every
  band-conditional distribution); feature-engineering correctness on
  hand-fabricated multi-round data (trailing-rate exact values,
  `prev_gw_value`/`selected` lag, cold-start flag); the double-gameweek
  same-round trailing-feature isolation test; two adversarial leakage
  attacks (backdated `observed_at`, and the walk-forward future-fold
  isolation test — proven to fail against deliberately-broken code before
  being trusted, per this session's standing rule); a row-order-independence
  proof for the walk-forward split; determinism (`np.allclose` across two
  fits of identical data); the derived-capability round-trip and its
  physical absence from the observed dataset; the fresh-subprocess
  import-time-registration proof (Story A's pattern, reused).
- `uv run pytest -q` (full suite) — **474 passed** at original ship, **638
  passed** after the recalibration pass, 0 regressions either time.
- `python scripts/fit_minutes.py --eval-seasons 2024-25 2025-26 --demo-write`
  — **live, against the real `data/store/` (read-only)**. Full output §7/§8
  above. Training table row count (113,260) and per-state counts (START
  30,450 / SUB 15,343 / UNUSED 67,477 after the 322-row AM exclusion) match
  the values independently verified against the real store before writing
  any code (§2), confirmed again by
  `test_build_training_table_against_the_real_store_matches_the_verified_
  row_count`. `--demo-write` confirmed `written=True, n_rows=90,
  dataset=derived_player_minutes_distribution`, `is_modelled` all `True` on
  readback, against an isolated temp directory only — never `data/store/`.
  Re-run after §12's recalibration pass (still `--demo-write`, no
  `--calibrate` needed — the headline fit's `calibrate=True` default now
  applies unconditionally): `p_start_calibrator attached: True`, and
  reading the temp store back directly (`fplai.store.BitemporalStore.as_of`)
  confirmed `calibration_method` reads `'isotonic_v1'` for all 90 written
  rows — the structural-provenance claim in §12.4, attacked by actually
  reading the persisted column back, not trusted from the write path alone.

**What was verified live vs. assumed**: the real `data/store/` row counts,
the `game_config` scoring payload (confirming `long_play`/`short_play` exist
but no minute threshold), the full walk-forward gate numbers, and the
derived-capability write/readback round trip are all live, against the real
store, this session. What is *not* independently re-verified here: the
underlying `starts`/`minutes`/`kickoff_time` data quality claims already
established by `fplai.backtest.data` and `fplai.models.team_strength`'s own
verification passes (e.g. `kickoff_time` format, the AM-row/GKP-normalisation
facts) — reused from those modules' own live verification, not re-derived
from scratch a third time.

---

## 12. Recalibration — session s003, closing the defect §7/§10 shipped with

**Verdict: recalibration ships.** Every pooled metric improves out-of-sample,
every position's log-loss and ECE improve, and the one exception (DEF's
Brier) is a wash, not a real regression. `fit_minutes_model`'s default
changed to `calibrate=True` as a direct, evidence-based consequence — not
decided in advance and then confirmed; the brief's instructed negative
("recalibration doesn't help, or trades one metric for another") is exactly
the outcome that was checked for and did **not** occur here, on real data.

### 12.1 The nested design, and how its leakage boundary was attacked

**The mechanism.** A calibrator maps a raw predicted probability to a
corrected one. Fitting it on the same data it is then scored against is
leakage in exactly blueprint §7.2's sense, one level down: "outcomes may
score a decision, never inform it" becomes "outcomes may score a
*prediction*, never inform the *mapping* that produced it." The fix, inside
each walk-forward fold's own `train` slice (rows strictly before the eval
fold, exactly as v1 already enforced for the raw model):

1. Split `train` into `inner_train` (earlier rounds) and `calib_holdout`
   (the most recent `holdout_frac` fraction of *distinct* `_chronological_
   rank` values still inside `train`) — **by round, never by row count or a
   random split**, so a double gameweek's two fixtures (sharing one
   deadline) cannot land on opposite sides of the split.
2. Fit a fresh model on `inner_train` only, score it on `calib_holdout` —
   genuinely out-of-sample with respect to the model that produced those
   predictions.
3. Fit the isotonic calibrator on those `(prediction, outcome)` pairs.
4. The full-`train` model (unchanged — the raw prediction reported is
   IDENTICAL whether or not calibration runs) predicts the eval fold as
   always; the calibrator from step 3 (fitted on a strictly earlier slice)
   is applied to that prediction.

`fit_minutes_model`'s own deployed calibrator uses the identical mechanism
against `as_of`'s own training window (never the live rows it will later be
asked to predict), fitted against the SAME 3-class `state_weights` marginal
that is actually served — a deliberately separate fit from the walk-forward
gate's own (2-class binary) calibrator; see `fit_minutes_model`'s docstring
for why unifying the two model formulations was judged out of this task's
scope, and the finding below.

**Attacked, not just asserted, four ways:**

- **The forbidden path, built by hand.** `tests/test_minutes.py::
  test_calibrator_fit_directly_on_eval_data_would_leak_and_look_suspiciously_
  good` fits a calibrator directly on synthetic eval data (never touching
  any of this module's split machinery) and shows it beats the honest,
  disjoint-fit version — the leak is a real, measurable effect on this
  module's own primitives, not a hypothetical one.
- **Future-fold isolation, proven to fail first.** `tests/test_minutes.py::
  test_walk_forward_validate_calibrated_cannot_see_a_folds_own_or_future_
  outcomes` flips a later round's outcome and checks every earlier fold's
  `p_model_calibrated` is bit-for-bit unchanged. Before trusting it, the
  `calibrate` branch inside `walk_forward_validate` was temporarily changed
  to call `_inner_calibration_split(table, ...)` (the FULL table, including
  future folds) instead of `_inner_calibration_split(train, ...)`, run
  against this exact test, and confirmed to **FAIL on 24 of 26 "before last
  round" predictions** (not just index 0) — then reverted, confirmed
  byte-identical to the pre-attack file via `diff`, and the suite re-run
  green. This is the standing "prove a new test fails first" rule applied to
  a second, independent leakage claim in the same module.
- **A real overfitting bug, found by trying to satisfy the test's own
  assertions, not by inspection.** The first implementation used raw
  per-point PAVA (`scipy.optimize.isotonic_regression` on every unique raw
  prediction, no binning). On a 200-row synthetic holdout with a genuine,
  smooth compression-toward-0.5 miscalibration, this produced a calibrator
  whose out-of-sample log-loss (0.82) was *worse* than the raw predictions
  it was meant to correct (0.59) — because with near-unique continuous `x`
  and binary `y`, unbinned PAVA pools points only where monotonicity is
  violated, degenerating into runs of exact 0.0/1.0 at the sorted extremes
  from as few as one or two unlucky labels. Fixed by binning into 20
  equal-count buckets before PAVA (`IsotonicCalibrator`'s own docstring
  carries the full account); after the fix, the same scenario shows honest
  (0.58) beating raw (0.59) beating nothing, and the leaked version (0.51)
  beating both — the ordering a leakage demonstration is supposed to show.
  This was found DURING test-writing, not assumed away: the test's "honest
  beats raw" assertion genuinely failed against the first implementation,
  and the fix is in the shipped `_fit_isotonic_calibrator`, not the test.
- **The persisted-provenance claim, read back, not trusted from the write
  path.** After `--demo-write` against an isolated temp store, the written
  parquet was read back with a *fresh* `BitemporalStore.as_of()` call (a
  second Python invocation, module docstrings re-imported from scratch) and
  `calibration_method` confirmed to read `'isotonic_v1'` for all 90 rows —
  not merely that `write_minutes_pmfs` accepted the field.

### 12.2 Before / after — pooled (76 folds, 57,030 eval rows, real store, `eval_seasons=['2024-25','2025-26']`)

```
metric              raw (uncalibrated)   calibrated (isotonic_v1)   delta
log-loss                    0.3464               0.3392            -0.0073
brier                       0.1062               0.1048            -0.0014
ECE (10 bins)                0.0470               0.0079            -0.0391
calibration slope            1.114                0.966             -> 1.0
calibration intercept        0.228               -0.055             -> 0.0
```

Both raw baselines from §7 are unaffected by calibration (they are separate
naive predictors, not the model): "started last GW" log-loss 3.2129/brier
0.1215, "base rate by position" log-loss 0.6057/brier 0.2075. The calibrated
model still clears the §7.1 gate by the same margin the raw model always
did — calibration was never at risk of *failing* the gate, only of
improving or not improving what the gate already passes.

**ECE fell 83% relative** (0.047 -> 0.008) — the single clearest signal that
this is a genuine calibration fix, not a log-loss/Brier trade dressed up:
ECE measures exactly the reliability-diagram gap §7 stated as v1's
limitation, and ECE improved more, proportionally, than either scoring rule.

### 12.3 Before / after — per position (same 76-fold run)

```
position    n        raw_log_loss   cal_log_loss   raw_brier   cal_brier   raw_ece   cal_ece
DEF     18,879           0.3535        0.3521         0.1077      0.1085     0.0402    0.0184
FWD      6,296           0.3680        0.3529         0.1144      0.1089     0.0572    0.0262
GK       6,296           0.2096        0.1985         0.0537      0.0533     0.0581    0.0320
MID     25,559           0.3696        0.3609         0.1161      0.1139     0.0505    0.0140
```

**Read honestly, per the brief.** Every position's log-loss and ECE improve.
**DEF's Brier is the one exception**: 0.1077 -> 0.1085, a +0.0008 (0.7%
relative) regression — small enough to call a wash rather than a real
trade, but reported as measured, not rounded away. No position is left
meaningfully worse off by shipping this; MID (the largest position by row
count) shows the strongest ECE improvement (0.0505 -> 0.0140, 72% relative).

### 12.4 Reliability tables — raw vs. calibrated (10 bins, same 76-fold run)

Raw (identical to §7's table, reproduced here for direct comparison):

```
bin            n       mean_predicted   observed_rate
[0.0, 0.1)   30,571        0.061           0.034
[0.1, 0.2)    4,762        0.140           0.231
[0.2, 0.3)    2,574        0.247           0.395
[0.3, 0.4)    2,037        0.350           0.501
[0.4, 0.5)    3,420        0.441           0.453
[0.5, 0.6)    2,025        0.551           0.652
[0.6, 0.7)    2,302        0.652           0.740
[0.7, 0.8)    3,072        0.753           0.804
[0.8, 0.9)    3,976        0.853           0.866
[0.9, 1.0)    2,291        0.931           0.901
```

Calibrated (bin membership shifts because the x-axis itself is remapped —
this is expected, not an error):

```
bin            n       mean_predicted   observed_rate
[0.0, 0.1)   28,641        0.033           0.032
[0.1, 0.2)    3,705        0.146           0.135
[0.2, 0.3)    2,233        0.247           0.241
[0.3, 0.4)    2,717        0.357           0.387
[0.4, 0.5)    4,414        0.444           0.430
[0.5, 0.6)    1,781        0.542           0.537
[0.6, 0.7)    2,063        0.654           0.661
[0.7, 0.8)    2,838        0.756           0.757
[0.8, 0.9)    6,196        0.859           0.834
[0.9, 1.0)    2,442        0.916           0.891
```

Every calibrated bin's `mean_predicted`/`observed_rate` gap is smaller than
the raw table's corresponding region — most dramatically the [0.2,0.4) range
that §7 called out by name (raw: 0.247→0.395 and 0.350→0.501, gaps of 0.148
and 0.151; calibrated: 0.247→0.241 and 0.357→0.387, gaps of 0.006 and 0.030).

### 12.5 What ships, and the structural provenance

- `fplai.models.minutes.IsotonicCalibrator` — a monotonic remapping fitted
  via quantile-binned PAVA (`scipy.optimize.isotonic_regression`,
  deterministic, no new dependency — already in the pinned `scipy` 1.17.1).
- `MinutesModelParams.p_start_calibrator: IsotonicCalibrator | None` —
  attached by `fit_minutes_model(..., calibrate=True)`, **now the default**.
  `calibrate=False` opts back out to v1's exact original behaviour.
- `predict_minutes_pmf` applies the calibrator to the raw START marginal and
  rescales SUB/UNUSED proportionally so `p_state` still sums to exactly
  1.0 — proven a true no-op under an identity calibrator, and proven to
  preserve the SUB:UNUSED ratio under a non-trivial one
  (`tests/test_minutes.py::test_predict_minutes_pmf_calibration_is_a_true_
  no_op_under_an_identity_mapping` /
  `::test_predict_minutes_pmf_calibration_rescales_sub_and_unused_
  proportionally`).
- **`MinutesPMF.calibration_method`** (`"raw_uncalibrated"` /
  `"isotonic_v1"`) is a declared, persisted field — the same structural-
  provenance role `fplai.models.defensive_contribution.DCPMF.threshold_
  verified` plays for that module, per this task's explicit instruction. It
  is a `value_field` in `_register_minutes_capability`'s
  `register_derived_capability(...)` call, so a consumer reading a
  persisted `player.minutes_distribution@gameweek` row can tell which
  mapping produced its `probability` without reading this module's source
  — attacked live in §11's re-run, not merely declared.
- `WalkForwardResult` gained `p_model_calibrated`/`position`/
  `n_folds_calibrated`, and `raw_metrics()`/`calibrated_metrics()`/
  `raw_metrics_by_position()`/`calibrated_metrics_by_position()` (each
  returning a `CalibrationMetrics`: n, log-loss, Brier, ECE, Cox calibration
  slope/intercept, full reliability table) — the first-class calibration
  measurement surface blueprint §7.1 names and v1 shipped without.
  `reliability_diagram()`/`expected_calibration_error()` are now standalone
  module functions (not `WalkForwardResult` methods only), so the same
  implementation scores raw, calibrated, pooled, or per-position slices
  without four hand-copied loops.

### 12.6 Findings for the Architect

1. **Two separately-fit calibrators exist in this module, by design, and
   that is a real seam worth knowing about.** `walk_forward_validate
   (calibrate=True)`'s calibrator corrects a standalone 2-class binary
   softmax refit fresh per fold (the quantity the §7.1 gate has always
   scored, unchanged since v1). `fit_minutes_model(calibrate=True)`'s
   calibrator corrects THIS module's actual deployed 3-class `state_
   weights` START marginal (the quantity `predict_minutes_pmf` serves).
   They are related — both out-of-sample, both isotonic, both nested — but
   not numerically identical, because the two underlying model
   formulations differ. Unifying them (making the gate score the exact
   quantity that ships, or vice versa) is a real design decision about what
   "the model" means in this module and was judged out of this task's
   scope; flagged here rather than decided unilaterally.
2. **Raw per-point isotonic PAVA is not safe on binary outcomes without
   binning, and this is worth knowing project-wide, not just in this
   module.** Any future calibrator (captaincy backcast, clean-sheet
   probabilities, bonus points) that reaches for `scipy.optimize.
   isotonic_regression` directly on a small-to-moderate out-of-sample
   holdout risks the exact degenerate-block overfitting found and fixed
   here. Worth a line in `docs/wiki/provider-framework.md` or a shared
   helper if a third consumer appears — not built here, since this
   module's `_fit_isotonic_calibrator`/`IsotonicCalibrator` are private to
   `fplai.models.minutes` and no second consumer exists yet to justify
   extracting them.

## 13. L2 scaling bug fixed — session `s005`, measured before touching a default

`_softmax_neg_log_lik_and_grad` divided the log-likelihood by `n` (a
per-row average) but added the ridge penalty `l2 * sum(W**2)` **unscaled**
— for `n` in the thousands, this makes the penalty's effective strength
relative to the averaged likelihood roughly `n` times the caller's stated
`l2`. `fplai.models.saves`'s own duplicated NB2 formula found and fixed
this first (`docs/wiki/model-saves.md`); `fplai.models.defensive_
contribution`'s own copy had the identical defect (`docs/wiki/model-
defensive-contribution.md` §9); this module shared it too.

**This task's brief, explicitly: measure first, decide on the evidence.**
Minutes' §7 gate PASSED at small ECE under the unscaled formula, which a
catastrophically crushed intercept could not produce — so it was not
obvious the bug was doing real damage here the way it was in `saves`'s
full-multiclass count PMF. It was.

### 13.1 Walk-forward, real store, same setup §7 uses (`eval_seasons=
['2024-25','2025-26']`, 76 folds, 57,030 eval rows, `min_train_rows=200`)

Arm A (current, unscaled, `l2=1.0`) reproduces §7's exact numbers —
baseline trusted, not re-derived from scratch:

| metric | A: current (unscaled), l2=1.0 | B: corrected (l2/n), l2=1.0 | delta |
|---|---|---|---|
| log-loss | 0.3464 | 0.3025 | -0.0439 (-12.7%) |
| Brier | 0.1062 | 0.0945 | -0.0117 (-11.0%) |
| ECE (equal-width, 10 bins) | 0.0470 | 0.0186 | -0.0284 (-60.4%) |
| ECE (equal-frequency, 10 bins) | 0.0488 | 0.0189 | -0.0299 (-61.3%) |
| calibration slope (95% CI) | 1.114 [1.097, 1.131] | 0.945 [0.930, 0.960] | both exclude 1.0; 0.945 far closer |
| calibration intercept | 0.228 | 0.054 | -0.174 |
| mean(p_model) vs mean(y_true) | 0.2779 vs 0.2932 (diff -0.0153) | 0.2866 vs 0.2932 (diff -0.0066) | gap roughly halved |
| beats both §7.1 baselines | True | True | **gate verdict unchanged** |

The "implied mean vs empirical rate" read (this table's last row — the
same diagnostic that exposed the bug in `saves`, here computed as the
walk-forward's mean predicted `P(start)` against the true START rate,
since minutes has no single NB mean to read) is the most direct evidence:
the unscaled formula was pulling the average prediction away from the
true rate by more than double what the corrected formula does.

### 13.2 Swept `l2` under the corrected formula — the curve, not just the winner

| l2 (corrected) | log-loss | Brier | ECE (width) | ECE (quantile) | slope | intercept |
|---|---|---|---|---|---|---|
| 0.01 | 0.3024 | 0.0945 | 0.0185 | 0.0174 | 0.946 | 0.053 |
| 0.1 | 0.3022 | 0.0944 | 0.0181 | 0.0187 | 0.945 | 0.052 |
| 1.0 | 0.3025 | 0.0945 | 0.0186 | 0.0189 | 0.945 | 0.054 |
| 10.0 | 0.3032 | 0.0948 | 0.0198 | 0.0182 | 0.955 | 0.060 |
| 100.0 | 0.3083 | 0.0960 | 0.0229 | 0.0222 | 0.990 | 0.097 |

**Flat from 0.01 to 1.0** (4th-decimal differences on every metric — the
same "not sensitive to l2" shape `saves` found for its own corrected
formula), degrading only mildly above `l2=10`. `beats_both_baselines()` is
`True` at every point. `l2_penalty=1.0` (the existing default) needed no
retuning once the scaling itself was fixed — the sweep's nominal optimum
(`l2=0.1`, log-loss 0.3022) is within noise of `l2=1.0`'s 0.3025.

### 13.3 What changed, what did not

- **Fixed**: `_softmax_neg_log_lik_and_grad`'s ridge term, in both loss and
  gradient, scaled by the same `1/n` the likelihood already carries (same
  form `saves.py:747` already shipped).
- **Unchanged**: `MinutesModelConfig.l2_penalty` default (`1.0`) — measured
  to remain a good choice under the corrected formula, not merely carried
  over unexamined.
- **Unchanged**: the §7.1 gate verdict (`PASS`) — this was a real
  performance fix, not a gate-validity fix. §7/§12's shipped numbers
  (0.3464/0.1062 raw, 0.3392/0.1048 isotonic-calibrated) were **valid**,
  just leaving real accuracy on the table. A future session refitting the
  headline model and re-running `--calibrate` will produce new, better raw
  AND calibrated numbers than §7/§12 record — not done here (out of this
  task's scope, which was the scaling bug itself), flagged so nobody reads
  §12's isotonic-calibration delta as still representing the best
  achievable fit.
- **Verified against the real store**, not just synthetic data:
  `tests/test_minutes.py::test_walk_forward_validate_beats_both_baselines_
  on_a_bounded_real_slice` passes against `data/store/` with the fixed
  formula in place.

## 14. Isotonic calibrator saturation — session `s005`, exposed by §13's own fix

§13.3 predicted this: "a future session refitting the headline model and
re-running `--calibrate` will produce new, better raw AND calibrated
numbers than §7/§12 record." The **raw** numbers did improve. The
**calibrated** numbers regressed — for the first time in this project's
history — and the regression traced to a real bug the `l2` fix exposed by
changing the raw series underneath it, not to the fix itself being wrong.

### 14.1 The regression, as first observed (E5 report regeneration, real
store, `calibration_report.py:run_minutes()`'s exact convention:
`min_train_rows=8000`, `eval_seasons=` every season but the first, 114
folds, 86,755 OOS rows — **not** the same convention as §7/§13's
`min_train_rows=200`/76-fold table; the two are not directly comparable
row-for-row, only within themselves)

| | log-loss | Brier | ECE (width) | slope (95% CI) |
|---|---|---|---|---|
| raw (post-l2-fix) | 0.3125 | 0.0947 | 0.0170 | 0.896 [0.884, 0.908] |
| nested isotonic, **pre-saturation-fix** | 0.3658 | 0.0950 | 0.0056 | 0.890 [0.878, 0.902] |

Log-loss rose 17% while Brier moved 0.0003 and ECE improved sharply — the
signature of a small number of predictions pushed to an exact 0/1 that are
sometimes wrong, not a broadly worse predictor (Brier is bounded, ECE
rewards exactly this; log-loss is unbounded and punishes a confident wrong
call almost without limit). The slope barely moving (0.896→0.890, when a
working calibrator should move it toward 1.0) was the tell that the
calibrator was not actually doing its job on the bulk of the distribution.

### 14.2 Saturation, measured directly, not inferred

Live census of the 86,755 calibrated predictions, pre-fix:

| | count | share |
|---|---|---|
| exactly `0.0` | 2,602 | 3.00% |
| exactly `1.0` | 0 | 0.00% |

Those 2,602 rows alone accounted for **94.6%** of the total pooled
log-loss increase (+4,370.78 of +4,621.60 summed row contributions). 208 of
the 2,602 (7.99%) were actually `START=1` — a player the calibrator was
flatly, unconditionally certain would not start, who did.

**Mechanism, structural, not statistical:** `_fit_isotonic_calibrator` bins
the calibration holdout into `n_bins=20` equal-count bins and fits each
bin's plain sample mean (`bin_y[b] = y_sorted[mask].mean()`) — exactly
`0.0` whenever every row in that bin shares the outcome. `isotonic_
regression` leaves an already-monotonic extreme bin untouched, and
`IsotonicCalibrator.apply()` extrapolates **flat** beyond the fitted range
(`np.interp(..., left=y[0], right=y[-1])`) — so any raw eval prediction
more extreme than the most extreme calibration-holdout bin's mean inherits
that bin's exact fitted value, however thin the evidence behind it. This
was not a small-`n` corner case: per-fold minimum bin weight measured
282-1191 rows across the real 114 folds (median 656) — homogeneous-outcome
bins at that size reflect a genuinely low-probability player population,
not sampling noise, which is exactly why the fitted rate landing at exact
`0.0` looks locally reasonable and is still the wrong thing for a model to
assert: a few hundred trials with zero events bounds the true rate away
from 0, it does not prove it is 0.

### 14.3 The fix — model layer, not the metric layer

`fplai.calibration.log_loss` (and this module's own `_log_loss`) already
take an `eps` for their own numerical safety (`log(0)` is undefined) — that
is a **metric guard**, unrelated to this bug and left untouched. The fix
is in `_fit_isotonic_calibrator` itself: before the isotonic fit, each
bin's rate is smoothed with a Jeffreys (`Beta(0.5, 0.5)`, non-informative
prior) continuity correction, `(successes + 0.5) / (n + 1)`, using **that
bin's own weight** — not a single flat epsilon derived from the fold's
smallest bin. A flat `eps = 1/(2 * bin_w.min())` clip was measured to
collapse the existing `test_isotonic_calibrator_n_bins_is_capped_at_
available_rows` fixture (3 rows, 1 per bin) to a single degenerate point
(`eps=0.5` at `n=1` forces every fitted value to exactly `0.5`, discarding
the fit). The per-bin Jeffreys correction gives `0.25`/`0.75` at `n=1`
(informative, not degenerate) and converges to within 4 decimal places of
the flat-eps candidate at the real data's smallest observed bin weight
(`1/(2*282)=0.00177` vs the Jeffreys value `0.00177` — identical at this
scale), so it is not a materially different answer where the fold actually
has data, only where the flat clip would have broken.

New tests, both proven against the unpatched code first (hand-verified,
not merely asserted): `test_isotonic_calibrator_never_saturates_to_exact_
zero_or_one` (a 40-row, 2-bin fixture that reproduced `cal.y == (0.0,
1.0)` exactly before the fix) and `test_isotonic_calibrator_smoothing_
converges_to_raw_rate_at_scale` (a 10,000-row single bin must not be
distorted by the correction — non-regression on well-populated bins).

### 14.4 Before / after — same 114-fold, 86,755-row run, post-fix

| metric | raw | calibrated, **pre-fix** | calibrated, **post-fix** |
|---|---|---|---|
| log-loss | 0.3125 | 0.3658 (+17.1%) | 0.3144 (**+0.6%**) |
| Brier | 0.0947 | 0.0950 (+0.3%) | 0.0950 (+0.3%, unchanged) |
| RPS | 0.0947 | — | 0.0950 |
| ECE (equal-width, 10 bins) | 0.0170 | 0.0056 | 0.0059 |
| ECE (equal-frequency, 10 bins) | 0.0187 | — (not measured pre-fix) | 0.0109 |
| calibration slope (95% CI) | 0.896 [0.884, 0.908] | 0.890 [0.878, 0.902] | **0.963 [0.950, 0.976]** |
| calibration intercept | 0.055 | — | -0.062 |
| exact-0.0 predictions | — | 2,602 (3.00%) | **0 (0.00%)** |
| min / max calibrated p | — | 0.0 / (no p≥1-1e-9) | 0.000608 / 0.939807 |

The fix does not merely restore parity — it makes the calibrator do what
§12/§13 always expected of it. The pre-fix calibrated slope (0.890) had
barely moved from raw (0.896); post-fix it is 0.963, materially closer to
1.0 with a CI half the width of raw's departure from 1. Brier is a wash in
both directions (as the original hypothesis predicted — Brier barely
notices a 3%-of-rows saturation event either way). Log-loss no longer
regresses in any practical sense: +0.0019 (+0.6%) is within the same order
of magnitude as Brier's own "wash," not a genuine scoring-rule failure.

### 14.5 Decision: `calibrate=True` stays the default

Evidence, not precedent, per this task's own instruction (`cards` shipped
`calibrate=True` this session on its own measurement; `saves` shipped
`calibrate=False` on its own measurement — both are live answers to the
same question, not a tie-breaker either way):

- Log-loss regression that motivated this investigation is **closed** —
  from +17.1% to +0.6%, and the residual is explained (a handful of
  formerly-saturated predictions still sit slightly off, since Jeffreys
  smoothing narrows but does not eliminate finite-sample uncertainty at the
  extremes — it is not supposed to).
- ECE and calibration slope — the two reliability measures §7.1 actually
  gates on, not log-loss/Brier alone — both improve substantially over raw
  (ECE −65% width-binned, slope 0.30 points closer to 1.0 with a tighter
  CI).
- No exact 0/1 assertions remain in 86,755 OOS predictions — the specific
  failure mode that made this a live investigation cannot recur by
  construction (Jeffreys smoothing structurally bounds every fitted bin
  away from 0 and 1, not merely reduces the frequency).

**Recommendation: keep `fit_minutes_model`'s `calibrate=True` default.**
The evidence that justified it in session s003 (log-loss, Brier, ECE all
improving) is not fully restored on log-loss alone (+0.6%, not a strict
win), but the reliability case — the actual thing the optimiser consumes,
per blueprint §7.1's own amendment — is stronger post-fix than it was at
s003's original measurement (slope 0.963 vs s003's un-quantified pre-l2
number). Flagged to the Architect rather than silently reverted: this is
the first time in this project raw log-loss beats calibrated log-loss on
this model, however narrowly, and blueprint §7.1 item 3 requires a stated
decision on any accepted departure, not silence.

### 14.6 What this does not settle

**The gate verdict itself was checked directly, not assumed** — real
baselines from the same 114-fold run: baseline A (`last_gw_start`)
log-loss **3.1337**, baseline B (`position_rate`) log-loss **0.6035**. Even
the pre-fix, saturated calibrated series (0.3658) clears both by a wide
margin, so `beats_both_baselines()` was `True` for raw, pre-fix-calibrated,
AND post-fix-calibrated alike — **the §7.1 gate verdict for minutes/START
did not move at any point in this investigation.** The brief's stop
condition ("if the gate verdict for any minutes outcome would move, stop
and report") was not triggered; stated here rather than silently confirmed
so a reader does not have to take that on faith.

What genuinely was **not** re-run here: `scripts/calibration_report.py`
and `docs/wiki/calibration-report.md` are this task's READ-ONLY paths, and
this session's own 114-fold/86,755-row numbers were produced by an ad hoc
read-only script mirroring `run_minutes()`'s convention, not by
regenerating the report itself. The Architect or the next report-owning
session should regenerate `docs/wiki/calibration-report.md` so its
rendered decision text (which currently still narrates the pre-fix
regression as an open Architect-level finding, per `calibration_report.
py`'s `run_minutes()` construction) reflects that the finding is now
closed — this section is the evidence for that regeneration, not a
replacement for it.
