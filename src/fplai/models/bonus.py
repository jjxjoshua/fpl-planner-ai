"""Bonus (BPS) model — blueprint §4 (fifth model row: "Expected bonus
distribution. Modelled from BPS components, not from historical bonus
alone."), §7.1 (calibration before points), §12.2 (derived-fact labelling).
Phase 2 / E5, session `s004`. Structural precedent: `fplai.models.
defensive_contribution` (feature engineering, DGW discipline, threshold-
provenance conventions) and `fplai.models.attacking` (feature
standardisation, composition-not-import for `MinutesPMF`); this module
differs from both in one load-bearing way explained below.

## Read this before anything else: bonus is a RANK phenomenon, not an
## independent per-player draw

A player earns bonus by finishing in the top 3 on BPS *within his own
fixture's full player pool* (both squads combined, minutes played or not —
verified below), not by clearing a fixed threshold the way DC does. The
brief's "modelled from BPS components, not from historical bonus alone" is
pointing at exactly this: this module fits a per-player BPS distribution
(the richer quantity, blueprint §7.1's "fit the richer quantity, derive the
narrower one" pattern DC/attacking both already establish), then DERIVES
the bonus PMF by coupling every player in a fixture's BPS draws through a
real, seeded Monte Carlo simulation of the rank/tie structure — never a
per-player logistic classifier on "did this player get bonus", which would
have no way to keep a fixture's total bonus internally consistent (§7.1's
gate would happily pass a model that gives every player in a 3-way tie a
55% chance of 3 points each, summing to a physically impossible 1.65
expected fixture-total from that tier alone).

## The award rule is PINNED FROM THE ARCHIVE, not press-sourced —
## different posture from `fplai.models.defensive_contribution`'s DC
## thresholds, and stated as a finding for the blueprint

DC's count thresholds (10/12) are a brand-new 2026/27 rule with no
historical archive to check them against, which is why that module reaches
for a live API observation (`scripts/pin_dc_thresholds.py`) instead. Bonus
scoring has been unchanged FPL rule-book mechanics for years, and `bps`/
`bonus` are BOTH complete (zero nulls) across every one of the 179,950
non-2019-20 rows this store holds (2020-21 through 2025-26; see "The
2019-20 exclusion" below) — the archive itself IS the ground truth here,
and this task's brief explicitly invites exactly this: *"If you conclude
the rules can be fully pinned from the archive, do it and say what you
attacked."*

**What was attacked, live, this session**: `assign_bonus_points`
(competition ranking — see below) applied to every real (season, round,
fixture) group's own `bps` values in this store, compared against that
SAME group's real `bonus` column, entity-keyed exactly like the archive
verification `pin_dc_thresholds.py` uses for DC (season, round, fixture).
Over 2,569 real fixtures / 179,950 real player-fixture rows spanning ALL 7
seasons in the store (2019-20 through 2025-26, i.e. INCLUDING the season
this module's own training table excludes for its feature columns —
`bps`/`bonus` themselves need no position/team join to check): **2,567 of
2,569 fixtures (99.92%) match EXACTLY.** The two exceptions:
1. `2019-20` round 29, fixture 275 — all 59 rows carry `bps=0` but
   `bonus=3` for every one of them; a known data-quality artefact of the
   season already flagged incomplete elsewhere in this store (no
   `position`/`team` at all — see "The 2019-20 exclusion").
2. `2021-22` round 1, fixture 8 — a single player (bps=28, tied with
   another player also at 28) received `bonus=1` where competition ranking
   predicts 0 (both tied-28 players structurally rank 4th behind three
   higher, DISTINCT BPS values in that match, so competition ranking gives
   neither of them a point) — not explained by tie logic alone; the most
   likely account is a genuine FPL `overrides` application on that specific
   fixture (blueprint §11 already flags this as a possibility for DC;
   evidently real for bonus too, at least once in 7 seasons), not a flaw in
   the rank logic, but recorded honestly as an unexplained residual rather
   than argued away.

`verify_bonus_award_rule_against_archive` (below) is this check as a real,
callable function — not just a docstring claim — re-run against the real
store by `tests/test_bonus.py`'s real-store-gated group and printed by
`scripts/fit_bonus.py`, so the 99.92% figure is reproducible from a commit
hash, not asserted once and left stale.

**Consequence for this module's design**: because the RANK TRANSFORM is
essentially deterministic and already verified, `verify=True` for
`BONUS_AWARD_RULE_VERIFIED` carries genuine machine-checked provenance —
BETTER evidence than DC's press citation, because it is drawn from the
archive's own settled scoring, not a rule-book description that could be
imprecisely paraphrased. The only genuinely UNCERTAIN quantity left in this
module is the BPS regression + residual distribution that feeds the
simulation — which is exactly where this module's calibration effort goes
(see "Walk-forward gate" below), rather than being split across both the
rule AND the regression the way DC had to split its effort across the
NB fit and the threshold pin.

## Competition ranking — `assign_bonus_points`

```
rank[player] = 1 + count(players with a STRICTLY GREATER bps value)
points = 3 if rank == 1, 2 if rank == 2, 1 if rank == 3, else 0
```

This single rule handles every tie shape observed in the store without a
special case: a 2-way tie for 1st both get rank 1 (both score 3, and
whoever is next gets rank 3, i.e. 1 point — never 2, matching FPL's own
published tie rule and the docstring's own 9-point observation, three-way
ties for 1st: all three score 3, nobody left ranks 2 or 3, fixture total 9,
matching the "9 -> 6 fixtures" figure this task's brief cites). A 2-way tie
for 2nd both get rank 2 (both score 2, nobody scores 1 — matches the "no
3rd-place point when 2nd is tied" FPL rule). A 2-way tie for 3rd both get
rank 3 (both score 1). This is exactly what `_assign_bonus_points_batch`'s
vectorised pairwise-comparison implementation computes, verified against
2,569 real fixtures above.

## `predict_bonus_pmfs_for_fixture` operates on a WHOLE FIXTURE, not one
## player — a deliberate, documented deviation from every sibling module's
## per-player `predict_*_pmf` signature

`fplai.models.attacking.predict_attacking_pmf`/`fplai.models.
defensive_contribution.predict_dc_pmf` both take one player's feature row
and return one player's PMF, because a goal/assist/DC count is genuinely
independent of what other players on the pitch do (module docstrings of
both). Bonus structurally is NOT independent across players in the same
fixture — this task's brief states this directly ("the probabilities have
to be coupled across the fixture's competitor set"). This module's
predict-time interface reflects that: `predict_bonus_pmfs_for_fixture`
takes every relevant player in a fixture (`Sequence[BonusPlayerInput]`) and
returns one `BonusPMF` per player, each one a genuine marginal derived from
the SAME joint Monte Carlo draw, not four calls to an independent
per-player function that happen to share a name.

## Composition with `MinutesPMF` — same shape as the sibling modules, at a
## different point in the pipeline

This module imports neither `fplai.models.minutes` nor `fplai.models.
attacking`/`team_strength` (the same "composition, not import" discipline
those two modules establish for each other). `BonusPlayerInput.
minute_exposure` is the exact `[(minutes_value, probability), ...]` shape
`fplai.models.defensive_contribution.predict_dc_pmf`/`fplai.models.
attacking.predict_attacking_pmf` already use, sourced from a fitted
`MinutesPMF`'s six band midpoints (`0.0, 15.0, 45.0, 67.0, 82.0, 90.0` —
documented here by value, per that same independence convention, not
imported). Unlike DC's multiplicative `log(minutes/90)` offset or
attacking's `minutes_frac * sigmoid(...)` parametrisation, THIS module puts
`minutes_frac` (`minutes.clip(0, 90) / 90.0`) directly into the standardised
feature vector as an ordinary regression feature (see "Why minutes_frac is
a feature, not an offset" below) — so composing over a minute-exposure
mixture means evaluating the regression once PER BAND (holding every other
feature fixed, varying only `minutes_frac`), then Monte-Carlo-mixing over
bands exactly as DC/attacking mix over minute bands for their own PMFs,
just one layer earlier (before the joint-rank simulation, not instead of
it).

## Why `minutes_frac` is a feature, not a multiplicative offset

DC's NB2 mean and attacking's Binomial success probability are both
STRUCTURALLY NON-NEGATIVE quantities, so a multiplicative
`exp(offset)`/`* minutes_frac` composition is natural and (for DC)
necessary to keep the fitted mean positive. BPS is NOT
sign-constrained — real observed range in this store is **-25 to 128**
(cards, missed penalties, own goals, goals conceded all push it negative;
see the module-level `bps min/max` figure verified live this session). A
multiplicative minutes term would force `bps -> 0` as `minutes -> 0`
UNCONDITIONALLY, which is correct in the modal case but wrong in principle
(a genuine forced substitution off before full time for a second yellow
does not un-happen because he played 40 minutes instead of 90) and, more
importantly, breaks down entirely for a genuinely negative predicted mean
(`exp(offset) * (negative mean)` flips sign as `offset` crosses zero,
which is nonsensical). Putting `minutes_frac` in as an ordinary
standardised numeric feature lets the regression itself learn the
(empirically near-linear, verified: 96,729 zero-minute rows in this store
carry `bps=0` in 96,717 of them, i.e. 99.99%) minutes relationship without
imposing a sign-forcing structural constraint the target does not obey.

## Zero-minute rows are INCLUDED in the fit and in the fixture-membership
## universe — a deliberate deviation from DC/attacking's `minutes > 0`
## filter, forced by the multiplicative-offset difference above

`fplai.models.defensive_contribution`/`fplai.models.attacking` both
restrict their regression fit to `minutes > 0` rows, because their
multiplicative offset terms are undefined (`log(0)`) or degenerate
(`Binomial(n, 0)`, structurally uninformative) at `minutes=0`. This
module's `minutes_frac` is an ordinary linear feature with no such
constraint, so `build_training_table` does NOT filter to `minutes > 0` —
and doing so would be actively WRONG here for a second reason: the
Monte Carlo joint-rank simulation needs the TRUE FULL competitive universe
of a fixture (both squads, played or not) to correctly reproduce which
players could plausibly beat a given player's BPS draw, exactly the
population `verify_bonus_award_rule_against_archive` checks against. A
model trained and evaluated only on `minutes > 0` rows would have no way to
represent "this fixture also contained 40 unused players who structurally
cannot outrank anyone" — harmless for correctness (a near-zero-mu player
essentially never wins a simulated tie) but WRONG for the *fit itself*,
since excluding ~96,729 real `bps=0, minutes=0` rows would silently bias
the fitted `minutes_frac` coefficient's magnitude (the regression would
never see the anchor case it needs to place the intercept/slope correctly).

## Feature design

`NUMERIC_FEATURE_COLUMNS_BONUS` (9, one more than DC's 9 — same order of
complexity, not coincidentally, since BPS visibly correlates with the same
per-player-and-team-style signal DC's own module docstring names):

- `player_trailing_bps_{3,5,10}` — this player's own trailing per-round BPS
  total, raw (not per-90), same "let the offset/feature do the minutes
  scaling job, leave trailing features as a raw counted signal" reasoning
  `fplai.models.defensive_contribution`'s own "Feature design notes" gives.
- `team_trailing_bps_mean_5` — the player's TEAM's trailing mean total
  round `bps` (summed across every rostered player, at ROUND grain,
  DGW-safe), the SAME team-style proxy DC's own module docstring justifies
  at length ("DC is a team-style statistic wearing a player's name") —
  applies just as directly to BPS, since a large share of BPS accrues from
  defensive actions and possession-adjacent play that scale with team
  style, not just individual quality.
- `games_played_this_season`, `cold_start`, `team_cold_start` — same
  cold-start discipline every sibling module uses.
- `was_home` — a fixture-own column, never rolled up.
- `minutes_frac` — see "Why minutes_frac is a feature" above.

Standardised at fit time and frozen (`BonusFeatureSpec.numeric_means`/
`numeric_stds`), the SAME discipline `fplai.models.attacking`'s own module
docstring diagnoses at length (an unstandardised fit + a fixed `l2_penalty`
penalises a unit of `beta` identically regardless of what that unit buys on
each feature's own scale — this module's own scale spread is at least as
wide as attacking's: `minutes_frac` ranges 0..1, `player_trailing_bps_*`
ranges roughly -10..80). Position is one-hot, no team-name dummy (same
"team-style feature, not team-identity dummy" reasoning DC's own module
docstring gives, for the same promoted-team-prior reason — Hull/Ipswich/
Coventry-class teams need a smooth numeric proxy, not an unestimable
categorical level).

## Set-piece/penalty duty and the raw BPS component breakdown — declared
## known-absent, `fplai.models.minutes`' `KNOWN_ABSENT_FEATURES` precedent

Same two features `fplai.models.attacking` already declares absent
(`primary_set_piece_taker`, `penalty_taker_duty`) apply here for the same
reason (they influence goals/assists, which feed BPS, and this store has
no forward-looking assignment signal for either). A THIRD is specific to
this module: `bps_component_breakdown` — the Premier League's own BPS
calculation sums many sub-actions (successful crosses, tackles won, key
passes, clearances off the line, etc; the DC wiki's §2.3 lists the
adjacent PL API fields) but this store carries only the FINAL AGGREGATE
`bps` number and a handful of coarse box-score columns, never the PL's own
per-action point breakdown. `player_trailing_bps_*`/`team_trailing_bps_
mean_5` are built entirely from the aggregate, which is what is genuinely
available — not a substitute for the component breakdown, an honest
absence of it.

## Ridge regression, closed-form — not L-BFGS

`fplai.models.attacking`'s own module docstring documents a real bug this
project already paid for: `l2_penalty=1.0` on an unstandardised design
matrix converges an ITERATIVE optimiser (L-BFGS-B) to a genuine but
football-nonsensical stationary point. This module sidesteps the
CONVERGENCE half of that risk entirely (not just the scale-transfer half,
which standardisation still handles): minimising `(1/n)||X beta - y||^2 +
l2 ||beta||^2` over a REAL-valued target is a convex quadratic with a
unique closed-form minimiser (`_fit_ridge`, `beta = (X^T X / n + l2*I)^-1 *
X^T y / n`) — no iteration, no `maxiter`, no "did not converge cleanly"
warning path, and therefore no way for this module to reproduce the exact
convergence-to-a-bad-optimum failure mode attacking.py diagnosed, PROVIDED
the same standardisation discipline is applied (it is — see "Feature
design" above). `_ridge_loss_and_grad` exists purely so a test can verify
`_fit_ridge`'s output is a genuine zero-gradient stationary point (the
quadratic-loss analogue of attacking's finite-difference gradient check) —
cross-checked against an independent `scipy.optimize.minimize` L-BFGS run
in `tests/test_bonus.py`, not merely asserted.

## Residual distribution — empirical bootstrap, bucketed by position, not
## an assumed parametric family

`fplai.models.defensive_contribution`'s own module docstring documents
measuring (not assuming) its residual shape and finding it right-skewed.
This module goes one step further and does not assume ANY parametric
family at all for the BPS residual: `_fit_from_table` stores every TRAINING
row's real `bps - X@beta` residual, bucketed by `position` (GK residuals
are structurally much tighter than an attacking FWD's, since a GK's BPS
ceiling and floor are both narrower), and `predict_bonus_pmfs_for_fixture`/
`walk_forward_validate` SAMPLE from this empirical pool (with replacement,
seeded) rather than drawing from a fitted Normal/skew-normal/etc — a
non-parametric bootstrap is the correct tool exactly when (as here) the
downstream use (feeding a rank/tie simulation that already depends on
getting integer-valued near-ties right) makes an assumed distributional
SHAPE a genuine risk, not merely a convenience the shape could get away
with being wrong about. `residual_pool_all` is the fallback pool for a
position with zero training-fold rows (a real possibility in an early
small walk-forward fold, or a hand-fabricated test table).

## Monte Carlo simulation — vectorised, seeded (CLAUDE.md rule 7)

`_assign_bonus_points_batch` computes competition rank via one vectorised
pairwise comparison over a `(n_sims, n_players)` array (`O(n_players^2)`
per simulation draw, cheap at this module's real fixture sizes — verified
live this session: mean 71.7, max 115 players per real fixture across
2020-21 through 2025-26, and a `(2000, 70)`-shaped benchmark on this
machine completes in ~14ms), so an entire fixture's joint bonus-outcome
distribution is one call, not a Python loop over simulation draws. Every
random draw (which minute band a player's exposure resolves to; which
residual is bootstrapped) goes through one `numpy.random.default_rng(seed)`
instance per call, `seed` an explicit, required-with-a-stated-default
keyword argument — never Python's global `random` module, never an
unseeded `default_rng()` — so a result is reproducible from a commit hash
plus that seed, exactly as CLAUDE.md rule 7 requires.

## Walk-forward gate (blueprint §7.1) — what is actually scored, and why
## it is a genuinely 4-CLASS gate, not a binarised one

Unlike DC's "count >= threshold" or attacking's "count >= 1", bonus is
already the exact quantity blueprint §4 asks this module to predict
("expected bonus distribution"), and CLAUDE.md rule 5 forbids collapsing
it to a scalar or a single binary event for the HEADLINE gate (a per-outcome
reliability report is required separately anyway — see below). The gate
therefore scores the full categorical distribution directly: TRUE
multiclass log-loss (`-mean(log(p[true_class]))`) and multiclass Brier
(`mean(sum_k (p_k - 1{k=true})^2)`), against TWO baselines, both built
fold-internally (train-only, never touching eval rows, walk-forward split
by ROUND — never by row count, which would break a DGW's two simultaneous
fixtures apart):

1. **Group (position) base rate** — the TRAINING-split categorical
   frequency of `bonus` within that eval row's position, recomputed fresh
   at every fold.
2. **Player-trailing rate** — this player's own trailing per-class
   frequency over the last 5 rounds (`player_trailing_bonus_class_rate_
   {0,1,2,3}_5`), nullable per-class for a genuine cold start (falling back
   to the position rate per class exactly as every sibling module's
   trailing-rate baseline does), defensively renormalised to sum to 1.0 (a
   DGW round where a player's summed round bonus exceeds 3 — a real but
   rare case, see `_build_player_round_rollup` — can leave the four
   indicator columns summing to less than 1 within the trailing window;
   this baseline is a stated best-effort approximation, not a formally
   verified PMF, and is documented as such rather than silently accepted
   as exact).

`walk_forward_validate` refits (closed-form, deterministic) at every
`(season, round)` fold using only strictly-earlier rows, groups that fold's
eval rows by `fixture` (a gameweek has several simultaneous fixtures — this
is required, not optional, for the joint simulation to see the RIGHT
competitor set per match), and runs the SAME Monte Carlo machinery
`predict_bonus_pmfs_for_fixture` uses — except with each row's own REAL
observed minutes (a point mass, not a mixture) as its `minutes_frac`,
mirroring the exact "walk-forward uses real historical minutes directly,
never a `MinutesPMF` forecast" convention `fplai.models.attacking.
walk_forward_validate`/`fplai.models.defensive_contribution.walk_forward_
validate` both already establish (minutes forecasting is a separate
model's job, out of scope for evaluating THIS module's own calibration).

## Reliability — required per-outcome, not a single pooled number

This task's brief is explicit, and names the minutes model's own
miscalibration (log-loss 0.3464 / Brier 0.1062 while carrying ECE 0.0470,
slope 1.114 — passed its own gate anyway) as the standing warning. Because
bonus is 4-class, "per outcome" here means one full reliability report
(ECE, Cox calibration slope/intercept, the full reliability table) for EACH
of the four one-vs-rest series `P(bonus == k)` vs `1{bonus == k}`, `k` in
`0, 1, 2, 3` — `BonusWalkForwardResult.reliability_for_outcome(k)`.
`reliability_diagram`/`expected_calibration_error`/`_calibration_slope_
intercept`/`CalibrationMetrics` below are a direct duplication of `fplai.
models.minutes`'s own implementations (same "no cross-model import"
discipline every sibling module states) — same math, same Cox-regression
calibration slope/intercept convention, applied to a different quantity.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import polars as pl

from fplai.derived import CalibrationReference, DerivationInput, write_derived
from fplai.gameweek_stats import read_player_gameweek_stats
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    PLAYER_BONUS_DISTRIBUTION_DATASET,
    PLAYER_BONUS_DISTRIBUTION_GAMEWEEK,
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
    "bps",
    "bonus",
)

POSITIONS: tuple[str, ...] = ("GK", "DEF", "MID", "FWD")
OUTCOMES: tuple[int, ...] = (0, 1, 2, 3)


class BonusModelError(ValueError):
    """Misuse or a genuine data problem this module refuses to guess past —
    a naive `as_of`, an empty training window, a PMF that would not
    (re)normalise, a `minute_exposure` that does not sum to 1, or a fixture
    with fewer than 2 players (bonus cannot be ranked against a field of
    one)."""


# ---------------------------------------------------------------------------
# The award rule — pinned from the archive (module docstring). Pure
# functions, no store, no fitted parameters.
# ---------------------------------------------------------------------------


def _assign_bonus_points_batch(bps: np.ndarray) -> np.ndarray:
    """Vectorised competition ranking (module docstring, "Competition
    ranking"). `bps`: `(n_sims, n_players)`. Returns points of the same
    shape, each entry in `{0, 1, 2, 3}`.

    `rank[i, j] = 1 + count(k such that bps[i, k] > bps[i, j])` — computed
    via one pairwise-comparison broadcast rather than `scipy.stats.
    rankdata` per row, so the whole `(n_sims, n_players)` batch is one
    vectorised call (module docstring, "Monte Carlo simulation")."""
    if bps.ndim != 2:
        raise BonusModelError(
            f"_assign_bonus_points_batch expects a 2D (n_sims, n_players) array, got ndim={bps.ndim}"
        )
    if bps.shape[1] < 2:
        raise BonusModelError(
            f"_assign_bonus_points_batch needs at least 2 players per simulation row, got {bps.shape[1]}"
        )
    greater = bps[:, None, :] > bps[:, :, None]
    rank = greater.sum(axis=2) + 1
    points = np.zeros_like(rank, dtype=np.int64)
    points[rank == 1] = 3
    points[rank == 2] = 2
    points[rank == 3] = 1
    return points


def assign_bonus_points(bps_values: Sequence[float]) -> tuple[int, ...]:
    """Single-fixture convenience wrapper over `_assign_bonus_points_batch`
    — the function `verify_bonus_award_rule_against_archive` and
    `tests/test_bonus.py`'s hand-built tie-shape tests both call directly."""
    arr = np.asarray(list(bps_values), dtype=np.float64).reshape(1, -1)
    return tuple(int(v) for v in _assign_bonus_points_batch(arr)[0])


@dataclass(frozen=True)
class BonusAwardRuleVerification:
    """The result of attacking `assign_bonus_points` against every real
    fixture in the store — module docstring, "The award rule is PINNED
    FROM THE ARCHIVE". `mismatch_examples` names every mismatched
    `(season, round, fixture)` up to `max_examples`, so a caller can go
    inspect one directly rather than trusting a bare match-rate number."""

    n_fixtures: int
    n_mismatched_fixtures: int
    n_player_rows: int
    match_rate: float
    mismatch_examples: tuple[tuple[str, int, int], ...]


def verify_bonus_award_rule_against_archive(
    store: BitemporalStore, *, as_of: datetime, max_examples: int = 10
) -> BonusAwardRuleVerification:
    """Re-derive the 99.92% figure this module's docstring cites, LIVE,
    against whatever store is passed in — never a cached/hardcoded number.
    Groups every row by `(season, round, fixture)` (the exact grain
    `fplai.models.defensive_contribution`'s DGW discipline already
    establishes for this dataset), applies `assign_bonus_points` to that
    group's real `bps` values, and compares against that SAME group's real
    `bonus` column."""
    if as_of.tzinfo is None:
        raise BonusModelError(f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2.")
    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise BonusModelError(f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()}")

    # Decision 2: attribution_complete=False rows never train (degraded DGW rows)
    raw = raw.filter(pl.col("attribution_complete") != False)  # noqa: E712 (explicit equality with bool)

    missing = [c for c in ("season", "round", "fixture", "bps", "bonus") if c not in raw.columns]
    if missing:
        raise BonusModelError(f"player gameweek stats is missing required column(s) {missing}")

    n_fixtures = 0
    n_mismatched = 0
    examples: list[tuple[str, int, int]] = []
    for (season, round_, fixture), sub in raw.group_by(["season", "round", "fixture"]):
        if sub.height < 2:
            continue  # cannot rank a field of one -- not a mismatch, structurally excluded
        n_fixtures += 1
        bps_vals = sub["bps"].cast(pl.Float64).to_numpy()
        bonus_vals = sub["bonus"].cast(pl.Int64).to_numpy()
        predicted = np.array(assign_bonus_points(bps_vals), dtype=np.int64)
        if not np.array_equal(predicted, bonus_vals):
            n_mismatched += 1
            if len(examples) < max_examples:
                examples.append((str(season), int(round_), int(fixture)))

    match_rate = 1.0 - (n_mismatched / n_fixtures) if n_fixtures else 0.0
    return BonusAwardRuleVerification(
        n_fixtures=n_fixtures,
        n_mismatched_fixtures=n_mismatched,
        n_player_rows=raw.height,
        match_rate=match_rate,
        mismatch_examples=tuple(examples),
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BonusModelConfig:
    """Every fitting hyperparameter, in one place — same convention every
    sibling module establishes. Defaults are stated, reasoned starting
    points (module docstring, "Ridge regression, closed-form"), not
    calibrated optima."""

    player_trailing_windows: tuple[int, ...] = (3, 5, 10)
    team_trailing_window: int = 5
    l2_penalty: float = 0.01
    """Same value `fplai.models.attacking.AttackingModelConfig.l2_penalty`
    settled on after a real sweep against its own real training rows, for
    the same reason: standardised features with a genuinely wide scale
    spread (`minutes_frac` 0..1 against `player_trailing_bps_*`'s ~-10..80)
    make DC/minutes' shared `l2_penalty=1.0` risk over-shrinking this
    module's regression toward zero. `scripts/fit_bonus.py`'s own
    punch-out records the real walk-forward numbers this default produces
    against the real store.

    **Session `s005`: `_fit_ridge`'s L2-scaling bug was fixed** (see that
    function's own docstring) and this default re-measured, RAW and swept
    across a grid spanning `0.001`..`1.0`, before being touched. Unlike
    every sibling model this session, the fix is a NEGATIVE result here —
    every metric moves by 0.1-0.2% across the entire grid, indistinguishable
    from noise, because this module's loss operates on a raw-valued target
    (BPS, residual variance in the tens) rather than a log-likelihood-scale
    one, so the missing `1/n` on the penalty term was never large enough to
    matter at this `l2`. `docs/wiki/model-bonus.md` has the full grid and
    the loss-scale reasoning; the default is unchanged."""
    n_simulations: int = 4000
    """Default Monte Carlo draw count for `predict_bonus_pmfs_for_fixture`
    — module docstring, "Monte Carlo simulation" (benchmarked ~14ms per
    fixture at this setting and this store's real fixture sizes)."""


# ---------------------------------------------------------------------------
# Raw-row normalisation — same drift case every sibling module documents
# for this exact dataset, duplicated (not imported) to keep this module
# independent.
# ---------------------------------------------------------------------------


def _normalise_and_filter_positions(raw: pl.DataFrame) -> pl.DataFrame:
    """Drops `position == "AM"` rows and remaps `GKP -> GK`. A NULL
    `position` (every 2019-20 row in this store — verified live this
    session, 16,556/16,556) compares as NULL against `"AM"`, which
    `.filter()` treats as excluded, not included — so this same call
    structurally excludes 2019-20 too, without a separate hardcoded season
    string (module docstring, "The 2019-20 exclusion"). `build_training_
    table` ALSO filters `team.is_not_null()` explicitly rather than relying
    on this incidental behaviour alone, so the exclusion is deliberate and
    legible at the call site, not merely a side effect of the AM filter."""
    if "position" not in raw.columns:
        return raw
    raw = raw.filter(pl.col("position") != "AM")
    return raw.with_columns(
        pl.when(pl.col("position") == "GKP")
        .then(pl.lit("GK"))
        .otherwise(pl.col("position"))
        .alias("position")
    )


# ---------------------------------------------------------------------------
# Trailing feature rollups — round grain, leakage-safe, DGW-safe (module
# docstring, "Feature design" / mirrors fplai.models.defensive_contribution
# exactly in its double-gameweek discipline).
# ---------------------------------------------------------------------------


def _build_team_round_rollup(raw: pl.DataFrame, config: BonusModelConfig) -> pl.DataFrame:
    team_round = (
        raw.group_by(["season", "team", "round"])
        .agg(pl.col("bps").sum().alias("team_round_bps_total"))
        .sort(["season", "team", "round"])
    )
    w = config.team_trailing_window
    team_round = team_round.with_columns(
        pl.col("team_round_bps_total")
        .cast(pl.Float64)
        .shift(1)
        .rolling_mean(window_size=w, min_samples=1)
        .over(["season", "team"])
        .alias(f"team_trailing_bps_mean_{w}"),
        pl.col("team_round_bps_total").shift(1).over(["season", "team"]).is_null().alias("team_cold_start"),
    )
    return team_round.with_columns(pl.col(f"team_trailing_bps_mean_{w}").fill_null(0.0)).select(
        ["season", "team", "round", f"team_trailing_bps_mean_{w}", "team_cold_start"]
    )


def _build_player_round_rollup(raw: pl.DataFrame, config: BonusModelConfig) -> pl.DataFrame:
    """`round_bonus_total` (a DGW-round sum, can exceed 3 — see the
    trailing-rate columns below) is used ONLY to build the baseline-only
    per-class trailing rate features, never fed to the regression itself
    (module docstring, "Walk-forward gate")."""
    rollup = (
        raw.group_by(["season", "element", "round"])
        .agg(
            pl.col("bps").sum().alias("round_bps"),
            pl.col("bonus").sum().alias("round_bonus_total"),
            pl.col("minutes").sum().alias("round_minutes"),
        )
        .sort(["season", "element", "round"])
    )
    appeared = (pl.col("round_minutes") > 0).cast(pl.Float64)

    trailing_exprs = []
    for w in config.player_trailing_windows:
        trailing_exprs.append(
            pl.col("round_bps")
            .cast(pl.Float64)
            .shift(1)
            .rolling_mean(window_size=w, min_samples=1)
            .over(["season", "element"])
            .alias(f"player_trailing_bps_{w}")
        )
    trailing_exprs.append(
        appeared.shift(1).fill_null(0.0).cum_sum().over(["season", "element"]).alias("games_played_this_season")
    )
    # Baseline-only features (never fed to the regression itself — see
    # walk_forward_validate) — this player's own trailing per-class bonus
    # rate, kept nullable so a genuine cold start is visibly absent, same
    # convention every sibling module's own trailing-rate baseline uses.
    for k in OUTCOMES:
        indicator = (pl.col("round_bonus_total") == k).cast(pl.Float64)
        trailing_exprs.append(
            indicator.shift(1)
            .rolling_mean(window_size=5, min_samples=1)
            .over(["season", "element"])
            .alias(f"player_trailing_bonus_class_rate_{k}_5")
        )

    rollup = rollup.with_columns(trailing_exprs)
    fill_zero = [f"player_trailing_bps_{w}" for w in config.player_trailing_windows]
    rollup = rollup.with_columns([pl.col(c).fill_null(0.0) for c in fill_zero])
    rollup = rollup.with_columns((pl.col("games_played_this_season") == 0).alias("cold_start"))
    return rollup


NUMERIC_FEATURE_COLUMNS_BONUS: tuple[str, ...] = (
    "player_trailing_bps_3",
    "player_trailing_bps_5",
    "player_trailing_bps_10",
    "team_trailing_bps_mean_5",
    "games_played_this_season",
    "cold_start",
    "team_cold_start",
    "was_home",
    "minutes_frac",
)

# See module docstring, "Set-piece/penalty duty and the raw BPS component
# breakdown". Declared as data, not just prose, same convention every
# sibling module establishes.
KNOWN_ABSENT_FEATURES: tuple[str, ...] = (
    "primary_set_piece_taker",
    "penalty_taker_duty",
    "bps_component_breakdown",
)

assert not set(NUMERIC_FEATURE_COLUMNS_BONUS) & set(KNOWN_ABSENT_FEATURES)


# ---------------------------------------------------------------------------
# Training table — fixture grain, zero-minute rows INCLUDED (module
# docstring, "Zero-minute rows are INCLUDED").
# ---------------------------------------------------------------------------


def build_training_table(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: BonusModelConfig = BonusModelConfig(),
    allow_live_season: bool = False,
) -> pl.DataFrame:
    """The full leakage-safe feature+target table, one row per (season,
    round, element, fixture) — INCLUDING zero-minute rows (module
    docstring). Reads via the capability reader on `kickoff_time`, the
    same bitemporal primitive every sibling module uses for this dataset."""
    if as_of.tzinfo is None:
        raise BonusModelError(
            f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2."
        )

    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise BonusModelError(f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()}")

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
        raise BonusModelError(f"player gameweek stats is missing required column(s) {missing}")

    raw = _normalise_and_filter_positions(raw)
    raw = raw.filter(pl.col("team").is_not_null())  # explicit, not incidental (module docstring)

    if seasons is not None:
        seasons = tuple(seasons)
        raw = raw.filter(pl.col("season").is_in(list(seasons)))
        if raw.is_empty():
            raise BonusModelError(
                f"no player gameweek stats for seasons {seasons!r} at as_of={as_of.isoformat()}. "
                "Note that allow_live_season=False excludes FPL-API-sourced rows BEFORE this "
                "season filter is applied, so a live season requested here resolves to nothing."
            )

    if raw.is_empty():
        raise BonusModelError(
            "no rows remain after position/team normalisation -- this module excludes 2019-20 "
            "(no position/team upstream, module docstring); check the requested seasons/as_of."
        )

    unexpected_positions = set(raw["position"].unique().to_list()) - set(POSITIONS)
    if unexpected_positions:
        raise BonusModelError(
            f"unexpected position value(s) after normalisation: {unexpected_positions} — expected "
            f"a subset of {POSITIONS}."
        )

    raw = raw.with_columns((pl.col("minutes").clip(0, 90).cast(pl.Float64) / 90.0).alias("minutes_frac"))

    team_rollup = _build_team_round_rollup(raw, config)
    team_col = f"team_trailing_bps_mean_{config.team_trailing_window}"
    player_rollup = _build_player_round_rollup(raw, config)

    trailing_cols = (
        [f"player_trailing_bps_{w}" for w in config.player_trailing_windows]
        + ["games_played_this_season", "cold_start"]
        + [f"player_trailing_bonus_class_rate_{k}_5" for k in OUTCOMES]
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

    check_cols = [
        c
        for c in NUMERIC_FEATURE_COLUMNS_BONUS
        if c not in ("was_home", "minutes_frac", "team_trailing_bps_mean_5", "team_cold_start")
    ]
    missing_after_join = [c for c in check_cols if table[c].null_count() > 0]
    if missing_after_join:
        raise BonusModelError(
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
# Feature spec + design matrix (standardised — module docstring, "Feature
# design").
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BonusFeatureSpec:
    numeric_columns: tuple[str, ...]
    position_categories: tuple[str, ...]
    numeric_means: tuple[float, ...]
    numeric_stds: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.numeric_means) != len(self.numeric_columns) or len(self.numeric_stds) != len(
            self.numeric_columns
        ):
            raise BonusModelError(
                "numeric_means/numeric_stds must have exactly one entry per numeric_columns entry"
            )

    @property
    def feature_names(self) -> tuple[str, ...]:
        return ("intercept",) + self.numeric_columns + tuple(f"position={p}" for p in self.position_categories)

    @property
    def n_features(self) -> int:
        return len(self.feature_names)


def _build_feature_spec(table: pl.DataFrame) -> BonusFeatureSpec:
    positions = tuple(sorted(table["position"].unique().to_list()))
    unexpected = set(positions) - set(POSITIONS)
    if unexpected:
        raise BonusModelError(f"unexpected position categories in training table: {unexpected}")
    means: list[float] = []
    stds: list[float] = []
    for c in NUMERIC_FEATURE_COLUMNS_BONUS:
        series = table[c].cast(pl.Float64)
        mean = float(series.mean()) if table.height > 0 else 0.0
        std = float(series.std(ddof=0)) if table.height > 0 else 0.0
        means.append(mean)
        stds.append(std if std > 1e-8 else 1.0)
    return BonusFeatureSpec(
        numeric_columns=NUMERIC_FEATURE_COLUMNS_BONUS,
        position_categories=positions,
        numeric_means=tuple(means),
        numeric_stds=tuple(stds),
    )


def _design_matrix(table: pl.DataFrame, spec: BonusFeatureSpec) -> np.ndarray:
    n = table.height
    cols: list[np.ndarray] = [np.ones(n, dtype=np.float64)]
    for c, mean, std in zip(spec.numeric_columns, spec.numeric_means, spec.numeric_stds):
        raw = table[c].cast(pl.Float64).to_numpy()
        cols.append((raw - mean) / std)
    positions = table["position"].to_list()
    for p in spec.position_categories:
        cols.append(np.array([1.0 if v == p else 0.0 for v in positions], dtype=np.float64))
    return np.column_stack(cols)


def _feature_row_to_vector(feature_row: dict, spec: BonusFeatureSpec) -> np.ndarray:
    missing = [c for c in spec.numeric_columns if c not in feature_row]
    if missing:
        raise BonusModelError(f"feature_row is missing required feature(s) {missing}")
    if "position" not in feature_row:
        raise BonusModelError("feature_row must carry 'position'")
    row_table = pl.DataFrame(
        {**{c: [feature_row[c]] for c in spec.numeric_columns}, "position": [feature_row["position"]]}
    )
    return _design_matrix(row_table, spec)[0]


# ---------------------------------------------------------------------------
# Closed-form ridge regression (module docstring, "Ridge regression,
# closed-form").
# ---------------------------------------------------------------------------


def _ridge_loss_and_grad(beta: np.ndarray, X: np.ndarray, y: np.ndarray, l2: float) -> tuple[float, np.ndarray]:
    """`(1/n)||X beta - y||^2 + (l2/n) * ||beta||^2` and its gradient — used
    ONLY by `tests/test_bonus.py` to verify `_fit_ridge`'s closed-form
    output is a genuine zero-gradient stationary point (module docstring),
    never called from the fitting path itself (which needs no iteration).

    **L2 scaling fixed session `s005`** — this module's own copy of the same
    defect every sibling module found and fixed independently this session
    (`docs/wiki/model-minutes.md` §13 has the fullest derivation): the ridge
    term was `l2 * sum(beta**2)`, unscaled against the per-row-AVERAGED
    `mean(resid**2)` term — effectively `l2*n` for `n` in the thousands.
    Now scaled by the same `1/n` the mean-squared-error term already
    carries, in both the loss and `_fit_ridge`'s closed-form solution below
    (which is now the textbook closed-form ridge solution `(X^T X +
    l2*I)^-1 X^T y`, not a rescaled variant of it — see that function's own
    docstring for the algebra). This module's own `l2_penalty` default
    (`0.01`) was already chosen specifically to compensate for this bug
    (`BonusModelConfig.l2_penalty`'s own docstring cites the same
    over-shrinking risk `fplai.models.attacking`'s docstring names), so the
    fix was re-measured against the real store before the default was
    touched — see `docs/wiki/model-bonus.md` for the full before/after and
    swept-grid tables."""
    n = X.shape[0]
    resid = X @ beta - y
    loss = float(np.mean(resid**2) + (l2 / n) * float(np.sum(beta * beta)))
    grad = (2.0 / n) * (X.T @ resid) + 2.0 * (l2 / n) * beta
    return loss, grad


def _fit_ridge(X: np.ndarray, y: np.ndarray, l2: float) -> np.ndarray:
    """Closed-form minimiser of `_ridge_loss_and_grad`'s (corrected, session
    `s005`) loss: `beta = (X^T X / n + (l2/n) * I)^-1 * (X^T y / n)`, which
    simplifies algebraically to `(X^T X + l2*I)^-1 * X^T y` — the standard
    textbook closed-form ridge solution, with no `n`-dependence left at all
    once both terms carry the same `1/n` (verify: multiply both sides of
    `(X^TX/n + (l2/n)I) beta = X^Ty/n` by `n`). The version this module
    shipped with before session `s005` divided `X^T X`/`X^T y` by `n` but
    added `l2*I` UNSCALED — algebraically equivalent to using an `n`-times
    LARGER penalty than the loss it was solving for, the closed-form twin of
    the iterative-optimiser bug every sibling module's own `l2` fix
    documents. Deterministic, no randomness, no iteration (CLAUDE.md rule
    7)."""
    n, p = X.shape
    if n == 0:
        raise BonusModelError("_fit_ridge called with zero rows -- nothing to fit")
    xtx = (X.T @ X) / n
    xty = (X.T @ y) / n
    a = xtx + (l2 / n) * np.eye(p, dtype=np.float64)
    return np.linalg.solve(a, xty)


# ---------------------------------------------------------------------------
# Fitted params
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class BonusModelParams:
    """`eq=False`: carries numpy arrays, same convention every sibling
    module's own fitted-params dataclass establishes."""

    beta: np.ndarray
    feature_spec: BonusFeatureSpec
    residual_pool_by_position: dict[str, tuple[float, ...]]
    residual_pool_all: tuple[float, ...]
    config: BonusModelConfig
    as_of: datetime
    seasons_used: tuple[str, ...]
    n_rows_used: int


def _residual_pool_for(params: BonusModelParams, position: str) -> np.ndarray:
    """Position-specific pool, falling back to the pooled cross-position
    pool for a position genuinely absent from the fitting window (module
    docstring, "Residual distribution")."""
    pool = params.residual_pool_by_position.get(position)
    if pool:
        return np.asarray(pool, dtype=np.float64)
    return np.asarray(params.residual_pool_all, dtype=np.float64)


def _fit_from_table(table: pl.DataFrame, *, config: BonusModelConfig, as_of: datetime) -> BonusModelParams:
    if table.is_empty():
        raise BonusModelError("no rows in this training table -- nothing to fit")

    spec = _build_feature_spec(table)
    X = _design_matrix(table, spec)
    y = table["bps"].cast(pl.Float64).to_numpy()
    beta = _fit_ridge(X, y, config.l2_penalty)
    residuals = y - X @ beta

    positions = table["position"].to_list()
    residual_pool_by_position: dict[str, tuple[float, ...]] = {}
    for p in POSITIONS:
        idx = [i for i, pp in enumerate(positions) if pp == p]
        if idx:
            residual_pool_by_position[p] = tuple(float(residuals[i]) for i in idx)

    return BonusModelParams(
        beta=beta,
        feature_spec=spec,
        residual_pool_by_position=residual_pool_by_position,
        residual_pool_all=tuple(float(r) for r in residuals),
        config=config,
        as_of=as_of,
        seasons_used=tuple(sorted(table["season"].unique().to_list())),
        n_rows_used=table.height,
    )


def fit_bonus_model(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: BonusModelConfig = BonusModelConfig(),
) -> BonusModelParams:
    table = build_training_table(store, as_of=as_of, seasons=seasons, config=config)
    return _fit_from_table(table, config=config, as_of=as_of)


# ---------------------------------------------------------------------------
# PMF + fixture-joint prediction (module docstring, "predict_bonus_pmfs_
# for_fixture operates on a WHOLE FIXTURE").
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BonusPMF:
    """The model's actual output for one player-fixture: a full PMF over
    `{0, 1, 2, 3}` — never a scalar (CLAUDE.md rule 5). `n_simulations`
    carries the Monte Carlo draw count this PMF was estimated from, so a
    consumer can judge its own resolution (module docstring)."""

    element: int
    fixture: int
    counts: tuple[int, ...]
    probabilities: tuple[float, ...]
    n_simulations: int

    def __post_init__(self) -> None:
        if len(self.counts) != len(self.probabilities):
            raise BonusModelError("counts and probabilities must be the same length")
        total = sum(self.probabilities)
        if abs(total - 1.0) > 1e-6:
            raise BonusModelError(f"BonusPMF probabilities do not sum to 1.0 (got {total}) for element={self.element}")
        if self.n_simulations <= 0:
            raise BonusModelError(f"n_simulations must be positive, got {self.n_simulations}")

    def p_bonus_awarded(self) -> float:
        return sum(p for c, p in zip(self.counts, self.probabilities) if c >= 1)

    def expected_bonus_points(self) -> float:
        return sum(c * p for c, p in zip(self.counts, self.probabilities))

    def to_polars(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "element": [self.element] * len(self.counts),
                "fixture": [self.fixture] * len(self.counts),
                "count": list(self.counts),
                "probability": list(self.probabilities),
                "n_simulations": [self.n_simulations] * len(self.counts),
            }
        )


@dataclass(frozen=True)
class BonusPlayerInput:
    """One player's inputs to a fixture-joint bonus simulation.
    `feature_row` must carry every column in `NUMERIC_FEATURE_COLUMNS_
    BONUS` EXCEPT `minutes_frac` (supplied per-band via `minute_exposure`,
    module docstring "Composition with MinutesPMF") plus, implicitly,
    `position` (supplied separately, used both to build the design row and
    to pick the right residual pool)."""

    element: int
    position: str
    feature_row: dict
    minute_exposure: Sequence[tuple[float, float]]

    def __post_init__(self) -> None:
        if "minutes_frac" in self.feature_row:
            raise BonusModelError(
                "BonusPlayerInput.feature_row must not carry 'minutes_frac' -- supplied via "
                "minute_exposure per-band (module docstring, 'Composition with MinutesPMF')."
            )
        if "position" in self.feature_row:
            raise BonusModelError(
                "BonusPlayerInput.feature_row must not carry 'position' -- supplied via the "
                "dedicated 'position' field."
            )
        total = sum(w for _, w in self.minute_exposure)
        if abs(total - 1.0) > 1e-6:
            raise BonusModelError(
                f"minute_exposure probabilities must sum to 1.0 for element={self.element}, got {total}"
            )


def predict_bonus_pmfs_for_fixture(
    params: BonusModelParams,
    players: Sequence[BonusPlayerInput],
    *,
    fixture: int,
    n_simulations: int | None = None,
    seed: int = 0,
) -> list[BonusPMF]:
    """Simulate this fixture's joint BPS draw `n_simulations` times (one
    `numpy.random.default_rng(seed)` instance, CLAUDE.md rule 7), derive
    each player's bonus PMF from the SAME joint draws via `_assign_bonus_
    points_batch` (module docstring). Needs at least 2 players — bonus
    cannot be ranked against a field of one.

    Each player's own `minute_exposure` mixture is resolved into a
    per-simulation minutes BAND (weighted random choice), and that band's
    own `minutes_frac` is substituted into an otherwise-fixed feature row
    to get that band's predicted mean BPS (`mu`) -- exactly the "vary only
    minutes_frac, hold everything else fixed" composition the module
    docstring describes. A residual is then bootstrapped (with
    replacement) from that player's own POSITION's empirical residual pool
    and added to `mu`, and the whole batch is rounded to the nearest
    integer (module docstring, "Monte Carlo simulation" -- BPS is
    genuinely integer-valued, and un-rounded floats would essentially
    never tie, silently understating the real ~28% tie-fixture rate)."""
    if len(players) < 2:
        raise BonusModelError(
            f"predict_bonus_pmfs_for_fixture needs at least 2 players to rank against each other, "
            f"got {len(players)}"
        )
    n_sims = n_simulations if n_simulations is not None else params.config.n_simulations
    if n_sims <= 0:
        raise BonusModelError(f"n_simulations must be positive, got {n_sims}")
    rng = np.random.default_rng(seed)
    spec = params.feature_spec

    bps_matrix = np.zeros((len(players), n_sims), dtype=np.float64)
    for i, player in enumerate(players):
        bands = list(player.minute_exposure)
        band_values = [m for m, _ in bands]
        band_weights = np.array([w for _, w in bands], dtype=np.float64)
        band_weights = band_weights / band_weights.sum()
        mu_per_band = np.array(
            [
                float(
                    _feature_row_to_vector(
                        {
                            **player.feature_row,
                            "minutes_frac": min(max(float(m), 0.0), 90.0) / 90.0,
                            "position": player.position,
                        },
                        spec,
                    )
                    @ params.beta
                )
                for m in band_values
            ]
        )
        band_idx = rng.choice(len(bands), size=n_sims, p=band_weights)
        mu_sim = mu_per_band[band_idx]
        pool = _residual_pool_for(params, player.position)
        residual_sim = rng.choice(pool, size=n_sims, replace=True)
        bps_matrix[i] = mu_sim + residual_sim

    bps_sim_rounded = np.round(bps_matrix.T)  # (n_sims, n_players)
    points_matrix = _assign_bonus_points_batch(bps_sim_rounded)  # (n_sims, n_players)

    pmfs: list[BonusPMF] = []
    for i, player in enumerate(players):
        outcomes_this = points_matrix[:, i]
        probs = tuple(float(np.mean(outcomes_this == k)) for k in OUTCOMES)
        pmfs.append(BonusPMF(element=player.element, fixture=fixture, counts=OUTCOMES, probabilities=probs, n_simulations=n_sims))
    return pmfs


# ---------------------------------------------------------------------------
# Reliability helpers — duplicated from fplai.models.minutes (module
# docstring, "Reliability — required per-outcome").
# ---------------------------------------------------------------------------


def _log_loss(y_true: Sequence[int], p: Sequence[float], eps: float = 1e-12) -> float:
    n = len(y_true)
    if n == 0:
        raise BonusModelError("log_loss over zero rows is undefined")
    total = 0.0
    for y, prob in zip(y_true, p):
        prob = min(max(prob, eps), 1.0 - eps)
        total += -(y * math.log(prob) + (1 - y) * math.log(1 - prob))
    return total / n


def _brier(y_true: Sequence[int], p: Sequence[float]) -> float:
    n = len(y_true)
    if n == 0:
        raise BonusModelError("brier score over zero rows is undefined")
    return sum((prob - y) ** 2 for y, prob in zip(y_true, p)) / n


@dataclass(frozen=True)
class ReliabilityBin:
    bin_lo: float
    bin_hi: float
    n: int
    mean_predicted: float
    observed_rate: float


def reliability_diagram(y_true: Sequence[int], p: Sequence[float], n_bins: int = 10) -> tuple[ReliabilityBin, ...]:
    n = len(y_true)
    if n == 0:
        raise BonusModelError("reliability_diagram over zero rows is undefined")
    bins: list[ReliabilityBin] = []
    edges = [i / n_bins for i in range(n_bins + 1)]
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, prob in enumerate(p) if (prob >= lo and (prob < hi or hi == 1.0))]
        if not idx:
            continue
        preds = [p[i] for i in idx]
        actuals = [y_true[i] for i in idx]
        bins.append(
            ReliabilityBin(
                bin_lo=lo,
                bin_hi=hi,
                n=len(idx),
                mean_predicted=sum(preds) / len(preds),
                observed_rate=sum(actuals) / len(actuals),
            )
        )
    return tuple(bins)


def expected_calibration_error(y_true: Sequence[int], p: Sequence[float], n_bins: int = 10) -> float:
    n = len(y_true)
    if n == 0:
        raise BonusModelError("expected_calibration_error over zero rows is undefined")
    bins = reliability_diagram(y_true, p, n_bins=n_bins)
    return sum(b.n * abs(b.mean_predicted - b.observed_rate) for b in bins) / n


def _calibration_slope_intercept(y_true: Sequence[int], p: Sequence[float]) -> tuple[float, float]:
    p_arr = np.clip(np.asarray(p, dtype=np.float64), 1e-6, 1.0 - 1e-6)
    x = np.log(p_arr / (1.0 - p_arr))
    y = np.asarray(y_true, dtype=np.float64)
    X = np.column_stack([np.ones_like(x), x])
    beta = np.zeros(2, dtype=np.float64)
    for _ in range(50):
        eta = X @ beta
        pi = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(pi * (1.0 - pi), 1e-10, None)
        grad = X.T @ (y - pi)
        hessian = (X * w[:, None]).T @ X + np.eye(2) * 1e-8
        delta = np.linalg.solve(hessian, grad)
        beta = beta + delta
        if np.max(np.abs(delta)) < 1e-10:
            break
    return float(beta[1]), float(beta[0])  # slope, intercept


@dataclass(frozen=True)
class CalibrationMetrics:
    n: int
    log_loss: float
    brier: float
    ece: float
    calibration_slope: float
    calibration_intercept: float
    reliability: tuple[ReliabilityBin, ...]


def _calibration_metrics(y_true: Sequence[int], p: Sequence[float], n_bins: int = 10) -> CalibrationMetrics:
    slope, intercept = _calibration_slope_intercept(y_true, p)
    return CalibrationMetrics(
        n=len(y_true),
        log_loss=_log_loss(y_true, p),
        brier=_brier(y_true, p),
        ece=expected_calibration_error(y_true, p, n_bins=n_bins),
        calibration_slope=slope,
        calibration_intercept=intercept,
        reliability=reliability_diagram(y_true, p, n_bins=n_bins),
    )


# ---------------------------------------------------------------------------
# Multiclass gate metrics (module docstring, "Walk-forward gate").
# ---------------------------------------------------------------------------


def _multiclass_log_loss(y_true_class: Sequence[int], p_matrix: Sequence[Sequence[float]], eps: float = 1e-12) -> float:
    n = len(y_true_class)
    if n == 0:
        raise BonusModelError("multiclass log_loss over zero rows is undefined")
    total = 0.0
    for y, row in zip(y_true_class, p_matrix):
        p = min(max(row[y], eps), 1.0 - eps)
        total += -math.log(p)
    return total / n


def _multiclass_brier(y_true_class: Sequence[int], p_matrix: Sequence[Sequence[float]]) -> float:
    n = len(y_true_class)
    if n == 0:
        raise BonusModelError("multiclass brier over zero rows is undefined")
    total = 0.0
    for y, row in zip(y_true_class, p_matrix):
        for k, p in enumerate(row):
            onehot = 1.0 if k == y else 0.0
            total += (p - onehot) ** 2
    return total / n


@dataclass(frozen=True)
class BonusWalkForwardResult:
    n_folds: int
    n_eval_rows: int
    y_true_class: tuple[int, ...]
    p_model: tuple[tuple[float, ...], ...]
    p_baseline_group_rate: tuple[tuple[float, ...], ...]
    p_baseline_player_trailing: tuple[tuple[float, ...], ...]

    def model_log_loss(self) -> float:
        return _multiclass_log_loss(self.y_true_class, self.p_model)

    def model_brier(self) -> float:
        return _multiclass_brier(self.y_true_class, self.p_model)

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
        multiclass Brier than BOTH baselines. Reports the verdict; does
        not tune to pass it."""
        return (
            self.model_log_loss() < self.baseline_group_rate_log_loss()
            and self.model_log_loss() < self.baseline_player_trailing_log_loss()
            and self.model_brier() < self.baseline_group_rate_brier()
            and self.model_brier() < self.baseline_player_trailing_brier()
        )

    def reliability_for_outcome(self, k: int) -> CalibrationMetrics:
        """Module docstring, "Reliability — required per-outcome": the
        one-vs-rest `P(bonus == k)` series' own full reliability report."""
        if k not in OUTCOMES:
            raise BonusModelError(f"k must be one of {OUTCOMES}, got {k}")
        y_bin = tuple(1 if y == k else 0 for y in self.y_true_class)
        p_bin = tuple(row[k] for row in self.p_model)
        return _calibration_metrics(y_bin, p_bin)


def walk_forward_validate(
    table: pl.DataFrame,
    *,
    min_train_rows: int = 2000,
    n_simulations: int = 4000,
    seed: int = 0,
    config: BonusModelConfig = BonusModelConfig(),
) -> BonusWalkForwardResult:
    """Refits (closed-form) at every `(season, round)` fold using only
    strictly-earlier rows, groups that fold's eval rows by `fixture`
    (module docstring, "Walk-forward gate"), and runs the same Monte Carlo
    machinery `predict_bonus_pmfs_for_fixture` uses with each row's own
    REAL observed `minutes_frac` (a point mass, not a mixture)."""
    if "_chronological_rank" not in table.columns:
        raise BonusModelError("table must carry _chronological_rank -- build it via build_training_table")

    fold_keys = table.select(["season", "round", "_chronological_rank"]).unique().sort("_chronological_rank")
    rng = np.random.default_rng(seed)

    y_true_class: list[int] = []
    p_model: list[tuple[float, ...]] = []
    p_baseline_group: list[tuple[float, ...]] = []
    p_baseline_trailing: list[tuple[float, ...]] = []
    n_folds = 0

    for season, round_, rank in fold_keys.iter_rows():
        train = table.filter(pl.col("_chronological_rank") < rank)
        eval_rows = table.filter(pl.col("_chronological_rank") == rank)
        if train.height < min_train_rows or eval_rows.is_empty():
            continue

        n_folds += 1
        spec = _build_feature_spec(train)
        X_train = _design_matrix(train, spec)
        y_train = train["bps"].cast(pl.Float64).to_numpy()
        beta = _fit_ridge(X_train, y_train, config.l2_penalty)
        residuals = y_train - X_train @ beta

        train_positions = train["position"].to_list()
        residual_pool_by_position: dict[str, np.ndarray] = {}
        for p in POSITIONS:
            idx = [i for i, pp in enumerate(train_positions) if pp == p]
            if idx:
                residual_pool_by_position[p] = residuals[idx]
        residual_pool_all = residuals

        group_rate_table = train.group_by("position").agg(
            *[((pl.col("bonus") == k).sum() / pl.len()).alias(f"rate_{k}") for k in OUTCOMES]
        )
        group_rate_map = {
            row["position"]: tuple(float(row[f"rate_{k}"]) for k in OUTCOMES) for row in group_rate_table.to_dicts()
        }
        overall_rate = tuple(float((train["bonus"] == k).sum()) / train.height for k in OUTCOMES)

        for (season_g, round_g, fixture_id), fixture_group in eval_rows.group_by(["season", "round", "fixture"]):
            n_players = fixture_group.height
            if n_players < 2:
                continue  # cannot rank a field of one -- structurally excluded (module docstring)

            X_eval = _design_matrix(fixture_group, spec)
            mu = X_eval @ beta
            positions_eval = fixture_group["position"].to_list()

            bps_sim = np.zeros((n_players, n_simulations), dtype=np.float64)
            for i, pos in enumerate(positions_eval):
                pool = residual_pool_by_position.get(pos)
                if pool is None or len(pool) == 0:
                    pool = residual_pool_all
                residual_draws = rng.choice(pool, size=n_simulations, replace=True)
                bps_sim[i] = mu[i] + residual_draws

            points_matrix = _assign_bonus_points_batch(np.round(bps_sim.T))  # (n_sims, n_players)
            p_model_rows = [
                tuple(float(np.mean(points_matrix[:, i] == k)) for k in OUTCOMES) for i in range(n_players)
            ]

            actual_bonus = fixture_group["bonus"].to_list()
            trailing_cols_vals = {
                k: fixture_group[f"player_trailing_bonus_class_rate_{k}_5"].to_list() for k in OUTCOMES
            }
            for i in range(n_players):
                pos = positions_eval[i]
                fallback = group_rate_map.get(pos, overall_rate)
                y_true_class.append(int(actual_bonus[i]))
                p_model.append(p_model_rows[i])
                p_baseline_group.append(fallback)

                trailing_row = tuple(
                    float(trailing_cols_vals[k][i]) if trailing_cols_vals[k][i] is not None else fallback[k]
                    for k in OUTCOMES
                )
                total = sum(trailing_row)
                if total > 0:
                    trailing_row = tuple(v / total for v in trailing_row)
                else:
                    trailing_row = fallback
                p_baseline_trailing.append(trailing_row)

    if n_folds == 0:
        raise BonusModelError(f"no usable folds with min_train_rows={min_train_rows}")

    return BonusWalkForwardResult(
        n_folds=n_folds,
        n_eval_rows=len(y_true_class),
        y_true_class=tuple(y_true_class),
        p_model=tuple(p_model),
        p_baseline_group_rate=tuple(p_baseline_group),
        p_baseline_player_trailing=tuple(p_baseline_trailing),
    )


# ---------------------------------------------------------------------------
# Derived-capability registration + persistence
# ---------------------------------------------------------------------------


def _register_bonus_capability() -> FactTableSchema:
    existing = CANONICAL_SCHEMAS.get(PLAYER_BONUS_DISTRIBUTION_GAMEWEEK)
    if existing is not None:
        return existing
    return register_derived_capability(
        PLAYER_BONUS_DISTRIBUTION_GAMEWEEK,
        entity_key=("season", "round", "element", "fixture", "count"),
        value_fields=("probability", "n_simulations"),
        dataset=PLAYER_BONUS_DISTRIBUTION_DATASET,
        description=(
            "Bonus (BPS) estimator (blueprint §4, E5, session s004) -- LONG "
            "format, one row per (player, fixture, count) with its "
            "probability under the fitted BPS regression + empirical "
            "residual bootstrap, composed with a caller-supplied minutes "
            "exposure and coupled across a fixture's whole player pool via "
            "a seeded Monte Carlo simulation of the real competition-rank "
            "award rule (verified against the archive -- see fplai.models."
            "bonus's module docstring). `n_simulations` records the Monte "
            "Carlo draw count this row's probability was estimated from. "
            "Summing `probability` over every `count` for one (season, "
            "round, element, fixture) must equal 1.0."
        ),
    )


BONUS_SCHEMA: FactTableSchema = _register_bonus_capability()


def pmfs_to_rows(pmfs: Sequence[BonusPMF], *, season: str, round_: int) -> pl.DataFrame:
    if not pmfs:
        raise BonusModelError("pmfs_to_rows called with zero PMFs -- nothing to persist")
    frames = [pmf.to_polars() for pmf in pmfs]
    combined = pl.concat(frames)
    return combined.with_columns(pl.lit(season).alias("season"), pl.lit(round_).alias("round"))


def write_bonus_pmfs(
    store: BitemporalStore,
    pmfs: Sequence[BonusPMF],
    *,
    season: str,
    round_: int,
    valid_at: datetime,
    calibration: CalibrationReference,
    source: str = "fplai.models.bonus",
    skip_if_unchanged: bool = True,
) -> WriteResult:
    if not pmfs:
        raise BonusModelError("write_bonus_pmfs called with zero PMFs -- nothing to write")
    rows = pmfs_to_rows(pmfs, season=season, round_=round_)
    derived_from = [
        DerivationInput(
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            entity_key={"season": season},
            note=(
                "trailing per-player and per-team-round BPS totals, aggregated per the feature "
                "engineering in fplai.models.bonus -- whole-season aggregate input, not an "
                "individually-named row subset."
            ),
        )
    ]
    return write_derived(
        store,
        PLAYER_BONUS_DISTRIBUTION_GAMEWEEK,
        rows,
        valid_at=valid_at,
        observed_at=datetime.now(timezone.utc),
        source=source,
        derived_from=derived_from,
        calibration=calibration,
        skip_if_unchanged=skip_if_unchanged,
    )
