"""Tests for fplai.store — the bitemporal writer and the as_of read
primitive. No network; a temp directory stands in for the store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from fplai.store import BitemporalError, BitemporalStore, content_hash


UTC = timezone.utc


def dt(hour: int) -> datetime:
    return datetime(2026, 8, 19, hour, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


# -- write() validation ----------------------------------------------------


def test_naive_valid_at_is_rejected(store):
    df = pl.DataFrame({"id": [1]})
    with pytest.raises(BitemporalError):
        store.write("x", df, valid_at=datetime(2026, 8, 19), observed_at=dt(0), source="t")


def test_naive_observed_at_is_rejected(store):
    df = pl.DataFrame({"id": [1]})
    with pytest.raises(BitemporalError):
        store.write("x", df, valid_at=dt(0), observed_at=datetime(2026, 8, 19), source="t")


def test_reserved_column_collision_is_rejected(store):
    df = pl.DataFrame({"observed_at": [1]})
    with pytest.raises(BitemporalError):
        store.write("x", df, valid_at=dt(0), observed_at=dt(0), source="t")


def test_empty_batch_is_rejected(store):
    with pytest.raises(BitemporalError):
        store.write("x", pl.DataFrame(), valid_at=dt(0), observed_at=dt(0), source="t")


def test_write_returns_written_true_for_new_content(store):
    df = pl.DataFrame({"id": [1, 2], "price": [50, 55]})
    result = store.write("elements", df, valid_at=dt(0), observed_at=dt(0), source="t")
    assert result.written
    assert result.n_rows == 2
    assert result.path is not None
    assert result.path.exists()


# -- idempotency -------------------------------------------------------


def test_identical_content_is_skipped(store):
    df = pl.DataFrame({"id": [1], "price": [50]})
    r1 = store.write("elements", df, valid_at=dt(0), observed_at=dt(0), source="t")
    r2 = store.write("elements", df, valid_at=dt(1), observed_at=dt(1), source="t")
    assert r1.written
    assert not r2.written
    assert r1.content_hash == r2.content_hash


def test_changed_content_always_writes(store):
    df1 = pl.DataFrame({"id": [1], "price": [50]})
    df2 = pl.DataFrame({"id": [1], "price": [51]})
    r1 = store.write("elements", df1, valid_at=dt(0), observed_at=dt(0), source="t")
    r2 = store.write("elements", df2, valid_at=dt(1), observed_at=dt(1), source="t")
    assert r1.written and r2.written
    assert r1.content_hash != r2.content_hash


def test_skip_if_unchanged_false_always_writes(store):
    df = pl.DataFrame({"id": [1], "price": [50]})
    r1 = store.write("elements", df, valid_at=dt(0), observed_at=dt(0), source="t", skip_if_unchanged=False)
    r2 = store.write("elements", df, valid_at=dt(1), observed_at=dt(1), source="t", skip_if_unchanged=False)
    assert r1.written and r2.written


def test_content_hash_is_order_independent():
    df1 = pl.DataFrame({"id": [1, 2], "price": [50, 60]})
    df2 = pl.DataFrame({"id": [2, 1], "price": [60, 50]})
    assert content_hash(df1) == content_hash(df2)


def test_content_hash_is_column_order_independent():
    df1 = pl.DataFrame({"id": [1], "price": [50]})
    df2 = pl.DataFrame({"price": [50], "id": [1]})
    assert content_hash(df1) == content_hash(df2)


# -- as_of: the leakage-prevention primitive, and STATE not the stream ----
#
# Decision 2026-08-20 (blueprint §3.2): as_of() returns STATE — one row per
# declared entity key — never the raw observation stream. The raw stream is
# observations(), an explicit opt-in. Several tests below were rewritten
# under this decision; each says so at the point of change.


def test_as_of_before_any_write_is_empty(store):
    df = pl.DataFrame({"id": [1], "price": [50]})
    store.write("elements", df, valid_at=dt(5), observed_at=dt(5), source="t")
    result = store.as_of("elements", dt(0))
    assert result.is_empty()


def test_as_of_excludes_rows_observed_after_cutoff(store):
    df1 = pl.DataFrame({"id": [1], "price": [50]})
    df2 = pl.DataFrame({"id": [1], "price": [99]})
    store.write("elements", df1, valid_at=dt(0), observed_at=dt(0), source="t")
    store.write("elements", df2, valid_at=dt(10), observed_at=dt(10), source="t")

    # as_of a cutoff between the two observations must NOT see the later one.
    result = store.as_of("elements", dt(5))
    assert result.height == 1
    assert result["price"].to_list() == [50]


def test_as_of_mid_history_returns_value_correct_at_that_instant_not_newest(store):
    # Entity-key resolution at a mid-history timestamp: value correct AS OF
    # that instant, not the newest value that happens to exist overall.
    store.write("elements", pl.DataFrame({"id": [1], "price": [50]}), valid_at=dt(0), observed_at=dt(0), source="t")
    store.write("elements", pl.DataFrame({"id": [1], "price": [51]}), valid_at=dt(1), observed_at=dt(1), source="t")
    store.write("elements", pl.DataFrame({"id": [1], "price": [52]}), valid_at=dt(2), observed_at=dt(2), source="t")

    result = store.as_of("elements", dt(1))
    assert result.height == 1
    assert result["price"].to_list() == [51]  # not 52, the newest overall


def test_as_of_on_nonexistent_dataset_is_empty(store):
    result = store.as_of("does_not_exist", dt(0))
    assert result.is_empty()


def test_as_of_requires_tz_aware_cutoff(store):
    with pytest.raises(BitemporalError):
        store.as_of("elements", datetime(2026, 8, 19))


def test_as_of_leakage_guard_far_past_cutoff_is_empty(store):
    # The exact production check from the brief: as_of('elements', a date
    # long before any data existed) must return zero rows.
    store.write("elements", pl.DataFrame({"id": [1], "price": [50]}), valid_at=dt(0), observed_at=dt(0), source="t")
    result = store.as_of("elements", datetime(2020, 1, 1, tzinfo=UTC))
    assert result.is_empty()


def test_as_of_collapses_to_one_row_per_declared_entity_key(store):
    # elements declares entity_key=("id",) in fplai.schemas.DATASET_ENTITY_KEYS
    # -- as_of() now collapses on it automatically, no caller opt-in needed.
    store.write(
        "elements",
        pl.DataFrame({"id": [1, 2], "price": [50, 60]}),
        valid_at=dt(0),
        observed_at=dt(0),
        source="t",
    )
    store.write(
        "elements",
        pl.DataFrame({"id": [1, 2], "price": [51, 60]}),
        valid_at=dt(1),
        observed_at=dt(1),
        source="t",
    )
    result = store.as_of("elements", dt(2))
    prices = dict(zip(result["id"].to_list(), result["price"].to_list()))
    assert prices == {1: 51, 2: 60}


def test_as_of_raises_for_dataset_with_no_declared_entity_key(store):
    # REWRITTEN 2026-08-20: this used to test that latest_only=True without
    # an entity_key argument raised. That caller-supplied-override design is
    # gone -- as_of() now always resolves the key from
    # fplai.schemas.DATASET_ENTITY_KEYS, and raises for datasets that have
    # no declaration there at all (never guesses one). "mystery_dataset" is
    # deliberately not in DATASET_ENTITY_KEYS.
    store.write("mystery_dataset", pl.DataFrame({"id": [1]}), valid_at=dt(0), observed_at=dt(0), source="t")
    with pytest.raises(BitemporalError, match="mystery_dataset"):
        store.as_of("mystery_dataset", dt(1))


def test_as_of_tie_break_on_identical_observed_at_is_stable_across_repeated_queries(store):
    # Ties (two rows for the same entity key sharing the same observed_at)
    # break on batch_id descending -- arbitrary, but STABLE: batch_id is
    # fixed at write time and persisted, so repeated queries against the
    # same on-disk data always pick the same winner (CLAUDE.md rule 7).
    store.write("elements", pl.DataFrame({"id": [1], "price": [50]}), valid_at=dt(0), observed_at=dt(0), source="t")
    store.write(
        "elements", pl.DataFrame({"id": [1], "price": [999]}), valid_at=dt(0), observed_at=dt(0), source="t",
        skip_if_unchanged=False,
    )

    first = store.as_of("elements", dt(1))["price"].to_list()
    second = store.as_of("elements", dt(1))["price"].to_list()
    assert first == second
    assert first[0] in (50, 999)


def test_as_of_disappearing_entity_persists_in_state_at_last_known_value(store):
    # Design decision, documented in docs/wiki/provider-framework.md: an
    # entity that stops appearing in later batches (e.g. a player purged
    # from a provider's payload) is NOT treated as deleted -- this
    # append-only store has no tombstone event. as_of() keeps returning its
    # last known row.
    store.write(
        "elements",
        pl.DataFrame({"id": [1, 2], "price": [50, 60]}),
        valid_at=dt(0), observed_at=dt(0), source="t",
    )
    store.write(
        "elements",
        pl.DataFrame({"id": [1], "price": [55]}),  # id=2 no longer present
        valid_at=dt(1), observed_at=dt(1), source="t",
    )
    result = store.as_of("elements", dt(2))
    prices = dict(zip(result["id"].to_list(), result["price"].to_list()))
    assert prices == {1: 55, 2: 60}  # id=2 persists at its last known value


def test_as_of_singleton_dataset_collapses_to_exactly_one_row(store):
    store.write("game_config", pl.DataFrame({"payload": ["{}"]}), valid_at=dt(0), observed_at=dt(0), source="t")
    store.write(
        "game_config", pl.DataFrame({"payload": ['{"a":1}']}), valid_at=dt(1), observed_at=dt(1), source="t",
        skip_if_unchanged=False,
    )
    result = store.as_of("game_config", dt(2))
    assert result.height == 1
    assert result["payload"].to_list() == ['{"a":1}']


def test_as_of_returns_one_row_per_entity_across_many_snapshot_batches(store):
    # Regression test for the exact production bug found 2026-08-20:
    # as_of('elements', now) returned 7,735 rows = 595 players x 13
    # snapshots (every observation, not the state). Reproduced here at a
    # smaller N/M: as_of() must return exactly N; observations() must
    # return N*M.
    n_entities = 20
    n_batches = 13
    for batch in range(n_batches):
        df = pl.DataFrame(
            {
                "id": list(range(n_entities)),
                "price": [50 + batch] * n_entities,  # changes every batch, so never skipped
            }
        )
        store.write("elements", df, valid_at=dt(0), observed_at=dt(batch), source="t")

    state = store.as_of("elements", dt(n_batches))
    assert state.height == n_entities

    stream = store.observations("elements", until=dt(n_batches))
    assert stream.height == n_entities * n_batches


# -- observations(): the raw append-only stream, explicit opt-in ----------


def test_observations_returns_every_historical_batch_raw_stream(store):
    # REWRITTEN 2026-08-20: this used to assert as_of() WITHOUT latest_only
    # returned the raw stream for picks/ ("no single current row per entity
    # to collapse to"). That was the bug the blueprint decision closed --
    # as_of() now always returns state. The raw-stream behaviour this test
    # protects still exists, just under its own name.
    store.write(
        "picks",
        pl.DataFrame({"entry_id": [1], "event": [1], "element": [10]}),
        valid_at=dt(0), observed_at=dt(0), source="t", skip_if_unchanged=False,
    )
    store.write(
        "picks",
        pl.DataFrame({"entry_id": [1], "event": [1], "element": [10]}),
        valid_at=dt(1), observed_at=dt(1), source="t", skip_if_unchanged=False,
    )
    assert store.observations("picks", until=dt(2)).height == 2
    # Same (entry_id, event, element) key both times -> as_of collapses to 1.
    assert store.as_of("picks", dt(2)).height == 1


def test_observations_requires_tz_aware_cutoff(store):
    store.write("elements", pl.DataFrame({"id": [1]}), valid_at=dt(0), observed_at=dt(0), source="t")
    with pytest.raises(BitemporalError):
        store.observations("elements", until=datetime(2026, 8, 19))


def test_observations_on_nonexistent_dataset_is_empty(store):
    assert store.observations("does_not_exist", until=dt(0)).is_empty()


def test_observations_works_without_a_declared_entity_key(store):
    # No state to collapse to, so nothing to guess -- unlike as_of(),
    # observations() never needs a declared key.
    store.write("mystery_dataset", pl.DataFrame({"id": [1]}), valid_at=dt(0), observed_at=dt(0), source="t")
    result = store.observations("mystery_dataset", until=dt(1))
    assert result.height == 1


# -- determinism (blueprint §7.2 gate-repair session, s003; CLAUDE.md rule 7) --
#
# `scripts/run_baselines.py --seed 42` produced 1907/1909/1887 across three
# runs on identical code and data. `as_of()`/`observations()` had no
# canonical outer ORDER BY -- unlike effective_at(), which already carries
# one after the walk-forward-validate ordering bug (see that method's
# docstring). PROVEN FAIL-FIRST: run against unpatched store.py (the
# `ORDER BY`/`ORDER BY ALL` lines removed) before writing the fix, both
# assertions below failed -- see this session's punch-card `finding` for the
# exact captured (pre-fix) output. Both use the SAME attack pattern
# effective_at()'s own ordering test already uses one call up (two
# independently-built stores, same logical content, opposite physical
# write/batch order) rather than a single run compared to a stored constant
# -- a single run proves nothing about determinism.


def _write_one_row_per_batch(store: BitemporalStore, ids: list[int]) -> None:
    for i, entity_id in enumerate(ids):
        store.write(
            "elements",
            pl.DataFrame({"id": [entity_id], "price": [50 + entity_id]}),
            valid_at=dt(0),
            observed_at=dt(0) + timedelta(seconds=i),
            source="t",
            skip_if_unchanged=False,
        )


def test_as_of_row_order_is_a_pure_function_of_content_not_write_order(tmp_path):
    ids = list(range(1, 31))
    store_forward = BitemporalStore(base_path=tmp_path / "forward")
    store_reversed = BitemporalStore(base_path=tmp_path / "reversed")
    _write_one_row_per_batch(store_forward, ids)
    _write_one_row_per_batch(store_reversed, list(reversed(ids)))

    out_forward = store_forward.as_of("elements", dt(0) + timedelta(days=1000))["id"].to_list()
    out_reversed = store_reversed.as_of("elements", dt(0) + timedelta(days=1000))["id"].to_list()
    assert out_forward == out_reversed
    assert out_forward == sorted(out_forward)


def test_observations_row_order_is_a_pure_function_of_content_not_write_order(tmp_path):
    ids = list(range(1, 31))
    store_forward = BitemporalStore(base_path=tmp_path / "forward")
    store_reversed = BitemporalStore(base_path=tmp_path / "reversed")
    _write_one_row_per_batch(store_forward, ids)
    _write_one_row_per_batch(store_reversed, list(reversed(ids)))

    out_forward = store_forward.observations("elements", until=dt(0) + timedelta(days=1000))["id"].to_list()
    out_reversed = store_reversed.observations("elements", until=dt(0) + timedelta(days=1000))["id"].to_list()
    assert out_forward == out_reversed


def test_as_of_and_observations_are_stable_across_repeated_queries_same_store(store):
    # Repeated runs (N=5, not one run compared to a constant) against the
    # SAME on-disk data must always return the same row order.
    _write_one_row_per_batch(store, [5, 1, 4, 2, 3])
    as_of_runs = [store.as_of("elements", dt(0) + timedelta(days=1000))["id"].to_list() for _ in range(5)]
    obs_runs = [store.observations("elements", until=dt(0) + timedelta(days=1000))["id"].to_list() for _ in range(5)]
    assert all(r == as_of_runs[0] for r in as_of_runs)
    assert all(r == obs_runs[0] for r in obs_runs)


def test_latest_is_sugar_for_as_of_now(store):
    store.write("elements", pl.DataFrame({"id": [1], "price": [50]}), valid_at=dt(0), observed_at=dt(0), source="t")
    result = store.latest("elements")
    assert result.height == 1
    assert result["price"].to_list() == [50]


def test_write_result_records_source_and_hash_per_row(store):
    df = pl.DataFrame({"id": [1, 2]})
    store.write("elements", df, valid_at=dt(0), observed_at=dt(0), source="unit-test-source")
    result = store.as_of("elements", dt(1))
    assert set(result["source"].to_list()) == {"unit-test-source"}
    assert result["content_hash"].n_unique() == 1
    assert result["batch_id"].n_unique() == 1


# -- provenance (blueprint §12.3) -----------------------------------------


def test_provenance_fields_default_to_null_when_not_supplied(store):
    df = pl.DataFrame({"id": [1]})
    store.write("elements", df, valid_at=dt(0), observed_at=dt(0), source="t")
    result = store.as_of("elements", dt(1))
    assert result["provider_id"].to_list() == [None]
    assert result["capability"].to_list() == [None]
    assert result["endpoint"].to_list() == [None]


def test_provenance_fields_are_recorded_when_supplied(store):
    df = pl.DataFrame({"id": [1]})
    store.write(
        "elements",
        df,
        valid_at=dt(0),
        observed_at=dt(0),
        source="fpl_api:bootstrap-static",
        provider_id="fpl_api",
        capability="player.attributes@current",
        endpoint="bootstrap-static/",
    )
    result = store.as_of("elements", dt(1))
    assert result["provider_id"].to_list() == ["fpl_api"]
    assert result["capability"].to_list() == ["player.attributes@current"]
    assert result["endpoint"].to_list() == ["bootstrap-static/"]


def test_provider_id_collides_with_reserved_column(store):
    df = pl.DataFrame({"provider_id": ["should not be a payload column"]})
    with pytest.raises(BitemporalError):
        store.write("x", df, valid_at=dt(0), observed_at=dt(0), source="t")


def test_reading_across_old_and_new_schema_batches_does_not_break(store):
    """The exact scenario this store already faces in production: batches
    written before provider_id/capability/endpoint existed sit on disk next
    to batches written after. union_by_name must make this transparent —
    old rows read back with NULL for the new columns, nothing raises."""
    old = pl.DataFrame({"id": [1], "price": [50]})
    store.write("elements", old, valid_at=dt(0), observed_at=dt(0), source="t")  # no provenance kwargs

    new = pl.DataFrame({"id": [2], "price": [60]})
    store.write(
        "elements",
        new,
        valid_at=dt(1),
        observed_at=dt(1),
        source="t",
        skip_if_unchanged=False,
        provider_id="fpl_api",
        capability="player.attributes@current",
        endpoint="bootstrap-static/",
    )

    result = store.as_of("elements", dt(2)).sort("id")
    assert result["id"].to_list() == [1, 2]
    assert result["provider_id"].to_list() == [None, "fpl_api"]


def test_dtype_inference_regression_large_old_schema_batch_then_new_schema(store):
    """Regression test for the dtype inference bug: _fetch_polars() used to
    infer dtypes from the first 100 rows. When batches written before
    provider_id/capability/endpoint columns existed (no columns, hence NULL after
    union_by_name) came first, all 100 inference rows showed NULL, so the
    column was typed Null. The first actual string value like 'fpl_api' then
    raised: 'could not append value ... of type: str to the builder'.

    This test writes 150+ rows WITHOUT optional columns (exceeding the 100-row
    inference window), then adds a batch WITH provider_id populated. as_of()
    must return all rows with correct dtypes, no raise.
    """
    # 160 rows without provider_id/capability/endpoint — exceeds polars' default
    # 100-row inference window, ensuring all inference rows see NULL.
    old_schema = pl.DataFrame({
        "id": list(range(160)),
        "name": [f"player_{i}" for i in range(160)],
    })
    r1 = store.write("elements", old_schema, valid_at=dt(0), observed_at=dt(0), source="t")
    assert r1.written

    # Now add a batch with provider_id and other optional columns populated.
    new_schema = pl.DataFrame({
        "id": [160, 161],
        "name": ["player_160", "player_161"],
    })
    r2 = store.write(
        "elements",
        new_schema,
        valid_at=dt(1),
        observed_at=dt(1),
        source="t",
        skip_if_unchanged=False,
        provider_id="fpl_api",
        capability="player.attributes@current",
        endpoint="bootstrap-static/",
    )
    assert r2.written

    # as_of() must return all 162 rows without raising, with correct types.
    result = store.as_of("elements", dt(2)).sort("id")
    assert result.height == 162

    # Verify provider_id is correctly typed as string (or null), not Null.
    provider_ids = result["provider_id"].to_list()
    assert provider_ids[:160] == [None] * 160  # Old batch has NULL
    assert provider_ids[160:] == ["fpl_api", "fpl_api"]  # New batch has string

    # Verify the optional columns exist and have the right types.
    assert "capability" in result.columns
    assert "endpoint" in result.columns


def test_materializing_tz_aware_timestamp_column_requires_tzdata(store):
    """Regression test for the tzdata/ZoneInfo bug: on Windows without the
    tzdata package installed, polars' Rust layer panics with
    ZoneInfoNotFoundError when materialising a tz-aware TIMESTAMP column —
    calling .to_list(), .to_pandas(), .item(), indexing a Series, etc.

    This bug is silent in tests that only check dtypes or call len() (which do
    not materialise), but crashes in production when the column is actually
    read. This test verifies the fix: tzdata must be installed and ZoneInfo
    must resolve UTC.

    The test writes a row, reads it back via as_of() (which produces a
    DataFrame with observed_at and valid_at as pl.Datetime("us", "UTC")),
    and then calls .to_list() to force materialisation of the timezone-aware
    column. Before the fix, this would panic with an uncatchable
    PanicException; with tzdata installed, it succeeds.
    """
    df = pl.DataFrame({"id": [1], "price": [50]})
    store.write("elements", df, valid_at=dt(0), observed_at=dt(0), source="t")

    # Read back via as_of(), which returns observed_at and valid_at as
    # tz-aware Datetime columns (pl.Datetime("us", "UTC")).
    result = store.as_of("elements", dt(1))
    assert result.height == 1

    # Check that observed_at column exists and is tz-aware.
    assert "observed_at" in result.columns
    observed_at_col = result["observed_at"]
    assert isinstance(observed_at_col.dtype, pl.Datetime)
    assert observed_at_col.dtype.time_zone == "UTC"

    # Call .to_list() to force materialisation of the tz-aware column.
    # Before the fix (no tzdata), this would panic inside polars' Rust layer.
    # With tzdata installed, it succeeds and returns a list of datetime objects.
    observed_at_list = observed_at_col.to_list()
    assert len(observed_at_list) == 1
    assert isinstance(observed_at_list[0], datetime)


# -- derived-capability naming invariant, bidirectional (E2b story 9, closed
#    2026-08-21 after the Architect demonstrated store.write() would accept
#    a forged is_modelled row into an observed dataset) --------------------


def test_derived_provenance_columns_rejected_outside_derived_namespace(store):
    """The Architect's attack, reproduced as a test. A legitimate row is
    written to an OBSERVED dataset first, then a second write to the SAME
    dataset carrying is_modelled/derived_from is attempted — it must raise,
    and the observed dataset's as_of() schema must never gain the column
    (i.e. the rejected write must not have reached disk at all)."""
    legit = pl.DataFrame({"season": ["2025-26"], "id": [1], "code": [111], "web_name": ["Real Player"]})
    store.write("vaastav_player_identity", legit, valid_at=dt(0), observed_at=dt(0), source="t")

    smuggle = pl.DataFrame(
        {
            "season": ["2025-26"],
            "id": [2],
            "code": [222],
            "web_name": ["Smuggled"],
            "is_modelled": [True],
            "derived_from": ["[]"],
        }
    )
    with pytest.raises(BitemporalError):
        store.write("vaastav_player_identity", smuggle, valid_at=dt(1), observed_at=dt(1), source="attack")

    out = store.as_of("vaastav_player_identity", dt(2))
    assert out.height == 1  # only the legitimate row ever reached disk
    assert "is_modelled" not in out.columns
    assert "derived_from" not in out.columns


def test_derived_dataset_missing_provenance_columns_is_rejected(store):
    """The reverse direction: a payload written to a derived_-namespaced
    dataset without the full derived-provenance column set must also raise
    — an unlabelled row in a derived dataset is the same failure wearing
    the opposite hat."""
    df = pl.DataFrame({"id": [1], "estimate": [1.2]})
    with pytest.raises(BitemporalError):
        store.write("derived_dc_estimate_match", df, valid_at=dt(0), observed_at=dt(0), source="attack")


def test_derived_dataset_with_full_provenance_columns_is_accepted(store):
    """The positive path: a derived_-namespaced dataset carrying every
    derived-provenance column writes normally."""
    df = pl.DataFrame(
        {
            "id": [1],
            "estimate": [1.2],
            "is_modelled": [True],
            "derived_from": ["[]"],
            "calibration_reference": ["x"],
            "calibration_residual_mean": [0.0],
            "calibration_residual_std": [0.1],
        }
    )
    result = store.write("derived_dc_estimate_match", df, valid_at=dt(0), observed_at=dt(0), source="t")
    assert result.written


# -- naive-temporal-column guard (closes docs/wiki/provider-framework.md
#    §14.6's "presence, not dtype" gap — a structural backstop at write(),
#    independent of whether the caller already called FactTableSchema.
#    validate(), mirroring the derived-naming-invariant precedent above) ---


def test_write_rejects_a_timezone_aware_datetime_column(store):
    # Reconstructs the real incident directly against store.write(), one
    # layer below the schema-level check: a caller that builds a DataFrame
    # and calls write() directly, bypassing any provider adapter's
    # FactTableSchema.validate() call, must still be refused.
    aware = datetime(2026, 8, 22, 15, 0, tzinfo=UTC)
    df = pl.DataFrame({"id": [1], "commence_time": [aware]})
    with pytest.raises(BitemporalError, match="timezone-AWARE"):
        store.write("mystery_dataset", df, valid_at=dt(0), observed_at=dt(0), source="attack")


def test_write_rejects_timezone_aware_column_even_on_an_unregistered_dataset(store):
    # Deliberately schema-agnostic — the guard does not require `dataset`
    # to be a registered capability (many tests in this file write partial
    # payloads to unregistered/partial-shape dataset names by design).
    aware = datetime(2026, 8, 22, tzinfo=UTC)
    df = pl.DataFrame({"anything": [aware]})
    with pytest.raises(BitemporalError):
        store.write("totally_unregistered_dataset", df, valid_at=dt(0), observed_at=dt(0), source="attack")


def test_write_accepts_naive_datetime_columns(store):
    naive = datetime(2026, 8, 22, 15, 0)
    df = pl.DataFrame({"id": [1], "commence_time": [naive]})
    result = store.write("mystery_dataset", df, valid_at=dt(0), observed_at=dt(0), source="t")
    assert result.written


def test_write_naive_temporal_guard_does_not_reject_partial_payloads_on_registered_datasets(store):
    # The guard must not reshape write()'s existing contract: a payload far
    # short of a registered capability's required_fields (as many tests in
    # this file already exercise, e.g. "elements" with just id/price) must
    # keep writing — this guard checks tz-awareness only, never presence or
    # dtype family (that stays in FactTableSchema.validate(), a stricter,
    # capability-aware check this module deliberately does not duplicate).
    df = pl.DataFrame({"id": [1], "price": [50]})
    result = store.write("elements", df, valid_at=dt(0), observed_at=dt(0), source="t")
    assert result.written


# -- effective_at(): the valid-time bitemporal read primitive (blueprint
#    §3.2, "Valid time is per ROW, not per batch", decision 2026-08-22) ----
#
# vaastav_player_gameweek_stats is the only dataset that currently declares
# a valid_time_column (kickoff_time, string-formatted -- see
# fplai.schemas.PLAYER_GAMEWEEK_STATS_GAMEWEEK). Its entity key is (season,
# round, element, fixture); minimal payloads here carry only what these
# tests actually exercise, the same convention every other test in this
# file already uses for "elements" (id/price only, far short of a real
# capability's required_fields) -- store.write() checks tz-awareness and
# reserved-column collisions only, never required-field presence.

_GW_DATASET = "vaastav_player_gameweek_stats"


def _gw_row(season="2024-25", round_=1, element=1, fixture=1, kickoff="2024-08-16T19:00:00Z", **extra) -> dict:
    row = {"season": season, "round": round_, "element": element, "fixture": fixture, "kickoff_time": kickoff}
    row.update(extra)
    return row


def test_effective_at_excludes_a_row_at_or_after_the_cutoff_strict_boundary(store):
    # The boundary this ruling is explicit about: strictly BEFORE, not at.
    # A fixture with kickoff_time == effective_ts has not been played yet.
    store.write(_GW_DATASET, pl.DataFrame([_gw_row(kickoff="2024-08-16T19:00:00Z")]), valid_at=dt(0), observed_at=dt(0), source="t")
    kickoff_exact = datetime(2024, 8, 16, 19, 0, tzinfo=UTC)
    assert store.effective_at(_GW_DATASET, kickoff_exact).is_empty()
    one_second_before = datetime(2024, 8, 16, 18, 59, 59, tzinfo=UTC)
    assert store.effective_at(_GW_DATASET, one_second_before).is_empty()
    one_second_after = datetime(2024, 8, 16, 19, 0, 1, tzinfo=UTC)
    assert store.effective_at(_GW_DATASET, one_second_after).height == 1


def test_effective_at_boundary_is_strict_not_inclusive_unlike_as_of(store):
    # Explicit contrast with as_of()'s own (correct, unchanged) <= boundary
    # — the two primitives use DIFFERENT operators on purpose (see
    # effective_at()'s docstring table). Same fixture, same cutoff instant:
    # as_of() would be a BitemporalError here anyway (no observed_at axis
    # relevance), so this asserts effective_at()'s own operator directly
    # rather than by contrast with a call that cannot be made comparable.
    kickoff = datetime(2024, 8, 16, 19, 0, tzinfo=UTC)
    store.write(_GW_DATASET, pl.DataFrame([_gw_row(kickoff="2024-08-16T19:00:00Z")]), valid_at=dt(0), observed_at=dt(0), source="t")
    assert store.effective_at(_GW_DATASET, kickoff).height == 0


def test_effective_at_requires_tz_aware_cutoff(store):
    store.write(_GW_DATASET, pl.DataFrame([_gw_row()]), valid_at=dt(0), observed_at=dt(0), source="t")
    with pytest.raises(BitemporalError):
        store.effective_at(_GW_DATASET, datetime(2026, 1, 1))


def test_effective_at_on_nonexistent_dataset_is_empty_no_raise(store):
    # Mirrors as_of()'s own "nothing to guess wrong about" behaviour for a
    # dataset with literally no data yet — checked BEFORE the valid-time-
    # column declaration lookup, same order as_of() checks _dataset_exists
    # before _resolve_entity_key.
    assert store.effective_at("does_not_exist", dt(1)).is_empty()


def test_effective_at_raises_for_a_dataset_with_no_declared_valid_time_column(store):
    # "elements" has real data and a declared entity key, but NO declared
    # valid_time_column — effective_at() must raise, not silently fall back
    # to some other column or return an empty/wrong frame (blueprint §3.2
    # ruling point 4: "a dataset that declares no valid-time column keeps
    # today's behaviour", i.e. it simply isn't queryable through this
    # primitive at all).
    store.write("elements", pl.DataFrame({"id": [1], "price": [50]}), valid_at=dt(0), observed_at=dt(0), source="t")
    with pytest.raises(BitemporalError, match="elements"):
        store.effective_at("elements", dt(1))


def test_effective_at_collapses_to_one_row_per_declared_entity_key(store):
    # Attacks the exact real-store bug found live while migrating
    # fplai.models.team_strength/minutes to this primitive: an exact-
    # duplicate row within one batch (verified live, 2025-26 element 100)
    # must collapse to ONE row, not two — "collapsing to state on the
    # entity key exactly as as_of() does" (ruling point 3), not merely a
    # time filter.
    df = pl.DataFrame([_gw_row(element=1), _gw_row(element=1)])  # exact duplicate
    store.write(_GW_DATASET, df, valid_at=dt(0), observed_at=dt(0), source="t")
    out = store.effective_at(_GW_DATASET, dt(1))
    assert out.height == 1


def test_effective_at_tie_break_prefers_the_later_observed_at_correction(store):
    # Attacks the collapse ordering directly: two batches share an entity
    # key AND the same valid time (kickoff_time never changes for the same
    # fixture) but disagree on some other column, written with different
    # observed_at. effective_at() must resolve to the LATER-observed_at
    # version (a correction learned more recently), never an arbitrary
    # batch_id-only tiebreak — the same "prefer what we learned more
    # recently" contract as_of() already uses as ITS primary tiebreak.
    store.write(
        _GW_DATASET,
        pl.DataFrame([_gw_row(element=1, minutes=5)]),
        valid_at=dt(0), observed_at=dt(0), source="batch1",
    )
    store.write(
        _GW_DATASET,
        pl.DataFrame([_gw_row(element=1, minutes=90)]),  # a correction
        valid_at=dt(0), observed_at=dt(10), source="batch2-correction",
    )
    out = store.effective_at(_GW_DATASET, dt(11))
    assert out.height == 1
    assert out["minutes"].to_list() == [90]


def test_effective_at_tie_break_is_stable_across_repeated_queries(store):
    # Same CLAUDE.md rule 7 guarantee as_of() already provides for its own
    # tie-break — repeated queries against the same on-disk data must
    # always pick the same winner.
    store.write(
        _GW_DATASET,
        pl.DataFrame([_gw_row(element=1, minutes=5)]),
        valid_at=dt(0), observed_at=dt(0), source="batch1",
    )
    store.write(
        _GW_DATASET,
        pl.DataFrame([_gw_row(element=1, minutes=90)]),
        valid_at=dt(0), observed_at=dt(0), source="batch2",  # SAME observed_at too
        skip_if_unchanged=False,
    )
    first = store.effective_at(_GW_DATASET, dt(1))["minutes"].to_list()
    second = store.effective_at(_GW_DATASET, dt(1))["minutes"].to_list()
    assert first == second


def test_effective_at_row_order_is_a_pure_function_of_content_not_scan_order(store):
    # Regression for the exact live bug found migrating fplai.models.
    # minutes: two DIFFERENT datasets sharing rows for a common prefix of
    # entity keys must return that shared prefix in IDENTICAL relative
    # order — a QUALIFY/window-function query has no order guarantee from
    # DuckDB without an explicit outer ORDER BY, and this store's own
    # walk-forward test (tests/test_minutes.py::
    # test_walk_forward_validate_cannot_see_a_folds_own_or_future_outcomes)
    # failed on exactly this before effective_at() sorted its output on the
    # entity key.
    shared_rows = [_gw_row(element=e, round_=1, fixture=1) for e in range(1, 6)]
    store.write(_GW_DATASET, pl.DataFrame(shared_rows), valid_at=dt(0), observed_at=dt(0), source="t")
    out = store.effective_at(_GW_DATASET, dt(1))
    # A test that only checks order on a frame that could ALSO silently be
    # missing rows is not a real test (CLAUDE.md lesson 5, "a test that
    # cannot fail is worse than no test") — five DISTINCT entity keys (only
    # `element` varies) must all survive the collapse, not just the top-
    # ranked one; this line alone would have caught the live bug where an
    # earlier draft of effective_at() forgot PARTITION BY entirely and
    # collapsed the whole batch to its single globally-latest row.
    assert out.height == 5
    assert out["element"].to_list() == sorted(out["element"].to_list())


def test_effective_at_never_reads_the_stores_own_valid_at_metadata_column(store, monkeypatch):
    # ATTACK, not a sanctioned-path test (CLAUDE.md: "no path is unsafe" is
    # the standard). Directly tamper with the registry to declare a dataset
    # whose valid_time_column IS this store's own reserved 'valid_at'
    # metadata column name — exactly the "wrong primitive by accident"
    # blueprint §3.2 ruling point 4 forbids, and exactly the failure mode
    # ruling point 5 warns about (valid_at on a bulk-ingested dataset is a
    # write-time-stamped constant nothing may depend on). No FactTableSchema
    # in this codebase does this today; this proves the guard holds even if
    # one someday tried to, attacked from outside FactTableSchema's own
    # __post_init__ validation (which cannot see a RESERVED_COLUMNS
    # collision — that set lives in store.py, not schemas.py).
    import fplai.store as store_module

    monkeypatch.setitem(store_module.DATASET_VALID_TIME_COLUMNS, "elements", ("valid_at", None))
    store.write("elements", pl.DataFrame({"id": [1], "price": [50]}), valid_at=dt(0), observed_at=dt(0), source="t")
    with pytest.raises(BitemporalError, match="valid_at"):
        store.effective_at("elements", dt(1))


def test_effective_at_singleton_dataset_collapses_to_one_row(store, monkeypatch):
    # No currently-declared valid_time_column dataset is a singleton
    # (entity_key == ()), but the code path exists and mirrors as_of()'s
    # own singleton handling — exercised here via the same
    # DATASET_VALID_TIME_COLUMNS tamper technique as the reserved-column
    # attack above, pairing a declared valid_time_column with
    # game_config's real singleton entity key (()), to prove the
    # empty-PARTITION-BY branch collapses correctly rather than crashing
    # or returning every row.
    import fplai.store as store_module

    monkeypatch.setitem(store_module.DATASET_VALID_TIME_COLUMNS, "game_config", ("as_of_marker", None))
    # `as_of_marker` must be naive (store.write() rejects a tz-aware DATA
    # column — only bitemporal metadata may be tz-aware), unlike the `dt()`
    # helper above which is tz-aware by design for valid_at/observed_at.
    store.write(
        "game_config",
        pl.DataFrame({"payload": ["{}"], "as_of_marker": [datetime(2026, 8, 19, 0, 0)]}),
        valid_at=dt(0), observed_at=dt(0), source="t",
    )
    store.write(
        "game_config",
        pl.DataFrame({"payload": ['{"a":1}'], "as_of_marker": [datetime(2026, 8, 19, 5, 0)]}),
        valid_at=dt(1), observed_at=dt(1), source="t", skip_if_unchanged=False,
    )
    out = store_module.BitemporalStore(base_path=store.base_path).effective_at("game_config", dt(10))
    assert out.height == 1
    assert out["payload"].to_list() == ['{"a":1}']


def test_effective_at_does_not_touch_observed_at_axis_at_all(store):
    # A row's inclusion depends purely on the declared valid-time column —
    # backdating observed_at (an attempted "smuggle a future fact as
    # already known" attack) must not change whether a row is INCLUDED,
    # only which of several tied rows wins the collapse (see the tie-break
    # test above). This is the same adversarial pattern
    # tests/test_team_strength.py and tests/test_minutes.py already run
    # against the two real consumers of this primitive, reproduced here at
    # the store layer directly.
    store.write(
        _GW_DATASET,
        pl.DataFrame([_gw_row(element=1, kickoff="2027-05-01T19:00:00Z")]),  # future kickoff, after dt(1)'s 2026-08-19
        valid_at=dt(0), observed_at=dt(0), source="backdated-observed-at",  # but "observed" long ago
    )
    out = store.effective_at(_GW_DATASET, dt(1))  # cutoff long before that kickoff
    assert out.is_empty(), "a backdated observed_at must not smuggle a future-kickoff row past effective_at()"


def test_as_of_survives_an_entity_key_column_named_with_a_reserved_sql_word(store, monkeypatch):
    # REGRESSION, found in production rather than in review (2026-08-27):
    # registering `dc_threshold_observations` with the entity key
    # (season, group, round) made as_of() emit
    #   PARTITION BY season, group, round
    # and DuckDB answered `Parser Error: syntax error at or near "group"`.
    # The dataset was well-formed; the STORE was interpolating entity-key
    # columns into generated SQL as bare identifiers, so any reserved word
    # was a hard failure — for every dataset, not just that one.
    #
    # Attacked through the registry rather than through the real dataset,
    # so this keeps testing the store's SQL generation even if that
    # particular schema is later renamed or dropped.
    import fplai.store as store_module

    monkeypatch.setitem(store_module.DATASET_ENTITY_KEYS, "elements", ("group", "order"))
    frame = pl.DataFrame({"group": ["DEF", "MID"], "order": [1, 1], "value": [10, 12]})
    store.write("elements", frame, valid_at=dt(0), observed_at=dt(0), source="t")

    state = store.as_of("elements", dt(1))

    assert state.height == 2
    assert set(state["group"]) == {"DEF", "MID"}
    # and the canonical outer ORDER BY still applies, on the quoted columns
    assert state["group"].to_list() == ["DEF", "MID"]

