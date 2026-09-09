"""Thin, rate-limited FPL Official API client.

Why in-house: there is no usable third-party client (`fpl` 0.6.35 is three
years stale), and the bitemporal store (see `store.py`) needs to control
exactly when a response was observed. This module owns the network edge only —
it does not know about the store and does not interpret payloads beyond JSON
decoding.

Hard limits (blueprint CLAUDE.md, "FPL API discipline"; verified in
docs/wiki/data-sources.md §2.7):
  - 2 req/s ceiling, single connection, +/-20% jitter. NOT a target to approach —
    a ceiling. Probing found ~3.6 req/s clean; that is not a budget.
  - No rate-limit headers exist. Back off on ANY sign of trouble (429/403).
  - Realistic browser User-Agent.
  - Full response caching keyed by URL so re-runs don't re-hit the API.

`RateLimiter` / `ResponseCache` / `CacheEntry` / the backoff constants now
live in `transport.py` (E2b story 4 — shared transport primitives, so a
future HTTP provider doesn't reimplement them). Re-exported here unchanged
so existing imports (`from fplai.client import RateLimiter, ...`) keep
working — this module's own request loop (`_get`) is intentionally left
as-is rather than rewired onto `transport.HttpTransport`; see
`providers/fpl.py`'s module docstring for why.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import requests

from fplai.transport import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_TRIGGER_STATUSES,
    DEFAULT_USER_AGENT,
    MAX_RETRIES,
    CacheEntry,
    RateLimiter,
    ResponseCache,
)

logger = logging.getLogger("fplai.client")

BASE_URL = "https://fantasy.premierleague.com/api/"

TARGET_REQUESTS_PER_SECOND = 2.0
JITTER_FRACTION = 0.20  # +/-20%

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = _PROJECT_ROOT / "cache" / "fpl_api"

# -- pre-deadline gate-repair session s003 (blueprint §3.4 "Blocker 1") ----
#
# `entry/{id}/event/{gw}/picks/` returns HTTP 503, body "The game is being
# updated.", during FPL's post-deadline maintenance window — observed live,
# continuously, 17:31-17:41 UTC (8 consecutive probes) and again ~27 minutes
# total (503 from ~17:30Z to first 200 at 17:57Z) on GW1's real deadline.
# `transport.BACKOFF_TRIGGER_STATUSES` is (429, 403) only — shared by every
# other HTTP provider (PL API, Odds API) via `HttpTransport`, which this
# module deliberately does NOT change (no live evidence this session that
# 503 means the same "temporary, provider-specific maintenance" thing for
# any other provider). `FPL_BACKOFF_TRIGGER_STATUSES` is this client's OWN,
# additive default — every other backoff-trigger status still applies.
FPL_BACKOFF_TRIGGER_STATUSES: tuple[int, ...] = BACKOFF_TRIGGER_STATUSES + (503,)

# 404 is a TEMPORAL state for `entry/{id}/` ("not issued yet") and
# `entry/{id}/event/{gw}/picks/` ("not public yet") — blueprint §3.4
# "Blocker 2". 1 hour comfortably invalidates a cache poisoned days earlier
# (the real incident: 9 `entry/` 404s cached 19-21 Aug would otherwise
# answer Tue 25 Aug's binary search from cache, at zero live requests) while
# still letting one script invocation's own within-run lookups (which
# complete in seconds to low minutes) reuse a cache entry it just wrote.
# See `ResponseCache`'s docstring (transport.py) for the full mechanism.
ENTRY_404_TTL_SECONDS = 3600.0


class FPLApiError(RuntimeError):
    """Raised when a request fails after exhausting retries, or on an
    unexpected non-404 error status. 404 on entry_picks is NOT this — it is an
    expected outcome, see `entry_picks`."""


class FPLClient:
    """Typed accessors over the FPL Official API. Single session, single
    connection, rate-limited, cached, with bounded backoff on 429/403.
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        session: requests.Session | None = None,
        rate_limiter: RateLimiter | None = None,
        cache: ResponseCache | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        max_retries: int = MAX_RETRIES,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
        backoff_trigger_statuses: tuple[int, ...] = FPL_BACKOFF_TRIGGER_STATUSES,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json",
            }
        )
        # Explicit, not RateLimiter()/ResponseCache()'s own generic defaults —
        # transport.py's classes are shared across providers now and must not
        # silently drift if their defaults ever change. This is the FPL
        # policy: 2 req/s +/-20% jitter, cache under cache/fpl_api, 404s on
        # this cache expire after ENTRY_404_TTL_SECONDS (blueprint §3.4
        # "Blocker 2" — see ResponseCache's docstring). A caller that passes
        # its own `cache=` (every existing test does) opts out of the TTL
        # unless it asks for it explicitly — unchanged behaviour for them.
        self.rate_limiter = rate_limiter or RateLimiter(
            requests_per_second=TARGET_REQUESTS_PER_SECOND, jitter_fraction=JITTER_FRACTION
        )
        self.cache = (
            cache
            if cache is not None
            else ResponseCache(DEFAULT_CACHE_DIR, max_age_for_404_seconds=ENTRY_404_TTL_SECONDS)
        )
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        # blueprint §3.4 "Blocker 1": 503 ("The game is being updated.") is
        # a retryable backoff-trigger status for THIS client by default —
        # see FPL_BACKOFF_TRIGGER_STATUSES above for why it is not folded
        # into the shared transport.py constant.
        self.backoff_trigger_statuses = backoff_trigger_statuses
        self._sleep = sleep_fn

    # -- low-level -----------------------------------------------------

    def _get(
        self, path: str, *, force_refresh: bool = False, max_retries: int | None = None
    ) -> tuple[int, str]:
        """GET `path` (relative to base_url). Returns (status_code, text).
        Uses the URL-keyed cache unless `force_refresh`. Applies the rate
        limiter before every live request and retries with exponential
        backoff on `self.backoff_trigger_statuses` (429/403 always, plus 503
        by default — blueprint §3.4 "Blocker 1") up to `max_retries` times.

        `max_retries`, if given, overrides `self.max_retries` for THIS call
        only — added so a caller can ask for a FAST, single-attempt probe
        (`max_retries=0`) without waiting through the full backoff ladder
        (up to ~7 minutes at the default base/count) for a request it
        already suspects will fail. `sample_picks.py`'s readiness probe uses
        this; the 10,000-entry sampling loop uses a smaller-than-default
        retry count for the same reason at a different scale (bounding how
        much one bad entry can slow the whole ~83-minute run)."""
        url = self.base_url + path.lstrip("/")
        effective_max_retries = self.max_retries if max_retries is None else max_retries

        if not force_refresh:
            cached = self.cache.get(url)
            if cached is not None:
                logger.debug("cache hit: %s", url)
                return cached.status_code, cached.body

        attempt = 0
        while True:
            self.rate_limiter.wait()
            response = self.session.get(url, timeout=30)

            if response.status_code not in self.backoff_trigger_statuses:
                break

            attempt += 1
            if attempt > effective_max_retries:
                raise FPLApiError(
                    f"GET {url} failed after {effective_max_retries} retries "
                    f"(last status {response.status_code})"
                )
            backoff = self.backoff_base_seconds * (2 ** (attempt - 1))
            logger.warning(
                "GET %s -> %s, backing off %.0fs (attempt %d/%d)",
                url,
                response.status_code,
                backoff,
                attempt,
                effective_max_retries,
            )
            self._sleep(backoff)

        status_code, text = response.status_code, response.text
        # Cache successes and expected 404s; do not cache other errors so a
        # transient 500 does not become permanently "sticky".
        if status_code == 200 or status_code == 404:
            self.cache.set(url, CacheEntry(status_code=status_code, body=text))
        return status_code, text

    def _get_json(
        self, path: str, *, force_refresh: bool = False, max_retries: int | None = None
    ) -> Any:
        status_code, text = self._get(path, force_refresh=force_refresh, max_retries=max_retries)
        if status_code != 200:
            raise FPLApiError(f"GET {self.base_url + path} returned {status_code}: {text[:200]}")
        return json.loads(text)

    # -- typed accessors -------------------------------------------------

    def bootstrap_static(self, *, force_refresh: bool = False) -> dict:
        """The big one: elements, teams, events, game_config, game_settings,
        chips, phases, element_stats, element_types, total_players."""
        return self._get_json("bootstrap-static/", force_refresh=force_refresh)

    def fixtures(self, event: int | None = None, *, force_refresh: bool = False) -> list[dict]:
        path = "fixtures/"
        if event is not None:
            path += f"?event={event}"
        return self._get_json(path, force_refresh=force_refresh)

    def entry(self, entry_id: int, *, force_refresh: bool = False) -> dict | None:
        """Manager metadata. Returns None on 404 (entry id does not exist)."""
        status_code, text = self._get(f"entry/{entry_id}/", force_refresh=force_refresh)
        if status_code == 404:
            return None
        if status_code != 200:
            raise FPLApiError(f"GET entry/{entry_id}/ returned {status_code}: {text[:200]}")
        return json.loads(text)

    def entry_picks(
        self,
        entry_id: int,
        event: int,
        *,
        force_refresh: bool = False,
        max_retries: int | None = None,
    ) -> dict | None:
        """Picks for one entry, one gameweek. Returns None on 404 — this is
        EXPECTED and non-exceptional both pre-deadline (picks not yet public)
        and for entry ids that don't exist. Callers must not treat None as an
        error; see docs/wiki/data-sources.md §2.1.

        `max_retries` overrides `self.max_retries` for this call only — see
        `_get`'s docstring."""
        status_code, text = self._get(
            f"entry/{entry_id}/event/{event}/picks/",
            force_refresh=force_refresh,
            max_retries=max_retries,
        )
        if status_code == 404:
            return None
        if status_code != 200:
            raise FPLApiError(
                f"GET entry/{entry_id}/event/{event}/picks/ returned {status_code}: {text[:200]}"
            )
        return json.loads(text)

    def event_live(self, event: int, *, force_refresh: bool = False) -> dict:
        """Per-element live stats + `explain` blocks for gameweek `event`.
        `{"elements": []}` pre-season / before the GW is played."""
        return self._get_json(f"event/{event}/live/", force_refresh=force_refresh)

    def event_status(self, *, force_refresh: bool = False) -> dict:
        """Bonus/points-confirmation state — used to decide when a GW's
        picks are immutable and safe to sample (blueprint §3.4)."""
        return self._get_json("event-status/", force_refresh=force_refresh)
