"""Minutes model v1 — blueprint §4.1 ("the model that decides the
project"), §4.3 (PMF discipline), §7.1 (calibration before points), §12.2
(derived-fact labelling). Phase 2 / E5, session `s003`.

A player with elite xG at 60% start probability is usually worse than a
mediocre player at 95% — this module is what makes that comparison
possible: a per-player-gameweek probability distribution over START/SUB/
UNUSED, and, conditional on that outcome, a distribution over minutes
bands. **The output is always a PMF, never a scalar** (CLAUDE.md rule 5) —
`MinutesPMF.expected_minutes()` is a convenience *computed from* the PMF,
never the module's actual interface.

## Data — verified live, 2026-08-22, not re-derived from the brief

`starts` is observed and complete for **2022-23 -> 2025-26: 113,592
player-gameweeks** (verified: 26,505 + 29,725 + 27,605 + 29,757). It is
**wholly NULL for 2019-20/2020-21/2021-22** (16,556 + 24,365 + 25,447 null
rows respectively) — those three seasons carry no start label at all. This
module never infers `starts` from `minutes`: a 20-minute hook and a
20-minute cameo off the bench are indistinguishable on `minutes` alone, and
a silently-wrong label is worse than three fewer seasons of training data.
Rows with a NULL `starts` are excluded from every stage of this module —
feature engineering, target construction, and the walk-forward gate.

Also verified live (informs the state/band design below): every labelled
row is internally consistent — `starts=1` NEVER co-occurs with `minutes=0`
(0/30,450 START rows), `starts=0 & minutes>0` (SUB) always has
`1 <= minutes <= 90` (15,343 rows), and `starts=0 & minutes=0` (UNUSED,
67,799 rows) is the remainder. `30,450 + 15,343 + 67,799 = 113,592` exactly.

## The three-state formulation

`minutes_state(starts, minutes)` returns one of `STATES = ("START", "SUB",
"UNUSED")`. This is the brief's required first stage: a categorical over
whether the player started, appeared as a substitute, or did not feature at
all. `MinutesPMF.p_start()` — the START class probability — is what the
walk-forward gate (§ below) scores against both naive baselines.

## Minute bands — discretised on FPL-meaningful boundaries, not arbitrary ones

`MINUTE_BANDS = ("0", "1-29", "30-59", "60-74", "75-89", "90+")`. The one
boundary that must be exact is **60** — `long_play`/`short_play` in
`game_config`'s `scoring` blob pay `long_play=2` points for 60+ minutes and
`short_play=1` for 1-59 (blueprint §11; verified live in the real store's
`game_config`, 2026-08-22). **The 60-minute THRESHOLD itself is not present
anywhere in `game_config`'s payload** — only the two point VALUES are; the
minute cliff is an unpublished constant of FPL's rules, the same
"absent from the API entirely" gap CLAUDE.md already documents for
defensive-contribution thresholds (blueprint §11). `APPEARANCE_POINTS_
MINUTE_CLIFF = 60` below is therefore a **stated, verified-unpublished
constant** (checked, not assumed), not a value read from live config,
because there is no live-config value to read.

The other four boundaries exist because defensive-contribution thresholds
(and general rotation-risk reasoning) scale with *how much* of a match a
player was on the pitch for, not just whether they cleared the 60-minute
appearance cliff: `1-29` (a token cameo, minimal DC-accrual chance),
`30-59` (a longer sub appearance, still under the appearance cliff),
`60-74` (cleared the cliff but hooked before the final quarter — the
"hooked at 60" case blueprint §4.1 names explicitly), `75-89` (played
almost the whole match), `90+` (a genuinely full match, allowing for
stoppage time). This directly gives the model the "hooked at 60 vs plays
90" distinction as separate, orderable outcomes rather than folding them
into one "played" bucket.

## Features — all leakage-safe, strictly before the deadline being predicted

Every trailing feature is computed from rows with `round < this row's
round`, **never** from another fixture within the *same* round: a double
gameweek's two fixtures share one deadline, so from that deadline's own
vantage point neither fixture's outcome is knowable yet, and a trailing
feature that peeked at "the other fixture this round" would be exactly the
same class of leak blueprint §7.2 already fixed once (own-gameweek
`selected` is not fully knowable at that gameweek's own deadline). This
mirrors `fplai.backtest.replay.GameweekView`'s `round < gameweek` boundary
exactly (see `_build_round_rollup`'s docstring).

`days_since_team_previous_fixture` is different in kind: kickoff times and
the fixture list are public well before a deadline, so a gap to a *fixture-
chronologically* earlier kickoff (even one in the *same* round, for a
double gameweek's second leg — a real, short rest gap, exactly the
congestion signal wanted) is not leakage the way an outcome would be. See
`_build_team_fixture_gap`.

Ownership/price enter only as `prev_gw_value`/`prev_gw_selected` — the
STRICTLY PREVIOUS round's price/ownership, never the row's own round's
value, per this task's brief and blueprint §7.2's "a gameweek's `selected`
figure is not fully knowable at its own deadline" rule, applied here
literally to both `value` and `selected` even though `fplai.backtest.data`
treats the *current* round's `value` as pre-deadline-safe for a different
purpose (template-baseline construction) — that is a different module with
a different, already-settled rationale; this brief's own instruction is
followed here without attempting to reconcile the two.

Cold start (a genuinely new player to this training window — a signing, or
simply round 1 of a season) is handled **explicitly**, not silently: every
trailing feature that would otherwise be NULL for a player's first labelled
round is filled to `0.0`, and `cold_start` (no prior labelled row this
season) is carried as its own boolean feature so the fitted model can learn
a *different* relationship for a cold-start row rather than reading "0.0
trailing start rate" as "this player never starts". `games_played_this_
season` (a count, not a rate) is the companion signal distinguishing "one
game of history" from "twenty".

## Features that are NOT sourceable — declared, not silently dropped

Checked directly against this store, not assumed from the brief. Two of
blueprint §4.1's named strongest predictors cannot be built from anything
ingested:

- **European congestion** — a midweight UCL/UEL fixture within N days of a
  Premier League match. This needs a non-PL fixture calendar; the
  capability registry (`fplai.schemas`) carries a `competition` axis on
  every `CapabilityKey`'s implicit provider-declared coverage, but **no
  registered provider declares anything other than PL** (`providers/pl.py`,
  `providers/fpl.py`, `providers/vaastav.py`, `providers/olbauday.py` are
  all PL-only). There is no capability to query for this at all — not a
  missing column on an existing one.
- **Manager rotation priors and the regime-change flag** — no manager-
  identity capability exists anywhere in this store. `pl_match_lineups`
  names players and roles, never a manager; nothing anywhere resolves "who
  was picking this team" as an entity at all, so there is no identity to
  attach a rotation prior *to*.

Sourcing either is a `fpl-data-scout` task, out of this module's scope.
Silently omitting them would be exactly this project's recurring failure
mode (an unstated assumption standing in for a real one) — see
`docs/wiki/model-minutes.md` for the fuller account and what capability
each would require if ever built.

## Bitemporal fitting — the same wrong-primitive lesson team_strength.py
already found, re-verified for this module rather than assumed to transfer

`vaastav_player_gameweek_stats` was bulk-ingested in one backfill session
(2026-08-21) — every row's `observed_at` is approximately "today"
regardless of which historical season/gameweek the row describes. `as_of
(dataset, deadline_t)` filters on `observed_at <= deadline_t`, which is
false for every row at any genuinely historical `deadline_t`, so `as_of()`
on this dataset returns EMPTY for any real backtest cutoff — the exact
failure `fplai.models.team_strength`'s module docstring documents, and
re-verified (not assumed to still hold) directly here:
`tests/test_minutes.py::test_store_as_of_is_empty_for_a_genuinely_
historical_deadline_on_this_dataset`. This is the **third** module in this
project to hit and route around this exact primitive — see this task's
punch-out for the `finding` this raises about `as_of()`'s de facto
interface for bulk-ingested archives.

**Session s003, 2026-08-22:** this module originally enforced the actual
bitemporal safety property the same way `fplai.models.team_strength` did —
`store.observations()` plus a hand-written `kickoff_time` filter, with a
near-identical justifying comment. That was the third independent instance
of the same workaround (`fplai.backtest.data` was, on inspection, NOT a
fourth — its own leakage boundary is round-number-based, `round <
gameweek`, and never touches `kickoff_time` at all; see this session's
punch-out). The Architect closed the hole at the store level:
`BitemporalStore.effective_at()` is now the one sanctioned primitive for
"state effective at this deadline, resolved on a dataset's own declared
valid-time column" (blueprint §3.2, `fplai.schemas.
PLAYER_GAMEWEEK_STATS_GAMEWEEK.valid_time_column="kickoff_time"`).
`build_training_table` calls it directly instead of hand-rolling the
filter. `build_training_table`'s `as_of` parameter stays REQUIRED, tz-aware,
no default, for exactly the reason `effective_at`'s `effective_ts` has no
default.

A real, live-verified consequence, not a hidden one: `effective_at()`
collapses to one row per entity key (blueprint §3.2 ruling point 3), which
the old hand-rolled filter never did. The real store carries 10 exact-
duplicate rows in one write batch (2025-26, elements 100/391 — verified
live, an upstream archive artefact). Every one of those 10 rows has a
non-null `starts` label, so the pre-migration training table (113,270 rows)
silently double-counted all 10; the post-migration table is 113,260 rows —
see this session's punch-out for the exact state-count deltas and the
updated real-store-gated test.

## Validation — walk-forward by gameweek, reusing the Phase 1 leakage
boundary

`walk_forward_validate` iterates chronologically over `(season, round)`
folds. For fold *t*, the model is refit using **only** rows whose own
`(season, round)` is strictly earlier than *t* — the same "never fit on
data at or after the deadline being predicted" rule
`fplai.backtest.replay.SeasonReplay` enforces structurally via
`GameweekView`, applied here to model-fitting instead of squad selection.
Per §7.1, the gate scores **`P(start)`** (log-loss, Brier, plus a
reliability diagram) against two naive baselines computed the same
leakage-safe way: "started last gameweek" (this player's own immediately
preceding round, falling back to the position base rate for a genuinely
cold-start player) and "base rate by position" (the START rate among
*training* rows only, recomputed fresh at every fold). See
`docs/wiki/model-minutes.md` for the real numbers from
`scripts/fit_minutes.py` against the live store.

## Persistence

`write_minutes_pmfs` routes through `fplai.derived.write_derived` (never a
bare `store.write()`), via the derived capability
`player.minutes_distribution@gameweek` -> dataset
`derived_player_minutes_distribution`, registered at **this module's own
import time** — Story A's resolution (`docs/wiki/model-team-strength.md`
§10; `fplai.schemas`' "Phase 2, E5" section), reused here rather than
re-litigated: a second real derived-capability module importing the same
pattern is exactly what that resolution was designed to generalise to.

## Recalibration — session s003, closing the "deliberately not built here"
gap v1 shipped with

v1's own reliability diagram (§ above) showed real, out-of-sample mid-range
underconfidence and was shipped uncorrected, named explicitly as future
work. This session closes it, or reports honestly that it doesn't close --
see `docs/wiki/model-minutes.md` for which happened, with the real numbers.
The mechanism (isotonic regression via PAVA, nested strictly inside each
walk-forward fold's own training window) is documented in full where the
code lives -- the section immediately above `MinutesModelParams` titled
"Nested out-of-sample P(start) calibration". The short version: a
calibrator fitted on the same data it is then scored against is leakage and
will look spectacular for a reason that has nothing to do with genuine
calibration (it is memorising, not correcting) — every calibrator in this
module is fitted on a chronologically-earlier slice of whichever training
window it lives inside, never on the fold/model it is later applied to.
`MinutesPMF.calibration_method` (`"raw_uncalibrated"` / `"isotonic_v1"`) is
declared, structural provenance on every persisted row — the same role
`fplai.models.defensive_contribution.DCPMF.threshold_verified` plays for
that module — not a docstring promise a reader has to trust.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import polars as pl
from scipy.optimize import minimize

from fplai.calibration import IsotonicCalibrator, fit_isotonic_calibrator as _fit_isotonic_calibrator
from fplai.derived import CalibrationReference, DerivationInput, write_derived
from fplai.gameweek_stats import read_player_gameweek_stats
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    PLAYER_MINUTES_DISTRIBUTION_DATASET,
    PLAYER_MINUTES_DISTRIBUTION_GAMEWEEK,
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
    "opponent_team",
    "position",
    "team",
    "starts",
    "was_home",
    "value",
    "selected",
)

STATES: tuple[str, ...] = ("START", "SUB", "UNUSED")
MINUTE_BANDS: tuple[str, ...] = ("0", "1-29", "30-59", "60-74", "75-89", "90+")
POSITIONS: tuple[str, ...] = ("GK", "DEF", "MID", "FWD")

# See module docstring, "Minute bands". Verified live 2026-08-22 against the
# real store's game_config: the scoring blob carries long_play=2/short_play=1
# (POINT VALUES), never the 60-minute THRESHOLD itself -- there is no
# live-config field to read this from, so it is stated here as a checked,
# unpublished constant rather than silently hardcoded without comment.
APPEARANCE_POINTS_MINUTE_CLIFF = 60

# Representative minute value per band, used only by MinutesPMF.expected_minutes()
# (a CONVENIENCE property computed *from* the PMF -- CLAUDE.md rule 5 -- never
# this module's actual interface). Midpoints for interior bands; 0 and 90 for
# the two edge bands (a "0" band player played exactly zero minutes; a "90+"
# player is treated as exactly 90 for this purpose -- stoppage time beyond 90
# is not separately modelled).
_BAND_MIDPOINT: dict[str, float] = {
    "0": 0.0,
    "1-29": 15.0,
    "30-59": 45.0,
    "60-74": 67.0,
    "75-89": 82.0,
    "90+": 90.0,
}

_LONG_APPEARANCE_BANDS = frozenset({"60-74", "75-89", "90+"})

# Features that blueprint §4.1 names as among the strongest minutes
# predictors and that this store genuinely cannot supply -- see module
# docstring, "Features that are NOT sourceable". Declared as data (not just
# prose) so a future capability addition has something concrete to diff
# against, and so `docs/wiki/model-minutes.md` has one place to point at.
KNOWN_ABSENT_FEATURES: tuple[str, ...] = (
    "european_congestion",
    "manager_rotation_prior",
    "regime_change_flag",
)


class MinutesModelError(ValueError):
    """Misuse or a genuine data problem this module refuses to guess past —
    an unresolvable position/team category, a naive `as_of`, an empty
    training window, or a PMF that would not sum to 1."""


# ---------------------------------------------------------------------------
# State / band label functions -- the single source of truth for the
# boundaries described in the module docstring. `tests/test_minutes.py`
# checks these agree with the vectorised Polars expressions used in
# `_label_fixture_rows` across the full 0..120 minute range, so the two
# implementations cannot silently drift apart.
# ---------------------------------------------------------------------------


def minute_band(minutes: int) -> str:
    """Which of `MINUTE_BANDS` `minutes` falls into. See module docstring."""
    if minutes <= 0:
        return "0"
    if minutes < 30:
        return "1-29"
    if minutes < 60:
        return "30-59"
    if minutes < 75:
        return "60-74"
    if minutes < 90:
        return "75-89"
    return "90+"


def minutes_state(starts: int, minutes: int) -> str:
    """Which of `STATES` this (starts, minutes) pair represents. Verified
    live (module docstring): `starts=1` never co-occurs with `minutes=0` in
    this store, so this function does not special-case it -- a future row
    that did would simply be classified START, same as every other
    `starts=1` row, not silently misclassified."""
    if starts == 1:
        return "START"
    if minutes > 0:
        return "SUB"
    return "UNUSED"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MinutesModelConfig:
    """Every fitting hyperparameter, in one place -- same convention
    `fplai.models.team_strength.TeamStrengthConfig` established. Defaults
    are stated, reasoned choices, not calibrated optima (same status as
    that module's `goals_source_weight`)."""

    start_rate_windows: tuple[int, ...] = (3, 5, 10)
    """Trailing-start-rate window sizes, in rounds. "Several windows" per
    this task's brief -- short (form/rotation-risk), medium, and long
    (established-starter) horizons."""

    minutes_mean_windows: tuple[int, ...] = (3, 5)
    """Trailing mean-minutes-per-round window sizes."""

    full_appearance_window: int = 5
    full_appearance_minute_threshold: int = 75
    """Trailing fraction of the last `full_appearance_window` rounds where
    the player played >= this many minutes -- the explicit "hooked at 60 vs
    plays 90" signal the brief names, distinct from the start-rate windows
    above (a player can start every match and still be hooked every time)."""

    team_first_fixture_gap_days_default: float = 7.0
    """Sentinel `days_since_team_previous_fixture` for a team's first
    fixture inside the training window (no earlier fixture to measure a gap
    against) -- a typical single-gameweek rest gap, not a guess at zero or
    an extreme value. `team_first_fixture_in_window` carries the flag
    itself, so the model can learn a different relationship for this case
    rather than silently trusting the sentinel as a real observation."""

    l2_penalty: float = 1.0
    """Ridge penalty on every softmax fit's weight matrix (state model and
    both band-conditional models) -- same dual role
    `TeamStrengthConfig.attack_defence_l2` documents: standard
    regularisation AND the mechanism that resolves the position/team
    one-hot encoding's gauge freedom (every category dummy plus an
    intercept is a rank-deficient design without it). Deliberately
    UNCALIBRATED -- pending the Phase 2 gate's real numbers, same
    provisional status as every other placeholder hyperparameter in this
    project's Phase 2 models.

    **Its meaning changed session `s005`**: `_softmax_neg_log_lik_and_grad`'s
    ridge term is now scaled by `1/n` (matching the per-row-averaged
    likelihood it is added to) -- see that function's docstring for the
    bug this fixes and the measured before/after. `1.0` was re-measured
    against the corrected formula (not just carried over) and confirmed
    still a good default -- a swept grid 0.01-100 is flat from 0.01-1.0
    and only degrades mildly above 10; see `docs/wiki/model-minutes.md`."""

    max_lbfgs_iterations: int = 300


# ---------------------------------------------------------------------------
# Raw-row normalisation
# ---------------------------------------------------------------------------


def _normalise_and_filter_positions(raw: pl.DataFrame) -> pl.DataFrame:
    """The same two verified drift cases `fplai.backtest.data._normalise_
    position` documents for this exact dataset (that function is private to
    that module -- this module reads the raw store independently, the same
    relationship `fplai.models.team_strength` already has with it):

    1. `GKP` -> `GK` (a one-round 2021-22 vaastav inconsistency).
    2. `AM` rows dropped entirely -- the Assistant Manager chip (existed
       pre-2026/27, removed for 2026/27 per blueprint §11); a manager
       entry is not a player with a minutes distribution to predict.
    """
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
# Feature engineering
# ---------------------------------------------------------------------------

NUMERIC_FEATURE_COLUMNS: tuple[str, ...] = (
    "trailing_start_rate_3",
    "trailing_start_rate_5",
    "trailing_start_rate_10",
    "trailing_minutes_mean_3",
    "trailing_minutes_mean_5",
    "trailing_full_appearance_rate",
    "games_played_this_season",
    "cold_start",
    "days_since_team_previous_fixture",
    "team_first_fixture_in_window",
    "prev_gw_value",
    "prev_gw_selected_log1p",
    "was_home",
)


def _build_round_rollup(labeled: pl.DataFrame, config: MinutesModelConfig) -> pl.DataFrame:
    """One row per (season, element, round) -- collapses a double-gameweek
    player's 2+ fixture rows into one summary row, purely for TRAILING-
    FEATURE bookkeeping (see module docstring, "Features"). Both fixtures
    of a double gameweek share one deadline, so both are equally unknown to
    a strategy deciding for that gameweek -- trailing features for either
    fixture must stop at round-1, never peek at "the other fixture this
    round". Mirrors `fplai.backtest.replay.GameweekView`'s `round <
    gameweek` boundary exactly, applied to feature engineering instead of
    squad selection.
    """
    rollup = labeled.group_by(["season", "element", "round"]).agg(
        pl.col("starts").max().alias("any_start"),
        pl.col("minutes").sum().alias("round_minutes"),
        pl.col("value").first().alias("round_value"),
        pl.col("selected").first().alias("round_selected"),
        pl.col("position").first().alias("position"),
        pl.col("team").first().alias("team"),
    )
    rollup = rollup.sort(["season", "element", "round"])

    threshold = float(config.full_appearance_minute_threshold)
    appeared = ((pl.col("any_start") == 1) | (pl.col("round_minutes") > 0)).cast(pl.Float64)
    full_appearance = (pl.col("round_minutes") >= threshold).cast(pl.Float64)

    trailing_exprs = []
    for w in config.start_rate_windows:
        trailing_exprs.append(
            pl.col("any_start")
            .cast(pl.Float64)
            .shift(1)
            .rolling_mean(window_size=w, min_samples=1)
            .over(["season", "element"])
            .alias(f"trailing_start_rate_{w}")
        )
    for w in config.minutes_mean_windows:
        trailing_exprs.append(
            pl.col("round_minutes")
            .cast(pl.Float64)
            .shift(1)
            .rolling_mean(window_size=w, min_samples=1)
            .over(["season", "element"])
            .alias(f"trailing_minutes_mean_{w}")
        )
    trailing_exprs.append(
        full_appearance.shift(1)
        .rolling_mean(window_size=config.full_appearance_window, min_samples=1)
        .over(["season", "element"])
        .alias("trailing_full_appearance_rate")
    )
    trailing_exprs.append(
        appeared.shift(1).fill_null(0.0).cum_sum().over(["season", "element"]).alias("games_played_this_season")
    )
    trailing_exprs.append(pl.col("round_value").shift(1).over(["season", "element"]).alias("prev_gw_value"))
    trailing_exprs.append(pl.col("round_selected").shift(1).over(["season", "element"]).alias("prev_gw_selected"))
    # Raw (non-windowed) "did this player start their immediately preceding
    # round" -- distinct from trailing_start_rate_* (which are ALSO fed to
    # the model): this one feeds ONLY the "started last gameweek" naive
    # baseline in walk_forward_validate, kept nullable (no fill_null) so a
    # genuinely cold-start row is visibly absent, not silently zero.
    trailing_exprs.append(pl.col("any_start").cast(pl.Float64).shift(1).over(["season", "element"]).alias("prev_round_any_start"))

    rollup = rollup.with_columns(trailing_exprs)

    fill_zero = [c for c in NUMERIC_FEATURE_COLUMNS if c not in ("games_played_this_season", "cold_start", "days_since_team_previous_fixture", "team_first_fixture_in_window", "was_home")]
    rollup = rollup.with_columns([pl.col(c).fill_null(0.0) for c in fill_zero if c in rollup.columns])
    rollup = rollup.with_columns(
        (pl.col("games_played_this_season") == 0).alias("cold_start"),
        pl.col("prev_gw_selected").fill_null(0.0).log1p().alias("prev_gw_selected_log1p"),
    )
    return rollup


def _build_team_fixture_gap(raw: pl.DataFrame, config: MinutesModelConfig) -> pl.DataFrame:
    """Per (season, team, fixture): days since that team's chronologically
    previous fixture (which, for a double gameweek's second leg, is
    legitimately the FIRST leg of the same round -- see module docstring,
    "Features": kickoff times are public pre-deadline, so this is not a
    leakage boundary the way `round < gameweek` is)."""
    calendar = (
        raw.select(["season", "team", "fixture", "kickoff_time"])
        .unique()
        .with_columns(pl.col("kickoff_time").str.strptime(pl.Datetime, "%Y-%m-%dT%H:%M:%SZ").alias("_kickoff"))
        .sort(["season", "team", "_kickoff"])
    )
    calendar = calendar.with_columns(
        pl.col("_kickoff").shift(1).over(["season", "team"]).alias("_prev_kickoff")
    )
    calendar = calendar.with_columns(
        pl.when(pl.col("_prev_kickoff").is_null())
        .then(pl.lit(config.team_first_fixture_gap_days_default))
        .otherwise((pl.col("_kickoff") - pl.col("_prev_kickoff")).dt.total_minutes() / (60.0 * 24.0))
        .alias("days_since_team_previous_fixture"),
        pl.col("_prev_kickoff").is_null().alias("team_first_fixture_in_window"),
    )
    return calendar.select(["season", "team", "fixture", "days_since_team_previous_fixture", "team_first_fixture_in_window"])


def _label_fixture_rows(labeled: pl.DataFrame) -> pl.DataFrame:
    """Adds `state`/`band` target columns to the per-FIXTURE rows, using
    THIS fixture's own `starts`/`minutes` -- never the round rollup, which
    is trailing-feature bookkeeping only. Vectorised Polars mirror of
    `minutes_state`/`minute_band`; `tests/test_minutes.py` proves the two
    stay in agreement across every minute value 0..120."""
    return labeled.with_columns(
        pl.when(pl.col("starts") == 1)
        .then(pl.lit("START"))
        .when(pl.col("minutes") > 0)
        .then(pl.lit("SUB"))
        .otherwise(pl.lit("UNUSED"))
        .alias("state"),
        pl.when(pl.col("minutes") <= 0)
        .then(pl.lit("0"))
        .when(pl.col("minutes") < 30)
        .then(pl.lit("1-29"))
        .when(pl.col("minutes") < 60)
        .then(pl.lit("30-59"))
        .when(pl.col("minutes") < 75)
        .then(pl.lit("60-74"))
        .when(pl.col("minutes") < 90)
        .then(pl.lit("75-89"))
        .otherwise(pl.lit("90+"))
        .alias("band"),
    )


def build_training_table(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: MinutesModelConfig = MinutesModelConfig(),
    allow_live_season: bool = False,
) -> pl.DataFrame:
    """The full leakage-safe feature+target table, one row per (season,
    round, element, fixture) with a `starts` label. See module docstring,
    "Bitemporal fitting" for why `as_of` resolves state on `kickoff_time`
    via the capability reader, never on `observed_at`, and is required
    with no default.
    """
    if as_of.tzinfo is None:
        raise MinutesModelError(
            f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — blueprint §3.2."
        )

    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise MinutesModelError(
            f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()} "
            "(either the dataset has no data in the store at all, or nothing has a kickoff_time "
            "before this cutoff)"
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
        raise MinutesModelError(f"player gameweek stats is missing required column(s) {missing}")

    raw = _normalise_and_filter_positions(raw)

    # `store.effective_at()` already restricted `raw` to rows with
    # kickoff_time strictly before `as_of` (and collapsed duplicate rows per
    # entity key — see module docstring, "A real, live-verified
    # consequence"). `_build_team_fixture_gap` below parses `kickoff_time`
    # itself for the fixture-gap feature; nothing here needs a second parse.
    if seasons is not None:
        seasons = tuple(seasons)
        raw = raw.filter(pl.col("season").is_in(list(seasons)))
        if raw.is_empty():
            raise MinutesModelError(
                f"no player gameweek stats for seasons {seasons!r} at as_of={as_of.isoformat()}. "
                "Note that allow_live_season=False excludes FPL-API-sourced rows BEFORE this "
                "season filter is applied, so a live season requested here resolves to nothing."
            )

    labeled = raw.filter(pl.col("starts").is_not_null())
    if labeled.is_empty():
        raise MinutesModelError(
            "no rows with a non-null `starts` label in the requested window — this model "
            "only trains on 2022-23 onward (verified live; see module docstring)."
        )

    rollup = _build_round_rollup(labeled, config)
    gap = _build_team_fixture_gap(raw, config)
    fixture_rows = _label_fixture_rows(labeled)

    trailing_cols = [c for c in NUMERIC_FEATURE_COLUMNS if c in rollup.columns] + ["prev_round_any_start"]
    table = fixture_rows.join(
        rollup.select(["season", "element", "round", *trailing_cols]),
        on=["season", "element", "round"],
        how="left",
    )
    table = table.join(gap, on=["season", "team", "fixture"], how="left")

    missing_after_join = [c for c in (*NUMERIC_FEATURE_COLUMNS, "prev_round_any_start") if table[c].null_count() > 0 and c != "prev_round_any_start"]
    if missing_after_join:
        raise MinutesModelError(
            f"unexpected NULLs after joining trailing features: {missing_after_join} — a row failed "
            "to match its own (season, element, round) rollup or (season, team, fixture) gap row, "
            "which should be structurally impossible; refusing to silently proceed with NaNs."
        )

    unexpected_positions = set(table["position"].unique().to_list()) - set(POSITIONS)
    if unexpected_positions:
        raise MinutesModelError(
            f"unexpected position value(s) after normalisation: {unexpected_positions} — expected "
            f"a subset of {POSITIONS}."
        )

    # Chronological fold index for walk-forward validation (§ module
    # docstring) -- season strings "YYYY-YY" sort lexicographically in the
    # correct chronological order for every season this dataset covers
    # (verified: "2022-23" < "2023-24" < "2024-25" < "2025-26"), so a plain
    # (season, round) tuple comparison IS chronological order; this column
    # just makes that explicit and cheap to filter on.
    season_order = {s: i for i, s in enumerate(sorted(table["season"].unique().to_list()))}
    table = table.with_columns(
        (pl.col("season").replace_strict(season_order, return_dtype=pl.Int64) * 100 + pl.col("round")).alias(
            "_chronological_rank"
        )
    )
    return table


# ---------------------------------------------------------------------------
# Feature spec + design matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MinutesFeatureSpec:
    """The exact feature vocabulary a fitted model expects, frozen at fit
    time so predict-time design-matrix construction cannot silently drift
    from what the model was trained on. `team_categories` is derived live
    from the training table (not a fixed constant) — a team name never seen
    in training (e.g. a genuinely new club) simply contributes an all-zero
    dummy at predict time (no signal for that category, not a crash); see
    `_design_matrix`."""

    numeric_columns: tuple[str, ...]
    position_categories: tuple[str, ...]
    team_categories: tuple[str, ...]

    @property
    def feature_names(self) -> tuple[str, ...]:
        return (
            ("intercept",)
            + self.numeric_columns
            + tuple(f"position={p}" for p in self.position_categories)
            + tuple(f"team={t}" for t in self.team_categories)
        )

    @property
    def n_features(self) -> int:
        return len(self.feature_names)


def _build_feature_spec(table: pl.DataFrame) -> MinutesFeatureSpec:
    positions = tuple(sorted(table["position"].unique().to_list()))
    unexpected = set(positions) - set(POSITIONS)
    if unexpected:
        raise MinutesModelError(f"unexpected position categories in training table: {unexpected}")
    teams = tuple(sorted(table["team"].unique().to_list()))
    return MinutesFeatureSpec(
        numeric_columns=NUMERIC_FEATURE_COLUMNS,
        position_categories=positions,
        team_categories=teams,
    )


def _design_matrix(table: pl.DataFrame, spec: MinutesFeatureSpec) -> np.ndarray:
    """Build the (n_rows, spec.n_features) design matrix from a Polars
    frame carrying every column in `spec.numeric_columns` plus `position`/
    `team`. A category not in `spec.position_categories`/`team_categories`
    (predict-time only — never possible at fit time, since the spec is
    derived from the same table) gets an all-zero dummy row: no signal for
    an unseen category, not an error, the correct behaviour under the
    intercept+ridge design (see `MinutesFeatureSpec` docstring)."""
    n = table.height
    cols: list[np.ndarray] = [np.ones(n, dtype=np.float64)]
    for c in spec.numeric_columns:
        series = table[c]
        if series.dtype == pl.Boolean:
            cols.append(series.cast(pl.Float64).to_numpy())
        else:
            cols.append(series.cast(pl.Float64).to_numpy())
    positions = table["position"].to_list()
    for p in spec.position_categories:
        cols.append(np.array([1.0 if v == p else 0.0 for v in positions], dtype=np.float64))
    teams = table["team"].to_list()
    for t in spec.team_categories:
        cols.append(np.array([1.0 if v == t else 0.0 for v in teams], dtype=np.float64))
    return np.column_stack(cols)


# ---------------------------------------------------------------------------
# Softmax fitting -- scipy L-BFGS-B, deterministic zero init (CLAUDE.md rule 7)
# ---------------------------------------------------------------------------


def _softmax_neg_log_lik_and_grad(
    flat_w: np.ndarray, X: np.ndarray, y_onehot: np.ndarray, n_features: int, n_classes: int, l2: float
) -> tuple[float, np.ndarray]:
    """**L2 scaling fixed session `s005`** — the log-likelihood term below
    is a per-row AVERAGE (`/n`) but the ridge penalty must be scaled by the
    same `1/n` to stay commensurate with it, or `l2` is effectively `l2*n`
    against the averaged likelihood (the bug `fplai.models.saves`'s own
    corrected NB2 copy first diagnosed and this module shared, unfixed,
    until now — `defensive_contribution.py`'s NB2 fit had the identical
    defect and was fixed in the same session). Measured walk-forward on the
    real store (`eval_seasons=['2024-25','2025-26']`, 76 folds, 57,030 eval
    rows) before/after, same `l2_penalty=1.0` default: log-loss
    0.3464->0.3025 (-12.7% relative), Brier 0.1062->0.0945 (-11.0%), ECE
    (equal-width) 0.0470->0.0186 (-60.4%), calibration slope 1.114->0.945
    (both CIs exclude 1.0, but 0.945 sits far closer than 1.114), intercept
    0.228->0.054, and the direct "implied mean vs empirical rate" read
    (mean predicted P(start) vs the true START rate) improved from a 0.0153
    gap to 0.0066. A swept grid of `l2` under the corrected formula
    (0.01/0.1/1.0/10/100) is FLAT from 0.01-1.0 (4th-decimal differences)
    and only degrades mildly above 10 — `l2_penalty=1.0` needed no retuning
    once the scaling itself was fixed; see `docs/wiki/model-minutes.md` for
    the full table. `beats_both_baselines()` was `True` before AND after at
    every grid point — the §7.1 gate verdict never changed, only its
    margin."""
    W = flat_w.reshape(n_features, n_classes)
    logits = X @ W
    logits = logits - logits.max(axis=1, keepdims=True)
    exp_logits = np.exp(logits)
    probs = exp_logits / exp_logits.sum(axis=1, keepdims=True)
    n = X.shape[0]
    log_probs = np.log(np.clip(probs, 1e-12, 1.0))
    nll = -float(np.sum(y_onehot * log_probs)) / n + (l2 / n) * float(np.sum(W * W))
    grad = (X.T @ (probs - y_onehot)) / n + 2.0 * (l2 / n) * W
    return nll, grad.ravel()


def _fit_softmax(X: np.ndarray, y_idx: np.ndarray, n_classes: int, config: MinutesModelConfig) -> np.ndarray:
    """Deterministic multinomial-logistic (softmax) fit: fixed x0=zeros,
    L-BFGS-B (a deterministic algorithm from a fixed start), L2-regularised
    — no stochastic component anywhere, same determinism posture
    `fplai.models.team_strength`'s Adam/ternary-search fit documents (no
    `seed:` parameter exists because there is no randomness to seed).
    Returns weights shaped (n_features, n_classes)."""
    n_rows, n_features = X.shape
    y_onehot = np.zeros((n_rows, n_classes), dtype=np.float64)
    y_onehot[np.arange(n_rows), y_idx] = 1.0
    x0 = np.zeros(n_features * n_classes, dtype=np.float64)
    result = minimize(
        _softmax_neg_log_lik_and_grad,
        x0,
        args=(X, y_onehot, n_features, n_classes, config.l2_penalty),
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": config.max_lbfgs_iterations},
    )
    if not result.success and result.status not in (0, 1):
        logger.warning("minutes model: softmax fit did not converge cleanly: %s", result.message)
    return result.x.reshape(n_features, n_classes)


def _softmax_predict(x_row: np.ndarray, W: np.ndarray) -> np.ndarray:
    logits = x_row @ W
    logits = logits - logits.max()
    exp_logits = np.exp(logits)
    return exp_logits / exp_logits.sum()


def _softmax_predict_batch(X: np.ndarray, W: np.ndarray) -> np.ndarray:
    """Vectorised form of `_softmax_predict` for a whole design matrix at
    once -- same computation `_softmax_neg_log_lik_and_grad` already does
    internally, factored out so a calibration-holdout batch (potentially
    tens of thousands of rows, once per walk-forward fold) is not scored via
    a Python-level per-row loop."""
    logits = X @ W
    logits = logits - logits.max(axis=1, keepdims=True)
    exp_logits = np.exp(logits)
    return exp_logits / exp_logits.sum(axis=1, keepdims=True)


# ---------------------------------------------------------------------------
# Nested out-of-sample P(start) calibration -- session s003, blueprint §7.1.
#
# THE LEAKAGE TRAP THIS SECTION EXISTS TO AVOID: a calibrator fitted on the
# same rows it is then scored against will look like a spectacular
# improvement, because it is not correcting genuine miscalibration -- it is
# memorising that exact eval fold's outcomes. The rule applied here is
# identical in kind to blueprint §7.2's ("outcomes may score a decision,
# never inform it"), one level down: outcomes may score a PREDICTION, never
# inform the MAPPING that produced it.
#
# The fix: `_inner_calibration_split` carves each fold's own TRAINING window
# (rows strictly before the eval fold -- walk_forward_validate's existing
# `train`) into two chronologically ordered pieces: `inner_train` (earlier
# rounds) and `calib_holdout` (the most recent rounds still strictly before
# the eval fold). The raw model is refit on `inner_train` ONLY, scored on
# `calib_holdout` -- genuinely out-of-sample with respect to the model that
# produced those predictions -- and the isotonic calibrator is fitted on
# THOSE (raw_prediction, actual_outcome) pairs. The eval fold itself is never
# touched by any of this; it only ever supplies the y_true/p_model pair
# walk_forward_validate already computed the ordinary (uncalibrated) way, to
# which the calibrator (fitted on a strictly earlier slice) is then applied.
#
# Attacked directly, not just asserted correct (this session's standing
# rule): `tests/test_minutes.py::
# test_calibrator_fit_directly_on_eval_data_would_leak_and_look_suspiciously_
# good` constructs the FORBIDDEN path by hand (fit the calibrator on the eval
# fold's own predictions/outcomes) and shows it produces a strictly better
# log-loss than the honest nested version on the same real-shaped synthetic
# data -- i.e. the leak this section exists to prevent is a real, measurable
# effect, not a hypothetical one, and the shipped code path never takes it.
# `tests/test_minutes.py::
# test_nested_calibration_cannot_see_a_folds_own_or_future_outcomes` is the
# walk-forward future-fold-isolation attack (already proven against a
# deliberately-broken version once for the raw model, §7 above) repeated for
# `p_model_calibrated` specifically.
# ---------------------------------------------------------------------------


# `IsotonicCalibrator`/`fit_isotonic_calibrator` moved to `fplai.calibration`
# session s005 (consolidation) -- this was the THIRD independently-drifting
# copy of the same mechanism (`fplai.models.cards`/`saves` each carried
# their own too), and the Jeffreys-smoothing fix below had to be found and
# applied three separate times before anyone noticed the shared root cause.
# `fplai.calibration` is not a sibling MODEL (it fits nothing, predicts
# nothing, touches no store) so importing it is not the cross-model coupling
# this module's own "no cross-model import" convention forbids -- see that
# module's own docstring, "Session s005 (consolidation)", for the full diff
# against the other two copies (none found; a pure refactor) and why the
# fitting mechanism stays generic while the out-of-sample NESTING below
# (`_inner_calibration_split`, walk-forward fold boundaries) stays here,
# where the fold actually lives.


def _inner_calibration_split(
    train: pl.DataFrame, *, holdout_frac: float, min_holdout_rows: int, min_inner_train_rows: int
) -> tuple[pl.DataFrame, pl.DataFrame] | None:
    """Split a fold's own `train` slice (already strictly before the eval
    fold) into `inner_train` (earlier rounds) and `calib_holdout` (the most
    recent `holdout_frac` fraction of DISTINCT `_chronological_rank` values
    still inside `train`) -- never by row count or a random split, which
    would let two fixtures of the same round land on opposite sides and
    leak trailing features computed from one into the calibrator fit from
    the other's outcome (the same double-gameweek boundary §4/module
    docstring already protects for the outer split).

    Returns `None` (not a degenerate split) when there is not yet enough
    data for a meaningful inner fit+holdout -- the caller falls back to
    reporting the raw (uncalibrated) prediction for that fold rather than
    fabricating a calibrator from too little evidence. This is the walk-
    forward's own warmup-floor pattern (`min_train_rows`) applied one level
    deeper, not a new concept."""
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


# ---------------------------------------------------------------------------
# Fitted model + PMF
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class MinutesModelParams:
    """A fitted minutes model: the state (START/SUB/UNUSED) softmax, plus
    two band-conditional softmaxes (fit on START-only and SUB-only rows
    respectively). UNUSED's band distribution is not fitted — it is
    structurally degenerate (verified live: UNUSED always means
    `minutes=0`, module docstring) and fixed to `{"0": 1.0}`.
    `eq=False`: this dataclass carries numpy arrays, whose `==` is
    elementwise and does not collapse to a single bool — never compare two
    `MinutesModelParams` for equality."""

    feature_spec: MinutesFeatureSpec
    state_weights: np.ndarray  # (n_features, 3), columns ordered per STATES
    start_band_weights: np.ndarray  # (n_features, 6), columns ordered per MINUTE_BANDS
    sub_band_weights: np.ndarray  # (n_features, 6)
    config: MinutesModelConfig
    as_of: datetime
    seasons_used: tuple[str, ...]
    n_rows_used: int
    p_start_calibrator: IsotonicCalibrator | None = None
    """`None` unless `fit_minutes_model(..., calibrate=True)` fitted one --
    see that function's docstring and the module's "Nested out-of-sample
    P(start) calibration" section. Applied to `state_weights`' own START
    marginal at predict time (`predict_minutes_pmf`), never to a different
    model's output -- fitting and applying the same quantity is what keeps
    this internally consistent (see `fit_minutes_model`'s docstring for why
    this is a SEPARATE fit from `walk_forward_validate(calibrate=True)`'s
    gate-diagnostic calibrator, not a shared one)."""


def fit_minutes_model(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    config: MinutesModelConfig = MinutesModelConfig(),
    calibrate: bool = True,
    calibration_holdout_frac: float = 0.2,
    min_calibration_holdout_rows: int = 200,
    min_inner_train_rows: int = 200,
) -> MinutesModelParams:
    """Fit the deployed minutes model. `calibrate=True` is the default AS OF
    SESSION S003, evidence-based, not assumed, and RE-CONFIRMED session
    s005 after two intervening fixes changed the raw series and the
    calibrator's own saturation behaviour respectively -- see `docs/wiki/
    model-minutes.md` §§12-14 for the full before/after history rather than
    a hardcoded number here, which would go stale exactly as s003's
    original figures did across §13 (l2 scaling fix) and §14 (isotonic
    saturation fix). §14's headline: post both fixes, the nested calibrator
    still narrows Brier/log-loss to within noise of raw (log-loss +0.6%,
    Brier a wash) while cutting ECE ~65% (width-binned) and moving the
    calibration slope from 0.896 to 0.963 -- the reliability axis §7.1
    actually gates on. Pass `calibrate=False` to opt back OUT
    (bit-identical to this module's pre-s003 behaviour) -- e.g. for a
    caller that cannot afford the extra inner fit, or that wants to reason
    about the raw softmax specifically. A caller whose training window is
    too small never notices the difference either way: `_inner_calibration_
    split` returns `None` and this function silently (with a logged
    warning) falls back to the raw model regardless of which way this flag
    was set.

    `calibrate=True` fits a nested, out-of-sample P(start)
    isotonic calibrator (session s003, blueprint §7.1 -- see the "Nested
    out-of-sample P(start) calibration" section above `MinutesModelParams`).
    The calibrator is fitted on a chronologically-EARLIER slice of this same
    `as_of` training window only (`_inner_calibration_split`): a fresh model
    is fit on `inner_train`, scored out-of-sample on `calib_holdout`, and the
    calibrator learned from THOSE (prediction, outcome) pairs -- never from
    the live predictions this returned model will later be asked to make.
    This is a SEPARATE fit from `walk_forward_validate(calibrate=True)`'s own
    inner calibrator: that one calibrates a standalone 2-class binary
    softmax fit fresh per gate-fold (the quantity the §7.1 gate has always
    scored); this one calibrates THIS function's own 3-class `state_weights`
    START marginal (the quantity `predict_minutes_pmf` actually serves) --
    related, both out-of-sample, but not numerically identical, because they
    are two different model formulations. Deliberate, not an oversight: the
    alternative (unifying the two into one shared binary model) is a real
    design change to what "the model" means in this module, out of this
    task's scope -- see the punch-out finding.

    If the training window is too small for a meaningful inner split,
    returns the RAW (uncalibrated) model with a logged warning rather than
    fabricating a calibrator from too little evidence -- `p_start_calibrator`
    stays `None`, and every PMF this model predicts reads
    `calibration_method="raw_uncalibrated"`.
    """
    table = build_training_table(store, as_of=as_of, seasons=seasons, config=config)
    params = _fit_from_table(table, config=config, as_of=as_of)
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
            "fit_minutes_model(calibrate=True): training window too small for a "
            "meaningful inner calibration split (need >= %d inner-train rows and "
            ">= %d holdout rows) -- shipping the raw (uncalibrated) model; every "
            "predicted PMF will read calibration_method='raw_uncalibrated'.",
            min_inner_train_rows,
            min_calibration_holdout_rows,
        )
        return params

    inner_train, calib_holdout = split
    inner_params = _fit_from_table(inner_train, config=config, as_of=as_of)
    X_holdout = _design_matrix(calib_holdout, inner_params.feature_spec)
    probs_holdout = _softmax_predict_batch(X_holdout, inner_params.state_weights)
    holdout_raw_p = probs_holdout[:, STATES.index("START")]
    holdout_y = np.array(
        [1.0 if s == "START" else 0.0 for s in calib_holdout["state"].to_list()], dtype=np.float64
    )
    calibrator = _fit_isotonic_calibrator(holdout_raw_p, holdout_y)
    return replace(params, p_start_calibrator=calibrator)


def _fit_band_conditional_or_uniform(
    subset: pl.DataFrame, spec: MinutesFeatureSpec, state_name: str, config: MinutesModelConfig
) -> np.ndarray:
    """Fit the band-conditional softmax for `state_name` on `subset`
    (already filtered to that state's rows). If `subset` is empty — a
    genuinely possible case on a small or synthetic training window, e.g.
    a short walk-forward warmup fold with zero SUB appearances yet, even
    though the real store always has both (module docstring, "Data") — this
    returns an all-zero weight matrix (a UNIFORM distribution over
    `MINUTE_BANDS`, since every logit is 0) rather than raising: "no
    training signal for this state yet, assume no preference among bands"
    is an honest, explicit fallback, not a silent wrong answer, and a
    warning is logged so it is never mistaken for a real fit."""
    if subset.is_empty():
        logger.warning(
            "minutes model: zero %s rows in this training window — band-conditional "
            "distribution for %s falls back to UNIFORM over MINUTE_BANDS (all-zero weights), "
            "not fitted from data.",
            state_name,
            state_name,
        )
        return np.zeros((spec.n_features, len(MINUTE_BANDS)), dtype=np.float64)
    X_subset = _design_matrix(subset, spec)
    band_idx = np.array([MINUTE_BANDS.index(b) for b in subset["band"].to_list()], dtype=np.int64)
    return _fit_softmax(X_subset, band_idx, len(MINUTE_BANDS), config)


def _fit_from_table(table: pl.DataFrame, *, config: MinutesModelConfig, as_of: datetime) -> MinutesModelParams:
    spec = _build_feature_spec(table)
    X = _design_matrix(table, spec)

    state_idx = np.array([STATES.index(s) for s in table["state"].to_list()], dtype=np.int64)
    state_weights = _fit_softmax(X, state_idx, len(STATES), config)

    start_table = table.filter(pl.col("state") == "START")
    sub_table = table.filter(pl.col("state") == "SUB")
    start_band_weights = _fit_band_conditional_or_uniform(start_table, spec, "START", config)
    sub_band_weights = _fit_band_conditional_or_uniform(sub_table, spec, "SUB", config)

    return MinutesModelParams(
        feature_spec=spec,
        state_weights=state_weights,
        start_band_weights=start_band_weights,
        sub_band_weights=sub_band_weights,
        config=config,
        as_of=as_of,
        seasons_used=tuple(sorted(table["season"].unique().to_list())),
        n_rows_used=table.height,
    )


@dataclass(frozen=True)
class MinutesPMF:
    """The model's actual output for one player-fixture: a full joint
    distribution over (state, band) — never a scalar (CLAUDE.md rule 5).
    `expected_minutes()`/`p_appearance_60_plus()` are convenience
    properties computed FROM this PMF, never a substitute for it.

    `calibration_method` is STRUCTURAL provenance, not a docstring promise
    -- same precedent `fplai.models.defensive_contribution.DCPMF.
    threshold_verified` sets: a declared field, present on every PMF,
    carried through `to_polars()` into the persisted derived row (see
    `_register_minutes_capability`'s `value_fields`), so a consumer reading
    a persisted `player.minutes_distribution@gameweek` row can tell which
    mapping produced its `probability` without reading this module's source.
    `"raw_uncalibrated"` (the default) means `p_state` is the softmax output
    directly; `"isotonic_v1"` means `p_state["START"]` was passed through a
    `MinutesModelParams.p_start_calibrator` and SUB/UNUSED rescaled to
    preserve sum-to-1 (see `predict_minutes_pmf`)."""

    element: int
    fixture: int
    p_state: dict[str, float]
    band_given_state: dict[str, tuple[float, ...]]
    calibration_method: str = "raw_uncalibrated"

    def __post_init__(self) -> None:
        total_state = sum(self.p_state.get(s, 0.0) for s in STATES)
        if abs(total_state - 1.0) > 1e-6:
            raise MinutesModelError(f"p_state does not sum to 1.0 (got {total_state}) for element={self.element}")
        for s in STATES:
            bands = self.band_given_state.get(s)
            if bands is None or len(bands) != len(MINUTE_BANDS):
                raise MinutesModelError(f"band_given_state[{s!r}] must have {len(MINUTE_BANDS)} entries")
            total_band = sum(bands)
            if abs(total_band - 1.0) > 1e-6:
                raise MinutesModelError(f"band_given_state[{s!r}] does not sum to 1.0 (got {total_band})")

    def p_start(self) -> float:
        return self.p_state["START"]

    def joint(self) -> dict[tuple[str, str], float]:
        return {
            (state, band): self.p_state[state] * p_band
            for state in STATES
            for band, p_band in zip(MINUTE_BANDS, self.band_given_state[state])
        }

    def expected_minutes(self) -> float:
        return sum(p * _BAND_MIDPOINT[band] for (_, band), p in self.joint().items())

    def p_appearance_60_plus(self) -> float:
        return sum(p for (_, band), p in self.joint().items() if band in _LONG_APPEARANCE_BANDS)

    def to_polars(self) -> pl.DataFrame:
        rows = [
            {
                "element": self.element,
                "fixture": self.fixture,
                "state": s,
                "band": b,
                "probability": p,
                "calibration_method": self.calibration_method,
            }
            for (s, b), p in self.joint().items()
        ]
        return pl.DataFrame(rows)


def predict_minutes_pmf(params: MinutesModelParams, feature_row: dict, *, element: int, fixture: int) -> MinutesPMF:
    """`feature_row` must carry every column in `params.feature_spec.
    numeric_columns` plus `position`/`team` — the same shape
    `build_training_table` produces per row (a caller predicting live would
    typically build one row the same way and pass its `.to_dicts()[0]`).

    If `params.p_start_calibrator` is set (`fit_minutes_model(...,
    calibrate=True)`), the raw softmax START probability is passed through
    it and SUB/UNUSED rescaled proportionally so `p_state` still sums to
    exactly 1.0 -- see module docstring, "Nested out-of-sample P(start)
    calibration". `MinutesPMF.calibration_method` records which happened."""
    spec = params.feature_spec
    row_table = pl.DataFrame({**{c: [feature_row[c]] for c in spec.numeric_columns}, "position": [feature_row["position"]], "team": [feature_row["team"]]})
    x = _design_matrix(row_table, spec)[0]

    p_state_vec = _softmax_predict(x, params.state_weights)
    p_state = {s: float(p_state_vec[i]) for i, s in enumerate(STATES)}
    calibration_method = "raw_uncalibrated"

    if params.p_start_calibrator is not None:
        raw_start = p_state["START"]
        calibrated_start = float(params.p_start_calibrator.apply(np.array([raw_start]))[0])
        calibrated_start = min(max(calibrated_start, 0.0), 1.0)
        remaining_raw = 1.0 - raw_start
        remaining_calibrated = 1.0 - calibrated_start
        if remaining_raw > 1e-9:
            scale = remaining_calibrated / remaining_raw
            p_sub = p_state["SUB"] * scale
            p_unused = p_state["UNUSED"] * scale
        else:
            # raw_start was ~1.0 -- no SUB/UNUSED mass to rescale proportionally.
            # An honest, stated fallback for this degenerate corner (split the
            # newly freed mass evenly), never a silent divide-by-zero.
            p_sub = remaining_calibrated / 2.0
            p_unused = remaining_calibrated / 2.0
        p_state = {"START": calibrated_start, "SUB": p_sub, "UNUSED": p_unused}
        calibration_method = "isotonic_v1"

    p_band_start = _softmax_predict(x, params.start_band_weights)
    p_band_sub = _softmax_predict(x, params.sub_band_weights)
    band_given_state = {
        "START": tuple(float(v) for v in p_band_start),
        "SUB": tuple(float(v) for v in p_band_sub),
        "UNUSED": tuple(1.0 if b == "0" else 0.0 for b in MINUTE_BANDS),
    }
    return MinutesPMF(
        element=element,
        fixture=fixture,
        p_state=p_state,
        band_given_state=band_given_state,
        calibration_method=calibration_method,
    )


# ---------------------------------------------------------------------------
# Walk-forward calibration (blueprint §7.1)
# ---------------------------------------------------------------------------


def _log_loss(y_true: Sequence[int], p: Sequence[float], eps: float = 1e-12) -> float:
    n = len(y_true)
    if n == 0:
        raise MinutesModelError("log_loss over zero rows is undefined")
    total = 0.0
    for y, prob in zip(y_true, p):
        prob = min(max(prob, eps), 1.0 - eps)
        total += -(y * math.log(prob) + (1 - y) * math.log(1 - prob))
    return total / n


def _brier(y_true: Sequence[int], p: Sequence[float]) -> float:
    n = len(y_true)
    if n == 0:
        raise MinutesModelError("brier score over zero rows is undefined")
    return sum((prob - y) ** 2 for y, prob in zip(y_true, p)) / n


@dataclass(frozen=True)
class ReliabilityBin:
    bin_lo: float
    bin_hi: float
    n: int
    mean_predicted: float
    observed_rate: float


def reliability_diagram(y_true: Sequence[int], p: Sequence[float], n_bins: int = 10) -> tuple[ReliabilityBin, ...]:
    """Standalone (module-level, not a `WalkForwardResult` method) so it can
    score ANY (y_true, p) pair -- the model's raw predictions, its
    calibrated predictions, or a per-position slice of either -- from one
    implementation, rather than four hand-copied loops. `WalkForwardResult.
    reliability_diagram()` below is now a thin wrapper over this."""
    n = len(y_true)
    if n == 0:
        raise MinutesModelError("reliability_diagram over zero rows is undefined")
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
    """ECE: the sample-size-weighted mean absolute gap between predicted and
    observed rate across `reliability_diagram`'s bins -- the standard scalar
    summary of a reliability diagram (blueprint §7.1 names both explicitly).
    A single number that can hide a bad bin the way any pooled statistic
    can -- always read alongside the full table, never instead of it."""
    n = len(y_true)
    if n == 0:
        raise MinutesModelError("expected_calibration_error over zero rows is undefined")
    bins = reliability_diagram(y_true, p, n_bins=n_bins)
    return sum(b.n * abs(b.mean_predicted - b.observed_rate) for b in bins) / n


def _calibration_slope_intercept(y_true: Sequence[int], p: Sequence[float]) -> tuple[float, float]:
    """Cox calibration regression: fit `y ~ intercept + slope * logit(p)` by
    unregularised binary logistic regression (Newton's method / IRLS, fixed
    zero init -- deterministic, no randomness, CLAUDE.md rule 7). Perfect
    calibration is `slope=1.0, intercept=0.0`; `slope < 1` is the classic
    signature of exactly this module's diagnosed defect (predictions too
    close to 0.5, i.e. not extreme enough) -- but it is a ONE-NUMBER summary
    of a possibly non-linear miscalibration shape, never a substitute for
    the full reliability table. A small ridge term guards only against
    near-perfect separation in a small fold; it is not applied to any
    model this module actually predicts with."""
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
    """The full §7.1 calibration report for one (y_true, p) pair -- log-loss,
    Brier, ECE, Cox calibration slope/intercept, and the full reliability
    table. One dataclass so pooled and per-position, raw and calibrated, are
    all the same shape and can be printed/compared identically."""

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


@dataclass(frozen=True)
class WalkForwardResult:
    """Every fold's predictions for `P(start)`, model vs. both naive
    baselines, plus the actual outcome — enough to compute log-loss/Brier
    for all three and a reliability diagram for the model. See module
    docstring, "Validation".

    `p_model_calibrated`/`n_folds_calibrated` are populated only when
    `walk_forward_validate(..., calibrate=True)` was used (session s003,
    blueprint §7.1) — see the module's "Nested out-of-sample P(start)
    calibration" section. `position` (per-eval-row) is always populated,
    calibrated or not, so `*_by_position` reporting never depends on the
    calibration flag."""

    n_folds: int
    y_true: tuple[int, ...]
    p_model: tuple[float, ...]
    p_baseline_last_gw: tuple[float, ...]
    p_baseline_position_rate: tuple[float, ...]
    position: tuple[str, ...] = ()
    p_model_calibrated: tuple[float, ...] | None = None
    n_folds_calibrated: int = 0

    def model_log_loss(self) -> float:
        return _log_loss(self.y_true, self.p_model)

    def model_brier(self) -> float:
        return _brier(self.y_true, self.p_model)

    def baseline_last_gw_log_loss(self) -> float:
        return _log_loss(self.y_true, self.p_baseline_last_gw)

    def baseline_last_gw_brier(self) -> float:
        return _brier(self.y_true, self.p_baseline_last_gw)

    def baseline_position_rate_log_loss(self) -> float:
        return _log_loss(self.y_true, self.p_baseline_position_rate)

    def baseline_position_rate_brier(self) -> float:
        return _brier(self.y_true, self.p_baseline_position_rate)

    def beats_both_baselines(self) -> bool:
        """The gate, per §7.1: strictly lower log-loss AND Brier than BOTH
        naive baselines. Beating neither/one is a failed gate — this
        function reports the verdict; it is the caller's job (the script,
        the wiki, the punch-out) to report a failure honestly rather than
        tune until it passes (this task's brief, explicit)."""
        return (
            self.model_log_loss() < self.baseline_last_gw_log_loss()
            and self.model_log_loss() < self.baseline_position_rate_log_loss()
            and self.model_brier() < self.baseline_last_gw_brier()
            and self.model_brier() < self.baseline_position_rate_brier()
        )

    def reliability_diagram(self, n_bins: int = 10) -> tuple[ReliabilityBin, ...]:
        return reliability_diagram(self.y_true, self.p_model, n_bins=n_bins)

    def raw_metrics(self, n_bins: int = 10) -> CalibrationMetrics:
        """The full §7.1 report for the RAW (uncalibrated) predictions —
        available regardless of `calibrate`."""
        return _calibration_metrics(self.y_true, self.p_model, n_bins=n_bins)

    def calibrated_metrics(self, n_bins: int = 10) -> CalibrationMetrics:
        """The same report for the nested-calibrated predictions. Raises if
        `walk_forward_validate` was not run with `calibrate=True`."""
        if self.p_model_calibrated is None:
            raise MinutesModelError(
                "walk_forward_validate was not run with calibrate=True -- no calibrated "
                "predictions to report."
            )
        return _calibration_metrics(self.y_true, self.p_model_calibrated, n_bins=n_bins)

    def _positions_present(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.position)))

    def raw_metrics_by_position(self, n_bins: int = 10) -> dict[str, CalibrationMetrics]:
        out: dict[str, CalibrationMetrics] = {}
        for pos in self._positions_present():
            idx = [i for i, p in enumerate(self.position) if p == pos]
            out[pos] = _calibration_metrics(
                [self.y_true[i] for i in idx], [self.p_model[i] for i in idx], n_bins=n_bins
            )
        return out

    def calibrated_metrics_by_position(self, n_bins: int = 10) -> dict[str, CalibrationMetrics]:
        if self.p_model_calibrated is None:
            raise MinutesModelError(
                "walk_forward_validate was not run with calibrate=True -- no calibrated "
                "predictions to report."
            )
        out: dict[str, CalibrationMetrics] = {}
        for pos in self._positions_present():
            idx = [i for i, p in enumerate(self.position) if p == pos]
            out[pos] = _calibration_metrics(
                [self.y_true[i] for i in idx], [self.p_model_calibrated[i] for i in idx], n_bins=n_bins
            )
        return out


def walk_forward_validate(
    table: pl.DataFrame,
    *,
    eval_seasons: Sequence[str],
    min_train_rows: int = 200,
    config: MinutesModelConfig = MinutesModelConfig(),
    calibrate: bool = False,
    calibration_holdout_frac: float = 0.2,
    min_calibration_holdout_rows: int = 200,
    min_inner_train_rows: int = 200,
) -> WalkForwardResult:
    """Refits the STATE model (the only piece the §7.1 gate scores) at
    every `(season, round)` fold whose season is in `eval_seasons`, using
    only rows strictly earlier (by `_chronological_rank`, built in
    `build_training_table`) than that fold. `min_train_rows` skips folds too
    early to have a meaningful training set (a warmup floor, not a leakage
    concern — every fold still trains on strictly-prior data regardless).

    Adversarially attacked (`tests/test_minutes.py::
    test_walk_forward_cannot_see_a_fold_s_own_outcome_even_via_a_shuffled_
    row_order`), per this session's standing rule: shuffling the input
    table's row order before calling this function must not change a single
    fold's prediction, because the train/eval split is computed from
    `_chronological_rank`, not from row position.

    `calibrate=True` (session s003, blueprint §7.1) additionally computes a
    NESTED out-of-sample calibrated prediction for every eval row -- see the
    module's "Nested out-of-sample P(start) calibration" section above for
    the full design and its leakage boundary. Per fold: `train` (rows
    strictly before this fold, exactly as before) is split by
    `_inner_calibration_split` into `inner_train`/`calib_holdout`, a model is
    fit on `inner_train` alone and scored on `calib_holdout`
    (out-of-sample), and an `IsotonicCalibrator` is fitted from those pairs.
    The FULL-`train` model (identical to the `calibrate=False` path — the
    raw prediction reported is unaffected by this flag) then predicts the
    eval fold as always, and THAT prediction is what the calibrator is
    applied to. A fold too small for a meaningful inner split falls back to
    reporting the raw prediction for `p_model_calibrated` too (not skipped,
    not crashed) — `n_folds_calibrated` counts how many folds actually got a
    real calibrator, so this fallback rate is visible, not silent.
    """
    if "_chronological_rank" not in table.columns:
        raise MinutesModelError("table must carry _chronological_rank -- build it via build_training_table")

    fold_keys = (
        table.filter(pl.col("season").is_in(list(eval_seasons)))
        .select(["season", "round", "_chronological_rank"])
        .unique()
        .sort("_chronological_rank")
    )

    y_true: list[int] = []
    p_model: list[float] = []
    p_model_calibrated: list[float] = []
    p_baseline_last_gw: list[float] = []
    p_baseline_position_rate: list[float] = []
    positions: list[str] = []
    n_folds = 0
    n_folds_calibrated = 0

    for season, round_, rank in fold_keys.iter_rows():
        train = table.filter(pl.col("_chronological_rank") < rank)
        eval_rows = table.filter(pl.col("_chronological_rank") == rank)
        if train.height < min_train_rows or eval_rows.is_empty():
            continue

        n_folds += 1
        spec = _build_feature_spec(train)
        X_train = _design_matrix(train, spec)
        y_train_idx = np.array([1 if s == "START" else 0 for s in train["state"].to_list()], dtype=np.int64)
        # Binary softmax (2-class) is exactly logistic regression for
        # P(start) -- the gate's own target -- fit the same deterministic
        # way as the 3-way state model, just restricted to 2 classes here
        # since the walk-forward gate only ever scores P(start).
        weights = _fit_softmax(X_train, y_train_idx, 2, config)

        position_rate = (
            train.group_by("position")
            .agg(((pl.col("state") == "START").sum() / pl.len()).alias("rate"))
        )
        position_rate_map = dict(zip(position_rate["position"].to_list(), position_rate["rate"].to_list()))
        overall_rate = float((train["state"] == "START").sum()) / train.height

        X_eval = _design_matrix(eval_rows, spec)
        raw_eval_p: list[float] = []
        for i, row in enumerate(eval_rows.iter_rows(named=True)):
            y_true.append(1 if row["state"] == "START" else 0)
            p = _softmax_predict(X_eval[i], weights)[1]
            p_model.append(float(p))
            raw_eval_p.append(float(p))
            positions.append(row["position"])

            prev = row["prev_round_any_start"]
            baseline1 = float(prev) if prev is not None else position_rate_map.get(row["position"], overall_rate)
            p_baseline_last_gw.append(baseline1)

            p_baseline_position_rate.append(position_rate_map.get(row["position"], overall_rate))

        if calibrate:
            split = _inner_calibration_split(
                train,
                holdout_frac=calibration_holdout_frac,
                min_holdout_rows=min_calibration_holdout_rows,
                min_inner_train_rows=min_inner_train_rows,
            )
            if split is None:
                p_model_calibrated.extend(raw_eval_p)
            else:
                inner_train, calib_holdout = split
                inner_spec = _build_feature_spec(inner_train)
                X_inner = _design_matrix(inner_train, inner_spec)
                y_inner_idx = np.array(
                    [1 if s == "START" else 0 for s in inner_train["state"].to_list()], dtype=np.int64
                )
                inner_weights = _fit_softmax(X_inner, y_inner_idx, 2, config)

                X_holdout = _design_matrix(calib_holdout, inner_spec)
                probs_holdout = _softmax_predict_batch(X_holdout, inner_weights)
                holdout_raw_p = probs_holdout[:, 1]
                holdout_y = np.array(
                    [1.0 if s == "START" else 0.0 for s in calib_holdout["state"].to_list()], dtype=np.float64
                )
                calibrator = _fit_isotonic_calibrator(holdout_raw_p, holdout_y)
                calibrated = calibrator.apply(np.array(raw_eval_p, dtype=np.float64))
                p_model_calibrated.extend(float(v) for v in calibrated)
                n_folds_calibrated += 1

    if n_folds == 0:
        raise MinutesModelError(
            f"no usable folds for eval_seasons={eval_seasons!r} with min_train_rows={min_train_rows} — "
            "every candidate fold had too little training data or no eval rows."
        )

    return WalkForwardResult(
        n_folds=n_folds,
        y_true=tuple(y_true),
        p_model=tuple(p_model),
        p_baseline_last_gw=tuple(p_baseline_last_gw),
        p_baseline_position_rate=tuple(p_baseline_position_rate),
        position=tuple(positions),
        p_model_calibrated=tuple(p_model_calibrated) if calibrate else None,
        n_folds_calibrated=n_folds_calibrated,
    )


# ---------------------------------------------------------------------------
# Derived-capability registration (import time — Story A's resolution,
# fplai.schemas' "Phase 2, E5" section; see also model-team-strength.md §10)
# ---------------------------------------------------------------------------


def _register_minutes_capability() -> FactTableSchema:
    existing = CANONICAL_SCHEMAS.get(PLAYER_MINUTES_DISTRIBUTION_GAMEWEEK)
    if existing is not None:
        return existing
    return register_derived_capability(
        PLAYER_MINUTES_DISTRIBUTION_GAMEWEEK,
        entity_key=("season", "round", "element", "fixture", "state", "band"),
        value_fields=("probability", "calibration_method"),
        dataset=PLAYER_MINUTES_DISTRIBUTION_DATASET,
        description=(
            "Minutes model v1 (blueprint §4.1, E5) -- LONG format, one row "
            "per (player, fixture, state, band) combination with its joint "
            "probability (same long-format convention team.match_stats@"
            "match/player.season_stats@season already use for a sparse/"
            "enumerated key). `state` in ('START','SUB','UNUSED'), `band` "
            "in ('0','1-29','30-59','60-74','75-89','90+') -- see "
            "fplai.models.minutes' module docstring for the boundaries' "
            "justification. Summing `probability` over every (state, band) "
            "row for one (season, round, element, fixture) must equal 1.0. "
            "`calibration_method` (session s003, blueprint §7.1) is "
            "STRUCTURAL provenance, same role "
            "fplai.models.defensive_contribution's `threshold_verified` "
            "plays: 'raw_uncalibrated' or 'isotonic_v1', declared as a "
            "value_field rather than left to a docstring promise -- see "
            "MinutesPMF's own docstring for the full account."
        ),
    )


MINUTES_SCHEMA: FactTableSchema = _register_minutes_capability()


def pmfs_to_rows(pmfs: Sequence[MinutesPMF], *, season: str, round_: int) -> pl.DataFrame:
    frames = [pmf.to_polars() for pmf in pmfs]
    combined = pl.concat(frames) if frames else pl.DataFrame(
        schema={
            "element": pl.Int64,
            "fixture": pl.Int64,
            "state": pl.String,
            "band": pl.String,
            "probability": pl.Float64,
            "calibration_method": pl.String,
        }
    )
    return combined.with_columns(pl.lit(season).alias("season"), pl.lit(round_).alias("round"))


def write_minutes_pmfs(
    store: BitemporalStore,
    pmfs: Sequence[MinutesPMF],
    *,
    season: str,
    round_: int,
    valid_at: datetime,
    calibration: CalibrationReference,
    source: str = "fplai.models.minutes",
    skip_if_unchanged: bool = True,
) -> WriteResult:
    """The only sanctioned way to persist minutes predictions — routes
    through `fplai.derived.write_derived` (never a bare `store.write()`).
    `calibration` must be supplied by the caller (unlike
    `write_team_strength`, which computes an in-sample residual itself):
    this module's meaningful calibration numbers come from
    `walk_forward_validate`'s OUT-OF-SAMPLE gate, computed over a
    validation window a single fit call has no way to know about — passing
    it explicitly keeps that distinction honest rather than fabricating an
    in-sample number that would understate this model's real uncertainty.
    """
    if not pmfs:
        raise MinutesModelError("write_minutes_pmfs called with zero PMFs — nothing to write")
    rows = pmfs_to_rows(pmfs, season=season, round_=round_)

    derived_from = [
        DerivationInput(
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            entity_key={"season": season},
            note=(
                "trailing per-player-gameweek starts/minutes history and this "
                "fixture's position/team/was_home, aggregated per the feature "
                "engineering in fplai.models.minutes -- whole-season aggregate "
                "input, not an individually-named row subset."
            ),
        )
    ]

    return write_derived(
        store,
        PLAYER_MINUTES_DISTRIBUTION_GAMEWEEK,
        rows,
        valid_at=valid_at,
        observed_at=datetime.now(timezone.utc),
        source=source,
        derived_from=derived_from,
        calibration=calibration,
        skip_if_unchanged=skip_if_unchanged,
    )
