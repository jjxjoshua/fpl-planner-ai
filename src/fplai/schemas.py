"""Canonical schemas — story 1 of E2b (blueprint §12, §12.1, §12.2).

Domain entities and fact tables, keyed by capability. No I/O — this module
only describes shapes; nothing here makes a network call or touches the
store.

Every capability the ingest layer can be asked for is `(entity, measure,
grain)` — the same logical fact at a different GRAIN is a DIFFERENT
capability (blueprint §12.1). `player.defensive_actions@match` and
`player.defensive_actions@season` are two keys, not one key with a
"granularity" field a caller could forget to check — this is the specific
bug (a season-grain provider silently answering a match-grain query) the
design exists to prevent.

`FactTableSchema.required_fields` is deliberately a MINIMUM contract, not
the full field list. FPL's bootstrap-static payload carries 109+ fields per
element; the ingest scripts keep all of them (see
docs/wiki/runbook-ingest.md — "flatten and keep everything" beats
hand-curating a list). `required_fields` only pins down what callers may
rely on existing, so validation catches a genuinely broken/reshaped
response without also breaking on a provider that (legitimately) supplies
more than the minimum.

`is_modelled` exists now, unused, for story 9 (derived-capability
framework — out of scope here). Blueprint §12.2: a DERIVED fact (e.g. the
DC per-player-per-match estimator) must never be queryable as though it
were an observation. This flag is the schema-level half of that contract;
nothing in this slice sets it True because nothing in this slice computes
a derived capability yet.

The first seven `CANONICAL_SCHEMAS` entries below are exactly the
capabilities the FPL provider (`providers/fpl.py`) serves — i.e. exactly
what the two existing ingest scripts already write to the store (E2b
story 5). Five more were added by E2b stories 6-7 for the Premier League
API provider (`providers/pl.py`): `match.lineups@match`,
`match.substitutions@match`, `team.match_stats@match`,
`player.season_stats@season`, `match.fixtures@matchweek`. A sixth,
`match.officials@match`, was added by session s004 for the same provider —
see that block's own comment for why it is a separate live request, not a
free ride on an already-fetched payload. Two more were
added by E2b story 10 for the vaastav archive provider
(`providers/vaastav.py`): `player.gameweek_stats@gameweek`,
`player.identity@season`. One more was added by E2b story 7b, closing the
player/team asymmetry blueprint §12.5 flags: `team.identity@season`, also
served by `providers/vaastav.py` (`data/{season}/teams.csv` — see that
module's docstring for the verified layout and its own drift, distinct
from `players_raw.csv`'s). Two more were added by E2b story 10b for the
olbauday archive provider (`providers/olbauday.py`): `player.attributes@
gameweek`, `gameweek.field_summary@gameweek`. In every case the rule is the
same: a schema is declared here only when a real adapter serves it and a
real test exercises it — never speculative surface.

**`player.attributes@gameweek` (olbauday) is deliberately NOT the same
capability as `player.gameweek_stats@gameweek` (vaastav)**, even though
both are keyed at player-per-gameweek grain and both come from an archive.
They are different MEASURES at the same grain (blueprint §12.1's whole
point): vaastav's is match *performance* (`total_points`, `minutes`,
`fixture`, `was_home` — what happened on the pitch); olbauday's is an
*attribute snapshot* (`selected_by_percent`, `status`, `now_cost`,
`chance_of_playing_next_round` — what the game's UI showed at some point in
time). Registering them under one key would let `CapabilityRegistry`
silently substitute one for the other on a priority tiebreak — exactly the
failure §12.1 exists to prevent. Note also `selected_by_percent`
(olbauday) and `selected` (vaastav) are DIFFERENT UNITS — a percentage of
managers vs. a raw ownership count — never divide/compare them directly
without converting.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


class SchemaError(ValueError):
    """A payload does not satisfy a FactTableSchema's contract."""


# -- dtype contract for FactTableSchema.validate() (closes the "presence,
#    not dtype" gap, docs/wiki/provider-framework.md §14.6) ----------------
#
# A CLASS check, not exact-dtype equality — see FactTableSchema.validate's
# docstring for the full reasoning. `pl.Utf8` and `pl.String` are the same
# class object in this Polars version (verified: `pl.Utf8 is pl.String`),
# listed once; `pl.Decimal` is grouped with float (never actually produced
# by an adapter today, included for completeness — a Decimal column would
# otherwise be unclassifiable for no real reason).
#
# `pl.Null` is its OWN accepted family, deliberately — not rejected as
# "unclassifiable". Verified against a real, already-documented case, not
# hypothesised: `MATCH_ODDS_FIXTURE.outcome_point` is REQUIRED and legitimately
# NULL for every row of an h2h/h2h_lay outcome (this schema's own
# description above: "NULL for h2h/h2h_lay outcomes"). `providers/odds.py`
# builds it from `outcome.get("point")`, which is `None` whenever a
# bookmaker's response for that fixture carries no "totals" market at
# all — plausible for a lower-profile fixture on a given capture, not a
# contrived edge case. If every row in one batch has `outcome_point=None`,
# Polars infers the column as dtype `Null` (no observed value to type it
# from) — rejecting that would make a schema-conformant capture fail this
# check for a reason unrelated to the presence-not-dtype gap this exists to
# close. The genuinely dangerous shapes are List/Struct/Array/Categorical/
# Enum/Object — a nested or enumerated value landing where a scalar (or a
# legitimately-absent scalar) is expected — and those alone are what
# `_dtype_family` returns `None` for.
_DTYPE_FAMILIES: dict[str, tuple[type, ...]] = {
    "integer": (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64),
    "float": (pl.Float32, pl.Float64, pl.Decimal),
    "string": (pl.Utf8,),
    "boolean": (pl.Boolean,),
    "temporal": (pl.Date, pl.Datetime, pl.Time, pl.Duration),
    "null": (pl.Null,),
}


def _dtype_family(dtype: pl.DataType) -> str | None:
    """Classify a Polars dtype (e.g. `Datetime(time_unit='us',
    time_zone='UTC')`) into one of `_DTYPE_FAMILIES` by its `base_type()`
    (the parametrised instance stripped to its class, e.g. `Datetime`) —
    or `None` if it fits none of them. `None` is what `FactTableSchema.
    validate()` treats as a schema violation for a required field."""
    base = dtype.base_type()
    for family, members in _DTYPE_FAMILIES.items():
        if base in members:
            return family
    return None


@dataclass(frozen=True)
class CapabilityKey:
    """`(entity, measure, grain)` — e.g. `player.defensive_actions@match`.

    Grain is part of the key, not an attribute of it: equality and hashing
    are structural (a plain frozen dataclass), so this is directly usable
    as a dict key in the capability registry and in `CANONICAL_SCHEMAS`
    below — there is no separate "does the grain match" check a caller
    could skip.
    """

    entity: str
    measure: str
    grain: str

    def __str__(self) -> str:
        return f"{self.entity}.{self.measure}@{self.grain}"

    def __repr__(self) -> str:
        return f"CapabilityKey({self!s})"


@dataclass(frozen=True)
class FactTableSchema:
    """The canonical shape of one capability's rows.

    `entity_key` names the column(s) that identify one entity within the
    table. The store dataset (not capability) this schema backs is looked
    up in `DATASET_ENTITY_KEYS` below and used by `BitemporalStore.as_of()`
    to collapse to STATE (blueprint §3.2). An empty tuple means the table
    is a singleton (e.g. game config) with no per-row entity to collapse
    to — `as_of()` returns exactly one row, the latest observation of the
    whole table.

    `valid_time_column`/`valid_time_format` — blueprint §3.2's "Valid time
    is per ROW, not per batch" ruling, decision 2026-08-22. `BitemporalStore.
    write()` stamps ONE scalar `valid_at` on every row of a batch (right for
    a snapshot; a meaningless constant for a bulk-ingested archive where each
    row's real valid time is its own domain fact, e.g. a fixture's kickoff).
    A dataset whose rows carry their OWN per-row valid time DECLARES the
    column that holds it here — declaration, not inference, same posture as
    `entity_key`. `None` (the default) means this dataset has no such
    column; it keeps today's behaviour (`as_of()`/`observations()` only,
    `BitemporalStore.effective_at()` refuses it — see that method).
    `valid_time_format` is a `strptime` format string ONLY if the declared
    column is stored as a STRING (verified live: `vaastav_player_gameweek_
    stats.kickoff_time` is `"2019-08-10T11:30:00Z"`, VARCHAR on disk, not a
    native Datetime — a real, present shape, not a hypothetical); `None`
    means the column is already a native (naive UTC) `Datetime`. This is
    declared, not sniffed from the on-disk dtype at query time, for the same
    reason `_dtype_family` is a class check rather than silent coercion: a
    caller should never have `effective_at()`'s parsing behaviour depend on
    which era's file happened to be read.
    """

    capability: CapabilityKey
    entity_key: tuple[str, ...]
    required_fields: tuple[str, ...]
    is_modelled: bool = False
    description: str = ""
    valid_time_column: str | None = None
    valid_time_format: str | None = None

    def __post_init__(self) -> None:
        missing_key_in_fields = [k for k in self.entity_key if k not in self.required_fields]
        if missing_key_in_fields:
            raise SchemaError(
                f"{self.capability}: entity_key column(s) {missing_key_in_fields} must also "
                "appear in required_fields — an entity key that isn't guaranteed present "
                "can't identify anything."
            )
        if self.valid_time_format is not None and self.valid_time_column is None:
            raise SchemaError(
                f"{self.capability}: valid_time_format is set ({self.valid_time_format!r}) but "
                "valid_time_column is None — a parse format only makes sense for a declared "
                "valid-time column."
            )
        if self.valid_time_column is not None and self.valid_time_column not in self.required_fields:
            raise SchemaError(
                f"{self.capability}: valid_time_column {self.valid_time_column!r} must also "
                "appear in required_fields — a declared valid-time column that isn't guaranteed "
                "present can't be resolved against (same rule entity_key already follows)."
            )

    def validate(self, df: pl.DataFrame) -> None:
        """Raise SchemaError if `df` does not satisfy this schema's
        contract. Three checks, in order:

        1. PRESENCE — every `required_fields` column exists.
        2. DTYPE FAMILY (required fields only) — each required field's
           dtype must classify into one of `_DTYPE_FAMILIES` (integer,
           float, string, boolean, temporal). This is deliberately a
           *class* check, not exact-dtype equality: `Int32` vs `Int64`
           (CSV-era vaastav vs a fresh API payload) and `Utf8` vs a parsed
           `Float64` for the same logical percentage are both real,
           legitimate drift already present across this store's providers
           and eras (see the module comment above `PLAYER_GAMEWEEK_STATS_
           GAMEWEEK` and `PLAYER_IDENTITY_SEASON` on `opta_code`) — pinning
           an exact dtype would fail a season this store is explicitly
           designed to accept. `Null` (a column with literally no observed
           value this batch — e.g. `MATCH_ODDS_FIXTURE.outcome_point` for a
           fixture whose bookmakers returned no "totals" market at all,
           already documented as legitimately NULL above) is its own
           accepted family, not a violation — see `_dtype_family`'s module
           comment for why. What this check refuses is a dtype that fits
           NONE of the six families: `List`/`Struct`/`Array` (a nested
           shape landing where a scalar is expected, the "genuinely broken/
           reshaped response" class this module's docstring already names
           as the reason `required_fields` validation exists at all), or
           `Categorical`/`Enum`/`Object` (never produced by any current
           adapter — new territory that should fail loudly on first
           appearance, not be silently accepted).
        3. TIMEZONE-NAIVE (every column, not just required ones) — no
           column may be a timezone-AWARE `Datetime`. This is the specific
           gap that let a genuinely tz-aware `commence_time`/`market_last_
           update` column reach the real store un-objected-to
           (docs/wiki/provider-framework.md §14.6): Polars/pyarrow writes
           a tz-aware column as a real Parquet `TIMESTAMP WITH TIME ZONE`,
           and DuckDB's Python conversion path for that type requires
           `pytz`, deliberately not a project dependency (blueprint §3.2 —
           `tzdata` solves a different problem, Python's own `zoneinfo`,
           not DuckDB's TIMESTAMPTZ-to-Python bridge). Every provider's
           in-`rows` timestamp column is naive UTC by convention (this
           store's bitemporal METADATA — `valid_at`/`observed_at` — is the
           only thing that is ever genuinely tz-aware, and
           `BitemporalStore._require_utc` normalises those itself before
           they reach disk; a DATA column inside `df` must already be
           naive when it gets here). Checked across every column of `df`,
           not only `required_fields`, because the failure mode (a real
           TIMESTAMPTZ column DuckDB can't read back) does not care
           whether the offending column happens to be on anyone's minimum
           contract — the incident's two offending columns were required
           only by coincidence.

        Dtype normalisation itself still belongs to the adapter (unchanged
        from the original design) — this check does not coerce anything,
        it only refuses to let a violating payload continue past this
        point. Because every current provider's `fetch()` already calls
        this method before returning a `FetchResult` (and `fplai.derived.
        write_derived` calls it again on the enriched derived row), no
        adapter needs to change to satisfy checks 2/3 — they already all
        comply (verified against every dataset in the real store,
        2026-08-21: no dtype outside these five families, no tz-aware
        column anywhere on disk today)."""
        missing = [f for f in self.required_fields if f not in df.columns]
        if missing:
            raise SchemaError(
                f"{self.capability}: payload is missing required fields {missing}. "
                f"Present columns: {sorted(df.columns)}"
            )

        unclassifiable = [
            (f, df.schema[f]) for f in self.required_fields if _dtype_family(df.schema[f]) is None
        ]
        if unclassifiable:
            raise SchemaError(
                f"{self.capability}: required field(s) have a dtype that fits none of "
                f"this store's six accepted families (integer/float/string/boolean/"
                f"temporal/null): {[(f, str(d)) for f, d in unclassifiable]}. This is "
                "the presence-not-dtype gap (docs/wiki/provider-framework.md §14.6) — "
                "a genuinely broken or reshaped response (a nested Struct/List/Array, "
                "or a Categorical/Enum/Object column no current adapter produces) must "
                "fail here, not three layers downstream at a DuckDB read."
            )

        tz_aware = [
            name
            for name, dtype in df.schema.items()
            if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None
        ]
        if tz_aware:
            raise SchemaError(
                f"{self.capability}: column(s) {tz_aware} are timezone-AWARE Datetime "
                "columns. Every DATA column in this store is naive UTC by convention — "
                "only bitemporal metadata (valid_at/observed_at) is ever genuinely "
                "tz-aware, and BitemporalStore normalises those itself. A tz-aware DATA "
                "column persists as a real Parquet TIMESTAMP WITH TIME ZONE, which "
                "DuckDB cannot read back without pytz (not a project dependency) — this "
                "is the exact incident in docs/wiki/provider-framework.md §14.6. "
                "Normalise to naive UTC in the adapter "
                "(`.astimezone(timezone.utc).replace(tzinfo=None)`) before returning it."
            )


# -- capability key vocabulary, this slice (FPL provider only) -------------

PLAYER_ATTRIBUTES_CURRENT = CapabilityKey("player", "attributes", "current")
TEAM_ATTRIBUTES_CURRENT = CapabilityKey("team", "attributes", "current")
GAMEWEEK_ATTRIBUTES_SEASON = CapabilityKey("gameweek", "attributes", "season")
CHIP_WINDOW_SEASON = CapabilityKey("chip", "window", "season")
GAME_CONFIG_CURRENT = CapabilityKey("game", "config", "current")
GAME_SETTINGS_CURRENT = CapabilityKey("game", "settings", "current")
MANAGER_PICKS_SELECTION_GAMEWEEK = CapabilityKey("manager_picks", "selection", "gameweek")
# Pre-deadline gate-repair session s003 (blueprint §3.4 "Blocker 3") —
# `automatic_subs[]` is a TOP-LEVEL list in the picks/ payload, a DIFFERENT
# GRAIN from `picks[]` (blueprint §12.1's whole point, same as
# player.attributes@gameweek vs player.gameweek_stats@gameweek above): a
# picks row is "one squad slot"; an automatic-subs row is "one substitution
# EVENT that happened to this entry this gameweek" — most entries have ZERO
# of these most gameweeks (verified live: automatic_subs is [] in every
# real payload captured so far, all pre-scoring), some have one, and in
# principle more than one on a gameweek with several blanks. Cramming this
# into a picks row (there is no natural 1:1 pick to attach it to — the
# player who's OUT already has its own picks row, and the player who came
# ON does too) or flattening it under the picks entity key would repeat
# exactly the "entity key doesn't match the data's actual grain" bug class
# already hit twice on this project (picks omitting `event`,
# vaastav_player_gameweek_stats omitting `fixture`) — see this module's
# docstring precedent. It gets its own capability and dataset instead.
MANAGER_AUTOMATIC_SUBS_GAMEWEEK = CapabilityKey("manager_picks", "automatic_subs", "gameweek")


CANONICAL_SCHEMAS: dict[CapabilityKey, FactTableSchema] = {
    PLAYER_ATTRIBUTES_CURRENT: FactTableSchema(
        capability=PLAYER_ATTRIBUTES_CURRENT,
        entity_key=("id",),
        required_fields=("id", "web_name", "team", "element_type", "now_cost", "selected_by_percent", "status"),
        description=(
            "FPL bootstrap-static 'elements' — one row per player, current "
            "state only. No history; every unsnapshotted change is lost "
            "(blueprint §3.4). 109+ fields present in practice; only the "
            "minimum identifying/pricing/status set is required here."
        ),
    ),
    TEAM_ATTRIBUTES_CURRENT: FactTableSchema(
        capability=TEAM_ATTRIBUTES_CURRENT,
        entity_key=("id",),
        required_fields=("id", "name", "short_name"),
        description="FPL bootstrap-static 'teams' — one row per club, current state.",
    ),
    GAMEWEEK_ATTRIBUTES_SEASON: FactTableSchema(
        capability=GAMEWEEK_ATTRIBUTES_SEASON,
        entity_key=("id",),
        required_fields=("id", "deadline_time"),
        description="FPL bootstrap-static 'events' — one row per gameweek, whole season in one call.",
    ),
    CHIP_WINDOW_SEASON: FactTableSchema(
        capability=CHIP_WINDOW_SEASON,
        entity_key=("id",),
        required_fields=("id", "name"),
        description="FPL bootstrap-static 'chips' — one row per chip x half (blueprint §11).",
    ),
    GAME_CONFIG_CURRENT: FactTableSchema(
        capability=GAME_CONFIG_CURRENT,
        entity_key=(),
        required_fields=("payload",),
        description=(
            "FPL bootstrap-static 'game_config' — singleton, whole object "
            "JSON-encoded into one 'payload' column (scoring, squad rules, "
            "chip counts; blueprint §11 — read from here, never hardcoded)."
        ),
    ),
    GAME_SETTINGS_CURRENT: FactTableSchema(
        capability=GAME_SETTINGS_CURRENT,
        entity_key=(),
        required_fields=("payload",),
        description="FPL bootstrap-static 'game_settings' — singleton, whole object JSON-encoded.",
    ),
    MANAGER_PICKS_SELECTION_GAMEWEEK: FactTableSchema(
        capability=MANAGER_PICKS_SELECTION_GAMEWEEK,
        entity_key=("entry_id", "event", "element"),
        required_fields=(
            "event",
            "entry_id",
            "element",
            "multiplier",
            "is_captain",
            "is_vice_captain",
            # Widened, pre-deadline gate-repair session s003 (blueprint §3.4
            # "Blocker 3"). `overall_rank` was ALREADY produced by every
            # existing writer of this dataset (both scripts/sample_picks.py's
            # own row-builder and the parallel fplai.providers.fpl.
            # _picks_to_rows — see the description below for why there are
            # two) even though it was never declared required; promoted to
            # required here since both already guarantee it, at zero risk.
            "overall_rank",
        ),
        description=(
            "FPL entry/{id}/event/{gw}/picks/ — one row per (entry, "
            "event, element) pick. `event` (gameweek) MUST be part of the "
            "entity key, not just (entry_id, element): the same entry+ "
            "element pair recurs every gameweek the player is picked, and "
            "those are different facts about different gameweeks, not "
            "revisions of the same fact — collapsing across `event` would "
            "silently merge separate gameweeks' picks into one row. This "
            "is the EO/captaincy sampling panel (blueprint §3.4) — the "
            "only source of real captaincy data, with no archive and no "
            "recovery path for an unsampled GW. `overall_rank` is "
            "legitimately ALL-NULL for a whole batch sampled before scores "
            "settle (verified live, 22 Aug: every one of 11 real GW1 "
            "captures had it null) — accepted by the dtype contract's "
            "`null` family (see FactTableSchema.validate's docstring), same "
            "precedent as MATCH_ODDS_FIXTURE.outcome_point. "
            "**Present-but-NOT-required, widened session s003 (blueprint "
            "§3.4 'Blocker 3', zero marginal request cost — same response, "
            "already paid for): `element_type` (picks[].element_type — "
            "position); and entry_history's `event` (kept as "
            "`entry_history_event`, an audit copy distinct from the row's "
            "own `event` — must agree or something upstream changed "
            "shape), `points` (kept as `gameweek_points` — THIS gameweek's "
            "score, not cumulative, to avoid colliding with `points_on_"
            "bench`), `total_points`, `rank`, `rank_sort`, "
            "`percentile_rank` (THE rank-aware objective's exact quantity, "
            "blueprint §2/§10: target top 10% overall, observed rather "
            "than inferred from rank / total_players) and `overall_rank_"
            "percentage`. All settle over the same timeline `overall_rank` "
            "does. NOT required (unlike overall_rank) because only "
            "scripts/sample_picks.py's row-builder produces them today — "
            "`fplai.providers.fpl._picks_to_rows` is a SEPARATE, narrower "
            "implementation of the same capability (its own docstring: "
            "'sample_picks.py itself still calls FPLClient.entry_picks() "
            "directly ... this function exists so the picks capability is "
            "genuinely implemented and unit-tested here regardless') that "
            "was out of this story's owned paths (src/fplai/providers/"
            "fpl.py) to update — requiring these fields here would break "
            "its own validate() call. Two implementations of one "
            "capability that can silently drift in their column coverage "
            "is itself worth the Architect's attention; flagged, not fixed, "
            "this session — see the s003 punch-card finding."
        ),
    ),
    MANAGER_AUTOMATIC_SUBS_GAMEWEEK: FactTableSchema(
        capability=MANAGER_AUTOMATIC_SUBS_GAMEWEEK,
        entity_key=("entry_id", "event", "element_out"),
        required_fields=("entry_id", "event", "element_in", "element_out"),
        description=(
            "FPL entry/{id}/event/{gw}/picks/ — the TOP-LEVEL `automatic_"
            "subs[]` list, a DIFFERENT GRAIN from picks[] (see the module-"
            "level comment above this capability's CapabilityKey constant "
            "for the full grain-mismatch reasoning). Each row is one "
            "automatic substitution FPL applied for one entry in one "
            "gameweek: `element_out` is the starter who didn't play and "
            "was subbed out, `element_in` is the bench player subbed in "
            "for them (field names verified against the FPL API's own "
            "documented shape — session s003 could not verify a POPULATED "
            "example live, because GW1 had not finished scoring yet at "
            "capture time; automatic_subs was [] in every real payload "
            "sampled this session, 11/11). `element_out` is chosen as the "
            "entity-key disambiguator (not element_in) because a starter "
            "can only be subbed out once per gameweek by construction — a "
            "bench player can equally only come on once, so `element_in` "
            "would work as well, but `element_out` parallels picks[]'s own "
            "`element` as 'the squad slot this row is about'. "
            "`scripts/sample_picks.py` ALSO asserts this key's uniqueness "
            "against every real batch it collects at write time (not just "
            "inspected once here) — the runtime check this description "
            "cannot substitute for, given the live-verification gap above. "
            "Value, per the s003 brief: this is OBSERVED autosubs, where "
            "the Phase 1 backtest SIMULATES autosubs from stored `minutes` "
            "(blueprint §7.2) — a free validation of a backtest component "
            "nobody has checked against ground truth yet."
        ),
    ),
}

# -- capability key vocabulary, E2b stories 6-7 (Premier League API adapter +
#    identity resolution) ------------------------------------------------
#
# Five capabilities served by providers/pl.py. `player.defensive_actions@match`
# is DELIBERATELY not declared here — blueprint §12.1's protection is that a
# query for an unregistered capability fails loudly via
# `registry.resolve()` (RegistryError), rather than silently being answered
# by a different grain. No source exists for it (docs/wiki/provider-
# evaluation.md, "Architect verification" section, "P2 verdict: NEGATIVE").
#
# `team.match_stats@match` and `player.season_stats@season` are both stored
# LONG (`(entity_key..., stat_key) -> value`), not one column per stat. The
# PL API returns 185 (team, per match) / 100+ (player, per season) keys per
# call, the key set is not documented as stable across seasons, and we pay
# for the whole payload on every fetch regardless (§3.2's "never re-fetch to
# fill gaps" — no source ever revisits an old response once cached). Long
# format means a season with a different key set never requires a schema
# migration; DuckDB pivots long -> wide trivially for a consumer that wants
# columns. Long-for-`team.match_stats@match` is an explicit Architect
# decision (E2b story 6/7 brief); long-for-`player.season_stats@season` is
# the same rationale applied by XL-Coder to a second capability with an
# identical shape (100+ sparse keys, undocumented stability) — see
# docs/wiki/provider-framework.md for the record of that call.
PLAYER_SEASON_STATS_SEASON = CapabilityKey("player", "season_stats", "season")
MATCH_LINEUPS_MATCH = CapabilityKey("match", "lineups", "match")
MATCH_SUBSTITUTIONS_MATCH = CapabilityKey("match", "substitutions", "match")
TEAM_MATCH_STATS_MATCH = CapabilityKey("team", "match_stats", "match")
MATCH_FIXTURES_MATCHWEEK = CapabilityKey("match", "fixtures", "matchweek")

CANONICAL_SCHEMAS[MATCH_LINEUPS_MATCH] = FactTableSchema(
    capability=MATCH_LINEUPS_MATCH,
    entity_key=("match_id", "player_element_id"),
    required_fields=(
        "match_id",
        "team_code",
        "player_element_id",
        "player_code",
        "role",
        "position",
        "shirt_number",
        "is_captain",
    ),
    description=(
        "PL API /v3/matches/{id}/lineups — one row per player named in a "
        "match's squad (starting XI + bench). `role` is 'start'/'bench' "
        "(derived from the PL API's `position == 'Substitute'` flag). "
        "`player_element_id` is the CURRENT-SEASON FPL element id, resolved "
        "via fplai.identity.PlayerIdentityMap from the PL API's raw numeric "
        "player id (`player_code`, == FPL elements[].code, blueprint §12.5 "
        "— identity resolution fails loudly, an unresolved player raises)."
    ),
)

CANONICAL_SCHEMAS[MATCH_SUBSTITUTIONS_MATCH] = FactTableSchema(
    capability=MATCH_SUBSTITUTIONS_MATCH,
    entity_key=("match_id", "team_code", "minute", "player_off_element_id"),
    required_fields=(
        "match_id",
        "team_code",
        "period",
        "minute",
        "player_on_element_id",
        "player_off_element_id",
        "player_on_code",
        "player_off_code",
    ),
    description=(
        "PL API /v1/matches/{id}/events, the `subs[]` block per side — one "
        "row per substitution, with the minute (blueprint §4.1's 'hooked at "
        "60 vs playing 90' distinction the minutes model needs). "
        "`player_on_element_id`/`player_on_code` are present but NOT "
        "required to be non-null — verified live (session s004, 2026-08-28, "
        "Crystal Palace v Aston Villa 2025-26, match_id 2561915, minute 90): "
        "a player can be subbed OFF with no one coming ON (subs already "
        "exhausted, or a deliberate 10-men finish). Real, not malformed; "
        "`player_off_element_id`/`player_off_code` (the entity-key "
        "disambiguator) are never observed null and raise loudly if they "
        "ever are."
    ),
)

CANONICAL_SCHEMAS[TEAM_MATCH_STATS_MATCH] = FactTableSchema(
    capability=TEAM_MATCH_STATS_MATCH,
    entity_key=("match_id", "side", "stat_key"),
    required_fields=("match_id", "side", "stat_key", "value"),
    description=(
        "PL API /v3/matches/{id}/stats — LONG format (Architect decision, "
        "E2b story 6/7 brief), one row per (match, side, stat). 185 raw "
        "Opta keys per side observed live, incl. expectedGoals, "
        "expectedGoalsOnTarget, expectedAssists, "
        "expectedGoalsOnTargetConceded, ballRecovery, totalTackle, "
        "wonTackle, interception, totalClearance, effectiveClearance, "
        "outfielderBlock, blockedPass, blockedScoringAtt, blockedCross, "
        "duelWon, duelLost — captured in full, never a chosen subset "
        "(§3.2: we pay for the whole response regardless, and never "
        "re-fetch to fill a gap). `team_code` is present but NOT required — "
        "the raw endpoint reports `side` ('Home'/'Away') only, with no team "
        "id in the payload; `team_code` is filled in only when the caller "
        "supplies the match's home/away team codes (typically already known "
        "from `match.fixtures@matchweek`). `value_raw` is present but NOT "
        "required — most of the 185 keys are plain numbers (`value`), but "
        "at least one observed live (`fastestPlayer`) is a nested object "
        "(`{topSpeed, playerId}`), not a scalar; `value` is null and "
        "`value_raw` carries its JSON-encoded original for any key that "
        "isn't a plain number, rather than silently dropping it."
    ),
)

CANONICAL_SCHEMAS[PLAYER_SEASON_STATS_SEASON] = FactTableSchema(
    capability=PLAYER_SEASON_STATS_SEASON,
    entity_key=("season", "player_element_id", "stat_key"),
    required_fields=("season", "player_element_id", "player_code", "stat_key", "value"),
    description=(
        "PL API /v2/competitions/{c}/seasons/{s}/players/{p}/stats — LONG "
        "format (XL-Coder decision, same rationale as team.match_stats: "
        "100+ sparse Opta keys per player, no documented stability across "
        "seasons). `season` is the PL API's own convention (starting-year "
        "string, e.g. '2025' for 2025/26), NOT FPL's 'YYYY-YY' string — "
        "see providers/pl.py's module docstring. `value_raw` — same "
        "non-scalar-value escape hatch as team.match_stats@match, present "
        "but not required."
    ),
)

CANONICAL_SCHEMAS[MATCH_FIXTURES_MATCHWEEK] = FactTableSchema(
    capability=MATCH_FIXTURES_MATCHWEEK,
    entity_key=("match_id",),
    required_fields=(
        "match_id",
        "season",
        "matchweek",
        "kickoff",
        "home_team_code",
        "away_team_code",
    ),
    description=(
        "PL API /v1/competitions/{c}/seasons/{s}/matchweeks/{n}/matches — "
        "one row per fixture. `home_team_code`/`away_team_code` are FPL "
        "team codes, resolved via fplai.identity.TeamIdentityMap from the "
        "PL API's raw numeric team id at fetch time — this is also the "
        "capability used to BUILD that map in the first place (blueprint "
        "§12.5's 'matches resolve by (kickoff date, home team, away team) "
        "after teams resolve' is exactly `fplai.identity.resolve_match` "
        "querying this table's rows)."
    ),
)

# -- capability key vocabulary, session s004 (PL API match officials) -------
#
# Sixth capability served by providers/pl.py, added s004. `GET /v1/matches/
# {id}/officials` is a SEPARATE endpoint from the two the adapter already
# calls at match grain — `v1/matches/{id}/events` (substitutions) and
# `v3/matches/{id}/lineups` — verified live (2026-08-28) that NEITHER
# existing response embeds officials/referee data anywhere in its payload.
# This capability therefore costs one ADDITIONAL live request per match, not
# a free ride on an already-paid-for fetch — see providers/pl.py's module
# docstring for the investigation this claim rests on.
#
# No player/team numeric id exists anywhere in this payload — only a name
# (`official.firstName`/`lastName`/`name`). There is nothing to resolve via
# fplai.identity.PlayerIdentityMap or TeamIdentityMap here; the ONLY
# identity concern at this grain is the MATCH itself, and that is already
# solved — `match_id` is the same PL API match id every other match-grain
# capability in this file uses, and `fplai.identity.resolve_match` (already
# built and live-verified for lineups/substitutions/team_match_stats) is the
# existing, unmodified mechanism a consumer (the cards model) uses to join
# this table onto vaastav's FPL `fixture` column, via (kickoff date,
# home_team_code, away_team_code). No new join mechanism was built or is
# needed for this capability.
MATCH_OFFICIALS_MATCH = CapabilityKey("match", "officials", "match")

CANONICAL_SCHEMAS[MATCH_OFFICIALS_MATCH] = FactTableSchema(
    capability=MATCH_OFFICIALS_MATCH,
    entity_key=("match_id", "role"),
    required_fields=(
        "match_id",
        "role",
        "is_referee",
        "official_name",
        "official_first_name",
        "official_last_name",
    ),
    description=(
        "PL API /v1/matches/{id}/officials — one row per official named "
        "against a match. 6 verified live (2 real fixtures, seasons 2020/21 "
        "and 2025/26): Referee, Assistant Referee#1, Assistant Referee#2, "
        "Fourth official, Video Assistant Referee, Assistant VAR Official. "
        "`role` is the PL API's own `type` string, verified DISTINCT per "
        "match in both live checks, which is why (match_id, role) is a "
        "sound entity key (CLAUDE.md lesson 2 — verified as uniqueness "
        "within a real observation batch, not assumed). `is_referee` is a "
        "derived boolean (`role == 'Referee'`) so a consumer never has to "
        "string-match the raw role literal to find the primary appointment. "
        "NO STABLE NUMERIC ID EXISTS for an official anywhere in this "
        "payload — `official_name`/`_first_name`/`_last_name` are the only "
        "identifiers the source provides. A referee-level historical model "
        "(e.g. a card-rate prior per referee) must key on NAME, which is a "
        "real, undocumented-upstream risk (accents, initials, homonyms) — "
        "recorded here rather than glossed over; no name-normalisation is "
        "attempted by this adapter, the raw strings are stored as-is."
    ),
)


# -- capability key vocabulary, E2b story 10 (vaastav archive adapter) ----
#
# Two capabilities served by providers/vaastav.py. Both sourced from
# `vaastav/Fantasy-Premier-League` (github.com/vaastav/Fantasy-Premier-League),
# a community-maintained per-season CSV archive, 2016-17 -> present. Verified
# live against seasons 2016-17 and 2025-26 (XL-Coder, 2026-08-20) — the
# repo's real layout, NOT assumed from the brief:
#
#   data/{season}/players_raw.csv        <- one row per player, THAT season's
#                                            bootstrap-static 'elements' snapshot
#   data/{season}/teams.csv               <- one row per club, that season
#   data/{season}/gws/gw{N}.csv           <- one row per player, one gameweek
#
# `{season}` is FPL's own "YYYY-YY" convention (e.g. "2025-26"), NOT the PL
# API's starting-year convention providers/pl.py uses — every call site in
# providers/vaastav.py takes `season` in the archive's own format and never
# hardcodes one (CLAUDE.md rule 4).
#
# SCHEMA DRIFT IS REAL AND VERIFIED, NOT A THEORETICAL RISK: 2016-17's
# players_raw.csv has NO `opta_code` column at all (it did not exist in the
# FPL API yet); 2016-17's gw CSV has NO `position`/`team`/`xP`/
# `expected_*`/`defensive_contribution` columns that 2025-26's does, and
# uses different per-player identifiers in places (`ea_index` instead of
# newer fields). This is handled EXPLICITLY, not by coercing a uniform shape
# across eras: `required_fields` below is deliberately the columns verified
# present in BOTH the oldest (2016-17) and newest (2025-26) seasons checked
# — a season that is missing even one of those genuinely required columns
# fails `FactTableSchema.validate()` loudly, rather than being silently
# null-padded to look complete. `opta_code` is a real example of a field
# that matters a great deal (it is the join key `fplai.identity.
# PlayerIdentityMap` needs) but is NOT in `required_fields` here — a
# pre-opta season's rows still validate as a legitimate "season's player
# list" capability; the SEPARATE failure (an unresolvable identity) is
# raised later, at the correct layer, by `PlayerIdentityMap.build()` itself
# when it finds the column missing (§12.5 — unmatched entities raise, at
# the layer that actually needs the join, not at schema-validation time
# for a capability that doesn't strictly require it).
PLAYER_GAMEWEEK_STATS_GAMEWEEK = CapabilityKey("player", "gameweek_stats", "gameweek")
PLAYER_IDENTITY_SEASON = CapabilityKey("player", "identity", "season")
# E2b story 7b — the team-identity half of blueprint §12.5's "identity maps
# are season-scoped" that story 10 left for players only. Declared here,
# alongside PLAYER_IDENTITY_SEASON, deliberately — same archive, same
# season-scoping requirement, same reason it belongs in this file rather
# than a new one.
TEAM_IDENTITY_SEASON = CapabilityKey("team", "identity", "season")

CANONICAL_SCHEMAS[PLAYER_GAMEWEEK_STATS_GAMEWEEK] = FactTableSchema(
    capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    entity_key=("season", "round", "element", "fixture"),
    required_fields=(
        "season",
        "round",
        "element",
        "fixture",
        "name",
        "total_points",
        "minutes",
        "selected",
        "value",
        "was_home",
        "team_a_score",
        "team_h_score",
        "kickoff_time",
    ),
    description=(
        "vaastav archive data/{season}/gws/gw{N}.csv — one row per player "
        "per gameweek per fixture (entity key = (season, round, element, "
        "fixture) to handle double/triple gameweeks where a player appears "
        "multiple times). `season` is injected by the adapter (not a column "
        "in the raw file); `round` IS the raw file's own gameweek-number "
        "column (verified present, same name, every season 2016-17 -> "
        "2025-26 — no separate 'gw' column is invented alongside it). "
        "`fixture` uniquely identifies the match within the gameweek. "
        "`selected` is the RAW OWNERSHIP COUNT (e.g. 550,561), not a "
        "percentage — this is the archived ownership blueprint §3.4 calls "
        "'recoverable'. `value` is the point-in-time price (x10, FPL's own "
        "convention, e.g. 55 = £5.5m). Both are the reason this capability "
        "exists: the FPL live API has no ownership/price HISTORY at all. "
        "Only the columns verified present in every season checked "
        "(2016-17 oldest, 2025-26 newest) are required — see this file's "
        "module-level comment above for the schema-drift columns "
        "(`xP`, `expected_*`, `defensive_contribution`, `position`, "
        "`team`, ...) that exist in recent seasons only and are NOT "
        "required, present-but-optional when the source season has them. "
        "\n\nSession s005 — this capability now has a SECOND provider: "
        "fplai.providers.fpl.FPLProvider, sourced from `event/{gw}/live/` "
        "for the CURRENT live season, dataset name "
        "'fpl_api_player_gameweek_stats' (deliberately NOT written into "
        "this dataset — see the '_DATASET_TO_CAPABILITY' comment above). "
        "It satisfies every REQUIRED field above except that `selected`/"
        "`value` are always NULL (the live API genuinely has no "
        "price/ownership history — the exact gap this docstring's "
        "previous paragraph already names as vaastav's reason for "
        "existing). It also adds `attribution_complete` (bool, present-"
        "but-not-required): False on a double-gameweek element's degraded "
        "per-fixture row, where only total_points/minutes and a subset of "
        "linear-scoring identifiers are exactly attributable per fixture "
        "and the divisor/threshold identifiers (saves, goals_conceded, "
        "defensive_contribution) are NULL rather than guessed — see "
        "fplai.providers.fpl's module docstring and fplai.gameweek_stats "
        "for the full design and its untested-on-real-DGW-data caveat. "
        "fplai.gameweek_stats.read_player_gameweek_stats is the capability-"
        "level reader that unions both providers' datasets, resolved "
        "bitemporally via effective_at() on each, with a `source_provider` "
        "column so a caller can always tell which produced a given row."
        "\n\nSession s005, CONTINUED — `position`/`team` (both present-but-"
        "optional above, same as on vaastav's own rows) are now ALSO "
        "populated on fpl_api-sourced rows, when the caller supplies "
        "already-resolved `elements`/`teams` snapshots taken AS OF THE "
        "GAMEWEEK'S OWN DEADLINE (fplai.gameweek_stats.resolve_elements_"
        "and_teams_as_of_deadline; `scripts/snapshot_gameweek_stats.py` is "
        "the one live caller). NULL otherwise, never guessed — this "
        "provider makes no store call itself (fplai.providers.fpl's "
        "module docstring explains why). `position` is FPL's live "
        "`element_types[].singular_name_short` for that gameweek's "
        "element_type code (read fresh off the same bootstrap-static "
        "payload every time, never hardcoded — CLAUDE.md rule 4); `team` "
        "is the resolved team id's `name` on the as-of `teams` snapshot — "
        "the same FPL display-name convention vaastav's own `team` column "
        "uses (verified live), though the two sides reach that string by "
        "different mechanisms (an archived CSV column vs. a live "
        "snapshot join) and vaastav's `position` string varies by season "
        "era (GK/GKP/AM/null) where fpl_api's always reflects whatever "
        "the CURRENT season's element_types payload defines."
    ),
    # Blueprint §3.2, decision 2026-08-22 ("Valid time is per ROW, not per
    # batch"). This dataset is bulk-ingested (one backfill session,
    # 2026-08-21): every row's `observed_at` is ~ingest time regardless of
    # which historical season/gameweek it describes, so `as_of()` (which
    # resolves OBSERVED time) correctly returns EMPTY for any real historical
    # deadline — useless for training/backtesting, which need THIS row's own
    # domain valid time, `kickoff_time`. Declared here so `BitemporalStore.
    # effective_at()` is the one sanctioned primitive for that, instead of
    # three (now two, see docs/wiki/provider-framework.md) modules each
    # hand-rolling `observations()` + a manual `kickoff_time` filter with
    # its own copy of this same justifying comment. `valid_time_format`
    # matches the verified on-disk shape exactly (VARCHAR, not a native
    # Datetime) — see this file's module comment above `PLAYER_GAMEWEEK_
    # STATS_GAMEWEEK` for the schema-drift context; the format string itself
    # never varies across seasons (verified: every season's gw CSV files use
    # this exact ISO-8601-with-Z shape).
    valid_time_column="kickoff_time",
    valid_time_format="%Y-%m-%dT%H:%M:%SZ",
)

CANONICAL_SCHEMAS[PLAYER_IDENTITY_SEASON] = FactTableSchema(
    capability=PLAYER_IDENTITY_SEASON,
    entity_key=("season", "id"),
    required_fields=(
        "season",
        "id",
        "code",
        "element_type",
        "team",
        "team_code",
        "web_name",
        "first_name",
        "second_name",
        "now_cost",
    ),
    description=(
        "vaastav archive data/{season}/players_raw.csv — THE season-scoped "
        "player list (blueprint §12.5's 'identity maps are season-scoped', "
        "extended 2026-08-20). One row per player, as that season's FPL "
        "bootstrap-static 'elements' payload looked (same column "
        "vocabulary as PLAYER_ATTRIBUTES_CURRENT, verified: id, code, "
        "element_type, team, team_code, web_name, now_cost all present "
        "2016-17 -> 2025-26 unchanged). `season` is injected by the "
        "adapter. `opta_code` deliberately NOT required here — present "
        "2025-26 (verified), ABSENT 2016-17 (verified, the FPL API had no "
        "Opta join key yet) — see the module-level comment above for why "
        "that absence is allowed to pass THIS schema and instead raises, "
        "correctly, at fplai.identity.PlayerIdentityMap.build() for a "
        "season that genuinely cannot support the opta_code join."
    ),
)

CANONICAL_SCHEMAS[TEAM_IDENTITY_SEASON] = FactTableSchema(
    capability=TEAM_IDENTITY_SEASON,
    entity_key=("season", "id"),
    required_fields=("season", "id", "code", "name", "short_name"),
    description=(
        "vaastav archive data/{season}/teams.csv — THE season-scoped team "
        "list (blueprint §12.5's player/team asymmetry, closed E2b story "
        "7b). `teams.csv` does NOT exist for 2016-17/2017-18/2018-19 "
        "(verified live — 404 on all three; the vaastav archive only grew "
        "this file from 2019-20 onward), unlike players_raw.csv, which "
        "goes back to 2016-17 — a genuine, real gap, not a bug in this "
        "adapter; `VaastavProvider.fetch` for a pre-2019-20 season "
        "surfaces the transport's 404 as a TransportError rather than "
        "inventing rows. Column set is otherwise stable across every "
        "season checked (2019-20 -> 2025-26): id, code, name, short_name "
        "all present unchanged; 2025-26 additionally has `link_url`, not "
        "required here. `id` is the season-LOCAL 1-20 slot (NOT stable "
        "across seasons on its own — a promoted club can reuse a departed "
        "one's `id` next season); `season` is therefore part of the entity "
        "key, same pattern as PLAYER_IDENTITY_SEASON. `code` is the "
        "cross-season-STABLE legacy id `fplai.identity."
        "build_team_identity_map_for_season`/`TeamIdentityMap.build` "
        "actually joins on — required here because, unlike "
        "PLAYER_IDENTITY_SEASON's opta_code, there is no legitimate "
        "'season's team list' row without it; every season this archive "
        "has teams.csv for carries `code` unbroken. `season` is injected "
        "by the adapter, exactly as for the other vaastav capabilities."
    ),
)


# -- capability key vocabulary, E2b story 10b (olbauday archive adapter) ----
#
# Two capabilities served by providers/olbauday.py, from
# `olbauday/FPL-Core-Insights` (github.com/olbauday/FPL-Core-Insights,
# branch `main` — NOT `master`, unlike vaastav). Verified live against all
# three available seasons (XL-Coder, 2026-08-21) — data-sources.md §3.2 was
# a survey, not a live probe of every season, and undersold two real
# things: the season string is FPL's "YYYY-YYYY" convention (e.g.
# "2025-2026"), NOT vaastav's "YYYY-YY" ("2025-26"); and every data file
# lives under a `data/` prefix (`data/{season}/...`), which §3.2's listing
# omitted.
#
# SCHEMA DRIFT IS REAL, VERIFIED, AND SEVERE FOR 2024-2025 — not a minor
# column or two. `playerstats.csv` for 2024-2025 has 58 columns; 2025-2026
# and 2026-2027 both have 87 (identical headers). The 29 columns 2024-2025
# lacks include `web_name`/`first_name`/`second_name` (NO player name in
# this file for that season at all), `news`/`news_added` (no injury-flag
# text), `minutes`/`goals_scored`/`assists`/`clean_sheets` (no match
# outcome fields), and the whole defensive-contribution family
# (`defensive_contribution`, `tackles`, `clearances_blocks_interceptions`,
# `recoveries` — consistent with DC not existing as an FPL scoring category
# before 2025/26, blueprint §11). `required_fields` below is the verified
# intersection across all three seasons — none of the 2025-26+-only fields
# are required, same drift-handling philosophy as vaastav's opta_code
# (present-but-optional, not null-padded to fake a uniform shape).
# 2024-2025 ALSO uses a different directory layout entirely:
# `data/2024-2025/playerstats/playerstats.csv` (nested), not
# `data/2024-2025/playerstats.csv` (flat, 2025-2026/2026-2027's shape) —
# `providers/olbauday.py` tries flat first, falls back to nested on a 404,
# rather than hardcoding a season->layout lookup table.
#
# `gameweek_summaries.csv` DOES NOT EXIST AT ALL for 2024-2025 (verified —
# 404, and that season's directory listing has no events/deadlines file of
# any kind: only `matches/`, `playermatchstats/`, `players/`,
# `playerstats/`, `teams/` subdirectories, a structurally different
# generation of this repo's scraper). This is the direct cause of a real
# coverage gap — see providers/olbauday.py's module docstring and
# docs/wiki/provider-framework.md §13's corrected record: `player.
# attributes@gameweek`'s `observed_at` is IMPUTED from `gameweek_summaries
# .csv`'s own `deadline_time` column (verified correct per-row), never
# from that file's `snapshot_time` column (verified live to be a stale
# FILE-GENERATION stamp, constant across a whole season's rows — NOT an
# observation timestamp; treating it as one was this capability's original,
# corrected bug, blueprint §12.5). For 2024-2025 that deadline source
# simply does not exist — `PLAYER_ATTRIBUTES_GAMEWEEK` therefore CANNOT be
# fetched for 2024-2025 at all (raises `ProviderError`), even though
# `playerstats.csv` itself is fetchable for that season. This is a "no
# in-archive deadline source" gap, not a "no snapshot" one; other in-repo
# sources (vaastav `kickoff_time`, FPL `events`, PL API fixtures) could
# supply a deadline for 2024-2025 but wiring one in is a deferred decision,
# not built here. A capability that is technically downloadable but cannot
# be honestly timestamped is treated as absent, not ingested with a
# fabricated `observed_at`.
PLAYER_ATTRIBUTES_GAMEWEEK = CapabilityKey("player", "attributes", "gameweek")
GAMEWEEK_FIELD_SUMMARY_GAMEWEEK = CapabilityKey("gameweek", "field_summary", "gameweek")

CANONICAL_SCHEMAS[PLAYER_ATTRIBUTES_GAMEWEEK] = FactTableSchema(
    capability=PLAYER_ATTRIBUTES_GAMEWEEK,
    entity_key=("season", "gw", "id"),
    required_fields=(
        "season",
        "gw",
        "id",
        "status",
        "chance_of_playing_next_round",
        "selected_by_percent",
        "now_cost",
        "cost_change_event",
        "form",
        "ep_next",
        "ep_this",
        "expected_goals",
        "expected_assists",
        "expected_goal_involvements",
        "expected_goals_conceded",
        "penalties_order",
        "direct_freekicks_order",
        "corners_and_indirect_freekicks_order",
        "set_piece_threat",
        "total_points",
        "bonus",
        "bps",
        "observed_at_source",
        "observed_at_imputed",
    ),
    description=(
        "olbauday archive data/{season}/playerstats.csv (data/{season}/"
        "playerstats/playerstats.csv for 2024-2025 — see module comment "
        "above) — a POST-GAMEWEEK snapshot of that gameweek's own "
        "bootstrap-static 'elements' object (verified live, 2026-08-21: "
        "playerstats.csv's cumulative total_points at gw=N matches vaastav's "
        "per-gameweek total_points summed THROUGH gw=N, not through N-1 — "
        "32/32 player/gameweek comparisons agreed), ONE FILE PER SEASON "
        "covering every gameweek (`gw` column), unlike vaastav's "
        "one-file-per-gw layout; the adapter fetches the whole season file "
        "once (cached) and filters to the requested `gw`. `id` is that "
        "season's FPL element id (same convention as PLAYER_IDENTITY_"
        "SEASON's `id`, but this schema does NOT itself resolve identity — "
        "join against PLAYER_IDENTITY_SEASON/PLAYER_ATTRIBUTES_CURRENT for "
        "a name). `selected_by_percent` is a PERCENTAGE (e.g. 14.9), NOT "
        "vaastav's `selected` raw count — never compare the two directly. "
        "This is deliberately a SEPARATE capability from vaastav's "
        "`player.gameweek_stats@gameweek` (blueprint §12.1 — see this "
        "file's module docstring): that one is match PERFORMANCE, this one "
        "is an ATTRIBUTE SNAPSHOT (price, ownership%, injury flag, "
        "set-piece order) — the two must never be registered under one key "
        "or silently substituted for each other. `web_name`, `news`, "
        "`news_added`, `minutes`, `goals_scored`, `defensive_contribution`, "
        "`tackles`, `recoveries` and the rest of the 2025-26+-only columns "
        "are present-but-NOT-required (absent for 2024-2025, verified "
        "live) — same philosophy as vaastav's opta_code. "
        "`observed_at_source`/`observed_at_imputed` are the CLAUDE.md "
        "rule 3 label for this row's `observed_at`: it is IMPUTED (never a "
        "genuine per-row capture) from `gameweek_summaries.csv`'s "
        "`deadline_time` for gw+1 (or gw's own deadline plus a documented "
        "offset for a season's final gameweek) — see providers/olbauday.py "
        "for the full resolution chain and the live evidence that its raw "
        "`snapshot_time` column is a stale file-generation stamp, not an "
        "observation time, and must never be used as one."
    ),
)

CANONICAL_SCHEMAS[GAMEWEEK_FIELD_SUMMARY_GAMEWEEK] = FactTableSchema(
    capability=GAMEWEEK_FIELD_SUMMARY_GAMEWEEK,
    entity_key=("season", "id"),
    required_fields=(
        "season",
        "id",
        "name",
        "deadline_time",
        "finished",
        "average_entry_score",
        "chip_plays",
        "most_captained",
        "most_vice_captained",
        "ranked_count",
        "observed_at_source",
        "observed_at_imputed",
    ),
    description=(
        "olbauday archive data/{season}/gameweek_summaries.csv — the "
        "archived FPL `events[]` object, one row per gameweek per season "
        "(`id` is the gameweek/event number). Only verified to exist for "
        "2025-2026 and 2026-2027 (identical 29-column headers, both "
        "checked live) — 2024-2025 has NO file at this path at all "
        "(confirmed 404; that season's directory layout has no "
        "events/deadlines file anywhere), so this capability is simply "
        "unfetchable for 2024-2025, surfaced as the transport's own "
        "TransportError, not invented rows — a 'no in-archive deadline "
        "source' gap, same as PLAYER_ATTRIBUTES_GAMEWEEK's. "
        "**Corrected 2026-08-21**: this row's content (`average_entry_"
        "score`, `highest_score`, `chip_plays`, `most_captained`) is "
        "POST-GAMEWEEK — it cannot be known at gw N's own deadline, so "
        "`observed_at` is IMPUTED as gw N+1's `deadline_time` (or gw N's "
        "own deadline plus a documented offset for a season's final "
        "gameweek — see providers/olbauday.py's `_FINAL_GAMEWEEK_"
        "OBSERVED_AT_OFFSET`, 7 days), never this row's own `deadline_"
        "time` and never its `snapshot_time`. This capability's own raw "
        "`snapshot_time` column (present but deliberately NOT required "
        "here) is a STALE FILE-GENERATION STAMP, not an observation time: "
        "verified live, it is CONSTANT across every one of the ~38 rows "
        "in a season's file (2025-2026: all 38 rows stamp "
        "2025-08-17T04:46:20Z even though GW38, deadline 2026-05-24, "
        "already carries `finished=True` and a real `average_entry_"
        "score` — the file was regenerated long after the season ended "
        "and kept its original stamp) — using it as `observed_at` was "
        "this capability's original, corrected bug: with entity key "
        "(season, id) treating every gameweek as its own entity, a "
        "constant `observed_at` let `BitemporalStore.as_of()` at an EARLY "
        "gameweek's deadline return a LATE gameweek's row — real leakage, "
        "not a cosmetic imprecision (docs/wiki/provider-framework.md §13 "
        "has the full account and the corrected chip_plays/most_captained "
        "consequences for E8's EO backtest). `chip_plays`/`most_captained` "
        "are E8's only historical EO signals — imputing their "
        "`observed_at` at gw N+1's deadline is what keeps a backtest from "
        "seeing them before it decides."
    ),
)


# -- capability key vocabulary, E2b story 8 (The Odds API adapter) ---------
#
# Two capabilities served by providers/odds.py. Verified live against the
# real API (XL-Coder, 2026-08-21) against blueprint §3.3's claims — see
# providers/odds.py's module docstring for the full before/after and
# docs/wiki/provider-framework.md for the live record.
#
# `/historical/` is 401 on the free plan (blueprint §3.3), so odds get NO
# GrainPlan and NO backfill entry — there is nothing to backfill. Both
# capabilities are RAW market observations: no de-vig, no implied-
# probability column, no P(start) gating (blueprint §3.3.1 — that is a
# hard rule, not a style choice: a market price reaching the optimiser
# without passing through the minutes model already produced one wrong
# live recommendation on this project). Deriving anything from these rows
# (de-vig, normalisation against team goal expectation) is model-layer
# work, Phase 2 at the earliest.
#
# `player_shots_on_target` is DELIBERATELY DEFERRED (not registered) —
# another ~10 credits/gameweek on top of the ~12 these two capabilities
# already cost, and §3.3 names de-vigged anytime-goalscorer as the single
# market to keep if only one could be kept.
MATCH_ODDS_FIXTURE = CapabilityKey("match", "odds", "fixture")
PLAYER_GOAL_ODDS_FIXTURE = CapabilityKey("player", "goal_odds", "fixture")

CANONICAL_SCHEMAS[MATCH_ODDS_FIXTURE] = FactTableSchema(
    capability=MATCH_ODDS_FIXTURE,
    entity_key=("provider_event_id", "bookmaker_key", "market_key", "outcome_name", "outcome_point"),
    required_fields=(
        "season",
        "provider_event_id",
        "home_team_code",
        "away_team_code",
        "commence_time",
        "bookmaker_key",
        "bookmaker_title",
        "market_key",
        "market_last_update",
        "outcome_name",
        "outcome_price",
        "outcome_point",
    ),
    description=(
        "The Odds API /v4/sports/soccer_epl/odds, markets=h2h,totals, "
        "regions=uk — ONE call covers every fixture in the sport (cost = "
        "markets x regions = 2 credits total, not per fixture). LONG "
        "format, one row per (fixture, bookmaker, market, outcome) quote — "
        "same rationale as PL API's team.match_stats@match: the exact "
        "market/outcome set is not documented as stable (verified live: "
        "Betfair Exchange also returns an unrequested 'h2h_lay' market "
        "alongside the requested 'h2h'/'totals'; a fixed-width schema "
        "would either drop it or churn on the next surprise). RAW "
        "bookmaker prices only — no de-vig, no implied probability, "
        "nothing derived (blueprint §3.3.1: a market price must pass "
        "through the minutes model's P(start) before it reaches the "
        "optimiser; this table is upstream of that, not a substitute for "
        "it). `outcome_point` is the totals line (e.g. 2.5) and is NULL "
        "for h2h/h2h_lay outcomes — present as a column either way, so "
        "the entity key (which includes it) is well-defined for both "
        "market shapes. `provider_event_id` is The Odds API's own opaque "
        "event id, kept verbatim for traceability; `home_team_code`/"
        "`away_team_code` are FPL team codes, resolved from the API's own "
        "team-name strings (its own full-name convention, e.g. "
        "'Manchester City' — NOT FPL's short teams[].name, 'Man City') via "
        "an explicit, verified alias table in providers/odds.py for the "
        "handful of clubs where the two disagree; an unresolved team name "
        "raises (blueprint §12.5), never silently drops. BITEMPORAL: "
        "`observed_at` is fetch time (genuinely `now()` here — unlike an "
        "archive's imputed `observed_at`, this row IS an observation made "
        "at that instant); `valid_at` is the EARLIEST `market_last_update` "
        "across the whole batch (the convention `fplai.backfill._valid_at_"
        "for` uses for fixtures — a batch spanning several bookmakers' own "
        "update instants is anchored at the earliest, the conservative "
        "direction). NO GrainPlan / NO backfill entry: /historical/ is 401 "
        "on the free plan (blueprint §3.3) — there is nothing to backfill, "
        "this is a live-only overlay that can only ever be forward-tested."
    ),
)

CANONICAL_SCHEMAS[PLAYER_GOAL_ODDS_FIXTURE] = FactTableSchema(
    capability=PLAYER_GOAL_ODDS_FIXTURE,
    # entity_key uses player_name_raw, NOT player_element_id — the
    # §12.5 re-fetchability exception (blueprint, amended 2026-08-21)
    # means player_element_id is now NULLABLE (unresolved rows are
    # preserved, not discarded), so it cannot be a key component: two
    # different unresolved outcomes from the same bookmaker would both
    # carry a NULL id and collide. player_name_raw is always present
    # (the raw API string) and is what the source actually reports as
    # distinguishing one outcome from another, resolved or not.
    entity_key=("provider_event_id", "bookmaker_key", "market_key", "player_name_raw", "outcome_name"),
    required_fields=(
        "season",
        "provider_event_id",
        "home_team_code",
        "away_team_code",
        "commence_time",
        "player_element_id",
        "player_name_raw",
        "identity_resolved",
        "bookmaker_key",
        "bookmaker_title",
        "market_key",
        "market_last_update",
        "outcome_name",
        "outcome_price",
    ),
    description=(
        "The Odds API /v4/sports/soccer_epl/events/{event_id}/odds, "
        "markets=player_goal_scorer_anytime, regions=uk — ONE call PER "
        "FIXTURE (~1 credit each; blueprint §3.3 estimated ~10/gameweek "
        "across 10 fixtures). LONG format, one row per (fixture, "
        "bookmaker, player) quote. Book coverage is NOT constant per "
        "fixture — verified live 2026-08-21: 5 books/212 outcomes was "
        "blueprint §3.3's observed MAXIMUM for one sampled fixture, not a "
        "guarantee; a newly-promoted club's fixture returned only 3 books "
        "(107 outcomes total) the same day. `outcome_name` is 'Yes' on "
        "every outcome observed live — anytime-goalscorer books quote "
        "only the Yes side (blueprint §3.3's de-vig note), kept as a "
        "column rather than assumed, in case a book ever quotes 'No'. "
        "RAW bookmaker prices only, same §3.3.1 discipline as match.odds@"
        "fixture — no de-vig, no P(start) gating, nothing derived. "
        "`player_element_id` is the FPL element id, resolved from the "
        "API's raw `outcome.description` player-name string (NOT "
        "`outcome.name`, which is always 'Yes') — see providers/odds.py "
        "for the full, verified resolution chain (normalised full-name "
        "match first, a normalised-surname fallback restricted to the "
        "fixture's two squads second) and its live match-rate finding. "
        "**§12.5's re-fetchability exception applies to THIS capability "
        "ONLY** (blueprint, amended 2026-08-21, after this adapter's "
        "first live capture lost 9 of 10 fixtures to one unresolvable "
        "name under the old all-or-nothing rule): an unresolved player "
        "name is PRESERVED, never discarded — `player_element_id` is "
        "NULL, `identity_resolved` is `false`, and `player_name_raw` "
        "keeps the API's raw string verbatim, so identity can be "
        "repaired OFFLINE from the stored string with no re-fetch, which "
        "is exactly what this live-only source cannot offer once a "
        "fixture kicks off (blueprint §3.3, /historical/ is 401 on the "
        "free plan). `match.odds@fixture` deliberately does NOT get this "
        "exception — team-name resolution is a fully separate, far more "
        "robust join (20/20 live-verified) and still raises all-or-"
        "nothing for the whole batch on any miss, per §12.5's original "
        "rule. Unresolved rows are UNUSABLE BY DEFAULT: `player_element_"
        "id` is a genuine nullable int column, so an ordinary equi-join "
        "against `elements` on `player_element_id == id` excludes them "
        "structurally (SQL/Polars NULL never equals anything) — a "
        "consumer must explicitly filter `identity_resolved == true` (or "
        "handle nulls itself) to see them at all; nothing here lets a "
        "null id quietly participate in a join. Bitemporal convention "
        "identical to match.odds@fixture (see that schema's description) "
        "— `observed_at` is fetch-time now(), `valid_at` is the earliest "
        "`market_last_update` in the batch. NO GrainPlan / NO backfill "
        "entry, same reason as match.odds@fixture: /historical/ is 401 "
        "on the free plan."
    ),
)


# -- operational heartbeat, session s003 (PROGRESS.md E2: "heartbeat row so
#    'nothing changed' != 'scheduler was down'") ----------------------------
#
# A genuine OBSERVATION about a job's own execution, not a derived/modelled
# fact — is_modelled stays False and the dataset is NOT namespaced under
# DERIVED_DATASET_PREFIX. It exists because `store.write(...,
# skip_if_unchanged=True)` (the correct behaviour for every real dataset —
# a quiet 30 minutes should not bloat the store) makes "no new row" ambiguous
# between "nothing changed" and "the scheduler never ran". Demonstrated live
# 2026-08-22: the newest `events` row was ~11h old against a 30-min cadence,
# indistinguishable from an outage without Task Scheduler's own LastRunTime
# (not queryable, not bitemporal, not part of this store).
#
# GRAIN: this is an EVENT STREAM (one row per (job run, thing that job
# touched)), not entity state to collapse — the same distinction
# MANAGER_PICKS_SELECTION_GAMEWEEK's module comment makes for `event`, and
# PLAYER_GAMEWEEK_STATS_GAMEWEEK's for `fixture`: the temporal/disambiguating
# column MUST be part of the entity key, or `as_of()`'s state-collapse would
# silently merge every run this job has ever made into one row per
# `target_dataset`, which defeats the entire point (a monitoring tool that
# can only ever see the LATEST run cannot compute a gap between consecutive
# runs). `entity_key = (job, run_ts, target_dataset)`:
#   - `job`              which script/cron this row came from ("snapshot_
#                        bootstrap" today; present so `scripts/snapshot_
#                        odds.py` and `scripts/sample_picks.py` can write
#                        into this SAME dataset later, unwired, no schema
#                        change — out of this story's scope to wire up).
#   - `run_ts`           this run's own timestamp, shared by every row the
#                        run writes (one heartbeat batch = one run). A
#                        domain column, deliberately distinct from the
#                        store's own `observed_at`/`valid_at` metadata
#                        (RESERVED_COLUMNS) — required_fields may not
#                        reference a reserved column (schema.py's own
#                        FactTableSchema convention throughout this file),
#                        and an event-stream entity key must be resolvable
#                        from the PAYLOAD itself, the same posture every
#                        other capability in this file already takes.
#   - `target_dataset`   which downstream dataset (e.g. "elements") this
#                        row's outcome describes — named `target_dataset`,
#                        not `dataset`, specifically to avoid being read as
#                        THIS row's own storage dataset (which is always
#                        "heartbeat" — the `dataset` argument to
#                        `store.write()` itself, a different thing).
# UNIQUENESS WITHIN ONE BATCH (CLAUDE.md lesson 2 — three bugs of this class
# already: picks omitting `event`, vaastav_player_gameweek_stats omitting
# `fixture`, olbauday's stale `snapshot_time`): one run of `snapshot_
# bootstrap.py` writes exactly one heartbeat row per entry in its own
# `DATASET_CAPABILITIES` list (6 today) — `job` and `run_ts` are constant
# across the whole batch, so `target_dataset` alone must be, and is,
# distinct per row within that batch; verified by
# `tests/test_snapshot_bootstrap.py::test_heartbeat_rows_have_a_unique_
# entity_key_within_one_run`, not merely asserted here.
#
# VALUE FIELDS: `outcome` ("written" / "skipped_unchanged" / "failed" —
# never a boolean, so a future fourth state doesn't need a schema change),
# `payload_hash` (that dataset's `WriteResult.content_hash` for this run —
# present even when `outcome == "skipped_unchanged"`, since the store
# computes the hash before deciding whether to skip; null when `outcome ==
# "failed"`, nothing to hash), `n_rows`, `error` (free text, null on
# success) — enough that a reader can reconstruct WHAT THE JOB SAW, not
# merely that it woke up (brief's own requirement).
JOB_HEARTBEAT_RUN = CapabilityKey("job", "heartbeat", "run")

CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN] = FactTableSchema(
    capability=JOB_HEARTBEAT_RUN,
    entity_key=("job", "run_ts", "target_dataset"),
    required_fields=("job", "run_ts", "target_dataset", "outcome", "payload_hash", "n_rows", "error"),
    description=(
        "Operational self-observation, written by every ingest job's own "
        "run — session s003, PROGRESS.md E2. NOT namespaced under "
        "DERIVED_DATASET_PREFIX and is_modelled=False: this is a genuine "
        "observation ('this job ran at run_ts and saw these payload "
        "hashes'), never a statistical inference, so it must not be held "
        "to §12.2's derived-provenance contract (calibration_reference "
        "etc. would be meaningless here) and must remain queryable as a "
        "real observation. MUST be written with skip_if_unchanged=False "
        "on every run, unconditionally — see scripts/snapshot_bootstrap.py's "
        "write_heartbeat() and its own docstring for how that is enforced "
        "structurally (a literal kwarg, checked by a dedicated regression "
        "test) rather than left to caller discipline, and for how a "
        "failure writing this dataset is guaranteed not to take down the "
        "job's real ownership-capture writes (a monitoring feature must "
        "never take down the thing it monitors, CLAUDE.md's stated "
        "constraint for this exact story)."
    ),
)


# -- game.dc_threshold_observation@gameweek (session s004, PROGRESS.md E5) --
#
# One row per (season, group, gameweek) DC-threshold pin ATTEMPT, written by
# scripts/pin_dc_thresholds.py once a gameweek has settled.
#
# OBSERVED, NOT DERIVED — Architect ruling 2026-08-27, and the same call the
# JOB_HEARTBEAT_RUN block above makes for the same reason. A pin is a
# deterministic bounds-meeting computation over real (count, points) pairs
# read from event/{gw}/live/: max(count | DC not awarded) + 1 == min(count |
# DC awarded). There is no fitted parameter, no residual and no training
# window, so §12.2's derived-provenance contract (`calibration_reference`
# and friends) would be meaningless here, and the fact must stay queryable
# as what it is — an observation of FPL's actual scoring rule, not a model's
# opinion about it.
#
# WHY IT IS REGISTERED AT ALL: tests/test_store_invariants.py scans every
# real on-disk dataset directory and requires a declared entity key, and it
# is right to — an unregistered dataset makes as_of()/effective_at() raise
# rather than resolve. The alternative (leave it unregistered and hand-roll
# every read) was taken as an interim measure while tests/test_schemas.py
# was out of scope for the implementing agent; it is closed here.
#
# `count_threshold` is NULLABLE and that is load-bearing: null means the
# attempt was inconclusive (a gap between the bounds) or contradictory (the
# same count seen with both outcomes). Those rows are written, never
# omitted — a gameweek that failed to pin is evidence about the gameweek,
# and dropping it would leave the record silently flattering.
GAME_DC_THRESHOLD_OBSERVATION_GAMEWEEK = CapabilityKey("game", "dc_threshold_observation", "gameweek")

CANONICAL_SCHEMAS[GAME_DC_THRESHOLD_OBSERVATION_GAMEWEEK] = FactTableSchema(
    capability=GAME_DC_THRESHOLD_OBSERVATION_GAMEWEEK,
    entity_key=("season", "group", "round"),
    required_fields=(
        "season",
        "group",
        "round",
        "lower_bound",
        "upper_bound",
        "count_threshold",
        "n_observations",
        "n_unattributable",
        "contradiction",
    ),
    is_modelled=False,
    description=(
        "One row per (season, group, gameweek) DC-threshold pin ATTEMPT, "
        "written by scripts/pin_dc_thresholds.py once that gameweek has "
        "settled. `count_threshold` is non-null only when the observed "
        "bounds met exactly (max(count, DC not awarded) + 1 == min(count, "
        "awarded)); null means that gameweek's attempt was inconclusive (a "
        "gap between the bounds) or contradictory (the same count observed "
        "with both outcomes) — recorded honestly either way, never "
        "omitted. is_modelled=False: a genuine observation of FPL's "
        "scoring rule from the live event/{gw}/live/ payload, never a "
        "statistical inference — see the block comment above and "
        "fplai.models.defensive_contribution's module docstring."
    ),
)


# -- dataset-name -> entity-key declaration (blueprint §3.2, decision 2026-08-20) --
#
# BitemporalStore.as_of()/observations() are keyed by the STORE dataset name
# (e.g. "elements", the Parquet subdirectory under data/store/) — a layout
# that predates the capability abstraction above. This table is the single
# place a dataset name resolves to the entity key that defines "one row of
# STATE" for `as_of()`. Built from CANONICAL_SCHEMAS so there is exactly one
# place an entity key is declared, never two that can drift apart.
#
# An empty tuple means "singleton" — the dataset has no per-row entity to
# collapse to; `as_of()` returns exactly one row: the latest observation of
# the whole table.
#
# A dataset NOT in this mapping has no declared key. `BitemporalStore.as_of()`
# raises rather than guessing one — see its docstring (blueprint §3.2: "a
# dataset with no declared key must make as_of raise, not guess").
_DATASET_TO_CAPABILITY: dict[str, CapabilityKey] = {
    "elements": PLAYER_ATTRIBUTES_CURRENT,
    "teams": TEAM_ATTRIBUTES_CURRENT,
    "events": GAMEWEEK_ATTRIBUTES_SEASON,
    "chips": CHIP_WINDOW_SEASON,
    "game_config": GAME_CONFIG_CURRENT,
    "game_settings": GAME_SETTINGS_CURRENT,
    "picks": MANAGER_PICKS_SELECTION_GAMEWEEK,
    # Blueprint §3.4 "Blocker 3", session s003 — different grain from
    # "picks" (see MANAGER_AUTOMATIC_SUBS_GAMEWEEK's own module-level and
    # schema-level comments), so its own dataset name, not a column tacked
    # onto "picks".
    "automatic_subs": MANAGER_AUTOMATIC_SUBS_GAMEWEEK,
    # E2b stories 6-7 — PL API adapter. "pl_"-prefixed dataset names so they
    # can never collide with the FPL-native datasets above even though both
    # live under the same data/store/ root.
    "pl_match_lineups": MATCH_LINEUPS_MATCH,
    "pl_match_substitutions": MATCH_SUBSTITUTIONS_MATCH,
    "pl_team_match_stats": TEAM_MATCH_STATS_MATCH,
    "pl_player_season_stats": PLAYER_SEASON_STATS_SEASON,
    "pl_match_fixtures": MATCH_FIXTURES_MATCHWEEK,
    # Session s004 — see the MATCH_OFFICIALS_MATCH block above.
    "pl_match_officials": MATCH_OFFICIALS_MATCH,
    # E2b story 10 — vaastav archive adapter. "vaastav_"-prefixed for the
    # same collision-avoidance reason as the "pl_" prefix above.
    "vaastav_player_gameweek_stats": PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    # Session s005 — FPL API becomes a SECOND provider of this SAME
    # capability (blueprint §12.6 swappability), sourced from `event/{gw}/
    # live/` instead of the vaastav archive. Deliberately a DIFFERENT
    # dataset name, not a write into "vaastav_player_gameweek_stats" — the
    # two providers cannot honestly satisfy identical row shapes (the FPL
    # live API has no historical price/ownership at all, so `selected`/
    # `value` are always NULL on this dataset's rows — see fplai.providers.
    # fpl's module docstring), and mixing two providers' rows under one
    # dataset name would make "which provider produced this row" a
    # property a caller has to reconstruct from other columns instead of
    # reading directly. `fplai.gameweek_stats.read_player_gameweek_stats`
    # is the capability-level reader that unions both datasets with
    # provenance preserved — see that module for the full design,
    # including the double-gameweek fixture-attribution limitation.
    "fpl_api_player_gameweek_stats": PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    "vaastav_player_identity": PLAYER_IDENTITY_SEASON,
    # E2b story 7b — team-identity half of the season-scoping fix.
    "vaastav_team_identity": TEAM_IDENTITY_SEASON,
    # E2b story 10b — olbauday archive adapter. "olbauday_"-prefixed for the
    # same collision-avoidance reason as the "pl_"/"vaastav_" prefixes above.
    "olbauday_player_attributes": PLAYER_ATTRIBUTES_GAMEWEEK,
    "olbauday_gameweek_field_summary": GAMEWEEK_FIELD_SUMMARY_GAMEWEEK,
    # E2b story 8 — The Odds API adapter. "odds_"-prefixed for the same
    # collision-avoidance reason as the "pl_"/"vaastav_"/"olbauday_" prefixes.
    "odds_match_odds": MATCH_ODDS_FIXTURE,
    "odds_player_goal_odds": PLAYER_GOAL_ODDS_FIXTURE,
    # Session s003 (PROGRESS.md E2) — operational heartbeat, not a provider
    # capability. See the JOB_HEARTBEAT_RUN block above for the full design.
    "heartbeat": JOB_HEARTBEAT_RUN,
    # Session s004 (PROGRESS.md E5) — DC threshold pins observed from
    # event/{gw}/live/. See the GAME_DC_THRESHOLD_OBSERVATION_GAMEWEEK block
    # above for why this is on the observed side of the partition.
    "dc_threshold_observations": GAME_DC_THRESHOLD_OBSERVATION_GAMEWEEK,
}

DATASET_ENTITY_KEYS: dict[str, tuple[str, ...]] = {
    dataset: CANONICAL_SCHEMAS[capability].entity_key
    for dataset, capability in _DATASET_TO_CAPABILITY.items()
}

# -- dataset-name -> declared valid-time column (blueprint §3.2, decision
#    2026-08-22, "Valid time is per ROW, not per batch") ---------------------
#
# Mirrors DATASET_ENTITY_KEYS immediately above, same construction (built
# once from CANONICAL_SCHEMAS, never hand-duplicated) — but only datasets
# whose FactTableSchema actually DECLARES a valid_time_column appear here at
# all. A dataset absent from this dict has none (ruling point 4: "a dataset
# that declares no valid-time column keeps today's behaviour") —
# `BitemporalStore.effective_at()` looks up this exact dict and RAISES for a
# dataset not present in it, rather than defaulting or guessing (same
# "raise, don't guess" posture `_resolve_entity_key` already uses for a
# dataset with no declared entity key).
#
# No derived capability declares one today (`register_derived_capability`
# does not currently accept a valid_time_column parameter — nothing derived
# has needed one yet), so, unlike DATASET_ENTITY_KEYS, nothing appends to
# this dict after this point; it is correct to build it once, statically,
# right here.
DATASET_VALID_TIME_COLUMNS: dict[str, tuple[str, str | None]] = {
    dataset: (CANONICAL_SCHEMAS[capability].valid_time_column, CANONICAL_SCHEMAS[capability].valid_time_format)
    for dataset, capability in _DATASET_TO_CAPABILITY.items()
    if CANONICAL_SCHEMAS[capability].valid_time_column is not None
}


# -- derived-capability framework — E2b story 9 (blueprint §12.2, CLAUDE.md
#    rule 3: "modelled data is labelled as modelled ... schema requirement,
#    not convention") -------------------------------------------------------
#
# A DERIVED capability is satisfied by computation over other capabilities —
# the DC estimator (§11: player season rate x team per-match total,
# calibrated against 2025-26's real defensive_contribution counts) is the
# first real consumer; the captaincy backcast (§3.4, Phase 5) is the second,
# not built here but this design must not preclude it.
#
# THE STRUCTURAL GUARANTEE, and why it holds without touching store.py (out
# of scope for this story — see docs/wiki/provider-framework.md's story-9
# record for the full argument): `BitemporalStore.as_of()`/`observations()`
# are keyed by DATASET NAME and glob ONLY that dataset's own directory on
# disk (`<base_path>/<dataset>/**/*.parquet`). A derived row that is never
# written under an OBSERVED capability's dataset name is therefore not
# merely unlikely but PHYSICALLY ABSENT from any read of that dataset — the
# same class of guarantee the odds adapter uses at a different layer (a null
# `player_element_id` cannot equi-join; a derived row cannot live in a
# directory it was never written to). `DERIVED_DATASET_PREFIX` is the
# namespace boundary: every derived capability's dataset name MUST start
# with it, enforced at REGISTRATION time by `register_derived_capability`
# below (not by convention — a dataset name that doesn't start with
# "derived_" cannot be registered as is_modelled=True at all, and the
# reverse: an is_modelled=False dataset name starting with "derived_" cannot
# be registered either, both checked in the validation loop just below).
#
# This started as registration-time-only enforcement, deliberately NOT
# reaching into `BitemporalStore.write()` (E2b story 9's original scope
# excluded store.py: "if your design genuinely requires a store change,
# stop and report before writing it"). The Architect then attacked exactly
# that gap live, 2026-08-21 — wrote a legitimate row to an observed
# dataset, then a forged `is_modelled=True` row to the SAME dataset
# through plain `store.write()`, and it was accepted — and ruled the store
# change in: registration-time-only enforcement means the guarantee reads
# "a derived fact cannot leak *if you use `write_derived`*", which is
# strictly weaker than the standard the odds adapter's null-id trick sets
# (there, the data itself enforces the rule; no code path can silently
# join an unresolved row, regardless of which function a caller uses).
# `BitemporalStore.write()` (`store.py`) now calls
# `_require_derived_naming_invariant` on every write, bidirectionally,
# independent of whether the caller goes through `fplai.derived.
# write_derived` — see that function's docstring in `store.py` for the
# full rationale, and `tests/test_store.py`'s `test_derived_provenance_
# columns_rejected_outside_derived_namespace` / `test_derived_dataset_
# missing_provenance_columns_is_rejected` for the attack reproduced as
# tests, proven to fail against the pre-fix code before the guard was
# restored. `fplai.derived.write_derived` remains the RECOMMENDED path
# (it also resolves the correct dataset automatically and stamps every
# provenance column itself, so a caller cannot even attempt a mismatch),
# but it is no longer the ONLY thing standing between a derived row and an
# observed dataset — the store enforces the same invariant independently
# now, for every writer, present and future.
DERIVED_DATASET_PREFIX = "derived_"

# Every derived row carries these four things, always, as STORED COLUMNS
# (not `FetchResult.meta`, which is discarded at write time — E2b story 10b
# nearly lost the olbauday `observed_at` imputation exactly this way, closed
# by promoting it to a required schema field; the same fix, applied here
# before there is a real consumer to lose it from):
#   - is_modelled            -- always True; stamped by write_derived, never
#                                caller-supplied (see that function).
#   - derived_from           -- JSON-encoded list of {capability, entity_key,
#                                note}, "enough to re-derive it" (story-9
#                                brief). fplai.derived.DerivationInput /
#                                encode_derivation_inputs build this value.
#   - calibration_reference  -- free text: what this batch was calibrated
#                                against (blueprint §11's DC constraint,
#                                generalised to any derived capability).
#   - calibration_residual_mean / calibration_residual_std -- the
#                                calibration residual CARRIED AS UNCERTAINTY,
#                                not discarded (§11, explicit). Two numbers
#                                is the minimum structured representation
#                                that satisfies CLAUDE.md rule 5 ("a scalar
#                                xPts at a module boundary is a design
#                                error") applied to a calibration correction
#                                rather than a prediction — a bare correction
#                                factor with no spread would be the same
#                                mistake. A capability that needs a richer
#                                residual (quantiles, a full empirical
#                                distribution) adds those as ADDITIONAL,
#                                capability-specific columns — the same
#                                present-but-optional escape hatch this file
#                                already uses for `value_raw` alongside
#                                `value` (team.match_stats@match,
#                                player.season_stats@season above).
DERIVED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "is_modelled",
    "derived_from",
    "calibration_reference",
    "calibration_residual_mean",
    "calibration_residual_std",
)


class DerivedCapabilityError(SchemaError):
    """Misuse of the derived-capability registration API: a dataset name
    that doesn't respect `DERIVED_DATASET_PREFIX`, or an attempt to
    register/reuse a capability key or dataset name that already exists."""


def _validate_derived_naming_invariant(dataset: str, capability: CapabilityKey) -> None:
    """The two-way naming check `register_derived_capability` enforces on
    every new entry, and that this module's own static
    `_DATASET_TO_CAPABILITY` table is checked against once at import time
    (immediately below) so a future direct edit to that table — bypassing
    `register_derived_capability` entirely — cannot silently violate the
    same invariant."""
    is_modelled = CANONICAL_SCHEMAS[capability].is_modelled
    dataset_is_derived_namespace = dataset.startswith(DERIVED_DATASET_PREFIX)
    if is_modelled and not dataset_is_derived_namespace:
        raise DerivedCapabilityError(
            f"{capability} is is_modelled=True but dataset {dataset!r} is not "
            f"namespaced under {DERIVED_DATASET_PREFIX!r} — this is the exact "
            "leak §12.2 exists to prevent: a derived fact reachable through an "
            "observed-looking dataset name."
        )
    if dataset_is_derived_namespace and not is_modelled:
        raise DerivedCapabilityError(
            f"dataset {dataset!r} is namespaced under {DERIVED_DATASET_PREFIX!r} "
            f"but {capability} is is_modelled=False — an OBSERVED capability may "
            "not be registered inside the derived namespace; that would let a "
            "derived-looking dataset actually answer for real observations."
        )


# Self-check: every capability registered by E2b stories 1-8 above is
# is_modelled=False and none of their dataset names start with
# DERIVED_DATASET_PREFIX. Verified true today; asserted here so it stays
# true — a future hand-edit to _DATASET_TO_CAPABILITY that violates it fails
# at import time, not silently.
for _dataset, _capability in _DATASET_TO_CAPABILITY.items():
    _validate_derived_naming_invariant(_dataset, _capability)


def capability_dataset(capability: CapabilityKey) -> str:
    """Reverse lookup: the dataset name a capability is stored under.
    Raises `SchemaError` if the capability isn't registered — same "fail
    loudly, never guess" convention as `store._resolve_entity_key`."""
    for dataset, registered_capability in _DATASET_TO_CAPABILITY.items():
        if registered_capability == capability:
            return dataset
    raise SchemaError(f"{capability} has no registered dataset — was it ever registered?")


# -- provenance partition (Architect ruling, 2026-08-22 — see "Phase 2, E5"
#    section below for the tension this closes) -----------------------------
#
# `CANONICAL_SCHEMAS`/`DATASET_ENTITY_KEYS` hold BOTH observed (E2b, is_modelled
# =False) and derived (§12.2, is_modelled=True) entries in one dict each —
# there was only ever one partition (E2b's) until team_strength.py became the
# second real derived-capability module. These four functions are the single
# place that partition is computed, so `tests/test_schemas.py`'s exact-set
# assertions (and any future caller) never hand-filter `CANONICAL_SCHEMAS`
# themselves and risk drifting from this definition.
def observed_capabilities() -> dict[CapabilityKey, FactTableSchema]:
    """Every registered capability that is NOT derived — i.e. what a real
    provider adapter actually observed. `is_modelled=False` is exactly this
    partition already (no new flag needed); this function exists so the
    partition has one name, not a hand-written filter at every call site."""
    return {k: v for k, v in CANONICAL_SCHEMAS.items() if not v.is_modelled}


def derived_capabilities() -> dict[CapabilityKey, FactTableSchema]:
    """The `is_modelled=True` partition — see `observed_capabilities()`."""
    return {k: v for k, v in CANONICAL_SCHEMAS.items() if v.is_modelled}


def observed_dataset_entity_keys() -> dict[str, tuple[str, ...]]:
    """`DATASET_ENTITY_KEYS`, restricted to observed-only dataset names —
    see `observed_capabilities()`."""
    return {
        dataset: key
        for dataset, key in DATASET_ENTITY_KEYS.items()
        if not CANONICAL_SCHEMAS[_DATASET_TO_CAPABILITY[dataset]].is_modelled
    }


def derived_dataset_entity_keys() -> dict[str, tuple[str, ...]]:
    """`DATASET_ENTITY_KEYS`, restricted to derived-only dataset names —
    see `derived_capabilities()`."""
    return {
        dataset: key
        for dataset, key in DATASET_ENTITY_KEYS.items()
        if CANONICAL_SCHEMAS[_DATASET_TO_CAPABILITY[dataset]].is_modelled
    }


def register_derived_capability(
    capability: CapabilityKey,
    *,
    entity_key: tuple[str, ...],
    value_fields: tuple[str, ...],
    dataset: str,
    description: str = "",
) -> FactTableSchema:
    """The ONLY sanctioned way to add a derived capability's schema.

    Mirrors exactly what every OBSERVED capability above does by hand (a
    module-level `CapabilityKey` + `CANONICAL_SCHEMAS[key] = FactTableSchema
    (...)` + an entry in `_DATASET_TO_CAPABILITY`), routed through one
    function so the naming/provenance rules are enforced instead of
    hand-copied per capability — the exact way a hand-copied rule drifts.

    Call once, at the derived-capability module's own import time (e.g. a
    future `fplai/derived_capabilities/dc_estimate.py` for the DC
    estimator, or the Phase 5 captaincy backcast) — this registers the
    capability process-wide, same lifetime as every other entry in
    `CANONICAL_SCHEMAS`.

    Enforces, before anything is registered:
    1. `dataset` must start with `DERIVED_DATASET_PREFIX` — the physical-
       separation half of blueprint §12.2's guarantee (see the module
       comment above `DERIVED_DATASET_PREFIX`).
    2. `capability` must not already be registered, observed or derived —
       a derived capability may never reuse or shadow an existing key
       (blueprint §12.1's whole point, applied to registration instead of
       provider selection).
    3. `dataset` must not already be registered under a different
       capability — keeps `CANONICAL_SCHEMAS` and `DATASET_ENTITY_KEYS`
       from drifting apart, this file's own stated invariant (see the
       module docstring's "dataset-name -> entity-key declaration" comment
       above `DATASET_ENTITY_KEYS`).
    4. `required_fields` always includes `DERIVED_PROVENANCE_FIELDS` — a
       derived schema that "forgot" `is_modelled`/`derived_from`/
       calibration is impossible to construct through this function;
       `FactTableSchema.validate()` then refuses any batch missing them,
       the same mechanism every other schema already uses.

    Returns the constructed, already-registered `FactTableSchema`.
    """
    if capability in CANONICAL_SCHEMAS:
        raise DerivedCapabilityError(
            f"{capability} is already registered ({CANONICAL_SCHEMAS[capability]}) — "
            "a derived capability may not reuse an existing capability key."
        )
    if dataset in DATASET_ENTITY_KEYS:
        raise DerivedCapabilityError(
            f"dataset {dataset!r} is already registered to "
            f"{_DATASET_TO_CAPABILITY[dataset]!r}; dataset names must be unique."
        )

    required_fields = tuple(
        dict.fromkeys((*entity_key, *value_fields, *DERIVED_PROVENANCE_FIELDS))
    )
    schema = FactTableSchema(
        capability=capability,
        entity_key=entity_key,
        required_fields=required_fields,
        is_modelled=True,
        description=description,
    )

    # Register provisionally, then run the same naming check the static
    # table is checked against at import time, so a caller gets the loud
    # DerivedCapabilityError instead of a half-registered capability.
    CANONICAL_SCHEMAS[capability] = schema
    try:
        _validate_derived_naming_invariant(dataset, capability)
    except DerivedCapabilityError:
        del CANONICAL_SCHEMAS[capability]
        raise
    _DATASET_TO_CAPABILITY[dataset] = capability
    DATASET_ENTITY_KEYS[dataset] = entity_key
    return schema


def unregister_derived_capability(capability: CapabilityKey) -> None:
    """TEST-SUPPORT ONLY. Reverses `register_derived_capability` so a test
    that registers a throwaway derived capability can restore
    `CANONICAL_SCHEMAS`/`DATASET_ENTITY_KEYS` to their prior state in a
    `finally`/fixture teardown, instead of leaking a test capability into
    the process-wide registry for the rest of the test session (which would
    fail the exhaustive-coverage assertions in `tests/test_schemas.py`:
    `test_canonical_schemas_cover_all_fpl_pl_vaastav_olbauday_and_odds_
    capabilities` and `test_dataset_entity_keys_covers_every_store_dataset_
    this_slice_writes` both assert an EXACT set of keys). Production derived
    capabilities are registered once at import time and never unregistered
    — this function exists for test isolation, not for real use."""
    dataset = capability_dataset(capability)
    del CANONICAL_SCHEMAS[capability]
    del _DATASET_TO_CAPABILITY[dataset]
    del DATASET_ENTITY_KEYS[dataset]


# -- Phase 2, E5: team strength (blueprint §4, §4.2, §4.3) -----------------
#
# `fplai.models.team_strength`'s Dixon-Coles model is the first real
# consumer of the derived-capability framework built above (E2b story 9).
# Only the CapabilityKey and dataset name are declared HERE, as inert
# constants — matching this file's existing vocabulary-declaration
# convention (every other capability key is a module-level constant here
# too). The actual `register_derived_capability(...)` call lives in
# `fplai.models.team_strength` itself and runs at THAT module's import time
# — per §15.4's stated intention ("registered once at import time, same
# lifetime as CANONICAL_SCHEMAS").
#
# **Resolved 2026-08-22 (Architect ruling, session s003) — see
# `docs/wiki/model-team-strength.md` §10 for the full account.** This
# section used to document a lazy-registration workaround (register only
# when a caller actually calls `write_team_strength`, register/unregister
# around each test) adopted to avoid corrupting `tests/test_schemas.py`'s
# exact-set assertions. That workaround is gone: it made
# `CANONICAL_SCHEMAS`/`DATASET_ENTITY_KEYS`'s contents depend on CALL
# HISTORY (had a write ever happened, in this process, yet?), which
# conflicts with CLAUDE.md rule 7 (deterministic, reproducible) — the same
# test suite could see a different registered set depending on incidental
# test-execution order, not just which files were collected.
#
# The actual resolution is neither "every future derived module repeats the
# workaround" nor "loosen the exact-set assertions to contains-at-least"
# (the second throws away the one thing the check exists to catch: an
# accidental *derived* dataset landing where an *observed* one is expected).
# It is a **partition, not a loosening**: `observed_capabilities()`/
# `derived_capabilities()`/`observed_dataset_entity_keys()`/
# `derived_dataset_entity_keys()` above split `CANONICAL_SCHEMAS`/
# `DATASET_ENTITY_KEYS` by `is_modelled`, and `tests/test_schemas.py` now
# asserts an exact set over EACH partition separately — the observed
# partition's assertion is exactly as strict as it always was, and the
# derived partition gets a NEW exact-set assertion nothing enforced before
# (a strengthening, not a weakening). Determinism is restored by import-time
# registration: the derived partition's exact-set test imports every model
# module whose derived capability it expects to see, so its result is a
# pure function of which modules were imported, not of what ran and in what
# order.
TEAM_STRENGTH_RATING_GAMEWEEK = CapabilityKey("team", "strength_rating", "gameweek")
TEAM_STRENGTH_RATING_DATASET = "derived_team_strength_rating"


# -- Phase 2, E5: minutes (blueprint §4.1, §4.3, §7.1, §12.2) --------------
#
# `fplai.models.minutes`'s three-state (START/SUB/UNUSED) + minute-band PMF
# model is the SECOND real derived-capability module, and the first to
# actually exercise the import-time registration this file's "Phase 2, E5"
# section above describes -- `_register_minutes_capability()` in that
# module runs unconditionally at ITS OWN import time, exactly like
# `fplai.models.team_strength`'s `_register_team_strength_capability()`.
# Only the CapabilityKey and dataset name live here, as inert constants —
# same vocabulary-declaration convention every capability in this file
# follows.
PLAYER_MINUTES_DISTRIBUTION_GAMEWEEK = CapabilityKey("player", "minutes_distribution", "gameweek")
PLAYER_MINUTES_DISTRIBUTION_DATASET = "derived_player_minutes_distribution"


# -- Phase 2, E5: defensive contribution (blueprint §4, §7.1, §11, §12.2) --
#
# `fplai.models.defensive_contribution`'s per-match CBIT/CBIRT count PMF
# (fitted Negative Binomial, blueprint §11's DC calibration constraint) is
# the THIRD real derived-capability module, same import-time-registration
# convention as team_strength/minutes above. Only the CapabilityKey and
# dataset name live here.
PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK = CapabilityKey(
    "player", "defensive_contribution_distribution", "gameweek"
)
PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_DATASET = "derived_player_defensive_contribution_distribution"


# -- Phase 2, E5: attacking involvement (blueprint §4, §4.3, §12.2) --------
#
# `fplai.models.attacking`'s player goal/assist SHARE-of-team-goals model
# (a Binomial regression composed at PREDICT time with a caller-supplied
# `team_strength.ScorelinePMF` goal marginal and a `MinutesPMF`-shaped
# minute-exposure mixture -- see that module's docstring) is the FOURTH
# real derived-capability module, same import-time-registration convention
# as team_strength/minutes/defensive_contribution above. Only the
# CapabilityKey and dataset name live here, as inert constants.
PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK = CapabilityKey(
    "player", "attacking_involvement_distribution", "gameweek"
)
PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_DATASET = "derived_player_attacking_involvement_distribution"


# -- Phase 2, E5: bonus / BPS (blueprint §4, §7.1, §12.2) ------------------
#
# `fplai.models.bonus`'s per-fixture BPS regression, composed at PREDICT
# time with a caller-supplied minutes exposure and coupled across a
# fixture's whole player pool via a seeded Monte Carlo simulation of the
# real (archive-verified, see that module's docstring) competition-rank
# award rule, is the FIFTH real derived-capability module, same
# import-time-registration convention as team_strength/minutes/defensive_
# contribution/attacking above. Only the CapabilityKey and dataset name
# live here, as inert constants.
PLAYER_BONUS_DISTRIBUTION_GAMEWEEK = CapabilityKey("player", "bonus_distribution", "gameweek")
PLAYER_BONUS_DISTRIBUTION_DATASET = "derived_player_bonus_distribution"


# -- Phase 2, E5: cards / discipline (blueprint §4, §7.1, §12.2) -----------
#
# `fplai.models.cards`'s per-fixture yellow/red discipline model (a
# multinomial-logit count-exposure regression, blueprint §4's "referee
# assignment is a real feature" -- MEASURED here, not deployed, because the
# store anchors match.officials@match at kickoff, ~1.5h AFTER the FPL
# deadline; see that module's docstring) is the SIXTH real derived-capability
# module, same import-time-registration convention as team_strength/minutes/
# defensive_contribution/attacking/bonus above. Only the CapabilityKey and
# dataset name live here, as inert constants.
PLAYER_CARDS_DISTRIBUTION_GAMEWEEK = CapabilityKey("player", "cards_distribution", "gameweek")
PLAYER_CARDS_DISTRIBUTION_DATASET = "derived_player_cards_distribution"


# -- Phase 3 prerequisite: goalkeeper saves (blueprint §4, §7.1, §12.2) ----
#
# `fplai.models.saves`'s per-fixture GK saves count model (a Negative-
# Binomial count regression, minutes exposure offset, opponent attacking
# strength CONSUMED from `fplai.models.team_strength`'s scoreline marginal
# by composition rather than rebuilt as a parallel team-quality feature —
# see that module's docstring) is the SEVENTH real derived-capability
# module, and the last gap in goalkeeper points (measured 2026-08-29:
# saves are 18.9% of all GK points; omitting them removes a systematic
# ~0.21 pts/app differential that always favours premium keepers on strong
# defences, docs/HANDOFF.md §3) — same import-time-registration convention
# as team_strength/minutes/defensive_contribution/attacking/bonus/cards
# above. Only the CapabilityKey and dataset name live here, as inert
# constants.
PLAYER_SAVES_DISTRIBUTION_GAMEWEEK = CapabilityKey("player", "saves_distribution", "gameweek")
PLAYER_SAVES_DISTRIBUTION_DATASET = "derived_player_saves_distribution"
