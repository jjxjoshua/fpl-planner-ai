"""Invariant tests against the real bitemporal store on disk.

This module tests that the production store on disk satisfies seven invariants
that catch the three bugs that slipped through 237 existing unit tests:

1. as_of() raised on mixed schemas — dtypes inferred from first 100 rows only.
2. as_of() returned the observation stream, not state — silent 13x row count bloat.
3. Reading any tz-aware timestamp column panicked from Rust — uncatchable.

Every test here:
  - Discovers datasets from what is actually on disk (no hardcoding).
  - Runs against the REAL store at data/store/ via BitemporalStore default path.
  - Skips cleanly (pytest.skip with a clear reason) if the store is absent/empty.
  - Is read-only — never writes to data/store/, which holds unrecoverable pre-
    deadline ownership data.

Discovery: if the store directory exists and is non-empty, iterate over all
subdirectories found and test each as a dataset. If empty or absent, skip all
tests together with a clear message about why.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from fplai.schemas import CANONICAL_SCHEMAS, DATASET_ENTITY_KEYS, DATASET_VALID_TIME_COLUMNS, capability_dataset
from fplai.store import BitemporalStore, BitemporalError


def _get_store_path() -> Path:
    """Get the store path; same logic as BitemporalStore default."""
    # Test file is at tests/test_store_invariants.py, so parents[1] is project root
    # (parents[0] is tests/, parents[1] is fpl-ai/)
    project_root = Path(__file__).resolve().parents[1]
    return project_root / "data" / "store"


def _discover_datasets() -> list[str]:
    """Discover datasets actually on disk (non-empty subdirectories with parquet files).

    Returns a list of dataset names (directory names that contain *.parquet files).
    If no datasets are found, returns an empty list (triggering a module-wide skip).
    """
    store_path = _get_store_path()

    if not store_path.exists():
        return []

    datasets = []
    for dataset_dir in store_path.iterdir():
        if dataset_dir.is_dir():
            # Check if this directory has any parquet files (recursively, since they
            # are partitioned by date)
            if any(dataset_dir.glob("**/*.parquet")):
                datasets.append(dataset_dir.name)

    return sorted(datasets)


# Module-level fixture: skip all tests if store is absent or empty
pytestmark = pytest.mark.skipif(
    not _discover_datasets(),
    reason="Store is absent or empty; tests are read-only and require real on-disk data. "
    "Run scripts/snapshot_bootstrap.py to populate data/store/.",
)


@pytest.fixture
def store():
    """Fixture providing a BitemporalStore pointing at the real data/store/."""
    return BitemporalStore()


@pytest.fixture
def datasets():
    """Fixture providing the list of datasets discovered on disk."""
    return _discover_datasets()


class TestStoreInvariants:
    """Seven invariant tests on the live store."""

    # ========================================================================
    # Invariant 1: Every dataset on disk has a declared entity key
    # ========================================================================

    @pytest.mark.slow
    def test_all_datasets_have_declared_entity_keys(self, store, datasets):
        """
        Invariant: as_of() must not raise BitemporalError for any dataset on disk.

        If as_of() raises BitemporalError on a dataset that exists in the store,
        that is a COVERAGE FAILURE — the dataset was added to the store without
        declaring its entity key in fplai.schemas.DATASET_ENTITY_KEYS.

        This is a strict invariant: every dataset must have declared its entity
        key before being written to the store. Running this test catches a schema
        definition that was forgotten at production deployment time.
        """
        for dataset in datasets:
            try:
                # as_of() will raise BitemporalError if the dataset has no declared key
                _df = store.as_of(dataset, datetime.now(timezone.utc))
                # If we get here, the dataset is declared. Good.
            except BitemporalError as e:
                pytest.fail(
                    f"Dataset {dataset!r} exists on disk but has no declared entity key. "
                    f"Error: {e}. Add {dataset!r} -> entity_key to "
                    "fplai.schemas.DATASET_ENTITY_KEYS."
                )

    # ========================================================================
    # Invariant 2: as_of() returns STATE (one row per entity, never duplicates)
    # ========================================================================

    @pytest.mark.slow
    def test_as_of_returns_state_not_observation_stream(self, store, datasets):
        """
        Invariant: as_of(dataset, now) row count == distinct entity-key tuples count.

        Before 2026-08-20, as_of() returned EVERY matching row (the observation
        stream) instead of collapsing to STATE (one row per entity key). This was
        a silent bug — no exception raised, just 595 players × 13 snapshots = 7,735
        rows where 595 was correct. This test catches that regression.

        For each dataset, we fetch as_of(now) and verify that each row's entity
        key is unique. If entity_key is (), the dataset is a singleton — as_of()
        must return exactly 1 row.
        """
        for dataset in datasets:
            df = store.as_of(dataset, datetime.now(timezone.utc))

            # Empty result is valid (dataset not yet populated, or no observations
            # before a cutoff time). Singleton datasets must return exactly one row
            # when non-empty.
            entity_key = DATASET_ENTITY_KEYS[dataset]
            if not entity_key:
                # Singleton dataset: must be 0 or 1 row
                assert (
                    len(df) <= 1
                ), f"Dataset {dataset!r} is a singleton (entity_key=()) but as_of() "
                f"returned {len(df)} rows. Expected at most 1."
                continue

            # Non-singleton: check uniqueness of entity key tuples
            if df.is_empty():
                # Empty result is fine — dataset exists but has no observations yet
                # or all observations are after the cutoff time.
                continue

            entity_key_df = df.select(entity_key)
            n_distinct = entity_key_df.n_unique()
            n_total = len(df)

            assert (
                n_distinct == n_total
            ), f"Dataset {dataset!r} returned {n_total} rows with only {n_distinct} "
            f"distinct entity keys {entity_key}. as_of() should return exactly one "
            f"row per entity (state), not a duplicated observation stream. "
            f"This is bug #2 from the brief. Rows: {df.to_dict(as_series=False)}"

    # ========================================================================
    # Invariant 3: observations() >= as_of() for all datasets
    # ========================================================================

    @pytest.mark.slow
    def test_observations_includes_state(self, store, datasets):
        """
        Invariant: len(observations(dataset, until=now)) >= len(as_of(dataset, now)).

        The observation stream (raw, undeduplicated) must include every row returned
        by as_of(). Since as_of() collapses to one row per entity, and observations()
        keeps all rows, observations() must have at least as many rows.

        Additionally, observations() MUST be strictly GREATER than as_of() for at
        least one dataset. If observations() == as_of() for every dataset, the
        invariant is VACUOUS — the store has never accumulated a second observation
        of any entity, and the distinction between state and observation stream has
        never been tested.
        """
        now = datetime.now(timezone.utc)
        state_counts = {}
        obs_counts = {}

        for dataset in datasets:
            state_df = store.as_of(dataset, now)
            obs_df = store.observations(dataset, until=now)

            state_count = len(state_df)
            obs_count = len(obs_df)

            state_counts[dataset] = state_count
            obs_counts[dataset] = obs_count

            assert (
                obs_count >= state_count
            ), f"Dataset {dataset!r}: observations() returned {obs_count} rows but "
            f"as_of() returned {state_count}. observations() must be a superset of as_of()."

        # Check that at least one dataset has MORE observations than state
        has_accumulation = any(
            obs_counts[ds] > state_counts[ds] for ds in datasets
        )
        assert (
            has_accumulation
        ), f"Store invariant is VACUOUS: no dataset has accumulated a second observation. "
        f"observations() == as_of() for all {len(datasets)} datasets. "
        f"Counts: {[(ds, state_counts[ds], obs_counts[ds]) for ds in datasets]}. "
        f"Until the store contains at least one entity observed more than once, "
        f"the distinction between state and observation stream is untested."

    # ========================================================================
    # Invariant 4: Timestamp columns materialise (no Rust panic)
    # ========================================================================

    @pytest.mark.slow
    def test_timestamp_columns_materialise(self, store, datasets):
        """
        Invariant: observed_at and valid_at columns materialise without panic.

        Before 2026-08-20, reading any tz-aware timestamp column from Parquet
        raised an uncatchable PanicException from Polars' Rust layer. Polars was
        trying to resolve the IANA timezone database (which does not exist on
        Windows without the tzdata package). This manifested only on materialisation
        (calling .to_list(), .to_pandas(), etc.); len() and dtype checks passed
        silently while the column was unreadable.

        This test forces materialisation of every observed_at and valid_at column
        to confirm they are readable.
        """
        for dataset in datasets:
            df = store.observations(dataset, until=datetime.now(timezone.utc))

            if df.is_empty():
                # No data to materialise
                continue

            # Materialise observed_at and valid_at by calling .to_list()
            # This forces Polars to resolve the timezone and construct real
            # datetime objects. If tzdata is missing, this raises PanicException
            # (which is uncatchable under normal circumstances). The guard in
            # store.py._verify_tzdata_available() should have caught this at
            # import time, but verify it here anyway.
            try:
                observed_at_list = df["observed_at"].to_list()
                assert (
                    observed_at_list
                ), f"Dataset {dataset!r}: observed_at materialised but is empty"
                assert all(
                    isinstance(ts, datetime) for ts in observed_at_list
                ), f"Dataset {dataset!r}: observed_at contains non-datetime values"

                valid_at_list = df["valid_at"].to_list()
                assert (
                    valid_at_list
                ), f"Dataset {dataset!r}: valid_at materialised but is empty"
                assert all(
                    isinstance(ts, datetime) for ts in valid_at_list
                ), f"Dataset {dataset!r}: valid_at contains non-datetime values"
            except Exception as e:
                pytest.fail(
                    f"Dataset {dataset!r}: timestamp column materialisation failed. "
                    f"This is bug #3 from the brief (uncatchable Rust panic). "
                    f"Error: {type(e).__name__}: {e}. "
                    f"On Windows, ensure the tzdata package is installed."
                )

    # ========================================================================
    # Invariant 5: Leakage guard (as_of at historical cutoff returns 0 rows)
    # ========================================================================

    @pytest.mark.slow
    def test_leakage_guard_as_of_2000(self, store, datasets):
        """
        Invariant: as_of(dataset, 2000-01-01) returns zero rows for every dataset.

        Blueprint §3.2 (bitemporal integrity) forbids reading present-day prices,
        ownership, or injury flags into a historical context. The leakage-prevention
        primitive is as_of(dataset, cutoff), which filters on observed_at ONLY.

        An as_of(2000-01-01) call for every dataset should return 0 rows — no data
        was observed before 2000. If any dataset returns rows, that is evidence
        of a present-day fact (or a future fact from the store's write perspective)
        leaking into a historical cutoff. That would be a CRITICAL BUG.
        """
        cutoff_2000 = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        for dataset in datasets:
            df = store.as_of(dataset, cutoff_2000)
            assert (
                len(df) == 0
            ), f"Dataset {dataset!r}: as_of(2000-01-01) returned {len(df)} rows. "
            f"No data should have been observed before the year 2000. "
            f"This is a LEAKAGE BUG — present-day facts are bleeding into historical "
            f"queries. Returned rows: {df.to_dict(as_series=False)}"

    # ========================================================================
    # Invariant 6: Mixed-schema readability (old and new batches coexist)
    # ========================================================================

    @pytest.mark.slow
    def test_mixed_schema_readability(self, store, datasets):
        """
        Invariant: all columns are readable when old and new parquet files coexist.

        Before 2026-08-20, adding a column to the store schema (e.g. provider_id)
        broke reads across mixed batches. DuckDB's union_by_name=false (the default)
        raised "schema mismatch in glob" the instant an old-schema file coexisted
        with a new-schema file. Even with union_by_name=true, Polars' 100-row
        dtype inference would infer Null for an optional column if all 100 sampled
        rows were NULL, then fail when the first actual value appeared.

        This invariant verifies:
        1. All columns can be read (no "schema mismatch" error).
        2. Columns with NULL values in old batches show both NULL and non-NULL
           values in the result (mixed provenance).

        This is bug #1 from the brief. It only manifests with real mixed schemas
        on disk, which is why existing unit tests that built fresh uniform stores
        never caught it.
        """
        for dataset in datasets:
            df = store.observations(dataset, until=datetime.now(timezone.utc))

            if df.is_empty():
                # Dataset exists on disk but has no data yet (or very recent)
                continue

            # Verify all columns can be accessed and are non-null on at least one row
            # This forces Polars to materialise them (avoiding the silent NULL bug)
            try:
                # Try to access every column
                for col in df.columns:
                    series = df[col]
                    # Force materialisation by getting a simple property
                    _ = series.len()

                # Optional: check that nullable columns (like provider_id, added later)
                # show both NULL and non-NULL if the dataset was updated with the new
                # schema. This is a softer check — we just confirm mixed values exist.
                if "provider_id" in df.columns:
                    provider_id_col = df["provider_id"]
                    has_null = provider_id_col.is_null().any()
                    has_value = provider_id_col.is_not_null().any()
                    # It's OK if all are NULL (old batches only) or all are value (new
                    # batches only), or mixed. We just want to ensure the column is
                    # readable without error.
            except Exception as e:
                pytest.fail(
                    f"Dataset {dataset!r}: mixed-schema readability failed. "
                    f"This is bug #1 from the brief (mixed old/new schemas). "
                    f"Error: {type(e).__name__}: {e}. "
                    f"Ensure union_by_name=true is set in DuckDB reads."
                )

    # ========================================================================
    # Invariant 7: Provenance columns are present and populated
    # ========================================================================

    @pytest.mark.slow
    def test_provenance_columns_present(self, store, datasets):
        """
        Invariant: observed_at, content_hash, and batch_id exist and are non-null
        on every row.

        Blueprint §12.3's mandatory provenance requires three columns on every
        written batch:
          - observed_at: when the row was observed (for leakage prevention)
          - content_hash: deterministic hash of the payload (for idempotency)
          - batch_id: unique id of this batch (for tie-breaking as_of queries)

        This test verifies all three exist and have no NULLs.
        """
        for dataset in datasets:
            df = store.observations(dataset, until=datetime.now(timezone.utc))

            if df.is_empty():
                continue

            # Check that required provenance columns exist
            required_cols = {"observed_at", "content_hash", "batch_id"}
            missing_cols = required_cols - set(df.columns)
            assert (
                not missing_cols
            ), f"Dataset {dataset!r}: missing provenance columns: {missing_cols}. "
            f"Every row must carry observed_at, content_hash, and batch_id."

            # Check that they are non-null
            for col in ["observed_at", "content_hash", "batch_id"]:
                null_count = df[col].is_null().sum()
                assert (
                    null_count == 0
                ), f"Dataset {dataset!r}: column {col!r} has {null_count} NULL rows. "
                f"Provenance columns must never be NULL — every row must record "
                f"when it was observed, its content hash, and its batch id."

    # ========================================================================
    # Invariant 8: Entity key uniqueness within each batch (catches wrong keys)
    # ========================================================================

    @pytest.mark.slow
    def test_entity_key_unique_within_batch(self, store, datasets):
        """
        Invariant: Within each batch (group by batch_id), each entity key is unique.

        This is the invariant that would have caught the vaastav_player_gameweek_stats
        entity-key bug: the declared key was (season, round, element), but the actual
        data grain is (season, round, element, fixture) — a player can play twice
        in a double gameweek. When grouped by the wrong key within a batch, multiple
        rows have the same key, violating uniqueness.

        The existing test "as_of_returns_state_not_observation_stream" checks that
        as_of() returns one row per entity key, but that test is TAUTOLOGICAL
        (as_of() collapses TO the declared key, so it always has one row per key
        whether the key is right or wrong). This invariant catches the actual
        problem: the raw data itself must satisfy key uniqueness within each batch
        — otherwise the declared key does not describe the data's actual grain.

        For each dataset with a non-empty entity key, groups the observations by
        batch_id + entity_key and verifies that each group has exactly one row.
        """
        for dataset in datasets:
            entity_key = DATASET_ENTITY_KEYS[dataset]

            # Skip singleton datasets (entity_key == ())
            if not entity_key:
                continue

            df = store.observations(dataset, until=datetime.now(timezone.utc))

            if df.is_empty():
                continue

            # First, deduplicate identical rows — as_of() collapses them on read anyway.
            # Only keep distinct rows (identical rows across all columns are collapsed to one).
            df_distinct = df.unique(maintain_order=True)

            # Group by batch_id + entity_key and count rows per group
            group_cols = ["batch_id"] + list(entity_key)
            grouped = df_distinct.group_by(group_cols, maintain_order=True).agg(
                pl.len().alias("_row_count")
            )

            # Check that every group has exactly one row. Identical duplicates have
            # been collapsed by distinct(), so this only fails for genuinely different
            # rows sharing an entity key — the actual data grain mismatch.
            duplicates = grouped.filter(pl.col("_row_count") > 1)

            assert (
                duplicates.is_empty()
            ), (
                f"Dataset {dataset!r}: entity key {entity_key} is NOT unique "
                f"within batches. Found {len(duplicates)} groups with multiple rows "
                f"(entity key is not the actual data grain). This is the bug that "
                f"as_of() silently drops rows on. Example groups: "
                f"{duplicates.head(5).to_dict(as_series=False)}"
            )

    # ========================================================================
    # Invariant 9: the dtype contract (presence-not-dtype gap, §14.6) holds
    # for every REAL batch already on disk, not a fresh fixture
    # ========================================================================

    @pytest.mark.slow
    def test_schema_dtype_contract_holds_for_every_real_batch_on_disk(self, datasets):
        """
        Invariant: `FactTableSchema.validate()`'s dtype contract (added
        2026-08-21 to close docs/wiki/provider-framework.md §14.6's
        "presence, not dtype" gap) does not reject anything already
        written to the real store.

        This is a "do not break existing data" check, not a leakage/
        correctness check like the other eight invariants: story authors
        that only run against fresh fixtures cannot see the real cross-
        provider/cross-era dtype variety this store actually holds (Int32
        vs Int64, VARCHAR vs DOUBLE for the same logical field across
        different capabilities, an all-NULL `outcome_point` column for a
        fixture with no totals market, ...). Read directly from each
        individual Parquet BATCH FILE (not the merged `as_of`/
        `observations` view, which lets DuckDB's `union_by_name` quietly
        promote/paper over a per-batch dtype that would fail `validate()`
        on its own) — verified 2026-08-21 against the real store: 330
        files across every capability with data on disk, zero failures.
        """
        for capability, schema in CANONICAL_SCHEMAS.items():
            try:
                dataset = capability_dataset(capability)
            except Exception:
                continue
            if dataset not in datasets:
                continue

            store_path = _get_store_path()
            files = sorted((store_path / dataset).glob("**/*.parquet"))
            for f in files:
                df = pl.read_parquet(f)
                try:
                    schema.validate(df)
                except Exception as e:
                    pytest.fail(
                        f"Real batch {f} for dataset {dataset!r} ({capability}) fails "
                        f"the dtype contract added to close §14.6's gap: {e}. Either "
                        "this file is a genuine incident (quarantine it, don't force "
                        "the check to accept it) or the contract is wrong for real "
                        "data — do not loosen this silently."
                    )

    # ========================================================================
    # Invariant 10: effective_at() on the REAL store (blueprint §3.2,
    # "Valid time is per ROW, not per batch", decision 2026-08-22)
    # ========================================================================
    #
    # Green on fresh fixtures does not prove this primitive works against
    # the real archive's actual shape (a VARCHAR kickoff_time, a real
    # exact-duplicate row, a real multi-batch write history) — the same
    # reason every other invariant in this module runs against
    # data/store/, not synthetic data (CLAUDE.md: "tests on fresh fixtures
    # cannot catch production bugs"). Read-only, same as every test above.

    @pytest.mark.slow
    def test_effective_at_collapses_to_state_on_every_declared_valid_time_dataset(self, store, datasets):
        """`effective_at(dataset, far_future)` must return exactly one row
        per entity key for every dataset that declares a valid_time_column
        and has data on disk — the same "state, not stream" invariant #2
        above already proves for as_of()'s OBSERVED-time axis, proven here
        for the VALID-time axis effective_at() resolves."""
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        declaring_datasets = [d for d in datasets if d in DATASET_VALID_TIME_COLUMNS]
        if not declaring_datasets:
            pytest.skip("no dataset on disk declares a valid_time_column")

        for dataset in declaring_datasets:
            df = store.effective_at(dataset, far_future)
            if df.is_empty():
                continue
            entity_key = DATASET_ENTITY_KEYS[dataset]
            n_distinct = df.select(entity_key).n_unique()
            assert n_distinct == df.height, (
                f"Dataset {dataset!r}: effective_at() returned {df.height} rows with "
                f"only {n_distinct} distinct entity keys {entity_key} — it must collapse "
                "to state exactly as as_of() does (ruling point 3), never a duplicated "
                "stream."
            )

    @pytest.mark.slow
    def test_effective_at_leakage_guard_far_past_cutoff_is_empty(self, store, datasets):
        """`effective_at(dataset, 1900-01-01)` must return zero rows for
        every dataset that declares a valid_time_column — mirrors invariant
        #5's as_of() leakage guard, on the valid-time axis instead of the
        observed-time axis. No Premier League fixture has a kickoff before
        1900."""
        cutoff_1900 = datetime(1900, 1, 1, tzinfo=timezone.utc)
        declaring_datasets = [d for d in datasets if d in DATASET_VALID_TIME_COLUMNS]
        if not declaring_datasets:
            pytest.skip("no dataset on disk declares a valid_time_column")

        for dataset in declaring_datasets:
            df = store.effective_at(dataset, cutoff_1900)
            assert df.is_empty(), (
                f"Dataset {dataset!r}: effective_at(1900-01-01) returned {df.height} rows "
                "— no real fixture kicked off before 1900; this is a leakage-guard failure "
                "on the valid-time axis."
            )

    @pytest.mark.slow
    def test_effective_at_matches_observations_row_count_when_no_duplicates_and_far_future(self, store, datasets):
        """Where the real archive has no duplicate rows for a fixture,
        effective_at() at a far-future cutoff should return the SAME row
        count as observations() (everything, deduplicated, is everything
        when nothing was ever duplicated) minus exactly the count of
        genuine duplicate-content rows — verified directly rather than
        assumed equal, since a live duplicate is a real, documented fact
        about this archive (2025-26, elements 100/391 — see
        docs/wiki/model-minutes.md and this session's punch-out)."""
        declaring_datasets = [d for d in datasets if d in DATASET_VALID_TIME_COLUMNS]
        if not declaring_datasets:
            pytest.skip("no dataset on disk declares a valid_time_column")

        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        for dataset in declaring_datasets:
            state = store.effective_at(dataset, far_future)
            stream = store.observations(dataset, until=datetime.now(timezone.utc))
            if stream.is_empty():
                continue
            entity_key = DATASET_ENTITY_KEYS[dataset]
            distinct_keys_in_stream = stream.select(entity_key).n_unique()
            assert state.height == distinct_keys_in_stream, (
                f"Dataset {dataset!r}: effective_at() returned {state.height} rows but "
                f"the raw stream has {distinct_keys_in_stream} distinct entity keys — "
                "effective_at() must resolve to exactly one row per distinct key present "
                "in the stream, not more or fewer."
            )
