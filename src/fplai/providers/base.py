"""Provider interface — story 3 of E2b (blueprint §12).

A `Provider` is NOT necessarily an HTTP API (design note: archive adapters,
story 10, read bulk CSV/Parquet). This interface therefore says nothing
about a base URL, a session, or a request-per-second model — that lives in
`transport.py`, which HTTP-based providers use if and when they need to.
What every provider MUST supply is: what it serves (`provider_id`,
`policy`), whether it can plausibly answer a given capability
(`supports`), and how it turns that capability into canonical rows for the
store (`fetch`) — nothing else.

`Provider` is a `Protocol` (structural typing), not an ABC a provider must
inherit from. That matters for exactly the non-HTTP case: a future archive
adapter with no session, no base_url and no per-request rate limiter still
satisfies this interface by shape, without a common base class forcing
HTTP-shaped constructor arguments onto something that isn't HTTP at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import polars as pl

from fplai.schemas import CapabilityKey
from fplai.transport import TransportPolicy


class ProviderError(RuntimeError):
    """A provider cannot serve a request it was asked for: an unsupported
    capability, a coverage mismatch it discovered at fetch time, or an
    upstream failure it chose to surface as a provider-level error rather
    than let a lower-level transport exception leak through unexplained."""


@dataclass(frozen=True)
class FetchResult:
    """What every `Provider.fetch()` call returns.

    `rows` is already in the capability's canonical shape — validated
    against `schemas.CANONICAL_SCHEMAS[capability]` by the adapter before
    this is constructed. Callers never see raw, provider-specific field
    names or nesting; blueprint §12.6 is explicit that no provider-specific
    response shape may leak past its adapter.

    Provenance fields (`provider_id`, `endpoint`, `observed_at`,
    `content_hash`, `capability`) satisfy blueprint §12.3's "provenance is
    mandatory" — every one of the five is present on every `FetchResult`,
    not optional metadata a caller might forget to pass through to the
    store. See `store.py`'s `write(provider_id=..., capability=...,
    endpoint=...)` for how these reach the persisted row.
    """

    rows: pl.DataFrame
    capability: CapabilityKey
    provider_id: str
    endpoint: str
    observed_at: datetime
    content_hash: str
    is_modelled: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    """Structural interface for a data provider.

    `supports()` is a cheap, provider-local self-description — "do I even
    know about this capability, roughly at this season/competition" — used
    e.g. when constructing a provider directly without going through a
    `CapabilityRegistry`. It is NOT the authoritative source of truth for
    provider selection; `registry.CapabilityRegistry` (story 2), with its
    declared `CoverageSpec` per registration, is what selection code should
    actually consult. The two exist for different reasons: `supports()`
    needs no registry at all; the registry needs no live provider instance
    to answer "who covers this" and is where fallback logging happens.
    """

    provider_id: str
    policy: TransportPolicy

    def supports(
        self,
        capability: CapabilityKey,
        *,
        season: str | None = None,
        competition: str | None = None,
    ) -> bool: ...

    def fetch(self, capability: CapabilityKey, **params: Any) -> FetchResult: ...
