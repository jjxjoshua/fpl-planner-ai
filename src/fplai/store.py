"""Bitemporal, append-only Parquet store, queried through DuckDB.

This is the load-bearing design decision in the ingest spine (CLAUDE.md rule 2,
blueprint §3.2). Two rules follow directly from that section and are enforced
here, not left to caller discipline:

1. Every row carries `valid_at` (when the fact was true) AND `observed_at`
   (when we learned it). Nothing is ever updated in place — a correction is a
   new row with a later `observed_at`.
2. All time-travelling reads go through `as_of(...)`, which filters on
   `observed_at` AND collapses to STATE — one row per declared entity key,
   never a duplicated observation stream (blueprint §3.2, decision
   2026-08-20). The raw append-only stream is available, explicit opt-in,
   via `observations(...)`. There is deliberately no "give me the current
   state" convenience that omits a cutoff — see `latest()` docstring for the
   one exception and why it is safe.

Both timestamps must be timezone-aware. A naive datetime is rejected outright:
silently assuming UTC (or worse, local time) at a module boundary is exactly
the kind of thing that produces a leakage bug nobody notices.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import polars as pl

from fplai.schemas import (
    DATASET_ENTITY_KEYS,
    DATASET_VALID_TIME_COLUMNS,
    DERIVED_DATASET_PREFIX,
    DERIVED_PROVENANCE_FIELDS,
)

# duckdb's polars/arrow bridge (`.pl()` / `.arrow()`) pulls in pyarrow, which
# is not otherwise needed. Converting via fetchall()+description keeps the
# dependency set to requests+polars+duckdb only.

logger = logging.getLogger(__name__)


def _verify_tzdata_available() -> None:
    """Verify that tzdata is installed and ZoneInfo can resolve UTC.

    Polars timestamps carry timezone information and attempt to resolve the
    IANA timezone database when materialising tz-aware columns (e.g., calling
    .to_list() or .to_pandas()). On Windows, the system ships no tzdata; if
    the tzdata Python package is not installed, ZoneInfo("UTC") raises
    ZoneInfoNotFoundError, which polars converts to an uncatchable
    PanicException inside its Rust layer.

    This guard runs at import time to catch the problem early with a clear
    error message instead of letting it fail mysteriously during materialisation.
    """
    try:
        ZoneInfo("UTC")
    except Exception as exc:
        raise RuntimeError(
            "Timezone database (tzdata) is not available. "
            "This is required for fplai.store to read timestamp columns. "
            "Install it with: pip install tzdata (or uv sync if using uv). "
            "On Windows without system tzdata, this package is mandatory."
        ) from exc


_verify_tzdata_available()


def _duckdb_type_to_polars(duckdb_type_obj: object) -> pl.DataType:
    """Map a DuckDB type object to a Polars dtype.

    DuckDB type objects stringify to their type name (e.g., 'INTEGER',
    'VARCHAR'); we map these to Polars equivalents. NULL-only columns
    (which hold no values but carry a declared DuckDB type) are handled
    correctly.

    If a DuckDB type has no mapping, we fall back to Utf8 and log a warning.
    An unmapped type must not take down a query.
    """
    type_str = str(duckdb_type_obj)

    type_map = {
        "BIGINT": pl.Int64,
        "INTEGER": pl.Int32,
        "SMALLINT": pl.Int16,
        "TINYINT": pl.Int8,
        "UBIGINT": pl.UInt64,
        "UINTEGER": pl.UInt32,
        "USMALLINT": pl.UInt16,
        "UTINYINT": pl.UInt8,
        "DOUBLE": pl.Float64,
        "FLOAT": pl.Float32,
        "DECIMAL": pl.Float64,  # conservative fallback
        "VARCHAR": pl.Utf8,
        "TEXT": pl.Utf8,
        "STRING": pl.Utf8,
        "BOOLEAN": pl.Boolean,
        "BOOL": pl.Boolean,
        "DATE": pl.Date,
        "TIMESTAMP": pl.Datetime("us", "UTC"),
        "TIMESTAMP WITH TIME ZONE": pl.Datetime("us", "UTC"),
        "TIMESTAMPTZ": pl.Datetime("us", "UTC"),
        "TIME": pl.Time,
    }

    result_dtype = type_map.get(type_str)
    if result_dtype is None:
        logger.warning(
            f"DuckDB type {type_str!r} has no Polars mapping; falling back to Utf8"
        )
        return pl.Utf8
    return result_dtype


def _duckdb_result_to_polars_schema(
    result: duckdb.DuckDBPyConnection,
) -> dict[str, pl.DataType]:
    """Derive a {column_name: dtype} schema from DuckDB result metadata.

    This avoids Polars' default 100-row dtype inference, which silently fails
    when old parquet files (written before a schema extension like provider_id)
    coexist with new ones. Old files come back as NULL after union_by_name=true;
    if all 100 inference rows are NULL, the column gets typed Null; the first
    actual string value then raises "could not append value ... to the builder".

    Instead, we derive the schema explicitly from DuckDB's result.description
    metadata, which exposes column types directly.
    """
    schema = {}
    for col_desc in result.description:
        col_name = col_desc[0]
        col_type_obj = col_desc[1]  # Type object at index 1
        schema[col_name] = _duckdb_type_to_polars(col_type_obj)
    return schema


def _fetch_polars(result: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Fetch a DuckDB result into a Polars DataFrame, preserving dtypes.

    Uses DuckDB's result metadata to derive the schema instead of relying on
    Polars' 100-row dtype inference. This prevents silent type mismatches when
    old and new parquet batches coexist (e.g., NULL-only inference -> Null type,
    then first actual string value raises).
    """
    rows = result.fetchall()
    schema = _duckdb_result_to_polars_schema(result)

    if not rows:
        # Empty result: return with correct dtypes, not inferred Null columns.
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema, orient="row")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE_PATH = _PROJECT_ROOT / "data" / "store"

RESERVED_COLUMNS = {
    "observed_at",
    "valid_at",
    "source",
    "content_hash",
    "batch_id",
    "provider_id",
    "capability",
    "endpoint",
}


class BitemporalError(ValueError):
    """Raised on misuse that would silently corrupt bitemporal semantics —
    e.g. a naive datetime, or a payload that collides with a reserved
    metadata column."""


def _require_utc(name: str, value: datetime) -> datetime:
    """Validate that `value` is timezone-aware, then normalise to naive UTC
    for storage. Rejecting naive input at the boundary is the actual safety
    property — silently assuming UTC (or local time) is exactly how
    present-day-vs-historical leakage creeps in unnoticed. Parquet/DuckDB
    TIMESTAMP columns are naive; by convention every value here is UTC, never
    anything else, so naive-UTC in storage loses no information as long as
    this function is the only path in."""
    if value.tzinfo is None:
        raise BitemporalError(
            f"{name} must be timezone-aware (got a naive datetime). "
            "Naive timestamps are how present-day-vs-historical leakage happens "
            "by accident — pass an explicit UTC datetime."
        )
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _quote_ident(name: str) -> str:
    """Quote a COLUMN identifier for interpolation into generated SQL.

    Entity-key column names reach `PARTITION BY` / `ORDER BY` as bare
    identifiers, so any name DuckDB reserves is a parser error rather than
    a wrong answer — found 2026-08-27 when `dc_threshold_observations` was
    registered with the entity key `(season, group, round)` and `as_of()`
    emitted `PARTITION BY season, group, round`: `Parser Error: syntax
    error at or near "group"`. The dataset was fine and the store was not.
    Doubling embedded quotes is the SQL-standard escape, the same posture
    `_sql_quote` already takes for string literals.
    """
    return '"' + name.replace('"', '""') + '"'


def _sql_quote(value: str) -> str:
    """Quote `value` as a DuckDB SQL string literal. Used only for values
    this module itself controls (a `FactTableSchema.valid_time_format`
    string, declared in `fplai.schemas`, never caller input) — escaping is
    still applied defensively rather than assumed safe."""
    return "'" + value.replace("'", "''") + "'"


def _resolve_valid_time_column(dataset: str) -> tuple[str, str | None]:
    """Look up `dataset`'s declared valid-time column (and optional
    `strptime` parse format) for `BitemporalStore.effective_at()` —
    blueprint §3.2, decision 2026-08-22. Raises rather than guessing, same
    posture as `_resolve_entity_key`: a dataset that never declared a
    per-row valid time has no business being queried through a primitive
    that resolves on one.

    Also refuses a declared column that COLLIDES with this store's own
    reserved bitemporal metadata (`RESERVED_COLUMNS`, e.g. `valid_at`
    itself) — attacked directly in `tests/test_store.py`: a schema that
    (accidentally or otherwise) declared `valid_time_column="valid_at"`
    would make `effective_at()` silently read the store's own write-time-
    stamped, per-batch-constant `valid_at` metadata column instead of a
    real per-row domain fact — exactly the "queryable through the wrong
    primitive by accident" failure blueprint §3.2 ruling point 4 forbids,
    and exactly the kind of thing ruling point 5 says nothing may depend
    on. No `FactTableSchema` in this codebase does this today (verified:
    none of `CANONICAL_SCHEMAS`'s entries declare a `valid_time_column` in
    `RESERVED_COLUMNS`) — this is a structural guard against a future one,
    not a fix for an existing bug.
    """
    if dataset not in DATASET_VALID_TIME_COLUMNS:
        raise BitemporalError(
            f"dataset {dataset!r} declares no valid-time column (blueprint §3.2 ruling "
            "point 4: 'a dataset that declares no valid-time column keeps today's "
            "behaviour') — effective_at() cannot resolve a domain valid-time state for "
            "it. Use as_of() (state resolved on OBSERVED time) or observations() (the "
            "raw stream) instead. If this dataset genuinely carries a per-row domain "
            "valid time, declare it via valid_time_column on its FactTableSchema in "
            "fplai.schemas."
        )
    column, fmt = DATASET_VALID_TIME_COLUMNS[dataset]
    if column in RESERVED_COLUMNS:
        raise BitemporalError(
            f"dataset {dataset!r} declares valid_time_column={column!r}, which collides "
            f"with this store's own reserved bitemporal metadata columns {sorted(RESERVED_COLUMNS)} "
            "— a domain valid-time column may never share a name with the store's own "
            "'valid_at'/'observed_at' stamps (blueprint §3.2 ruling point 5: this store's "
            "own 'valid_at' on a bulk-ingested archive is a write-time-stamped constant, "
            "not this row's real valid time, and nothing may depend on it — declaring the "
            "same name here would let effective_at() silently read that meaningless "
            "constant instead of the real per-row valid time). Fix the FactTableSchema "
            "declaration in fplai.schemas."
        )
    return column, fmt


def _resolve_entity_key(dataset: str) -> tuple[str, ...]:
    """Look up the entity key `as_of()` must collapse `dataset` on
    (blueprint §3.2). Raises rather than guessing — see `fplai.schemas.
    DATASET_ENTITY_KEYS`, the single declared source of truth."""
    if dataset not in DATASET_ENTITY_KEYS:
        raise BitemporalError(
            f"dataset {dataset!r} has no declared entity key, so as_of() cannot "
            "collapse its observations to state (blueprint §3.2: 'a dataset with "
            "no declared key must make as_of raise, not guess'). Declare "
            f"{dataset!r} in fplai.schemas.DATASET_ENTITY_KEYS (empty tuple for a "
            "singleton dataset), or call observations() instead if you genuinely "
            "want the raw, undeduplicated stream."
        )
    return DATASET_ENTITY_KEYS[dataset]


def _require_derived_naming_invariant(dataset: str, df: pl.DataFrame) -> None:
    """Bidirectional validate-on-write guard for the derived-capability
    naming invariant (blueprint §12.2, E2b story 9). Added 2026-08-21 to
    close a real gap the Architect found by attack: `fplai.derived.
    write_derived` is the SANCTIONED path for a derived row, but nothing
    previously stopped an ordinary `store.write()` call from writing a
    forged `is_modelled` (or any other `DERIVED_PROVENANCE_FIELDS`) column
    straight into an OBSERVED dataset — the guarantee held only "if you use
    write_derived", which is weaker than the standard the odds adapter's
    null-id trick set (there, the data itself enforces the rule; no code
    path can silently join an unresolved row, whether or not a caller
    remembers a helper function). This makes `store.write()` itself
    enforce the same invariant `fplai.schemas.register_derived_capability`
    already enforces at registration time — a second, independent check at
    the point data actually reaches disk, in the same "raise rather than
    guess" spirit as `_resolve_entity_key` above.

    Both directions are checked:
    1. A payload carrying ANY `DERIVED_PROVENANCE_FIELDS` column, written
       to a dataset that is NOT namespaced under `DERIVED_DATASET_PREFIX`
       — the exact attack: an `is_modelled=True` row written straight into
       an observed dataset's name, bypassing `write_derived` entirely.
    2. A payload written to a `DERIVED_DATASET_PREFIX`-namespaced dataset
       that is MISSING any `DERIVED_PROVENANCE_FIELDS` column — the same
       failure wearing the opposite hat: an unlabelled row in a derived
       dataset is just as capable of silently reading back as though
       observed, the moment a consumer reads that dataset without itself
       checking `is_modelled` (`fplai.derived.read_derived` does, but
       nothing before this guard forced a caller to go through it).

    No migration risk: verified live against every dataset in
    `DATASET_ENTITY_KEYS` on the real store (`data/store/`), 2026-08-21 —
    none carries any `DERIVED_PROVENANCE_FIELDS` column today.
    """
    reserved_present = set(DERIVED_PROVENANCE_FIELDS) & set(df.columns)
    dataset_is_derived_namespace = dataset.startswith(DERIVED_DATASET_PREFIX)

    if reserved_present and not dataset_is_derived_namespace:
        raise BitemporalError(
            f"dataset {dataset!r} is not namespaced under "
            f"{DERIVED_DATASET_PREFIX!r} but the payload carries derived-"
            f"provenance column(s) {reserved_present} — a derived fact may "
            "only be written to a dataset in the derived namespace "
            "(blueprint §12.2). Use fplai.derived.write_derived, which "
            "resolves the correct dataset for a registered derived "
            "capability automatically rather than accepting a dataset "
            "name from the caller."
        )

    if dataset_is_derived_namespace:
        missing = set(DERIVED_PROVENANCE_FIELDS) - set(df.columns)
        if missing:
            raise BitemporalError(
                f"dataset {dataset!r} is namespaced under "
                f"{DERIVED_DATASET_PREFIX!r} but the payload is missing "
                f"derived-provenance column(s) {missing} — every row in a "
                "derived dataset must carry the full derived-provenance "
                "contract (blueprint §12.2), never an unlabelled row that "
                "could pass as observed. Use fplai.derived.write_derived, "
                "which stamps all of them itself."
            )


def _require_naive_temporal_columns(dataset: str, df: pl.DataFrame) -> None:
    """Structural backstop for the presence-not-dtype gap
    (`fplai.schemas.FactTableSchema.validate`, docs/wiki/provider-
    framework.md §14.6) — added 2026-08-21, same session as
    `_require_derived_naming_invariant` above, deliberately mirroring its
    shape. Every current provider's `fetch()` already calls
    `CANONICAL_SCHEMAS[...].validate(df)` before returning a `FetchResult`
    (and `fplai.derived.write_derived` calls it again on the enriched
    derived row), which is where a tz-aware column is caught today. But
    "the sanctioned path validates first" is exactly the caller-discipline
    guarantee this project already ruled insufficient once this session,
    for a materially similar reason: `_require_derived_naming_invariant`
    exists because a plain `store.write()` call was demonstrated to accept
    a forged `is_modelled=True` row that bypassed `write_derived` entirely.
    A future caller that builds a DataFrame and calls `store.write()`
    directly — bypassing any provider adapter or `write_derived` — would
    get no protection at all without this. "The sanctioned path is safe"
    is not the standard; "no path is unsafe" is.

    Deliberately schema-agnostic, unlike `FactTableSchema.validate()`:
    this does NOT require `dataset` to be a registered capability, and
    does not re-run required-field presence or dtype-family checks. Many
    of this module's own tests write partial-shape payloads to registered
    dataset names on purpose (e.g. `write("elements", {"id": [1], "price":
    [50]}, ...)` — far short of `PLAYER_ATTRIBUTES_CURRENT`'s required
    fields) to exercise store mechanics in isolation from schema concerns;
    re-running a full `schema.validate()` here would reject those and
    reshape `write()`'s contract, out of this fix's scope (the brief that
    authorised this change was explicit: a full schema re-check belongs to
    the Architect's decision, not this guard's). This checks exactly one
    universal thing, independent of any registered schema: no column in
    `df` may be a timezone-AWARE `Datetime`. Every DATA column this store
    has ever held is naive UTC by convention; only `valid_at`/`observed_at`
    (bitemporal METADATA, added below by this function's caller, never
    part of the payload checked here) are genuinely tz-aware, and
    `_require_utc` normalises those itself before they reach disk.
    """
    offending = [
        name
        for name, dtype in df.schema.items()
        if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None
    ]
    if offending:
        raise BitemporalError(
            f"dataset {dataset!r}: column(s) {offending} are timezone-AWARE Datetime "
            "columns. Every DATA column in this store is naive UTC by convention; a "
            "tz-aware column persists as a real Parquet TIMESTAMP WITH TIME ZONE, "
            "which DuckDB cannot read back without pytz (not a project dependency) — "
            "the exact incident in docs/wiki/provider-framework.md §14.6. Normalise "
            "to naive UTC (`.astimezone(timezone.utc).replace(tzinfo=None)`) in the "
            "adapter before this payload reaches store.write()."
        )


def content_hash(df: pl.DataFrame) -> str:
    """Deterministic hash of a payload DataFrame's contents, independent of
    row order (fetch order is not meaningful) and column order. Used for
    idempotency (skip re-writing an unchanged snapshot) and for audit."""
    ordered = df.select(sorted(df.columns))
    sort_cols = ordered.columns
    if sort_cols:
        ordered = ordered.sort(by=sort_cols)
    canonical = ordered.write_ndjson()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WriteResult:
    written: bool
    dataset: str
    content_hash: str
    batch_id: str | None
    path: Path | None
    n_rows: int


class BitemporalStore:
    """Append-only Parquet store partitioned by dataset and observation date,
    queryable through DuckDB.

    Layout: `<base_path>/<dataset>/date=YYYY-MM-DD/<observed_at>__<batch_id8>.parquet`
    """

    def __init__(self, base_path: Path | str = DEFAULT_STORE_PATH) -> None:
        self.base_path = Path(base_path)

    # -- writing -----------------------------------------------------

    def write(
        self,
        dataset: str,
        df: pl.DataFrame,
        *,
        valid_at: datetime,
        observed_at: datetime,
        source: str,
        skip_if_unchanged: bool = True,
        provider_id: str | None = None,
        capability: str | None = None,
        endpoint: str | None = None,
    ) -> WriteResult:
        """Append `df` as one immutable batch. Never updates existing rows.

        If `skip_if_unchanged` (default) and the payload's content hash
        matches the dataset's most recently written batch, nothing is
        written — this is what makes re-running a snapshot script every
        30-60 minutes idempotent without silently discarding genuine
        changes (any actual diff always gets a new row).

        `provider_id` / `capability` / `endpoint` are the structured half
        of blueprint §12.3's mandatory provenance (`source` already carried
        an informal version of this, e.g. `"fpl_api:bootstrap-static"`, and
        keeps working unchanged). All three are OPTIONAL and default to
        NULL — existing call sites that only pass `source` are unaffected.
        This is a deliberate additive-only change: production parquet
        files already on disk (written before this column existed) do not
        have these columns at all, and reads across old + new batches only
        stay correct because `as_of()` reads with `union_by_name=True`
        (see below) — an old batch's missing columns come back as NULL
        rather than breaking the read.

        Also enforces the derived-capability naming invariant (blueprint
        §12.2, E2b story 9) BIDIRECTIONALLY — see
        `_require_derived_naming_invariant` for the full rationale: a
        payload carrying any `DERIVED_PROVENANCE_FIELDS` column may only
        be written to a `DERIVED_DATASET_PREFIX`-namespaced dataset, and a
        payload written to such a dataset must carry ALL of them. This is
        what makes "a derived fact cannot be returned as an observation"
        hold regardless of whether a caller goes through
        `fplai.derived.write_derived` — added 2026-08-21 after the
        Architect demonstrated `write()` would otherwise accept a forged
        `is_modelled=True` row into an ordinary observed dataset.

        Also enforces (`_require_naive_temporal_columns`, same session,
        closing docs/wiki/provider-framework.md §14.6's "presence, not
        dtype" gap): no column in `df` may be a timezone-AWARE `Datetime`.
        A schema-agnostic structural backstop, independent of whether the
        caller went through `FactTableSchema.validate()` first (every
        current provider does; this is the "no path is unsafe" guarantee
        applied a second time, not a replacement for the schema-level
        check, which additionally validates required-field presence and
        dtype family).
        """
        if df.is_empty():
            raise BitemporalError(f"refusing to write an empty batch for dataset {dataset!r}")

        collision = RESERVED_COLUMNS & set(df.columns)
        if collision:
            raise BitemporalError(
                f"payload columns collide with reserved bitemporal metadata: {collision}"
            )

        _require_derived_naming_invariant(dataset, df)
        _require_naive_temporal_columns(dataset, df)

        valid_at = _require_utc("valid_at", valid_at)
        observed_at = _require_utc("observed_at", observed_at)

        batch_hash = content_hash(df)

        if skip_if_unchanged:
            previous = self._latest_content_hash(dataset)
            if previous is not None and previous == batch_hash:
                return WriteResult(
                    written=False,
                    dataset=dataset,
                    content_hash=batch_hash,
                    batch_id=None,
                    path=None,
                    n_rows=df.height,
                )

        batch_id = uuid.uuid4().hex
        enriched = df.with_columns(
            pl.lit(valid_at).alias("valid_at"),
            pl.lit(observed_at).alias("observed_at"),
            pl.lit(source).alias("source"),
            pl.lit(batch_hash).alias("content_hash"),
            pl.lit(batch_id).alias("batch_id"),
            pl.lit(provider_id, dtype=pl.Utf8).alias("provider_id"),
            pl.lit(capability, dtype=pl.Utf8).alias("capability"),
            pl.lit(endpoint, dtype=pl.Utf8).alias("endpoint"),
        )

        date_partition = observed_at.strftime("%Y-%m-%d")
        observed_compact = observed_at.strftime("%Y%m%dT%H%M%S%f")
        out_dir = self.base_path / dataset / f"date={date_partition}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{observed_compact}__{batch_id[:8]}.parquet"
        enriched.write_parquet(out_path)

        return WriteResult(
            written=True,
            dataset=dataset,
            content_hash=batch_hash,
            batch_id=batch_id,
            path=out_path,
            n_rows=df.height,
        )

    # -- reading -------------------------------------------------------

    def _glob(self, dataset: str) -> str:
        return str(self.base_path / dataset / "**" / "*.parquet")

    def _dataset_exists(self, dataset: str) -> bool:
        return any((self.base_path / dataset).glob("**/*.parquet"))

    def _latest_content_hash(self, dataset: str) -> str | None:
        if not self._dataset_exists(dataset):
            return None
        con = duckdb.connect()
        try:
            row = con.execute(
                f"""
                SELECT content_hash
                FROM read_parquet('{self._glob(dataset)}', union_by_name=true)
                ORDER BY observed_at DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def as_of(self, dataset: str, as_of_ts: datetime) -> pl.DataFrame:
        """The bitemporal read primitive. Returns STATE at `as_of_ts`: one
        row per entity key (blueprint §3.2's `DATASET_ENTITY_KEYS`), the
        latest observation with `observed_at <= as_of_ts` — everything we
        knew by that instant, never anything learned later, and never more
        than one row per entity. This is what training and backtest queries
        MUST use instead of "current state".

        Before 2026-08-20 this method returned every matching row —
        `595 players x 13 snapshots = 7,735 rows` on the live store instead
        of the correct 595. That is now `observations()`, an explicit
        opt-in for the raw stream; `as_of()` never returns duplicated
        observations of the same entity again.

        Singleton datasets (`entity_key == ()`, e.g. `game_config`) collapse
        to exactly one row: the latest observation of the whole table.

        Ties (more than one row for the same entity key sharing the same
        `observed_at`) break on `batch_id` descending. This is arbitrary —
        `batch_id` carries no meaning beyond "a row's own batch" — but it is
        STABLE: `batch_id` is generated once at write time and persisted,
        so repeated queries against the same on-disk data always pick the
        same winner (blueprint CLAUDE.md rule 7, seeded/deterministic).

        Raises `BitemporalError` if `dataset` has no declared entity key —
        see `_resolve_entity_key` / `fplai.schemas.DATASET_ENTITY_KEYS`.
        Guessing a key is how this class of silent wrongness returns by
        another route. A dataset with literally no data yet returns an
        empty frame without raising — there's nothing to guess wrong about.

        Disappearing entities: an entity observed in an earlier batch that
        stops appearing in later ones (e.g. a player purged from a
        provider's payload) is NOT treated as deleted — this append-only
        store has no tombstone event. `as_of()` keeps returning its last
        known row indefinitely. See docs/wiki/provider-framework.md for the
        reasoning (inferring deletion from absence is its own class of
        silent-wrongness bug, and wrong outright for datasets like `picks`
        that are samples, not full snapshots).

        Row order (blueprint §7.2 gate-repair session, s003 — the same
        finding `effective_at()` already documents): a QUALIFY/window-
        function query has no output-order guarantee from DuckDB without an
        explicit outer `ORDER BY`. Attacked directly, not assumed: two
        temp stores holding the identical 30 entities, written as 30
        separate single-row batches in OPPOSITE id order, returned
        completely different `as_of()` row orders before this method sorted
        its output on the entity key — proof that row order was a function
        of write/scan order, not content. Singleton datasets (`entity_key
        == ()`) always collapse to exactly one row, so there is nothing to
        sort.
        """
        as_of_ts = _require_utc("as_of_ts", as_of_ts)
        if not self._dataset_exists(dataset):
            return pl.DataFrame()

        entity_key = _resolve_entity_key(dataset)

        con = duckdb.connect()
        try:
            base_sql = f"""
                SELECT *
                FROM read_parquet('{self._glob(dataset)}', union_by_name=true)
                WHERE observed_at <= ?
            """
            window = "ORDER BY observed_at DESC, batch_id DESC"
            if entity_key:
                partition = ", ".join(_quote_ident(c) for c in entity_key)
                window = f"PARTITION BY {partition} {window}"
            order_by = f"ORDER BY {', '.join(_quote_ident(c) for c in entity_key)}" if entity_key else ""
            sql = f"""
                SELECT * FROM ({base_sql})
                QUALIFY ROW_NUMBER() OVER ({window}) = 1
                {order_by}
            """
            result = con.execute(sql, [as_of_ts])
            df = _fetch_polars(result)
        finally:
            con.close()
        return df

    def effective_at(self, dataset: str, effective_ts: datetime) -> pl.DataFrame:
        """The VALID-time bitemporal read primitive (blueprint §3.2, "Valid
        time is per ROW, not per batch", decision 2026-08-22). Returns
        STATE effective at `effective_ts`: one row per entity key, the
        latest row whose DECLARED VALID-TIME COLUMN (`fplai.schemas.
        FactTableSchema.valid_time_column` — e.g. `vaastav_player_gameweek_
        stats.kickoff_time`, NEVER this store's own `valid_at` metadata
        column, see below) is STRICTLY BEFORE `effective_ts`.

        **This is NOT `as_of()` with a different column name — the two
        resolve different axes and use different boundary operators, by
        design, per the ruling's naming precedent (`latest()`/`as_of(now)`,
        `as_of()`/`observations()`: two operations that differ subtly get
        two names, so misuse reads as a bug rather than relying on caller
        discipline):**

        | | resolves | boundary | why |
        |---|---|---|---|
        | `as_of(dataset, t)` | OBSERVED time (`observed_at`) — "what did we know by t" | `<=` (inclusive) | knowing something exactly at t counts as knowing it by t |
        | `effective_at(dataset, t)` | domain VALID time (the declared column) — "what was true at t" | `<` (exclusive) | a fixture with `kickoff_time == t` has not been played AT the deadline instant itself — the boundary is strictly before, not at, kickoff (blueprint §3.2's own stated example) |

        Only usable for a dataset that DECLARES a valid-time column — raises
        `BitemporalError` otherwise (`_resolve_valid_time_column`), same
        "raise rather than guess" posture `_resolve_entity_key` already
        uses for a dataset with no declared entity key. A dataset that
        declares no valid-time column has no business being read through
        this primitive; use `as_of()` or `observations()` instead.

        **This reads the DECLARED domain column, e.g. `kickoff_time` — it
        NEVER reads this store's own `valid_at` metadata column**, and
        `_resolve_valid_time_column` structurally refuses a schema that
        would make it (see that function's docstring). Per blueprint §3.2
        ruling point 5, `valid_at` on a bulk-ingested archive remains a
        write-time-stamped CONSTANT — every row of one batch shares the one
        scalar `valid_at` passed to `write()` — until a follow-on story
        populates it per row; nothing may depend on it, including this
        primitive.

        A dataset's declared valid-time column may be stored as a STRING
        (verified live: `kickoff_time` is VARCHAR on disk, `"2019-08-10T11
        :30:00Z"`, not a native Datetime) rather than a native Datetime; the
        declared `valid_time_format`, if set, is the `strptime` format used
        to parse it before comparing. `None` means the column is already a
        native (naive UTC) Datetime.

        Collapse ordering, for an entity key with more than one row passing
        the `< effective_ts` filter (e.g. a genuinely re-scheduled fixture,
        or — verified live, 2026-08-22 — an exact duplicate row within one
        batch, `vaastav_player_gameweek_stats` element 100/391 in 2025-26,
        an upstream archive artefact, not a bug in this store): PRIMARY sort
        is the parsed valid-time column DESCENDING (pick the most recent
        version whose valid time does not exceed the cutoff — the direct
        valid-time analogue of `as_of()`'s own "most recent state" contract);
        SECONDARY is `observed_at` DESCENDING (among rows sharing the same
        valid time — e.g. a later correction to some other field of the
        same fact — prefer the one we learned about more recently, exactly
        `as_of()`'s own primary tie-break, not an arbitrary one); TERTIARY
        is `batch_id` DESCENDING, same final deterministic tiebreak `as_of()`
        uses (CLAUDE.md rule 7). Attacked directly in `tests/test_store.py`:
        two batches sharing an entity key and a valid time but disagreeing
        on some other column, written with different `observed_at`, must
        resolve to the LATER-`observed_at` version, never an arbitrary one.

        Singleton datasets and "no data yet" behave exactly as `as_of()`
        documents (collapse to one row; empty frame, no raise, for an absent
        dataset or a cutoff before every row's valid time — there is
        nothing to guess wrong about either way).
        """
        effective_ts = _require_utc("effective_ts", effective_ts)
        if not self._dataset_exists(dataset):
            return pl.DataFrame()

        valid_time_column, valid_time_format = _resolve_valid_time_column(dataset)
        entity_key = _resolve_entity_key(dataset)

        quoted_column = f'"{valid_time_column}"'
        if valid_time_format is not None:
            parsed_expr = f"strptime({quoted_column}, {_sql_quote(valid_time_format)})"
        else:
            parsed_expr = quoted_column

        con = duckdb.connect()
        try:
            base_sql = f"""
                SELECT *
                FROM read_parquet('{self._glob(dataset)}', union_by_name=true)
                WHERE {parsed_expr} < ?
            """
            window = f"ORDER BY {parsed_expr} DESC, observed_at DESC, batch_id DESC"
            if entity_key:
                partition = ", ".join(_quote_ident(c) for c in entity_key)
                window = f"PARTITION BY {partition} {window}"
            # Canonical final row order (CLAUDE.md rule 7, deterministic and
            # reproducible): a QUALIFY/window-function query has no
            # guaranteed output order from DuckDB without an explicit outer
            # ORDER BY — attacked directly, not assumed: two temp stores
            # holding logically-identical rows for a shared prefix of
            # entity keys returned those rows in DIFFERENT relative order
            # (`tests/test_minutes.py::
            # test_walk_forward_validate_cannot_see_a_folds_own_or_future_
            # outcomes` failed on exactly this before this ORDER BY was
            # added — two players' predictions swapped list positions
            # between two builds that differed only in a later round).
            # Sorting on the entity key ascending makes row order a pure
            # function of CONTENT, never of physical parquet scan order.
            order_by = f"ORDER BY {', '.join(_quote_ident(c) for c in entity_key)}" if entity_key else ""
            sql = f"""
                SELECT * FROM ({base_sql})
                QUALIFY ROW_NUMBER() OVER ({window}) = 1
                {order_by}
            """
            result = con.execute(sql, [effective_ts])
            df = _fetch_polars(result)
        finally:
            con.close()
        return df

    def observations(self, dataset: str, *, until: datetime) -> pl.DataFrame:
        """The raw append-only observation stream: every row of `dataset`
        with `observed_at <= until`, deliberately NOT collapsed to state —
        multiple rows per entity if it was observed more than once. This is
        what `as_of()` returned before 2026-08-20 (blueprint §3.2); it is
        now an explicit opt-in for audit and for modelling observation
        cadence, never the default a training/backtest query reaches for.

        Works for any dataset, including ones with no declared entity key —
        there is no state to collapse to, so nothing to guess.

        Row order (blueprint §7.2 gate-repair session, s003): this method
        previously issued no `ORDER BY` at all, so its row order was
        whatever DuckDB's multi-file glob scan happened to produce — a
        function of on-disk file/write order, not of content. Attacked
        directly: two temp stores holding the identical 30 rows, written as
        30 separate batches in OPPOSITE order, came back in OPPOSITE row
        order before this fix. `ORDER BY ALL` sorts every returned column
        left-to-right, so two datasets with the same rows always come back
        in the same order regardless of how many files they are split
        across or in what order those files were written — it works even
        for a dataset with no declared entity key (unlike `as_of()`, which
        can sort on the declared key), because it does not need one: it
        sorts on the row's own content instead.
        """
        until = _require_utc("until", until)
        if not self._dataset_exists(dataset):
            return pl.DataFrame()

        con = duckdb.connect()
        try:
            sql = f"""
                SELECT *
                FROM read_parquet('{self._glob(dataset)}', union_by_name=true)
                WHERE observed_at <= ?
                ORDER BY ALL
            """
            result = con.execute(sql, [until])
            df = _fetch_polars(result)
        finally:
            con.close()
        return df

    def latest(self, dataset: str) -> pl.DataFrame:
        """Sugar for `as_of(dataset, now)` — STATE as of this instant. Safe
        for OPERATIONAL use (e.g. "what's the current calibration check
        against selected_by_percent"). NEVER use this in a training or
        backtest path — pass an explicit historical deadline to `as_of`
        there instead. The name is deliberately different from `as_of` so a
        reviewer sees the difference at a glance.
        """
        return self.as_of(dataset, datetime.now(timezone.utc))
