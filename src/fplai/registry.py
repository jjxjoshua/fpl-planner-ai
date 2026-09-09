"""Capability registry — story 2 of E2b (blueprint §12, §12.3).

Maps a `CapabilityKey` to the ordered candidate providers that can serve
it, for a given season/competition. Coverage is DECLARED DATA — each
provider's `CoverageSpec` states what seasons/competitions it serves — not
inferred by branching on a provider's name or type. A registry that let a
season-grain provider silently answer a match-grain query, or a
2022-24-only provider silently answer a 2026 query, is the exact failure
this module exists to prevent (blueprint §12.1, §3.6's swappability
obligation).

Selection and any fallback are always LOGGED (blueprint §12.3) — a silent
fallback to a lower-quality source is the failure mode this whole layer
exists to prevent. In this slice there is exactly one provider per
capability, so `resolve()` never actually falls back to anything — the
logging path exists and is tested against a synthetic two-provider case so
it is proven before story 6 needs it for real.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fplai.providers.base import Provider
from fplai.schemas import CapabilityKey

logger = logging.getLogger("fplai.registry")


class RegistryError(LookupError):
    """No registered provider can serve a requested capability at the
    requested season/competition. Raised, never silently swallowed — an
    unresolved capability must fail loudly (blueprint §12.5's principle
    applied to selection, not just identity)."""


@dataclass(frozen=True)
class CoverageSpec:
    """What a provider claims to serve for one capability.

    `seasons`: e.g. `frozenset({"2016-17", ..., "2026-27"})`, or `None` for
    "not season-scoped" (a live-only provider like the FPL API, which has
    no season parameter at all — it always serves whatever is currently
    live; that limitation is documented in prose on the provider, not
    encoded as a season-equality check here, since asserting a specific
    season string would itself be a hardcoded value CLAUDE.md rule 4
    forbids).
    `competitions`: e.g. `frozenset({"PL"})`; `None` means unrestricted.
    `priority`: lower = preferred when multiple providers serve the same
    capability at the same season/competition (e.g. a free source
    preferred over a paid fallback). Ties keep registration order.
    """

    seasons: frozenset[str] | None = None
    competitions: frozenset[str] | None = None
    priority: int = 100

    def matches(self, *, season: str | None, competition: str | None) -> bool:
        if self.seasons is not None and season is not None and season not in self.seasons:
            return False
        if self.competitions is not None and competition is not None and competition not in self.competitions:
            return False
        return True


@dataclass(frozen=True)
class RegistryEntry:
    provider: Provider
    coverage: CoverageSpec


class CapabilityRegistry:
    """`(entity, measure, grain)` -> ordered candidate providers.

    Registration is explicit and additive: `register(capability, provider,
    coverage)`. There is no discovery and no reflection — a provider claims
    a capability only by an explicit registry entry, which is exactly the
    E2b gate: adding a provider is an adapter plus a registry entry, never
    a change to code that resolves capabilities.
    """

    def __init__(self) -> None:
        self._entries: dict[CapabilityKey, list[RegistryEntry]] = {}

    def register(self, capability: CapabilityKey, provider: Provider, coverage: CoverageSpec) -> None:
        self._entries.setdefault(capability, []).append(RegistryEntry(provider, coverage))

    def candidates(
        self,
        capability: CapabilityKey,
        *,
        season: str | None = None,
        competition: str | None = None,
    ) -> list[Provider]:
        """All registered providers whose declared coverage matches, best
        (lowest priority number) first. Empty list, never an exception —
        `resolve()` is the loud-failure entry point; `candidates()` is for
        callers that want to inspect options themselves."""
        entries = self._entries.get(capability, [])
        matching = [e for e in entries if e.coverage.matches(season=season, competition=competition)]
        matching.sort(key=lambda e: e.coverage.priority)
        return [e.provider for e in matching]

    def resolve(
        self,
        capability: CapabilityKey,
        *,
        season: str | None = None,
        competition: str | None = None,
    ) -> Provider:
        """Return the best candidate, LOGGING the selection and any
        fallback (blueprint §12.3). Raises `RegistryError` if nothing
        matches — an unresolved capability must fail loudly, never
        silently return something that can't actually answer the query."""
        candidates = self.candidates(capability, season=season, competition=competition)
        if not candidates:
            raise RegistryError(
                f"no provider registered for {capability} (season={season!r}, competition={competition!r})"
            )
        chosen = candidates[0]
        if len(candidates) > 1:
            logger.info(
                "capability %s: selected provider %r over %d fallback candidate(s) %r",
                capability,
                chosen.provider_id,
                len(candidates) - 1,
                [c.provider_id for c in candidates[1:]],
            )
        else:
            logger.debug("capability %s: resolved to sole provider %r", capability, chosen.provider_id)
        return chosen
