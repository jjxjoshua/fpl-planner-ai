"""Capability-level reader for `player.gameweek_stats@gameweek` — session
s005. Unions vaastav's archive (`vaastav_player_gameweek_stats`, historical
seasons) and the FPL API's own live ingestion (`fpl_api_player_gameweek_
stats`, session s005, current season) into ONE frame, with provenance
preserved so a caller can always tell which provider a row came from.

## Why this module exists

`fplai.schemas.CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK]`'s own
docstring already establishes this is ONE capability now served by TWO
providers (blueprint §12.6 swappability) — but every model in `fplai.
models` today reads a DATASET, not a capability: `DATASET = "vaastav_
player_gameweek_stats"` is hardcoded at module scope in `attacking.py`,
`bonus.py`, `cards.py`, `defensive_contribution.py`, `minutes.py` and
`team_strength.py`, and each module's own `build_training_table` calls
`store.effective_at(DATASET, as_of)` directly. That is the underlying
design flaw this reader exists to fix — a single, capability-level read
that resolves BOTH sources and hands back one frame, so a future caller
never has to know there are two datasets behind one capability at all.

**Migrating the six models onto this reader is explicitly NOT done in
this story** — see "What a migration would still need to do" below. This
module only builds and proves the reader.

## Bitemporal resolution — `effective_at()` on EACH dataset, never `as_of()`

Both source datasets are bulk/batch-ingested (vaastav in one backfill
session per season; the FPL API provider in one fetch per gameweek, well
after that gameweek's own kickoff) — `observed_at`-based resolution
(`as_of()`) is therefore close to meaningless for either: it would return
EMPTY for any real historical deadline (see `PLAYER_GAMEWEEK_STATS_
GAMEWEEK`'s own schema docstring, "Valid time is per ROW, not per
batch"). Both share the SAME declared `valid_time_column` ("kickoff_time",
same string format) because they are the SAME capability satisfying the
SAME `FactTableSchema` — so `store.effective_at(dataset, as_of)` is the
correct, already-tested primitive for both, called once per dataset here
exactly the way every model's own `build_training_table` already calls it
once against vaastav alone.

## Column alignment between the two sources — characterised, not papered over

Both sources satisfy every REQUIRED field (`season`, `round`, `element`,
`fixture`, `name`, `total_points`, `minutes`, `selected`, `value`,
`was_home`, `team_a_score`, `team_h_score`, `kickoff_time`). Beyond that:

- **`selected`/`value` are ALWAYS NULL on FPL-API-sourced rows.** The
  live `event/{gw}/live/` endpoint has no historical price/ownership at
  all — this is literally the reason vaastav's archive is a capability in
  the first place (see the schema's own docstring). Never guessed,
  backfilled, or silently approximated from a different (current-state)
  source in this reader.
- **`position`/`team` are now populated on FPL-API-sourced rows too — the
  follow-up this module's own docstring named as deliberately not built,
  closed later in session s005.** `providers/fpl.py::FPLProvider.
  _fetch_gameweek_stats` resolves them from the `elements`/`teams` time
  series **as of the gameweek's own deadline** (never "today's" state —
  CLAUDE.md rule 2 / lesson 4), when its caller supplies the two already-
  resolved snapshots (`elements_as_of`/`teams_as_of` params — the provider
  itself makes no store call, matching every other provider in this
  codebase; see that module's docstring for the full design).
  `resolve_elements_and_teams_as_of_deadline()` below is the one sanctioned
  way to build those two snapshots — `scripts/snapshot_gameweek_stats.py`
  is the only live caller today. If the caller omits them, both columns
  stay NULL, same degraded-but-honest behaviour as before this fix.
  **The two sides do not mean EXACTLY the same string for `position`**:
  vaastav's is whatever that season's raw CSV recorded (`GK` or `GKP`
  depending on era, occasionally `AM` for the Assistant Manager chip
  position, `null` for pre-2020 seasons that never had the column at all —
  see `schemas.py`'s module comment); the FPL-API-sourced value is always
  that CURRENT season's live `element_types[].singular_name_short`, read
  fresh off the same bootstrap-static payload already being fetched (never
  hardcoded — CLAUDE.md rule 4), so an unrecognised `element_type` code
  (e.g. a future season reintroducing a 5th, Assistant-Manager-only type)
  resolves to `None` rather than being silently mapped onto an outfield
  position. `team` is the FPL team's own `name` field on both sides
  (verified live: the real store's `teams.name` — "Man Utd", "Spurs" — is
  the same short display-name convention vaastav's archive uses), so that
  column DOES mean the same thing on both sides, just resolved via a
  different mechanism (archived CSV column vs. a live snapshot join).
- **Many vaastav-only optional columns STILL have no FPL-API-provider
  counterpart**: `transfers_balance`, `transfers_in`, `transfers_out`,
  `threat`, `creativity`, `influence`, `ict_index`, `xP`, and the `mng_*`
  (Assistant Manager chip) family. `pl.concat(..., how="diagonal_relaxed")`
  fills these NULL for FPL-sourced rows — the honest reading ("this
  provider never claimed to have this field") is indistinguishable from
  "this column doesn't apply to this row", which is exactly true here;
  nothing is silently invented to fill the gap.
- **`attribution_complete`** — added by THIS reader for vaastav's rows
  (always `True`: a post-season archive with no live per-fixture
  ambiguity of the kind this flag exists to name) and read straight off
  the FPL API provider's own rows, where it is `False` for exactly the
  double-gameweek degraded rows described below — never silently
  indistinguishable between a complete and a degraded row.
- **`source_provider`** — `"vaastav_archive"` or `"fpl_api"`, added by
  this reader on every row, always present, so a caller never has to
  reconstruct provenance from other columns.

## The double-gameweek limitation on FPL-API-sourced rows

`total_points` and `minutes` are always exact, even on a double gameweek
(see `fplai.providers.fpl`'s module docstring for why those two are
structurally safe to reconstruct per fixture). `goals_scored`, `assists`,
`clean_sheets`, `own_goals`, `yellow_cards`, `red_cards`, `penalties_
saved`, `penalties_missed` and `bonus` are also exact. `saves`, `goals_
conceded` and `defensive_contribution` are NULL unless that identifier
happened to score points in that specific fixture — genuinely unknown,
never guessed as zero. **No real double gameweek has occurred in 2026/27
as of this session** (verified: GW1, the only settled gameweek, has zero
multi-fixture elements) — this path is exercised only by a synthetic,
hand-built payload in `tests/test_provider_fpl.py`, not by real data.

## What a migration onto this reader would still need to do

Not done in this story (explicitly out of scope — see the s005 brief;
recorded here so a future session does not have to re-derive it):

1. Each of the six model modules currently does `raw = store.effective_
   at(DATASET, as_of)` with `DATASET = "vaastav_player_gameweek_stats"`
   hardcoded at module scope. A migration replaces that one call with
   `raw = read_player_gameweek_stats(store, as_of=as_of)` — every
   downstream filter/aggregation in that module's own `build_training_
   table` should not need to change, since this reader's output schema
   is a strict superset of vaastav's own (same required columns, plus
   `attribution_complete`/`source_provider`).
2. Two of the six model modules (`minutes.py`, `defensive_contribution.py`)
   are owned by a PARALLEL agent in this same session — migrating them
   here would collide with in-flight work on the same files. Sequencing
   the migration after both land is the Architect's call, not this
   story's.
3. `defensive_contribution.py`'s `build_training_table` filters/
   aggregates the `defensive_contribution` column directly; on a
   double-gameweek FPL-sourced row that column can be NULL (see above).
   A migrated model must decide explicitly what NULL means for its own
   training rows — dropping the row is the conservative default, but
   nothing enforces that without an explicit decision (treating NULL as
   "did not meet threshold" would be WRONG — the exact same reasoning
   `pin_dc_thresholds.py` already established: absence is not evidence
   of zero).
4. Every migrated model must decide what `attribution_complete=False`
   means for its own gate — most plausibly, exclude degraded double-
   gameweek rows from training/backtesting entirely rather than silently
   training on a partially-null outcome vector. This reader does not make
   that decision for a caller; it only makes the flag visible.
5. Any model that currently assumes a vaastav-only optional column
   (`xP`, `threat`/`creativity`/`influence`/`ict_index`, `mng_*`) is
   always populated must be checked against the new NULL-for-FPL-rows
   reality, not assumed safe.

## No cross-source deduplication on the entity key

This reader does NOT deduplicate across sources — if a `(season, round,
element, fixture)` combination were genuinely present in BOTH datasets, it
returns BOTH rows, never silently picking one. This is not a risk today:
vaastav's archive stops at 2025-26 (its own module docstring); the FPL
API provider is registered with `seasons=frozenset({<current season>})`
only (`providers/fpl.py::register`). But nothing at this layer enforces
that disjointness — a consumer that needs entity-key uniqueness after the
union (most callers will) must check/dedupe itself. Recorded honestly
rather than silently assumed away — this session had no real overlapping
data to design a merge POLICY (which source wins?) correctly against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import polars as pl

from fplai.store import BitemporalStore

VAASTAV_DATASET = "vaastav_player_gameweek_stats"
FPL_API_DATASET = "fpl_api_player_gameweek_stats"

# FPL's own timestamp convention for `events[].deadline_time` — the same
# format `PLAYER_GAMEWEEK_STATS_GAMEWEEK.valid_time_format` already
# declares for `kickoff_time` (fplai.schemas), verified live to be
# identical: ISO-8601 with a literal trailing "Z", never a numeric offset.
_DEADLINE_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def read_player_gameweek_stats(
    store: BitemporalStore, *, as_of: datetime, season: str | None = None
) -> pl.DataFrame:
    """The capability-level read for `player.gameweek_stats@gameweek` —
    unions vaastav's archive and the FPL API's own live ingestion,
    resolved bitemporally via `effective_at()` on EACH dataset
    independently (see this module's docstring for why `as_of()` would be
    wrong for either). Adds `source_provider` (`"vaastav_archive"` /
    `"fpl_api"`) and, for vaastav's rows only, `attribution_complete=True`
    (the FPL API provider's own rows already carry this column).

    `season`, if given, filters BOTH sources before the union — cheaper
    than filtering after, and avoids ever materialising the full
    179,960+-row vaastav history for a caller that only wants one season.

    Returns an EMPTY frame (not a raise) if both sources are empty at
    `as_of` — matches `BitemporalStore.effective_at()`'s own "nothing
    ingested this far back" behaviour, never a crash for that case.
    """
    frames: list[pl.DataFrame] = []

    vaastav_rows = store.effective_at(VAASTAV_DATASET, as_of)
    if not vaastav_rows.is_empty():
        if season is not None:
            vaastav_rows = vaastav_rows.filter(pl.col("season") == str(season))
        if not vaastav_rows.is_empty():
            vaastav_rows = vaastav_rows.with_columns(
                pl.lit("vaastav_archive").alias("source_provider"),
                pl.lit(True).alias("attribution_complete"),
            )
            frames.append(vaastav_rows)

    fpl_rows = store.effective_at(FPL_API_DATASET, as_of)
    if not fpl_rows.is_empty():
        if season is not None:
            fpl_rows = fpl_rows.filter(pl.col("season") == str(season))
        if not fpl_rows.is_empty():
            fpl_rows = fpl_rows.with_columns(pl.lit("fpl_api").alias("source_provider"))
            frames.append(fpl_rows)

    if not frames:
        return pl.DataFrame()
    if len(frames) == 1:
        return frames[0]

    return pl.concat(frames, how="diagonal_relaxed")


# -- ingest-time position/team resolution (session s005, continued) --------
#
# Closes the load-bearing gap this module's own docstring named and left:
# `position`/`team` were NULL on every FPL-API-sourced row, and every
# model in `fplai.models` filters or groups by position, so the union
# reader above could not yet feed a model for the current season.
#
# **Where the join lives, and why here rather than inside the provider or
# inside `read_player_gameweek_stats` itself.** Three places were
# considered:
#
# 1. Inside `FPLProvider._fetch_gameweek_stats` itself, reading the store
#    directly. Rejected: verified against every existing adapter in
#    `fplai/providers/` that NONE of them import `fplai.store` for actual
#    I/O (only `content_hash`, a pure hashing helper) — providers in this
#    codebase are a deliberate, consistent "wraps the live API/archive,
#    touches no store" layer, and `PLProvider` already has a precedent for
#    exactly this situation (`elements`/`teams` DataFrames handed in at
#    construction as plain data, never fetched by the provider itself).
#    Blurring that boundary for this one capability would be introducing
#    a store dependency at a module boundary "for convenience" — the same
#    smell CLAUDE.md warns about for a scalar where the optimiser needs a
#    shape, applied to a different boundary.
# 2. Inside `src/fplai/backfill.py`'s orchestrator, right before
#    `store.write()`. Rejected outright: `backfill.py` is READ-ONLY for
#    this story, and in any case `scripts/backfill.py`'s CLI does not
#    even wire `--provider fpl_api` as a choice today — the ONLY live
#    entry point that ingests `fpl_api_player_gameweek_stats` is
#    `scripts/snapshot_gameweek_stats.py`. Putting the fix in a path that
#    is not actually exercised would not close the gap.
# 3. HERE, resolved ONCE per gameweek (this dataset's own natural ingest
#    grain — one script invocation, one `--gw`) and handed to the
#    provider as two already-resolved snapshots (`elements_as_of`,
#    `teams_as_of`), which it uses as pure lookups — mirroring
#    `PLProvider`'s own precedent exactly, and keeping this function
#    trivially unit-testable against a temp store with no live API
#    involved at all (see `tests/test_gameweek_stats.py`'s leakage-attack
#    tests). This is genuinely "at ingest": the resolved values are
#    written into `fpl_api_player_gameweek_stats` as real stored columns,
#    not recomputed on every read.
#
# **The bitemporal resolution itself.** `elements`/`teams` declare NO
# `valid_time_column` (`fplai.schemas.DATASET_VALID_TIME_COLUMNS` has no
# entry for either) — there is no per-row domain valid-time fact for "a
# player's team", only "what our snapshot cadence had observed by instant
# T". `store.as_of(dataset, T)` (OBSERVED-time resolution, `<=` inclusive)
# is therefore the correct primitive, not `effective_at()` (which would
# raise for either dataset — see `_resolve_valid_time_column`). `T` here
# is the gameweek's own `deadline_time` (from `events`), never "today" —
# resolving against today's state would be exactly the "reading present-
# day team/position into a historical gameweek's row" leakage CLAUDE.md
# rule 2 names explicitly, made concrete: a transferred player's mid-
# season row would silently carry their NEW club. Lesson 4 (docs/
# HANDOFF.md) applies directly here too: player/team identity is
# asymmetric, and "team codes persisting across seasons make historical
# resolution APPEAR to work" is exactly the trap a same-season transfer
# would spring on a caller that read `store.latest()` instead of
# `store.as_of(..., deadline)`.


@dataclass(frozen=True)
class DeadlineResolvedSnapshots:
    """`elements`/`teams` STATE as of one gameweek's own deadline — see
    `resolve_elements_and_teams_as_of_deadline()`. `deadline` is carried
    for provenance/logging on the composed object (mirrors `fplai.scoring.
    ScoringConfig.valid_as_of`'s same convention) — nothing downstream
    re-derives it from the two frames."""

    deadline: datetime
    elements: pl.DataFrame
    teams: pl.DataFrame


def resolve_elements_and_teams_as_of_deadline(
    store: BitemporalStore, deadline_time: str, *, deadline_format: str = _DEADLINE_TIME_FORMAT
) -> DeadlineResolvedSnapshots:
    """Resolve `elements`/`teams` STATE as of a gameweek's own
    `deadline_time` (the string FPL's `events[].deadline_time` carries,
    e.g. `"2026-08-21T17:30:00Z"` — the SAME format `PLAYER_GAMEWEEK_
    STATS_GAMEWEEK.valid_time_format` already declares for `kickoff_time`)
    — never "today's" state. See this module's comment block above for
    the full bitemporal reasoning.

    Both returned frames may be EMPTY (not a raise) if this store has no
    `elements`/`teams` observation at or before the deadline — matches
    `BitemporalStore.as_of()`'s own "nothing ingested this far back"
    behaviour. A caller (`FPLProvider._fetch_gameweek_stats`) that gets an
    empty frame here resolves `position`/`team` to `None` for every row,
    never guessing — the same "genuinely unknown, never guessed"
    discipline this module's docstring already applies to the divisor/
    threshold identifiers on a double-gameweek row.

    Raises `BitemporalError` (from `store.as_of`) if `deadline_time` fails
    to parse against `deadline_format` — surfaced as-is, not swallowed,
    because a caller with an unparseable deadline has no honest fallback
    to resolve either snapshot against.
    """
    deadline = datetime.strptime(deadline_time, deadline_format).replace(tzinfo=timezone.utc)
    elements = store.as_of("elements", deadline)
    teams = store.as_of("teams", deadline)
    return DeadlineResolvedSnapshots(deadline=deadline, elements=elements, teams=teams)
