"""Cards / discipline model — blueprint §4 (sixth model row: "Yellow/red
probabilities. Referee assignment is a real feature."), §7.1 (calibration
before points), §12.2 (derived-fact labelling). Phase 2 / E5, session
`s004` — the last model before the Phase 2 (E5) calibration gate.
Structural precedent: `fplai.models.defensive_contribution` (exposure-offset
count regression composed with `MinutesPMF`-shaped minute exposure, team-
style trailing feature instead of a team-name dummy) and `fplai.models.bonus`
(K-class categorical PMF, per-outcome reliability, multiclass walk-forward
gate); this module borrows the exposure convention from the first and the
categorical-gate machinery from the second, and differs from both in the
one thing this task's brief calls "the interesting part" — see "Referee: a
real feature, deliberately not deployed" below.

## What is verified, live, before anything else was designed

**The outcome space collapses to one 3-class categorical, not two
correlated binaries.** `yellow_cards`/`red_cards` are both complete (zero
nulls) across all 179,950 rows, 7 seasons. Checked directly, not assumed:
`yellow_cards` is binary (0 or 1) in EVERY row — FPL's own field never
reports 2 even though a second caution before a red is, in principle,
two bookings — and, decisively, **`yellow_cards` and `red_cards` are never
both 1 on the same row, in any of the 179,950 rows, in any of the 7
seasons** (`verify_yellow_red_mutually_exclusive_against_archive` below is
this check as a real, callable function, re-run against the real store by
`tests/test_cards.py`'s real-store-gated group and printed by
`scripts/fit_cards.py`). This means FPL's own scoring already resolves a
second-yellow dismissal as `red_cards=1, yellow_cards=0` — it does not
double-count the yellow. `OUTCOMES = (0, 1, 2)` (NONE/YELLOW/RED) is
therefore a genuine, complete, non-overlapping partition of "what a player's
box score says about his discipline this fixture" — a single categorical
target, never two independently-fit binary models that could jointly assign
positive probability to an impossible (yellow=1, red=1) cell (the same class
of physical-impossibility failure `fplai.models.bonus`'s own module
docstring names for a naive per-player bonus classifier).

**A card without a minute is real, not a data error.** 12 rows across the 7
seasons carry `minutes=0` with a card (`yellow_cards=1` in all 12) — a
bench/technical-area caution (dissent, encroachment, an unused sub booked
for time-wasting protest), verified by name/round (e.g. Jamaal Lascelles,
2022-23 rounds 16 and 19; Ashley Barnes, 2025-26 round 22). This module
follows `fplai.models.defensive_contribution`/`fplai.models.attacking`'s
`minutes > 0` fit-time filter (not `fplai.models.bonus`'s zero-inclusion —
cards has no fixture-wide rank coupling that would need the full squad
population the way bonus's Monte Carlo does, see "Cards are per-player, not
fixture-coupled" below), and states this 12-row, 0.007%-of-rows exclusion
explicitly rather than silently dropping it as though it were noise.

## Cards are per-player, not fixture-coupled — the design choice this
## task's brief flags directly

Bonus structurally couples every player in a fixture (a rank statistic over
a shared BPS pool); a card does not — one player being booked has no
mechanical effect on whether a teammate or an opponent is. This module
therefore fits and predicts **one player's own PMF independently**, exactly
`fplai.models.defensive_contribution`/`fplai.models.attacking`'s per-player
signature (`predict_cards_pmf` takes one player's feature row, returns one
player's PMF), never `fplai.models.bonus`'s whole-fixture Monte Carlo
signature. What COULD in principle create fixture-level coupling — a match
turning feisty, both sides matching each other's aggression, a red card
provoking retaliation — is acknowledged as a real phenomenon this module
does not model (see "What match-level coupling was rejected" below), the
same class of stated, not hidden, scope boundary
`defensive_contribution`'s own docstring gives for squad-level correlated
Monte Carlo.

## Minutes as an EXPOSURE OFFSET, not an ordinary feature — following DC,
## not bonus

A card count is structurally non-negative (unlike bonus's BPS, which can be
negative and forced that module to put `minutes_frac` in as an ordinary
regression feature rather than a multiplicative offset) and genuinely scales
with time on the pitch (more minutes, more opportunities to commit a
bookable offence) — exactly `fplai.models.defensive_contribution`'s own
reasoning for its `offset = log(minutes / 90)` term. This module uses the
SAME construction: the multinomial log-odds for YELLOW/RED (relative to
NONE) each carry `offset = log(minutes_this_fixture / 90)` baked in at a
FIXED coefficient of 1 (never fit), so `sigmoid`-scale log-odds shrink
toward the reference class as minutes shrink, without a `minutes_frac`
feature eating a beta slot or risking the `exp(offset) * negative` sign
failure bonus's own module docstring documents (irrelevant here — a
multinomial log-odds offset has no sign constraint to violate). Composed
at PREDICT time with a caller-supplied `MinutesPMF`-shaped minute-exposure
mixture, exactly `predict_dc_pmf`'s own composition (no import, module
independence preserved).

## Referee: a real feature, deliberately not deployed — the interesting
## part of this task

`fplai.backfill._valid_at_for` anchors `match.officials@match` at
**kickoff** (verified directly in that module's own docstring, this
session) — the leak-SAFE direction (§3.2: understating how early a fact
became true is conservative), but it means a bitemporal query resolved
`as_of` the FPL deadline (~1.5h before the FIRST kickoff of a gameweek)
cannot see who the referee is, even though the Premier League genuinely
publishes appointments days ahead — **this store has never observed the
announcement instant**, only the kickoff-anchored settlement. Inventing an
offset ("assume it's known 3 days before kickoff") would fabricate a fact
nobody observed — exactly the failure mode blueprint §3.2 exists to
prevent. `KNOWN_ABSENT_FEATURES` below therefore declares
`referee_identity` absent for FORWARD, DEPLOYABLE use, the same
`fplai.models.attacking`/`fplai.models.minutes` precedent for a genuinely
missing forward-looking signal.

**What this module does instead, per this task's brief**: measures,
retrospectively, how much a referee-strictness feature would be worth IF
an announcement-time capability existed — `measure_referee_value_
NOT_DEPLOYABLE` fits the SAME multinomial model twice, walk-forward, once
on `NUMERIC_FEATURE_COLUMNS_CARDS` (deployable) and once on
`NUMERIC_FEATURE_COLUMNS_CARDS_WITH_REFEREE_NOT_DEPLOYABLE` (deployable +
one additional feature, `referee_trailing_cards_per_match_10` — this
referee's own trailing mean of total cards shown per match he has
officiated, built from real historical matches only, `shift(1)`ped so a
match never sees its own outcome), and reports the log-loss/Brier/ECE DELTA
between them. This is the size of the prize a real announcement-time
capability would buy — not a promise that it is worth building, a number to
decide with.

**Why a trailing RATE, not a 38-level categorical dummy.** `pl_match_
officials` carries 38 distinct referees over 2,274 matches (~60 matches per
referee, 6 seasons) — a one-hot dummy per referee would burn 37 parameters
to explain rare classes (5.2% yellow, 0.17% red) with genuinely thin
per-referee support in any early walk-forward fold, and — the more
important reason — a categorical dummy cannot degrade gracefully to a
referee the fitting window has never seen (the promoted-team-dummy problem
`fplai.models.defensive_contribution`'s own docstring already diagnoses for
teams, here recurring for referees). A single trailing numeric "how many
cards has this referee shown lately" feature is estimable, degrades
gracefully (cold-start fallback below), and is the literal football
quantity "referee strictness" that a scout would actually cite.

**No numeric referee id exists anywhere upstream** (`fplai.schemas`'s own
`MATCH_OFFICIALS_MATCH` schema block: "NO STABLE NUMERIC ID EXISTS for an
official... `official_name` is the only identifier the source provides") —
this module's referee-trailing rollup therefore keys on the raw name
string, inheriting that same undocumented-upstream risk (accents, initials,
homonyms) verbatim, stated here rather than silently assumed away.

**Structural guarantee, not just a naming convention: the referee feature
cannot reach a deployable fitted model.** `fit_cards_model`/`predict_cards_
pmf` (the only functions that produce or consume a `CardsModelParams` a
caller could use for a real prediction) hard-code `NUMERIC_FEATURE_COLUMNS_
CARDS` — there is no parameter on `fit_cards_model` that accepts a
different feature-column set, so no call to it can ever produce a fitted
model carrying a referee coefficient. The referee-inclusive variant exists
ONLY inside `walk_forward_validate`'s (parameterised) `feature_columns`
argument and `measure_referee_value_NOT_DEPLOYABLE`, which return METRICS
(`CardsWalkForwardResult`), never a `CardsModelParams` `predict_cards_pmf`
could accept. **Attacked directly, this session**
(`tests/test_cards.py::
test_predict_cards_pmf_structurally_cannot_be_influenced_by_a_referee_feature_smuggled_into_the_feature_row`):
a caller who fits a normal (deployable) model and then, at PREDICT time,
smuggles a `referee_trailing_cards_per_match_10` key into `feature_row`
gets back the EXACT SAME PMF as a caller who omits it — `_feature_row_to_
vector` only ever reads the columns named in `params.feature_spec.
numeric_columns`, which for any `CardsModelParams` produced by `fit_cards_
model` is always `NUMERIC_FEATURE_COLUMNS_CARDS` and nothing else, so there
is no coefficient a smuggled key could multiply against. This is the
break-first proof this project's standing rule requires for any claimed
guarantee, attacked from outside the sanctioned call path (a caller reaching
into `feature_row` directly, not going through some blessed "safe" helper).

## What match-level coupling was rejected

`pl_team_match_stats` carries team-level discipline stats at MATCH grain
(`totalYelCard`/`totalRedCard`/`fkFoulLost`/`fkFoulWon`, 246 distinct
`stat_key` values checked live this session, 701,810 rows, 2,274 matches —
the same match population `match.officials@match` covers) — a genuine,
directly-observed team-tackling-style/derby-intensity signal, unlike DC's
own team-style feature (built from DC's own history, a documented mild
circularity that module's docstring names). **Rejected for the FIT feature
set, for a concrete, verified reason, not a vague "out of scope."**
Building a round-grain team-trailing feature from it requires joining
`pl_team_match_stats` back onto a `(season, team, round)` key via `pl_
match_fixtures.matchweek` — and `matchweek` is **NOT** a reliable proxy for
FPL's own `round`: checked live, this session, over every one of the 2,274
matches this module's own referee join resolves, `round != matchweek` on
**101 of 2,274 (4.4%)**, with divergences as large as round 22 vs.
matchweek 8 (a postponed-and-rescheduled fixture, the PL's own matchweek
number frozen at its ORIGINAL slot while FPL's round reflects when it was
actually played). Using `matchweek` as a stand-in for `round` would
silently misalign a team's trailing discipline history against the wrong
gameweek for every one of those 101 matches — exactly the join-drops-the-
label failure CLAUDE.md rule 3 warns about, just one level more subtle
(the join succeeds, the VALUE it attaches is wrong). This module instead
builds its own team-style feature (`team_trailing_cards_mean_5`) entirely
from `vaastav_player_gameweek_stats`'s own `round` column — zero new joins,
the same "use what's already verified at this grain" discipline `fplai.
models.defensive_contribution`'s own `team_trailing_dc_mean_5` establishes.
**A real, named seam for a future session**: resolving the `matchweek` <->
`round` mapping properly (almost certainly via `kickoff_time`/`kickoff`
matching per-fixture rather than trusting either numbering directly) would
unlock `pl_team_match_stats`'s far richer per-team foul/tackle signal for
this and every other match-grain-dependent model in this project.

## Feature design

`NUMERIC_FEATURE_COLUMNS_CARDS` (8, deployable):

- `player_trailing_card_rate_{3,5,10}` — this player's own trailing
  per-round rate of "carded at all" (yellow OR red — see "Why one combined
  trailing signal, not split by class" below), same "player identity is the
  SECOND input" trailing-feature discipline every sibling module uses.
- `team_trailing_cards_mean_5` — the player's TEAM's trailing mean total
  cards shown across its whole roster, at ROUND grain, DGW-safe — the
  team-tackling-style proxy, built entirely from this store's own data (see
  "What match-level coupling was rejected" above for why this is NOT built
  from `pl_team_match_stats` instead).
- `games_played_this_season`, `cold_start`, `team_cold_start` — same
  cold-start discipline every sibling module uses.
- `was_home` — a fixture-own column, never rolled up. A real, literature-
  supported referee-bias signal (home teams are shown fewer cards on
  average) — left as a plain feature for the regression to estimate, not
  asserted here as a fact this module already knows the sign or magnitude
  of.

Position is one-hot (`GK`/`DEF`/`MID`/`FWD` — unlike `fplai.models.
defensive_contribution`, cards has no position GATE: every position carries
real card mass in this store, checked live: GK 289 yellow / 5 red across
18,077 rows, so a goalkeeper's PMF is a genuine fitted quantity, never a
structurally-degenerate spike).

## Why one combined trailing signal, not split by class

`player_trailing_card_rate_*`/`team_trailing_cards_mean_5` are built from
"any card" (yellow OR red combined), not two separate yellow-rate and
red-rate trailing features. Red is genuinely rare (310/179,950 rows,
0.17%) — a RED-specific trailing rate over even a 10-round window would sit
at exactly 0.0 for the overwhelming majority of players and carry almost no
signal, while adding real variance risk from the handful of players who
DO have one red in their trailing window (a single historical red would
swing a 10-round trailing red-rate from 0.0 to 0.1, a 10x jump on a
near-zero base). A combined "any card" trailing signal is the practically
estimable quantity — "this player fouls/gets into confrontations often" —
and the multinomial regression's own fitted `beta_RED` (a SEPARATE
coefficient vector from `beta_YELLOW`, not a shared one) is what lets that
same combined signal load differently onto the two outcome log-odds if the
real data supports it.

## Multinomial logit, closed-form gradient — deterministic L-BFGS-B

`OUTCOMES = (0, 1, 2)` (NONE/YELLOW/RED), reference class 0. Two log-odds,
`eta_1` (YELLOW vs NONE) and `eta_2` (RED vs NONE), each
`X @ beta_k + offset` (`offset = log(minutes_this_fixture / 90)`, module
docstring "Minutes as an EXPOSURE OFFSET"). `_multinomial_neg_log_lik_and_
grad` is the standard softmax cross-entropy + its analytic gradient
(`dNLL/d(beta_k) = X.T @ (p_k - 1{y=k}) / n + 2*l2*beta_k`), cross-checked
against finite differences (`tests/test_cards.py::
test_multinomial_neg_log_lik_gradient_matches_finite_differences`) — same
"a documented derivation AND a numeric check" discipline `fplai.models.
attacking`/`fplai.models.defensive_contribution` both establish for their
own analytic gradients. Every numeric feature is standardised (z-score,
frozen at fit time — `CardsFeatureSpec.numeric_means`/`numeric_stds`) for
the same reason `fplai.models.attacking`'s own module docstring diagnoses
at length: this module's own real scale spread is at least as wide
(`games_played_this_season` 0..38 against `player_trailing_card_rate_*`'s
0..1) to make a fixed `l2_penalty` risk crushing the small-scale features.
`l2_penalty`'s default is swept against the real store, not guessed — see
`CardsModelConfig.l2_penalty`'s own docstring for the real sweep table.

## Walk-forward gate (blueprint §7.1)

Same categorical-gate shape `fplai.models.bonus` already establishes for a
K-class PMF (multiclass log-loss/Brier, TWO fold-internal baselines: group/
position rate and player-trailing rate, split by ROUND never row count),
applied to this module's 3-class outcome. `walk_forward_validate` refits
(deterministic L-BFGS-B) at every `(season, round)` fold using only
strictly-earlier rows, restricted to `minutes > 0` throughout (module
docstring, "A card without a minute is real, not a data error" — the 12
real zero-minute cards are excluded from BOTH fitting and evaluation, the
same `minutes > 0` restriction `fplai.models.attacking`/`fplai.models.
defensive_contribution` apply to their own walk-forward gates).

## Reliability — required per-outcome (this task's brief, explicit)

`CardsWalkForwardResult.reliability_for_outcome(k)`/`reliability_for_
outcome_raw(k)` — one full ECE (BOTH equal-width and quantile binning),
Cox calibration slope/intercept WITH a 95% CI, and both reliability
diagrams, for EACH of the three one-vs-rest series `P(outcome == k)` vs.
`1{outcome == k}`, `k` in `0, 1, 2`. Session `s005`: this now DELEGATES to
`fplai.calibration.evaluate_binary_outcome` rather than a fifth local
duplicate of the same math -- `fplai.calibration`'s own module docstring
states plainly it exists so "the duplicated math converges instead of
being re-copied a sixth time" and explicitly carves itself OUT of the
"no cross-model import" rule ("this module is not a model... it does not
apply here"); the calibration-slope confidence interval and quantile ECE
this session's re-gate needs (see "Nested out-of-sample NONE/YELLOW
calibration" below) already exist there, correctly, so re-deriving them a
seventh time would be the exact duplication that module was built to stop.
The five per-model local copies this docstring used to describe
(`_log_loss`/`_brier`/`ReliabilityBin`/`reliability_diagram`/`expected_
calibration_error`/`_calibration_slope_intercept`/local `CalibrationMetrics`)
are REMOVED from this module, not left as dead code alongside the import.

## Nested out-of-sample NONE/YELLOW calibration — session s005, the E5
## blocking condition this task closes

E5 passed with a rejected acceptance (blueprint §7.1 amendment, `docs/
HANDOFF.md` §2): cards NONE/YELLOW are OVERCONFIDENT (slopes 0.592/0.630,
CIs nowhere near 1) -- the dangerous direction for an optimiser to inherit,
unlike bonus's accepted UNDERconfidence. The remedy is the same nested,
out-of-sample isotonic layer `fplai.models.minutes` already proved (slope
1.114->0.972, ECE 0.0507->0.0131) -- `IsotonicCalibrator`/`_fit_isotonic_
calibrator`/`_inner_calibration_split` below are DUPLICATED from that
module (this module's own "no cross-model import" discipline, restated
above for `fplai.calibration`'s deliberate exception -- this one is NOT an
exception: `fplai.models.minutes` is a sibling MODEL, not shared pure math),
adapted for this module's 3-class outcome space.

**Scope: NONE and YELLOW only, never RED.** RED is base-rate-only (slope
-0.081, CI containing 0, module docstring "What is verified" and `docs/
wiki/calibration-report.md`) -- calibrating a predictor that carries no
usable ranking signal is meaningless (there is no genuine reliability curve
to correct, only noise to overfit). `CardsModelParams.p_none_calibrator`/
`p_yellow_calibrator` are ONE-VS-REST isotonic calibrators, each fit on
(that class's own raw predicted probability, `1{outcome==that class}`)
pairs from a fold's/window's OWN nested `calib_holdout` slice -- exactly
`fit_minutes_model`'s inner-split boundary (fit on `inner_train`, score
OOS on `calib_holdout`, calibrate from THAT pair), never the eval fold's
own outcome.

**Renormalisation -- the real design question this task's brief names
directly.** Two independently-fit one-vs-rest calibrators do not, in
general, sum to anything meaningful with each other or with RED's raw
probability; `CardsPMF.probabilities` must still sum to 1.0 exactly (rule
5's shape guarantee, enforced in `__post_init__`). The choice made here
(`_apply_cards_calibration`): hold RED's raw probability COMPLETELY FIXED
(this module never learns anything about RED's calibration, so it never
touches RED's value at all -- the most literal reading of "leave RED
alone"), then rescale the two calibrated values `(cal_NONE, cal_YELLOW)`
PROPORTIONALLY so they sum to exactly `1 - p_RED_raw`, preserving the
RATIO the two independent calibrators produced while giving up their
individual absolute levels to the renormalisation. This is the same
mechanism `predict_minutes_pmf` already uses one dimension down (there,
ONE calibrated value -- START -- and the "remaining" SUB+UNUSED mass is
rescaled proportionally to make room for it, never re-calibrated itself);
here there are TWO calibrated values sharing one fixed remaining budget
instead of one calibrated value and two raw ones.

**The stated cost.** The final reported NONE/YELLOW are `cal * scale`, not
`cal` itself -- a further multiplicative perturbation on top of what each
calibrator actually learned, needed only to satisfy the sum-to-1 shape
constraint. `scale`'s expectation is 1 in aggregate (both calibrators are
fit on real out-of-sample base rates that should already integrate close
to `1 - p_RED_raw`), so this is a second-order correction relative to the
slope-0.60 miscalibration being fixed, not a competing distortion of
similar size -- but it is not proven to be negligible in general, only
measured to be small on this store's real data (see the punch-out/`docs/
wiki/model-cards.md` for the actual before/after numbers). The honest
alternative this session did NOT take -- calibrating the conditional
NONE-vs-YELLOW split (a single binary calibrator on `p_NONE/(p_NONE+
p_YELLOW)` restricted to non-RED rows, automatically summing to
`1-p_RED_raw` with no rescale step at all) -- would have zero renormalisation
cost but a different one: it cannot independently correct NONE's and
YELLOW's slopes to different targets, only their RELATIVE split, and the
measured slopes here (0.592 vs 0.630) are close enough that this was not
obviously the deciding factor, but it was not tested head-to-head this
session. Named as a real, cheaper alternative for a future session, not
silently dismissed.

**Where this shows up.** `fit_cards_model(calibrate=True)` (default) fits
the production `CardsModelParams`; `predict_cards_pmf` applies calibration
per minute-exposure component (module docstring, "Minutes as an EXPOSURE
OFFSET" -- each exposure entry has its own offset and therefore its own raw
`(p0,p1,p2)`, calibrated individually before weighting into the mixture).
`walk_forward_validate(calibrate=True)` (ALSO the new default, deliberately
DIFFERENT from `fplai.models.minutes.walk_forward_validate`'s own
`calibrate=False` default -- see that function's docstring for why) fits a
FRESH nested calibrator inside every fold, exactly the per-fold discipline
`fit_minutes_model`'s calibrator uses at the top-level `as_of` fit, applied
here per gate fold instead. `CardsWalkForwardResult.p_model` is the
CALIBRATED series when `calibrate=True`; `p_model_raw` is ALWAYS the raw
series regardless of the flag, so a single walk-forward run yields an
honest, apples-to-apples before/after comparison without a second run.

**Attacked, not just asserted (this task's brief, explicit).** `tests/
test_cards.py::test_cards_calibrator_fit_directly_on_eval_data_would_leak_
and_look_suspiciously_good` constructs the FORBIDDEN path by hand (fit
NONE/YELLOW calibrators on the eval fold's own predictions/outcomes) and
shows it produces a strictly better log-loss than the honest nested version
on the same real-shaped synthetic data. `tests/test_cards.py::
test_walk_forward_validate_calibrate_cannot_see_a_folds_own_or_future_
outcomes` perturbs a LATER round's outcomes and shows an EARLIER fold's
`p_model` entries are bit-identical before and after -- the nested split
never reaches into `train`'s own future relative to that fold, exactly
`fplai.models.minutes`'s own future-fold-isolation proof, repeated here
because (that test's own docstring states, restated for this module) the
nested-calibration boundary is a SEPARATE claim from the raw model's own
leakage boundary and must be attacked separately, not assumed to inherit
the raw proof for free.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import polars as pl

from fplai.calibration import (
    CalibrationMetrics,
    IsotonicCalibrator,
    evaluate_binary_outcome,
    fit_isotonic_calibrator as _fit_isotonic_calibrator,
)
from fplai.derived import CalibrationReference, DerivationInput, write_derived
from fplai.gameweek_stats import read_player_gameweek_stats
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    PLAYER_CARDS_DISTRIBUTION_DATASET,
    PLAYER_CARDS_DISTRIBUTION_GAMEWEEK,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    FactTableSchema,
    register_derived_capability,
)
from fplai.store import BitemporalStore, WriteResult

logger = logging.getLogger(__name__)

DATASET = "vaastav_player_gameweek_stats"

REQUIRED_COLUMNS = (
    "season",
    "round",
    "element",
    "fixture",
    "kickoff_time",
    "minutes",
    "position",
    "team",
    "was_home",
    "opponent_team",
    "yellow_cards",
    "red_cards",
)

POSITIONS: tuple[str, ...] = ("GK", "DEF", "MID", "FWD")
OUTCOMES: tuple[int, ...] = (0, 1, 2)
OUTCOME_LABELS: tuple[str, ...] = ("NONE", "YELLOW", "RED")

# Module docstring, "Referee: a real feature, deliberately not deployed" —
# same declare-don't-proxy discipline fplai.models.minutes.KNOWN_ABSENT_
# FEATURES/fplai.models.attacking.KNOWN_ABSENT_FEATURES/fplai.models.bonus.
# KNOWN_ABSENT_FEATURES all establish. `referee_identity` is genuinely
# OBSERVABLE in this store (match.officials@match) but not resolvable as of
# a pre-deadline as_of -- a different reason for absence than "this store
# has no such capability at all", stated as such.
KNOWN_ABSENT_FEATURES: tuple[str, ...] = ("referee_identity",)


class CardsModelError(ValueError):
    """Misuse or a genuine data problem this module refuses to guess past —
    a naive `as_of`, an empty training window, a PMF that would not
    (re)normalise, a `minute_exposure` that does not sum to 1, or a
    yellow_cards/red_cards row that violates the verified mutual-exclusion
    invariant."""


# ---------------------------------------------------------------------------
# The outcome-space guarantee — pinned from the archive, break-first
# verified (module docstring, "What is verified, live, before anything else
# was designed").
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeSpaceVerification:
    """The result of checking, over every real row in the store, that
    `yellow_cards`/`red_cards` never both read 1 and that `yellow_cards`
    itself is never anything but 0/1 — the two facts that let this module
    collapse discipline into ONE 3-class categorical rather than two
    binaries that could jointly assign mass to an impossible cell."""

    n_rows: int
    n_yellow_and_red_both_one: int
    n_yellow_out_of_range: int
    n_red_out_of_range: int

    @property
    def holds(self) -> bool:
        return self.n_yellow_and_red_both_one == 0 and self.n_yellow_out_of_range == 0 and self.n_red_out_of_range == 0


def verify_yellow_red_mutually_exclusive_against_archive(
    store: BitemporalStore, *, as_of: datetime
) -> OutcomeSpaceVerification:
    """Re-derive the "never both 1" claim this module's docstring states,
    LIVE, against whatever store is passed in — never a cached/hardcoded
    number. Attacked from outside the sanctioned call path: this checks
    the RAW `effective_at()` rows directly, not this module's own derived
    `outcome` column, so a bug in `build_training_table`'s own outcome
    derivation could never hide behind this check passing."""
    if as_of.tzinfo is None:
        raise CardsModelError(f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2.")
    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise CardsModelError(f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()}")

    # Decision 2: attribution_complete=False rows never train (degraded DGW rows)
    raw = raw.filter((pl.col("attribution_complete") == True) | pl.col("attribution_complete").is_null())

    missing = [c for c in ("yellow_cards", "red_cards") if c not in raw.columns]
    if missing:
        raise CardsModelError(f"player gameweek stats is missing required column(s) {missing}")

    both_one = raw.filter((pl.col("yellow_cards") == 1) & (pl.col("red_cards") == 1))
    yellow_bad = raw.filter(~pl.col("yellow_cards").is_in([0, 1]))
    red_bad = raw.filter(~pl.col("red_cards").is_in([0, 1]))
    return OutcomeSpaceVerification(
        n_rows=raw.height,
        n_yellow_and_red_both_one=both_one.height,
        n_yellow_out_of_range=yellow_bad.height,
        n_red_out_of_range=red_bad.height,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardsModelConfig:
    """Every fitting hyperparameter, in one place — same convention every
    sibling module establishes."""

    player_trailing_windows: tuple[int, ...] = (3, 5, 10)
    team_trailing_window: int = 5
    referee_trailing_window: int = 10
    """Module docstring, "Why a trailing RATE, not a 38-level categorical
    dummy" — ~60 matches/referee over the 6-season window this module's own
    referee join covers; 10 is a real, stated starting point (large enough
    to smooth a single outlier match, small enough that a referee genuinely
    changes strictness over a season is still tracked), not a swept
    optimum (this task's brief does not ask for one on the research-only
    branch)."""
    l2_penalty: float = 0.001
    """Ridge penalty on each of the two (standardised) multinomial log-odds'
    beta vectors. Swept against the real store this session (`{1.0, 0.3,
    0.1, 0.05, 0.02, 0.01, 0.005, 0.001, 0.0005, 0.0001}`,
    `scripts/fit_cards.py`'s own punch-out carries the full table) — every
    value `>= 0.005` FAILS the §7.1 gate outright on this module's real
    training rows (e.g. `l2=0.01`: model log-loss 0.4287 vs. the
    position-rate baseline's 0.4012) for the same reason `fplai.models.
    attacking`'s own module docstring diagnoses for its own Binomial-share
    likelihood: card issuance is a genuinely rare per-appearance event
    (5.2% yellow, 0.17% red on the real minutes>0 fit population — the walk-
    forward's own out-of-sample split measures a slightly different, still
    rare, 12.5%/0.4% split), so the likelihood's natural curvature is weak
    and a ridge penalty tuned for a higher-base-rate likelihood over-
    shrinks this one toward the reference class. `0.001` is the value
    chosen — the gate FIRST passes at this value (log-loss 0.3973 vs.
    baseline 0.4006, Brier 0.2215 vs. 0.2249) and keeps improving marginally
    through `0.0001`; `0.001` is not the sweep's most aggressive point,
    left with headroom rather than chased to the sweep's edge. **A real,
    stated defect, not silently accepted**: even at this and smaller `l2`,
    the per-outcome Cox calibration slope sits well below 1.0 for NONE/
    YELLOW (~0.60-0.64, i.e. genuinely overconfident) and near 0 for RED —
    checked NOT to be a code bug (a synthetic dataset drawn from a KNOWN
    multinomial-logit-with-offset process, fit through this exact walk-
    forward path, recovers the textbook-expected slope>1 when regularised
    ABOVE the true generating scale and approaches 1 as `l2` relaxes toward
    it — see `tests/test_cards.py::
    test_walk_forward_calibration_slope_recovers_correctly_on_a_known_synthetic_generative_process`)
    — so the real-data slope departure is a genuine property of this fit
    against real, non-stationary football data, not an artefact of this
    module's own machinery. ECE is nonetheless low (0.0099/0.0096/0.0060 for
    NONE/YELLOW/RED — the same ballpark `fplai.models.bonus`'s own shipped
    ECE range, 0.0002-0.0091), and this module ships with the same posture
    that module's own docstring states: measured, reported, flagged, not
    silently accepted or hidden behind a calibrator built without evidence
    that it helps. A per-outcome isotonic/Platt recalibration layer (the
    precedent `fplai.models.minutes`' nested isotonic P(start) calibrator
    sets) is a real, named seam for a future session — this task's brief
    asks for measurement and a decision stated either way, not a mandate to
    build one; the decision here is: not built this session, evidenced and
    left open. (NONE/YELLOW got exactly this layer the following `s005`
    session — module docstring, "Nested out-of-sample NONE/YELLOW
    calibration".)

    **Session `s005`, a second, later story: `_multinomial_neg_log_lik_
    and_grad`'s L2-scaling bug was fixed** (see that function's own
    docstring) and this default re-measured, RAW and CALIBRATED both,
    swept across `0.0001`..`1.0`, before being touched — including
    checking whether the sweep above (which found every `l2 >= 0.005`
    failing the gate) was itself an artefact of the bug. It was not: the
    corrected formula's own sweep still shows the gate passing comfortably
    at `0.001` with no retuning indicated. NONE/YELLOW improve modestly
    (~0.6% log-loss, both raw and calibrated); the NONE/YELLOW isotonic
    calibrator (added the session before this fix) is measured to still be
    closing essentially the same gap it always did, not made redundant by
    this fix. **RED's slope moves from clearly-unusable (-0.064, CI
    including 0) to marginally-distinguishable-from-zero (~0.07-0.12
    depending on `l2`, CI narrowly excluding 0 at this shipped default) —
    ARCHITECT RULING: RED stays BASE-RATE-ONLY regardless**, immaterial in
    points (~0.414% base rate, ~-3 points, roughly -0.012 expected
    points/appearance) and boundary-sensitive across the swept grid, not a
    robust new signal. `docs/wiki/model-cards.md` has the full grid, the
    raw-vs-calibrated comparison, and the ruling."""

    max_lbfgs_iterations: int = 300
    """No `max_count`/PMF-truncation field here, unlike `fplai.models.
    defensive_contribution`/`fplai.models.attacking`/`fplai.models.bonus`:
    this module's outcome space is always exactly `OUTCOMES = (0, 1, 2)`
    (NONE/YELLOW/RED), never truncated or expanded, because it IS already
    the complete, bounded outcome space (module docstring, "What is
    verified") — there is no truncation decision for a config field to
    parameterise."""


# ---------------------------------------------------------------------------
# Raw-row normalisation — same two verified drift cases every sibling
# module documents for this exact dataset, duplicated (not imported) to
# keep this module independent.
# ---------------------------------------------------------------------------


def _normalise_and_filter_positions(raw: pl.DataFrame) -> pl.DataFrame:
    if "position" not in raw.columns:
        return raw
    raw = raw.filter(pl.col("position") != "AM")
    return raw.with_columns(
        pl.when(pl.col("position") == "GKP")
        .then(pl.lit("GK"))
        .otherwise(pl.col("position"))
        .alias("position")
    )


def _add_outcome_column(raw: pl.DataFrame) -> pl.DataFrame:
    """`outcome` in `{0, 1, 2}` (NONE/YELLOW/RED) — module docstring, "What
    is verified". Raises loudly (never silently clips/prefers one column)
    if a row violates the mutual-exclusion invariant this module's whole
    design rests on; `build_training_table` calls this on every table it
    builds, so a future data revision that breaks the invariant fails here,
    not silently downstream in a PMF that no longer sums to 1."""
    bad = raw.filter((pl.col("yellow_cards") == 1) & (pl.col("red_cards") == 1))
    if not bad.is_empty():
        raise CardsModelError(
            f"{bad.height} row(s) have yellow_cards==1 AND red_cards==1 — this module's outcome-space "
            "design (module docstring, 'What is verified') assumes these are mutually exclusive, "
            "verified 0/179,950 live this session. Refusing to silently pick one."
        )
    return raw.with_columns(
        pl.when(pl.col("red_cards") == 1)
        .then(pl.lit(2))
        .when(pl.col("yellow_cards") == 1)
        .then(pl.lit(1))
        .otherwise(pl.lit(0))
        .cast(pl.Int64)
        .alias("outcome")
    )


# ---------------------------------------------------------------------------
# Trailing feature rollups — round grain, leakage-safe, DGW-safe (module
# docstring, "Feature design"; mirrors fplai.models.defensive_contribution's
# own team/player rollups exactly in double-gameweek discipline).
# ---------------------------------------------------------------------------


def _build_team_round_rollup(raw: pl.DataFrame, config: CardsModelConfig) -> pl.DataFrame:
    team_round = (
        raw.group_by(["season", "team", "round"])
        .agg((pl.col("yellow_cards") + pl.col("red_cards")).sum().alias("team_round_cards_total"))
        .sort(["season", "team", "round"])
    )
    w = config.team_trailing_window
    team_round = team_round.with_columns(
        pl.col("team_round_cards_total")
        .cast(pl.Float64)
        .shift(1)
        .rolling_mean(window_size=w, min_samples=1)
        .over(["season", "team"])
        .alias(f"team_trailing_cards_mean_{w}"),
        pl.col("team_round_cards_total").shift(1).over(["season", "team"]).is_null().alias("team_cold_start"),
    )
    return team_round.with_columns(pl.col(f"team_trailing_cards_mean_{w}").fill_null(0.0)).select(
        ["season", "team", "round", f"team_trailing_cards_mean_{w}", "team_cold_start"]
    )


def _build_player_round_rollup(raw: pl.DataFrame, config: CardsModelConfig) -> pl.DataFrame:
    """`round_carded_total` (a DGW-round sum of "carded at all", can exceed
    1 in a genuine double gameweek) feeds the BASELINE-only per-class
    trailing rate columns, never the regression itself — same convention
    `fplai.models.bonus._build_player_round_rollup` establishes for its own
    `player_trailing_bonus_class_rate_*`."""
    rollup = (
        raw.group_by(["season", "element", "round"])
        .agg(
            (pl.col("yellow_cards") + pl.col("red_cards")).sum().alias("round_carded_total"),
            pl.col("outcome").max().alias("round_outcome_max"),  # DGW: worst outcome that round, baseline-only
            pl.col("minutes").sum().alias("round_minutes"),
        )
        .sort(["season", "element", "round"])
    )
    appeared = (pl.col("round_minutes") > 0).cast(pl.Float64)
    carded_any = (pl.col("round_carded_total") > 0).cast(pl.Float64)

    trailing_exprs = []
    for w in config.player_trailing_windows:
        trailing_exprs.append(
            carded_any.shift(1)
            .rolling_mean(window_size=w, min_samples=1)
            .over(["season", "element"])
            .alias(f"player_trailing_card_rate_{w}")
        )
    trailing_exprs.append(
        appeared.shift(1).fill_null(0.0).cum_sum().over(["season", "element"]).alias("games_played_this_season")
    )
    # Baseline-only features (never fed to the multinomial regression itself
    # — see walk_forward_validate) — this player's own trailing per-class
    # outcome rate, kept nullable so a genuine cold start is visibly absent,
    # same convention every sibling module's own trailing-rate baseline uses.
    for k in OUTCOMES:
        indicator = (pl.col("round_outcome_max") == k).cast(pl.Float64)
        trailing_exprs.append(
            indicator.shift(1)
            .rolling_mean(window_size=5, min_samples=1)
            .over(["season", "element"])
            .alias(f"player_trailing_cards_class_rate_{k}_5")
        )

    rollup = rollup.with_columns(trailing_exprs)
    fill_zero = [f"player_trailing_card_rate_{w}" for w in config.player_trailing_windows]
    rollup = rollup.with_columns([pl.col(c).fill_null(0.0) for c in fill_zero])
    rollup = rollup.with_columns((pl.col("games_played_this_season") == 0).alias("cold_start"))
    return rollup


NUMERIC_FEATURE_COLUMNS_CARDS: tuple[str, ...] = (
    "player_trailing_card_rate_3",
    "player_trailing_card_rate_5",
    "player_trailing_card_rate_10",
    "team_trailing_cards_mean_5",
    "games_played_this_season",
    "cold_start",
    "team_cold_start",
    "was_home",
)

assert not set(NUMERIC_FEATURE_COLUMNS_CARDS) & set(KNOWN_ABSENT_FEATURES)

# Module docstring, "Referee: a real feature, deliberately not deployed" —
# the RESEARCH-ONLY superset. Never consumed by fit_cards_model/predict_
# cards_pmf (module docstring, "Structural guarantee").
NUMERIC_FEATURE_COLUMNS_CARDS_WITH_REFEREE_NOT_DEPLOYABLE: tuple[str, ...] = NUMERIC_FEATURE_COLUMNS_CARDS + (
    "referee_trailing_cards_per_match_10",
)


# ---------------------------------------------------------------------------
# Training table — fixture grain, minutes>0 only (module docstring, "A card
# without a minute is real, not a data error").
# ---------------------------------------------------------------------------


def build_training_table(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: CardsModelConfig = CardsModelConfig(),
    allow_live_season: bool = False,
) -> pl.DataFrame:
    """The full leakage-safe feature+target table, one row per (season,
    round, element, fixture), `minutes > 0` only. Reads via the capability
    reader on `kickoff_time`, the same bitemporal primitive every sibling
    module uses for this capability."""
    if as_of.tzinfo is None:
        raise CardsModelError(f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2.")

    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise CardsModelError(f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()}")

    # Decision 1: live-season contamination — filter with WARNING log by default
    if not allow_live_season:
        live_rows = raw.filter(pl.col("source_provider") == "fpl_api")
        if not live_rows.is_empty():
            n_dropped = live_rows.height
            seasons_dropped = sorted(live_rows["season"].unique().to_list())
            logger.warning(
                f"excluding {n_dropped} FPL-API-sourced row(s) from season(s) {seasons_dropped} "
                f"(allow_live_season=False). Set allow_live_season=True to include them."
            )
            raw = raw.filter(pl.col("source_provider") != "fpl_api")

    # Decision 2: attribution_complete=False rows never train (degraded DGW rows)
    raw = raw.filter((pl.col("attribution_complete") == True) | pl.col("attribution_complete").is_null())
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise CardsModelError(f"player gameweek stats is missing required column(s) {missing}")

    raw = _normalise_and_filter_positions(raw)
    raw = raw.filter(pl.col("team").is_not_null())  # excludes 2019-20 (no position/team upstream), explicit not incidental

    if seasons is not None:
        seasons = tuple(seasons)
        raw = raw.filter(pl.col("season").is_in(list(seasons)))
        if raw.is_empty():
            raise CardsModelError(
                f"no player gameweek stats for seasons {seasons!r} at as_of={as_of.isoformat()}. "
                "Note that allow_live_season=False excludes FPL-API-sourced rows BEFORE this "
                "season filter is applied, so a live season requested here resolves to nothing."
            )

    if raw.is_empty():
        raise CardsModelError(
            "no rows remain after position/team normalisation -- this module excludes 2019-20 "
            "(no position/team upstream); check the requested seasons/as_of."
        )

    unexpected_positions = set(raw["position"].unique().to_list()) - set(POSITIONS)
    if unexpected_positions:
        raise CardsModelError(
            f"unexpected position value(s) after normalisation: {unexpected_positions} — expected "
            f"a subset of {POSITIONS}."
        )

    raw = _add_outcome_column(raw)

    team_rollup = _build_team_round_rollup(raw, config)
    team_col = f"team_trailing_cards_mean_{config.team_trailing_window}"
    player_rollup = _build_player_round_rollup(raw, config)

    trailing_cols = (
        [f"player_trailing_card_rate_{w}" for w in config.player_trailing_windows]
        + ["games_played_this_season", "cold_start"]
        + [f"player_trailing_cards_class_rate_{k}_5" for k in OUTCOMES]
    )
    table = raw.join(
        player_rollup.select(["season", "element", "round", *trailing_cols]),
        on=["season", "element", "round"],
        how="left",
    )
    table = table.join(team_rollup, on=["season", "team", "round"], how="left")
    table = table.with_columns(
        pl.col(team_col).fill_null(0.0),
        pl.col("team_cold_start").fill_null(True),
    )

    # Module docstring, "A card without a minute is real, not a data
    # error" — minutes>0 fit-time restriction, explicit and stated, not the
    # incidental side effect of some other filter.
    table = table.filter(pl.col("minutes") > 0)
    if table.is_empty():
        raise CardsModelError("no minutes>0 rows remain in this training table -- nothing to fit")

    check_cols = [
        c for c in NUMERIC_FEATURE_COLUMNS_CARDS if c not in ("was_home", "team_trailing_cards_mean_5", "team_cold_start")
    ]
    missing_after_join = [c for c in check_cols if table[c].null_count() > 0]
    if missing_after_join:
        raise CardsModelError(
            f"unexpected NULLs after joining trailing features: {missing_after_join} — a row failed "
            "to match its own (season, element, round) rollup, which should be structurally impossible."
        )

    season_order = {s: i for i, s in enumerate(sorted(table["season"].unique().to_list()))}
    table = table.with_columns(
        (pl.col("season").replace_strict(season_order, return_dtype=pl.Int64) * 100 + pl.col("round")).alias(
            "_chronological_rank"
        )
    )
    return table


# ---------------------------------------------------------------------------
# Referee-trailing feature — RESEARCH ONLY (module docstring, "Referee: a
# real feature, deliberately not deployed"). Never called by
# build_training_table/fit_cards_model; a separate, clearly-named entry
# point a caller must opt into.
# ---------------------------------------------------------------------------


def build_referee_trailing_feature_table_NOT_DEPLOYABLE(
    store: BitemporalStore, *, as_of: datetime, config: CardsModelConfig = CardsModelConfig()
) -> pl.DataFrame:
    """One row per (season, round, fixture) carrying
    `referee_trailing_cards_per_match_{w}` and `match_id`/`official_name`
    for audit — the join this task's brief specified verbatim (module
    docstring): `vaastav_player_gameweek_stats` `was_home=True` rows'
    `opponent_team` resolved to an FPL team `code` via `vaastav_team_
    identity`, joined to `pl_match_fixtures` on (kickoff date, away_team_
    code) to get `match_id`, joined to `pl_match_officials` on `match_id`
    where `is_referee`.

    **NOT DEPLOYABLE.** This function reads `pl_match_officials`/`pl_match_
    fixtures` WITHOUT the `as_of` gate those datasets' own bitemporal
    convention would apply for a real prediction — deliberately, because
    this feature is used ONLY inside `measure_referee_value_NOT_DEPLOYABLE`
    (a retrospective research measurement, never a real forecast), and
    gating it would only add complexity without changing what is being
    measured (module docstring). `fit_cards_model`/`predict_cards_pmf`
    never call this function.
    """
    if as_of.tzinfo is None:
        raise CardsModelError(f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2.")

    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise CardsModelError(f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()}")

    # Decision 2: attribution_complete=False rows never train (degraded DGW rows)
    raw = raw.filter((pl.col("attribution_complete") == True) | pl.col("attribution_complete").is_null())

    raw = _normalise_and_filter_positions(raw).filter(pl.col("team").is_not_null())

    # Total real cards shown in each fixture, from EVERY player row (both
    # teams, played or not) — never just the was_home half.
    match_cards = raw.group_by(["season", "fixture"]).agg(
        (pl.col("yellow_cards") + pl.col("red_cards")).sum().alias("match_total_cards")
    )

    was_home = raw.filter(pl.col("was_home")).select(["season", "round", "fixture", "opponent_team", "kickoff_time"]).unique()
    if was_home.is_empty():
        raise CardsModelError("no was_home=True rows -- cannot resolve any fixture to a match_id")
    was_home = was_home.with_columns(
        pl.col("kickoff_time")
        .str.strptime(pl.Datetime, "%Y-%m-%dT%H:%M:%SZ")
        .dt.replace_time_zone("UTC")
        .alias("kickoff_dt")
    ).with_columns(pl.col("kickoff_dt").dt.date().alias("kickoff_date"))

    team_identity = store.as_of("vaastav_team_identity", as_of).select(["season", "id", "code"])
    was_home = was_home.join(
        team_identity, left_on=["season", "opponent_team"], right_on=["season", "id"], how="left"
    ).rename({"code": "away_team_code"})

    fixtures = store.as_of("pl_match_fixtures", as_of).select(["match_id", "kickoff", "away_team_code"]).with_columns(
        pl.col("kickoff").dt.date().alias("fx_date")
    )
    resolved = was_home.join(
        fixtures, left_on=["kickoff_date", "away_team_code"], right_on=["fx_date", "away_team_code"], how="left"
    )

    officials = store.as_of("pl_match_officials", as_of).filter(pl.col("is_referee")).select(
        ["match_id", "official_name"]
    )
    resolved = resolved.join(officials, on="match_id", how="left")
    resolved = resolved.join(match_cards, on=["season", "fixture"], how="left")

    resolved = resolved.filter(pl.col("official_name").is_not_null())
    if resolved.is_empty():
        raise CardsModelError("no fixture resolved to a real referee -- nothing to build a trailing feature from")

    resolved = resolved.sort("kickoff_dt")
    w = config.referee_trailing_window
    overall_mean = float(resolved["match_total_cards"].mean())
    resolved = resolved.with_columns(
        pl.col("match_total_cards")
        .cast(pl.Float64)
        .shift(1)
        .rolling_mean(window_size=w, min_samples=1)
        .over("official_name")
        .alias(f"referee_trailing_cards_per_match_{w}")
    )
    resolved = resolved.with_columns(pl.col(f"referee_trailing_cards_per_match_{w}").fill_null(overall_mean))

    return resolved.select(["season", "round", "fixture", "match_id", "official_name", f"referee_trailing_cards_per_match_{w}"])


def attach_referee_feature_NOT_DEPLOYABLE(
    table: pl.DataFrame, referee_table: pl.DataFrame, *, config: CardsModelConfig = CardsModelConfig()
) -> pl.DataFrame:
    """Left-joins `build_referee_trailing_feature_table_NOT_DEPLOYABLE`'s
    output onto a `build_training_table` output, broadcasting the same
    referee-trailing value to every player row of that fixture, then drops
    any row whose fixture did not resolve to a real referee (module
    docstring, "Referee: a real feature" — measured on the population that
    genuinely has the feature, never a silently-imputed one). Returns the
    joined table restricted to resolved rows; the caller is expected to run
    BOTH the deployable and referee-inclusive walk-forward validation over
    this SAME restricted population, so the measured delta is not
    confounded by the ~0.6% of fixtures (module docstring's own referee-join
    verification) that never had a resolvable referee at all."""
    col = f"referee_trailing_cards_per_match_{config.referee_trailing_window}"
    if col not in referee_table.columns:
        raise CardsModelError(f"referee_table is missing {col!r} — did it come from build_referee_trailing_feature_table_NOT_DEPLOYABLE?")
    joined = table.join(referee_table.select(["season", "round", "fixture", col]), on=["season", "round", "fixture"], how="left")
    return joined.filter(pl.col(col).is_not_null())


# ---------------------------------------------------------------------------
# Feature spec + design matrix — standardised (module docstring, "Multinomial
# logit, closed-form gradient"). `numeric_columns` is a FIELD, not a fixed
# constant, so the same spec/design-matrix machinery serves both the
# deployable and the (research-only) referee-inclusive feature sets.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardsFeatureSpec:
    numeric_columns: tuple[str, ...]
    position_categories: tuple[str, ...]
    numeric_means: tuple[float, ...]
    numeric_stds: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.numeric_means) != len(self.numeric_columns) or len(self.numeric_stds) != len(self.numeric_columns):
            raise CardsModelError("numeric_means/numeric_stds must have exactly one entry per numeric_columns entry")

    @property
    def feature_names(self) -> tuple[str, ...]:
        return ("intercept",) + self.numeric_columns + tuple(f"position={p}" for p in self.position_categories)

    @property
    def n_features(self) -> int:
        return len(self.feature_names)


def _build_feature_spec(table: pl.DataFrame, numeric_columns: tuple[str, ...]) -> CardsFeatureSpec:
    positions = tuple(sorted(table["position"].unique().to_list()))
    unexpected = set(positions) - set(POSITIONS)
    if unexpected:
        raise CardsModelError(f"unexpected position categories in training table: {unexpected}")
    means: list[float] = []
    stds: list[float] = []
    for c in numeric_columns:
        series = table[c].cast(pl.Float64)
        mean = float(series.mean()) if table.height > 0 else 0.0
        std = float(series.std(ddof=0)) if table.height > 0 else 0.0
        means.append(mean)
        stds.append(std if std > 1e-8 else 1.0)
    return CardsFeatureSpec(
        numeric_columns=numeric_columns,
        position_categories=positions,
        numeric_means=tuple(means),
        numeric_stds=tuple(stds),
    )


def _design_matrix(table: pl.DataFrame, spec: CardsFeatureSpec) -> np.ndarray:
    n = table.height
    cols: list[np.ndarray] = [np.ones(n, dtype=np.float64)]
    for c, mean, std in zip(spec.numeric_columns, spec.numeric_means, spec.numeric_stds):
        raw = table[c].cast(pl.Float64).to_numpy()
        cols.append((raw - mean) / std)
    positions = table["position"].to_list()
    for p in spec.position_categories:
        cols.append(np.array([1.0 if v == p else 0.0 for v in positions], dtype=np.float64))
    return np.column_stack(cols)


def _feature_row_to_vector(feature_row: dict, spec: CardsFeatureSpec) -> np.ndarray:
    """Reads ONLY the columns named in `spec.numeric_columns` — module
    docstring, "Structural guarantee, not just a naming convention": any
    OTHER key present in `feature_row` (e.g. a smuggled referee feature) is
    silently ignored because there is no coefficient in `spec`/`beta` that
    could ever multiply it."""
    missing = [c for c in spec.numeric_columns if c not in feature_row]
    if missing:
        raise CardsModelError(f"feature_row is missing required feature(s) {missing}")
    if "position" not in feature_row:
        raise CardsModelError("feature_row must carry 'position'")
    row_table = pl.DataFrame(
        {**{c: [feature_row[c]] for c in spec.numeric_columns}, "position": [feature_row["position"]]}
    )
    return _design_matrix(row_table, spec)[0]


# ---------------------------------------------------------------------------
# Multinomial logit — deterministic L-BFGS-B, analytic gradient (CLAUDE.md
# rule 7). params_flat = concat(beta_yellow, beta_red), each length
# n_features. Reference class 0 (NONE) is implicit (eta_0 == 0).
# ---------------------------------------------------------------------------


def _multinomial_neg_log_lik_and_grad(
    params_flat: np.ndarray, X: np.ndarray, y_class: np.ndarray, offset: np.ndarray, n_features: int, l2: float
) -> tuple[float, np.ndarray]:
    """`eta_k = X @ beta_k + offset` for `k in {1, 2}` (YELLOW, RED vs.
    NONE); `eta_0 = 0` fixed (module docstring, "Multinomial logit"). Stable
    softmax via subtracting `max(0, eta_1, eta_2)` before exponentiating.

    Derivation (also checked numerically, `tests/test_cards.py::
    test_multinomial_neg_log_lik_gradient_matches_finite_differences`):
    the standard multinomial-logit score, `dNLL/d(eta_k) = p_k - 1{y=k}`
    for `k in {1, 2}`, so `d(beta_k)`'s gradient is
    `X.T @ (p_k - 1{y=k}) / n + 2*(l2/n)*beta_k`.

    **L2 scaling fixed session `s005`** — this module's own copy of the same
    defect `fplai.models.saves`/`fplai.models.minutes`/`fplai.models.
    defensive_contribution`/`fplai.models.attacking`/`fplai.models.bonus`
    each independently found and fixed this session (`docs/wiki/
    model-minutes.md` §13 has the fullest derivation): the ridge term was
    `l2 * (sum(beta1**2) + sum(beta2**2))`, unscaled against the
    per-row-AVERAGED likelihood `-sum(ll)/n` — effectively `l2*n` for `n` in
    the thousands. Now scaled by the same `1/n` the likelihood already
    carries, in both the loss and the gradient, for BOTH beta vectors.
    **This module's own `l2_penalty` default (`0.001`) was chosen
    specifically to compensate for this exact bug** — `CardsModelConfig.
    l2_penalty`'s own docstring documents a sweep under the OLD unscaled
    formula where every `l2 >= 0.005` FAILED the §7.1 gate outright — so the
    corrected formula changes what this default effectively means, and was
    re-measured against the real store, RAW and CALIBRATED separately
    (module docstring, "Nested out-of-sample NONE/YELLOW calibration"),
    before either the default or the calibrator's continued necessity was
    judged. See `docs/wiki/model-cards.md` for the full before/after and
    swept-grid tables."""
    beta1 = params_flat[:n_features]
    beta2 = params_flat[n_features : 2 * n_features]
    n = X.shape[0]

    eta1 = X @ beta1 + offset
    eta2 = X @ beta2 + offset
    m = np.maximum(0.0, np.maximum(eta1, eta2))
    log_denom = m + np.log(np.exp(-m) + np.exp(eta1 - m) + np.exp(eta2 - m))

    log_p0 = -log_denom
    log_p1 = eta1 - log_denom
    log_p2 = eta2 - log_denom

    is0 = (y_class == 0).astype(np.float64)
    is1 = (y_class == 1).astype(np.float64)
    is2 = (y_class == 2).astype(np.float64)

    ll = is0 * log_p0 + is1 * log_p1 + is2 * log_p2
    nll = -float(np.sum(ll)) / n + (l2 / n) * (float(np.sum(beta1 * beta1)) + float(np.sum(beta2 * beta2)))

    p1 = np.exp(log_p1)
    p2 = np.exp(log_p2)
    grad_beta1 = (X.T @ (p1 - is1)) / n + 2.0 * (l2 / n) * beta1
    grad_beta2 = (X.T @ (p2 - is2)) / n + 2.0 * (l2 / n) * beta2

    return nll, np.concatenate([grad_beta1, grad_beta2])


def _fit_multinomial(
    X: np.ndarray, y_class: np.ndarray, offset: np.ndarray, n_features: int, config: CardsModelConfig
) -> np.ndarray:
    from scipy.optimize import minimize

    x0 = np.zeros(2 * n_features, dtype=np.float64)
    result = minimize(
        _multinomial_neg_log_lik_and_grad,
        x0,
        args=(X, y_class, offset, n_features, config.l2_penalty),
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": config.max_lbfgs_iterations},
    )
    if not result.success and result.status not in (0, 1):
        logger.warning("cards model: multinomial fit did not converge cleanly: %s", result.message)
    return result.x


def _class_probabilities(x: np.ndarray, beta1: np.ndarray, beta2: np.ndarray, offset: float) -> tuple[float, float, float]:
    eta1 = float(x @ beta1) + offset
    eta2 = float(x @ beta2) + offset
    m = max(0.0, eta1, eta2)
    denom = math.exp(-m) + math.exp(eta1 - m) + math.exp(eta2 - m)
    p0 = math.exp(-m) / denom
    p1 = math.exp(eta1 - m) / denom
    p2 = math.exp(eta2 - m) / denom
    return p0, p1, p2


def _class_probabilities_batch(
    X: np.ndarray, beta1: np.ndarray, beta2: np.ndarray, offset: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorised form of `_class_probabilities` for a whole design matrix
    at once -- same computation `_multinomial_neg_log_lik_and_grad` already
    does internally, factored out so a calibration-holdout batch (thousands
    of rows, once per walk-forward fold) is not scored via a Python-level
    per-row loop. Mirrors `fplai.models.minutes._softmax_predict_batch`."""
    eta1 = X @ beta1 + offset
    eta2 = X @ beta2 + offset
    m = np.maximum(0.0, np.maximum(eta1, eta2))
    denom = np.exp(-m) + np.exp(eta1 - m) + np.exp(eta2 - m)
    p0 = np.exp(-m) / denom
    p1 = np.exp(eta1 - m) / denom
    p2 = np.exp(eta2 - m) / denom
    return p0, p1, p2


# ---------------------------------------------------------------------------
# Nested out-of-sample NONE/YELLOW calibration -- session s005, module
# docstring "Nested out-of-sample NONE/YELLOW calibration". `IsotonicCalibrator`/
# `_fit_isotonic_calibrator` moved to the shared `fplai.calibration` module
# session s005 (consolidation) -- this was one of THREE independently-
# drifting copies (`fplai.models.minutes`/`saves` each carried their own
# too), and the Jeffreys-smoothing fix below had to be found and applied
# three separate times before anyone noticed the shared root cause.
# `fplai.calibration` is not a sibling MODEL (it fits nothing, predicts
# nothing, touches no store) so importing it is not the cross-model coupling
# this module's own "no cross-model import" convention forbids, same as the
# `evaluate_binary_outcome` import above. `_inner_calibration_split` below
# stays HERE, duplicated across all three models still -- the out-of-sample
# NESTING (walk-forward fold boundaries) is each model's own responsibility,
# not the shared fitting primitive's.
# ---------------------------------------------------------------------------


def _inner_calibration_split(
    train: pl.DataFrame, *, holdout_frac: float, min_holdout_rows: int, min_inner_train_rows: int
) -> tuple[pl.DataFrame, pl.DataFrame] | None:
    """Split a fold's own `train` slice (already strictly before the eval
    fold) into `inner_train`/`calib_holdout` by DISTINCT `_chronological_
    rank`, never by row count or a random split (module docstring's "Nested
    out-of-sample NONE/YELLOW calibration" -- identical contract to
    `fplai.models.minutes._inner_calibration_split`, duplicated verbatim;
    this function is genuinely generic over any table carrying
    `_chronological_rank`, which `build_training_table` gives this module
    too). Returns `None` when there is not enough data for a meaningful
    inner fit+holdout -- the caller falls back to the raw prediction rather
    than fabricating a calibrator from too little evidence."""
    ranks = train["_chronological_rank"].unique().sort().to_list()
    if len(ranks) < 2:
        return None
    n_holdout_ranks = max(1, round(len(ranks) * holdout_frac))
    n_holdout_ranks = min(n_holdout_ranks, len(ranks) - 1)
    cutoff_rank = ranks[len(ranks) - n_holdout_ranks]
    inner_train = train.filter(pl.col("_chronological_rank") < cutoff_rank)
    calib_holdout = train.filter(pl.col("_chronological_rank") >= cutoff_rank)
    if inner_train.height < min_inner_train_rows or calib_holdout.height < min_holdout_rows:
        return None
    return inner_train, calib_holdout


def _apply_cards_calibration(
    p0: float, p1: float, p2: float, none_calibrator: IsotonicCalibrator, yellow_calibrator: IsotonicCalibrator
) -> tuple[float, float, float]:
    """Module docstring, "Renormalisation -- the real design question": RED
    (`p2`) is held COMPLETELY FIXED at its raw value -- this module never
    learns anything about RED's calibration (RED is base-rate-only, module
    docstring "What is verified"), so it never touches it. `p0`/`p1` are
    each independently calibrated (one-vs-rest), clipped to `[0,1]`, then
    rescaled PROPORTIONALLY so `p0_final + p1_final + p2 == 1.0` exactly --
    preserving the ratio the two calibrators produced, spending the
    renormalisation entirely on their shared scale. A degenerate holdout
    where both calibrated values round to ~0 (both classes calibrated to
    "never happens") splits the remaining budget evenly rather than
    dividing by ~0 -- the same honest, stated fallback `predict_minutes_pmf`
    uses for its own degenerate corner."""
    cal0 = float(none_calibrator.apply(np.array([p0]))[0])
    cal1 = float(yellow_calibrator.apply(np.array([p1]))[0])
    cal0 = min(max(cal0, 0.0), 1.0)
    cal1 = min(max(cal1, 0.0), 1.0)
    remaining_budget = 1.0 - p2
    denom = cal0 + cal1
    if denom > 1e-9:
        scale = remaining_budget / denom
        p0_final = cal0 * scale
        p1_final = cal1 * scale
    else:
        p0_final = remaining_budget / 2.0
        p1_final = remaining_budget / 2.0
    return p0_final, p1_final, p2


# ---------------------------------------------------------------------------
# Fitted params
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class CardsModelParams:
    """`eq=False`: carries numpy arrays, same convention every sibling
    module's own fitted-params dataclass establishes. Produced ONLY by
    `fit_cards_model`/`_fit_from_table`, which always fit
    `NUMERIC_FEATURE_COLUMNS_CARDS` (module docstring, "Structural
    guarantee") — there is no code path that produces one of these carrying
    a referee coefficient.

    `p_none_calibrator`/`p_yellow_calibrator`: `None` unless `fit_cards_
    model(..., calibrate=True)` fitted them (module docstring, "Nested
    out-of-sample NONE/YELLOW calibration"). Always both `None` or both
    set together (`__post_init__` enforces this) -- this module never fits
    one without the other, since `_apply_cards_calibration` always needs
    both to renormalise. Applied at `predict_cards_pmf` time; never RED
    (module docstring, "Scope: NONE and YELLOW only, never RED")."""

    beta_yellow: np.ndarray
    beta_red: np.ndarray
    feature_spec: CardsFeatureSpec
    config: CardsModelConfig
    as_of: datetime
    seasons_used: tuple[str, ...]
    n_rows_used: int
    p_none_calibrator: IsotonicCalibrator | None = None
    p_yellow_calibrator: IsotonicCalibrator | None = None

    def __post_init__(self) -> None:
        has_none = self.p_none_calibrator is not None
        has_yellow = self.p_yellow_calibrator is not None
        if has_none != has_yellow:
            raise CardsModelError(
                "p_none_calibrator and p_yellow_calibrator must be set together or not at all "
                f"(got p_none_calibrator {'set' if has_none else 'None'}, "
                f"p_yellow_calibrator {'set' if has_yellow else 'None'})"
            )


def _fit_from_table(
    table: pl.DataFrame,
    *,
    config: CardsModelConfig,
    as_of: datetime,
    feature_columns: tuple[str, ...] = NUMERIC_FEATURE_COLUMNS_CARDS,
) -> CardsModelParams:
    if table.is_empty():
        raise CardsModelError("no rows in this training table -- nothing to fit")

    spec = _build_feature_spec(table, feature_columns)
    X = _design_matrix(table, spec)
    y_class = table["outcome"].cast(pl.Int64).to_numpy()
    offset = np.log(table["minutes"].cast(pl.Float64).to_numpy() / 90.0)
    n_features = spec.n_features
    params_flat = _fit_multinomial(X, y_class, offset, n_features, config)

    return CardsModelParams(
        beta_yellow=params_flat[:n_features],
        beta_red=params_flat[n_features : 2 * n_features],
        feature_spec=spec,
        config=config,
        as_of=as_of,
        seasons_used=tuple(sorted(table["season"].unique().to_list())),
        n_rows_used=table.height,
    )


def fit_cards_model(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: CardsModelConfig = CardsModelConfig(),
    calibrate: bool = True,
    calibration_holdout_frac: float = 0.2,
    min_calibration_holdout_rows: int = 200,
    min_inner_train_rows: int = 200,
) -> CardsModelParams:
    """The ONLY production fitting entry point. ALWAYS fits `NUMERIC_
    FEATURE_COLUMNS_CARDS` (module docstring, "Structural guarantee") — no
    parameter here can select the referee-inclusive feature set.

    `calibrate=True` (default, session s005 -- module docstring, "Nested
    out-of-sample NONE/YELLOW calibration") fits a nested, out-of-sample
    isotonic calibrator for NONE and YELLOW on a chronologically-EARLIER
    slice of this same `as_of` training window (`_inner_calibration_
    split`): a fresh model is fit on `inner_train`, scored out-of-sample on
    `calib_holdout`, and the two calibrators are learned from THOSE
    (prediction, outcome) pairs -- never from the live predictions this
    returned model will later be asked to make. If the training window is
    too small for a meaningful inner split, returns the RAW (uncalibrated)
    model with a logged warning rather than fabricating a calibrator from
    too little evidence -- `p_none_calibrator`/`p_yellow_calibrator` stay
    `None`, and every PMF this model predicts reads
    `calibration_method="raw_uncalibrated"`. Pass `calibrate=False` to opt
    back out entirely (bit-identical to this module's pre-s005 behaviour)."""
    table = build_training_table(store, as_of=as_of, seasons=seasons, config=config)
    params = _fit_from_table(table, config=config, as_of=as_of, feature_columns=NUMERIC_FEATURE_COLUMNS_CARDS)
    if not calibrate:
        return params

    split = _inner_calibration_split(
        table,
        holdout_frac=calibration_holdout_frac,
        min_holdout_rows=min_calibration_holdout_rows,
        min_inner_train_rows=min_inner_train_rows,
    )
    if split is None:
        logger.warning(
            "fit_cards_model(calibrate=True): training window too small for a meaningful inner "
            "calibration split (need >= %d inner-train rows and >= %d holdout rows) -- shipping "
            "the raw (uncalibrated) model; every predicted PMF will read "
            "calibration_method='raw_uncalibrated'.",
            min_inner_train_rows,
            min_calibration_holdout_rows,
        )
        return params

    inner_train, calib_holdout = split
    inner_params = _fit_from_table(inner_train, config=config, as_of=as_of, feature_columns=NUMERIC_FEATURE_COLUMNS_CARDS)
    X_holdout = _design_matrix(calib_holdout, inner_params.feature_spec)
    offset_holdout = np.log(calib_holdout["minutes"].cast(pl.Float64).to_numpy() / 90.0)
    p0_holdout, p1_holdout, _p2_holdout = _class_probabilities_batch(
        X_holdout, inner_params.beta_yellow, inner_params.beta_red, offset_holdout
    )
    y_holdout = calib_holdout["outcome"].cast(pl.Int64).to_numpy()
    none_calibrator = _fit_isotonic_calibrator(p0_holdout, (y_holdout == 0).astype(np.float64))
    yellow_calibrator = _fit_isotonic_calibrator(p1_holdout, (y_holdout == 1).astype(np.float64))
    return replace(params, p_none_calibrator=none_calibrator, p_yellow_calibrator=yellow_calibrator)


# ---------------------------------------------------------------------------
# PMF + prediction (module docstring, "Cards are per-player, not
# fixture-coupled").
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardsPMF:
    """The model's actual output for one player-fixture: a full PMF over
    `{0, 1, 2}` (NONE/YELLOW/RED) — never a scalar (CLAUDE.md rule 5).

    `calibration_method` is STRUCTURAL provenance, not a docstring promise
    -- same precedent `fplai.models.minutes.MinutesPMF.calibration_method`
    sets (session s005, module docstring "Nested out-of-sample NONE/YELLOW
    calibration"): a declared field, present on every PMF, carried through
    `to_polars()` into the persisted derived row, so a consumer reading a
    persisted `player.cards_distribution@gameweek` row can tell which
    mapping produced its `probability` without reading this module's
    source. `"raw_uncalibrated"` (the default) means every outcome is the
    multinomial-logit output directly; `"isotonic_v1"` means NONE/YELLOW
    were passed through `CardsModelParams.p_none_calibrator`/`p_yellow_
    calibrator` and renormalised (`_apply_cards_calibration`) -- RED is
    NEVER calibrated (module docstring, "Scope: NONE and YELLOW only")."""

    element: int
    fixture: int
    outcomes: tuple[int, ...]
    probabilities: tuple[float, ...]
    calibration_method: str = "raw_uncalibrated"

    def __post_init__(self) -> None:
        if self.outcomes != OUTCOMES:
            raise CardsModelError(f"CardsPMF.outcomes must be exactly {OUTCOMES}, got {self.outcomes}")
        if len(self.outcomes) != len(self.probabilities):
            raise CardsModelError("outcomes and probabilities must be the same length")
        total = sum(self.probabilities)
        if abs(total - 1.0) > 1e-6:
            raise CardsModelError(f"CardsPMF probabilities do not sum to 1.0 (got {total}) for element={self.element}")

    def p_carded(self) -> float:
        return self.probabilities[1] + self.probabilities[2]

    def p_red(self) -> float:
        return self.probabilities[2]

    def expected_card_points(self, *, yellow_points: int, red_points: int) -> float:
        """`yellow_points`/`red_points` must be LIVE-read (`read_card_
        points`), never hardcoded (CLAUDE.md rule 4)."""
        return self.probabilities[1] * yellow_points + self.probabilities[2] * red_points

    def to_polars(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "element": [self.element] * len(self.outcomes),
                "fixture": [self.fixture] * len(self.outcomes),
                "outcome": list(self.outcomes),
                "outcome_label": [OUTCOME_LABELS[o] for o in self.outcomes],
                "probability": list(self.probabilities),
                "calibration_method": [self.calibration_method] * len(self.outcomes),
            }
        )


def predict_cards_pmf(
    params: CardsModelParams,
    feature_row: dict,
    *,
    element: int,
    fixture: int,
    minute_exposure: Sequence[tuple[float, float]],
) -> CardsPMF:
    """`feature_row` must carry every column in `params.feature_spec.
    numeric_columns` plus `position` (never `minutes` itself — supplied via
    `minute_exposure`, module docstring "Minutes as an EXPOSURE OFFSET").
    `minute_exposure` is a `[(minutes_value, probability), ...]` mixture,
    e.g. a fitted `MinutesPMF`'s six band midpoints — must sum to 1.0.

    A `minute_exposure` entry of `(0.0, w)` contributes a spike at NONE
    (outcome 0) rather than `log(0)` — a player who did not play cannot be
    booked (module docstring). This spike bypasses calibration entirely
    (module docstring, "Nested out-of-sample NONE/YELLOW calibration") --
    it is a structural fact (0 minutes -> cannot be booked), never a model
    output, so there is nothing to calibrate.

    If `params.p_none_calibrator`/`p_yellow_calibrator` are set (`fit_cards_
    model(..., calibrate=True)`), each exposure component's raw `(p0, p1,
    p2)` (one per DISTINCT minutes value, since each carries its own
    `offset`) is individually calibrated via `_apply_cards_calibration`
    BEFORE being weighted into the mixture -- never the post-mixture
    aggregate, which would not correspond to any single (raw_p, outcome)
    pair either calibrator was ever fitted on."""
    total_weight = sum(w for _, w in minute_exposure)
    if abs(total_weight - 1.0) > 1e-6:
        raise CardsModelError(f"minute_exposure probabilities must sum to 1.0, got {total_weight}")

    spec = params.feature_spec
    x = _feature_row_to_vector(feature_row, spec)
    calibrated = params.p_none_calibrator is not None and params.p_yellow_calibrator is not None

    mixture = np.zeros(3, dtype=np.float64)
    for minutes_value, weight in minute_exposure:
        if minutes_value <= 0:
            mixture[0] += weight
            continue
        offset = math.log(min(max(float(minutes_value), 0.0), 90.0) / 90.0)
        p0, p1, p2 = _class_probabilities(x, params.beta_yellow, params.beta_red, offset)
        if calibrated:
            p0, p1, p2 = _apply_cards_calibration(p0, p1, p2, params.p_none_calibrator, params.p_yellow_calibrator)
        mixture += weight * np.array([p0, p1, p2])

    total = float(mixture.sum())
    if total <= 0.0:
        raise CardsModelError(f"predicted cards PMF has zero mass for element={element}, fixture={fixture}")
    mixture = mixture / total

    calibration_method = "isotonic_v1" if calibrated else "raw_uncalibrated"
    return CardsPMF(
        element=element,
        fixture=fixture,
        outcomes=OUTCOMES,
        probabilities=tuple(float(v) for v in mixture),
        calibration_method=calibration_method,
    )


# ---------------------------------------------------------------------------
# game_config live points (CLAUDE.md rule 4 — never hardcoded).
# ---------------------------------------------------------------------------


def read_card_points(store: BitemporalStore) -> tuple[int, int]:
    """LIVE-read `scoring.yellow_cards`/`scoring.red_cards` from
    `game_config` — `(-1, -3)` verified live this session, but never
    hardcoded here; a rule change would be picked up automatically."""
    df = store.latest("game_config")
    if df.is_empty():
        raise CardsModelError("game_config is empty in this store — cannot read card points live.")
    payload = json.loads(df["payload"][0])
    scoring = payload.get("scoring")
    if not isinstance(scoring, dict) or "yellow_cards" not in scoring or "red_cards" not in scoring:
        raise CardsModelError(
            "game_config payload has no scoring.yellow_cards/scoring.red_cards — "
            f"scoring keys: {sorted(scoring.keys()) if isinstance(scoring, dict) else scoring!r}"
        )
    return int(scoring["yellow_cards"]), int(scoring["red_cards"])


# ---------------------------------------------------------------------------
# Per-outcome reliability -- session s005: now DELEGATES to `fplai.
# calibration.evaluate_binary_outcome` (module docstring, "Reliability —
# required per-outcome") rather than a fifth local duplicate of log-loss/
# Brier/ECE/reliability-diagram/calibration-slope math. `CalibrationMetrics`
# is imported at module top, not redefined here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Multiclass gate metrics (module docstring, "Walk-forward gate").
# ---------------------------------------------------------------------------


def _multiclass_log_loss(y_true_class: Sequence[int], p_matrix: Sequence[Sequence[float]], eps: float = 1e-12) -> float:
    n = len(y_true_class)
    if n == 0:
        raise CardsModelError("multiclass log_loss over zero rows is undefined")
    total = 0.0
    for y, row in zip(y_true_class, p_matrix):
        p = min(max(row[y], eps), 1.0 - eps)
        total += -math.log(p)
    return total / n


def _multiclass_brier(y_true_class: Sequence[int], p_matrix: Sequence[Sequence[float]]) -> float:
    n = len(y_true_class)
    if n == 0:
        raise CardsModelError("multiclass brier over zero rows is undefined")
    total = 0.0
    for y, row in zip(y_true_class, p_matrix):
        for k, p in enumerate(row):
            onehot = 1.0 if k == y else 0.0
            total += (p - onehot) ** 2
    return total / n


@dataclass(frozen=True)
class CardsWalkForwardResult:
    """Session s005 addition, module docstring "Nested out-of-sample NONE/
    YELLOW calibration": `p_model` is the SHIPPED series (calibrated when
    this result was built with `calibrate=True`, the default -- identical
    to the raw model when `calibrate=False`); `p_model_raw` is ALWAYS the
    raw per-fold prediction regardless of `calibrate`, so a single walk-
    forward run gives an honest before/after comparison without refitting
    twice. `beats_both_baselines()`/`reliability_for_outcome()` read
    `p_model` -- i.e. they score whatever this result was actually built to
    represent; `reliability_for_outcome_raw()` reads `p_model_raw`
    explicitly for the raw comparison."""

    n_folds: int
    y_true_class: tuple[int, ...]
    p_model: tuple[tuple[float, ...], ...]
    p_model_raw: tuple[tuple[float, ...], ...]
    p_baseline_group_rate: tuple[tuple[float, ...], ...]
    p_baseline_player_trailing: tuple[tuple[float, ...], ...]
    feature_columns: tuple[str, ...]
    calibrated: bool = False
    n_folds_calibrated: int = 0

    def model_log_loss(self) -> float:
        return _multiclass_log_loss(self.y_true_class, self.p_model)

    def model_brier(self) -> float:
        return _multiclass_brier(self.y_true_class, self.p_model)

    def model_log_loss_raw(self) -> float:
        return _multiclass_log_loss(self.y_true_class, self.p_model_raw)

    def model_brier_raw(self) -> float:
        return _multiclass_brier(self.y_true_class, self.p_model_raw)

    def baseline_group_rate_log_loss(self) -> float:
        return _multiclass_log_loss(self.y_true_class, self.p_baseline_group_rate)

    def baseline_group_rate_brier(self) -> float:
        return _multiclass_brier(self.y_true_class, self.p_baseline_group_rate)

    def baseline_player_trailing_log_loss(self) -> float:
        return _multiclass_log_loss(self.y_true_class, self.p_baseline_player_trailing)

    def baseline_player_trailing_brier(self) -> float:
        return _multiclass_brier(self.y_true_class, self.p_baseline_player_trailing)

    def beats_both_baselines(self) -> bool:
        """The gate, §7.1: strictly lower multiclass log-loss AND
        multiclass Brier than BOTH baselines, scored on `p_model` -- the
        shipped series."""
        return (
            self.model_log_loss() < self.baseline_group_rate_log_loss()
            and self.model_log_loss() < self.baseline_player_trailing_log_loss()
            and self.model_brier() < self.baseline_group_rate_brier()
            and self.model_brier() < self.baseline_player_trailing_brier()
        )

    def calibrated_not_worse_than_raw(self) -> bool:
        """This task's brief, explicit gate condition: the calibrated model
        must not be worse than the raw one on ANY pooled metric. `<=` (a
        tie is not a failure -- a fold whose inner split fell back to raw,
        module docstring, makes `p_model` partially identical to `p_model_
        raw` already; a tiny float epsilon absorbs rounding, not a real
        regression). Meaningless (and not asserted by any test) when this
        result was built with `calibrate=False`, since then `p_model IS
        p_model_raw` by construction."""
        eps = 1e-12
        return (
            self.model_log_loss() <= self.model_log_loss_raw() + eps
            and self.model_brier() <= self.model_brier_raw() + eps
        )

    def reliability_for_outcome(self, k: int) -> CalibrationMetrics:
        if k not in OUTCOMES:
            raise CardsModelError(f"k must be one of {OUTCOMES}, got {k}")
        y_bin = [1 if y == k else 0 for y in self.y_true_class]
        p_bin = [row[k] for row in self.p_model]
        return evaluate_binary_outcome(y_bin, p_bin)

    def reliability_for_outcome_raw(self, k: int) -> CalibrationMetrics:
        if k not in OUTCOMES:
            raise CardsModelError(f"k must be one of {OUTCOMES}, got {k}")
        y_bin = [1 if y == k else 0 for y in self.y_true_class]
        p_bin = [row[k] for row in self.p_model_raw]
        return evaluate_binary_outcome(y_bin, p_bin)


def walk_forward_validate(
    table: pl.DataFrame,
    *,
    feature_columns: tuple[str, ...] = NUMERIC_FEATURE_COLUMNS_CARDS,
    min_train_rows: int = 2000,
    config: CardsModelConfig = CardsModelConfig(),
    calibrate: bool = True,
    calibration_holdout_frac: float = 0.2,
    min_calibration_holdout_rows: int = 200,
    min_inner_train_rows: int = 200,
) -> CardsWalkForwardResult:
    """Refits (deterministic L-BFGS-B) at every `(season, round)` fold
    using only strictly-earlier rows. `feature_columns` is a real parameter
    — module docstring, "Referee: a real feature, deliberately not
    deployed": `measure_referee_value_NOT_DEPLOYABLE` calls this TWICE with
    two different `feature_columns`, over the SAME table, for a fair
    apples-to-apples comparison. `fit_cards_model` never sets this
    parameter to anything other than its default.

    `calibrate=True` (DEFAULT -- module docstring, "Nested out-of-sample
    NONE/YELLOW calibration"). This default is DELIBERATELY DIFFERENT from
    `fplai.models.minutes.walk_forward_validate`'s own `calibrate=False`
    default: that function's `calibrate` flag exists purely as an optional
    gate-diagnostic (minutes' PRODUCTION calibration lives entirely in
    `fit_minutes_model`, a separate function, and `p_model` there is always
    raw regardless of the flag). This module's re-gate (`scripts/
    calibration_report.py`'s `run_cards`, READ-ONLY for this task) calls
    this function with its bare defaults and reads `result.p_model`
    directly -- for the regenerated report to reflect the fix this task
    exists to ship, `p_model` must BE the calibrated series by default;
    there is no other lever available without editing that script. Pass
    `calibrate=False` to score the raw model only (bit-identical to this
    module's pre-s005 behaviour, `p_model == p_model_raw`).

    Per fold, when `calibrate=True`: `train` (rows strictly before this
    fold, exactly as always) is split by `_inner_calibration_split` into
    `inner_train`/`calib_holdout`; a model is fit on `inner_train` alone
    and scored OUT-OF-SAMPLE on `calib_holdout`, and fresh NONE/YELLOW
    isotonic calibrators are fitted from those pairs. The FULL-`train`
    model (identical to the `calibrate=False` path -- `p_model_raw` is
    unaffected by this flag) then predicts the eval fold as always, and
    THOSE raw predictions are what the fold's own calibrators are applied
    to (`_apply_cards_calibration`) to produce that fold's `p_model`
    entries. A fold too small for a meaningful inner split falls back to
    reporting the raw prediction for `p_model` too (not skipped, not
    crashed) -- `n_folds_calibrated` counts how many folds actually got a
    real calibrator, so this fallback rate is visible, not silent."""
    if "_chronological_rank" not in table.columns:
        raise CardsModelError("table must carry _chronological_rank -- build it via build_training_table")

    fold_keys = table.select(["season", "round", "_chronological_rank"]).unique().sort("_chronological_rank")

    y_true_class: list[int] = []
    p_model: list[tuple[float, ...]] = []
    p_model_raw: list[tuple[float, ...]] = []
    p_baseline_group: list[tuple[float, ...]] = []
    p_baseline_trailing: list[tuple[float, ...]] = []
    n_folds = 0
    n_folds_calibrated = 0

    for season, round_, rank in fold_keys.iter_rows():
        train = table.filter(pl.col("_chronological_rank") < rank)
        eval_rows = table.filter(pl.col("_chronological_rank") == rank)
        if train.height < min_train_rows or eval_rows.is_empty():
            continue

        n_folds += 1
        spec = _build_feature_spec(train, feature_columns)
        X_train = _design_matrix(train, spec)
        y_train = train["outcome"].cast(pl.Int64).to_numpy()
        offset_train = np.log(train["minutes"].cast(pl.Float64).to_numpy() / 90.0)
        n_features = spec.n_features
        params_flat = _fit_multinomial(X_train, y_train, offset_train, n_features, config)
        beta1, beta2 = params_flat[:n_features], params_flat[n_features : 2 * n_features]

        group_rate_table = train.group_by("position").agg(
            *[((pl.col("outcome") == k).sum() / pl.len()).alias(f"rate_{k}") for k in OUTCOMES]
        )
        group_rate_map = {
            row["position"]: tuple(float(row[f"rate_{k}"]) for k in OUTCOMES) for row in group_rate_table.to_dicts()
        }
        overall_rate = tuple(float((train["outcome"] == k).sum()) / train.height for k in OUTCOMES)

        X_eval = _design_matrix(eval_rows, spec)
        offset_eval = np.log(eval_rows["minutes"].cast(pl.Float64).to_numpy() / 90.0)
        eval_outcomes = eval_rows["outcome"].to_list()
        eval_positions = eval_rows["position"].to_list()
        trailing_cols_vals = {k: eval_rows[f"player_trailing_cards_class_rate_{k}_5"].to_list() for k in OUTCOMES}

        fold_raw_triples: list[tuple[float, float, float]] = []
        for i in range(eval_rows.height):
            p0, p1, p2 = _class_probabilities(X_eval[i], beta1, beta2, float(offset_eval[i]))
            fold_raw_triples.append((p0, p1, p2))
            y_true_class.append(int(eval_outcomes[i]))
            p_model_raw.append((p0, p1, p2))

            pos = eval_positions[i]
            fallback = group_rate_map.get(pos, overall_rate)
            p_baseline_group.append(fallback)

            trailing_row = tuple(
                float(trailing_cols_vals[k][i]) if trailing_cols_vals[k][i] is not None else fallback[k] for k in OUTCOMES
            )
            total = sum(trailing_row)
            trailing_row = tuple(v / total for v in trailing_row) if total > 0 else fallback
            p_baseline_trailing.append(trailing_row)

        if not calibrate:
            p_model.extend(fold_raw_triples)
            continue

        split = _inner_calibration_split(
            train,
            holdout_frac=calibration_holdout_frac,
            min_holdout_rows=min_calibration_holdout_rows,
            min_inner_train_rows=min_inner_train_rows,
        )
        if split is None:
            p_model.extend(fold_raw_triples)
            continue

        inner_train, calib_holdout = split
        inner_spec = _build_feature_spec(inner_train, feature_columns)
        X_inner = _design_matrix(inner_train, inner_spec)
        y_inner = inner_train["outcome"].cast(pl.Int64).to_numpy()
        offset_inner = np.log(inner_train["minutes"].cast(pl.Float64).to_numpy() / 90.0)
        inner_n_features = inner_spec.n_features
        inner_params_flat = _fit_multinomial(X_inner, y_inner, offset_inner, inner_n_features, config)
        inner_beta1 = inner_params_flat[:inner_n_features]
        inner_beta2 = inner_params_flat[inner_n_features : 2 * inner_n_features]

        X_holdout = _design_matrix(calib_holdout, inner_spec)
        offset_holdout = np.log(calib_holdout["minutes"].cast(pl.Float64).to_numpy() / 90.0)
        p0_holdout, p1_holdout, _p2_holdout = _class_probabilities_batch(X_holdout, inner_beta1, inner_beta2, offset_holdout)
        y_holdout = calib_holdout["outcome"].cast(pl.Int64).to_numpy()
        none_calibrator = _fit_isotonic_calibrator(p0_holdout, (y_holdout == 0).astype(np.float64))
        yellow_calibrator = _fit_isotonic_calibrator(p1_holdout, (y_holdout == 1).astype(np.float64))

        for p0, p1, p2 in fold_raw_triples:
            p_model.append(_apply_cards_calibration(p0, p1, p2, none_calibrator, yellow_calibrator))
        n_folds_calibrated += 1

    if n_folds == 0:
        raise CardsModelError(f"no usable folds with min_train_rows={min_train_rows}")

    return CardsWalkForwardResult(
        n_folds=n_folds,
        y_true_class=tuple(y_true_class),
        p_model=tuple(p_model),
        p_model_raw=tuple(p_model_raw),
        p_baseline_group_rate=tuple(p_baseline_group),
        p_baseline_player_trailing=tuple(p_baseline_trailing),
        feature_columns=feature_columns,
        calibrated=calibrate,
        n_folds_calibrated=n_folds_calibrated,
    )


# ---------------------------------------------------------------------------
# Referee value measurement — RESEARCH ONLY (module docstring, "Referee: a
# real feature, deliberately not deployed").
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefereeValueComparison:
    """The size-of-the-prize measurement this task's brief asked for.
    `referee_inclusive_variant_NOT_DEPLOYABLE` is fit with a feature that
    uses a fact (referee identity) not available at the FPL deadline — see
    the module docstring. **Never use it for a real prediction.** It exists
    only so the log-loss/Brier/ECE DELTA below can be computed honestly."""

    deployable: CardsWalkForwardResult
    referee_inclusive_variant_NOT_DEPLOYABLE: CardsWalkForwardResult
    n_rows_compared: int

    def log_loss_delta(self) -> float:
        """Deployable minus referee-inclusive — POSITIVE means the referee
        feature would have reduced log-loss (an improvement) if it were
        deployable."""
        return self.deployable.model_log_loss() - self.referee_inclusive_variant_NOT_DEPLOYABLE.model_log_loss()

    def brier_delta(self) -> float:
        return self.deployable.model_brier() - self.referee_inclusive_variant_NOT_DEPLOYABLE.model_brier()


def measure_referee_value_NOT_DEPLOYABLE(
    table_with_referee: pl.DataFrame,
    *,
    min_train_rows: int = 2000,
    config: CardsModelConfig = CardsModelConfig(),
) -> RefereeValueComparison:
    """Fits the SAME multinomial model twice, walk-forward, over the SAME
    table (module docstring): once on `NUMERIC_FEATURE_COLUMNS_CARDS`
    (deployable) and once on `NUMERIC_FEATURE_COLUMNS_CARDS_WITH_REFEREE_
    NOT_DEPLOYABLE`. `table_with_referee` must already carry `referee_
    trailing_cards_per_match_{config.referee_trailing_window}` — build it
    via `build_referee_trailing_feature_table_NOT_DEPLOYABLE` +
    `attach_referee_feature_NOT_DEPLOYABLE`."""
    col = f"referee_trailing_cards_per_match_{config.referee_trailing_window}"
    if col not in table_with_referee.columns:
        raise CardsModelError(f"table_with_referee is missing {col!r} -- attach it first (module docstring)")

    deployable = walk_forward_validate(
        table_with_referee, feature_columns=NUMERIC_FEATURE_COLUMNS_CARDS, min_train_rows=min_train_rows, config=config
    )
    referee_inclusive = walk_forward_validate(
        table_with_referee,
        feature_columns=NUMERIC_FEATURE_COLUMNS_CARDS_WITH_REFEREE_NOT_DEPLOYABLE,
        min_train_rows=min_train_rows,
        config=config,
    )
    if len(deployable.y_true_class) != len(referee_inclusive.y_true_class):
        raise CardsModelError(
            "the two walk-forward runs evaluated a different number of rows "
            f"({len(deployable.y_true_class)} vs {len(referee_inclusive.y_true_class)}) — "
            "the comparison is not apples-to-apples; this should be structurally impossible "
            "since both ran over the exact same table."
        )
    return RefereeValueComparison(
        deployable=deployable,
        referee_inclusive_variant_NOT_DEPLOYABLE=referee_inclusive,
        n_rows_compared=len(deployable.y_true_class),
    )


# ---------------------------------------------------------------------------
# Derived-capability registration + persistence
# ---------------------------------------------------------------------------


def _register_cards_capability() -> FactTableSchema:
    existing = CANONICAL_SCHEMAS.get(PLAYER_CARDS_DISTRIBUTION_GAMEWEEK)
    if existing is not None:
        return existing
    return register_derived_capability(
        PLAYER_CARDS_DISTRIBUTION_GAMEWEEK,
        entity_key=("season", "round", "element", "fixture", "outcome"),
        value_fields=("probability", "outcome_label", "calibration_method"),
        dataset=PLAYER_CARDS_DISTRIBUTION_DATASET,
        description=(
            "Cards/discipline estimator (blueprint §4, E5, session s004; "
            "session s005 added the nested NONE/YELLOW isotonic calibration "
            "layer -- see fplai.models.cards's module docstring, 'Nested "
            "out-of-sample NONE/YELLOW calibration') -- LONG format, one row "
            "per (player, fixture, outcome) with its probability under the "
            "fitted multinomial-logit model. `outcome` in (0, 1, 2) = "
            "(NONE, YELLOW, RED) -- a single categorical partition, never "
            "two independently-modelled binaries (verified mutually "
            "exclusive against the archive -- see fplai.models.cards's "
            "module docstring). Fit WITHOUT referee identity -- it is not "
            "resolvable as of a pre-deadline as_of (match.officials@match "
            "is anchored at kickoff). `calibration_method` in "
            "('raw_uncalibrated', 'isotonic_v1') -- structural provenance, "
            "same convention player.minutes_distribution@gameweek's own "
            "`calibration_method` field establishes; RED is NEVER "
            "calibrated regardless of this value (base-rate-only, slope "
            "carries no usable ranking). Summing `probability` over every "
            "`outcome` for one (season, round, element, fixture) must equal "
            "1.0."
        ),
    )


CARDS_SCHEMA: FactTableSchema = _register_cards_capability()


def pmfs_to_rows(pmfs: Sequence[CardsPMF], *, season: str, round_: int) -> pl.DataFrame:
    if not pmfs:
        raise CardsModelError("pmfs_to_rows called with zero PMFs -- nothing to persist")
    frames = [pmf.to_polars() for pmf in pmfs]
    combined = pl.concat(frames)
    return combined.with_columns(pl.lit(season).alias("season"), pl.lit(round_).alias("round"))


def write_cards_pmfs(
    store: BitemporalStore,
    pmfs: Sequence[CardsPMF],
    *,
    season: str,
    round_: int,
    valid_at: datetime,
    calibration: CalibrationReference,
    source: str = "fplai.models.cards",
    skip_if_unchanged: bool = True,
) -> WriteResult:
    if not pmfs:
        raise CardsModelError("write_cards_pmfs called with zero PMFs -- nothing to write")
    rows = pmfs_to_rows(pmfs, season=season, round_=round_)
    derived_from = [
        DerivationInput(
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            entity_key={"season": season},
            note=(
                "trailing per-player and per-team-round card counts, aggregated per the feature "
                "engineering in fplai.models.cards -- whole-season aggregate input, not an "
                "individually-named row subset. Never referee identity (module docstring)."
            ),
        )
    ]
    return write_derived(
        store,
        PLAYER_CARDS_DISTRIBUTION_GAMEWEEK,
        rows,
        valid_at=valid_at,
        observed_at=datetime.now(timezone.utc),
        source=source,
        derived_from=derived_from,
        calibration=calibration,
        skip_if_unchanged=skip_if_unchanged,
    )
