"""Transport — story 4 of E2b (blueprint §12.4).

Per-provider network mechanics: rate limiting OR quota accounting OR
credit accounting (never more than one applies to a given provider), disk
caching, and bounded backoff. This module is deliberately silent about
*what* is being fetched — that is the provider adapter's job
(`providers/fpl.py`, and future `providers/*.py`). A provider need not be
an HTTP API at all (an archive adapter reads a bulk file), so nothing in
`providers/base.py`'s `Provider` interface requires a transport of any
kind — this module is opt-in infrastructure for providers that need it.

`RateLimiter`, `ResponseCache` and `CacheEntry` were lifted verbatim out of
`client.py` (behaviour unchanged, byte-for-byte identical bodies) so there
is exactly one implementation of each, shared by any future HTTP provider
instead of being copy-pasted per adapter. `client.py` re-exports them for
backwards compatibility — existing imports (`from fplai.client import
RateLimiter, ResponseCache`) keep working unchanged.

Four policy shapes (design note, blueprint §12.4 table) — a union, not one
dataclass with a pile of nullable fields, so each provider's constraint
shape is representable and a type checker can narrow on it:

  - `RatePolicy`        requests/sec, jittered            (FPL API, PL API)
  - `DailyQuotaPolicy`   fixed requests/day                 (API-Football)
  - `CreditPolicy`       monthly credits, cost varies/call  (The Odds API)
  - `BulkFilePolicy`     no limit, not HTTP at all           (archives)

`HttpTransport` is a generic per-provider HTTP engine dispatching on the
policy it's given (rate limiter, and/or quota/credit tracker). It is NOT
wired into `FPLClient` in this slice — see `providers/fpl.py`'s module
docstring for why porting `FPLClient`'s own request loop onto this shared
engine was deliberately deferred rather than risked during the pre-GW1
snapshotter window. `HttpTransport` exists, is independently tested here,
and is what a future HTTP-based adapter (PL API, API-Football, The Odds
API — stories 6/8) should build on directly instead of reimplementing a
request loop from scratch.

`FileTransport` (E2b story 10, blueprint §12.4's `BulkFilePolicy` row) is
the first NON-HTTP-request-shaped transport: fetch-and-cache a remote bulk
file (a season's worth of CSV in one GET, e.g. vaastav's `gws/gw{N}.csv`),
or read one already on local disk. Bulk archive sources are
**download-once, read-many**: one file answers thousands of per-entity
queries, so `FileTransport` caches by URL (content-hashed, on disk,
persisted across process restarts) rather than per-query — the same
"cache aggressively, never re-fetch what you already have" discipline
`ResponseCache` gives `HttpTransport`, applied to whole files instead of
individual JSON responses. `FileTransport` deliberately does NOT decode
what it fetches — decoding (`csv` -> `pl.DataFrame`, `json` -> a parsed
object) is `decoders.py`'s job, a genuinely separate axis: a provider
composes a transport (WHERE the bytes come from) and a decoder (WHAT SHAPE
they're in), and neither module needs to know the other exists. See
`providers/vaastav.py` for the worked example.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import requests

logger = logging.getLogger("fplai.transport")

# -- shared HTTP primitives (moved from client.py, unchanged) --------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

BACKOFF_BASE_SECONDS = 60.0
BACKOFF_TRIGGER_STATUSES = (429, 403)
MAX_RETRIES = 3


class TransportError(RuntimeError):
    """A transport-level failure: retries exhausted, or a policy budget
    (daily quota / monthly credits) would be exceeded by this call."""


class RateLimiter:
    """Enforces an average request rate with jitter, single connection.

    Sleeps *before* each request so that the gap since the previous
    request's start is at least `1 / requests_per_second`, jittered by
    +/-`jitter_fraction`. Deliberately simple (no token bucket, no burst
    allowance) — a burst is exactly what a rate ceiling forbids.
    """

    def __init__(
        self,
        requests_per_second: float = 1.0,
        jitter_fraction: float = 0.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.monotonic,
        rng=None,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        import random

        self._base_interval = 1.0 / requests_per_second
        self._jitter_fraction = jitter_fraction
        self._sleep = sleep_fn
        self._now = time_fn
        self._rng = rng or random.Random()
        self._last_request_at: float | None = None

    def wait(self) -> float:
        """Block until it is safe to issue the next request. Returns the
        seconds actually slept (for testability)."""
        jitter = 1.0 + self._rng.uniform(-self._jitter_fraction, self._jitter_fraction)
        target_interval = self._base_interval * jitter

        now = self._now()
        if self._last_request_at is None:
            slept = 0.0
        else:
            elapsed = now - self._last_request_at
            remaining = target_interval - elapsed
            slept = max(0.0, remaining)
            if slept > 0:
                self._sleep(slept)

        self._last_request_at = self._now()
        return slept


@dataclass(frozen=True)
class CacheEntry:
    status_code: int
    body: str  # raw response text; "" for a cached 404
    # Epoch seconds this entry was written, for the 404-TTL policy below.
    # Production call sites (client.py's `_get`, HttpTransport.get) never
    # set this themselves — `ResponseCache.set()` always stamps its OWN
    # `now()` when it is None, so no caller has to remember anything.
    # Tests set it explicitly to simulate an old entry without sleeping.
    cached_at: float | None = None


class ResponseCache:
    """Disk cache keyed by URL. One JSON file per URL hash under
    `cache_dir`. Caches both 200 and 404 responses.

    **404-TTL policy — pre-deadline gate-repair session s003 (blueprint
    §3.4 "Blocker 2").** The docstring above used to say a 404 is
    "meaningful and stable" unconditionally. That is true for a genuinely
    immutable-absent resource, and FALSE for `entry/{id}/` ("not issued
    yet") and `entry/{id}/event/{gw}/picks/` ("not public yet") — for both,
    404 is a TEMPORAL state of a resource that later becomes 200, not a
    permanent property. Caching it forever silently biases anything that
    replays the cache later: verified live, 9 real `entry/{id}/` 404s
    cached 19-21 Aug would otherwise answer `find_max_valid_entry_id`'s
    binary search **from cache, at zero live requests**, on the Tue 25 Aug
    EO sample — returning the max entry id as of 19-21 Aug and silently
    excluding everyone who registered since, exactly when the player base
    grows fastest.

    `max_age_for_404_seconds` (default `None` = UNCHANGED behaviour, cache
    404s forever — every existing caller/test that does not opt in is
    unaffected) makes a cached 404 older than the TTL invisible to `get()`:
    it returns `None` (a cache MISS) instead, so the caller falls through
    to its normal live-fetch path with ZERO code change at the call site —
    this is the "fix at the policy layer, not force_refresh sprinkled at
    call sites" requirement. `FPLClient`'s own default cache (client.py)
    enables this; other providers' `ResponseCache`/`HttpTransport`
    instances keep the old forever-cache behaviour unless they opt in too
    — deliberately not changed here, since no live evidence was gathered
    this session that another provider's 404s are similarly temporal.

    **A 404 entry with no stored `cached_at` at all (every file written by
    this class before this fix — verified: the real 9 poisoned entries in
    `cache/fpl_api/` are exactly this shape, `{"url":..., "status_code":
    404, "body": ...}`, no timestamp key) is treated as INFINITELY OLD, not
    infinitely fresh** — this is what invalidates the 9 pre-existing
    poisoned entries structurally, without touching a single cache file on
    disk (which is read-only for this story; `cache/**` may not be
    written or deleted here).

    `cache_dir` has no default — every provider must say where its own
    cache lives; sharing one directory across providers would let two
    providers' URL hashes collide by construction."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        max_age_for_404_seconds: float | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_age_for_404_seconds = max_age_for_404_seconds
        self._now = now_fn

    def _path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def get(self, url: str) -> CacheEntry | None:
        path = self._path(url)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        entry = CacheEntry(
            status_code=payload["status_code"],
            body=payload["body"],
            cached_at=payload.get("cached_at"),
        )
        if entry.status_code == 404 and self.max_age_for_404_seconds is not None:
            age = None if entry.cached_at is None else self._now() - entry.cached_at
            if age is None or age > self.max_age_for_404_seconds:
                return None  # stale (or pre-TTL-fix, unstamped) 404 -- a MISS, not a hit
        return entry

    def set(self, url: str, entry: CacheEntry) -> None:
        path = self._path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        cached_at = entry.cached_at if entry.cached_at is not None else self._now()
        path.write_text(
            json.dumps(
                {"url": url, "status_code": entry.status_code, "body": entry.body, "cached_at": cached_at}
            ),
            encoding="utf-8",
        )


# -- policy: the four dimensions our providers actually need ---------------


@dataclass(frozen=True)
class RatePolicy:
    """requests/sec ceiling, single connection, jittered. FPL API, PL API."""

    requests_per_second: float
    jitter_fraction: float = 0.20


@dataclass(frozen=True)
class DailyQuotaPolicy:
    """Fixed requests/day (e.g. API-Football free tier: 100/day, and
    separately 10/min — `requests_per_second` covers the latter if given)."""

    requests_per_day: int
    requests_per_second: float | None = None


@dataclass(frozen=True)
class CreditPolicy:
    """Monthly credit budget where cost varies per call (The Odds API: cost
    = regions x markets). Not auto-computed — the caller knows the cost of
    its own request shape and passes it to `HttpTransport.get(..., cost=N)`
    / `CreditTracker.consume(cost)`; `cost_per_call_description` is
    documentation for humans, not an enforced formula."""

    monthly_credits: int
    cost_per_call_description: str = ""


@dataclass(frozen=True)
class BulkFilePolicy:
    """No per-request limit — a bulk file or dump (vaastav, olbauday). Not
    an HTTP policy at all; a provider using this typically constructs no
    `HttpTransport` in the first place."""

    description: str = "bulk file, no rate limit"


TransportPolicy = RatePolicy | DailyQuotaPolicy | CreditPolicy | BulkFilePolicy


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class QuotaTracker:
    """Enforces a daily request quota (e.g. API-Football free: 100/day).
    Resets at UTC midnight. Raises rather than silently over-spending — a
    provider that quietly exceeds its quota risks a ban, which is a worse
    failure than a loud stop (CLAUDE.md: back off on any sign of trouble).

    **Durability, added E2b story 8 (blueprint §12.4).** `_count` was
    in-memory only, so it reset to 0 on every process restart — for a
    provider run as a scheduled task (a fresh process each invocation),
    the daily ceiling would never actually bind. `state_path`, if given, is
    a small JSON file (`{"period": "YYYY-MM-DD", "count": N}`), read at
    construction and rewritten after every successful `consume()` — the
    same "durable, on disk, one file, no daemon" discipline `FileCache`
    already uses. `state_path=None` (the default) preserves the exact
    original in-memory-only behaviour — every existing caller/test is
    unaffected. This is a defect fix behind an UNCHANGED interface: no
    method gained or lost a required argument, `consume()`/
    `remaining_today` keep their exact signatures."""

    requests_per_day: int
    now_fn: Callable[[], datetime] = field(default=_utcnow, repr=False)
    state_path: Path | None = field(default=None, repr=False)
    _date: str | None = field(default=None, init=False, repr=False)
    _count: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._load_persisted()

    def _load_persisted(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # corrupt/unreadable state file — start fresh rather than crash
        today = self.now_fn().strftime("%Y-%m-%d")
        if payload.get("period") == today:
            self._date = today
            self._count = int(payload.get("count", 0))

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"period": self._date, "count": self._count}), encoding="utf-8")

    def _roll_if_new_day(self) -> str:
        today = self.now_fn().strftime("%Y-%m-%d")
        if today != self._date:
            self._date = today
            self._count = 0
        return today

    def consume(self, n: int = 1) -> None:
        """Pre-flight enforcement: raises BEFORE the caller's request is
        allowed to proceed if it would breach the daily ceiling — never
        after the fact."""
        today = self._roll_if_new_day()
        if self._count + n > self.requests_per_day:
            raise TransportError(
                f"daily quota exceeded: {self._count + n} > {self.requests_per_day} requests on {today}"
            )
        self._count += n
        self._persist()

    @property
    def remaining_today(self) -> int:
        self._roll_if_new_day()
        return self.requests_per_day - self._count


@dataclass
class CreditTracker:
    """Enforces a monthly credit budget where cost varies per call (The
    Odds API). Resets at UTC month start.

    **Durability, added E2b story 8 (blueprint §12.4).** Same fix as
    `QuotaTracker` above, for the same reason: `scripts/snapshot_odds.py`
    runs as a scheduled task, a fresh process every invocation, so an
    in-memory-only `_spent` would never actually bind the 500-credit
    monthly ceiling — a latent ban risk the transport module's own
    docstring already warns about. `state_path`, if given, persists
    `{"period": "YYYY-MM", "spent": N}` to one JSON file, read at
    construction and rewritten after every `consume()`/`reconcile()`.
    `state_path=None` (default) is the original in-memory-only behaviour,
    unchanged — every existing test/caller is unaffected. Interface is
    unchanged: `consume(cost)` and `remaining_this_month` keep their exact
    signatures; `reconcile()` is a new method, not a change to an existing
    one."""

    monthly_credits: int
    now_fn: Callable[[], datetime] = field(default=_utcnow, repr=False)
    state_path: Path | None = field(default=None, repr=False)
    _month: str | None = field(default=None, init=False, repr=False)
    _spent: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._load_persisted()

    def _load_persisted(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # corrupt/unreadable state file — start fresh rather than crash
        month = self.now_fn().strftime("%Y-%m")
        if payload.get("period") == month:
            self._month = month
            self._spent = int(payload.get("spent", 0))

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"period": self._month, "spent": self._spent}), encoding="utf-8")

    def _roll_if_new_month(self) -> str:
        month = self.now_fn().strftime("%Y-%m")
        if month != self._month:
            self._month = month
            self._spent = 0
        return month

    def consume(self, cost: int) -> None:
        """Pre-flight enforcement (blueprint §12.4's "provider that quietly
        exceeds its quota risks a ban"): raises BEFORE the request that
        would breach the monthly budget is allowed to proceed."""
        month = self._roll_if_new_month()
        if self._spent + cost > self.monthly_credits:
            raise TransportError(
                f"monthly credit budget exceeded: {self._spent + cost} > {self.monthly_credits} credits in {month}"
            )
        self._spent += cost
        self._persist()

    @property
    def remaining_this_month(self) -> int:
        self._roll_if_new_month()
        return self.monthly_credits - self._spent

    def reconcile(self, *, remaining: int | None = None, used: int | None = None, tolerance: int = 2) -> None:
        """Reconciliation (the second of the two defences E2b story 8
        requires, alongside `consume()`'s pre-flight check): The Odds API
        returns `x-requests-remaining`/`x-requests-used` response headers
        on every live call — unlike the FPL API, which has no rate-limit
        headers at all. This compares OUR local count against the
        PROVIDER's own, authoritative one.

        A divergence beyond `tolerance` credits means OUR accounting is
        wrong somewhere (a call made outside this tracker/process, a
        miscounted `cost`, a lost or corrupted state file) — surfaced
        LOUDLY (an ERROR-level log naming both numbers), never silently
        trusted over the provider's number, per this story's brief. Does
        NOT raise: halting a whole run over what might be a small,
        explainable skew (e.g. another process/session also spending from
        the same account) would be a worse failure than logging and
        moving on with the corrected number. Either way, the local counter
        is RESYNCED to the provider's reported value — after this call,
        `remaining_this_month` reflects what the provider actually says is
        left, not our possibly-stale guess."""
        self._roll_if_new_month()
        if remaining is None and used is None:
            return
        reported_spent = (self.monthly_credits - remaining) if remaining is not None else used
        diff = abs(reported_spent - self._spent)
        if diff > tolerance:
            logger.error(
                "CreditTracker: local spend (%d) diverges from the provider's own accounting "
                "(reported spent=%d via response headers, tolerance=%d credits) — our local "
                "count is wrong. Resyncing to the PROVIDER's number, which is authoritative. "
                "Investigate: a call made outside this tracker/process, a miscounted `cost` "
                "argument, or a lost/corrupted persisted state file are the likely causes.",
                self._spent,
                reported_spent,
                tolerance,
            )
        elif diff > 0:
            logger.info(
                "CreditTracker: reconciled against provider headers (local=%d, reported=%d, "
                "diff=%d, within tolerance=%d)",
                self._spent,
                reported_spent,
                diff,
                tolerance,
            )
        self._spent = reported_spent
        self._persist()


class HttpTransport:
    """Generic per-provider HTTP transport: dispatches on `policy` to
    enforce a rate ceiling, a daily quota, or a monthly credit budget (never
    more than one), always through the same disk cache + bounded-backoff
    request path. Not used by `FPLClient` in this slice (see module
    docstring) — ready for the next HTTP-based adapter to use directly.
    """

    def __init__(
        self,
        base_url: str,
        *,
        policy: TransportPolicy,
        cache_dir: Path,
        session: "requests.Session | None" = None,
        user_agent: str = DEFAULT_USER_AGENT,
        max_retries: int = MAX_RETRIES,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
        backoff_trigger_statuses: tuple[int, ...] = BACKOFF_TRIGGER_STATUSES,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        import requests

        cache_dir = Path(cache_dir)
        self.base_url = base_url
        self.policy = policy
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self.cache = ResponseCache(cache_dir)
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_trigger_statuses = backoff_trigger_statuses
        self._sleep = sleep_fn

        self._rate_limiter: RateLimiter | None = None
        self._quota: QuotaTracker | None = None
        self._credits: CreditTracker | None = None
        # Diagnostic only, not part of get()'s return contract — the raw
        # headers of the most recent LIVE response (empty dict on a cache
        # hit, since ResponseCache doesn't store headers). Set here so a
        # caller that wants more than (status, text) can inspect it right
        # after calling get(), without get()'s own signature changing —
        # see QuotaTracker/CreditTracker's docstrings ("durability behind
        # an unchanged interface") for why this is deliberately NOT a third
        # return value.
        self._last_response_headers: dict[str, str] = {}

        if isinstance(policy, RatePolicy):
            self._rate_limiter = RateLimiter(
                requests_per_second=policy.requests_per_second,
                jitter_fraction=policy.jitter_fraction,
                sleep_fn=sleep_fn,
            )
        elif isinstance(policy, DailyQuotaPolicy):
            # state_path is DERIVED from cache_dir, not passed by the
            # caller — every HTTP provider that uses a DailyQuotaPolicy
            # gets durable quota accounting automatically, the same way
            # every provider gets disk caching automatically via
            # ResponseCache(cache_dir). Sibling file, not inside cache_dir
            # itself, so it's never mistaken for a cached response by
            # anything that globs that directory.
            self._quota = QuotaTracker(
                requests_per_day=policy.requests_per_day,
                state_path=cache_dir / "_quota_tracker_state.json",
            )
            if policy.requests_per_second:
                self._rate_limiter = RateLimiter(requests_per_second=policy.requests_per_second, sleep_fn=sleep_fn)
        elif isinstance(policy, CreditPolicy):
            # Same automatic-durability reasoning as DailyQuotaPolicy above
            # — this is the E2b story 8 fix: CreditTracker was in-memory
            # only, so a provider run as a scheduled task (fresh process
            # every invocation, e.g. scripts/snapshot_odds.py) would never
            # actually have its monthly ceiling enforced across restarts.
            self._credits = CreditTracker(
                monthly_credits=policy.monthly_credits,
                state_path=cache_dir / "_credit_tracker_state.json",
            )
        elif isinstance(policy, BulkFilePolicy):
            pass  # nothing to enforce
        else:
            raise TypeError(f"unknown TransportPolicy: {policy!r}")

    def get(self, path: str, *, force_refresh: bool = False, cost: int = 1) -> tuple[int, str]:
        """GET `path` (relative to base_url). Returns (status_code, text) —
        UNCHANGED signature and return shape (E2b story 8's "durability
        behind an unchanged interface" — see QuotaTracker/CreditTracker's
        docstrings). Cache-first unless `force_refresh`. Consults the
        quota/credit tracker (if any) BEFORE issuing a live request — a
        cache hit never spends budget, and never reconciles (there is no
        live response to reconcile against). Retries with exponential
        backoff on the configured trigger statuses, up to `max_retries`."""
        url = self.base_url + path.lstrip("/")

        if not force_refresh:
            cached = self.cache.get(url)
            if cached is not None:
                self._last_response_headers = {}
                return cached.status_code, cached.body

        if self._quota is not None:
            self._quota.consume(1)
        if self._credits is not None:
            self._credits.consume(cost)

        attempt = 0
        while True:
            if self._rate_limiter is not None:
                self._rate_limiter.wait()
            response = self.session.get(url, timeout=30)

            if response.status_code not in self.backoff_trigger_statuses:
                break

            attempt += 1
            if attempt > self.max_retries:
                raise TransportError(
                    f"GET {url} failed after {self.max_retries} retries (last status {response.status_code})"
                )
            backoff = self.backoff_base_seconds * (2 ** (attempt - 1))
            self._sleep(backoff)

        # getattr, not response.headers directly: several existing tests'
        # fake response objects (e.g. test_transport.py's FakeResponse) only
        # define status_code/text/content, no .headers — a hard attribute
        # access here would break every one of them for a diagnostic/
        # reconciliation feature they never exercise. A real requests.
        # Response always has .headers.
        headers = getattr(response, "headers", None) or {}
        self._last_response_headers = dict(headers)
        if self._credits is not None:
            self._reconcile_credits_from_headers(headers)

        status_code, text = response.status_code, response.text
        if status_code == 200 or status_code == 404:
            self.cache.set(url, CacheEntry(status_code=status_code, body=text))
        return status_code, text

    def _reconcile_credits_from_headers(self, headers) -> None:
        """The Odds API's `x-requests-remaining`/`x-requests-used` response
        headers, if present, are handed straight to `CreditTracker.
        reconcile()` — general infrastructure for ANY CreditPolicy
        provider whose API happens to report its own usage this way, not
        Odds-specific code baked into the transport layer. A provider
        whose API doesn't send these headers (none currently do besides
        Odds) simply never triggers this — `headers.get()` returns None,
        `reconcile()` no-ops on `remaining is None and used is None`."""
        remaining_raw = headers.get("x-requests-remaining")
        used_raw = headers.get("x-requests-used")
        if remaining_raw is None and used_raw is None:
            return
        try:
            remaining = int(remaining_raw) if remaining_raw is not None else None
            used = int(used_raw) if used_raw is not None else None
        except (TypeError, ValueError):
            logger.warning(
                "credit reconciliation headers present but not parseable as integers: "
                "x-requests-remaining=%r x-requests-used=%r",
                remaining_raw,
                used_raw,
            )
            return
        self._credits.reconcile(remaining=remaining, used=used)


def _looks_like_url(location: str) -> bool:
    """`True` for `http://`/`https://`; `False` for a local filesystem path
    (absolute, relative, or `file://` — normalised to a plain path by the
    caller). Deliberately simple: this module only ever sees two shapes,
    never e.g. `s3://`."""
    return location.startswith("http://") or location.startswith("https://")


class FileCache:
    """Disk cache for whole-file bytes, keyed by URL content hash — the
    bulk-file analogue of `ResponseCache`. NOT reused as-is: `ResponseCache`
    JSON-wraps a text body (`{"status_code":.., "body": "..."}"`), which is
    fine for a JSON API response but would force every cached CSV through
    a JSON-escape/unescape round trip for no benefit (bulk files here are
    already raw bytes, potentially several MB, and only ever a `200` or a
    raised error — there is no meaningful "cached 404" the way a per-entity
    picks/ 404 is meaningful). One `.bin` file per URL hash instead."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)

    def _path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.bin"

    def get(self, url: str) -> bytes | None:
        path = self._path(url)
        if not path.exists():
            return None
        return path.read_bytes()

    def set(self, url: str, raw: bytes) -> None:
        path = self._path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


class FileTransport:
    """Fetch-and-cache a remote bulk file, or read a local one — the
    non-HTTP-request-shaped transport blueprint §12.4 requires ("a provider
    need not be an HTTP API at all"). See this module's docstring for the
    transport/decoder split; `FileTransport.fetch()` returns raw `bytes`
    only, never a decoded value.

    Two cache layers, both keyed by URL (or resolved local path):

    1. **In-process memory** (`self._memory`) — a second call to `fetch()`
       for the same location within one `FileTransport` instance's
       lifetime never touches disk or network again. This is what makes
       "download once, read many" actually cheap for an adapter that needs
       the same season file to answer several different queries (e.g.
       `player.identity@season` and a later re-check both wanting
       `players_raw.csv` for the same season).
    2. **Disk** (`FileCache`) — survives process restarts; this is the
       "be polite to GitHub, don't re-download what you already have"
       half (story brief). A local-path `fetch()` skips this layer
       entirely — the file is already on disk, and caching a copy of a
       copy buys nothing.

    `policy` is accepted (and stored) for interface parity with
    `HttpTransport` — every provider states its policy (blueprint §12.4)
    — but a `BulkFilePolicy` has literally nothing to enforce (no rate,
    no quota, no credits), so no rate limiter/tracker is constructed here,
    matching `HttpTransport`'s own no-op branch for the same policy type.
    """

    def __init__(
        self,
        *,
        cache_dir: Path,
        policy: BulkFilePolicy,
        session: "requests.Session | None" = None,
        user_agent: str = DEFAULT_USER_AGENT,
        max_retries: int = MAX_RETRIES,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
        backoff_trigger_statuses: tuple[int, ...] = BACKOFF_TRIGGER_STATUSES,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(policy, BulkFilePolicy):
            raise TypeError(f"FileTransport requires a BulkFilePolicy, got {policy!r}")
        self.policy = policy
        self._session = session
        self._user_agent = user_agent
        self.cache = FileCache(cache_dir)
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_trigger_statuses = backoff_trigger_statuses
        self._sleep = sleep_fn
        self._memory: dict[str, bytes] = {}

    def _get_session(self) -> "requests.Session":
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self._user_agent})
        return self._session

    def fetch(self, location: str, *, force_refresh: bool = False) -> bytes:
        """Return the raw bytes at `location` — an `http(s)://` URL or a
        local filesystem path. Cache-first (memory, then disk) unless
        `force_refresh`. A local path is read directly every call (no
        caching layer makes sense for a file already on this machine) but
        still goes through the memory cache so repeat reads within one
        process are still cheap.

        Raises `TransportError` on a non-200 response (after retries) or
        on a local path that doesn't exist — a caller must not receive an
        empty/partial result silently.
        """
        if not force_refresh and location in self._memory:
            return self._memory[location]

        if not _looks_like_url(location):
            path = Path(location.removeprefix("file://"))
            if not path.exists():
                raise TransportError(f"FileTransport: local path does not exist: {path}")
            raw = path.read_bytes()
            self._memory[location] = raw
            return raw

        if not force_refresh:
            cached = self.cache.get(location)
            if cached is not None:
                self._memory[location] = cached
                return cached

        raw = self._get_live(location)
        self.cache.set(location, raw)
        self._memory[location] = raw
        return raw

    def _get_live(self, url: str) -> bytes:
        session = self._get_session()
        attempt = 0
        while True:
            response = session.get(url, timeout=60)
            if response.status_code not in self.backoff_trigger_statuses:
                break
            attempt += 1
            if attempt > self.max_retries:
                raise TransportError(
                    f"GET {url} failed after {self.max_retries} retries (last status {response.status_code})"
                )
            backoff = self.backoff_base_seconds * (2 ** (attempt - 1))
            self._sleep(backoff)

        if response.status_code != 200:
            raise TransportError(f"GET {url} returned {response.status_code}, expected 200")
        return response.content
