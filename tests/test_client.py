"""Tests for fplai.client — rate limiter, backoff, caching, typed accessors.
All network access is mocked; no live HTTP in this file."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fplai.client import (
    BACKOFF_TRIGGER_STATUSES,
    FPLApiError,
    FPLClient,
    RateLimiter,
    ResponseCache,
)


class FakeResponse:
    def __init__(self, status_code: int, body: dict | str):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)


def make_client(get_side_effect, tmp_path: Path, **kwargs) -> tuple[FPLClient, MagicMock]:
    session = MagicMock()
    session.headers = {}
    session.get.side_effect = get_side_effect
    client = FPLClient(
        session=session,
        rate_limiter=RateLimiter(sleep_fn=lambda s: None),
        cache=ResponseCache(cache_dir=tmp_path / "cache"),
        sleep_fn=lambda s: None,
        **kwargs,
    )
    return client, session


# -- RateLimiter -------------------------------------------------------


def test_rate_limiter_first_call_does_not_sleep():
    rl = RateLimiter(requests_per_second=2.0, sleep_fn=lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))
    slept = rl.wait()
    assert slept == 0.0


def test_rate_limiter_enforces_minimum_gap():
    sleeps = []
    times = iter([0.0, 0.0, 0.1, 0.1])  # wait() call 1: now=0.0 (start); call 2: now=0.1 (elapsed 0.1s)

    def time_fn():
        return next(times)

    rl = RateLimiter(
        requests_per_second=2.0,  # target interval 0.5s
        jitter_fraction=0.0,  # disable jitter for a deterministic assertion
        sleep_fn=lambda s: sleeps.append(s),
        time_fn=time_fn,
    )
    rl.wait()  # now=0.0 -> last_request_at set to 0.0 (second next())
    rl.wait()  # now=0.1 -> elapsed 0.1, remaining 0.4
    assert sleeps == [pytest.approx(0.4)]


def test_rate_limiter_applies_jitter_within_bounds():
    import random

    rng = random.Random(1)
    sleeps = []
    call_count = {"n": 0}

    def time_fn():
        # first wait(): now for elapsed check + now for last_request_at update
        # second wait(): now stays at same instant (0 elapsed) to force a full-interval sleep
        call_count["n"] += 1
        return 0.0

    rl = RateLimiter(requests_per_second=2.0, jitter_fraction=0.2, sleep_fn=lambda s: sleeps.append(s), time_fn=time_fn, rng=rng)
    rl.wait()
    rl.wait()
    assert len(sleeps) == 1
    # base interval 0.5s, jitter +/-20% -> [0.4, 0.6]
    assert 0.4 <= sleeps[0] <= 0.6


# -- backoff -------------------------------------------------------------


@pytest.mark.parametrize("status", BACKOFF_TRIGGER_STATUSES)
def test_backoff_retries_then_succeeds(tmp_path, status):
    attempts = {"n": 0}

    def get(url, timeout=30):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return FakeResponse(status, "blocked")
        return FakeResponse(200, {"ok": True})

    backoffs = []
    client, _ = make_client(get, tmp_path, backoff_base_seconds=1.0)
    client._sleep = lambda s: backoffs.append(s)

    result = client._get_json("fixtures/")
    assert result == {"ok": True}
    assert backoffs == [1.0, 2.0]  # exponential: base * 2^(attempt-1)
    assert attempts["n"] == 3


def test_backoff_exhausts_retries_and_raises(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(403, "forbidden")

    client, _ = make_client(get, tmp_path, backoff_base_seconds=0.0, max_retries=3)
    with pytest.raises(FPLApiError):
        client._get_json("fixtures/")


def test_non_backoff_error_status_raises_immediately(tmp_path):
    calls = {"n": 0}

    def get(url, timeout=30):
        calls["n"] += 1
        return FakeResponse(500, "server error")

    client, _ = make_client(get, tmp_path)
    with pytest.raises(FPLApiError):
        client._get_json("fixtures/")
    assert calls["n"] == 1  # 500 is not a backoff-trigger status; no retry loop


# -- caching -------------------------------------------------------------


def test_response_cache_avoids_second_network_call(tmp_path):
    calls = []

    def get(url, timeout=30):
        calls.append(url)
        return FakeResponse(200, {"elements": []})

    client, _ = make_client(get, tmp_path)
    client.bootstrap_static()
    client.bootstrap_static()
    assert len(calls) == 1


def test_force_refresh_bypasses_cache(tmp_path):
    calls = []

    def get(url, timeout=30):
        calls.append(url)
        return FakeResponse(200, {"elements": []})

    client, _ = make_client(get, tmp_path)
    client.bootstrap_static()
    client.bootstrap_static(force_refresh=True)
    assert len(calls) == 2


def test_transient_500_is_not_cached(tmp_path):
    calls = {"n": 0}

    def get(url, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(500, "oops")
        return FakeResponse(200, {"elements": []})

    client, _ = make_client(get, tmp_path)
    with pytest.raises(FPLApiError):
        client.bootstrap_static()
    data = client.bootstrap_static()
    assert data == {"elements": []}
    assert calls["n"] == 2  # the 500 must not have been cached


# -- typed accessors: entry_picks 404 handling ----------------------------


def test_entry_picks_404_returns_none_not_an_exception(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(404, "Not found")

    client, _ = make_client(get, tmp_path)
    result = client.entry_picks(12345, 1)
    assert result is None


def test_entry_picks_200_returns_parsed_json(tmp_path):
    payload = {"picks": [{"element": 1, "is_captain": True}], "active_chip": None}

    def get(url, timeout=30):
        return FakeResponse(200, payload)

    client, _ = make_client(get, tmp_path)
    result = client.entry_picks(1, 1)
    assert result == payload


def test_entry_returns_none_on_404(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(404, "not found")

    client, _ = make_client(get, tmp_path)
    assert client.entry(999_999_999) is None


def test_entry_picks_unexpected_error_status_raises(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(500, "server error")

    client, _ = make_client(get, tmp_path)
    with pytest.raises(FPLApiError):
        client.entry_picks(1, 1)


# -- Blocker 1: 503 ("The game is being updated.") is retryable ------------


def test_entry_picks_503_is_a_backoff_trigger_by_default(tmp_path):
    # Pre-fix, 503 was not in BACKOFF_TRIGGER_STATUSES at all: `_get` broke
    # out of the retry loop on the FIRST 503 and entry_picks raised
    # immediately, with zero retries. This proves 503 now goes through the
    # SAME retry-then-succeed path 429/403 already had.
    attempts = {"n": 0}

    def get(url, timeout=30):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return FakeResponse(503, "The game is being updated.")
        return FakeResponse(200, {"picks": [], "active_chip": None, "automatic_subs": [], "entry_history": {}})

    backoffs = []
    client, _ = make_client(get, tmp_path, backoff_base_seconds=1.0)
    client._sleep = lambda s: backoffs.append(s)

    result = client.entry_picks(1, 1)
    assert result is not None
    assert backoffs == [1.0, 2.0]
    assert attempts["n"] == 3


def test_entry_picks_503_exhausts_retries_and_raises_fplapierror(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(503, "The game is being updated.")

    client, _ = make_client(get, tmp_path, backoff_base_seconds=0.0, max_retries=2)
    with pytest.raises(FPLApiError):
        client.entry_picks(1, 1)


def test_503_is_not_cached(tmp_path):
    # `_get` only caches 200/404 — 503 must never become "sticky" the way a
    # real 200/404 does; this is what keeps a transient maintenance window
    # from poisoning the cache the way the 404-TTL bug did for a different
    # reason.
    calls = {"n": 0}

    def get(url, timeout=30):
        calls["n"] += 1
        return FakeResponse(503, "The game is being updated.")

    client, _ = make_client(get, tmp_path, backoff_base_seconds=0.0, max_retries=0)
    with pytest.raises(FPLApiError):
        client.entry_picks(1, 1)
    with pytest.raises(FPLApiError):
        client.entry_picks(1, 1)
    assert calls["n"] == 2  # no cache hit — both calls went live


# -- per-call max_retries override (readiness probe / bounded main-loop retry) --


def test_max_retries_override_makes_a_single_attempt_probe_fail_fast(tmp_path):
    calls = {"n": 0}

    def get(url, timeout=30):
        calls["n"] += 1
        return FakeResponse(503, "The game is being updated.")

    slept = []
    client, _ = make_client(get, tmp_path, backoff_base_seconds=60.0)
    client._sleep = lambda s: slept.append(s)
    with pytest.raises(FPLApiError):
        client.entry_picks(1, 1, max_retries=0)  # override: no retries at all
    assert calls["n"] == 1
    assert slept == []  # never slept — failed on the very first attempt


def test_max_retries_override_does_not_affect_the_client_default(tmp_path):
    # The override is per-call only; client.max_retries (used by every OTHER
    # call site that doesn't pass max_retries) must be unchanged.
    client, _ = make_client(lambda url, timeout=30: FakeResponse(200, {}), tmp_path, max_retries=3)
    client.entry_picks(1, 1, max_retries=0)
    assert client.max_retries == 3


# -- default cache wiring (blueprint §3.4 "Blocker 2") ----------------------


def test_default_client_wires_the_entry_404_ttl_into_its_own_cache():
    # Pure attribute check -- constructing FPLClient() does no I/O (it does
    # not touch disk or network until a request is actually made), so this
    # is safe to run without writing into the real cache/fpl_api/ directory.
    from fplai.client import DEFAULT_CACHE_DIR, ENTRY_404_TTL_SECONDS

    client = FPLClient()
    assert client.cache.cache_dir == DEFAULT_CACHE_DIR
    assert client.cache.max_age_for_404_seconds == ENTRY_404_TTL_SECONDS


def test_explicit_cache_argument_is_not_forced_onto_the_ttl_policy(tmp_path):
    # Existing behaviour, unaffected: a caller supplying its own ResponseCache
    # (every test in this file, via make_client()) is NOT silently opted
    # into the TTL — matches ResponseCache's own "None = unchanged" default.
    client, _ = make_client(lambda url, timeout=30: FakeResponse(200, {}), tmp_path)
    assert client.cache.max_age_for_404_seconds is None
