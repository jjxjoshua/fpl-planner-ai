"""Tests for fplai.registry — the capability registry (E2b story 2,
blueprint §12.1/§12.3). No network; providers are trivial fakes."""

from __future__ import annotations

import logging

import pytest

from fplai.registry import CapabilityRegistry, CoverageSpec, RegistryError
from fplai.schemas import CapabilityKey
from fplai.transport import RatePolicy


class FakeProvider:
    def __init__(self, provider_id: str):
        self.provider_id = provider_id
        self.policy = RatePolicy(requests_per_second=2.0)

    def supports(self, capability, *, season=None, competition=None):
        return True

    def fetch(self, capability, **params):
        raise NotImplementedError


MATCH_GRAIN = CapabilityKey("player", "defensive_actions", "match")
SEASON_GRAIN = CapabilityKey("player", "defensive_actions", "season")


def test_resolve_raises_for_an_unregistered_capability():
    registry = CapabilityRegistry()
    with pytest.raises(RegistryError):
        registry.resolve(MATCH_GRAIN)


def test_grain_is_respected_a_season_provider_never_answers_a_match_query():
    # blueprint §12.1: the exact bug this design exists to prevent.
    registry = CapabilityRegistry()
    season_only = FakeProvider("season_only")
    registry.register(SEASON_GRAIN, season_only, CoverageSpec())
    with pytest.raises(RegistryError):
        registry.resolve(MATCH_GRAIN)  # different key entirely — must not fall back


def test_resolve_returns_sole_candidate():
    registry = CapabilityRegistry()
    provider = FakeProvider("only_one")
    registry.register(MATCH_GRAIN, provider, CoverageSpec())
    assert registry.resolve(MATCH_GRAIN) is provider


def test_resolve_prefers_lower_priority_number():
    registry = CapabilityRegistry()
    cheap_and_good = FakeProvider("preferred")
    paid_fallback = FakeProvider("fallback")
    registry.register(MATCH_GRAIN, paid_fallback, CoverageSpec(priority=100))
    registry.register(MATCH_GRAIN, cheap_and_good, CoverageSpec(priority=10))
    assert registry.resolve(MATCH_GRAIN) is cheap_and_good


def test_coverage_season_filter_excludes_non_matching_season():
    registry = CapabilityRegistry()
    provider = FakeProvider("scoped")
    registry.register(MATCH_GRAIN, provider, CoverageSpec(seasons=frozenset({"2022-23", "2023-24"})))
    assert registry.candidates(MATCH_GRAIN, season="2026-27") == []
    assert registry.candidates(MATCH_GRAIN, season="2022-23") == [provider]


def test_coverage_seasons_none_matches_any_requested_season():
    # The FPL API case: no season parameter at all, always current.
    registry = CapabilityRegistry()
    provider = FakeProvider("live_only")
    registry.register(MATCH_GRAIN, provider, CoverageSpec(seasons=None))
    assert registry.candidates(MATCH_GRAIN, season="2026-27") == [provider]
    assert registry.candidates(MATCH_GRAIN, season=None) == [provider]


def test_coverage_competition_filter():
    registry = CapabilityRegistry()
    provider = FakeProvider("pl_only")
    registry.register(MATCH_GRAIN, provider, CoverageSpec(competitions=frozenset({"PL"})))
    assert registry.candidates(MATCH_GRAIN, competition="UCL") == []
    assert registry.candidates(MATCH_GRAIN, competition="PL") == [provider]


def test_fallback_selection_is_logged(caplog):
    registry = CapabilityRegistry()
    preferred = FakeProvider("preferred")
    fallback = FakeProvider("fallback")
    registry.register(MATCH_GRAIN, fallback, CoverageSpec(priority=100))
    registry.register(MATCH_GRAIN, preferred, CoverageSpec(priority=10))
    with caplog.at_level(logging.INFO, logger="fplai.registry"):
        chosen = registry.resolve(MATCH_GRAIN)
    assert chosen is preferred
    assert any("selected provider" in r.message and "fallback" in r.message for r in caplog.records)


def test_candidates_returns_empty_list_not_an_exception():
    registry = CapabilityRegistry()
    assert registry.candidates(MATCH_GRAIN) == []
