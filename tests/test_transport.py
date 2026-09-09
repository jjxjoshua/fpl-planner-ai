"""Tests for fplai.transport — shared rate/cache/backoff primitives (moved
here from client.py, behaviour unchanged) plus the new policy dataclasses,
quota/credit trackers, and the generic HttpTransport (E2b story 4). All
network access is mocked; no live HTTP in this file."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fplai.transport import (
    BulkFilePolicy,
    CreditPolicy,
    CreditTracker,
    DailyQuotaPolicy,
    FileCache,
    FileTransport,
    HttpTransport,
    QuotaTracker,
    RatePolicy,
    RateLimiter,
    ResponseCache,
    TransportError,
)


class FakeResponse:
    def __init__(self, status_code: int, body: dict | str, headers: dict | None = None):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode("utf-8")  # FileTransport reads .content (raw bytes)
        # `headers` deliberately only set when explicitly passed — proves
        # HttpTransport.get() uses getattr(..., None) rather than assuming
        # every response has one (most tests in this file never set it).
        if headers is not None:
            self.headers = headers


# -- RateLimiter / ResponseCache: parity with the old client.py behaviour --


def test_rate_limiter_first_call_does_not_sleep():
    rl = RateLimiter(requests_per_second=2.0, sleep_fn=lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))
    assert rl.wait() == 0.0


def test_response_cache_round_trips(tmp_path):
    cache = ResponseCache(tmp_path / "cache")
    assert cache.get("http://x") is None
    from fplai.transport import CacheEntry

    cache.set("http://x", CacheEntry(status_code=200, body="{}"))
    got = cache.get("http://x")
    assert got is not None
    assert got.status_code == 200 and got.body == "{}"


# -- ResponseCache 404 TTL (blueprint §3.4 "Blocker 2", session s003) ------
# Default behaviour (max_age_for_404_seconds=None) must stay byte-for-byte
# unchanged — every caller/test that does not opt in is unaffected.


def test_response_cache_404_without_ttl_never_expires(tmp_path):
    from fplai.transport import CacheEntry

    clock = {"t": 0.0}
    cache = ResponseCache(tmp_path / "cache", now_fn=lambda: clock["t"])
    cache.set("http://x", CacheEntry(status_code=404, body=""))
    clock["t"] = 10_000_000.0  # far in the future
    assert cache.get("http://x") is not None  # still a hit — no TTL configured


def test_response_cache_404_with_ttl_expires_after_max_age(tmp_path):
    from fplai.transport import CacheEntry

    clock = {"t": 0.0}
    cache = ResponseCache(tmp_path / "cache", max_age_for_404_seconds=3600.0, now_fn=lambda: clock["t"])
    cache.set("http://x", CacheEntry(status_code=404, body=""))
    clock["t"] = 3599.0
    assert cache.get("http://x") is not None  # still within TTL
    clock["t"] = 3601.0
    assert cache.get("http://x") is None  # stale -- a MISS


def test_response_cache_404_ttl_does_not_apply_to_200_entries(tmp_path):
    from fplai.transport import CacheEntry

    clock = {"t": 0.0}
    cache = ResponseCache(tmp_path / "cache", max_age_for_404_seconds=1.0, now_fn=lambda: clock["t"])
    cache.set("http://x", CacheEntry(status_code=200, body="{}"))
    clock["t"] = 1_000_000.0
    got = cache.get("http://x")
    assert got is not None and got.status_code == 200  # 200s are never TTL'd


def test_response_cache_404_with_no_stored_cached_at_is_treated_as_infinitely_stale(tmp_path):
    # The real incident: every 404 cached before this fix has NO cached_at
    # field at all (verified against the actual poisoned cache/fpl_api/
    # entries, s003 punch-card). A missing timestamp must invalidate, never
    # pass as fresh -- this is what neutralises the 9 pre-existing poisoned
    # entries without touching a single file on disk (cache/** is read-only
    # for this story).
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = ResponseCache(cache_dir, max_age_for_404_seconds=3600.0)
    import hashlib
    import json as _json

    url = "http://x/entry/123/"
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    (cache_dir / f"{key}.json").write_text(
        _json.dumps({"url": url, "status_code": 404, "body": ""}), encoding="utf-8"
    )  # pre-fix shape: no "cached_at" key
    assert cache.get(url) is None


def test_response_cache_set_respects_an_explicit_cached_at_for_tests(tmp_path):
    from fplai.transport import CacheEntry

    cache = ResponseCache(tmp_path / "cache", max_age_for_404_seconds=100.0, now_fn=lambda: 500.0)
    cache.set("http://x", CacheEntry(status_code=404, body="", cached_at=0.0))  # simulate an old write
    assert cache.get("http://x") is None  # 500 - 0 > 100 -- stale


# -- QuotaTracker ------------------------------------------------------


def test_quota_tracker_allows_up_to_the_daily_limit():
    tracker = QuotaTracker(requests_per_day=3, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    tracker.consume()
    tracker.consume()
    tracker.consume()
    assert tracker.remaining_today == 0


def test_quota_tracker_raises_when_exceeded():
    tracker = QuotaTracker(requests_per_day=2, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    tracker.consume()
    tracker.consume()
    with pytest.raises(TransportError):
        tracker.consume()


def test_quota_tracker_resets_on_a_new_day():
    day = {"d": 20}

    def now_fn():
        return datetime(2026, 8, day["d"], tzinfo=timezone.utc)

    tracker = QuotaTracker(requests_per_day=1, now_fn=now_fn)
    tracker.consume()
    with pytest.raises(TransportError):
        tracker.consume()
    day["d"] = 21
    tracker.consume()  # new day, budget reset — must not raise


# -- CreditTracker -------------------------------------------------------


def test_credit_tracker_enforces_monthly_budget():
    tracker = CreditTracker(monthly_credits=22, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    tracker.consume(10)  # anytime goalscorer, 10 fixtures
    tracker.consume(10)  # shots on target, 10 fixtures
    tracker.consume(2)  # h2h + totals
    assert tracker.remaining_this_month == 0
    with pytest.raises(TransportError):
        tracker.consume(1)


def test_credit_tracker_resets_on_a_new_month():
    month = {"m": 8}

    def now_fn():
        return datetime(2026, month["m"], 1, tzinfo=timezone.utc)

    tracker = CreditTracker(monthly_credits=5, now_fn=now_fn)
    tracker.consume(5)
    with pytest.raises(TransportError):
        tracker.consume(1)
    month["m"] = 9
    tracker.consume(5)  # new month, budget reset — must not raise


# -- CreditTracker/QuotaTracker durability (E2b story 8) ------------------
# `_spent`/`_count` were in-memory only — a fresh process (e.g. a scheduled
# task) never actually enforced the ceiling across restarts. `state_path`
# fixes that behind an UNCHANGED interface (consume()/remaining_* keep
# their exact signatures) — these tests prove the persistence, not a
# signature change.


def test_credit_tracker_persists_spend_across_a_new_instance_same_month(tmp_path):
    state_path = tmp_path / "credit_state.json"
    now_fn = lambda: datetime(2026, 8, 20, tzinfo=timezone.utc)  # noqa: E731

    t1 = CreditTracker(monthly_credits=10, now_fn=now_fn, state_path=state_path)
    t1.consume(7)
    assert t1.remaining_this_month == 3

    # Brand new instance, same state_path — simulates a fresh process
    # (scripts/snapshot_odds.py run as a scheduled task) picking up where
    # the last one left off, in the SAME month.
    t2 = CreditTracker(monthly_credits=10, now_fn=now_fn, state_path=state_path)
    assert t2.remaining_this_month == 3
    with pytest.raises(TransportError):
        t2.consume(4)  # 7 + 4 > 10 — pre-flight refuses BEFORE spending


def test_credit_tracker_persisted_state_resets_on_a_new_month(tmp_path):
    state_path = tmp_path / "credit_state.json"
    t1 = CreditTracker(monthly_credits=10, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc), state_path=state_path)
    t1.consume(10)

    # New instance, same file, but NOW is a new month — must NOT inherit
    # last month's exhausted budget.
    t2 = CreditTracker(monthly_credits=10, now_fn=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc), state_path=state_path)
    assert t2.remaining_this_month == 10
    t2.consume(10)  # must not raise


def test_credit_tracker_with_no_state_path_is_in_memory_only_unchanged_behaviour(tmp_path):
    # Default behaviour (state_path=None) must be byte-for-byte the same as
    # before this story — no file ever written, no cross-instance memory.
    t1 = CreditTracker(monthly_credits=10, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    t1.consume(10)
    t2 = CreditTracker(monthly_credits=10, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert t2.remaining_this_month == 10  # fresh, no memory of t1 at all
    assert not any(tmp_path.iterdir())  # nothing written anywhere


def test_quota_tracker_persists_count_across_a_new_instance_same_day(tmp_path):
    state_path = tmp_path / "quota_state.json"
    now_fn = lambda: datetime(2026, 8, 20, tzinfo=timezone.utc)  # noqa: E731
    t1 = QuotaTracker(requests_per_day=3, now_fn=now_fn, state_path=state_path)
    t1.consume(2)
    t2 = QuotaTracker(requests_per_day=3, now_fn=now_fn, state_path=state_path)
    assert t2.remaining_today == 1


def test_credit_tracker_reconcile_resyncs_to_the_providers_reported_spend():
    tracker = CreditTracker(monthly_credits=500, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    tracker.consume(10)
    assert tracker.remaining_this_month == 490
    # Provider says only 8 have actually been spent this month (e.g. a
    # different accounting of a prior partial run) — reconcile must trust
    # the PROVIDER's number, not ours.
    tracker.reconcile(remaining=492)
    assert tracker.remaining_this_month == 492


def test_credit_tracker_reconcile_logs_loudly_beyond_tolerance(caplog):
    tracker = CreditTracker(monthly_credits=500, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    tracker.consume(10)
    with caplog.at_level("ERROR", logger="fplai.transport"):
        tracker.reconcile(remaining=200, tolerance=2)  # reported spend=300, local=10 — way off
    assert any("diverges" in r.message for r in caplog.records)
    assert tracker.remaining_this_month == 200  # still resyncs even though it's loud


def test_credit_tracker_reconcile_within_tolerance_is_quiet(caplog):
    tracker = CreditTracker(monthly_credits=500, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    tracker.consume(10)
    with caplog.at_level("ERROR", logger="fplai.transport"):
        tracker.reconcile(remaining=489, tolerance=2)  # reported spend=11, local=10 — within tolerance
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_credit_tracker_reconcile_from_used_header_instead_of_remaining():
    tracker = CreditTracker(monthly_credits=500, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    tracker.consume(10)
    tracker.reconcile(used=12)
    assert tracker.remaining_this_month == 488


def test_credit_tracker_reconcile_noop_when_no_headers_supplied():
    tracker = CreditTracker(monthly_credits=500, now_fn=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))
    tracker.consume(10)
    tracker.reconcile()  # both None — must not touch _spent
    assert tracker.remaining_this_month == 490


# -- HttpTransport: policy dispatch ---------------------------------------


def _make_transport(policy, get_side_effect, tmp_path, **kwargs):
    session = MagicMock()
    session.headers = {}
    session.get.side_effect = get_side_effect
    return HttpTransport(
        base_url="https://example.test/api/",
        policy=policy,
        cache_dir=tmp_path / "cache",
        session=session,
        sleep_fn=lambda s: None,
        **kwargs,
    ), session


def test_http_transport_rate_policy_calls_rate_limiter(tmp_path):
    calls = []

    def get(url, timeout=30):
        calls.append(url)
        return FakeResponse(200, {"ok": True})

    transport, _ = _make_transport(RatePolicy(requests_per_second=2.0), get, tmp_path)
    status, body = transport.get("thing/")
    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert len(calls) == 1


def test_http_transport_caches_by_default(tmp_path):
    calls = []

    def get(url, timeout=30):
        calls.append(url)
        return FakeResponse(200, {"ok": True})

    transport, _ = _make_transport(RatePolicy(requests_per_second=2.0), get, tmp_path)
    transport.get("thing/")
    transport.get("thing/")
    assert len(calls) == 1


def test_http_transport_force_refresh_bypasses_cache(tmp_path):
    calls = []

    def get(url, timeout=30):
        calls.append(url)
        return FakeResponse(200, {"ok": True})

    transport, _ = _make_transport(RatePolicy(requests_per_second=2.0), get, tmp_path)
    transport.get("thing/")
    transport.get("thing/", force_refresh=True)
    assert len(calls) == 2


def test_http_transport_backs_off_then_succeeds(tmp_path):
    attempts = {"n": 0}

    def get(url, timeout=30):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return FakeResponse(429, "blocked")
        return FakeResponse(200, {"ok": True})

    backoffs = []
    transport, _ = _make_transport(
        RatePolicy(requests_per_second=2.0), get, tmp_path, backoff_base_seconds=1.0
    )
    transport._sleep = lambda s: backoffs.append(s)
    status, body = transport.get("thing/")
    assert status == 200
    assert backoffs == [1.0, 2.0]


def test_http_transport_exhausts_retries_and_raises(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(403, "forbidden")

    transport, _ = _make_transport(
        RatePolicy(requests_per_second=2.0), get, tmp_path, backoff_base_seconds=0.0, max_retries=2
    )
    with pytest.raises(TransportError):
        transport.get("thing/")


def test_http_transport_daily_quota_policy_enforced_before_request(tmp_path):
    calls = []

    def get(url, timeout=30):
        calls.append(url)
        return FakeResponse(200, {"ok": True})

    transport, _ = _make_transport(DailyQuotaPolicy(requests_per_day=1), get, tmp_path)
    transport.get("a/")
    with pytest.raises(TransportError):
        transport.get("b/")  # different URL -> not a cache hit, must consult quota
    assert len(calls) == 1


def test_http_transport_credit_policy_enforced_with_explicit_cost(tmp_path):
    calls = []

    def get(url, timeout=30):
        calls.append(url)
        return FakeResponse(200, {"ok": True})

    transport, _ = _make_transport(CreditPolicy(monthly_credits=10), get, tmp_path)
    transport.get("a/", cost=8)
    with pytest.raises(TransportError):
        transport.get("b/", cost=8)
    assert len(calls) == 1


def test_http_transport_credit_budget_survives_a_new_instance_same_cache_dir(tmp_path):
    """The E2b story 8 fix, exercised through HttpTransport rather than
    CreditTracker directly: `cache_dir` is the ONLY thing a caller supplies
    — state_path is derived automatically — so two HttpTransport instances
    built against the same cache_dir (simulating two separate process runs
    of scripts/snapshot_odds.py) must share the same spend, without any
    extra plumbing from the provider."""

    def get(url, timeout=30):
        return FakeResponse(200, {"ok": True})

    t1, _ = _make_transport(CreditPolicy(monthly_credits=10), get, tmp_path)
    t1.get("a/", cost=7, force_refresh=True)

    t2, _ = _make_transport(CreditPolicy(monthly_credits=10), get, tmp_path)
    assert t2._credits.remaining_this_month == 3
    with pytest.raises(TransportError):
        t2.get("b/", cost=4, force_refresh=True)  # 7 + 4 > 10


def test_http_transport_reconciles_credits_from_response_headers(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(200, {"ok": True}, headers={"x-requests-remaining": "486", "x-requests-used": "14"})

    transport, _ = _make_transport(CreditPolicy(monthly_credits=500), get, tmp_path)
    transport.get("a/", cost=2)
    # local spend was 2, but the provider says 14 have been used —
    # reconciliation must trust the provider's number.
    assert transport._credits.remaining_this_month == 486
    assert transport._last_response_headers.get("x-requests-remaining") == "486"


def test_http_transport_cache_hit_does_not_reconcile_or_touch_last_headers(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(200, {"ok": True}, headers={"x-requests-remaining": "486", "x-requests-used": "14"})

    transport, _ = _make_transport(CreditPolicy(monthly_credits=500), get, tmp_path)
    transport.get("a/", cost=2)  # live — reconciles to 486
    transport.get("a/", cost=2)  # cache hit — must not spend, must not reconcile again
    assert transport._credits.remaining_this_month == 486
    assert transport._last_response_headers == {}  # cleared on a cache hit


def test_http_transport_no_credit_headers_leaves_tracker_untouched(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(200, {"ok": True})  # no headers attribute at all

    transport, _ = _make_transport(CreditPolicy(monthly_credits=500), get, tmp_path)
    transport.get("a/", cost=3)
    assert transport._credits.remaining_this_month == 497  # pure local accounting, no reconciliation happened


def test_http_transport_bulk_file_policy_has_no_rate_limiter_or_quota(tmp_path):
    def get(url, timeout=30):
        return FakeResponse(200, {"ok": True})

    transport, _ = _make_transport(BulkFilePolicy(), get, tmp_path)
    status, _ = transport.get("a/")
    assert status == 200
    assert transport._rate_limiter is None
    assert transport._quota is None
    assert transport._credits is None


def test_http_transport_cache_hit_does_not_consume_quota(tmp_path):
    calls = []

    def get(url, timeout=30):
        calls.append(url)
        return FakeResponse(200, {"ok": True})

    transport, _ = _make_transport(DailyQuotaPolicy(requests_per_day=1), get, tmp_path)
    transport.get("a/")
    transport.get("a/")  # cache hit — must not touch the quota tracker
    assert len(calls) == 1
    assert transport._quota.remaining_today == 0


# -- FileCache -------------------------------------------------------------


def test_file_cache_round_trips_raw_bytes(tmp_path):
    cache = FileCache(tmp_path / "cache")
    assert cache.get("http://x/file.csv") is None
    cache.set("http://x/file.csv", b"id,name\n1,a\n")
    assert cache.get("http://x/file.csv") == b"id,name\n1,a\n"


# -- FileTransport (E2b story 10, blueprint §12.4's BulkFilePolicy row) ----


def _make_file_transport(get_side_effect, tmp_path, **kwargs):
    session = MagicMock()
    session.headers = {}
    session.get.side_effect = get_side_effect
    return FileTransport(cache_dir=tmp_path / "cache", policy=BulkFilePolicy(), session=session, sleep_fn=lambda s: None, **kwargs), session


def test_file_transport_rejects_a_non_bulk_file_policy(tmp_path):
    with pytest.raises(TypeError):
        FileTransport(cache_dir=tmp_path / "cache", policy=RatePolicy(requests_per_second=2.0))


def test_file_transport_fetches_and_caches_a_remote_file(tmp_path):
    calls = []

    def get(url, timeout=60):
        calls.append(url)
        return FakeResponse(200, "id,name\n1,Arsenal\n")

    transport, _ = _make_file_transport(get, tmp_path)
    raw = transport.fetch("https://example.test/data.csv")
    assert raw == b"id,name\n1,Arsenal\n"
    assert len(calls) == 1
    # Second fetch of the SAME url — download-once, read-many: no second
    # live request, not even a disk read (in-memory cache hit).
    transport.fetch("https://example.test/data.csv")
    assert len(calls) == 1


def test_file_transport_disk_cache_survives_a_new_instance(tmp_path):
    calls = []

    def get(url, timeout=60):
        calls.append(url)
        return FakeResponse(200, "id,name\n1,Arsenal\n")

    cache_dir = tmp_path / "cache"
    session = MagicMock()
    session.headers = {}
    session.get.side_effect = get
    t1 = FileTransport(cache_dir=cache_dir, policy=BulkFilePolicy(), session=session, sleep_fn=lambda s: None)
    t1.fetch("https://example.test/data.csv")

    # Brand new FileTransport instance, same cache_dir, same URL — the disk
    # cache (not the in-memory one, which is per-instance) must satisfy
    # this without a second live request. "Be polite to GitHub."
    session2 = MagicMock()
    session2.headers = {}
    session2.get.side_effect = get
    t2 = FileTransport(cache_dir=cache_dir, policy=BulkFilePolicy(), session=session2, sleep_fn=lambda s: None)
    raw = t2.fetch("https://example.test/data.csv")
    assert raw == b"id,name\n1,Arsenal\n"
    assert len(calls) == 1


def test_file_transport_force_refresh_bypasses_both_caches(tmp_path):
    calls = []

    def get(url, timeout=60):
        calls.append(url)
        return FakeResponse(200, f"call={len(calls)}")

    transport, _ = _make_file_transport(get, tmp_path)
    transport.fetch("https://example.test/data.csv")
    transport.fetch("https://example.test/data.csv", force_refresh=True)
    assert len(calls) == 2


def test_file_transport_raises_on_non_200(tmp_path):
    def get(url, timeout=60):
        return FakeResponse(404, "not found")

    transport, _ = _make_file_transport(get, tmp_path, max_retries=0)
    with pytest.raises(TransportError):
        transport.fetch("https://example.test/missing.csv")


def test_file_transport_backs_off_then_succeeds(tmp_path):
    attempts = {"n": 0}

    def get(url, timeout=60):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return FakeResponse(429, "blocked")
        return FakeResponse(200, "ok")

    backoffs = []
    transport, _ = _make_file_transport(get, tmp_path, backoff_base_seconds=1.0)
    transport._sleep = lambda s: backoffs.append(s)
    raw = transport.fetch("https://example.test/data.csv")
    assert raw == b"ok"
    assert backoffs == [1.0, 2.0]


def test_file_transport_reads_a_local_file_without_caching_or_network(tmp_path):
    local_file = tmp_path / "local.csv"
    local_file.write_bytes(b"id,name\n1,Local\n")

    def get(url, timeout=60):
        raise AssertionError("must not make a network call for a local path")

    transport, _ = _make_file_transport(get, tmp_path / "cache_dir_unused")
    raw = transport.fetch(str(local_file))
    assert raw == b"id,name\n1,Local\n"


def test_file_transport_raises_on_missing_local_path(tmp_path):
    def get(url, timeout=60):
        raise AssertionError("must not make a network call for a local path")

    transport, _ = _make_file_transport(get, tmp_path / "cache_dir_unused")
    with pytest.raises(TransportError):
        transport.fetch(str(tmp_path / "does_not_exist.csv"))
