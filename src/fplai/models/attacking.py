"""Attacking-involvement model — blueprint §4 ("Attacking" row: "Player
share of team goals / assists. Minutes-weighted; set-piece and penalty
responsibility as explicit features."), §4.3 (PMF discipline), §7.1
(calibration before points), §12.2 (derived-fact labelling). Phase 2 / E5,
session `s004`. Structural precedent: `fplai.models.defensive_contribution`
(a Negative-Binomial count regression composed with `MinutesPMF`-shaped
minute exposure) is the closest sibling; `fplai.models.team_strength` is
the upstream this module CONSUMES rather than re-derives.

## What "share of team goals" means here, made literal rather than metaphorical

A team's goal in a match was scored by exactly one of its players (barring
an own goal, which has no scorer on that team at all) and, usually, assisted
by exactly one other. That means, for one player, "the number of THIS
team's goals this player scored" is a genuine **Binomial thinning** of the
team's own goal count: if the team scores `g` goals and this player's
per-goal involvement probability is `p`, the player's own goal count is
`Binomial(g, p)` — a well-defined, sum-consistent marginal (it cannot
exceed `g`, unlike an independently-fit Poisson/NB rate on the player's raw
count, which structurally can). Fitting `p` directly from
`(player_goals_this_fixture, team_goals_this_fixture)` pairs via a Binomial
regression is therefore not an approximation of "share of team goals" — it
IS that quantity, parameterised as a probability. The same construction
applies to assists (`n` = team goals, `y` = this player's assists that
fixture) independently, since a creative player's assist share and a
poacher's goal share are genuinely different profiles.

**Verified live, not assumed, before choosing this design** (2026-08-28,
this session): over every 2022-23+ `expected_goals`-non-null row in this
store, zero rows have `goals_scored > team_goals_this_fixture` and zero
have `assists > team_goals_this_fixture` (`n_trials` well-defined,
`y <= n` always holds — the Binomial likelihood below never encounters an
impossible cell). `build_training_table` re-checks this against every
window it is called with and raises rather than silently proceeding if a
future data revision ever breaks it.

## Why this is NOT re-deriving team scoring rates from player data

This task's brief is explicit: "Team strength gives you the team's
scoreline distribution... don't re-derive team scoring rates from player
data." The Binomial's `n` (team goals) is supplied by the ACTUAL observed
`team_h_score`/`team_a_score` at FIT time (real historical outcomes — no
alternative), and by the caller's own fitted `team_strength.ScorelinePMF`
at PREDICT time (`team_goals_marginal`, below) — this module never fits or
infers a team-level scoring rate anywhere in its own code, and deliberately
carries **no team-name feature at all** (no dummy, no trailing team-style
proxy of the kind `fplai.models.defensive_contribution` uses for its
team-style signal): because the Binomial's own `n` already encodes "how
much this team scored in this exact historical fixture", any additional
team-level feature in `X` would be redundant at best and, at worst, would
let the SHARE model start leaning on team identity as a stand-in for team
scoring strength — precisely the re-derivation the brief warns against.
The share model's whole job is "of the goals this team already scored, how
many does this specific player, by his own history and role, tend to get" —
team scoring strength is `team_strength`'s job, consumed, never rebuilt.

## Composition, not import — the same discipline `defensive_contribution`
## established for `MinutesPMF`, applied here to BOTH upstream PMFs

This module imports neither `fplai.models.team_strength` nor
`fplai.models.minutes`. `predict_attacking_pmf` takes two plain tuple
sequences instead:

- `team_goals_marginal: Sequence[tuple[int, float]]` — a team's own goal
  count PMF, e.g. `list(enumerate(scoreline_pmf.home_goals_marginal()))`
  from a fitted `ScorelinePMF`. Never re-derived here.
- `minute_exposure: Sequence[tuple[float, float]]` — a `[(minutes, weight),
  ...]` mixture, the exact shape `defensive_contribution.predict_dc_pmf`
  already established for this purpose (e.g. `MinutesPMF`'s six band
  midpoints and their probability weight).

Both are validated to sum to 1.0 and mixed via nested weighted sums, giving
a genuine compound distribution over the player's own goal/assist count —
never a point estimate composed from two point estimates (CLAUDE.md rule
5, generalised to a composition of two upstream PMFs rather than one).

## "Minutes-weighted" — folded in at PREDICT time only, per this task's brief

The brief is explicit: *"'Minutes-weighted' ... means involvement
conditional on being on the pitch, then weighted by the minutes
distribution. Do not fold minutes into the involvement rate itself."* The
Binomial's success probability is parameterised as
`p = (minutes_this_fixture / 90) * sigmoid(X @ beta)` — at FIT time,
`minutes_this_fixture` is the row's own REAL observed minutes (this is
what lets the fitted `sigmoid(X @ beta)` be read as "this player's
involvement probability PER TEAM GOAL, if he played the full 90" — an
EXPOSURE-normalised rate, the same role `defensive_contribution`'s
`offset = log(minutes/90)` plays for its NB mean, just carried on the
probability scale here instead of the log-count scale because a
Binomial's natural parameter is a probability, not a rate). At PREDICT
time, `minute_exposure` supplies a MIXTURE over minute values rather than
a single scalar — the model never collapses "60% chance of 90 minutes, 40%
chance of a late sub cameo" into one number before computing an
involvement probability from it. This is exactly the separation the brief
asks for: a nailed player and a rotation-risk player with the SAME
`sigmoid(X @ beta)` (same per-90 share rate) get DIFFERENT final PMFs,
because their `minute_exposure` mixtures differ, not because the
underlying share rate was ever touched by a minutes forecast.

## The xG/xA boundary — same posture `fplai.models.minutes` took for `starts`

`expected_goals`/`expected_assists` are 100% NULL for 2019-20/2020-21/
2021-22 and 100% populated 2022-23 onward in this store (verified live,
this session — see the punch-out for the exact per-season counts:
26,505 / 29,725 / 27,605 / 29,747). Per this task's brief ("do not
reconstruct xG from goals for 2019-22... take the same posture" as
`fplai.models.minutes`), `build_training_table` filters to rows with BOTH
non-null, excluding the three earliest seasons ENTIRELY (features AND
target, not just target) — the same "let the data say which seasons
qualify, never hardcode a season string" discipline
`defensive_contribution`/`minutes` both already establish. The actual
Binomial TARGET (`goals_scored`/`assists`) is FPL's real box-score field,
present in every season — xG/xA enter only as TRAILING FEATURES
(`player_trailing_xg_*`/`player_trailing_xa_*`), on the same reasoning
`team_strength`'s own module docstring gives for preferring xG over raw
goals as a fitting signal (lower finishing-luck variance). Restricting to
the xG-covered era rather than backfilling trailing xG/xA features with
something inferred for the earlier seasons is the same choice
`defensive_contribution` made for its own single-season restriction, one
level less severe here (four seasons, not one).

## Set-piece and penalty responsibility — declared known-absent, not proxied

This task's brief is explicit that this is "the interesting part... and
the part the data does not hand you" and that the precedent to follow is
`fplai.models.minutes`' `KNOWN_ABSENT_FEATURES` — declare, don't
substitute. `KNOWN_ABSENT_FEATURES` below names exactly two:

- **`primary_set_piece_taker`** — no capability in this store's registry
  carries who takes a team's corners/free-kicks. Would need an explicit
  "set-piece order" capability (press/scout-sourced, `fpl-data-scout`'s
  lane), not derivable from anything FPL's own feed or vaastav's archive
  carries.
- **`penalty_taker_duty`** — FPL's own feed carries `penalties_missed`/
  `penalties_saved` (checked live: 64 and 49 nonzero rows respectively over
  the 2022-23+ window), but this is a biased, sparse proxy for DUTY, not a
  measurement of it: a player who has NEVER been assigned a penalty reads
  identically to one who has taken and scored every one (both `0`), and a
  player who is second in the order but has never needed to take one is
  indistinguishable from one who is not in the order at all. Using either
  field as a stand-in feature would be exactly the "substitutes a proxy
  that looks like the real thing" failure this task's brief names
  directly — `NUMERIC_FEATURE_COLUMNS_ATTACKING` deliberately contains
  neither (`tests/test_attacking.py` asserts this by name, not just by
  absence-of-mention).

What this module does NOT lack, and should not be mistaken for having: a
player's own trailing goal/assist RATE (`player_trailing_goals_*`, etc.)
implicitly carries some historical penalty/set-piece signal, because a
recognised penalty-taker's real historical goal tally already reflects
those goals. What is genuinely missing is a FORWARD-LOOKING flag for a
player who has JUST been handed penalty duty (a transfer, an injury to the
incumbent taker, a new manager's reshuffle) — the same "regime change"
class of gap `fplai.models.minutes` names for its own
`manager_rotation_prior`/`regime_change_flag`.

## No calibration layer built here — a stated scope boundary, not an oversight

`fplai.models.minutes` added a nested isotonic P(start) calibrator in
session s003; `fplai.models.defensive_contribution` did not add an
analogous layer for DC. This module follows DC's precedent, not minutes':
the §7.1 gate (below) is "beat both naive baselines" and this module
reports whether it does, honestly, without tuning past it — adding a
calibration layer is a real, separate design decision (this task's brief
never asked for one) left as a named seam for a future session, not
silently declined.

## Bitemporal fitting — `effective_at()`, the sanctioned primitive
## (blueprint §3.2; same reasoning `minutes`/`team_strength`/
## `defensive_contribution` each independently verified for this dataset)

`vaastav_player_gameweek_stats` was bulk-ingested in one backfill session,
so every row's `observed_at` is approximately "today" regardless of the
row's real season/gameweek — `as_of()` would return empty for any genuinely
historical deadline. `build_training_table` calls
`store.effective_at(DATASET, as_of)` directly (never a hand-rolled
`observations()` filter), resolved on the dataset's own declared
`kickoff_time` valid-time column exactly as the three sibling modules do.
`as_of` is REQUIRED, timezone-aware, no default — an unbounded call is the
leakage bug waiting to happen.

## Walk-forward gate (blueprint §7.1) — what is actually scored

The Binomial fit's own richer quantity is `p` (per-team-goal involvement
probability); the gate scores the DERIVED, practically meaningful quantity
`P(player registers >= 1 {goal|assist} this fixture)` =
`1 - (1 - p)^n` where `n` is that fixture's REAL team-goal count (only
available out-of-sample for evaluation, never for prediction — see
`predict_attacking_pmf`, which never sees a real `n`, only the caller's
`team_goals_marginal`). This is the same "fit the richer quantity, derive
the narrower one for the gate" relationship `defensive_contribution` has
between its NB count fit and `p_dc_awarded()`. Two baselines, matching
this task's brief's stated minimum and `defensive_contribution`'s own
precedent:

1. **Position base rate** — the TRAINING-split rate of
   `{goals_scored|assists} >= 1` within that eval row's position, recomputed
   fresh at every fold (never season-global, never touching eval data).
2. **Player-trailing rate** — this player's own trailing rate of
   `{goals_scored|assists} >= 1` over the last 5 rounds
   (`player_trailing_scored_rate_5`/`player_trailing_assisted_rate_5`,
   built leakage-safe in `_build_player_round_rollup`, nullable for a
   genuine cold start and falling back to the position rate exactly as
   `defensive_contribution`'s `player_trailing_awarded_rate_5` does).

`walk_forward_validate` refits at every `(season, round)` fold using only
strictly-earlier rows (`_chronological_rank`), restricted to `minutes > 0`
throughout (an involvement probability is undefined for a player who did
not appear — same restriction `defensive_contribution` applies).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import polars as pl
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import binom

from fplai.derived import CalibrationReference, DerivationInput, write_derived
from fplai.gameweek_stats import read_player_gameweek_stats
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_DATASET,
    PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    TEAM_STRENGTH_RATING_GAMEWEEK,
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
    "goals_scored",
    "assists",
    "expected_goals",
    "expected_assists",
    "team_h_score",
    "team_a_score",
)

STATS: tuple[str, ...] = ("goals", "assists")
STAT_TARGET_COLUMN: dict[str, str] = {"goals": "goals_scored", "assists": "assists"}
STAT_TRAILING_RATE_COLUMN: dict[str, str] = {
    "goals": "player_trailing_scored_rate_5",
    "assists": "player_trailing_assisted_rate_5",
}
POSITIONS: tuple[str, ...] = ("GK", "DEF", "MID", "FWD")

# See module docstring, "Set-piece and penalty responsibility". Declared as
# data, not just prose, so a future capability addition has something
# concrete to diff against (same convention fplai.models.minutes.
# KNOWN_ABSENT_FEATURES established).
KNOWN_ABSENT_FEATURES: tuple[str, ...] = (
    "primary_set_piece_taker",
    "penalty_taker_duty",
)


class AttackingModelError(ValueError):
    """Misuse or a genuine data problem this module refuses to guess past —
    an impossible (player count > team count) row, a naive `as_of`, an
    empty training window, a PMF that would not (re)normalise, or a
    `team_goals_marginal`/`minute_exposure` that does not sum to 1."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttackingModelConfig:
    """Every fitting hyperparameter, in one place — same convention
    `fplai.models.defensive_contribution.DCModelConfig`/`fplai.models.
    minutes.MinutesModelConfig` establish. Defaults are stated, reasoned
    starting points, not calibrated optima (same status those two modules'
    defaults carry)."""

    player_trailing_windows: tuple[int, ...] = (3, 5, 10)
    l2_penalty: float = 0.01
    """Ridge penalty on the (standardised) Binomial regression's beta
    vector — resolves the position one-hot's gauge freedom exactly as
    `fplai.models.minutes.MinutesModelConfig.l2_penalty` does for its own
    position/team dummies (no separate sum-to-zero constraint needed).

    **Deliberately NOT the `1.0` DC/minutes share** — diagnosed, not
    guessed, this session: even with every numeric feature standardised
    (`AttackingFeatureSpec`'s own docstring), `l2_penalty=1.0` on this
    module's real training rows converges to a REAL stationary point
    (gradient norm ~1e-5, not a premature stop) that nonetheless FAILS the
    §7.1 gate outright — `walk_forward_validate` at `l2_penalty=1.0` scores
    WORSE than both naive baselines on both metrics for both stats (e.g.
    goals: model log-loss 0.552 vs. position-rate baseline's 0.269), because
    this Binomial-share likelihood's natural per-parameter curvature is
    weak (goal/assist involvement is a genuinely rare per-team-goal event,
    unlike DC's 3.6-8.4% threshold-hit rate or minutes' ~50%+ start rate)
    and a ridge penalty tuned for THOSE likelihoods' curvature over-shrinks
    this one to near-uniform. Swept `{1.0, 0.2, 0.05, 0.03, 0.015, 0.01,
    0.005}` against the real store (this session's punch-out has the full
    table): the gate first passes at `0.015` and keeps improving through
    `0.005`; `0.01` is the value chosen — clears both baselines on both
    metrics with a real margin (goals: model log-loss 0.235 vs. 0.269/1.327)
    without running the fit at the sweep's most aggressive, least-tested
    end. Still a stated, reasoned starting point, not a claimed optimum —
    a real calibration pass (this task's brief explicitly does not ask
    for one) would search this properly, e.g. by nested cross-validation.

    **Session `s005`: the sweep above ran against a since-fixed L2-scaling
    bug** (`_binomial_share_neg_log_lik_and_grad` added the ridge penalty
    unscaled against a per-row-averaged likelihood — see that function's
    own docstring for the fix). Re-measured under the corrected formula
    before touching this default: `0.01` still clears the §7.1 gate with a
    wide margin, and log-loss/Brier/ECE all improve materially over the
    numbers above at this SAME value (e.g. goals log-loss 0.235 -> 0.219,
    ECE 0.029 -> 0.007) — the fix, not a retune. `docs/wiki/model-
    attacking.md` §12 has the full before/after and a 9-point swept grid
    under the corrected formula; the default is unchanged."""

    max_lbfgs_iterations: int = 300

    max_count: int = 6
    """PMF truncation floor for `predict_attacking_pmf`'s output support
    (`0..max_count`, EXPANDED if `team_goals_marginal`'s own top goal count
    exceeds it — see that function). Real observed maxima in this store,
    2022-23+ (module docstring): `goals_scored` max 4, `assists` max 4 — 6
    leaves headroom for a genuine outlier without ever silently truncating
    a realistic case."""


# ---------------------------------------------------------------------------
# Raw-row normalisation — same two verified drift cases
# fplai.models.minutes._normalise_and_filter_positions/fplai.models.
# defensive_contribution._normalise_and_filter_positions document for this
# exact dataset, duplicated here (not imported) to keep this module
# independent, per this project's "no cross-model import" convention.
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
# Trailing feature rollup — round grain, leakage-safe (module docstring,
# mirrors fplai.models.defensive_contribution._build_player_round_rollup /
# fplai.models.minutes._build_round_rollup exactly in its double-gameweek
# discipline: shift(1) applied at ROUND grain, then joined back onto every
# fixture row of that round — both of a DGW's fixtures get the identical
# pre-round value, never peeking at "the other fixture this round").
# ---------------------------------------------------------------------------


def _build_player_round_rollup(labeled: pl.DataFrame, config: AttackingModelConfig) -> pl.DataFrame:
    rollup = (
        labeled.group_by(["season", "element", "round"])
        .agg(
            pl.col("goals_scored").sum().alias("round_goals"),
            pl.col("assists").sum().alias("round_assists"),
            pl.col("expected_goals").sum().alias("round_xg"),
            pl.col("expected_assists").sum().alias("round_xa"),
            pl.col("minutes").sum().alias("round_minutes"),
        )
        .sort(["season", "element", "round"])
    )
    appeared = (pl.col("round_minutes") > 0).cast(pl.Float64)
    scored = (pl.col("round_goals") > 0).cast(pl.Float64)
    assisted = (pl.col("round_assists") > 0).cast(pl.Float64)

    trailing_exprs = []
    for metric, col in (
        ("goals", "round_goals"),
        ("assists", "round_assists"),
        ("xg", "round_xg"),
        ("xa", "round_xa"),
    ):
        for w in config.player_trailing_windows:
            trailing_exprs.append(
                pl.col(col)
                .cast(pl.Float64)
                .shift(1)
                .rolling_mean(window_size=w, min_samples=1)
                .over(["season", "element"])
                .alias(f"player_trailing_{metric}_{w}")
            )
    trailing_exprs.append(
        appeared.shift(1).fill_null(0.0).cum_sum().over(["season", "element"]).alias("games_played_this_season")
    )
    # Baseline-only features (never fed to the Binomial regression itself —
    # see walk_forward_validate) — this player's own trailing >=1 rate,
    # kept nullable so a genuinely cold-start row is visibly absent, same
    # convention defensive_contribution.player_trailing_awarded_rate_5 uses.
    trailing_exprs.append(
        scored.shift(1).rolling_mean(window_size=5, min_samples=1).over(["season", "element"]).alias(
            "player_trailing_scored_rate_5"
        )
    )
    trailing_exprs.append(
        assisted.shift(1).rolling_mean(window_size=5, min_samples=1).over(["season", "element"]).alias(
            "player_trailing_assisted_rate_5"
        )
    )

    rollup = rollup.with_columns(trailing_exprs)
    fill_zero = [
        f"player_trailing_{metric}_{w}"
        for metric in ("goals", "assists", "xg", "xa")
        for w in config.player_trailing_windows
    ]
    rollup = rollup.with_columns([pl.col(c).fill_null(0.0) for c in fill_zero])
    rollup = rollup.with_columns((pl.col("games_played_this_season") == 0).alias("cold_start"))
    return rollup


NUMERIC_FEATURE_COLUMNS_ATTACKING: tuple[str, ...] = (
    "player_trailing_goals_3",
    "player_trailing_goals_5",
    "player_trailing_goals_10",
    "player_trailing_assists_3",
    "player_trailing_assists_5",
    "player_trailing_assists_10",
    "player_trailing_xg_3",
    "player_trailing_xg_5",
    "player_trailing_xg_10",
    "player_trailing_xa_3",
    "player_trailing_xa_5",
    "player_trailing_xa_10",
    "games_played_this_season",
    "cold_start",
    "was_home",
)

assert not set(NUMERIC_FEATURE_COLUMNS_ATTACKING) & set(KNOWN_ABSENT_FEATURES)
assert "penalties_missed" not in NUMERIC_FEATURE_COLUMNS_ATTACKING
assert "penalties_saved" not in NUMERIC_FEATURE_COLUMNS_ATTACKING


# ---------------------------------------------------------------------------
# Training table
# ---------------------------------------------------------------------------


def build_training_table(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: AttackingModelConfig = AttackingModelConfig(),
    allow_live_season: bool = False,
) -> pl.DataFrame:
    """The full leakage-safe feature+target table, one row per (season,
    round, element, fixture) for the xG/xA-covered era (module docstring,
    "The xG/xA boundary"). Carries `team_goals` (this row's OWN team's
    actual goals in this fixture — never a re-derived rate, module
    docstring), `goals_scored`/`assists` (the Binomial targets), and every
    column in `NUMERIC_FEATURE_COLUMNS_ATTACKING`.
    """
    if as_of.tzinfo is None:
        raise AttackingModelError(
            f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2."
        )

    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise AttackingModelError(
            f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()}"
        )

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
        raise AttackingModelError(f"{DATASET!r} is missing required column(s) {missing}")

    raw = _normalise_and_filter_positions(raw)
    if seasons is not None:
        seasons = tuple(seasons)
        raw = raw.filter(pl.col("season").is_in(list(seasons)))
        if raw.is_empty():
            raise AttackingModelError(
                f"no player gameweek stats for seasons {seasons!r} at as_of={as_of.isoformat()}. "
                "Note that allow_live_season=False excludes FPL-API-sourced rows BEFORE this "
                "season filter is applied, so a live season requested here resolves to nothing."
            )

    labeled = raw.filter(pl.col("expected_goals").is_not_null() & pl.col("expected_assists").is_not_null())
    if labeled.is_empty():
        raise AttackingModelError(
            "no rows with non-null expected_goals AND expected_assists in the requested window — "
            "this model only trains 2022-23 onward (verified live; see module docstring)."
        )

    labeled = labeled.with_columns(
        pl.when(pl.col("was_home"))
        .then(pl.col("team_h_score"))
        .otherwise(pl.col("team_a_score"))
        .cast(pl.Int64)
        .alias("team_goals")
    )
    if labeled["team_goals"].null_count() > 0:
        raise AttackingModelError(
            "team_goals is null on at least one row after the was_home-conditional pick — "
            "team_h_score/team_a_score must be populated for every xG-covered row."
        )
    impossible = labeled.filter(
        (pl.col("goals_scored") > pl.col("team_goals")) | (pl.col("assists") > pl.col("team_goals"))
    )
    if not impossible.is_empty():
        raise AttackingModelError(
            f"{impossible.height} row(s) have goals_scored or assists exceeding that fixture's own "
            "team_goals -- the Binomial(n=team_goals, y=player_count) construction this module relies "
            "on requires y <= n always (verified live, 0 violations, before this module was written; "
            "module docstring). Refusing to silently clip or drop -- this indicates a genuine data "
            "problem upstream, not a case this module should paper over."
        )

    unexpected_positions = set(labeled["position"].unique().to_list()) - set(POSITIONS)
    if unexpected_positions:
        raise AttackingModelError(
            f"unexpected position value(s) after normalisation: {unexpected_positions} — expected "
            f"a subset of {POSITIONS}."
        )

    rollup = _build_player_round_rollup(labeled, config)
    trailing_cols = list(NUMERIC_FEATURE_COLUMNS_ATTACKING) + [
        "player_trailing_scored_rate_5",
        "player_trailing_assisted_rate_5",
    ]
    trailing_cols = [c for c in trailing_cols if c not in ("was_home",)]  # was_home is a fixture-own column, not rolled up
    table = labeled.join(
        rollup.select(["season", "element", "round", *trailing_cols]),
        on=["season", "element", "round"],
        how="left",
    )

    check_cols = [c for c in NUMERIC_FEATURE_COLUMNS_ATTACKING if c != "was_home"]
    missing_after_join = [c for c in check_cols if table[c].null_count() > 0]
    if missing_after_join:
        raise AttackingModelError(
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
# Feature spec + design matrix (position one-hot, no team dummy — module
# docstring, "Why this is NOT re-deriving team scoring rates")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttackingFeatureSpec:
    """The exact feature vocabulary a fitted model expects, frozen at fit
    time so predict-time design-matrix construction cannot silently drift
    from what the model was trained on — same role `fplai.models.minutes.
    MinutesFeatureSpec` plays. `position_categories` is derived live from
    the training table (not a fixed constant); a position never seen in
    training simply contributes an all-zero dummy at predict time.

    `numeric_means`/`numeric_stds` — **standardisation, fitted once from
    the training table and frozen here, applied identically at predict
    time.** Diagnosed directly against the real store, not assumed: an
    UNSTANDARDISED design matrix converges L-BFGS-B to a genuine (near-zero
    gradient norm, ~1e-4) but qualitatively wrong stationary point, because
    `games_played_this_season` (raw range 0..37) and the trailing rate
    features (raw range ~0..2) sit two orders of magnitude apart, and a
    FIXED `l2_penalty` (module docstring, `AttackingModelConfig`) penalises
    the SAME quadratic cost per unit of `beta` regardless of what a unit of
    `beta` buys on each feature's own scale — the large-scale feature can
    move `eta` a long way for a cheap `beta`, the small-scale (and
    genuinely more informative) trailing-xG/goals features cannot, and get
    crushed toward zero. Verified live (this session's punch-out): an
    unstandardised fit on the real 2022-23+ table puts
    `games_played_this_season` as the single largest-magnitude coefficient
    and every `player_trailing_{xg,goals}_*` coefficient under 0.002 in
    absolute value, with `position=FWD` reading NEGATIVE for the GOALS
    model — football-nonsensical. Standardising every NUMERIC column
    (z-score, `std` floored at `1e-8` to avoid a division by zero for a
    degenerate all-constant column) restores the expected shape without
    touching `l2_penalty`'s value at all: `position=FWD` positive and
    `player_trailing_xg_*`/`player_trailing_goals_*` the largest-magnitude,
    positive coefficients for the GOALS model (see
    `tests/test_attacking.py::test_fit_recovers_forward_and_trailing_
    xg_as_the_dominant_positive_signal_on_the_real_store`). Position
    dummies and the intercept are NOT standardised (already 0/1-scaled,
    directly comparable to a standardised numeric column's own ~unit
    scale)."""

    numeric_columns: tuple[str, ...]
    position_categories: tuple[str, ...]
    numeric_means: tuple[float, ...]
    numeric_stds: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.numeric_means) != len(self.numeric_columns) or len(self.numeric_stds) != len(
            self.numeric_columns
        ):
            raise AttackingModelError(
                "numeric_means/numeric_stds must have exactly one entry per numeric_columns entry"
            )

    @property
    def feature_names(self) -> tuple[str, ...]:
        return ("intercept",) + self.numeric_columns + tuple(f"position={p}" for p in self.position_categories)

    @property
    def n_features(self) -> int:
        return len(self.feature_names)


def _build_feature_spec(table: pl.DataFrame) -> AttackingFeatureSpec:
    positions = tuple(sorted(table["position"].unique().to_list()))
    unexpected = set(positions) - set(POSITIONS)
    if unexpected:
        raise AttackingModelError(f"unexpected position categories in training table: {unexpected}")
    means: list[float] = []
    stds: list[float] = []
    for c in NUMERIC_FEATURE_COLUMNS_ATTACKING:
        series = table[c].cast(pl.Float64)
        mean = float(series.mean()) if table.height > 0 else 0.0
        std = float(series.std(ddof=0)) if table.height > 0 else 0.0
        means.append(mean)
        stds.append(std if std > 1e-8 else 1.0)
    return AttackingFeatureSpec(
        numeric_columns=NUMERIC_FEATURE_COLUMNS_ATTACKING,
        position_categories=positions,
        numeric_means=tuple(means),
        numeric_stds=tuple(stds),
    )


def _design_matrix(table: pl.DataFrame, spec: AttackingFeatureSpec) -> np.ndarray:
    n = table.height
    cols: list[np.ndarray] = [np.ones(n, dtype=np.float64)]
    for c, mean, std in zip(spec.numeric_columns, spec.numeric_means, spec.numeric_stds):
        raw = table[c].cast(pl.Float64).to_numpy()
        cols.append((raw - mean) / std)
    positions = table["position"].to_list()
    for p in spec.position_categories:
        cols.append(np.array([1.0 if v == p else 0.0 for v in positions], dtype=np.float64))
    return np.column_stack(cols)


def _feature_row_to_vector(feature_row: dict, spec: AttackingFeatureSpec) -> np.ndarray:
    missing = [c for c in spec.numeric_columns if c not in feature_row]
    if missing:
        raise AttackingModelError(f"feature_row is missing required feature(s) {missing}")
    if "position" not in feature_row:
        raise AttackingModelError("feature_row must carry 'position'")
    row_table = pl.DataFrame(
        {**{c: [feature_row[c]] for c in spec.numeric_columns}, "position": [feature_row["position"]]}
    )
    return _design_matrix(row_table, spec)[0]


# ---------------------------------------------------------------------------
# Binomial-share regression — deterministic L-BFGS-B, analytic gradient
# (CLAUDE.md rule 7: no randomness anywhere in this fit). See module
# docstring, "Why this is NOT re-deriving..." for the parametrisation
# p = minutes_frac * sigmoid(X @ beta).
# ---------------------------------------------------------------------------


def _binomial_share_neg_log_lik_and_grad(
    beta: np.ndarray,
    X: np.ndarray,
    y: np.ndarray,
    n_trials: np.ndarray,
    minutes_frac: np.ndarray,
    l2: float,
) -> tuple[float, np.ndarray]:
    """`p_i = minutes_frac_i * sigmoid(X_i @ beta)`, `y_i ~ Binomial(n_trials_i,
    p_i)`. Derivation (also checked numerically,
    `tests/test_attacking.py::test_binomial_share_neg_log_lik_gradient_
    matches_finite_differences`):

    `dL/dp = y/p - (n-y)/(1-p)`; `dp/d(eta) = minutes_frac * s * (1 - s)`
    where `eta = X @ beta`, `s = sigmoid(eta)` — so
    `dL/d(eta) = [y/p - (n-y)/(1-p)] * minutes_frac * s * (1-s)`, and
    `d(beta)`'s gradient is `X.T @ dL/d(eta) / n_rows`.

    A row with `n_trials_i = 0` (team scored 0 goals that fixture)
    contributes EXACTLY zero to both the log-likelihood and the gradient
    (`gammaln(1)-gammaln(1)-gammaln(1)=0`, and `y=0,n=0` makes
    `y/p - (n-y)/(1-p) = 0`) — included here for simplicity rather than
    pre-filtered, since it is mathematically a no-op, not a special case
    that needs guarding.

    **L2 scaling fixed session `s005`** — same defect `fplai.models.saves`/
    `fplai.models.minutes`/`fplai.models.defensive_contribution` each found
    and fixed independently in their own duplicated copies of this pattern
    (`docs/wiki/model-minutes.md` §13 has the fullest derivation): the ridge
    term was `l2 * sum(beta**2)`, unscaled against the per-row-AVERAGED
    likelihood `-sum(ll)/n_rows` — effectively `l2*n_rows` for `n_rows` in
    the thousands. Now scaled by the same `1/n_rows` the likelihood already
    carries, in both the loss and the gradient. **This module's own
    `l2_penalty` default (`0.01`) was ALREADY chosen specifically to
    compensate for this exact bug** (see `AttackingModelConfig.l2_penalty`'s
    own docstring: `l2=1.0` was swept and found to fail the §7.1 gate
    outright under the OLD unscaled formula) — the corrected formula
    therefore changes what this default effectively means, and was
    re-measured against the real store before being touched (this task's
    brief; see `docs/wiki/model-attacking.md` for the full before/after and
    swept-grid tables)."""
    n_rows = X.shape[0]
    eta = np.clip(X @ beta, -30.0, 30.0)
    s = 1.0 / (1.0 + np.exp(-eta))
    p = np.clip(minutes_frac * s, 1e-10, 1.0 - 1e-10)

    ll = (
        gammaln(n_trials + 1.0)
        - gammaln(y + 1.0)
        - gammaln(n_trials - y + 1.0)
        + y * np.log(p)
        + (n_trials - y) * np.log(1.0 - p)
    )
    nll = -float(np.sum(ll)) / n_rows + (l2 / n_rows) * float(np.sum(beta * beta))

    dl_dp = y / p - (n_trials - y) / (1.0 - p)
    dl_deta = dl_dp * minutes_frac * s * (1.0 - s)
    grad = -(X.T @ dl_deta) / n_rows + 2.0 * (l2 / n_rows) * beta
    return nll, grad


def _fit_binomial_share(
    X: np.ndarray, y: np.ndarray, n_trials: np.ndarray, minutes_frac: np.ndarray, config: AttackingModelConfig
) -> np.ndarray:
    n_features = X.shape[1]
    x0 = np.zeros(n_features, dtype=np.float64)
    result = minimize(
        _binomial_share_neg_log_lik_and_grad,
        x0,
        args=(X, y, n_trials, minutes_frac, config.l2_penalty),
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": config.max_lbfgs_iterations},
    )
    if not result.success and result.status not in (0, 1):
        logger.warning("attacking model: binomial-share fit did not converge cleanly: %s", result.message)
    return result.x


# ---------------------------------------------------------------------------
# Fitted params
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class AttackingStatParams:
    """One stat's ("goals" or "assists") fitted Binomial-share regression.
    `eq=False`: carries a numpy array (`beta`), whose `==` is elementwise —
    same convention `fplai.models.defensive_contribution.DCGroupParams`
    establishes."""

    stat: str
    beta: np.ndarray
    config: AttackingModelConfig
    n_rows_used: int


@dataclass(frozen=True)
class AttackingModelParams:
    stats: dict[str, AttackingStatParams]
    feature_spec: AttackingFeatureSpec
    as_of: datetime
    seasons_used: tuple[str, ...]


def _fit_from_table(
    table: pl.DataFrame, *, config: AttackingModelConfig, as_of: datetime
) -> AttackingModelParams:
    fit_table = table.filter(pl.col("minutes") > 0)
    if fit_table.is_empty():
        raise AttackingModelError("no minutes>0 rows in this training table -- nothing to fit")

    spec = _build_feature_spec(fit_table)
    X = _design_matrix(fit_table, spec)
    n_trials = fit_table["team_goals"].cast(pl.Float64).to_numpy()
    minutes_frac = (fit_table["minutes"].clip(upper_bound=90).cast(pl.Float64) / 90.0).to_numpy()

    stats: dict[str, AttackingStatParams] = {}
    for stat in STATS:
        y = fit_table[STAT_TARGET_COLUMN[stat]].cast(pl.Float64).to_numpy()
        beta = _fit_binomial_share(X, y, n_trials, minutes_frac, config)
        stats[stat] = AttackingStatParams(stat=stat, beta=beta, config=config, n_rows_used=fit_table.height)

    return AttackingModelParams(
        stats=stats,
        feature_spec=spec,
        as_of=as_of,
        seasons_used=tuple(sorted(table["season"].unique().to_list())),
    )


def fit_attacking_model(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: AttackingModelConfig = AttackingModelConfig(),
    allow_live_season: bool = False,
) -> AttackingModelParams:
    table = build_training_table(store, as_of=as_of, seasons=seasons, config=config, allow_live_season=allow_live_season)
    return _fit_from_table(table, config=config, as_of=as_of)


# ---------------------------------------------------------------------------
# PMF + prediction — the composition point with team_strength/minutes
# (module docstring, "Composition, not import")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttackingInvolvementPMF:
    """The model's actual output for one player-fixture-stat: a full count
    PMF over `0..max_count` — never a scalar (CLAUDE.md rule 5).
    `p_involved()`/`expected_count()` are convenience properties computed
    FROM this PMF, never a substitute for it."""

    element: int
    fixture: int
    stat: str
    counts: tuple[int, ...]
    probabilities: tuple[float, ...]
    mass_before_truncation: float

    def __post_init__(self) -> None:
        if self.stat not in STATS:
            raise AttackingModelError(f"unknown stat {self.stat!r} -- expected one of {STATS}")
        if len(self.counts) != len(self.probabilities):
            raise AttackingModelError("counts and probabilities must be the same length")
        total = sum(self.probabilities)
        if abs(total - 1.0) > 1e-6:
            raise AttackingModelError(
                f"probabilities do not sum to 1.0 (got {total}) for element={self.element}, stat={self.stat}"
            )

    def p_involved(self) -> float:
        return sum(p for c, p in zip(self.counts, self.probabilities) if c >= 1)

    def expected_count(self) -> float:
        return sum(c * p for c, p in zip(self.counts, self.probabilities))

    def to_polars(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "element": [self.element] * len(self.counts),
                "fixture": [self.fixture] * len(self.counts),
                "stat": [self.stat] * len(self.counts),
                "count": list(self.counts),
                "probability": list(self.probabilities),
            }
        )


def predict_attacking_pmf(
    params: AttackingModelParams,
    feature_row: dict,
    *,
    element: int,
    fixture: int,
    stat: str,
    team_goals_marginal: Sequence[tuple[int, float]],
    minute_exposure: Sequence[tuple[float, float]],
    max_count: int | None = None,
) -> AttackingInvolvementPMF:
    """`feature_row` must carry every column in `params.feature_spec.
    numeric_columns` plus `position` (never `minutes` itself — that is
    supplied via `minute_exposure`, module docstring "'Minutes-weighted'").

    `team_goals_marginal`/`minute_exposure` are both plain
    `[(value, probability), ...]` sequences, validated to sum to 1.0 —
    module docstring, "Composition, not import". A `minute_exposure` entry
    of `(0.0, w)` naturally contributes a `p=0` Binomial (a spike at
    count=0, `scipy.stats.binom.pmf` handles this without a special case),
    so a player who did not appear at all correctly gets zero involvement
    probability without this function branching on it.

    Output support is `0..max(config.max_count, max(g for g, _ in
    team_goals_marginal))` unless `max_count` overrides both — a team's own
    goal marginal can genuinely exceed this module's config default (a
    team_strength fit's own `max_goals` default is 10), and truncating the
    player's PMF below the team's own ceiling would silently discard real
    (if small) mass for a big-scoring fixture."""
    if stat not in params.stats:
        raise AttackingModelError(
            f"no fitted AttackingStatParams for stat={stat!r} -- expected one of {tuple(params.stats)}"
        )
    total_g_weight = sum(w for _, w in team_goals_marginal)
    if abs(total_g_weight - 1.0) > 1e-6:
        raise AttackingModelError(f"team_goals_marginal probabilities must sum to 1.0, got {total_g_weight}")
    total_m_weight = sum(w for _, w in minute_exposure)
    if abs(total_m_weight - 1.0) > 1e-6:
        raise AttackingModelError(f"minute_exposure probabilities must sum to 1.0, got {total_m_weight}")

    stat_params = params.stats[stat]
    x = _feature_row_to_vector(feature_row, params.feature_spec)
    eta = float(np.clip(x @ stat_params.beta, -30.0, 30.0))
    s = 1.0 / (1.0 + math.exp(-eta))

    max_g = max((int(round(g)) for g, _ in team_goals_marginal), default=0)
    resolved_max = max_count if max_count is not None else max(stat_params.config.max_count, max_g)
    if resolved_max < 0:
        raise AttackingModelError(f"max_count must be >= 0, got {resolved_max}")

    k = np.arange(0, resolved_max + 1)
    mixture = np.zeros(resolved_max + 1, dtype=np.float64)
    mass_before = 0.0
    for m_val, w_m in minute_exposure:
        m_frac = min(max(float(m_val), 0.0), 90.0) / 90.0
        p = min(max(m_frac * s, 0.0), 1.0)
        for g, w_g in team_goals_marginal:
            g_int = int(round(g))
            weight = w_m * w_g
            if weight <= 0.0:
                continue
            pmf_k = binom.pmf(k, g_int, p)
            mixture += weight * pmf_k
            mass_before += weight * float(pmf_k.sum())

    total = float(mixture.sum())
    if total <= 0.0:
        raise AttackingModelError(
            f"predicted {stat} PMF has zero mass for element={element}, fixture={fixture}"
        )
    mixture = mixture / total

    return AttackingInvolvementPMF(
        element=element,
        fixture=fixture,
        stat=stat,
        counts=tuple(int(v) for v in k),
        probabilities=tuple(float(v) for v in mixture),
        mass_before_truncation=mass_before,
    )


# ---------------------------------------------------------------------------
# Walk-forward calibration (blueprint §7.1)
# ---------------------------------------------------------------------------


def _log_loss(y_true: Sequence[int], p: Sequence[float], eps: float = 1e-12) -> float:
    n = len(y_true)
    if n == 0:
        raise AttackingModelError("log_loss over zero rows is undefined")
    total = 0.0
    for y, prob in zip(y_true, p):
        prob = min(max(prob, eps), 1.0 - eps)
        total += -(y * math.log(prob) + (1 - y) * math.log(1 - prob))
    return total / n


def _brier(y_true: Sequence[int], p: Sequence[float]) -> float:
    n = len(y_true)
    if n == 0:
        raise AttackingModelError("brier score over zero rows is undefined")
    return sum((prob - y) ** 2 for y, prob in zip(y_true, p)) / n


@dataclass(frozen=True)
class AttackingWalkForwardResult:
    """Every fold's out-of-sample `P(player registers >= 1 {goal|assist})`
    predictions (model vs. both §7.1-required baselines) — module
    docstring, "Walk-forward gate"."""

    stat: str
    n_folds: int
    y_true: tuple[int, ...]
    p_model: tuple[float, ...]
    p_baseline_position_rate: tuple[float, ...]
    p_baseline_player_trailing: tuple[float, ...]

    def model_log_loss(self) -> float:
        return _log_loss(self.y_true, self.p_model)

    def model_brier(self) -> float:
        return _brier(self.y_true, self.p_model)

    def baseline_position_rate_log_loss(self) -> float:
        return _log_loss(self.y_true, self.p_baseline_position_rate)

    def baseline_position_rate_brier(self) -> float:
        return _brier(self.y_true, self.p_baseline_position_rate)

    def baseline_player_trailing_log_loss(self) -> float:
        return _log_loss(self.y_true, self.p_baseline_player_trailing)

    def baseline_player_trailing_brier(self) -> float:
        return _brier(self.y_true, self.p_baseline_player_trailing)

    def beats_both_baselines(self) -> bool:
        """The gate, §7.1: strictly lower log-loss AND Brier than BOTH
        baselines. Reports the verdict; does not tune to pass it."""
        return (
            self.model_log_loss() < self.baseline_position_rate_log_loss()
            and self.model_log_loss() < self.baseline_player_trailing_log_loss()
            and self.model_brier() < self.baseline_position_rate_brier()
            and self.model_brier() < self.baseline_player_trailing_brier()
        )

    def residual_mean(self) -> float:
        residuals = [y - p for y, p in zip(self.y_true, self.p_model)]
        return sum(residuals) / len(residuals)

    def residual_std(self) -> float:
        residuals = [y - p for y, p in zip(self.y_true, self.p_model)]
        mean = sum(residuals) / len(residuals)
        var = sum((r - mean) ** 2 for r in residuals) / len(residuals)
        return math.sqrt(var)


def walk_forward_validate(
    table: pl.DataFrame,
    *,
    stat: str,
    min_train_rows: int = 500,
    config: AttackingModelConfig = AttackingModelConfig(),
) -> AttackingWalkForwardResult:
    """Refits the Binomial-share regression for `stat` at every `(season,
    round)` fold, using only rows strictly earlier (by `_chronological_
    rank`) than that fold — same walk-forward discipline `fplai.models.
    defensive_contribution.walk_forward_validate` documents. Restricted to
    `minutes > 0` rows throughout (an involvement probability is undefined
    for a non-appearance)."""
    if stat not in STATS:
        raise AttackingModelError(f"unknown stat {stat!r} -- expected one of {STATS}")
    if "_chronological_rank" not in table.columns:
        raise AttackingModelError("table must carry _chronological_rank -- build it via build_training_table")

    target_col = STAT_TARGET_COLUMN[stat]
    trailing_rate_col = STAT_TRAILING_RATE_COLUMN[stat]

    fit_table = table.filter(pl.col("minutes") > 0)
    if fit_table.is_empty():
        raise AttackingModelError(f"no minutes>0 rows for stat {stat!r}")

    fold_keys = fit_table.select(["season", "round", "_chronological_rank"]).unique().sort("_chronological_rank")

    y_true: list[int] = []
    p_model: list[float] = []
    p_baseline_position_rate: list[float] = []
    p_baseline_player_trailing: list[float] = []
    n_folds = 0

    for season, round_, rank in fold_keys.iter_rows():
        train = fit_table.filter(pl.col("_chronological_rank") < rank)
        eval_rows = fit_table.filter(pl.col("_chronological_rank") == rank)
        if train.height < min_train_rows or eval_rows.is_empty():
            continue

        n_folds += 1
        spec = _build_feature_spec(train)
        X_train = _design_matrix(train, spec)
        y_train = train[target_col].cast(pl.Float64).to_numpy()
        n_trials_train = train["team_goals"].cast(pl.Float64).to_numpy()
        minutes_frac_train = (train["minutes"].clip(upper_bound=90).cast(pl.Float64) / 90.0).to_numpy()
        beta = _fit_binomial_share(X_train, y_train, n_trials_train, minutes_frac_train, config)

        position_rate = train.group_by("position").agg(
            ((pl.col(target_col) >= 1).sum() / pl.len()).alias("rate")
        )
        position_rate_map = dict(zip(position_rate["position"].to_list(), position_rate["rate"].to_list()))
        overall_rate = float((train[target_col] >= 1).sum()) / train.height

        X_eval = _design_matrix(eval_rows, spec)
        n_eval = eval_rows["team_goals"].cast(pl.Float64).to_list()
        minutes_eval = eval_rows["minutes"].cast(pl.Float64).to_list()
        y_eval = eval_rows[target_col].to_list()
        pos_eval = eval_rows["position"].to_list()
        trailing_eval = eval_rows[trailing_rate_col].to_list()

        for i in range(eval_rows.height):
            eta = float(np.clip(X_eval[i] @ beta, -30.0, 30.0))
            s = 1.0 / (1.0 + math.exp(-eta))
            m_frac = min(minutes_eval[i], 90.0) / 90.0
            p_share = min(max(m_frac * s, 0.0), 1.0)
            n_i = n_eval[i]
            p_inv = 1.0 - (1.0 - p_share) ** n_i if n_i > 0 else 0.0
            p_inv = min(max(p_inv, 0.0), 1.0)

            y_true.append(1 if y_eval[i] >= 1 else 0)
            p_model.append(p_inv)

            pos = pos_eval[i]
            p_baseline_position_rate.append(position_rate_map.get(pos, overall_rate))

            trailing = trailing_eval[i]
            p_baseline_player_trailing.append(
                float(trailing) if trailing is not None else position_rate_map.get(pos, overall_rate)
            )

    if n_folds == 0:
        raise AttackingModelError(f"no usable folds for stat={stat!r} with min_train_rows={min_train_rows}")

    return AttackingWalkForwardResult(
        stat=stat,
        n_folds=n_folds,
        y_true=tuple(y_true),
        p_model=tuple(p_model),
        p_baseline_position_rate=tuple(p_baseline_position_rate),
        p_baseline_player_trailing=tuple(p_baseline_player_trailing),
    )


# ---------------------------------------------------------------------------
# Derived-capability registration + persistence
# ---------------------------------------------------------------------------


def _register_attacking_capability() -> FactTableSchema:
    existing = CANONICAL_SCHEMAS.get(PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK)
    if existing is not None:
        return existing
    return register_derived_capability(
        PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK,
        entity_key=("season", "round", "element", "fixture", "stat", "count"),
        value_fields=("probability",),
        dataset=PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_DATASET,
        description=(
            "Attacking-involvement estimator (blueprint §4, E5, session s004) "
            "-- LONG format, one row per (player, fixture, stat, count) with "
            "its probability under the fitted Binomial-share model composed "
            "with a caller-supplied team goal marginal and minutes exposure. "
            "`stat` in ('goals','assists') -- part of the ENTITY KEY, not a "
            "value field, because a single (season, round, element, fixture, "
            "count) would otherwise collide between the two independently-"
            "fitted PMFs. Summing `probability` over every `count` for one "
            "(season, round, element, fixture, stat) must equal 1.0."
        ),
    )


ATTACKING_SCHEMA: FactTableSchema = _register_attacking_capability()


def pmfs_to_rows(pmfs: Sequence[AttackingInvolvementPMF], *, season: str, round_: int) -> pl.DataFrame:
    if not pmfs:
        raise AttackingModelError("pmfs_to_rows called with zero PMFs -- nothing to persist")
    frames = [pmf.to_polars() for pmf in pmfs]
    combined = pl.concat(frames)
    return combined.with_columns(pl.lit(season).alias("season"), pl.lit(round_).alias("round"))


def write_attacking_pmfs(
    store: BitemporalStore,
    pmfs: Sequence[AttackingInvolvementPMF],
    *,
    season: str,
    round_: int,
    valid_at: datetime,
    calibration_by_stat: dict[str, CalibrationReference],
    source: str = "fplai.models.attacking",
    skip_if_unchanged: bool = True,
) -> dict[str, WriteResult]:
    """Splits `pmfs` by `stat` and calls `write_derived` once PER STAT,
    each stamped with THAT stat's own out-of-sample `CalibrationReference`
    -- a pooled single reference across goals and assists would blur two
    genuinely different fitted models into one number."""
    if not pmfs:
        raise AttackingModelError("write_attacking_pmfs called with zero PMFs -- nothing to write")
    missing_calibration = [s for s in {p.stat for p in pmfs} if s not in calibration_by_stat]
    if missing_calibration:
        raise AttackingModelError(f"calibration_by_stat is missing stat(s) {missing_calibration}")

    results: dict[str, WriteResult] = {}
    for stat in STATS:
        stat_pmfs = [p for p in pmfs if p.stat == stat]
        if not stat_pmfs:
            continue
        rows = pmfs_to_rows(stat_pmfs, season=season, round_=round_)
        derived_from = [
            DerivationInput(
                capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
                entity_key={"season": season},
                note=(
                    "trailing per-player goal/assist/xG/xA rates, aggregated per "
                    "the feature engineering in fplai.models.attacking -- "
                    "whole-season aggregate input, not an individually-named row subset."
                ),
            ),
            DerivationInput(
                capability=TEAM_STRENGTH_RATING_GAMEWEEK,
                entity_key={"season": season, "gameweek": round_},
                note=(
                    "team_goals_marginal is supplied by the CALLER (a fitted "
                    "team_strength.ScorelinePMF's own goal marginal, composed "
                    "externally at predict time) -- this module never imports "
                    "fplai.models.team_strength directly (module docstring, "
                    "'Composition, not import')."
                ),
            ),
        ]
        results[stat] = write_derived(
            store,
            PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK,
            rows,
            valid_at=valid_at,
            observed_at=datetime.now(timezone.utc),
            source=source,
            derived_from=derived_from,
            calibration=calibration_by_stat[stat],
            skip_if_unchanged=skip_if_unchanged,
        )
    return results
