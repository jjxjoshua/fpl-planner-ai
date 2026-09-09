"""Goalkeeper saves model — blueprint §4 (goalkeeper points row), §7.1
(calibration before points), §12.2 (derived-fact labelling). Phase 3
prerequisite, session `s005`. The seventh, and last, outcome model —
"the last gap in goalkeeper points" (this task's brief). Structural
precedent: `fplai.models.defensive_contribution` (Negative-Binomial count
regression, minutes exposure offset, truncated-PMF convention) is the
closest sibling for the count machinery; `fplai.models.attacking` is the
precedent for CONSUMING `fplai.models.team_strength` by composition (never
by import) and for a nested double mixture at predict time.

## Why this model exists — the evidence, measured by the Architect before
## authorising the work, restated here so this module's design choices are
## auditable against it, not re-derived from scratch

Saves are 18.9% of all GK points (2,999 of 15,893 across 4,587 GK
appearances, 6 seasons: 2020-21..2025-26; 2019-20 excluded, no
`position`/`team` columns upstream — verified, same gap every sibling model
excludes for the same reason). Ex-post they rarely flip a REALISED
season-ranking (rank-corr 0.944-0.985 with vs without) — that is NOT why
this model exists, because the optimiser ranks on PREDICTED points, not
realised ones. The deciding measurement: pooled `corr(saves/app,
clean_sheets/app) = -0.527` (negative in all six seasons individually), so
a saves-blind estimate removes 0.762 pts/app from bad-defence keepers and
only 0.552 from good-defence keepers — a systematic ~0.21 pts/app
differential that always favours premium keepers on strong defences and
distorts budget allocation across the other 14 squad slots. **This module's
whole job is to price that differential in, not to chase rank-order
correctness at the top of a season table.**

## Data verified live, this session, not assumed from the brief

`saves` is non-null for every row in every one of the 7 seasons in this
store (179,950 rows total) — genuinely complete, unlike `starts`/
`expected_goals`/DC counters, which each have a real season-boundary gap.
`position`/`team` are absent for 2019-20 only (same gap `fplai.models.
minutes`/`fplai.models.cards`/`fplai.models.defensive_contribution` all
document for this exact dataset) — this module therefore trains on
2020-21..2025-26, `position == "GK"`, `minutes > 0`: **4,587 real GK
appearances**, mean 2.964 saves/app, variance 3.792 (var/mean = **1.279**,
skewness **+0.807**) — measured directly, not assumed, and the reason this
module is a Negative Binomial (NB2) rather than a plain Poisson: Poisson's
variance-to-mean ratio is structurally 1.0, and 1.279 is a real, if modest,
departure, positive in every one of the six seasons checked individually
(1.24-1.45), never crossing below 1 (never underdispersed). The near-
Gaussian-placeholder lesson `fplai.models.defensive_contribution`'s own
docstring names (assumed, then measured wrong) is the reason this was
checked rather than inherited — NB2 turns out to be the right family here
too, but on this module's own evidence, not DC's.

**A real, checked, non-blocking anomaly**: 22 of 145,317 non-GK
(`position != "GK"`, `position` non-null) rows carry `saves > 0` — almost
certainly an outfield player who finished a match in goal after a
goalkeeper was sent off or injured with no substitutes remaining, a real
football event FPL's own data does not flag structurally. 0.015% of
non-GK rows; `build_training_table` filters to `position == "GK"` only, so
these rows never enter this module's training population, named here
rather than silently absent from any docstring.

## Opponent attacking strength — CONSUMED from `fplai.models.team_strength`
## by composition, never rebuilt as a parallel team-quality feature

`fplai.models.defensive_contribution`/`fplai.models.cards` each build their
OWN team-trailing-style feature from this store's own player-gameweek
history (`team_trailing_dc_mean_5`/`team_trailing_cards_mean_5`) because
neither module had a fitted team-strength model available as an input at
design time in a form that composes cleanly. **This task's brief is
explicit that this module must not repeat that**: `fplai.models.
team_strength.predict_scoreline` already emits a fixture's full joint
scoreline PMF, and shots-faced (this module's real, unobserved driver of
saves) follows from opponent attacking strength net of this team's own
defensive strength — Dixon-Coles's own parametrisation already blends both
(`mu = exp(attack[opponent] + defence[this_team])`, `fplai.models.
team_strength.predict_scoreline`'s own docstring). Building a second,
independent team-quality proxy here would be exactly the "re-derive team
scoring rates from player data" mistake `fplai.models.attacking`'s own
module docstring was built to avoid, one level removed (there for the
team's OWN goals; here for the OPPONENT's).

**Composition, not import — the same discipline `fplai.models.attacking`
established for `team_strength`/`minutes`, applied here.** This module
imports neither `fplai.models.team_strength` nor `fplai.models.minutes`.
`predict_saves_pmf` takes two plain tuple-sequence mixtures instead:
`minute_exposure: Sequence[tuple[float, float]]` (unchanged convention,
every sibling model) and `opponent_goals_marginal: Sequence[tuple[int,
float]]` — e.g. `list(enumerate(scoreline_pmf.away_goals_marginal()))` if
this GK's team is HOME, `home_goals_marginal()` if AWAY (the marginal over
"how many goals will this team concede", i.e. the OPPONENT's goals in that
fixture from `fplai.models.team_strength.ScorelinePMF`).

**At FIT time**, real historical opponent goals are already known — no
forecast needed, so this module never calls `team_strength` at fit time at
all, mirroring exactly how `fplai.models.attacking` fits its Binomial `n`
from the row's own real `team_h_score`/`team_a_score` rather than a
forecast. `opponent_goals_this_fixture` is built here from the SAME two
columns (`team_a_score`/`team_h_score`, gated on `was_home`) — checked live
this session against `goals_conceded` for every full-90-minute GK
appearance (4,489 rows): **4,488/4,489 match exactly** (one real, named
discrepancy — 2023-24 round 21, fixture 210, likely an own-goal
attribution quirk in the source archive, not investigated further; 0.02%,
not blocking). This near-exact agreement is why `opponent_goals_this_
fixture` (a FIXTURE-level fact, available regardless of how many minutes
this specific GK played, unlike `goals_conceded`, which is a personal
per-appearance stat that only equals the team total for a full 90) is the
feature built here, not `goals_conceded` itself. Measured signal: `corr
(opponent_goals_this_fixture, saves)` = **+0.125** among the 4,587 real GK
appearances — positive, real, modest (a goal conceded is one realised
outcome of many shots faced, most of which do not score, so this is
expected to be a noisy proxy for the true unobserved "shots faced", not a
tight one).

## What this module does NOT claim about the saves / goals-conceded joint
## structure — stated explicitly per this task's brief

`SavesPMF` is a MARGINAL distribution, integrated over whatever
`opponent_goals_marginal` mixture the caller supplied. It is **not** a
joint distribution over (saves, goals conceded this fixture), and this
module does not expose one. Saves and goals conceded are NOT independent —
both rise with shots faced, the entire reason `opponent_goals_this_fixture`
is a feature here at all — but a consumer who independently draws a
clean-sheet/goals-conceded forecast (e.g. directly from `team_strength`'s
own scoreline marginal) and this module's marginal `SavesPMF`, and then
treats the two as independent random variables, will UNDERSTATE the real
coupling: a scenario with an unusually porous defence that game should
carry BOTH more goals conceded AND more saves than the two marginals'
product implies. A caller that needs the coupling can recover it without
this module exposing a new interface: call `predict_saves_pmf` once PER
opponent-goals scenario, passing a DEGENERATE one-point
`opponent_goals_marginal=[(g, 1.0)]` for each `g` in the SAME Monte Carlo
draw used to determine that scenario's goals-conceded/clean-sheet outcome
— the resulting `SavesPMF` is then genuinely CONDITIONAL on that scenario,
and the caller's own sampler carries the joint, not this module. This is
the same escape hatch `fplai.models.defensive_contribution`'s own
`minute_exposure` mixture already permits (pass `[(90.0, 1.0)]` for "player
definitely plays 90") applied to the opponent-goals dimension instead.

## Minutes as an exposure offset — unchanged sibling convention

`offset = log(minutes_this_fixture / 90)`, fixed coefficient 1, never fit —
same reasoning `fplai.models.defensive_contribution`/`fplai.models.cards`
give: saves genuinely scale with time on the pitch, and the count is
structurally non-negative, so a multiplicative offset is the right
mechanism (not an ordinary regression feature).

## Feature design

`NUMERIC_FEATURE_COLUMNS_SAVES` (7):

- `player_trailing_saves_mean_{3,5,10}` — this GK's own trailing per-round
  MEAN save count (not a rate — saves is a count, not a binary event; same
  "player identity is the SECOND input" trailing-feature discipline every
  sibling module uses).
- `games_played_this_season`, `cold_start` — same cold-start discipline
  every sibling module uses.
- `was_home` — a fixture-own column, never rolled up; left as a plain
  feature for the regression to estimate, no asserted sign (same posture
  `fplai.models.cards` takes for its own `was_home`).
- `opponent_goals_this_fixture` — the team-quality signal (see above),
  fit here from the row's own real historical value, supplied at PREDICT
  time via `opponent_goals_marginal`'s mixture rather than the caller's
  `feature_row` (module docstring, "Composition, not import" — mirrors how
  every sibling excludes its own externally-supplied exposure dimension
  from `feature_row`; `predict_saves_pmf` raises if a caller tries to
  smuggle it in directly).

Every numeric feature is standardised (z-score, frozen at fit time,
`SavesFeatureSpec.numeric_means`/`numeric_stds`) — same convention `fplai.
models.cards`/`fplai.models.attacking` use, for the same scale-mismatch
reason (`games_played_this_season` 0..38 against `cold_start` 0..1 against
`opponent_goals_this_fixture` 0..8).

No position one-hot: this module's population is `position == "GK"` only,
a single class, so a position dummy would be a constant column (absorbed
by the intercept) — unlike `fplai.models.cards`, which fits all four
positions in one regression.

## Negative Binomial (NB2), deterministic L-BFGS-B — same machinery as
## `fplai.models.defensive_contribution`, duplicated per this project's
## "no cross-model import" convention (every sibling model states this;
## restated here rather than silently assumed to still apply)

`mu = exp(X @ beta + offset)`, `r = exp(log_r)` (`alpha = 1/r`,
`var = mu + mu**2/r`). Closed-form analytic gradient, cross-checked against
finite differences (`tests/test_saves.py::
test_nb_neg_log_lik_gradient_matches_finite_differences`) — same
"documented derivation AND a numeric check" discipline every sibling
module's own analytic gradient carries.

## Walk-forward gate (blueprint §7.1) — proper scoring rules for an
## ORDINAL COUNT outcome, justified rather than assumed

The outcome space (`0..config.max_count`, truncated and renormalised, same
convention `fplai.models.team_strength.ScorelinePMF`/`fplai.models.
defensive_contribution.DCPMF` use for their own truncated supports) is
genuinely ORDINAL — 6 saves is "closer to" 5 than to 0. Plain multiclass
log-loss/Brier (`fplai.calibration.multiclass_log_loss`/`multiclass_brier`)
score the FULL PMF against the realised count but treat every wrong class
as equally wrong regardless of distance, so this module ALSO reports
`fplai.calibration.ranked_probability_score` (RPS) — Epstein's cumulative-
probability construction, already implemented and reduction-tested against
Brier at K=2 in `fplai.calibration`, and the module docstring there already
names counts as a legitimate ordinal use — as the metric that actually
credits "close" over "far". The §7.1 GATE itself (`beats_both_baselines`)
requires strictly lower multiclass log-loss AND Brier than BOTH the
group-rate and player-trailing baselines (same "two required baselines"
convention `fplai.models.defensive_contribution`/`fplai.models.cards`
establish, restated per this task's brief: the player-trailing baseline
IS `greedy_form` recast as a probability, per the E5 calibration report —
not re-opened here); RPS is reported alongside, not separately gating,
because no proper scoring rule in this codebase gates alone today (§7.1's
own amendment: proper scoring rules reward calibration AND sharpness
jointly, never trustworthiness on their own — this module's actual
trustworthiness gate is the per-threshold reliability below).

**Two baselines, both full count PMFs, not scalars** (rule 5 applies to a
baseline exactly as much as to a model): `p_baseline_group_rate` is the
pooled empirical histogram of `count` over the fold's own TRAINING rows
only (recomputed fresh every fold); `p_baseline_player_trailing` is a
Poisson PMF with `lambda = player_trailing_saves_mean_5` for that row (the
group rate if the player is cold-start) — "just use the trailing mean
directly" is exactly the `greedy_form` posture the E5 report already
established this baseline represents, generalised here from a scalar rate
to a full discretised Poisson shape because the gate needs a PMF to score
log-loss/Brier/RPS against, not a point estimate.

## Per-threshold reliability — the FPL-scoring-relevant binary reduction,
## not an arbitrary cut

FPL awards 1 point per 3 saves (`floor(saves / 3)`, `game_config`'s own
`scoring.saves` supplies the POINTS-PER-UNIT value, 1, live-verified this
session — the DIVISOR itself, 3, is **not present anywhere in `game_config`
's payload**, the same "unpublished constant" situation `fplai.models.
minutes`'s own module docstring documents for its 60-minute appearance
cliff; `SAVES_POINTS_DIVISOR = 3` below is a stated, checked-against-public-
rules constant, not a live-config value, because there is no live-config
value to read). `P(saves >= 3)` (earns >= 1 save point — the primary,
scoring-relevant cut) and `P(saves >= 1)` (any save at all — a secondary,
more common diagnostic) are each evaluated via `fplai.calibration.
evaluate_binary_outcome` (full ECE both binnings, calibration slope with a
95% CI) — same delegation `fplai.models.cards` establishes this session for
exactly this shared machinery, not a sixth local re-derivation.

## Nested out-of-sample calibration — measured, built, tested, and NOT
## shipped by default (a genuine negative result, CLAUDE.md lesson 9)

`walk_forward_validate`'s own measured `P(saves >= 3)` slope IS a real,
usable departure (0.786, 95% CI `[0.606, 0.966]`, excludes 1 in the
OVERCONFIDENT direction — the dangerous direction for an optimiser to
inherit, the same asymmetry the Architect's cards ruling established), so
this module DOES build and test the nested out-of-sample `IsotonicCalibrator`
(duplicated from `fplai.models.minutes`/`fplai.models.cards` — see those
modules' own docstrings for the leakage trap this exists to avoid and the
attack-first proof this module repeats, `tests/test_saves.py::
test_saves_calibrator_fit_directly_on_eval_data_would_leak_and_look_
suspiciously_good`), fit on `P(saves >= 3)` only, via the SAME `inner_train`/
`calib_holdout` split boundary, with a **two-block renormalisation**
generalising `fplai.models.cards`' "hold one class fixed, rescale the rest
proportionally" mechanism to a split point rather than a third category
(the raw model's own conditional SHAPE within `{0,1,2}` and within
`{3,...,max_count}` is each preserved, only the two blocks' relative
totals move to match the calibrated `P(>=3)`).

**It does not ship by default.** Measured on the real training table
(`scripts/fit_saves.py`, two independent `min_train_rows` settings): the
calibrator FAILS `calibrated_not_worse_than_raw()` — pooled log-loss/
Brier/RPS all get slightly worse, and the slope moves FURTHER from 1.0
(0.786 -> 0.739), not closer. `fit_saves_model`'s own docstring states the
full numbers and the most likely cause (this module's 4,587-4,611-row
population is 15-40x smaller than every sibling nested-calibration
precedent, thin enough that a per-fold `calib_holdout` plausibly captures
fold-level noise rather than a stable miscalibration curve). The mechanism
is real, implemented, and attacked (leak-vs-honest + fold-isolation
proofs) — `calibrate=True` is available for a caller who wants it — but
`fit_saves_model`'s DEFAULT is `calibrate=False`, because this module's own
evidence says calibrating makes it worse, not better, on this store's real
data. See `docs/wiki/model-saves.md` for the full before/after table.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import polars as pl
from scipy.optimize import minimize
from scipy.special import digamma, gammaln

from fplai.calibration import (
    CalibrationMetrics,
    IsotonicCalibrator,
    evaluate_binary_outcome,
    fit_isotonic_calibrator as _fit_isotonic_calibrator,
    multiclass_brier,
    multiclass_log_loss,
    ranked_probability_score,
)
from fplai.derived import CalibrationReference, DerivationInput, write_derived
from fplai.gameweek_stats import read_player_gameweek_stats
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    GAME_CONFIG_CURRENT,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    PLAYER_SAVES_DISTRIBUTION_DATASET,
    PLAYER_SAVES_DISTRIBUTION_GAMEWEEK,
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
    "team_a_score",
    "team_h_score",
    "saves",
)

# Module docstring, "Per-threshold reliability" -- verified-unpublished,
# same status fplai.models.minutes.APPEARANCE_POINTS_MINUTE_CLIFF carries
# for its own 60-minute cliff: checked to be ABSENT from game_config's
# payload this session, not a guess.
SAVES_POINTS_DIVISOR = 3


class SavesModelError(ValueError):
    """Misuse or a genuine data problem this module refuses to guess past —
    a naive `as_of`, an empty training window, a PMF that would not
    (re)normalise, a `minute_exposure`/`opponent_goals_marginal` that does
    not sum to 1, or a `feature_row` that smuggles `opponent_goals_this_
    fixture` directly instead of supplying it via the marginal."""


# ---------------------------------------------------------------------------
# Live game_config read -- the points-per-unit VALUE only (module docstring,
# "Per-threshold reliability"). The DIVISOR (3) is not live-readable.
# ---------------------------------------------------------------------------


def read_saves_points_per_unit(store: BitemporalStore) -> int:
    df = store.latest("game_config")
    if df.is_empty():
        raise SavesModelError("game_config is empty in this store -- cannot read the saves points value live.")
    payload = json.loads(df["payload"][0])
    scoring = payload.get("scoring")
    if not isinstance(scoring, dict) or "saves" not in scoring:
        raise SavesModelError(
            "game_config payload has no scoring.saves value -- "
            f"scoring keys: {sorted(scoring.keys()) if isinstance(scoring, dict) else scoring!r}"
        )
    return int(scoring["saves"])


# ---------------------------------------------------------------------------
# Data-shape verification -- re-derived live against whatever store is
# passed in, module docstring "Data verified live". Informational (this
# module's own logic does not depend on these numbers matching exactly),
# unlike fplai.models.cards' hard mutual-exclusion invariant.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SavesDataVerification:
    n_gk_appearances: int
    n_non_gk_rows_with_saves_positive: int
    n_full90_gk_rows_checked: int
    n_opponent_goals_vs_goals_conceded_mismatches: int
    corr_opponent_goals_saves: float


def verify_saves_data_shape_against_archive(store: BitemporalStore, *, as_of: datetime) -> SavesDataVerification:
    """Re-derive, LIVE, the three facts this module's design rests on:
    (1) how many non-GK rows carry a real save (a checked, non-blocking
    anomaly, module docstring), (2) how well `opponent_goals_this_fixture`
    (built from `team_a_score`/`team_h_score`/`was_home`) agrees with a
    full-90 GK's own `goals_conceded`, (3) the raw correlation between the
    two -- the evidence for using `opponent_goals_this_fixture` as a
    feature at all. Never cached or hardcoded; `scripts/fit_saves.py`
    prints this against the real store."""
    if as_of.tzinfo is None:
        raise SavesModelError(f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2.")
    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise SavesModelError(f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()}")

    # Decision 2: attribution_complete=False rows never train (degraded DGW rows)
    raw = raw.filter((pl.col("attribution_complete") == True) | pl.col("attribution_complete").is_null())

    non_gk = raw.filter(pl.col("position").is_not_null() & (pl.col("position") != "GK"))
    n_non_gk_positive = non_gk.filter(pl.col("saves") > 0).height

    gk = raw.filter((pl.col("position") == "GK") & (pl.col("minutes") > 0)).with_columns(
        pl.when(pl.col("was_home")).then(pl.col("team_a_score")).otherwise(pl.col("team_h_score")).alias("opponent_goals_this_fixture")
    )
    n_gk_apps = gk.height
    corr = float(gk.select(pl.corr("opponent_goals_this_fixture", "saves")).item()) if n_gk_apps > 2 else 0.0

    full90 = raw.filter((pl.col("position") == "GK") & (pl.col("minutes") >= 90) & pl.col("goals_conceded").is_not_null())
    full90 = full90.with_columns(
        pl.when(pl.col("was_home")).then(pl.col("team_a_score")).otherwise(pl.col("team_h_score")).alias("opponent_goals_this_fixture")
    )
    mismatches = full90.filter(pl.col("opponent_goals_this_fixture") != pl.col("goals_conceded")).height

    return SavesDataVerification(
        n_gk_appearances=n_gk_apps,
        n_non_gk_rows_with_saves_positive=n_non_gk_positive,
        n_full90_gk_rows_checked=full90.height,
        n_opponent_goals_vs_goals_conceded_mismatches=mismatches,
        corr_opponent_goals_saves=corr,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SavesModelConfig:
    """Every fitting hyperparameter, in one place — same convention every
    sibling module establishes. Defaults are stated, reasoned starting
    points (module docstring cites the real measurements behind the NB2
    choice and the L2/max_count values), not calibrated optima."""

    player_trailing_windows: tuple[int, ...] = (3, 5, 10)
    l2_penalty: float = 1.0
    """Ridge penalty on the NB regression's beta vector, including the
    intercept — the PENALTY TERM ITSELF is scaled by `1/n` inside
    `_nb_neg_log_lik_and_grad` (that function's own docstring: a real,
    measured bug in the shared formula DC's own `_nb_neg_log_lik_and_grad`
    uses unscaled, invisible to DC's binary gate, load-bearing for this
    module's full-multiclass one), so `l2_penalty` here means the same
    thing at any `n` and is NOT directly comparable to `fplai.models.
    defensive_contribution.DCModelConfig.l2_penalty`'s own `1.0` default.
    Swept against this module's real 4,611-row training table: `mu` at the
    feature means moves from 2.968 (l2=10.0) to 2.974 (l2=0.01) against a
    true population mean of 2.965 — every value in that range is within
    0.3% of the true mean, so this is NOT a sensitive hyperparameter once
    the scaling bug is fixed, unlike cards' own genuinely sensitive sweep.
    `1.0` is kept as a modest, real regulariser with no measured downside."""

    log_r_bounds: tuple[float, float] = (-5.0, 8.0)
    """Same bounds `fplai.models.defensive_contribution.DCModelConfig.
    log_r_bounds` uses, for the same numerical-stability reason — kept
    identical rather than re-derived, since both modules fit an NB2 mean
    in a comparable count range (saves mean ~3, DC counts mean ~5-8)."""

    max_lbfgs_iterations: int = 300

    max_count: int = 20
    """PMF truncation (module docstring, "Data verified live"): real
    observed maximum in this store is 13 (4,587 GK appearances,
    2020-21..2025-26). 20 leaves real headroom over that observed maximum
    (the tail above 10 is already thin: 39+9+4+1+1 = 54 of 4,587 rows,
    1.2%) while keeping the PMF small enough that the O(max_count) inner
    loop in `predict_saves_pmf`'s double mixture stays cheap."""

    ge_points_threshold: int = 3
    """`floor(saves/3)` earns a point — module docstring, "Per-threshold
    reliability". The PRIMARY binary reduction this module's gate reports
    slope/ECE against."""

    ge_any_threshold: int = 1
    """A secondary, more common diagnostic cut ("did this keeper make any
    save at all") — not scoring-relevant on its own, but a useful check
    that the model is not just chasing the rarer `>=3` event."""

    calib_holdout_frac: float = 0.2
    """Same role `fplai.models.minutes`/`fplai.models.cards`' own nested-
    calibration inner split uses — the most recent this fraction of a
    fold's own `train` window, by distinct `_chronological_rank`, held out
    for calibrator fitting."""

    min_inner_train_rows: int = 500
    min_calib_holdout_rows: int = 100


# ---------------------------------------------------------------------------
# Raw-row normalisation — same two verified drift cases every sibling
# module documents for this exact dataset, duplicated (not imported).
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


# ---------------------------------------------------------------------------
# Opponent-goals fixture-level feature (module docstring, "Opponent
# attacking strength").
# ---------------------------------------------------------------------------


def _build_opponent_goals_column(raw: pl.DataFrame) -> pl.DataFrame:
    return raw.with_columns(
        pl.when(pl.col("was_home"))
        .then(pl.col("team_a_score"))
        .otherwise(pl.col("team_h_score"))
        .cast(pl.Float64)
        .alias("opponent_goals_this_fixture")
    )


# ---------------------------------------------------------------------------
# Trailing player rollup — round grain, leakage-safe, DGW-safe (mirrors
# fplai.models.defensive_contribution._build_player_round_rollup exactly).
# ---------------------------------------------------------------------------


def _build_player_round_rollup(gk_rows: pl.DataFrame, config: SavesModelConfig) -> pl.DataFrame:
    rollup = (
        gk_rows.group_by(["season", "element", "round"])
        .agg(
            pl.col("saves").sum().alias("round_saves"),
            pl.col("minutes").sum().alias("round_minutes"),
        )
        .sort(["season", "element", "round"])
    )
    appeared = (pl.col("round_minutes") > 0).cast(pl.Float64)

    trailing_exprs = []
    for w in config.player_trailing_windows:
        trailing_exprs.append(
            pl.col("round_saves")
            .cast(pl.Float64)
            .shift(1)
            .rolling_mean(window_size=w, min_samples=1)
            .over(["season", "element"])
            .alias(f"player_trailing_saves_mean_{w}")
        )
    trailing_exprs.append(
        appeared.shift(1).fill_null(0.0).cum_sum().over(["season", "element"]).alias("games_played_this_season")
    )

    rollup = rollup.with_columns(trailing_exprs)
    fill_zero = [f"player_trailing_saves_mean_{w}" for w in config.player_trailing_windows]
    rollup = rollup.with_columns([pl.col(c).fill_null(0.0) for c in fill_zero])
    rollup = rollup.with_columns((pl.col("games_played_this_season") == 0).alias("cold_start"))
    return rollup


NUMERIC_FEATURE_COLUMNS_SAVES: tuple[str, ...] = (
    "player_trailing_saves_mean_3",
    "player_trailing_saves_mean_5",
    "player_trailing_saves_mean_10",
    "games_played_this_season",
    "cold_start",
    "was_home",
    "opponent_goals_this_fixture",
)

# The predict-time `feature_row` shape (module docstring, "Composition, not
# import") -- everything EXCEPT the caller-supplied-via-marginal exposure
# dimension. predict_saves_pmf raises if opponent_goals_this_fixture is
# present in feature_row directly.
PREDICT_FEATURE_ROW_COLUMNS: tuple[str, ...] = tuple(
    c for c in NUMERIC_FEATURE_COLUMNS_SAVES if c != "opponent_goals_this_fixture"
)


# ---------------------------------------------------------------------------
# Training table
# ---------------------------------------------------------------------------


def build_training_table(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: SavesModelConfig = SavesModelConfig(),
    allow_live_season: bool = False,
) -> pl.DataFrame:
    """The full leakage-safe feature+target table, one row per (season,
    round, element, fixture), `position == "GK"`, `minutes > 0` only.
    Reads via the capability reader on `kickoff_time`, same bitemporal
    primitive every sibling module uses for this capability."""
    if as_of.tzinfo is None:
        raise SavesModelError(f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2.")

    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise SavesModelError(f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()}")

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
        raise SavesModelError(f"player gameweek stats is missing required column(s) {missing}")

    raw = _normalise_and_filter_positions(raw)
    raw = raw.filter(pl.col("team").is_not_null())  # excludes 2019-20, explicit not incidental

    if seasons is not None:
        seasons = tuple(seasons)
        raw = raw.filter(pl.col("season").is_in(list(seasons)))
        if raw.is_empty():
            raise SavesModelError(
                f"no player gameweek stats for seasons {seasons!r} at as_of={as_of.isoformat()}. "
                "Note that allow_live_season=False excludes FPL-API-sourced rows BEFORE this "
                "season filter is applied, so a live season requested here resolves to nothing."
            )

    gk_rows = _build_opponent_goals_column(raw.filter(pl.col("position") == "GK"))
    if gk_rows.is_empty():
        raise SavesModelError("no position=='GK' rows remain in the requested window -- nothing to train on")

    player_rollup = _build_player_round_rollup(gk_rows, config)
    trailing_cols = [f"player_trailing_saves_mean_{w}" for w in config.player_trailing_windows] + [
        "games_played_this_season",
        "cold_start",
    ]
    table = gk_rows.join(
        player_rollup.select(["season", "element", "round", *trailing_cols]),
        on=["season", "element", "round"],
        how="left",
    )

    table = table.filter(pl.col("minutes") > 0)
    if table.is_empty():
        raise SavesModelError("no minutes>0 GK rows remain in this training table -- nothing to fit")

    missing_after_join = [c for c in NUMERIC_FEATURE_COLUMNS_SAVES if table[c].null_count() > 0]
    if missing_after_join:
        raise SavesModelError(
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
# Feature spec + design matrix — standardised (module docstring, "Feature
# design"), no position dummy (single-population module).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SavesFeatureSpec:
    numeric_columns: tuple[str, ...]
    numeric_means: tuple[float, ...]
    numeric_stds: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.numeric_means) != len(self.numeric_columns) or len(self.numeric_stds) != len(self.numeric_columns):
            raise SavesModelError("numeric_means/numeric_stds must have exactly one entry per numeric_columns entry")

    @property
    def feature_names(self) -> tuple[str, ...]:
        return ("intercept",) + self.numeric_columns

    @property
    def n_features(self) -> int:
        return len(self.feature_names)

    @property
    def opponent_goals_index(self) -> int:
        """Index into `numeric_columns` (i.e. `beta[1 + this]`) for
        `opponent_goals_this_fixture` — the slot `predict_saves_pmf`
        overwrites per mixture component."""
        return self.numeric_columns.index("opponent_goals_this_fixture")


def _build_feature_spec(table: pl.DataFrame, numeric_columns: tuple[str, ...] = NUMERIC_FEATURE_COLUMNS_SAVES) -> SavesFeatureSpec:
    means: list[float] = []
    stds: list[float] = []
    for c in numeric_columns:
        series = table[c].cast(pl.Float64)
        mean = float(series.mean()) if table.height > 0 else 0.0
        std = float(series.std(ddof=0)) if table.height > 0 else 0.0
        means.append(mean)
        stds.append(std if std > 1e-8 else 1.0)
    return SavesFeatureSpec(numeric_columns=numeric_columns, numeric_means=tuple(means), numeric_stds=tuple(stds))


def _design_matrix(table: pl.DataFrame, spec: SavesFeatureSpec) -> np.ndarray:
    n = table.height
    cols: list[np.ndarray] = [np.ones(n, dtype=np.float64)]
    for c, mean, std in zip(spec.numeric_columns, spec.numeric_means, spec.numeric_stds):
        raw = table[c].cast(pl.Float64).to_numpy()
        cols.append((raw - mean) / std)
    return np.column_stack(cols)


def _feature_row_to_vector(feature_row: dict, spec: SavesFeatureSpec) -> np.ndarray:
    missing = [c for c in spec.numeric_columns if c not in feature_row]
    if missing:
        raise SavesModelError(f"feature_row is missing required feature(s) {missing}")
    row_table = pl.DataFrame({c: [feature_row[c]] for c in spec.numeric_columns})
    return _design_matrix(row_table, spec)[0]


# ---------------------------------------------------------------------------
# Negative Binomial (NB2) regression — duplicated from fplai.models.
# defensive_contribution (module docstring, "no cross-model import").
# ---------------------------------------------------------------------------


def _nb_neg_log_lik_and_grad(
    params: np.ndarray, X: np.ndarray, y: np.ndarray, offset: np.ndarray, n_features: int, l2: float
) -> tuple[float, np.ndarray]:
    """**A real finding, this session, not present in `fplai.models.
    defensive_contribution`'s otherwise-identical formula**: DC's own
    `_nb_neg_log_lik_and_grad` divides the log-likelihood term by `n` (a
    per-row AVERAGE) but adds the L2 penalty `l2 * sum(beta**2)` UNSCALED
    -- for `n` in the thousands, that makes the penalty's effective
    strength relative to the per-row-averaged likelihood roughly `n` times
    the caller's stated `l2`, silently crushing every coefficient
    (including the intercept) toward 0 regardless of the data. Measured
    directly on this module's own real training table (4,611 rows): DC's
    unscaled form at `l2=1.0` (the shared sibling default) fits an
    intercept of 0.4259 (implied mean count at average features, full
    minutes, 1.531) against a REAL population mean of 2.965 -- a factor-
    of-~2 bias, invisible to DC's own gate because DC scores a BINARY
    threshold-crossing reduction of its PMF, which is far less sensitive to
    getting the mean right than this module's own full-multiclass gate is.
    Fixed HERE (this module's own copy only -- `defensive_contribution.py`
    is READ-ONLY in this task's owned paths, so this is a finding to
    escalate, not a bug this module is positioned to fix there) by scaling
    the L2 term (and its gradient) by the SAME `1/n` the likelihood already
    carries, making the two terms commensurate regardless of dataset size —
    verified this restores `mu` at the population mean to within 0.2%
    (`docs/wiki/model-saves.md` has the full before/after sweep)."""
    beta = params[:n_features]
    log_r = params[n_features]
    r = math.exp(log_r)
    eta = np.clip(X @ beta + offset, -30.0, 30.0)
    mu = np.exp(eta)
    n = X.shape[0]

    ll = gammaln(y + r) - gammaln(r) - gammaln(y + 1.0) + r * np.log(r / (r + mu)) + y * np.log(mu / (r + mu))
    nll = -float(np.sum(ll)) / n + (l2 / n) * float(np.sum(beta * beta))

    d_eta = r * (y - mu) / (mu + r)
    grad_beta = -(X.T @ d_eta) / n + 2.0 * (l2 / n) * beta

    dL_dr = digamma(y + r) - digamma(r) + np.log(r) - np.log(r + mu) + 1.0 - (r + y) / (r + mu)
    grad_log_r = -float(np.sum(dL_dr)) / n * r

    grad = np.concatenate([grad_beta, [grad_log_r]])
    return nll, grad


def _fit_negative_binomial(X: np.ndarray, y: np.ndarray, offset: np.ndarray, config: SavesModelConfig) -> tuple[np.ndarray, float]:
    n_features = X.shape[1]
    x0 = np.zeros(n_features + 1, dtype=np.float64)
    x0[-1] = 1.0
    bounds = [(None, None)] * n_features + [config.log_r_bounds]
    result = minimize(
        _nb_neg_log_lik_and_grad,
        x0,
        args=(X, y, offset, n_features, config.l2_penalty),
        jac=True,
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": config.max_lbfgs_iterations},
    )
    if not result.success and result.status not in (0, 1):
        logger.warning("saves model: NB fit did not converge cleanly: %s", result.message)
    beta = result.x[:n_features]
    log_r = float(result.x[n_features])
    return beta, log_r


def _nb_pmf(mu: float, r: float, max_count: int) -> tuple[np.ndarray, float]:
    if mu <= 0:
        pmf = np.zeros(max_count + 1, dtype=np.float64)
        pmf[0] = 1.0
        return pmf, 1.0
    p = r / (r + mu)
    k = np.arange(0, max_count + 1, dtype=np.float64)
    log_pmf = gammaln(k + r) - gammaln(r) - gammaln(k + 1.0) + r * math.log(p) + k * math.log(1.0 - p)
    pmf = np.exp(log_pmf)
    mass = float(pmf.sum())
    if mass <= 0.0:
        raise SavesModelError(f"NB PMF has zero mass within max_count={max_count} for mu={mu}, r={r}")
    return pmf / mass, mass


def _poisson_pmf(lam: float, max_count: int) -> np.ndarray:
    """Discretised Poisson PMF over `0..max_count`, truncated and
    renormalised — used ONLY for the player-trailing baseline (module
    docstring, "Two baselines, both full count PMFs"), never for the model
    itself (which is NB2, module docstring). Duplicated rather than
    imported from `fplai.models.team_strength._poisson_pmf` per this
    project's "no cross-model import" convention."""
    if lam <= 0:
        pmf = np.zeros(max_count + 1, dtype=np.float64)
        pmf[0] = 1.0
        return pmf
    k = np.arange(0, max_count + 1, dtype=np.float64)
    log_pmf = k * math.log(lam) - lam - gammaln(k + 1.0)
    pmf = np.exp(log_pmf)
    mass = float(pmf.sum())
    if mass <= 0.0:
        return np.concatenate(([1.0], np.zeros(max_count, dtype=np.float64)))
    return pmf / mass


# ---------------------------------------------------------------------------
# Nested out-of-sample P(saves >= threshold) calibration -- `IsotonicCalibrator`/
# `_fit_isotonic_calibrator` moved to the shared `fplai.calibration` module
# session s005 (consolidation) -- this was the third independently-drifting
# copy of the same mechanism (`fplai.models.minutes`/`cards` each carried
# their own too), adapted here only for this module's count outcome via the
# rescale helpers below. `_inner_calibration_split` stays HERE, duplicated
# across all three models still -- the out-of-sample NESTING (walk-forward
# fold boundaries) is each model's own responsibility, not the shared
# fitting primitive's. This module's own population (4,587-4,611 rows,
# per-fold `calib_holdout` a few hundred rows) is 15-40x smaller than
# minutes'/cards' -- precisely the regime where homogeneous bins are most
# likely, so `calibrate=False` ships as the default below regardless (see
# `fit_saves_model`'s own docstring) -- see `docs/wiki/model-saves.md`'s
# isotonic-saturation section for the measured counts.
# ---------------------------------------------------------------------------


def _inner_calibration_split(
    train: pl.DataFrame, *, holdout_frac: float, min_holdout_rows: int, min_inner_train_rows: int
) -> tuple[pl.DataFrame, pl.DataFrame] | None:
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


def _p_ge(pmf_row: Sequence[float], threshold: int) -> float:
    return float(sum(pmf_row[threshold:]))


def _apply_ge3_calibration(pmf_row: np.ndarray, calibrated_p_ge3: float, threshold: int) -> np.ndarray:
    """The two-block rescale (module docstring, "Nested out-of-sample
    calibration"): the raw shape WITHIN `{0..threshold-1}` and WITHIN
    `{threshold..max_count}` is each preserved, only the two blocks'
    relative TOTALS move to match `calibrated_p_ge3`. Falls back to the raw
    PMF unchanged if a block's raw mass is exactly 0 (nothing to rescale
    proportionally from)."""
    out = pmf_row.copy()
    head = out[:threshold]
    tail = out[threshold:]
    head_mass = float(head.sum())
    tail_mass = float(tail.sum())
    target_tail = min(max(calibrated_p_ge3, 0.0), 1.0)
    target_head = 1.0 - target_tail
    if head_mass > 1e-12:
        out[:threshold] = head * (target_head / head_mass)
    if tail_mass > 1e-12:
        out[threshold:] = tail * (target_tail / tail_mass)
    total = float(out.sum())
    if total > 1e-12:
        out = out / total
    return out


# ---------------------------------------------------------------------------
# Fitted params + PMF
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class SavesModelParams:
    feature_spec: SavesFeatureSpec
    beta: np.ndarray
    log_r: np.ndarray  # 0-d array, eq=False consistency with sibling convention
    config: SavesModelConfig
    as_of: datetime
    seasons_used: tuple[str, ...]
    n_rows_used: int
    p_ge3_calibrator: IsotonicCalibrator | None = None

    @property
    def r(self) -> float:
        return float(math.exp(float(self.log_r)))


def _fit_from_table(table: pl.DataFrame, *, config: SavesModelConfig) -> tuple[np.ndarray, float, SavesFeatureSpec]:
    spec = _build_feature_spec(table)
    X = _design_matrix(table, spec)
    y = table["saves"].cast(pl.Float64).to_numpy()
    offset = np.log(table["minutes"].cast(pl.Float64).to_numpy() / 90.0)
    beta, log_r = _fit_negative_binomial(X, y, offset, config)
    return beta, log_r, spec


def fit_saves_model(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: SavesModelConfig = SavesModelConfig(),
    calibrate: bool = False,
) -> SavesModelParams:
    """`calibrate=False` is the SHIPPED default, deliberately the opposite
    of `fplai.models.cards.fit_cards_model`'s own `calibrate=True` default
    — module docstring, "Nested out-of-sample calibration". First measured
    session s005 on the real 4,611-row training table, RE-MEASURED later
    the same session after `_fit_isotonic_calibrator`'s own isotonic-
    saturation bug (the plain-sample-mean `bin_y`, shared with `fplai.
    models.minutes`/`fplai.models.cards`, module docstring above) was fixed
    — the original measurement had been taken WITH that bug present, so the
    decision below is re-confirmed on corrected numbers, not merely
    inherited (CLAUDE.md: a decision measured under a known bug must be
    re-opened, not assumed to survive it).

    **The re-measurement is itself a genuine negative, one level deeper**:
    this module's `P(saves >= 3)` calibrator, live-checked at both the eval
    level and the per-bin level across all 152 folds / 3,040 bins, NEVER
    saturated to exactly 0.0 or 1.0 even BEFORE the fix (bin weights 16-46,
    smaller than cards' YELLOW calibrator's own 75-684 range, yet none ever
    homogeneous) — so this module's `calibrate=False` decision never rested
    on the saturation bug at all, and the Jeffreys fix changes essentially
    nothing here: pooled log-loss/Brier/RPS post-fix (1.9930->1.9987 /
    0.8409->0.8432 / 0.0521->0.0523 at min_train_rows=1500) are within noise
    of the pre-fix numbers (1.9930->1.9990 / 0.8409->0.8432 / 0.0521->0.0523),
    `calibrated_not_worse_than_raw()` is still `False`, and the per-threshold
    slope still sits on the wrong side of raw's own (raw 0.786 [0.606,0.966]
    vs calibrated 0.769 [0.572,0.966] post-fix — marginally better than the
    0.739 pre-fix figure, well within the two CIs' overlap, not a real
    reversal). **Decision UNCHANGED, on sound footing now rather than
    coincidentally correct**: `calibrate=False` ships. The raw model's own
    departure IS real and measured (slope 0.786, 95% CI [0.606,0.966],
    excludes 1 — the same overconfident direction the Architect's cards
    ruling treats as the dangerous one) but small relative to cards' own
    NONE/YELLOW slopes, and ECE is already low uncalibrated (0.0113-0.0185
    equal-width, 0.0147-0.0250 quantile across two settings). The most
    likely cause, stated rather than silently worked around: this module's
    real population (4,587-4,611 rows) is roughly 15-40x smaller than every
    sibling model's own nested-calibration precedent (minutes: 113,592;
    cards: 179,950 rows before its own minutes>0 filter) — a per-FOLD
    `calib_holdout` here is a few hundred rows, thin enough that a nested
    isotonic fit is plausibly capturing fold-level noise rather than a
    genuine, stable miscalibration curve; the saturation census confirms
    this is noise from THIN bins generally, not from the one specific
    saturation defect that hit three sibling modules' calibrators this
    session. `calibrate=True` remains fully implemented and tested (leak-
    vs-honest proof, fold-isolation proof, `tests/test_saves.py`) as a real,
    working mechanism a caller can opt into — it is not shipped as the
    default because THIS module's own measurement, twice now, shows it does
    not pass the bar cards' own gate imposes on itself. A negative result is
    the deliverable here (CLAUDE.md lesson 9), not a defect to route
    around."""
    table = build_training_table(store, as_of=as_of, seasons=seasons, config=config)
    beta, log_r, spec = _fit_from_table(table, config=config)

    p_ge3_calibrator: IsotonicCalibrator | None = None
    if calibrate:
        split = _inner_calibration_split(
            table,
            holdout_frac=config.calib_holdout_frac,
            min_holdout_rows=config.min_calib_holdout_rows,
            min_inner_train_rows=config.min_inner_train_rows,
        )
        if split is not None:
            inner_train, calib_holdout = split
            inner_beta, inner_log_r, inner_spec = _fit_from_table(inner_train, config=config)
            inner_r = math.exp(inner_log_r)
            X_holdout = _design_matrix(calib_holdout, inner_spec)
            offset_holdout = np.log(calib_holdout["minutes"].cast(pl.Float64).to_numpy() / 90.0)
            raw_p_ge3 = []
            for i in range(calib_holdout.height):
                eta = float(X_holdout[i] @ inner_beta) + float(offset_holdout[i])
                mu = math.exp(min(eta, 30.0))
                pmf_k, _mass = _nb_pmf(mu, inner_r, config.max_count)
                raw_p_ge3.append(_p_ge(pmf_k, config.ge_points_threshold))
            actual_ge3 = (calib_holdout["saves"].cast(pl.Int64) >= config.ge_points_threshold).cast(pl.Float64).to_numpy()
            p_ge3_calibrator = _fit_isotonic_calibrator(np.array(raw_p_ge3), actual_ge3)
        else:
            logger.warning("saves model: not enough data for a nested inner calibration split -- shipping raw (uncalibrated).")

    return SavesModelParams(
        feature_spec=spec,
        beta=beta,
        log_r=np.array(log_r),
        config=config,
        as_of=as_of,
        seasons_used=tuple(sorted(table["season"].unique().to_list())),
        n_rows_used=table.height,
        p_ge3_calibrator=p_ge3_calibrator,
    )


@dataclass(frozen=True)
class SavesPMF:
    """The model's actual output for one player-fixture: a full count PMF
    over `0..max_count` — never a scalar (CLAUDE.md rule 5). See module
    docstring, "What this module does NOT claim about the saves /
    goals-conceded joint structure" -- this is a MARGINAL, not a joint."""

    element: int
    fixture: int
    counts: tuple[int, ...]
    probabilities: tuple[float, ...]
    mass_before_truncation: float
    calibration_method: str = "raw_uncalibrated"

    def __post_init__(self) -> None:
        if len(self.counts) != len(self.probabilities):
            raise SavesModelError("counts and probabilities must be the same length")
        total = sum(self.probabilities)
        if abs(total - 1.0) > 1e-6:
            raise SavesModelError(f"SavesPMF probabilities do not sum to 1.0 (got {total}) for element={self.element}")

    def expected_count(self) -> float:
        return sum(c * p for c, p in zip(self.counts, self.probabilities))

    def p_at_least(self, threshold: int) -> float:
        return sum(p for c, p in zip(self.counts, self.probabilities) if c >= threshold)

    def expected_save_points(self, points_per_unit: int, *, divisor: int = SAVES_POINTS_DIVISOR) -> float:
        """`sum_k P(saves=k) * floor(k/divisor) * points_per_unit` — a
        CONVENIENCE computed FROM the PMF (CLAUDE.md rule 5), never this
        module's actual interface. `points_per_unit` must be supplied by
        the caller (`read_saves_points_per_unit`, live-read) rather than
        defaulted here, so a season where FPL changes the value cannot
        silently go stale inside this module."""
        return sum(p * (c // divisor) * points_per_unit for c, p in zip(self.counts, self.probabilities))

    def to_polars(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "element": [self.element] * len(self.counts),
                "fixture": [self.fixture] * len(self.counts),
                "count": list(self.counts),
                "probability": list(self.probabilities),
                "calibration_method": [self.calibration_method] * len(self.counts),
            }
        )


def predict_saves_pmf(
    params: SavesModelParams,
    feature_row: dict,
    *,
    element: int,
    fixture: int,
    minute_exposure: Sequence[tuple[float, float]],
    opponent_goals_marginal: Sequence[tuple[int, float]],
    max_count: int | None = None,
) -> SavesPMF:
    """`feature_row` must carry every column in `PREDICT_FEATURE_ROW_
    COLUMNS` and must NOT carry `opponent_goals_this_fixture` (module
    docstring, "Composition, not import" -- raises `SavesModelError` if it
    does, the same "never smuggle the externally-supplied exposure
    dimension into feature_row" discipline every sibling module enforces
    for its own minute_exposure).

    Nested double mixture over `minute_exposure` x `opponent_goals_
    marginal` — same generalisation `fplai.models.attacking.predict_
    attacking_pmf` already establishes for two upstream PMFs, here with an
    NB2 mean instead of a Binomial success probability. Both must sum to
    1.0 (checked). A `minute_exposure` entry of `(0.0, w)` contributes a
    spike at count=0 regardless of `opponent_goals_marginal` (same
    zero-exposure handling `fplai.models.defensive_contribution.predict_
    dc_pmf` uses)."""
    if "opponent_goals_this_fixture" in feature_row:
        raise SavesModelError(
            "feature_row must not carry 'opponent_goals_this_fixture' directly -- supply it via "
            "opponent_goals_marginal (module docstring, 'Composition, not import')."
        )
    total_m_weight = sum(w for _, w in minute_exposure)
    if abs(total_m_weight - 1.0) > 1e-6:
        raise SavesModelError(f"minute_exposure probabilities must sum to 1.0, got {total_m_weight}")
    total_g_weight = sum(w for _, w in opponent_goals_marginal)
    if abs(total_g_weight - 1.0) > 1e-6:
        raise SavesModelError(f"opponent_goals_marginal probabilities must sum to 1.0, got {total_g_weight}")

    spec = params.feature_spec
    base = _feature_row_to_vector({**feature_row, "opponent_goals_this_fixture": 0.0}, spec)
    opp_idx = 1 + spec.opponent_goals_index  # +1 for the intercept slot
    opp_mean = spec.numeric_means[spec.opponent_goals_index]
    opp_std = spec.numeric_stds[spec.opponent_goals_index]

    r = params.r
    resolved_max = max_count if max_count is not None else params.config.max_count
    if resolved_max < 0:
        raise SavesModelError(f"max_count must be >= 0, got {resolved_max}")

    mixture = np.zeros(resolved_max + 1, dtype=np.float64)
    mass_before = 0.0
    for m_val, w_m in minute_exposure:
        if m_val <= 0:
            mixture[0] += w_m
            mass_before += w_m
            continue
        offset = math.log(float(m_val) / 90.0)
        for g_val, w_g in opponent_goals_marginal:
            weight = w_m * w_g
            if weight <= 0.0:
                continue
            x = base.copy()
            x[opp_idx] = (float(g_val) - opp_mean) / opp_std
            eta = float(x @ params.beta) + offset
            mu = math.exp(min(eta, 30.0))
            pmf_k, mass = _nb_pmf(mu, r, resolved_max)
            mixture += weight * pmf_k
            mass_before += weight * mass

    total = float(mixture.sum())
    if total <= 0.0:
        raise SavesModelError(f"predicted saves PMF has zero mass for element={element}, fixture={fixture}")
    mixture = mixture / total

    calibration_method = "raw_uncalibrated"
    if params.p_ge3_calibrator is not None:
        raw_p_ge3 = _p_ge(mixture, params.config.ge_points_threshold)
        calibrated_p_ge3 = float(params.p_ge3_calibrator.apply(np.array([raw_p_ge3]))[0])
        mixture = _apply_ge3_calibration(mixture, calibrated_p_ge3, params.config.ge_points_threshold)
        calibration_method = "isotonic_v1"

    return SavesPMF(
        element=element,
        fixture=fixture,
        counts=tuple(range(resolved_max + 1)),
        probabilities=tuple(float(p) for p in mixture),
        mass_before_truncation=mass_before,
        calibration_method=calibration_method,
    )


# ---------------------------------------------------------------------------
# Walk-forward gate (blueprint §7.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SavesWalkForwardResult:
    n_folds: int
    n_folds_calibrated: int
    max_count: int
    y_true: tuple[int, ...]  # counts, clipped to max_count
    p_model_raw: tuple[tuple[float, ...], ...]
    p_model: tuple[tuple[float, ...], ...]  # == p_model_raw if never calibrated
    p_baseline_group_rate: tuple[tuple[float, ...], ...]
    p_baseline_player_trailing: tuple[tuple[float, ...], ...]

    def model_log_loss(self) -> float:
        return multiclass_log_loss(self.y_true, self.p_model)

    def model_brier(self) -> float:
        return multiclass_brier(self.y_true, self.p_model)

    def model_rps(self) -> float:
        return ranked_probability_score(self.y_true, self.p_model)

    def model_log_loss_raw(self) -> float:
        return multiclass_log_loss(self.y_true, self.p_model_raw)

    def model_brier_raw(self) -> float:
        return multiclass_brier(self.y_true, self.p_model_raw)

    def model_rps_raw(self) -> float:
        return ranked_probability_score(self.y_true, self.p_model_raw)

    def baseline_group_rate_log_loss(self) -> float:
        return multiclass_log_loss(self.y_true, self.p_baseline_group_rate)

    def baseline_group_rate_brier(self) -> float:
        return multiclass_brier(self.y_true, self.p_baseline_group_rate)

    def baseline_group_rate_rps(self) -> float:
        return ranked_probability_score(self.y_true, self.p_baseline_group_rate)

    def baseline_player_trailing_log_loss(self) -> float:
        return multiclass_log_loss(self.y_true, self.p_baseline_player_trailing)

    def baseline_player_trailing_brier(self) -> float:
        return multiclass_brier(self.y_true, self.p_baseline_player_trailing)

    def baseline_player_trailing_rps(self) -> float:
        return ranked_probability_score(self.y_true, self.p_baseline_player_trailing)

    def beats_both_baselines(self) -> bool:
        """The gate, §7.1: strictly lower log-loss AND Brier than BOTH
        baselines, using the CALIBRATED series (== raw if never
        calibrated) — module docstring, "Walk-forward gate"."""
        return (
            self.model_log_loss() < self.baseline_group_rate_log_loss()
            and self.model_log_loss() < self.baseline_player_trailing_log_loss()
            and self.model_brier() < self.baseline_group_rate_brier()
            and self.model_brier() < self.baseline_player_trailing_brier()
        )

    def calibrated_not_worse_than_raw(self) -> bool:
        return self.model_log_loss() <= self.model_log_loss_raw() + 1e-9 and self.model_brier() <= self.model_brier_raw() + 1e-9

    def reliability_for_threshold(self, threshold: int, *, raw: bool = False) -> CalibrationMetrics:
        series = self.p_model_raw if raw else self.p_model
        y_bin = [1 if y >= threshold else 0 for y in self.y_true]
        p_bin = [_p_ge(row, threshold) for row in series]
        return evaluate_binary_outcome(y_bin, p_bin)


def walk_forward_validate(
    table: pl.DataFrame,
    *,
    min_train_rows: int = 500,
    config: SavesModelConfig = SavesModelConfig(),
    calibrate: bool = True,
) -> SavesWalkForwardResult:
    """Refits the NB regression at every `(season, round)` fold, using only
    rows strictly earlier (by `_chronological_rank`) than that fold — same
    walk-forward discipline every sibling module documents. `calibrate=
    True` (default, matching `fplai.models.cards`' newer convention, NOT
    `fplai.models.minutes`' older `False` default — see `docs/HANDOFF.md`
    §3, "walk_forward_validate calibrate default now differs across
    sibling models", a known, named inconsistency this module does not
    resolve) additionally fits a FRESH nested isotonic calibrator inside
    every fold's own `train` window (module docstring, "Nested
    out-of-sample calibration")."""
    if "_chronological_rank" not in table.columns:
        raise SavesModelError("table must carry _chronological_rank -- build it via build_training_table")

    fold_keys = table.select(["season", "round", "_chronological_rank"]).unique().sort("_chronological_rank")

    y_true: list[int] = []
    p_model_raw: list[tuple[float, ...]] = []
    p_model: list[tuple[float, ...]] = []
    p_baseline_group_rate: list[tuple[float, ...]] = []
    p_baseline_player_trailing: list[tuple[float, ...]] = []
    n_folds = 0
    n_folds_calibrated = 0
    max_count = config.max_count

    for season, round_, rank in fold_keys.iter_rows():
        train = table.filter(pl.col("_chronological_rank") < rank)
        eval_rows = table.filter(pl.col("_chronological_rank") == rank)
        if train.height < min_train_rows or eval_rows.is_empty():
            continue

        n_folds += 1
        beta, log_r, spec = _fit_from_table(train, config=config)
        r = math.exp(log_r)

        train_counts = train["saves"].cast(pl.Int64).to_numpy()
        group_pmf = np.bincount(np.minimum(train_counts, max_count), minlength=max_count + 1).astype(np.float64)
        group_pmf = group_pmf / group_pmf.sum()

        calibrator = None
        if calibrate:
            split = _inner_calibration_split(
                train,
                holdout_frac=config.calib_holdout_frac,
                min_holdout_rows=config.min_calib_holdout_rows,
                min_inner_train_rows=config.min_inner_train_rows,
            )
            if split is not None:
                inner_train, calib_holdout = split
                inner_beta, inner_log_r, inner_spec = _fit_from_table(inner_train, config=config)
                inner_r = math.exp(inner_log_r)
                X_h = _design_matrix(calib_holdout, inner_spec)
                offset_h = np.log(calib_holdout["minutes"].cast(pl.Float64).to_numpy() / 90.0)
                raw_p_ge3 = []
                for i in range(calib_holdout.height):
                    eta = float(X_h[i] @ inner_beta) + float(offset_h[i])
                    mu = math.exp(min(eta, 30.0))
                    pmf_k, _mass = _nb_pmf(mu, inner_r, max_count)
                    raw_p_ge3.append(_p_ge(pmf_k, config.ge_points_threshold))
                actual_ge3 = (calib_holdout["saves"].cast(pl.Int64) >= config.ge_points_threshold).cast(pl.Float64).to_numpy()
                calibrator = _fit_isotonic_calibrator(np.array(raw_p_ge3), actual_ge3)
                n_folds_calibrated += 1

        X_eval = _design_matrix(eval_rows, spec)
        offset_eval = np.log(eval_rows["minutes"].cast(pl.Float64).to_numpy() / 90.0)
        eval_counts = eval_rows["saves"].to_list()
        eval_trailing = eval_rows["player_trailing_saves_mean_5"].to_list()

        for i in range(eval_rows.height):
            eta = float(X_eval[i] @ beta) + float(offset_eval[i])
            mu = math.exp(min(eta, 30.0))
            count_this = min(int(eval_counts[i]), max_count)
            y_true.append(count_this)

            pmf_k, _mass = _nb_pmf(mu, r, max_count)
            p_model_raw.append(tuple(float(v) for v in pmf_k))

            if calibrator is not None:
                raw_p_ge3 = _p_ge(pmf_k, config.ge_points_threshold)
                cal_p_ge3 = float(calibrator.apply(np.array([raw_p_ge3]))[0])
                calibrated_pmf = _apply_ge3_calibration(pmf_k, cal_p_ge3, config.ge_points_threshold)
                p_model.append(tuple(float(v) for v in calibrated_pmf))
            else:
                p_model.append(tuple(float(v) for v in pmf_k))

            p_baseline_group_rate.append(tuple(float(v) for v in group_pmf))
            trailing_lambda = eval_trailing[i] if eval_trailing[i] is not None else float(train_counts.mean())
            p_baseline_player_trailing.append(tuple(float(v) for v in _poisson_pmf(trailing_lambda, max_count)))

    if n_folds == 0:
        raise SavesModelError(f"no usable folds with min_train_rows={min_train_rows}")

    return SavesWalkForwardResult(
        n_folds=n_folds,
        n_folds_calibrated=n_folds_calibrated,
        max_count=max_count,
        y_true=tuple(y_true),
        p_model_raw=tuple(p_model_raw),
        p_model=tuple(p_model),
        p_baseline_group_rate=tuple(p_baseline_group_rate),
        p_baseline_player_trailing=tuple(p_baseline_player_trailing),
    )


# ---------------------------------------------------------------------------
# Derived-capability registration + persistence
# ---------------------------------------------------------------------------


def _register_saves_capability() -> FactTableSchema:
    existing = CANONICAL_SCHEMAS.get(PLAYER_SAVES_DISTRIBUTION_GAMEWEEK)
    if existing is not None:
        return existing
    return register_derived_capability(
        PLAYER_SAVES_DISTRIBUTION_GAMEWEEK,
        entity_key=("season", "round", "element", "fixture", "count"),
        value_fields=("probability", "calibration_method"),
        dataset=PLAYER_SAVES_DISTRIBUTION_DATASET,
        description=(
            "Goalkeeper saves estimator (blueprint §4, E5, session s005) -- "
            "LONG format, one row per (player, fixture, count) with its "
            "probability under the fitted Negative Binomial count PMF, "
            "position=='GK' only. `calibration_method` in "
            "('raw_uncalibrated','isotonic_v1') -- STRUCTURAL provenance, "
            "not a docstring promise. Summing `probability` over every "
            "`count` for one (season, round, element, fixture) must equal "
            "1.0. This is a MARGINAL over the caller's own opponent-goals "
            "mixture, not a joint with goals-conceded -- see fplai.models."
            "saves' own module docstring."
        ),
    )


SAVES_SCHEMA: FactTableSchema = _register_saves_capability()


def pmfs_to_rows(pmfs: Sequence[SavesPMF], *, season: str, round_: int) -> pl.DataFrame:
    if not pmfs:
        return pl.DataFrame(
            schema={
                "element": pl.Int64,
                "fixture": pl.Int64,
                "count": pl.Int64,
                "probability": pl.Float64,
                "calibration_method": pl.String,
                "season": pl.String,
                "round": pl.Int64,
            }
        )
    frames = [pmf.to_polars() for pmf in pmfs]
    combined = pl.concat(frames)
    return combined.with_columns(pl.lit(season).alias("season"), pl.lit(round_).alias("round"))


def write_saves_pmfs(
    store: BitemporalStore,
    pmfs: Sequence[SavesPMF],
    *,
    season: str,
    round_: int,
    valid_at: datetime,
    calibration: CalibrationReference,
    source: str = "fplai.models.saves",
    skip_if_unchanged: bool = True,
) -> WriteResult:
    if not pmfs:
        raise SavesModelError("write_saves_pmfs called with zero PMFs -- nothing to write")
    rows = pmfs_to_rows(pmfs, season=season, round_=round_)
    derived_from = [
        DerivationInput(
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            entity_key={"season": season},
            note=(
                "trailing per-GK save counts and this fixture's opponent goals "
                "(team_a_score/team_h_score gated on was_home), aggregated per "
                "the feature engineering in fplai.models.saves -- whole-season "
                "aggregate input, not an individually-named row subset."
            ),
        ),
        DerivationInput(
            capability=GAME_CONFIG_CURRENT,
            entity_key={},
            note="saves points-per-unit value -- singleton, live-read.",
        ),
    ]
    return write_derived(
        store,
        PLAYER_SAVES_DISTRIBUTION_GAMEWEEK,
        rows,
        valid_at=valid_at,
        observed_at=datetime.now(timezone.utc),
        source=source,
        derived_from=derived_from,
        calibration=calibration,
        skip_if_unchanged=skip_if_unchanged,
    )
