"""Derived-capability write/read gate — E2b story 9 (blueprint §12.2,
CLAUDE.md rule 3: "modelled data is labelled as modelled ... schema
requirement, not convention").

A DERIVED capability is satisfied by computation over other capabilities,
not by measurement — the DC estimator (blueprint §11: player season rate x
team per-match total, calibrated against 2025-26's real
`defensive_contribution` counts, the only season both FPL's own counters and
a third-party per-match count exist for) is the first real consumer; the
captaincy backcast (§3.4, Phase 5 — captaincy% modelled from ownership,
price, form, fixture, and `most_captained`, calibrated forward from GW1
2026/27) is the second, deliberately not built here.

## The structural guarantee, in two halves

`fplai.schemas.register_derived_capability` (owned by that module — see its
docstring for the full argument) enforces, AT REGISTRATION TIME, that a
derived capability's dataset lives under `DERIVED_DATASET_PREFIX`
("derived_"), a namespace entirely disjoint from every observed capability's
dataset. `BitemporalStore.as_of()`/`observations()` glob only the ONE
dataset directory they're asked for — a query against an observed dataset's
name (`"vaastav_player_gameweek_stats"`, `"elements"`, ...) therefore cannot
return a derived row: it was never written to that directory. This is the
same class of guarantee the odds adapter uses at a different layer
(`docs/wiki/provider-framework.md` §14.4a) — there, a null
`player_element_id` cannot equi-join; here, a derived row cannot be read
back by a query that names a different dataset, because dataset name IS the
physical location on disk.

THIS module is the second half: the only sanctioned way to actually WRITE a
derived batch. `write_derived` below:

- Refuses unless `capability` was registered via
  `register_derived_capability` (i.e. is genuinely `is_modelled=True`) —
  you cannot derive a row into an OBSERVED capability's identity through
  this function, closing the other direction of the same leak.
- Stamps `is_modelled=True` onto every row ITSELF, as a literal — never
  caller-supplied. A caller cannot pass a forged `is_modelled=False` (or
  omit it) because the column is REJECTED if already present on the input
  DataFrame (mirrors `store.write()`'s own `RESERVED_COLUMNS` collision
  check, `derived._RESERVED_COLUMNS` below). This is the story-9 brief's
  "not by convention, not by a flag a caller might forget" made concrete:
  the only way a stored row's `is_modelled` can be `True` is for it to have
  gone through this function.
- Also stamps `derived_from` (JSON-encoded `DerivationInput`s — "enough to
  re-derive it") and the three calibration columns (`calibration_reference`
  the string, `calibration_residual_mean`/`calibration_residual_std` the
  residual CARRIED AS UNCERTAINTY per blueprint §11, not discarded).
- Then delegates to `store.write()`, which (as of 2026-08-21, see below)
  ALSO enforces the naming invariant itself — this module is still
  entirely a caller of the write path, exactly the relationship
  `providers/*.py` already has with `store.write()`, but the store no
  longer merely trusts the caller to have validated first.

## The guarantee now holds independent of which function a caller uses

**2026-08-21, closed the same day it was built.** The Architect attacked
the first version of this design directly: `store.write()` had no notion
of `is_modelled` at all and accepted a forged `is_modelled=True` row
straight into an OBSERVED dataset's name, bypassing this module entirely.
That made the guarantee read "a derived fact cannot leak *if you use
`write_derived`*" — materially weaker than the standard the odds adapter's
null-id trick set (`docs/wiki/provider-framework.md` §14.4a), where the
data itself enforces the rule and no code path can silently join an
unresolved row, whether or not a caller remembers a helper function.

`BitemporalStore.write()` (`fplai/store.py`, story 9's originally
out-of-scope module — the Architect authorised and made this change, not
this story routing around its own boundary) now calls
`_require_derived_naming_invariant` on every write, BIDIRECTIONALLY: a
payload carrying any `DERIVED_PROVENANCE_FIELDS` column may only be
written to a `DERIVED_DATASET_PREFIX`-namespaced dataset, and a payload
written to such a dataset must carry the full set. This closes the exact
gap the Architect demonstrated — `tests/test_store.py`'s
`test_derived_provenance_columns_rejected_outside_derived_namespace` /
`test_derived_dataset_missing_provenance_columns_is_rejected` reproduce
the attack directly and were proven to fail against the pre-fix code
before the guard was restored.

**What the store-level guard checks, and what it deliberately does not.**
It checks column PRESENCE, not column VALUES: a row carrying
`is_modelled=False` (rather than `True`) under a `derived_`-namespaced
dataset with every required column present still writes successfully —
`_require_derived_naming_invariant` cannot tell a genuine calibration
result from a forged one, only that the shape is honest. `read_derived`
below is what catches a wrong VALUE, on the way out
(`tests/test_derived.py::
test_read_derived_raises_if_the_write_derived_contract_was_bypassed`).
The two checks are complementary, not redundant: the store enforces the
naming/shape invariant structurally, for every writer, regardless of
which function they call; `write_derived`/`read_derived` additionally
guarantee that a value actually went through this module's own logic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

import polars as pl

from fplai.schemas import (
    CANONICAL_SCHEMAS,
    CapabilityKey,
    capability_dataset,
)
from fplai.store import BitemporalStore, WriteResult

# The columns write_derived stamps itself. Rejected if the caller's
# DataFrame already carries any of them — same collision-guard shape as
# store.RESERVED_COLUMNS, applied one layer up so a caller cannot smuggle a
# per-row is_modelled value past this function at all.
_RESERVED_COLUMNS = {
    "is_modelled",
    "derived_from",
    "calibration_reference",
    "calibration_residual_mean",
    "calibration_residual_std",
}


class DerivedFactError(ValueError):
    """`write_derived`/`read_derived` misuse: a capability that was never
    registered via `fplai.schemas.register_derived_capability` (or was
    registered but is `is_modelled=False` — i.e. actually observed), a
    batch that collides with a reserved provenance column, an empty
    `derived_from`, or an invalid `CalibrationReference`."""


@dataclass(frozen=True)
class DerivationInput:
    """One capability + rows a derived batch was computed from — "enough
    to re-derive it" (story-9 brief). `entity_key` may be a PARTIAL key
    (e.g. `{"season": "2019-20", "element": 123}` with no `round`/
    `fixture`) to mean "every row matching this partial key", for a
    derivation that aggregates over many source rows (a player's
    whole-season per-90 rate, say) rather than naming every one
    individually — the consumer re-derives by querying that capability
    with this filter, not by looking up one exact row.
    """

    capability: CapabilityKey
    entity_key: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"capability": str(self.capability), "entity_key": self.entity_key, "note": self.note}


def encode_derivation_inputs(inputs: Sequence[DerivationInput]) -> str:
    """JSON-encode a batch's derivation inputs for the `derived_from`
    column. Refuses an empty sequence: blueprint §12.2 requires every
    derived row to carry which capabilities/rows it came from, and a
    derived row with no recorded input is indistinguishable from a bug that
    forgot to pass any."""
    if not inputs:
        raise DerivedFactError(
            "a derived batch must record at least one DerivationInput — "
            "blueprint §12.2 requires derived rows to carry the capabilities "
            "and rows they were derived from, enough to re-derive them."
        )
    return json.dumps([i.to_dict() for i in inputs], sort_keys=True)


def decode_derivation_inputs(raw: str) -> list[dict[str, Any]]:
    """Inverse of `encode_derivation_inputs` — for a consumer re-deriving
    or auditing a stored derived row."""
    return json.loads(raw)


@dataclass(frozen=True)
class CalibrationReference:
    """What a derived batch was calibrated against, and the residual
    carried as uncertainty rather than discarded (blueprint §11's DC
    calibration constraint, generalised here to any derived capability that
    approximates a quantity measured differently elsewhere — the constraint
    is not DC-specific, only DC-first).

    `residual_mean`/`residual_std` are the minimum structured
    representation of "the calibration residual, as uncertainty" — a bare
    correction factor with no spread would be CLAUDE.md rule 5's "a scalar
    xPts at a module boundary is a design error" applied to a calibration
    correction instead of a prediction, the same mistake by another name. A
    capability needing a richer residual (quantiles, a full empirical
    distribution) carries those as ADDITIONAL, capability-specific columns
    on top of these two required ones — the same present-but-optional
    escape hatch `schemas.py` already uses for `value_raw` alongside
    `value`.
    """

    reference: str
    residual_mean: float
    residual_std: float

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise DerivedFactError(
                "calibration_reference must name what this batch was calibrated "
                "against (e.g. '2025-26 observed defensive_contribution counts') "
                "— an empty reference is not auditable."
            )
        if self.residual_std < 0:
            raise DerivedFactError(
                f"calibration_residual_std must be >= 0, got {self.residual_std} — "
                "a negative std cannot represent uncertainty; this would silently "
                "discard the residual rather than carry it (blueprint §11)."
            )


def write_derived(
    store: BitemporalStore,
    capability: CapabilityKey,
    df: pl.DataFrame,
    *,
    valid_at: datetime,
    observed_at: datetime,
    source: str,
    derived_from: Sequence[DerivationInput],
    calibration: CalibrationReference,
    skip_if_unchanged: bool = True,
    provider_id: str | None = None,
    endpoint: str | None = None,
) -> WriteResult:
    """The ONLY sanctioned way to persist a derived-capability batch. See
    this module's docstring for the full guarantee and its boundary.

    `capability` must already be registered via
    `fplai.schemas.register_derived_capability` — this function looks up
    its schema and its dataset (`fplai.schemas.capability_dataset`) rather
    than accepting either as a parameter, so a caller cannot mismatch a
    capability against the wrong dataset name.

    Raises `DerivedFactError` if:
    - `capability` isn't registered, or is registered but
      `is_modelled=False` (i.e. it's actually an OBSERVED capability's key
      — this function refuses to write a "derived" row under an observed
      identity).
    - `df` already carries any of `_RESERVED_COLUMNS` — those are stamped
      by this function, never caller data.
    - `derived_from` is empty, or `calibration` is invalid (both raise from
      their own constructors before this function does any work).

    Then stamps `is_modelled=True`, the encoded `derived_from`, and the
    three calibration columns onto every row and delegates to the
    unmodified `store.write()`.
    """
    schema = CANONICAL_SCHEMAS.get(capability)
    if schema is None or not schema.is_modelled:
        raise DerivedFactError(
            f"{capability} is not a registered derived capability "
            f"(is_modelled={getattr(schema, 'is_modelled', None)}). Register it "
            "first via fplai.schemas.register_derived_capability — write_derived "
            "refuses to write a derived row under any other capability's identity "
            "(blueprint §12.2)."
        )

    collision = _RESERVED_COLUMNS & set(df.columns)
    if collision:
        raise DerivedFactError(
            f"payload columns collide with reserved derived-provenance metadata: "
            f"{collision} — these are stamped by write_derived, never supplied by "
            "the caller (this is what makes is_modelled structurally trustworthy: "
            "a caller cannot pass a forged value)."
        )

    derived_from_json = encode_derivation_inputs(derived_from)

    enriched = df.with_columns(
        pl.lit(True).alias("is_modelled"),
        pl.lit(derived_from_json).alias("derived_from"),
        pl.lit(calibration.reference).alias("calibration_reference"),
        pl.lit(calibration.residual_mean).alias("calibration_residual_mean"),
        pl.lit(calibration.residual_std).alias("calibration_residual_std"),
    )
    schema.validate(enriched)

    dataset = capability_dataset(capability)
    return store.write(
        dataset,
        enriched,
        valid_at=valid_at,
        observed_at=observed_at,
        source=source,
        skip_if_unchanged=skip_if_unchanged,
        provider_id=provider_id,
        capability=str(capability),
        endpoint=endpoint,
    )


def read_derived(store: BitemporalStore, capability: CapabilityKey, as_of_ts: datetime) -> pl.DataFrame:
    """Sugar for `as_of()` on a derived capability's own dataset, with one
    extra defensive check: every returned row's `is_modelled` column must
    be `True`. If it is not, the `write_derived` contract was bypassed
    somewhere upstream (e.g. a hand-written parquet file dropped into the
    `derived_` directory) — raised loudly here rather than silently
    returned as though it were trustworthy derived data.
    """
    schema = CANONICAL_SCHEMAS.get(capability)
    if schema is None or not schema.is_modelled:
        raise DerivedFactError(
            f"{capability} is not a registered derived capability — nothing to read."
        )
    dataset = capability_dataset(capability)
    df = store.as_of(dataset, as_of_ts)
    if not df.is_empty() and "is_modelled" in df.columns and not df["is_modelled"].all():
        raise DerivedFactError(
            f"dataset {dataset!r} contains rows with is_modelled != True — the "
            "write_derived contract was bypassed for at least one row in this "
            "dataset (blueprint §12.2). Refusing to return it as trustworthy "
            "derived data."
        )
    return df
