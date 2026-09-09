"""Backfill orchestrator — E2b story 11 (blueprint §12, §12.5, §3.6, §7).

Given a provider, a set of capabilities, and a season range, this module
enumerates the concrete work, executes it politely through the provider's
own transport/policy, checkpoints as it goes, and can resume after
interruption. It is the last piece of the provider epic — everything before
this (schemas, registry, transport, the FPL/PL/vaastav adapters, identity)
existed to be driven live for ONE call at a time; this is what drives them
across a season range and actually puts historical data in the store.

**Read this module's docstring before the code below** — the two design
decisions that matter are both here.

## 1. Work enumeration is two-tier, not one flat list

Some capabilities are fully enumerable with ZERO live calls — every unit is
knowable from `(season, matchweek/gameweek)` alone:

  - `match.fixtures@matchweek` (PL API) — one unit per (season, matchweek).
  - `player.gameweek_stats@gameweek` (vaastav) — one unit per (season, gw).
  - `player.identity@season` (vaastav) — one unit per season.
  - `team.identity@season` (vaastav) — one unit per season. Added E2b story
    10b as a bundled fix: this capability existed in `schemas.py` and
    `providers/vaastav.py` since story 7b but had NO `GrainPlan` at all —
    `BackfillSpec(capabilities=(TEAM_IDENTITY_SEASON,), ...)` raised
    `BackfillError` unconditionally (logged as an open bug, `PROGRESS.md`
    E2 / `docs/HANDOFF.md` §3). `_build_team_identity_units` mirrors
    `_build_player_identity_units` exactly — same shape, same one-unit-per-
    season enumeration, same reason (a season-scoped snapshot has no
    gameweek/matchweek parameter at all).

These are `GrainPlan(kind="static")` — `build_units()` returns the exact,
final `WorkUnit` list, no estimate involved.

Others need an entity list that only a LIVE fetch of something else can
supply — a match's id is only known once its matchweek's fixtures have been
fetched; a player's PL numeric id is only known once identity for that
season has been resolved:

  - `match.lineups@match`, `match.substitutions@match`,
    `team.match_stats@match` — one unit per MATCH, expanded from
    `match.fixtures@matchweek`'s own rows (already fetched and, critically,
    already WRITTEN to the store — see §3 below for why expansion reads the
    store rather than carrying rows in memory).
  - `player.season_stats@season` — one unit per PLAYER per season. Its cost
    model is implemented (`estimate_count`, for `--dry-run`) but its
    EXECUTION is deliberately NOT wired in this story (`GrainPlan.
    executable=False`) — see §4.

These are `GrainPlan(kind="dependent")`. `--dry-run` uses `estimate_count()`
— a documented, overridable ASSUMPTION (`BackfillSpec.matches_per_matchweek`,
`.players_per_season_estimate`) — because giving a real number before
spending a single request is the entire point of `--dry-run` (story brief:
"we need to see the cost before spending it"). A real `run()` never uses the
estimate for anything except the initial progress-bar denominator; the
actual unit list is expanded from real, already-fetched rows once they
exist (`_expand_all_dependent_units`).

## 2. Identity failure vs upstream absence — §9.3's open question, answered

`docs/wiki/provider-framework.md` §9.3 left this open: a historical
matchweek batch that mixes a current club and a since-relegated one raises
for the WHOLE batch (blueprint §12.5's "unmatched entities raise", working
exactly as specified) — should the ORCHESTRATOR now (a) keep propagating
that, (b) skip the offending fixture with a loud warning, or (c) require a
season-appropriate identity snapshot per season?

**Answer: (c), already delivered by story 10's `player.identity@season` +
`build_player_identity_map_for_season` — and this story's `run()` does
NOT accept a bare `Provider`, it accepts a `provider_factory: Callable[[str],
Provider]` keyed by season for exactly this reason.** A caller MUST supply a
season-appropriate provider (built from that season's own
`player.identity@season` archive rows, per story 10) — the orchestrator has
no season-agnostic fallback and cannot silently reach for "whatever
identity happens to be lying around", which was the actual root cause behind
every `IdentityError` seen in stories 6/7/10's live verification runs.

Given a correctly season-scoped provider, an `IdentityError` should no
longer happen. **If it does anyway, that is OUR bug — the caller supplied
the wrong snapshot for this season, or a provider's own identity resolution
has a real gap.** Blueprint §12.5 explicitly rejects a "partial success /
skip the misses" mode: turning a loud, correct failure into a silently
incomplete batch is the exact inversion of what §12.5 exists to prevent.
So:

  - `IdentityError` → `BackfillOrchestrator.run()` catches it, records
    NOTHING in the checkpoint for that unit (it is not "absent", it is
    unresolved — a future corrected run must retry it), and raises
    `BackfillHalted(reason="identity", ...)`. The run stops. This is a
    fail-fast bug report, not a recoverable per-unit outcome.

  - `ProviderError` (a genuine 404, "no matches for this matchweek", "no
    substitutions in this match", "season predates this data source") → a
    FACT ABOUT THE WORLD, not a bug. Recorded as a first-class `UnitOutcome
    (status="absent", reason=str(exc))`, checkpointed, and the run
    CONTINUES to the next unit. A backfill that dies on a 2016 fixture
    lacking a data source is useless (story brief) — this is the path that
    keeps it alive.

  - `TransportError` (the transport's OWN retries/backoff already
    exhausted — see `transport.HttpTransport.get`) → not a fact about one
    unit at all; a signal the run may be over whatever tolerance the
    provider has (blueprint §3.6 — "do not probe it"). Treated the same way
    as identity: `BackfillHalted(reason="transport", ...)`, run stops
    rather than hammering through a 429/403 storm.

These three paths are written as three separate `except` clauses in
`BackfillOrchestrator.run()`'s inner `_handle()` closure — deliberately not
collapsed into one generic "log and continue" handler, so a future reader
sees the distinction in the code, not just in this docstring.

## 3. Why dependent-unit expansion reads the STORE, not in-memory rows

A resumed run does not re-fetch a matchweek's fixtures it already fetched
(§3.6 — that would be rude and pointless; the store is idempotent, not
re-fetch-worthy). So the rows needed to expand `match.lineups@match` into
real `match_id`s must be readable WITHOUT having just fetched them in this
process. `BitemporalStore.latest("pl_match_fixtures")` — already-written,
free, no network — is exactly that. This is also why dependent-capability
expansion happens in its own phase, strictly AFTER every static unit
(including every `match.fixtures@matchweek` unit) has run: the store must
actually contain this run's fixtures before anything reads them back.

## 4. What is deliberately not wired: `player.season_stats@season` execution

Its cost model exists (`estimate_count`, exercised by `--dry-run` and
tested) so the 10-season dry-run number in this story's report is honest
and complete. Its real per-player expansion (`identity().players.
code_to_element_id.values()` → one `WorkUnit` per player) was judged out of
scope for the time this story had, given the two capabilities actually
verified live are `match.fixtures@matchweek` and `player.gameweek_stats@
gameweek`. `GrainPlan.executable=False` makes `run()` refuse to execute it
outright (`BackfillError`, naming exactly what's missing) rather than
silently no-op it — never a silent gap.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import polars as pl

from fplai.identity import IdentityError
from fplai.providers.base import ProviderError
from fplai.schemas import (
    CHIP_WINDOW_SEASON,
    GAME_CONFIG_CURRENT,
    GAME_SETTINGS_CURRENT,
    GAMEWEEK_ATTRIBUTES_SEASON,
    MANAGER_PICKS_SELECTION_GAMEWEEK,
    MATCH_FIXTURES_MATCHWEEK,
    MATCH_LINEUPS_MATCH,
    MATCH_OFFICIALS_MATCH,
    MATCH_SUBSTITUTIONS_MATCH,
    PLAYER_ATTRIBUTES_CURRENT,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    PLAYER_IDENTITY_SEASON,
    PLAYER_SEASON_STATS_SEASON,
    TEAM_ATTRIBUTES_CURRENT,
    TEAM_IDENTITY_SEASON,
    TEAM_MATCH_STATS_MATCH,
    CapabilityKey,
)
from fplai.store import BitemporalStore
from fplai.transport import (
    BulkFilePolicy,
    CreditPolicy,
    DailyQuotaPolicy,
    RatePolicy,
    TransportError,
    TransportPolicy,
)

logger = logging.getLogger("fplai.backfill")

# Live-only capabilities (blueprint §12: FPLProvider's CoverageSpec(seasons=
# None)) have no historical equivalent at all — there is no "2019 elements"
# endpoint. Backfilling them is a category error, not an unsupported-but-
# plausible request; named here so the error message can say so explicitly
# instead of just "not in GRAIN_PLANS".
_LIVE_ONLY_NO_HISTORY_HINT = (
    "this is a LIVE-ONLY capability (FPLProvider's CoverageSpec has "
    "seasons=None) — the FPL API has no historical-elements endpoint, so "
    "there is nothing to backfill; every unsnapshotted value is genuinely "
    "gone (blueprint §3.4), not merely unfetched."
)
_LIVE_ONLY_CAPABILITIES = frozenset({
    PLAYER_ATTRIBUTES_CURRENT,
    TEAM_ATTRIBUTES_CURRENT,
    GAMEWEEK_ATTRIBUTES_SEASON,
    CHIP_WINDOW_SEASON,
    GAME_CONFIG_CURRENT,
    GAME_SETTINGS_CURRENT,
    MANAGER_PICKS_SELECTION_GAMEWEEK,
})


class BackfillError(ValueError):
    """A misuse of this module: an unenumerable capability, a spec missing
    a prerequisite capability, or a dependent capability requested without
    the store it needs to expand from. Raised at planning time, before any
    request is made — never discovered mid-run."""


class BackfillHalted(RuntimeError):
    """Raised by `BackfillOrchestrator.run()` when the run stops before
    resolving every requested unit. See this module's docstring §2 — the
    ONLY two reasons this is ever raised:

      reason="identity"  — an `IdentityError` propagated from a provider
        fetch. Our bug (blueprint §12.5): the caller's `provider_factory`
        supplied the wrong season's identity snapshot, or a real gap exists
        in a provider's own resolution. Nothing is checkpointed for the
        failing unit — it is unresolved, not "absent"; a corrected re-run
        retries it.

      reason="transport"  — a `TransportError` (the transport's own
        retries/backoff already exhausted). Not a per-unit fact; a signal
        to stop asking rather than hammer through it (§3.6).

    Upstream absence (`ProviderError`) NEVER raises this — see
    `UnitOutcome(status="absent")` instead. Every unit resolved before the
    halt is durably checkpointed; re-running after fixing the cause resumes
    from exactly where this stopped.
    """

    def __init__(self, reason: str, unit: "WorkUnit", n_resolved_this_run: int, phase_total: int, cause: BaseException) -> None:
        if reason not in ("identity", "transport"):
            raise ValueError(f"BackfillHalted.reason must be 'identity' or 'transport', got {reason!r}")
        self.reason = reason
        self.unit = unit
        self.n_resolved_this_run = n_resolved_this_run
        self.phase_total = phase_total
        self.cause = cause
        super().__init__(
            f"backfill halted (reason={reason}) at unit {unit.key!r} "
            f"after resolving {n_resolved_this_run}/{phase_total} units this phase this run: {cause}"
        )


# -- work units --------------------------------------------------------------


@dataclass(frozen=True)
class WorkUnit:
    """One concrete, checkpointable piece of backfill work: one
    `provider.fetch(capability, **params)` call.

    `params` drive the fetch call directly. `meta` is deliberately separate
    and NEVER passed to `fetch()` — it carries orchestration-only context a
    capability's fetch signature doesn't accept (e.g. a match's kickoff
    time, needed to compute `valid_at` for the store write, not needed by
    `PLProvider._fetch_lineups`). `key` is fully deterministic from
    `(provider_id, capability, season, params)` — same inputs always
    produce the same key, on any machine, any run (blueprint §7) — and is
    what `CheckpointStore` uses to recognise "already done"."""

    capability: CapabilityKey
    provider_id: str
    season: str
    params: tuple[tuple[str, Any], ...]
    meta: tuple[tuple[str, Any], ...] = ()
    dataset: str | None = None
    requests: int = 1

    @staticmethod
    def make(
        capability: CapabilityKey,
        provider_id: str,
        unit_season: str,
        *,
        dataset: str | None = None,
        requests: int = 1,
        meta: dict[str, Any] | None = None,
        **fetch_kwargs: Any,
    ) -> "WorkUnit":
        """`unit_season` (not `season`) so callers may freely pass
        `season=...` as one of `**fetch_kwargs` — many capabilities' own
        `fetch()` signatures take a `season` kwarg (e.g. `match.fixtures@
        matchweek`), which is a genuinely different thing from which season
        this WORK UNIT belongs to, even though the value is usually
        identical; keeping the parameter names distinct avoids a keyword
        collision, not just a naming coincidence."""
        return WorkUnit(
            capability=capability,
            provider_id=provider_id,
            season=str(unit_season),
            params=tuple(sorted(fetch_kwargs.items(), key=lambda kv: kv[0])),
            meta=tuple(sorted((meta or {}).items(), key=lambda kv: kv[0])),
            dataset=dataset,
            requests=requests,
        )

    @property
    def fetch_kwargs(self) -> dict[str, Any]:
        return dict(self.params)

    @property
    def meta_dict(self) -> dict[str, Any]:
        return dict(self.meta)

    @property
    def key(self) -> str:
        parts = ",".join(f"{k}={v!r}" for k, v in self.params)
        return f"{self.provider_id}:{self.capability}:season={self.season!r},{parts}"


# -- spec ---------------------------------------------------------------------


@dataclass(frozen=True)
class BackfillSpec:
    """What to backfill. Single-provider by construction (one `provider_id`,
    one policy) — a backfill spanning two providers is two `BackfillSpec`s
    and two orchestrator runs, never one spec silently mixing policies.

    The three `*_per_season`/`*_estimate` fields are DECLARED ASSUMPTIONS,
    not measured facts (CLAUDE.md rule 4 targets scoring/prices/squad
    limits/DC thresholds — a season's real matchweek count is closer to "a
    planning estimate for a tool that hasn't fetched anything yet" than a
    game rule, same category as `providers/pl.py`'s `PL_COMPETITION_ID`).
    They are used ONLY for (a) `--dry-run`'s cost projection and (b) static,
    exactly-enumerable capabilities (`match.fixtures@matchweek`,
    `player.gameweek_stats@gameweek`) where a genuinely absent matchweek
    (e.g. a season with fewer/more gameweeks than assumed) is recorded as a
    normal `ProviderError`→"absent" outcome, never a crash — the assumption
    being wrong by a few gameweeks costs a handful of harmless 404s, not
    corrupted data. Override per call; never hardcoded deeper than this.
    """

    provider_id: str
    capabilities: tuple[CapabilityKey, ...]
    seasons: tuple[str, ...]
    matchweeks_per_season: int = 38
    matches_per_matchweek: int = 10
    gameweeks_per_season: int | None = None
    players_per_season_estimate: int = 700

    def __post_init__(self) -> None:
        if not self.capabilities:
            raise BackfillError("BackfillSpec needs at least one capability")
        if not self.seasons:
            raise BackfillError("BackfillSpec needs at least one season")
        unknown = [c for c in self.capabilities if _resolve_grain_plan(self.provider_id, c) is None]
        if unknown:
            hints = [f"{c}{' — ' + _LIVE_ONLY_NO_HISTORY_HINT if c in _LIVE_ONLY_CAPABILITIES else ''}" for c in unknown]
            raise BackfillError(
                f"no backfill enumeration strategy declared for: {hints}. "
                "Only capabilities with a GrainPlan in fplai.backfill.GRAIN_PLANS can be "
                "backfilled — see this module's docstring §1."
            )

    @property
    def effective_gameweeks_per_season(self) -> int:
        return self.gameweeks_per_season if self.gameweeks_per_season is not None else self.matchweeks_per_season


# -- grain plans: how each capability is enumerated and costed ----------------


def _build_fixture_units(spec: BackfillSpec) -> list[WorkUnit]:
    return [
        WorkUnit.make(
            MATCH_FIXTURES_MATCHWEEK, spec.provider_id, season,
            dataset="pl_match_fixtures", season=season, matchweek=mw,
        )
        for season in spec.seasons
        for mw in range(1, spec.matchweeks_per_season + 1)
    ]


def _build_gameweek_stats_units(spec: BackfillSpec) -> list[WorkUnit]:
    n_gw = spec.effective_gameweeks_per_season
    return [
        WorkUnit.make(
            PLAYER_GAMEWEEK_STATS_GAMEWEEK, spec.provider_id, season,
            dataset="vaastav_player_gameweek_stats", season=season, gameweek=gw,
        )
        for season in spec.seasons
        for gw in range(1, n_gw + 1)
    ]


def _build_player_identity_units(spec: BackfillSpec) -> list[WorkUnit]:
    return [
        WorkUnit.make(
            PLAYER_IDENTITY_SEASON, spec.provider_id, season,
            dataset="vaastav_player_identity", season=season,
        )
        for season in spec.seasons
    ]


def _build_team_identity_units(spec: BackfillSpec) -> list[WorkUnit]:
    # Bundled fix, E2b story 10b — mirrors _build_player_identity_units
    # exactly (see this module's docstring §1). `team.identity@season` had
    # been servable by providers/vaastav.py since story 7b but had no
    # GrainPlan, so it could not be enumerated by the orchestrator at all.
    return [
        WorkUnit.make(
            TEAM_IDENTITY_SEASON, spec.provider_id, season,
            dataset="vaastav_team_identity", season=season,
        )
        for season in spec.seasons
    ]


def _estimate_match_grain_count(spec: BackfillSpec) -> int:
    return len(spec.seasons) * spec.matchweeks_per_season * spec.matches_per_matchweek


def _estimate_player_season_count(spec: BackfillSpec) -> int:
    return len(spec.seasons) * spec.players_per_season_estimate


_MATCH_GRAIN_DATASETS = {
    MATCH_LINEUPS_MATCH: "pl_match_lineups",
    MATCH_OFFICIALS_MATCH: "pl_match_officials",
    MATCH_SUBSTITUTIONS_MATCH: "pl_match_substitutions",
    TEAM_MATCH_STATS_MATCH: "pl_team_match_stats",
}


def _make_match_expand(capability: CapabilityKey) -> Callable[[BackfillSpec, str, pl.DataFrame], list[WorkUnit]]:
    dataset = _MATCH_GRAIN_DATASETS[capability]

    def _expand(spec: BackfillSpec, season: str, fixtures_rows: pl.DataFrame) -> list[WorkUnit]:
        units = []
        for row in fixtures_rows.to_dicts():
            fetch_kwargs: dict[str, Any] = {"match_id": row["match_id"]}
            if capability == TEAM_MATCH_STATS_MATCH:
                fetch_kwargs["home_team_code"] = row.get("home_team_code")
                fetch_kwargs["away_team_code"] = row.get("away_team_code")
            units.append(
                WorkUnit.make(
                    capability, spec.provider_id, season, dataset=dataset,
                    meta={"kickoff": str(row.get("kickoff"))},
                    **fetch_kwargs,
                )
            )
        return units

    return _expand


@dataclass(frozen=True)
class GrainPlan:
    capability: CapabilityKey
    dataset: str
    kind: str  # "static" | "dependent"
    build_units: Callable[[BackfillSpec], list[WorkUnit]] | None = None
    parent_capability: CapabilityKey | None = None
    estimate_count: Callable[[BackfillSpec], int] | None = None
    expand: Callable[[BackfillSpec, str, pl.DataFrame], list[WorkUnit]] | None = None
    executable: bool = True


GRAIN_PLANS: dict[CapabilityKey, GrainPlan] = {
    MATCH_FIXTURES_MATCHWEEK: GrainPlan(
        capability=MATCH_FIXTURES_MATCHWEEK, dataset="pl_match_fixtures", kind="static",
        build_units=_build_fixture_units,
    ),
    PLAYER_GAMEWEEK_STATS_GAMEWEEK: GrainPlan(
        capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK, dataset="vaastav_player_gameweek_stats", kind="static",
        build_units=_build_gameweek_stats_units,
    ),
    PLAYER_IDENTITY_SEASON: GrainPlan(
        capability=PLAYER_IDENTITY_SEASON, dataset="vaastav_player_identity", kind="static",
        build_units=_build_player_identity_units,
    ),
    TEAM_IDENTITY_SEASON: GrainPlan(
        # Bundled fix, E2b story 10b — see module docstring §1.
        capability=TEAM_IDENTITY_SEASON, dataset="vaastav_team_identity", kind="static",
        build_units=_build_team_identity_units,
    ),
    MATCH_LINEUPS_MATCH: GrainPlan(
        capability=MATCH_LINEUPS_MATCH, dataset="pl_match_lineups", kind="dependent",
        parent_capability=MATCH_FIXTURES_MATCHWEEK,
        estimate_count=_estimate_match_grain_count,
        expand=_make_match_expand(MATCH_LINEUPS_MATCH),
    ),
    MATCH_SUBSTITUTIONS_MATCH: GrainPlan(
        capability=MATCH_SUBSTITUTIONS_MATCH, dataset="pl_match_substitutions", kind="dependent",
        parent_capability=MATCH_FIXTURES_MATCHWEEK,
        estimate_count=_estimate_match_grain_count,
        expand=_make_match_expand(MATCH_SUBSTITUTIONS_MATCH),
    ),
    # Session s004. Same match grain and same parent as lineups/subs above.
    # NOTE this capability needs NO player identity — the officials payload
    # carries names only, no numeric player id — which is what makes it
    # ingestible for the 2020-21..2023-24 seasons where vaastav's archive
    # has no `opta_code` at all and PlayerIdentityMap.build() correctly
    # refuses to guess. See scripts/backfill.py's lazy identity note.
    MATCH_OFFICIALS_MATCH: GrainPlan(
        capability=MATCH_OFFICIALS_MATCH, dataset="pl_match_officials", kind="dependent",
        parent_capability=MATCH_FIXTURES_MATCHWEEK,
        estimate_count=_estimate_match_grain_count,
        expand=_make_match_expand(MATCH_OFFICIALS_MATCH),
    ),
    TEAM_MATCH_STATS_MATCH: GrainPlan(
        capability=TEAM_MATCH_STATS_MATCH, dataset="pl_team_match_stats", kind="dependent",
        parent_capability=MATCH_FIXTURES_MATCHWEEK,
        estimate_count=_estimate_match_grain_count,
        expand=_make_match_expand(TEAM_MATCH_STATS_MATCH),
    ),
    PLAYER_SEASON_STATS_SEASON: GrainPlan(
        capability=PLAYER_SEASON_STATS_SEASON, dataset="pl_player_season_stats", kind="dependent",
        parent_capability=None,  # needs a resolved IdentityResolver, not another capability's rows
        estimate_count=_estimate_player_season_count,
        expand=None,
        executable=False,  # see module docstring §4 — cost model only, not wired to run() in this story
    ),
}


def _build_fpl_gameweek_stats_units(spec: BackfillSpec) -> list[WorkUnit]:
    """FPL API's own units for `player.gameweek_stats@gameweek` — session
    s005, second provider of an existing capability (see
    `PROVIDER_GRAIN_PLANS` below). Unlike vaastav's archive, `event/{gw}/
    live/` has NO season selector at all: it always answers for whatever
    season is currently live on the FPL API. A `BackfillSpec` naming more
    than one season here would silently give every requested season the
    SAME current-season payload under a different label — refused
    outright (CLAUDE.md rule 2: this is leakage, not a crash), never
    produced and left for a caller to notice later. `season` is still
    threaded through to `WorkUnit.make`'s `unit_season` (checkpoint
    bookkeeping/provenance only, exactly `providers/fpl.py::
    FPLProvider._fetch_gameweek_stats`'s own docstring already says about
    its own `season` parameter) — never passed as a `fetch()` kwarg,
    since the endpoint itself has no use for it."""
    if len(spec.seasons) != 1:
        raise BackfillError(
            "fpl_api's player.gameweek_stats@gameweek has no season selector -- "
            f"BackfillSpec.seasons must name exactly one season (the CURRENT live season), "
            f"got {spec.seasons!r}. Naming more than one season would silently label the "
            "same current-season payload under every season requested."
        )
    n_gw = spec.effective_gameweeks_per_season
    season = spec.seasons[0]
    return [
        WorkUnit.make(
            PLAYER_GAMEWEEK_STATS_GAMEWEEK, spec.provider_id, season,
            dataset="fpl_api_player_gameweek_stats", gameweek=gw,
        )
        for gw in range(1, n_gw + 1)
    ]


# -- provider-scoped grain plan overrides (session s005) ---------------------
#
# `GRAIN_PLANS` above is keyed by CapabilityKey alone -- one plan per
# capability, which was a sound invariant right up until a SECOND provider
# (fpl_api) started serving an EXISTING capability (player.gameweek_stats@
# gameweek) that vaastav_archive already has an entry for. Overwriting that
# entry in place would silently break every existing BackfillSpec(provider_
# id="vaastav_archive", capabilities=(PLAYER_GAMEWEEK_STATS_GAMEWEEK,)) call
# — including the ones `tests/test_backfill.py` already exercises, a file
# outside this session's owned paths and therefore not something this
# session may break. `PROVIDER_GRAIN_PLANS` is instead a purely ADDITIVE,
# `(provider_id, capability)`-keyed override: `_resolve_grain_plan` checks
# it FIRST and falls back to the untouched `GRAIN_PLANS[capability]` for
# every provider that doesn't have an override — which is every provider
# that predates this session, so their behaviour is bit-for-bit unchanged.
PROVIDER_GRAIN_PLANS: dict[tuple[str, CapabilityKey], GrainPlan] = {
    ("fpl_api", PLAYER_GAMEWEEK_STATS_GAMEWEEK): GrainPlan(
        capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK, dataset="fpl_api_player_gameweek_stats", kind="static",
        build_units=_build_fpl_gameweek_stats_units,
    ),
}


def _resolve_grain_plan(provider_id: str, capability: CapabilityKey) -> GrainPlan | None:
    """The one place a `(provider_id, capability)` pair resolves to a
    `GrainPlan` — every caller in this module goes through this instead of
    indexing `GRAIN_PLANS` directly, so a provider-scoped override
    (`PROVIDER_GRAIN_PLANS`) is never at risk of being bypassed by a stale
    direct lookup left over from before one existed. Returns `None` (never
    raises) for an unknown pair — callers decide whether that's an error."""
    override = PROVIDER_GRAIN_PLANS.get((provider_id, capability))
    if override is not None:
        return override
    return GRAIN_PLANS.get(capability)


def enumerate_static_units(spec: BackfillSpec) -> list[WorkUnit]:
    """Every fully-determined work unit this spec implies — no live call
    made, no estimate involved. Deterministic and re-derivable (blueprint
    §7): the same spec always enumerates the same units in the same order."""
    units: list[WorkUnit] = []
    for capability in spec.capabilities:
        plan = _resolve_grain_plan(spec.provider_id, capability)
        if plan.kind == "static":
            units.extend(plan.build_units(spec))
    return units


def estimate_dependent_counts(spec: BackfillSpec) -> dict[CapabilityKey, int]:
    """Documented ESTIMATES (module docstring §1) for every dependent
    capability in the spec — `--dry-run`'s only source for these, since a
    real count needs a live fetch this function deliberately never makes."""
    return {
        capability: _resolve_grain_plan(spec.provider_id, capability).estimate_count(spec)
        for capability in spec.capabilities
        if _resolve_grain_plan(spec.provider_id, capability).kind == "dependent"
    }


# -- dry run -------------------------------------------------------------------


def _format_duration(seconds: float) -> str:
    if seconds == float("inf") or seconds != seconds:  # NaN guard
        return "unknown"
    seconds = max(0.0, seconds)
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _estimate_wall_clock(n_requests: int, policy: TransportPolicy) -> tuple[float | None, str]:
    """Wall-clock at the PROVIDER'S OWN policy rate — never a number this
    module invents independently of the transport that will actually
    enforce it (module docstring's whole point: drive everything through
    the existing policy). Jitter and backoff/retry time are NOT modelled
    (this is a floor, stated as such), and quota/credit ceilings are
    reported separately from wall-clock since they bound requests-per-
    period, not requests-per-second."""
    if isinstance(policy, RatePolicy):
        seconds = n_requests / policy.requests_per_second
        return seconds, (
            f"{n_requests} requests @ {policy.requests_per_second}/s "
            "(RatePolicy) — floor only; jitter and any 429/403 backoff are not modelled"
        )
    if isinstance(policy, DailyQuotaPolicy):
        days_needed = math.ceil(n_requests / policy.requests_per_day) if policy.requests_per_day else None
        note = f"{n_requests} requests against a {policy.requests_per_day}/day quota"
        if days_needed and days_needed > 1:
            note += f" — needs at least {days_needed} day(s), spread across multiple runs, not one sitting"
        if policy.requests_per_second:
            return n_requests / policy.requests_per_second, note
        return None, note
    if isinstance(policy, CreditPolicy):
        return None, (
            f"{n_requests} calls against a {policy.monthly_credits}/month CREDIT budget — "
            f"cost is call-shape-dependent ({policy.cost_per_call_description or 'undocumented'}), "
            "not 1:1 with request count; this module does not auto-price a call, "
            "so no wall-clock number is given here."
        )
    if isinstance(policy, BulkFilePolicy):
        assumed_seconds_per_file = 1.0
        return n_requests * assumed_seconds_per_file, (
            f"{n_requests} bulk file fetches, NO rate ceiling (BulkFilePolicy) — wall-clock "
            f"here ASSUMES {assumed_seconds_per_file}s/file of network overhead purely to give a "
            "rough total; this is not a policy-derived number, there is no policy to derive it from"
        )
    raise TypeError(f"unknown TransportPolicy: {policy!r}")


@dataclass(frozen=True)
class DryRunLine:
    capability: CapabilityKey
    dataset: str
    kind: str  # "exact" | "estimated"
    n_units: int
    n_requests: int


@dataclass(frozen=True)
class DryRunReport:
    provider_id: str
    seasons: tuple[str, ...]
    lines: tuple[DryRunLine, ...]
    n_requests_total: int
    wall_clock_seconds: float | None
    wall_clock_note: str
    assumptions: tuple[str, ...]

    def render(self) -> str:
        out = [
            f"Backfill dry run — provider={self.provider_id!r}, "
            f"{len(self.seasons)} season(s): {list(self.seasons)}",
            "",
        ]
        header = f"{'capability':<34} {'dataset':<28} {'kind':<10} {'units':>10} {'requests':>10}"
        out.append(header)
        out.append("-" * len(header))
        for line in self.lines:
            out.append(
                f"{str(line.capability):<34} {line.dataset:<28} {line.kind:<10} "
                f"{line.n_units:>10,} {line.n_requests:>10,}"
            )
        out.append("-" * len(header))
        out.append(f"{'TOTAL':<34} {'':<28} {'':<10} {'':>10} {self.n_requests_total:>10,}")
        out.append("")
        if self.wall_clock_seconds is not None:
            out.append(f"Estimated wall-clock at policy rate: {_format_duration(self.wall_clock_seconds)}")
        else:
            out.append("Estimated wall-clock: not applicable at this policy shape (see note)")
        out.append(f"  ({self.wall_clock_note})")
        out.append("")
        out.append("Assumptions (override via BackfillSpec fields — see its docstring):")
        for a in self.assumptions:
            out.append(f"  - {a}")
        return "\n".join(out)


def dry_run(spec: BackfillSpec, policy: TransportPolicy) -> DryRunReport:
    """Report cost WITHOUT fetching anything — story brief's hard
    requirement. Every number here is computed from `spec` and `policy`
    alone; this function makes no I/O of any kind."""
    lines: list[DryRunLine] = []
    total_requests = 0
    for capability in spec.capabilities:
        plan = _resolve_grain_plan(spec.provider_id, capability)
        if plan.kind == "static":
            units = plan.build_units(spec)
            n_units = len(units)
            n_requests = sum(u.requests for u in units)
            kind = "exact"
        else:
            n_units = plan.estimate_count(spec)
            n_requests = n_units
            kind = "estimated"
        total_requests += n_requests
        lines.append(DryRunLine(capability=capability, dataset=plan.dataset, kind=kind, n_units=n_units, n_requests=n_requests))

    wall_clock_seconds, note = _estimate_wall_clock(total_requests, policy)
    assumptions = (
        f"matchweeks_per_season={spec.matchweeks_per_season} (20-club PL: each club plays every other twice)",
        f"matches_per_matchweek={spec.matches_per_matchweek}",
        f"gameweeks_per_season={spec.effective_gameweeks_per_season} (FPL calendar; blank/double GWs make this inexact)",
        f"players_per_season_estimate={spec.players_per_season_estimate} "
        "(vaastav's verified 2025-26 archive had 841 full-season entrants; varies by era/squad churn)",
    )
    return DryRunReport(
        provider_id=spec.provider_id, seasons=spec.seasons, lines=tuple(lines),
        n_requests_total=total_requests, wall_clock_seconds=wall_clock_seconds,
        wall_clock_note=note, assumptions=assumptions,
    )


# -- checkpointing --------------------------------------------------------------


@dataclass(frozen=True)
class UnitOutcome:
    unit_key: str
    capability: str
    season: str
    status: str  # "done" | "absent" — see module docstring §2. Never anything else.
    reason: str | None
    content_hash: str | None
    n_rows: int | None
    observed_at: str  # ISO-8601 UTC


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CheckpointStore:
    """Durable, append-only JSONL — the same convention as `.punchcard/`
    (append-only, never rewritten, trivially greppable/resumable). One line
    per resolved `WorkUnit`. `load_resolved()` folds the file into
    `{unit_key: UnitOutcome}` with LAST WRITE WINS per key (defensive: this
    process never re-records an already-resolved key, but a hand-edited or
    concatenated checkpoint file should still resolve deterministically
    rather than raise or pick arbitrarily)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load_resolved(self) -> dict[str, UnitOutcome]:
        resolved: dict[str, UnitOutcome] = {}
        if not self.path.exists():
            return resolved
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                resolved[row["unit_key"]] = UnitOutcome(**row)
        return resolved

    def record(self, outcome: UnitOutcome) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dataclasses.asdict(outcome)) + "\n")
            f.flush()


# -- valid_at conventions for historical writes ---------------------------------


def _season_start_date(season: str) -> datetime:
    """`"2025-26"` -> 2025-08-01 UTC. A documented CONVENTION (this
    module's docstring on `BackfillSpec` explains the category), not a
    measured fact — a season-scoped player-list snapshot doesn't have one
    true instant it became valid, so this anchors at the earliest
    plausible one, the conservative direction (never claims validity
    earlier than it could have existed)."""
    year_str = season[:4]
    if not year_str.isdigit():
        raise BackfillError(f"cannot derive a season-start date from season={season!r} (expected a 'YYYY-...' string)")
    return datetime(int(year_str), 8, 1, tzinfo=timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_kickoff_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    raise BackfillError(f"cannot parse a kickoff value of type {type(value)!r}: {value!r}")


def _valid_at_for(unit: WorkUnit, rows: pl.DataFrame) -> datetime:
    """`valid_at` for historical data is NEVER `datetime.now()` — that
    would misrepresent when the fact became true in the world (CLAUDE.md
    rule 2 / blueprint §3.2). This function is only about `valid_at`;
    `observed_at` — when WE learned the fact — is a separate axis with its
    own rule, stated here because a prior version of this docstring got it
    wrong and that mistake is exactly how a leak gets reintroduced:
    `observed_at` is `result.observed_at`, supplied by the PROVIDER that
    fetched the row (`BackfillOrchestrator.run()`'s `handle()` persists it
    unchanged) — never `datetime.now()` at write time. A provider's own
    `observed_at` is that provider's provenance to set; the orchestrator
    has no standing to overwrite it (E2b story 10b's coordinator-directed
    correction, 2026-08-21, after olbauday's adapter build surfaced that
    `handle()` was discarding `result.observed_at` and stamping the write
    instant instead — see docs/wiki/provider-framework.md §13 for the
    live-evidence account of why that class of bug matters).

    `store.write()` takes exactly one `valid_at` per batch, so a batch that
    spans several real-world instants (e.g. ten fixtures' worth of kickoffs
    in one matchweek) is anchored at the EARLIEST instant in it — the
    conservative direction; understating how early a fact became true is
    merely conservative, overstating it is the leakage direction (§3.2)."""
    if unit.capability == MATCH_FIXTURES_MATCHWEEK:
        kickoffs = rows["kickoff"].drop_nulls()
        if kickoffs.len() == 0:
            raise BackfillError(f"{unit.key}: fixtures batch has no non-null 'kickoff' to anchor valid_at")
        return _parse_kickoff_value(kickoffs.min())
    # MATCH_OFFICIALS_MATCH shares the per-match kickoff convention below.
    # **Read this before using referee as a forward feature.** Anchoring at
    # kickoff is the leak-SAFE direction (§3.2: understating how early a
    # fact became true is conservative; overstating it is the leakage
    # direction), but it has a real modelling consequence — the referee is
    # then NOT resolvable as of a pre-deadline timestamp, even though in
    # reality the PL publishes appointments days in advance. So a cards
    # model must not read this as "known at the deadline". Making it a
    # genuine forward feature needs an OBSERVED announcement time, i.e. its
    # own capability — not an invented offset from kickoff, which would be
    # fabricating a fact nobody observed.
    if unit.capability in (MATCH_LINEUPS_MATCH, MATCH_SUBSTITUTIONS_MATCH, TEAM_MATCH_STATS_MATCH, MATCH_OFFICIALS_MATCH):
        raw = unit.meta_dict.get("kickoff")
        if not raw or raw == "None":
            raise BackfillError(f"{unit.key}: no 'kickoff' in unit.meta — expand step must supply it from fixtures")
        return _parse_kickoff_value(raw)
    if unit.capability == PLAYER_GAMEWEEK_STATS_GAMEWEEK:
        kickoffs = rows["kickoff_time"].drop_nulls()
        if kickoffs.len() == 0:
            raise BackfillError(f"{unit.key}: gameweek-stats batch has no non-null 'kickoff_time' to anchor valid_at")
        return _parse_kickoff_value(kickoffs.min())
    if unit.capability == PLAYER_IDENTITY_SEASON:
        return _season_start_date(unit.season)
    if unit.capability == TEAM_IDENTITY_SEASON:
        # Bundled fix, E2b story 10b — same convention as PLAYER_IDENTITY_
        # SEASON: a season-scoped team list has no one true valid-from
        # instant either, so anchor at the season start (conservative
        # direction, never claims validity earlier than possible).
        return _season_start_date(unit.season)
    raise BackfillError(f"no valid_at convention declared for {unit.capability} — add one to fplai.backfill._valid_at_for")


# -- orchestrator ----------------------------------------------------------------


@dataclass
class RunSummary:
    """`n_done`/`n_absent` are CUMULATIVE totals across the whole checkpoint
    (units resolved in a prior run + units resolved this call), not "this
    call only" — `RunSummary` describes the state of the backfill after
    this call, matching what a caller checking "are we done yet" actually
    wants. `n_already_resolved_on_entry` is the one field that tells you
    how much of that total was already there before this call started;
    `outcomes` (every `UnitOutcome`, tagged with `observed_at`) is how to
    recover exactly what happened in this call specifically, if needed."""

    n_total: int
    n_done: int
    n_absent: int
    n_already_resolved_on_entry: int
    elapsed_seconds: float
    outcomes: list[UnitOutcome]
    stopped_early: bool = False


@dataclass(frozen=True)
class ProgressState:
    phase: str
    season: str | None
    i: int
    total: int
    n_done: int
    n_absent: int
    elapsed_seconds: float


class BackfillOrchestrator:
    """Enumerates, executes, checkpoints, and can resume a `BackfillSpec`
    against one provider. See this module's docstring for the two design
    decisions (enumeration tiers, identity-vs-absence). Nothing here
    maintains its own rate limiter or cache — every live request goes
    through `provider.fetch()`, which goes through that provider's own
    `HttpTransport`/`FileTransport` and therefore its own declared policy
    (RatePolicy/DailyQuotaPolicy/CreditPolicy/BulkFilePolicy). This module
    NEVER bypasses that.

    `provider_factory(season) -> Provider` — NOT a single provider
    instance. §12.5 requires identity resolved against the snapshot valid
    AT the season being backfilled; a caller must build (or select) a
    season-appropriate provider. Memoised per season (`_get_provider`) so a
    38-matchweek season's units share one provider instance rather than
    re-resolving identity 38 times.
    """

    def __init__(
        self,
        spec: BackfillSpec,
        provider_factory: Callable[[str], Any],
        *,
        checkpoint: CheckpointStore,
        store: BitemporalStore | None = None,
        progress_every: int = 10,
        progress_seconds: float = 30.0,
        on_progress: Callable[[ProgressState], None] | None = None,
    ) -> None:
        self.spec = spec
        self.provider_factory = provider_factory
        self.checkpoint = checkpoint
        self.store = store
        self.progress_every = progress_every
        self.progress_seconds = progress_seconds
        self.on_progress = on_progress or (lambda state: None)
        self._provider_cache: dict[str, Any] = {}

    def _get_provider(self, season: str) -> Any:
        if season not in self._provider_cache:
            self._provider_cache[season] = self.provider_factory(season)
        return self._provider_cache[season]

    def dry_run(self) -> DryRunReport:
        sample_provider = self._get_provider(self.spec.seasons[0])
        return dry_run(self.spec, sample_provider.policy)

    # -- dependent-unit expansion (module docstring §3) -----------------------

    def _expand_all_dependent_units(self) -> list[WorkUnit]:
        dependent_capabilities = [
            c for c in self.spec.capabilities if _resolve_grain_plan(self.spec.provider_id, c).kind == "dependent"
        ]
        if not dependent_capabilities:
            return []
        not_executable = [
            c for c in dependent_capabilities if not _resolve_grain_plan(self.spec.provider_id, c).executable
        ]
        if not_executable:
            raise BackfillError(
                f"{not_executable} have a cost model (usable by --dry-run) but no wired "
                "execution path in this story — see fplai.backfill's module docstring §4. "
                "Remove from BackfillSpec.capabilities to run the rest."
            )
        if self.store is None:
            raise BackfillError(
                f"{dependent_capabilities} expand from already-fetched match.fixtures@matchweek "
                "rows READ BACK FROM THE STORE (module docstring §3) — BackfillOrchestrator needs "
                "a `store` for that. Pass one, or drop these capabilities for a store-free run."
            )
        if MATCH_FIXTURES_MATCHWEEK not in self.spec.capabilities:
            raise BackfillError(
                f"{dependent_capabilities} need match.fixtures@matchweek's rows to resolve real "
                "match ids — add MATCH_FIXTURES_MATCHWEEK to BackfillSpec.capabilities."
            )

        fixtures_all = self.store.latest("pl_match_fixtures")
        units: list[WorkUnit] = []
        for capability in dependent_capabilities:
            plan = _resolve_grain_plan(self.spec.provider_id, capability)
            for season in self.spec.seasons:
                for mw in range(1, self.spec.matchweeks_per_season + 1):
                    if fixtures_all.is_empty():
                        continue
                    rows = fixtures_all.filter(
                        (pl.col("season") == str(season)) & (pl.col("matchweek") == mw)
                    )
                    if rows.is_empty():
                        # No fixtures on file for this matchweek — either
                        # genuinely absent upstream (already recorded as
                        # "absent" when the fixtures unit itself ran) or
                        # this matchweek's fixtures were never requested.
                        # Either way: nothing new to checkpoint, nothing to
                        # expand.
                        continue
                    units.extend(plan.expand(self.spec, season, rows))
        return units

    # -- progress ---------------------------------------------------------------

    def _report_progress(self, phase: str, season: str | None, i: int, total: int, n_done: int, n_absent: int, start: float, last_report: float) -> float:
        now = time.monotonic()
        is_last = i == total
        if not is_last and i % self.progress_every != 0 and (now - last_report) < self.progress_seconds:
            return last_report
        elapsed = now - start
        rate = i / elapsed if elapsed > 0 else 0.0
        eta = (total - i) / rate if rate > 0 else float("inf")
        pct = (100.0 * i / total) if total else 100.0
        logger.info(
            "[%s] %d/%d (%.0f%%) season=%s elapsed=%s eta=%s done=%d absent=%d",
            phase, i, total, pct, season, _format_duration(elapsed), _format_duration(eta), n_done, n_absent,
        )
        self.on_progress(ProgressState(phase=phase, season=season, i=i, total=total, n_done=n_done, n_absent=n_absent, elapsed_seconds=elapsed))
        return now

    # -- run ----------------------------------------------------------------

    def run(self, *, max_units: int | None = None) -> RunSummary:
        """Execute every unit not already resolved, in two phases (static,
        then dependent — module docstring §1/§3), checkpointing each
        resolution as it happens. Raises `BackfillHalted` on the first
        identity or transport failure (module docstring §2); every unit
        resolved before that point is already durably checkpointed, so
        re-running after the cause is fixed resumes rather than restarts.

        `max_units`, if given, is a DELIBERATE, clean early stop after
        processing that many NEWLY-resolved units this call (already-
        resolved units skipped via the resume path do not count) — time-
        boxing a run to fit a maintenance window, or (this story's live
        verification) standing in for an interruption without needing a
        real process kill to prove the resume path actually resumes. This
        is NOT a failure: `RunSummary.stopped_early=True`, no exception.
        """
        resolved = self.checkpoint.load_resolved()
        outcomes: list[UnitOutcome] = list(resolved.values())
        n_done = sum(1 for o in outcomes if o.status == "done")
        n_absent = sum(1 for o in outcomes if o.status == "absent")
        n_already_resolved_on_entry = len(resolved)
        n_processed_this_call = 0
        stopped_early = False

        start = time.monotonic()
        last_report = start

        class _StopEarly(Exception):
            pass

        def handle(unit: WorkUnit, phase: str, i: int, phase_total: int) -> None:
            nonlocal n_done, n_absent, last_report, n_processed_this_call
            if unit.key in resolved:
                return  # already resolved (this run or a prior one) — the resume path.
            if max_units is not None and n_processed_this_call >= max_units:
                raise _StopEarly()

            provider = self._get_provider(unit.season)
            try:
                result = provider.fetch(unit.capability, **unit.fetch_kwargs)

            except IdentityError as exc:
                # OUR BUG (module docstring §2) — nothing checkpointed for
                # this unit; it stays unresolved for a corrected re-run.
                raise BackfillHalted("identity", unit, n_done + n_absent, phase_total, exc) from exc

            except TransportError as exc:
                # Retries/backoff already exhausted inside the transport —
                # a signal to stop asking, not a per-unit fact. Also
                # nothing checkpointed; the unit is retried on resume.
                raise BackfillHalted("transport", unit, n_done + n_absent, phase_total, exc) from exc

            except ProviderError as exc:
                # A FACT ABOUT THE WORLD (module docstring §2) — genuine
                # 404 / no-data-for-this-unit. First-class outcome,
                # checkpointed, run continues.
                outcome = UnitOutcome(
                    unit_key=unit.key, capability=str(unit.capability), season=unit.season,
                    status="absent", reason=str(exc), content_hash=None, n_rows=None, observed_at=_now_iso(),
                )
                self.checkpoint.record(outcome)
                resolved[unit.key] = outcome
                outcomes.append(outcome)
                n_absent += 1
                logger.info("ABSENT  %s — %s", unit.key, exc)

            else:
                if self.store is not None and unit.dataset is not None:
                    valid_at = _valid_at_for(unit, result.rows)
                    # observed_at is the PROVIDER's own provenance — when
                    # IT fetched/learned this fact — never the orchestrator
                    # run's own now(). Persisting datetime.now() here would
                    # silently discard result.observed_at and defeat any
                    # adapter (e.g. providers/olbauday.py) that computes a
                    # real, non-now() observed_at for exactly this reason.
                    # E2b story 10b, coordinator-directed correction,
                    # 2026-08-21 — see docs/wiki/provider-framework.md §13.
                    self.store.write(
                        unit.dataset, result.rows,
                        valid_at=valid_at, observed_at=result.observed_at,
                        source=f"{result.provider_id}:backfill",
                        provider_id=result.provider_id, capability=str(result.capability), endpoint=result.endpoint,
                    )
                outcome = UnitOutcome(
                    unit_key=unit.key, capability=str(unit.capability), season=unit.season,
                    status="done", reason=None, content_hash=result.content_hash,
                    n_rows=result.rows.height, observed_at=_now_iso(),
                )
                self.checkpoint.record(outcome)
                resolved[unit.key] = outcome
                outcomes.append(outcome)
                n_done += 1
                logger.debug("DONE    %s rows=%d", unit.key, result.rows.height)

            n_processed_this_call += 1
            last_report = self._report_progress(phase, unit.season, i, phase_total, n_done, n_absent, start, last_report)

        def run_phase(units: list[WorkUnit], phase: str) -> bool:
            """Returns True if `max_units` was hit partway through — the
            caller must not start the next phase (module docstring §3: a
            partially-run static phase means the store may not yet contain
            every fixture a dependent phase would need to expand from)."""
            for i, unit in enumerate(units, start=1):
                try:
                    handle(unit, phase, i, len(units))
                except _StopEarly:
                    return True
            return False

        static_units = enumerate_static_units(self.spec)
        stopped_early = run_phase(static_units, "static")

        dependent_units: list[WorkUnit] = []
        if not stopped_early:
            dependent_units = self._expand_all_dependent_units()
            stopped_early = run_phase(dependent_units, "dependent")

        if stopped_early:
            logger.info(
                "run() stopped early at max_units=%d (%d done, %d absent this call) — "
                "resume by calling run() again with the same CheckpointStore",
                max_units, n_done, n_absent,
            )

        elapsed = time.monotonic() - start
        return RunSummary(
            n_total=len(static_units) + len(dependent_units),
            n_done=n_done, n_absent=n_absent,
            n_already_resolved_on_entry=n_already_resolved_on_entry,
            stopped_early=stopped_early,
            elapsed_seconds=elapsed, outcomes=outcomes,
        )
