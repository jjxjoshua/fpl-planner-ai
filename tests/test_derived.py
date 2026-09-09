"""Tests for fplai.derived and the derived-capability registration API in
fplai.schemas — E2b story 9 (blueprint §12.2, CLAUDE.md rule 3).

Three groups:
  1. Registration-time guards (fplai.schemas.register_derived_capability) —
     temp store, no network.
  2. write_derived/read_derived guards — temp store, no network.
  3. THE structural guarantee — an observed capability's `as_of()` cannot
     return a derived row, proven against a store that genuinely contains
     both, seeded in part from REAL identifiers read (read-only) from the
     production store at data/store/ (never written to — see conftest-style
     skip below, mirroring tests/test_store.py / test_store_invariants.py).

Every registered test capability is unregistered in a fixture `finally`
block: `CANONICAL_SCHEMAS`/`DATASET_ENTITY_KEYS` are process-wide dicts, and
tests/test_schemas.py asserts an EXACT set of keys on both — a leaked test
registration would fail those tests, not this module.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from fplai.derived import (
    CalibrationReference,
    DerivationInput,
    DerivedFactError,
    decode_derivation_inputs,
    encode_derivation_inputs,
    read_derived,
    write_derived,
)
from fplai.schemas import (
    CANONICAL_SCHEMAS,
    DATASET_ENTITY_KEYS,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    CapabilityKey,
    DerivedCapabilityError,
    capability_dataset,
    register_derived_capability,
    unregister_derived_capability,
)
from fplai.store import RESERVED_COLUMNS, BitemporalError, BitemporalStore

UTC = timezone.utc
_counter = itertools.count()


def dt(hour: int, day: int = 19) -> datetime:
    return datetime(2026, 8, day, hour, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


@pytest.fixture
def derived_capability():
    """Registers a fresh, uniquely-named test-only derived capability
    (entity key matching vaastav's player-gameweek grain, so it can
    plausibly reference real rows from that dataset as inputs) and
    guarantees it is unregistered afterwards even if the test fails."""
    n = next(_counter)
    capability = CapabilityKey("test_entity", f"derived_measure_{n}", "match")
    dataset = f"derived_test_entity_derived_measure_{n}_match"
    schema = register_derived_capability(
        capability,
        entity_key=("season", "round", "element", "fixture"),
        value_fields=("dc_estimate_mean", "dc_estimate_std"),
        dataset=dataset,
        description="test-only derived capability, tests/test_derived.py",
    )
    try:
        yield capability, dataset, schema
    finally:
        unregister_derived_capability(capability)


def _real_store_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "store" / "vaastav_player_gameweek_stats"


def _real_store_available() -> bool:
    return _real_store_path().exists() and any(_real_store_path().glob("**/*.parquet"))


requires_real_store = pytest.mark.skipif(
    not _real_store_available(),
    reason="data/store/vaastav_player_gameweek_stats absent; these tests read (never write) the real store.",
)


# -- 1. registration-time guards (fplai.schemas) ----------------------------


def test_register_derived_capability_requires_derived_prefix():
    cap = CapabilityKey("test_entity", "bad_prefix", "match")
    with pytest.raises(DerivedCapabilityError):
        register_derived_capability(
            cap, entity_key=("id",), value_fields=("x",), dataset="not_prefixed_dataset"
        )
    # Failed registration must not leak into the global dicts.
    assert cap not in CANONICAL_SCHEMAS
    assert "not_prefixed_dataset" not in DATASET_ENTITY_KEYS


def test_register_derived_capability_rejects_existing_capability_key():
    with pytest.raises(DerivedCapabilityError):
        register_derived_capability(
            PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            entity_key=("id",),
            value_fields=("x",),
            dataset="derived_should_never_land",
        )
    assert "derived_should_never_land" not in DATASET_ENTITY_KEYS


def test_register_derived_capability_rejects_existing_dataset_name():
    cap = CapabilityKey("test_entity", "dataset_collision", "match")
    with pytest.raises(DerivedCapabilityError):
        register_derived_capability(
            cap,
            entity_key=("id",),
            value_fields=("x",),
            dataset="vaastav_player_gameweek_stats",  # already registered, observed
        )
    assert cap not in CANONICAL_SCHEMAS


def test_registered_derived_schema_requires_provenance_fields(derived_capability):
    capability, dataset, schema = derived_capability
    assert schema.is_modelled is True
    for field in (
        "is_modelled",
        "derived_from",
        "calibration_reference",
        "calibration_residual_mean",
        "calibration_residual_std",
    ):
        assert field in schema.required_fields
    # A batch missing calibration_residual_std fails FactTableSchema.validate()
    # the same way any other missing required field does — no special-casing.
    incomplete = pl.DataFrame(
        {
            "season": ["2025-26"],
            "round": [1],
            "element": [1],
            "fixture": [1],
            "dc_estimate_mean": [1.2],
            "dc_estimate_std": [0.3],
            "is_modelled": [True],
            "derived_from": ["[]"],
            "calibration_reference": ["x"],
            "calibration_residual_mean": [0.0],
            # calibration_residual_std deliberately omitted
        }
    )
    from fplai.schemas import SchemaError

    with pytest.raises(SchemaError):
        schema.validate(incomplete)


def test_capability_dataset_round_trips(derived_capability):
    capability, dataset, _schema = derived_capability
    assert capability_dataset(capability) == dataset


def test_unregister_restores_prior_state():
    cap = CapabilityKey("test_entity", "temp_only", "match")
    dataset = "derived_temp_only_match"
    register_derived_capability(cap, entity_key=("id",), value_fields=("x",), dataset=dataset)
    assert cap in CANONICAL_SCHEMAS
    assert dataset in DATASET_ENTITY_KEYS
    unregister_derived_capability(cap)
    assert cap not in CANONICAL_SCHEMAS
    assert dataset not in DATASET_ENTITY_KEYS


# -- 2. write_derived / read_derived guards ---------------------------------


def _calibration() -> CalibrationReference:
    return CalibrationReference(
        reference="2025-26 observed defensive_contribution counts", residual_mean=0.0, residual_std=0.42
    )


def _one_input() -> list[DerivationInput]:
    return [
        DerivationInput(
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            entity_key={"season": "2019-20", "element": 123},
            note="whole-season per-90 rate, all rounds",
        )
    ]


def test_derivation_inputs_must_be_nonempty():
    with pytest.raises(DerivedFactError):
        encode_derivation_inputs([])


def test_calibration_reference_rejects_empty_reference():
    with pytest.raises(DerivedFactError):
        CalibrationReference(reference="   ", residual_mean=0.0, residual_std=0.1)


def test_calibration_reference_rejects_negative_std():
    with pytest.raises(DerivedFactError):
        CalibrationReference(reference="2025-26 DC counts", residual_mean=0.0, residual_std=-0.1)


def test_write_derived_refuses_to_write_under_an_observed_capability(store):
    df = pl.DataFrame(
        {"season": ["2025-26"], "round": [1], "element": [1], "fixture": [1], "name": ["x"]}
    )
    with pytest.raises(DerivedFactError):
        write_derived(
            store,
            PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            df,
            valid_at=dt(0),
            observed_at=dt(0),
            source="test",
            derived_from=_one_input(),
            calibration=_calibration(),
        )


def test_write_derived_rejects_a_forged_is_modelled_column(store, derived_capability):
    capability, dataset, _schema = derived_capability
    df = pl.DataFrame(
        {
            "season": ["2025-26"],
            "round": [1],
            "element": [1],
            "fixture": [1],
            "dc_estimate_mean": [1.0],
            "dc_estimate_std": [0.1],
            "is_modelled": [False],  # forged — must be rejected, not silently overwritten
        }
    )
    with pytest.raises(DerivedFactError):
        write_derived(
            store,
            capability,
            df,
            valid_at=dt(0),
            observed_at=dt(0),
            source="test",
            derived_from=_one_input(),
            calibration=_calibration(),
        )


def test_write_derived_then_read_derived_round_trips(store, derived_capability):
    capability, dataset, _schema = derived_capability
    df = pl.DataFrame(
        {
            "season": ["2025-26"],
            "round": [1],
            "element": [123],
            "fixture": [5],
            "dc_estimate_mean": [2.4],
            "dc_estimate_std": [0.6],
        }
    )
    result = write_derived(
        store,
        capability,
        df,
        valid_at=dt(0),
        observed_at=dt(0),
        source="test",
        derived_from=_one_input(),
        calibration=_calibration(),
    )
    assert result.written
    assert result.n_rows == 1

    out = read_derived(store, capability, dt(1))
    assert out.height == 1
    assert out["is_modelled"].to_list() == [True]
    assert out["calibration_reference"].to_list() == ["2025-26 observed defensive_contribution counts"]
    assert out["calibration_residual_std"].to_list() == [0.42]
    decoded = decode_derivation_inputs(out["derived_from"][0])
    assert decoded == [i.to_dict() for i in _one_input()]


def test_read_derived_raises_if_the_write_derived_contract_was_bypassed(store, derived_capability):
    """store.write() (2026-08-21: now enforces the derived-namespace
    naming invariant itself, see fplai.store._require_derived_naming_
    invariant) checks COLUMN PRESENCE, not column VALUES — a forged
    is_modelled=False still writes successfully as long as every required
    derived-provenance column is present under a derived_-namespaced
    dataset name. read_derived is what catches the wrong VALUE, on the way
    OUT, not the store — the two checks are complementary, not redundant:
    store.write() guards the naming invariant structurally; read_derived
    guards is_modelled's actual truthiness."""
    capability, dataset, schema = derived_capability
    forged = pl.DataFrame(
        {
            "season": ["2025-26"],
            "round": [1],
            "element": [999],
            "fixture": [5],
            "dc_estimate_mean": [1.0],
            "dc_estimate_std": [0.1],
            "is_modelled": [False],
            "derived_from": ["[]"],
            "calibration_reference": ["bypassed"],
            "calibration_residual_mean": [0.0],
            "calibration_residual_std": [0.0],
        }
    )
    write_result = store.write(dataset, forged, valid_at=dt(0), observed_at=dt(0), source="bypass")
    assert write_result.written  # store.write() itself raised no objection at all

    with pytest.raises(DerivedFactError):
        read_derived(store, capability, dt(1))


def test_store_write_itself_now_rejects_a_forged_is_modelled_column(store):
    """CLOSED 2026-08-21 (Architect finding, reproduced independently
    below and in tests/test_store.py's own attack-reproduction tests):
    store.write() previously had no is_modelled awareness at all and would
    accept a forged is_modelled=True column under an OBSERVED-looking
    dataset name without complaint — the guarantee held only 'if you use
    write_derived', strictly weaker than the odds adapter's null-id trick,
    where no code path can silently join an unresolved row regardless of
    which function a caller uses. fplai.store._require_derived_naming_
    invariant now makes store.write() itself enforce the same naming
    invariant fplai.schemas.register_derived_capability enforces at
    registration time — independent of write_derived, and independent of
    which dataset name or column set a caller tries."""
    df = pl.DataFrame({"id": [1], "is_modelled": [True], "derived_from": ["forged"]})
    with pytest.raises(BitemporalError):
        store.write("some_dataset_store_has_never_heard_of", df, valid_at=dt(0), observed_at=dt(0), source="t")


# -- 3. THE structural guarantee, against real identifiers -----------------


@pytest.mark.slow
@requires_real_store
def test_derivation_input_can_reference_real_store_rows():
    """Exercises the framework against genuine identifiers pulled from
    data/store/ (read-only), not fabricated ones — the ids referenced in
    derived_from actually exist in the production archive."""
    real_store = BitemporalStore()  # default path = data/store/, read-only here
    real_rows = real_store.as_of("vaastav_player_gameweek_stats", datetime.now(UTC))
    sample = real_rows.filter(
        (pl.col("season") == "2025-26") & (pl.col("round") == 1)
    ).head(1)
    assert sample.height == 1, "expected at least one 2025-26 GW1 row in the real store"
    row = sample.to_dicts()[0]

    inputs = [
        DerivationInput(
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            entity_key={
                "season": row["season"],
                "round": row["round"],
                "element": row["element"],
                "fixture": row["fixture"],
            },
            note="real row pulled from data/store/, story-9 verification",
        )
    ]
    decoded = decode_derivation_inputs(encode_derivation_inputs(inputs))
    assert decoded[0]["entity_key"]["element"] == row["element"]
    assert decoded[0]["entity_key"]["fixture"] == row["fixture"]


@pytest.mark.slow
@requires_real_store
def test_real_observed_store_has_no_is_modelled_column_today():
    """Baseline: today's real, unmodified store carries no is_modelled
    column at all — confirms this is genuinely new machinery, not
    redundant with something already on disk."""
    real_store = BitemporalStore()
    df = real_store.as_of("vaastav_player_gameweek_stats", datetime.now(UTC))
    assert "is_modelled" not in df.columns


@pytest.mark.slow
@requires_real_store
def test_derived_rows_are_structurally_absent_from_the_observed_dataset(store, derived_capability):
    """THE guarantee. A single store instance holds BOTH an observed
    dataset (vaastav_player_gameweek_stats, seeded with REAL rows read
    read-only from data/store/) and a derived dataset (the fixture's test
    capability) side by side. A query against the observed dataset's name
    must return exactly the observed rows and nothing derived — proven by
    row count, by column set (is_modelled never appears there), and by
    entity-key content (the derived row's own key is absent).
    """
    capability, dataset, _schema = derived_capability

    real_store = BitemporalStore()
    real_rows = (
        real_store.as_of("vaastav_player_gameweek_stats", datetime.now(UTC))
        .filter((pl.col("season") == "2025-26") & (pl.col("round") == 1))
        .head(3)
    )
    assert real_rows.height == 3, "expected >=3 real 2025-26 GW1 rows in the store"

    # Seed the OBSERVED side of the temp store with these real rows —
    # genuinely real values, replayed into an isolated store because
    # data/store/ itself may never be written to (E2b story 9 scope).
    # as_of() returns rows already carrying the store's own reserved
    # provenance columns (observed_at, batch_id, ...) — strip them before
    # re-writing, exactly as any ordinary ingest script would (it never
    # sees them on the way in, only on the way out).
    replay_rows = real_rows.drop([c for c in RESERVED_COLUMNS if c in real_rows.columns])
    store.write(
        "vaastav_player_gameweek_stats",
        replay_rows,
        valid_at=dt(0),
        observed_at=dt(0),
        source="test:replayed-from-real-store",
    )

    real_row = real_rows.to_dicts()[0]
    derived_from = [
        DerivationInput(
            capability=PLAYER_GAMEWEEK_STATS_GAMEWEEK,
            entity_key={
                "season": real_row["season"],
                "round": real_row["round"],
                "element": real_row["element"],
                "fixture": real_row["fixture"],
            },
            note="calibrating rate derived from this real row",
        )
    ]
    derived_df = pl.DataFrame(
        {
            "season": [real_row["season"]],
            "round": [real_row["round"]],
            "element": [real_row["element"]],
            "fixture": [real_row["fixture"]],
            "dc_estimate_mean": [1.7],
            "dc_estimate_std": [0.35],
        }
    )
    write_derived(
        store,
        capability,
        derived_df,
        valid_at=dt(1),
        observed_at=dt(1),
        source="test",
        derived_from=derived_from,
        calibration=_calibration(),
    )

    now = dt(2)
    observed_out = store.as_of("vaastav_player_gameweek_stats", now)
    derived_out = store.as_of(dataset, now)

    # The observed dataset sees exactly the 3 real rows, never the derived one.
    assert observed_out.height == 3
    assert "is_modelled" not in observed_out.columns
    assert "dc_estimate_mean" not in observed_out.columns

    # The derived dataset sees exactly the 1 derived row, correctly labelled.
    assert derived_out.height == 1
    assert derived_out["is_modelled"].to_list() == [True]
    assert derived_out["dc_estimate_mean"].to_list() == [1.7]
