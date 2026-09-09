"""Team strength — Dixon-Coles on xG, decay-weighted. Blueprint §4 (E5),
§4.2, §4.3, §12.2. The first real model in the project.

This module fits per-team attack/defence parameters plus a home-advantage
term and the Dixon-Coles low-score dependence correction, and turns them
into a full joint scoreline PMF `P(home_goals, away_goals)` per fixture —
never a scalar expected-goals pair (CLAUDE.md rule 5: "a scalar xPts at a
module boundary is a design error", generalised here to the team model's
own output).

## The xG-versus-goals boundary — a design decision, stated and justified

Blueprint §4.2: fit on xG, "the lower-variance signal", because a single
match's goal count carries finishing-luck variance on top of the same
underlying scoring process xG is meant to estimate more stably. But xG is
only available from 2022-23 onward in this store (verified live,
2026-08-21: `vaastav_player_gameweek_stats.expected_goals` is 100% NULL
for 2019-20/2020-21/2021-22 and 100% populated 2022-23 onward — a hard
boundary, not a gradual thinning). Three options were on the table:
decline the pre-2022-23 seasons entirely, fit xG-only where available and
goals with a different noise assumption elsewhere, or fit everything on
goals and ignore xG. This module does the second:

- **Every match contributes to the SAME attack/defence/home-advantage fit**
  (`_fit_attack_defence_home`), via a quasi-Poisson log-likelihood whose
  target is xG where the season has it, actual team goals where it does
  not. This treats xG as a lower-noise draw from the same latent scoring
  rate the Poisson mean represents — standard practice in public xG-based
  team-strength models, and explicit here rather than assumed.
- **Every goals-sourced match is down-weighted** relative to an
  xG-sourced one, via `TeamStrengthConfig.goals_source_weight` (default
  0.6, on top of the time-decay weight) — this is the "different noise
  assumption" the brief asked for: a goals observation is noisier evidence
  of the same underlying rate, so it should move the fit less than an xG
  observation from the same era's recency. 0.6 is a stated, deliberately
  round, UNCALIBRATED starting point (documented the same way
  `derived.CalibrationReference`'s `residual_mean`/`residual_std` is
  flagged as a placeholder in `fplai/derived.py` — this number should move
  once the Phase 2 calibration gate, blueprint §7.1, has real Brier/
  log-loss/RPS numbers to tune it against, not be trusted as final).
- **Declining the early seasons entirely was rejected** because Phase 1
  already showed multi-season data materially changes what a backtest can
  say (six seasons scored, blueprint §7.2) — three fewer seasons of
  team-strength history would weaken exactly the promoted/relegated-team
  cases (§below) this model needs the most history for.

## The low-score correction cannot be fit on xG — a second, separate stage

The Dixon-Coles `tau(x, y)` correction is defined for actual match outcomes
(0-0, 1-0, 0-1, 1-1) — it has no meaning for a continuous xG value ("0.3
expected home goals, 0.7 expected away goals" is not a low-score
scoreline). So this module fits attack/defence/home-advantage in ONE
stage (against the xG/goals blend above), then fits `rho` in a SEPARATE
stage against ACTUAL FULL-TIME GOALS ONLY (`team_h_score`/`team_a_score`,
always present, every season), holding the first stage's parameters fixed.
This is a legitimate two-stage (profile) MLE, not a shortcut: the two
stages literally cannot share one likelihood because the two stages have
different response variables (a continuous xG-or-goals blend; a discrete
goal scoreline), and `rho`'s effect on the likelihood is exactly zero for
any match outside the four low-score cells, so nothing is lost by fitting
it in isolation. See `_fit_rho`.

## The promoted-team prior

A team with zero matches in the fitting window (promoted, or newly
returned from relegation beyond the fit's lookback) gets `attack`/`defence`
initialised at, and regularised toward, `TeamStrengthConfig.
promoted_team_attack_prior`/`promoted_team_defence_prior` on the model's
log scale — see `_fit_attack_defence_home`'s docstring for exactly how,
and why this is provably isolated from every other team's fitted value.

**Session s003, 2026-08-22 — no longer `0.0` (league average).** Before
this session the prior WAS exactly `0.0`: "assume a newly-promoted side is
average until data says otherwise", falling out of the same L2 ridge
regularisation that resolves the model's gauge freedom. That default was
always flagged as a documented limitation, not a claimed feature, and the
symptom was real and visible: an unfiltered ranking put Coventry City,
Hull City and Ipswich Town inside or beside the "best 8 defence" band,
ahead of genuinely fitted values like Brighton's and Man Utd's — league
average is a materially generous prior for a team that has, empirically,
never once landed there in this store's 6 promotion cohorts (2020-21
through 2025-26, 18 promoted-team-season instances, 17/18 worse on
defence, 16/18 weaker on attack — see `TeamStrengthConfig.
promoted_team_attack_prior`'s docstring and
`docs/wiki/model-team-strength.md` §3 for the full estimator and
instance table). The two exceptions were both Leeds (2020-21 and
2025-26) — a real, named exception the estimate does not smooth away,
not evidence the estimate is wrong.

This remains a genuine simplification, stated rather than hidden: it is
ONE prior for every promoted team, not conditioned on which division they
came from, their transfer spend, or their previous top-flight spell (a
yo-yo club like Ipswich Town, task 2's own live-verified fix, has real
recent top-flight history the moment it is correctly identity-resolved —
see "Team identity" below — and should reasonably get a LESS pessimistic
prior than a club promoted for the first time in the sport's history; this
model does not yet make that distinction). `TeamStrengthParams.
teams_with_no_history` still names exactly which teams got this prior on
a given fit, so a downstream consumer (or a future revision of this
module) can find and treat them differently — e.g. blend this prior with
a club-specific Championship-form signal, not built here.

## Determinism and the seed (CLAUDE.md rule 7)

This model has **no stochastic component**. Parameters initialise at
0.0 (the gauge-neutral point — see above), or at the promoted-team prior
for a team with no training-window matches (session s003, see above), and
both optimisation stages
(`_fit_attack_defence_home`'s Adam ascent, `_fit_rho`'s ternary search) are
fully deterministic functions of the training data and
`TeamStrengthConfig`'s hyperparameters. No `seed:` parameter is threaded
through this module, and none should be added as decoration — an unused
seed parameter would be worse than none (dishonest determinism theatre).
**The seam, if one is ever needed**: a future revision that adds randomised
initialisation, minibatching, or a Monte Carlo residual-uncertainty
estimate would need a `seed: int` parameter at that point, threaded into
whatever `random`/`random.Random(seed)` instance it introduces — there is
none to thread today because there is no randomness to seed.

## The odds-shrinkage seam (deliberately not built here)

Blueprint §4's team-strength row says "shrunk to odds" — explicitly out of
scope here per this task's brief, because blueprint §3.3 is unambiguous
that odds are a live-only overlay with no historical endpoint on the free
plan and therefore cannot enter a backtest. `shrink_to_odds` below is the
marked, unimplemented seam: it takes a fitted `ScorelinePMF` and a market-
implied probability and documents exactly where a live-time blend would
attach, without building it — see its docstring.

## Bitemporal fitting — as_of() is the wrong primitive for this dataset;
## effective_at() is the sanctioned one (blueprint §3.2, decision 2026-08-22)

The brief's own framing ("reads only `store.as_of(deadline_t)`") is the
GENERAL rule (blueprint §3.2). It is the wrong PRIMITIVE for THIS specific
dataset, for a reason `fplai.backtest.data` already discovered and
documented: `vaastav_player_gameweek_stats` was bulk-ingested in one
backfill session (2026-08-21), so every row's `observed_at` is
approximately "today", regardless of which historical season/gameweek the
row describes. `as_of(dataset, deadline_t)` filters on `observed_at <=
deadline_t` — for any real historical `deadline_t` (say, GW10 of
2020-21), that predicate is `<today's date> <= <a 2020 timestamp>`, which
is false for every row, so `as_of()` on this dataset returns EMPTY for any
genuinely historical deadline. This is not a hypothetical: it was verified
directly (`tests/test_team_strength.py::
test_store_as_of_is_empty_for_a_genuinely_historical_deadline_on_this_
dataset`) before writing a single line of the training-table builder,
per the "prove it fails first" standing rule.

**Session s003, 2026-08-22:** this module originally routed around the gap
above with `store.observations()` (the raw stream) plus a hand-written
`kickoff_time` filter — the same workaround `fplai.models.minutes`
independently arrived at, with a near-identical justifying comment. The
Architect closed that hole at the store level: `BitemporalStore.
effective_at()` is now the one sanctioned primitive for "state effective at
this deadline, resolved on a dataset's own declared valid-time column" —
see its docstring and `fplai.schemas.PLAYER_GAMEWEEK_STATS_GAMEWEEK`'s
`valid_time_column="kickoff_time"` declaration. `build_match_table` calls
it directly; the bitemporal safety property is unchanged (never let a
fixture that had not yet kicked off by `as_of` influence the fit,
`effective_at`'s boundary is strictly `<`, matched exactly to what this
module verified by hand before), it is just no longer this module's own
filter to hand-roll or get subtly wrong. `build_match_table`'s `as_of`
parameter stays REQUIRED (no default, no "now" fallback) for exactly the
reason `effective_at`'s own `effective_ts` has no default — an unbounded
call is the leakage bug waiting to happen.

CLAUDE.md's "attack your own guarantees from outside the sanctioned path"
rule was applied here directly, both before this migration (against the
hand-rolled filter) and re-verified after it (against `effective_at()`):
`tests/test_team_strength.py` includes an adversarial test that tries to
make a fit see a future match by a route other than passing a later
`as_of` (mutating a `MatchRecord`'s `kickoff` after the fact, and
confirming `fit_team_strength` has no code path that re-reads the store or
accepts anything but the frozen `MatchRecord`s it was given), a backdated-
`observed_at` smuggling attempt, and an exact-boundary case (`as_of ==
kickoff` must exclude, not include).

**A real, live-verified side effect of this migration, not a hidden one:**
`effective_at()` collapses to one row per entity key (blueprint §3.2 ruling
point 3, "exactly as as_of() does"), which the old hand-rolled
`observations()`-based filter never did. The real store carries 10 exact-
duplicate rows within a single write batch (`vaastav_player_gameweek_stats`,
2025-26, elements 100/391 — Bournemouth-adjacent players, none of them
Man City fixtures — verified live, 2026-08-22: same `batch_id`, byte-
identical content, an upstream archive artefact, not a bug introduced by
this store). Before this migration, `build_match_table`'s per-fixture xG
aggregation (`.sum()` over each side's players) silently double-counted
those 10 rows' `expected_goals` contribution wherever it was nonzero. After
migration, `effective_at()` deduplicates them before aggregation, which is
the CORRECT behaviour, not a regression — see this session's punch-out for
the exact before/after numbers. The pinned Man City attack rating
(`+0.4737`) is unaffected (verified: none of the 10 duplicate rows'
fixtures involve Man City, and the joint MLE's gradient for a team's
attack/defence parameter only accumulates from that team's own matches);
Bournemouth's own rating and the shared `home_advantage`/`rho` parameters
move by a magnitude too small to matter at 4-decimal precision.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import polars as pl

from fplai.derived import CalibrationReference, DerivationInput, write_derived
from fplai.gameweek_stats import read_player_gameweek_stats
from fplai.identity import TeamNameCanonicalisationMap
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    TEAM_STRENGTH_RATING_DATASET,
    TEAM_STRENGTH_RATING_GAMEWEEK,
    FactTableSchema,
    register_derived_capability,
)
from fplai.store import BitemporalStore, WriteResult

logger = logging.getLogger(__name__)

DATASET = "vaastav_player_gameweek_stats"
REQUIRED_COLUMNS = (
    "season",
    "fixture",
    "kickoff_time",
    "team",
    "was_home",
    "team_h_score",
    "team_a_score",
)


class TeamStrengthError(ValueError):
    """Misuse or a genuine data problem this module refuses to guess past —
    e.g. an unresolvable team name, a naive `as_of`, an empty training
    window, or a PMF that would normalise to zero mass."""


@dataclass(frozen=True)
class TeamStrengthConfig:
    """Every fitting hyperparameter, in one place, so a caller (or a future
    calibration pass) can see and override the full set rather than have
    them scattered as bare numbers through the fitting code. Defaults are
    stated, reasoned choices — see this module's docstring — not
    calibrated optima. None of them is hardcoded into a call site;
    `fit_team_strength` always takes a `TeamStrengthConfig`, defaulting to
    this dataclass's own defaults, never a bare literal at a call site
    (CLAUDE.md rule 4's spirit, applied to model hyperparameters).
    """

    half_life_days: float = 180.0
    """Time-decay half-life. ~half a season: short enough that a squad
    overhaul or managerial change stops dominating the fit within one
    season, long enough that a team with a light recent fixture list (a
    postponed match, an early-season promoted side) still draws on real
    history rather than nothing. UNCALIBRATED — a placeholder pending the
    Phase 2 gate's RPS/log-loss numbers, exactly like `goals_source_weight`
    below and `derived.CalibrationReference`'s residual placeholder."""

    goals_source_weight: float = 0.6
    """Relative weight of a goals-sourced (pre-2022-23) match's likelihood
    contribution versus an xG-sourced one (implicitly 1.0). See this
    module's docstring, "The xG-versus-goals boundary"."""

    attack_defence_l2: float = 0.05
    """L2 (ridge) penalty on every team's attack/defence log-scale
    parameter, applied around EACH team's own regularisation centre (`0.0`
    for a team with match history in the fit; `promoted_team_attack_prior`/
    `promoted_team_defence_prior` below for a team with none — see
    `_fit_attack_defence_home`). Does two jobs at once: (1) resolves the
    model's gauge freedom (adding a constant to every attack parameter and
    subtracting it from every defence parameter leaves every fixture's
    lambda/mu unchanged — a genuinely flat likelihood direction that
    unregularised gradient ascent would never pin down) without a separate
    sum-to-zero constraint or post-hoc recentring step — the vast majority
    of teams are still centred at `0.0`, so this still pins the gauge; (2)
    IS the promoted-team prior's DELIVERY mechanism — a team with no
    matches has zero likelihood gradient and simply stays exactly at its
    regularisation centre (see module docstring, "The promoted-team
    prior")."""

    promoted_team_attack_prior: float = -0.3061
    """Regularisation centre for a team's attack parameter when it has
    ZERO matches in the fit's training window, replacing the earlier
    `0.0` (league-average) assumption. **Estimated, session s003,
    2026-08-22** — not assumed, not tuned by eye: fit a SEPARATE
    single-season Dixon-Coles model for each of 2020-21..2025-26 (isolating
    each season from every other, so the estimate cannot leak across a
    promoted team's own later, established-club seasons), identified that
    season's 3 promoted clubs from `vaastav_team_identity`'s own
    season-over-season `code` set difference (not a hardcoded name list —
    the same clubs a football follower would name: Leeds/West Brom/Fulham
    2020-21 through Leeds/Sunderland/Burnley 2025-26), and averaged
    `attack[promoted_team] - mean(attack)` over the resulting 18
    promoted-team-season instances (this fit's own `attack`, already
    relative to that season's own gauge — see `attack_defence_l2` above —
    so no separate "league average" needs computing). See
    `docs/wiki/model-team-strength.md` §3 for the full instance-by-instance
    table and the estimator's own code. UNCALIBRATED against the Phase 2
    gate (blueprint §7.1) like `goals_source_weight`/`half_life_days`
    above — a real, data-derived starting point, not a final number."""

    promoted_team_defence_prior: float = 0.2216
    """Same estimator as `promoted_team_attack_prior`, defence side (higher
    = leakier = worse on this log scale). Both priors are POSITIVE evidence
    a promoted team starts below league average on attack AND above it on
    defensive leakiness — 17/18 instances were worse on defence, 16/18
    weaker on attack (the two exceptions were both Leeds, both years they
    were promoted: 2020-21 and 2025-26 — a real, named exception, not
    smoothed away)."""

    max_goals: int = 10
    """PMF truncation. `ScorelinePMF` renormalises after truncating, and
    reports the truncated-away mass — see its docstring."""

    n_adam_iterations: int = 400
    adam_lr: float = 0.05
    adam_beta1: float = 0.9
    adam_beta2: float = 0.999
    adam_eps: float = 1e-8

    rho_bounds: tuple[float, float] = (-0.25, 0.25)
    rho_search_iterations: int = 60


@dataclass(frozen=True)
class MatchRecord:
    """One fixture's team-level facts — the unit `fit_team_strength`
    trains on. `home_xg`/`away_xg` are `None` when the season predates xG
    coverage; `home_goals`/`away_goals` are always present (every season
    this store holds carries `team_h_score`/`team_a_score`)."""

    season: str
    fixture: int
    kickoff: datetime  # naive UTC, by this store's convention
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    home_xg: float | None
    away_xg: float | None

    @property
    def is_xg_sourced(self) -> bool:
        return self.home_xg is not None and self.away_xg is not None


def build_match_table(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    team_name_canonicaliser: TeamNameCanonicalisationMap | None = None,
    allow_live_season: bool = False,
) -> tuple[MatchRecord, ...]:
    """One row per (season, fixture) with kickoff strictly before `as_of` —
    the leakage boundary (see module docstring, "Bitemporal fitting").

    `as_of` is REQUIRED, tz-aware, no default — mirrors
    `BitemporalStore.observations()`'s own `until` parameter, and for the
    same reason: an unbounded call is exactly the leakage bug this
    signature exists to make impossible to write by accident.

    Aggregates `vaastav_player_gameweek_stats`'s player-per-fixture rows up
    to team-per-fixture: home/away team identified from `was_home`, goals
    from `team_h_score`/`team_a_score` (verified live: constant within a
    fixture, so `.first()` is exact, not a guess), xG from summing
    `expected_goals` across each side's players for that fixture (`None`
    when the season carries no xG at all — verified 100%/0% per season,
    never a partial mix within one season).

    2019-20 is excluded (not degraded): its `team` column is 100% NULL in
    this store (verified live) — the same gap `fplai.backtest.data`
    documents and excludes for the same reason (no way to tell home from
    away without it). Any fixture missing a resolvable home or away side
    after aggregation is excluded too, loudly logged, never guessed.

    `team_name_canonicaliser` — session s003, the Ipswich/Ipswich Town fix
    (module docstring §4/§7, `docs/wiki/model-team-strength.md` §4/§7).
    Omit it (the default) and this function's behaviour is UNCHANGED from
    before this fix: the raw `team` string is used as-is, so a club whose
    FPL-assigned name drifted between seasons is silently treated as two
    different teams. Pass a `fplai.identity.TeamNameCanonicalisationMap` —
    built by the CALLER, e.g. from `store.latest("vaastav_team_identity")`
    plus `store.latest("teams")` (see `scripts/fit_team_strength.py`) — to
    relabel every match's `team` value to that club's one canonical name
    BEFORE the home/away split, so the SAME club's history pools under one
    key across seasons regardless of which name FPL happened to use that
    season. Deliberately not fetched internally here, for the same reason
    `teams=` on `fit_team_strength` is caller-supplied, not fetched inside
    it: an implicit "read today's snapshot" inside a function this module
    ALSO uses for historical backtests is the leakage bug waiting to
    happen (blueprint §3.2) — the caller decides what "canonical" means
    for their own `as_of`, visibly, not this function.
    """
    if as_of.tzinfo is None:
        raise TeamStrengthError(
            f"as_of must be timezone-aware (got a naive datetime: {as_of!r}) — "
            "blueprint §3.2, and the same rule store.py enforces on write."
        )

    raw = read_player_gameweek_stats(store, as_of=as_of)
    if raw.is_empty():
        raise TeamStrengthError(
            f"no player gameweek stats kicked off strictly before as_of={as_of.isoformat()} "
            "(either the capability has no data in the store at all, or nothing has a kickoff_time "
            "before this cutoff) — nothing to train on at this deadline"
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
        raise TeamStrengthError(f"{DATASET!r} is missing required column(s) {missing}")

    if seasons is not None:
        seasons = tuple(seasons)
        raw = raw.filter(pl.col("season").is_in(list(seasons)))
        if raw.is_empty():
            raise TeamStrengthError(
                f"no player gameweek stats for seasons {seasons!r} at as_of={as_of.isoformat()}. "
                "Note that allow_live_season=False excludes FPL-API-sourced rows BEFORE this "
                "season filter is applied, so a live season requested here resolves to nothing."
            )

    if team_name_canonicaliser is not None:
        # Canonicalise on the small set of DISTINCT (season, team) pairs
        # present, not the full row count — a real fit sees ~180k rows but
        # only ~140 distinct (season, team) pairs (7 seasons x 20 clubs).
        pairs = raw.select("season", "team").unique().drop_nulls()
        mapping = pairs.with_columns(
            pl.struct(["season", "team"])
            .map_elements(
                lambda s: team_name_canonicaliser.canonicalise(s["season"], s["team"]),
                return_dtype=pl.Utf8,
            )
            .alias("_canonical_team")
        )
        raw = raw.join(mapping, on=["season", "team"], how="left").with_columns(
            pl.coalesce(["_canonical_team", "team"]).alias("team")
        ).drop("_canonical_team")

    null_team_seasons = (
        raw.group_by("season")
        .agg((pl.col("team").null_count() == pl.len()).alias("team_wholly_null"))
        .filter(pl.col("team_wholly_null"))["season"]
        .to_list()
    )
    if null_team_seasons:
        logger.warning(
            "team_strength.build_match_table: excluding season(s) %s -- 'team' "
            "column is wholly NULL (same gap fplai.backtest.data documents for "
            "2019-20; cannot tell home from away without it)",
            null_team_seasons,
        )
        raw = raw.filter(~pl.col("season").is_in(null_team_seasons))

    if raw.is_empty():
        raise TeamStrengthError("no usable rows remain after excluding null-team seasons")

    # `store.effective_at()` already restricted `raw` to rows with
    # kickoff_time strictly before `as_of` (and collapsed duplicate rows per
    # entity key — see module docstring, "A real, live-verified side
    # effect"). This parses the same column into a Polars Datetime purely
    # for aggregation (min/grouping below); it is not a second filter.
    raw = raw.with_columns(
        pl.col("kickoff_time").str.strptime(pl.Datetime, "%Y-%m-%dT%H:%M:%SZ").alias("_kickoff")
    )

    has_xg = "expected_goals" in raw.columns

    agg_exprs = [
        pl.col("_kickoff").min().alias("kickoff"),
        pl.col("team").filter(pl.col("was_home")).first().alias("home_team"),
        pl.col("team").filter(~pl.col("was_home")).first().alias("away_team"),
        pl.col("team_h_score").first().alias("home_goals"),
        pl.col("team_a_score").first().alias("away_goals"),
    ]
    if has_xg:
        agg_exprs += [
            pl.col("expected_goals").filter(pl.col("was_home")).null_count().alias("_home_xg_nulls"),
            pl.col("expected_goals").filter(pl.col("was_home")).len().alias("_home_xg_n"),
            pl.col("expected_goals").filter(pl.col("was_home")).sum().alias("_home_xg_sum"),
            pl.col("expected_goals").filter(~pl.col("was_home")).null_count().alias("_away_xg_nulls"),
            pl.col("expected_goals").filter(~pl.col("was_home")).len().alias("_away_xg_n"),
            pl.col("expected_goals").filter(~pl.col("was_home")).sum().alias("_away_xg_sum"),
        ]

    grouped = raw.group_by(["season", "fixture"]).agg(agg_exprs)

    incomplete = grouped.filter(pl.col("home_team").is_null() | pl.col("away_team").is_null())
    if incomplete.height:
        logger.warning(
            "team_strength.build_match_table: excluding %d fixture(s) missing a "
            "resolvable home or away side entirely",
            incomplete.height,
        )
        grouped = grouped.filter(pl.col("home_team").is_not_null() & pl.col("away_team").is_not_null())

    if grouped.is_empty():
        raise TeamStrengthError("no fixtures remain after excluding incomplete rows")

    records: list[MatchRecord] = []
    for row in grouped.sort(["season", "kickoff"]).to_dicts():
        home_xg: float | None = None
        away_xg: float | None = None
        if (
            has_xg
            and row["_home_xg_nulls"] == 0
            and row["_home_xg_n"] > 0
            and row["_away_xg_nulls"] == 0
            and row["_away_xg_n"] > 0
        ):
            home_xg = float(row["_home_xg_sum"])
            away_xg = float(row["_away_xg_sum"])
        records.append(
            MatchRecord(
                season=row["season"],
                fixture=row["fixture"],
                kickoff=row["kickoff"],
                home_team=row["home_team"],
                away_team=row["away_team"],
                home_goals=int(row["home_goals"]),
                away_goals=int(row["away_goals"]),
                home_xg=home_xg,
                away_xg=away_xg,
            )
        )
    return tuple(records)


def _dc_tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """The Dixon-Coles low-score dependence correction (Dixon & Coles,
    1997). `1.0` (no correction) for every scoreline outside the four cells
    where the independent-Poisson assumption is known to misfit."""
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def _poisson_pmf(k: int, lam: float) -> float:
    lam = max(lam, 1e-12)
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def _fit_attack_defence_home(
    matches: Sequence[MatchRecord],
    teams: Sequence[str],
    weights: Sequence[float],
    targets_home: Sequence[float],
    targets_away: Sequence[float],
    config: TeamStrengthConfig,
    no_history_teams: frozenset[str] = frozenset(),
) -> tuple[dict[str, float], dict[str, float], float]:
    """Stage 1: decayed, source-weighted quasi-Poisson MLE for every team's
    attack/defence parameter (log scale) and one global home-advantage
    term, by plain Adam gradient ascent in pure Python — no numpy/scipy in
    this project's dependency set (checked before writing this function;
    neither is installed, see this module's live-verification record in
    `docs/wiki/model-team-strength.md`), so every vector here is a plain
    list/dict, not an array.

    Parametrisation: `lambda = exp(a[home] + d[away] + h)`,
    `mu = exp(a[away] + d[home])` — `a` is attack, `d` is defence
    (higher = leakier, i.e. WORSE defence, on this log scale), `h` is
    home advantage. Objective (maximised): decayed-weighted quasi-Poisson
    log-likelihood (the `log(x!)` term is dropped — it does not depend on
    any parameter, so it never affects the argmax) minus an L2 penalty on
    every `a`/`d` (see `TeamStrengthConfig.attack_defence_l2`'s docstring
    for why this single term also resolves the model's gauge freedom).

    `no_history_teams` — session s003, the promoted-team-prior estimate
    (module docstring, `TeamStrengthConfig.promoted_team_attack_prior`/
    `_defence_prior`). Every team in this set is EXCLUDED from `matches`
    by construction (a team with real matches in the training window is
    never in this set — see `fit_team_strength`'s computation of it, done
    BEFORE this function is called, from the exact same `matches`). Its
    `a`/`d` are therefore INITIALISED at the prior (not `0.0`) and
    regularised toward the prior (not `0.0`) instead of every other team's
    `0.0` centre. This is provably isolated from every other team's fitted
    value, not merely "probably fine in practice": a team in
    `no_history_teams` contributes ZERO gradient from the likelihood term
    (it appears in neither `home_idx` nor `away_idx`, by the very
    definition of "no history"), so its own parameter sits EXACTLY at the
    prior for every iteration (regularisation gradient
    `-2*reg*(prior-prior) == 0` at initialisation, and Adam's momentum
    terms start and stay at `0`) — and, symmetrically, it can never appear
    as an opponent in any OTHER team's likelihood gradient either, so no
    other team's fitted value moves by so much as a rounding error from
    this change. `tests/test_team_strength.py::
    test_adding_a_no_history_team_does_not_perturb_any_other_teams_fit`
    proves this directly, not just by this argument.
    """
    team_index = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    prior_a = config.promoted_team_attack_prior
    prior_d = config.promoted_team_defence_prior
    no_history_idx = frozenset(team_index[t] for t in no_history_teams if t in team_index)
    a = [prior_a if i in no_history_idx else 0.0 for i in range(n)]
    d = [prior_d if i in no_history_idx else 0.0 for i in range(n)]
    h = 0.0
    m_a, v_a = [0.0] * n, [0.0] * n
    m_d, v_d = [0.0] * n, [0.0] * n
    m_h = v_h = 0.0

    beta1, beta2, eps, lr = config.adam_beta1, config.adam_beta2, config.adam_eps, config.adam_lr
    reg = config.attack_defence_l2

    home_idx = [team_index[m.home_team] for m in matches]
    away_idx = [team_index[m.away_team] for m in matches]

    for it in range(1, config.n_adam_iterations + 1):
        grad_a = [-2.0 * reg * (a[i] - prior_a if i in no_history_idx else a[i]) for i in range(n)]
        grad_d = [-2.0 * reg * (d[i] - prior_d if i in no_history_idx else d[i]) for i in range(n)]
        grad_h = 0.0

        for k in range(len(matches)):
            i, j, w = home_idx[k], away_idx[k], weights[k]
            lam = math.exp(a[i] + d[j] + h)
            mu = math.exp(a[j] + d[i])
            eh = w * (targets_home[k] - lam)
            ea = w * (targets_away[k] - mu)
            grad_a[i] += eh
            grad_a[j] += ea
            grad_d[j] += eh
            grad_d[i] += ea
            grad_h += eh

        bias1 = 1.0 - beta1**it
        bias2 = 1.0 - beta2**it
        for i in range(n):
            m_a[i] = beta1 * m_a[i] + (1 - beta1) * grad_a[i]
            v_a[i] = beta2 * v_a[i] + (1 - beta2) * grad_a[i] ** 2
            a[i] += lr * (m_a[i] / bias1) / (math.sqrt(v_a[i] / bias2) + eps)

            m_d[i] = beta1 * m_d[i] + (1 - beta1) * grad_d[i]
            v_d[i] = beta2 * v_d[i] + (1 - beta2) * grad_d[i] ** 2
            d[i] += lr * (m_d[i] / bias1) / (math.sqrt(v_d[i] / bias2) + eps)

        m_h = beta1 * m_h + (1 - beta1) * grad_h
        v_h = beta2 * v_h + (1 - beta2) * grad_h**2
        h += lr * (m_h / bias1) / (math.sqrt(v_h / bias2) + eps)

    attack = {t: a[team_index[t]] for t in teams}
    defence = {t: d[team_index[t]] for t in teams}
    return attack, defence, h


def _fit_rho(
    matches: Sequence[MatchRecord],
    weights: Sequence[float],
    attack: dict[str, float],
    defence: dict[str, float],
    home_advantage: float,
    config: TeamStrengthConfig,
) -> float:
    """Stage 2 (see module docstring): `rho` fit against ACTUAL full-time
    goals only, holding stage 1's attack/defence/home-advantage fixed.
    Deterministic bounded ternary search — `rho`'s log-likelihood
    contribution is the sum of `log(tau(...))` over only the four low-score
    cells (every other match contributes `log(1) = 0`, independent of
    `rho`), which is concave over the region where every included match's
    `tau` stays positive; a fixed-iteration ternary search needs no
    gradient and is exact enough for a single scalar."""
    lo, hi = config.rho_bounds

    low_score_cells: list[tuple[float, float, int, int, float]] = []
    for k, m in enumerate(matches):
        x, y = m.home_goals, m.away_goals
        if (x, y) not in {(0, 0), (0, 1), (1, 0), (1, 1)}:
            continue
        lam = math.exp(attack[m.home_team] + defence[m.away_team] + home_advantage)
        mu = math.exp(attack[m.away_team] + defence[m.home_team])
        low_score_cells.append((lam, mu, x, y, weights[k]))

    if not low_score_cells:
        return 0.0

    def neg_log_lik(rho: float) -> float:
        total = 0.0
        for lam, mu, x, y, w in low_score_cells:
            tau = _dc_tau(x, y, lam, mu, rho)
            if tau <= 0.0:
                return math.inf
            total += w * math.log(tau)
        return -total

    for _ in range(config.rho_search_iterations):
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        if neg_log_lik(m1) < neg_log_lik(m2):
            hi = m2
        else:
            lo = m1
    return (lo + hi) / 2.0


@dataclass(frozen=True)
class TeamStrengthParams:
    """A fitted Dixon-Coles team-strength model. Every field needed to
    reproduce or audit the fit — attack/defence are on the LOG scale (see
    `_fit_attack_defence_home`'s docstring for the parametrisation)."""

    teams: tuple[str, ...]
    attack: dict[str, float]
    defence: dict[str, float]
    home_advantage: float
    rho: float
    config: TeamStrengthConfig
    as_of: datetime
    n_matches_used: int
    seasons_used: tuple[str, ...]
    teams_with_no_history: tuple[str, ...]
    in_sample_residual_mean: float
    in_sample_residual_std: float


def fit_team_strength(
    store: BitemporalStore,
    *,
    as_of: datetime,
    seasons: Sequence[str] | None = None,
    teams: Sequence[str] | None = None,
    config: TeamStrengthConfig = TeamStrengthConfig(),
    team_name_canonicaliser: TeamNameCanonicalisationMap | None = None,
) -> TeamStrengthParams:
    """Fit a Dixon-Coles team-strength model from `store`, using only
    fixtures that had kicked off strictly before `as_of` (see module
    docstring, "Bitemporal fitting"). `teams` is the target universe to
    produce ratings for (e.g. the current season's 20 clubs); any team in
    it absent from the training window gets the promoted-team prior (see
    module docstring, and `TeamStrengthConfig.promoted_team_attack_prior`/
    `promoted_team_defence_prior`). Defaults to exactly the teams observed
    in the training window if not given.

    `team_name_canonicaliser` is passed straight through to
    `build_match_table` — see its docstring (session s003's Ipswich/
    Ipswich Town fix). Passing one is how a caller gets a promoted-but-
    recently-relegated club's real history pooled under the SAME name it
    passes via `teams=`, instead of that history silently going to a
    differently-spelled dict key while `teams=` gets the promoted-team
    prior for what is, in football terms, a club with real recent form.
    """
    matches = build_match_table(
        store, as_of=as_of, seasons=seasons, team_name_canonicaliser=team_name_canonicaliser
    )

    as_of_naive = as_of.astimezone(timezone.utc).replace(tzinfo=None)
    weights: list[float] = []
    targets_home: list[float] = []
    targets_away: list[float] = []
    for m in matches:
        days_before = (as_of_naive - m.kickoff).total_seconds() / 86400.0
        decay = math.exp(-math.log(2.0) * days_before / config.half_life_days)
        source_weight = 1.0 if m.is_xg_sourced else config.goals_source_weight
        weights.append(decay * source_weight)
        targets_home.append(m.home_xg if m.is_xg_sourced else float(m.home_goals))
        targets_away.append(m.away_xg if m.is_xg_sourced else float(m.away_goals))

    total_weight = sum(weights)
    if total_weight <= 1e-9:
        raise TeamStrengthError(
            f"total decay-weighted training signal is ~0 (sum={total_weight!r}) over "
            f"{len(matches)} matches -- as_of={as_of.isoformat()} is almost certainly "
            "too far from every match's kickoff relative to "
            f"config.half_life_days={config.half_life_days} for exp(-ln2*days/half_life) "
            "to be distinguishable from float underflow. A fit that proceeded anyway "
            "would silently report a no-op result (every team's attack/defence stuck at "
            "its 0.0 initial value, indistinguishable from a real league-average team) -- "
            "raising here instead of returning that quietly. Verified live: this exact "
            "failure mode was caught by a test-helper bug that set as_of ~8000 years past "
            "every match's kickoff before this guard existed."
        )

    seen_teams = {m.home_team for m in matches} | {m.away_team for m in matches}
    team_universe = sorted(set(teams) | seen_teams) if teams is not None else sorted(seen_teams)
    # Computed BEFORE fitting (not just after, as before this session) so
    # `_fit_attack_defence_home` can apply the promoted-team prior AS the
    # regularisation target for exactly these teams — see that function's
    # docstring for why this is provably isolated from every other team's
    # fitted value.
    no_history_teams = frozenset(t for t in team_universe if t not in seen_teams)

    attack, defence, home_advantage = _fit_attack_defence_home(
        matches, team_universe, weights, targets_home, targets_away, config,
        no_history_teams=no_history_teams,
    )
    rho = _fit_rho(matches, weights, attack, defence, home_advantage, config)

    residuals: list[float] = []
    for m in matches:
        lam = math.exp(attack[m.home_team] + defence[m.away_team] + home_advantage)
        mu = math.exp(attack[m.away_team] + defence[m.home_team])
        residuals.append(m.home_goals - lam)
        residuals.append(m.away_goals - mu)
    n_res = len(residuals)
    mean_res = sum(residuals) / n_res
    var_res = sum((r - mean_res) ** 2 for r in residuals) / n_res if n_res > 1 else 0.0

    # Recomputed here from `team_universe`/`no_history_teams` (both fixed
    # above, before fitting) rather than a second `{m.home_team ...}` pass
    # — single source of truth for "which teams got the promoted prior",
    # matching exactly what `_fit_attack_defence_home` was actually told.
    teams_with_no_history = tuple(t for t in team_universe if t in no_history_teams)

    return TeamStrengthParams(
        teams=tuple(team_universe),
        attack=attack,
        defence=defence,
        home_advantage=home_advantage,
        rho=rho,
        config=config,
        as_of=as_of,
        n_matches_used=len(matches),
        seasons_used=tuple(sorted({m.season for m in matches})),
        teams_with_no_history=teams_with_no_history,
        in_sample_residual_mean=mean_res,
        in_sample_residual_std=math.sqrt(var_res),
    )


@dataclass(frozen=True)
class ScorelinePMF:
    """A full joint probability mass function over `(home_goals,
    away_goals)`, `0..max_goals` each side — the model's actual output
    (CLAUDE.md rule 5). Never collapse this to an expected-goals scalar at
    a module boundary; every consumer (RPS calibration, correlated Monte
    Carlo in Phase 5, captaincy) needs the shape, not the centre.

    Truncated at construction to `len(grid) - 1` goals per side and
    RENORMALISED so `sum(prob(h, a) for h, a in ...) == 1.0` — `mass_before_
    truncation` records how much of the true (untruncated) Poisson mass
    the truncated grid captured before that rescale, so a caller can see
    how much was discarded (negligible at `max_goals=10` for realistic
    Premier League rates, but never silently assumed to be zero)."""

    home_team: str
    away_team: str
    grid: tuple[tuple[float, ...], ...]  # grid[h][a], already renormalised
    mass_before_truncation: float

    @property
    def max_goals(self) -> int:
        return len(self.grid) - 1

    def prob(self, home_goals: int, away_goals: int) -> float:
        if not (0 <= home_goals <= self.max_goals and 0 <= away_goals <= self.max_goals):
            return 0.0
        return self.grid[home_goals][away_goals]

    @property
    def home_win(self) -> float:
        return sum(self.grid[h][a] for h in range(self.max_goals + 1) for a in range(self.max_goals + 1) if h > a)

    @property
    def draw(self) -> float:
        return sum(self.grid[h][h] for h in range(self.max_goals + 1))

    @property
    def away_win(self) -> float:
        return sum(self.grid[h][a] for h in range(self.max_goals + 1) for a in range(self.max_goals + 1) if h < a)

    def home_goals_marginal(self) -> tuple[float, ...]:
        return tuple(sum(row) for row in self.grid)

    def away_goals_marginal(self) -> tuple[float, ...]:
        return tuple(sum(row[a] for row in self.grid) for a in range(self.max_goals + 1))

    def expected_home_goals(self) -> float:
        return sum(h * p for h, p in enumerate(self.home_goals_marginal()))

    def expected_away_goals(self) -> float:
        return sum(a * p for a, p in enumerate(self.away_goals_marginal()))

    def most_likely_scoreline(self) -> tuple[int, int]:
        best = (0, 0)
        best_p = -1.0
        for h in range(self.max_goals + 1):
            for a in range(self.max_goals + 1):
                if self.grid[h][a] > best_p:
                    best_p = self.grid[h][a]
                    best = (h, a)
        return best

    def to_polars(self) -> pl.DataFrame:
        """Long format `(home_team, away_team, home_goals, away_goals,
        probability)` — the raw per-fixture material a future calibration
        report (blueprint §7.1, not built by this module) would score
        against actual results."""
        rows = [
            {
                "home_team": self.home_team,
                "away_team": self.away_team,
                "home_goals": h,
                "away_goals": a,
                "probability": self.grid[h][a],
            }
            for h in range(self.max_goals + 1)
            for a in range(self.max_goals + 1)
        ]
        return pl.DataFrame(rows)


def predict_scoreline(
    params: TeamStrengthParams,
    home_team: str,
    away_team: str,
    *,
    max_goals: int | None = None,
) -> ScorelinePMF:
    """The model's actual output for one fixture: the full joint scoreline
    PMF. Raises `TeamStrengthError` for a team not in `params.teams` —
    never silently substitutes a default rating (a caller wanting the
    promoted-team prior applied should pass that team to `fit_team_strength`
    via `teams=`, so it appears in `params.teams` with an explicit,
    inspectable `attack`/`defence` of `0.0` and shows up in
    `teams_with_no_history` — an unresolved name here is a caller bug, not
    a promoted team)."""
    if home_team not in params.attack or away_team not in params.attack:
        unknown = [t for t in (home_team, away_team) if t not in params.attack]
        raise TeamStrengthError(
            f"unknown team(s) {unknown} -- not in this fit's team universe "
            f"({sorted(params.teams)}). Pass `teams=` to fit_team_strength "
            "to include a team with no match history yet (it will receive "
            "the promoted-team prior, not be silently dropped)."
        )

    mg = max_goals if max_goals is not None else params.config.max_goals
    lam = math.exp(params.attack[home_team] + params.defence[away_team] + params.home_advantage)
    mu = math.exp(params.attack[away_team] + params.defence[home_team])

    home_pmf = [_poisson_pmf(h, lam) for h in range(mg + 1)]
    away_pmf = [_poisson_pmf(a, mu) for a in range(mg + 1)]

    grid: list[list[float]] = []
    total = 0.0
    for h in range(mg + 1):
        row: list[float] = []
        for a in range(mg + 1):
            p = max(0.0, home_pmf[h] * away_pmf[a] * _dc_tau(h, a, lam, mu, params.rho))
            row.append(p)
            total += p
        grid.append(row)

    if total <= 0.0:
        raise TeamStrengthError(
            f"predicted scoreline PMF for {home_team} v {away_team} has zero "
            "mass within max_goals={mg} -- this indicates a parameter-fit "
            "problem (lambda/mu blew up), not a truncation edge case."
        )

    normalised = tuple(tuple(p / total for p in row) for row in grid)
    return ScorelinePMF(
        home_team=home_team,
        away_team=away_team,
        grid=normalised,
        mass_before_truncation=total,
    )


def shrink_to_odds(pmf: ScorelinePMF, *, market_home_win: float, market_draw: float, market_away_win: float, weight: float) -> ScorelinePMF:
    """**Not implemented — the documented seam for live-time odds
    shrinkage.** Blueprint §4 lists team strength as "shrunk to odds";
    §3.3 is explicit that odds have no historical endpoint on the free
    plan and therefore cannot enter a backtest, so this model is built and
    validated standalone, exactly as this task's brief requires. This
    function is where a live-time blend would attach: given a fitted
    `ScorelinePMF` and the market-implied 1X2 probabilities (from
    `providers/odds.py`'s `match.odds@fixture`, de-vigged — not built by
    this module), shrink the model's PMF toward the market by `weight`
    (e.g. an iterative proportional fit / Dixon-Coles-style rescaling that
    preserves the model's own scoreline SHAPE while nudging its 1X2
    marginals toward the market's). Left unimplemented, not stubbed to
    silently no-op, so a caller cannot mistake "not built" for "built and
    doing nothing": raises immediately.
    """
    raise NotImplementedError(
        "shrink_to_odds is a documented seam, not built (blueprint §3.3: odds "
        "are a live-only overlay that cannot enter a backtest, so this model "
        "stands alone and this function is deliberately out of scope for the "
        "Phase 2 team-strength task). Wire it in at live-prediction time only, "
        "against providers/odds.py's match.odds@fixture, never in a backtest path."
    )


# -- derived-capability registration (blueprint §12.2, E2b story 9) --------
#
# Runs UNCONDITIONALLY at THIS MODULE's import time — Architect ruling,
# 2026-08-22, closing the tension `docs/wiki/model-team-strength.md` §10
# originally flagged (see `fplai.schemas`'s "Phase 2, E5" section for the
# full resolution). The earlier lazy-registration workaround (register only
# on the first real `write_team_strength` call; register/unregister around
# every test) made `CANONICAL_SCHEMAS`'s contents depend on call history,
# which conflicts with CLAUDE.md rule 7. Import-time registration is the
# deterministic alternative: the registered set is a pure function of which
# modules were imported.
#
# `_register_team_strength_capability` is defensively idempotent (checks
# `CANONICAL_SCHEMAS` first) even though Python's own module-import cache
# already makes a second `import fplai.models.team_strength` a no-op at the
# top-level-code layer — cheap insurance against an unusual re-entry (e.g.
# a test harness that reloads the module), not load-bearing for the normal
# case.
def _register_team_strength_capability() -> FactTableSchema:
    existing = CANONICAL_SCHEMAS.get(TEAM_STRENGTH_RATING_GAMEWEEK)
    if existing is not None:
        return existing
    return register_derived_capability(
        TEAM_STRENGTH_RATING_GAMEWEEK,
        entity_key=("season", "gameweek", "team"),
        value_fields=(
            "attack",
            "defence",
            "home_advantage",
            "rho",
            "decay_half_life_days",
            "goals_source_weight",
            "n_matches_used",
            "is_promoted_prior",
        ),
        dataset=TEAM_STRENGTH_RATING_DATASET,
        description=(
            "Dixon-Coles team attack/defence ratings (log scale), one row "
            "per team per (season, gameweek) fit -- blueprint §4 (E5). "
            "`home_advantage`/`rho`/`decay_half_life_days`/"
            "`goals_source_weight`/`n_matches_used` are the same value on "
            "every row of one fit (global parameters, denormalised for a "
            "simple long schema -- same pattern team.match_stats@match "
            "already uses). `is_promoted_prior` marks a team that had zero "
            "match history in this fit's training window (attack=defence=0.0, "
            "the league-average prior -- see fplai.models.team_strength's "
            "module docstring)."
        ),
    )


TEAM_STRENGTH_SCHEMA: FactTableSchema = _register_team_strength_capability()


def params_to_rows(params: TeamStrengthParams, *, season: str, gameweek: int) -> pl.DataFrame:
    """Flatten a fitted `TeamStrengthParams` into the derived dataset's row
    shape. `season`/`gameweek` label WHICH gameweek these ratings are
    intended to inform (the caller's own bookkeeping — this fit's actual
    leakage boundary is `params.as_of`, not this label; a caller fitting
    for a backtest replay's gameweek `t` would pass that `t` here)."""
    teams = list(params.teams)
    no_history = set(params.teams_with_no_history)
    return pl.DataFrame(
        {
            "season": [season] * len(teams),
            "gameweek": [gameweek] * len(teams),
            "team": teams,
            "attack": [params.attack[t] for t in teams],
            "defence": [params.defence[t] for t in teams],
            "home_advantage": [params.home_advantage] * len(teams),
            "rho": [params.rho] * len(teams),
            "decay_half_life_days": [params.config.half_life_days] * len(teams),
            "goals_source_weight": [params.config.goals_source_weight] * len(teams),
            "n_matches_used": [params.n_matches_used] * len(teams),
            "is_promoted_prior": [t in no_history for t in teams],
        }
    )


def write_team_strength(
    store: BitemporalStore,
    params: TeamStrengthParams,
    *,
    season: str,
    gameweek: int,
    source: str = "fplai.models.team_strength",
    skip_if_unchanged: bool = True,
) -> WriteResult:
    """The only sanctioned way to persist a fitted model — routes through
    `fplai.derived.write_derived` (never a bare `store.write()`), which is
    itself the only sanctioned way to write a derived batch. The capability
    is already registered by the time this can be called — at THIS
    module's own import time (see `_register_team_strength_capability`
    above) — so no per-call registration step is needed here.

    `calibration_reference` is honest about what this module actually
    computed: an IN-SAMPLE residual over the matches used in the fit, not
    an out-of-sample calibration — that is blueprint §7.1's Phase 2 gate
    (Brier/log-loss/RPS over held-out fixtures), a later slice, not this
    one (per this task's brief: "the calibration report itself... is not
    yours").
    """
    rows = params_to_rows(params, season=season, gameweek=gameweek)

    derived_from = [
        DerivationInput(
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            entity_key={"season": s},
            note=(
                "team-level goals (team_h_score/team_a_score) and, where "
                "available, summed player expected_goals, aggregated per "
                "fixture -- every row of this season's data, not an "
                "individually-named subset (whole-season aggregate input)."
            ),
        )
        for s in params.seasons_used
    ]
    calibration = CalibrationReference(
        reference=(
            f"IN-SAMPLE decayed-weighted residual (actual goals minus fitted "
            f"lambda/mu) over {params.n_matches_used} matches used in this fit "
            f"(seasons {', '.join(params.seasons_used)}, as_of={params.as_of.isoformat()}). "
            "NOT an out-of-sample/held-out calibration -- that is blueprint "
            "§7.1's Phase 2 gate (Brier/log-loss/RPS), a separate, later slice."
        ),
        residual_mean=params.in_sample_residual_mean,
        residual_std=params.in_sample_residual_std,
    )

    return write_derived(
        store,
        TEAM_STRENGTH_RATING_GAMEWEEK,
        rows,
        valid_at=params.as_of,
        observed_at=datetime.now(timezone.utc),
        source=source,
        derived_from=derived_from,
        calibration=calibration,
        skip_if_unchanged=skip_if_unchanged,
    )
