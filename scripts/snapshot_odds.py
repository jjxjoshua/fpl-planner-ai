#!/usr/bin/env python
"""Capture The Odds API into the bitemporal store (E2b story 8).

Two writes per run:
  - odds_match_odds         h2h + totals, ALL fixtures, ONE call (cost=2).
                             Written FIRST, before any goalscorer call —
                             this path is unconditionally secured before
                             anything that could touch player identity
                             runs (the coordinator's explicit instruction:
                             match odds must never be put at risk by a
                             goalscorer-side problem).
  - odds_player_goal_odds   anytime-goalscorer, PER FIXTURE (cost=~1 each,
                             up to n_fixtures calls). Blueprint §12.5's
                             re-fetchability exception (amended 2026-08-21)
                             means an unresolved PLAYER name no longer
                             skips the event — every fixture is captured
                             and written, with unresolved outcomes
                             preserved as `identity_resolved=false` rows
                             carrying a null id and the raw name string.
                             Only a TEAM-identity failure (not covered by
                             the exception, effectively theoretical —
                             20/20 live-verified) still skips an event,
                             logged loudly; match odds for that fixture is
                             unaffected regardless (a fully separate
                             capability/write, already committed above).

**Odds get NO GrainPlan and NO backfill entry (blueprint §3.3: /historical/
is 401 on the free plan — there is nothing to backfill). This script is
therefore NOT orchestrated through fplai.backfill; it is a standalone
capture, the same shape as scripts/snapshot_bootstrap.py.**

**`force_refresh=True` on every call, deliberately, for both
capabilities.** `HttpTransport`'s disk cache is keyed by URL, and the
match-odds URL never changes between runs — a `force_refresh=False` second
run would silently replay the FIRST run's cached response instead of
reading the market again, defeating the entire point of running this on a
schedule (same reasoning `snapshot_bootstrap.py`'s own docstring already
gives for `bootstrap-static/`).

**`store.write()`'s `skip_if_unchanged` (default True) still applies AFTER
the live fetch** — if a bookmaker's price/timestamp is byte-for-byte
identical to the last written batch, the second write is a no-op by
design (this is the store's normal idempotency, not a bug). This script
logs `result.written` explicitly on every write so a capture run's log
always states whether a genuinely NEW observation was recorded, rather
than leaving that ambiguous.

**Deadline-relative, self-gated scheduling (session s005).** This script is
now meant to be fired on a frequent, DUMB schedule (hourly — the exact
shape `scripts/run_snapshot_bootstrap.bat`'s own registered task already
uses, just a coarser repetition) and decides FOR ITSELF, on every firing,
whether this is one of the four capture points for the NEXT upcoming
deadline: **T-78h, T-26h, T-6h, T-2h** (session s005 correction — see below
for why these are NOT T-72h/T-24h). The next deadline is read from the
`events` dataset already in the store (`store.as_of("events", now)` —
operational "what does the schedule look like right now" use, resolved
against this run's own `now`, never a fixed weekly cron, because deadlines are
NOT weekly-at-a-fixed-time (GW3: Fri 17:30 UTC; GW4: Sat 12:30 UTC, an early
kickoff) and a fixed trigger would silently miss any gameweek whose deadline
falls outside its assumed slot. See `decide_capture()` below for the exact
window arithmetic and `docs/wiki/` (Architect to promote) for the credit-
budget reasoning (~18-20 credits per capture x 4/gameweek fits inside the
500/month Odds API allowance with room to spare, but NOT room for a script
that fires repeatedly because a window check is wrong — hence the
`MAX_ATTEMPTS_PER_WINDOW` backstop below).

**Why 78/26, not 72/24 (coordinator correction, session s005).** An offset
that is a whole multiple of 24h inherits the deadline's own LOCAL time-of-
day — "hours before deadline" and "a civilised hour locally" are not
independent choices. FPL's standard deadline is 01:30 Asia/Kuala_Lumpur
(17:30 UTC); T-72h and T-24h from that land at 01:00 local, squarely inside
the documented overnight machine-off window (the 27 Aug heartbeat hole ran
00:45->18:45 local) — the two offsets most likely to silently never fire
would have been T-24h (arguably the most valuable of the four, since team
news starts landing) and T-72h. Breaking the 24h symmetry by 6h moves both
into waking hours for every deadline shape checked against the real
`events` data (both the standard 17:30 UTC slot and GW4's early 12:30 UTC
kickoff) — see `test_capture_windows_land_in_waking_hours_for_a_1730_utc_
deadline` for the encoded constraint.

**A window is derived from the store, never a local marker file.** "Has
this window already fired?" is answered by querying the `heartbeat` dataset
(job=`snapshot_odds`, `target_dataset="odds_match_odds"`) for a run whose
`run_ts` falls inside the window and whose outcome was a genuine (not
failed) attempt — a re-clone or a fresh checkout sees exactly the same
answer the original machine would, because the answer lives in the store,
not on disk next to this file.

**Outside a window, this exits cleanly (code 0)** — logging why, exactly
`scripts/sample_picks.py`'s convention for "retry later, this is not a
failure". A heartbeat row is written on EVERY invocation, including this
no-op one (`outcome="skipped_..."`), for the same reason
`snapshot_bootstrap.py`'s heartbeat exists at all: a no-op that writes
nothing is indistinguishable from a scheduler that never fired.

Usage:
    python scripts/snapshot_odds.py [--store-path PATH] [-v]

THE_ODDS_API_KEY must be set in the environment or in a `.env` file at the
repo root — see verify_odds_provider.py's `_load_dotenv_into_environ` (same
tiny inline loader, duplicated deliberately rather than adding a shared
import between two standalone entry-point scripts). Never logged or printed.
Not required at all, and never checked, on a run that exits via the window
gate before any capture is attempted.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

import polars as pl  # noqa: E402

from fplai.client import FPLClient  # noqa: E402
from fplai.identity import IdentityError  # noqa: E402
from fplai.providers.base import ProviderError  # noqa: E402
from fplai.providers.fpl import FPLProvider  # noqa: E402
from fplai.providers.odds import MATCH_ODDS_FIXTURE, PLAYER_GOAL_ODDS_FIXTURE, OddsProvider, build_transport  # noqa: E402
from fplai.schemas import CANONICAL_SCHEMAS, JOB_HEARTBEAT_RUN, PLAYER_ATTRIBUTES_CURRENT, TEAM_ATTRIBUTES_CURRENT  # noqa: E402
from fplai.store import BitemporalStore, WriteResult  # noqa: E402
from fplai.transport import TransportError  # noqa: E402

logger = logging.getLogger("fplai.snapshot_odds")

_ODDS_CACHE_DIR = _PROJECT_ROOT / "cache" / "odds"

SOURCE = "odds_api"

# -- deadline-relative capture scheduling (session s005) --------------------
#
# Four capture points per gameweek, expressed as hours BEFORE the deadline —
# not a fixed weekly cron (see module docstring: deadlines are not
# weekly-at-a-fixed-time).
# T-78h/T-26h, NOT T-72h/T-24h — see module docstring "Why 78/26, not
# 72/24" above. Do not "round" these back to a multiple of 24 without
# re-checking where they land in local time for every deadline shape.
CAPTURE_OFFSETS_HOURS: tuple[int, ...] = (78, 26, 6, 2)

# Matches the scheduled task's own firing interval (hourly — see this
# script's registration command, produced but not run by this story).
# WHY this must equal the firing interval, not be chosen independently: a
# window narrower than the firing gap could fall entirely between two
# firings and never be seen at all; a window wider than the firing gap could
# have two separate firings land inside the SAME window, which is exactly
# the double-fire this design exists to prevent (the at-most-once guarantee
# below closes that gap too, but it shouldn't have to rely on it).
WINDOW_WIDTH = timedelta(hours=1)

# Credit-safety backstop (brief: "a bug here spends real money-equivalent").
# Ordinarily at most ONE firing lands inside any one window (WINDOW_WIDTH ==
# the firing interval), so this never binds in normal operation. It exists
# for the case where the schedule is ever misconfigured to fire more often
# than hourly, or a transient failure causes more than one genuine retry
# inside one window: after this many ATTEMPTS (successful or not) for one
# window, stop retrying and surface the situation loudly instead of
# continuing to spend credits silently.
MAX_ATTEMPTS_PER_WINDOW = 2

# `job` column value for this script's heartbeat rows — see
# snapshot_bootstrap.py's own HEARTBEAT_JOB for why this must be distinct
# per job (shared entity key `(job, run_ts, target_dataset)`).
HEARTBEAT_JOB = "snapshot_odds"

# The two datasets this script ever writes to (match odds is a single
# WriteResult per run; goal odds is one WriteResult PER FIXTURE, aggregated
# into a single heartbeat row per run — see build_heartbeat_rows).
HEARTBEAT_TARGETS: tuple[str, ...] = ("odds_match_odds", "odds_player_goal_odds")


@dataclass(frozen=True)
class CaptureWindow:
    """One of the four capture points for one upcoming deadline. `label` is
    e.g. `"T-78h"`; `window_start`/`window_end` are tz-aware UTC and
    half-open (`[start, end)`), each `WINDOW_WIDTH` wide, centred on
    `deadline - offset_hours`."""

    label: str
    deadline: datetime
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True)
class CaptureDecision:
    """The output of `decide_capture()`. Exactly one of two shapes:
    `window is not None` (this run should attempt a live capture — `reason`
    is informational only) or `window is None` (this run should not —
    `outcome` names WHY as a heartbeat `outcome` value and `reason` is the
    human-readable detail, both persisted so the reason a window was skipped
    is auditable from the store itself, not just this run's log line)."""

    window: CaptureWindow | None
    outcome: str | None  # set iff window is None; one of the "skipped_*" values below
    reason: str


def _parse_event_deadline(raw: str) -> datetime:
    """Same tiny inline ISO-8601 parser `sample_picks.py::_parse_deadline`
    already uses for this exact FPL field (`"2026-09-04T17:30:00Z"`, verified
    against the real store 2026-08-29) — duplicated deliberately, matching
    this module's existing precedent of small, deliberate duplication
    between standalone entry-point scripts (see `_load_dotenv_into_environ`'s
    own docstring above) rather than importing across two scripts/ modules
    that are not a shared package."""
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def next_deadline(events_df: pl.DataFrame, *, now: datetime) -> datetime | None:
    """The earliest `deadline_time` in `events_df` that is STRICTLY AFTER
    `now`, or `None` if there isn't one (empty/malformed `events`, or every
    deadline in the store has already passed — end of season). Deliberately
    does not depend on `finished`/`is_next` (bootstrap-static fields this
    schema does not guarantee present, only `id`/`deadline_time` are
    required) — "earliest future deadline" is sufficient on its own and
    correctly rolls from one gameweek to the next the moment its deadline
    passes, without needing to know which event FPL itself currently calls
    "next"."""
    if events_df.is_empty() or "deadline_time" not in events_df.columns:
        return None
    upcoming = []
    for raw in events_df["deadline_time"].drop_nulls().to_list():
        try:
            dt = _parse_event_deadline(raw)
        except (ValueError, TypeError):
            continue
        if dt > now:
            upcoming.append(dt)
    return min(upcoming) if upcoming else None


def capture_windows_for_deadline(deadline: datetime) -> list[CaptureWindow]:
    """The four `CaptureWindow`s for one deadline, in `CAPTURE_OFFSETS_HOURS`
    order (descending — furthest from the deadline first)."""
    windows = []
    half_width = WINDOW_WIDTH / 2
    for hours in CAPTURE_OFFSETS_HOURS:
        target = deadline - timedelta(hours=hours)
        windows.append(
            CaptureWindow(
                label=f"T-{hours}h",
                deadline=deadline,
                window_start=target - half_width,
                window_end=target + half_width,
            )
        )
    return windows


def find_open_window(windows: list[CaptureWindow], *, now: datetime) -> CaptureWindow | None:
    """The window (if any) whose half-open `[window_start, window_end)`
    contains `now`. At most one can ever match — the four windows for one
    deadline are `WINDOW_WIDTH`-spaced by construction and never overlap
    for any realistic `CAPTURE_OFFSETS_HOURS` gap (6h is the smallest gap
    between offsets, far wider than the 1h window)."""
    for window in windows:
        if window.window_start <= now < window.window_end:
            return window
    return None


def _window_heartbeat_attempts(store: BitemporalStore, window: CaptureWindow, *, now: datetime) -> list[dict]:
    """Every heartbeat row this job has EVER written for `odds_match_odds`
    (the trigger dataset — see `build_heartbeat_rows`) whose `run_ts` falls
    inside `window`. This — not a local marker file — is how "has this
    window already fired" is answered: it survives a fresh checkout or a
    re-clone because the answer lives in the store."""
    df = store.observations("heartbeat", until=now)
    if df.is_empty():
        return []
    # `run_ts` is written naive UTC (FactTableSchema.validate's tz-aware-
    # column check) but comes back tz-aware UTC on this read — DuckDB's own
    # parquet round-trip, the exact same behaviour check_heartbeat.py's
    # `load_run_timestamps` docstring already documents and normalises for.
    # Compared here against `window.window_start`/`window.window_end`
    # directly (already tz-aware) rather than stripping them to naive —
    # simpler than re-normalising the column, and correct either way since
    # both sides just need to agree.
    df = df.filter(
        (pl.col("job") == HEARTBEAT_JOB)
        & (pl.col("target_dataset") == "odds_match_odds")
        & (pl.col("run_ts") >= window.window_start)
        & (pl.col("run_ts") < window.window_end)
    )
    return df.to_dicts()


def decide_capture(store: BitemporalStore, *, now: datetime) -> CaptureDecision:
    """The single decision point this whole scheduling design rests on:
    should THIS invocation, at THIS instant, attempt a live odds capture?

    Reads `events` via `store.as_of("events", now)` — state as of exactly
    `now` (the same `now` this decision is being made for, not a second,
    independent wall-clock read) — to find the next upcoming deadline,
    derives that deadline's four capture windows, and checks the
    `heartbeat` stream for whether the currently-open window (if any) has
    already had a successful attempt, or has already exhausted
    `MAX_ATTEMPTS_PER_WINDOW`. Deliberately NOT `store.latest()`: that sugar
    hardcodes the REAL wall clock, which would make this function's answer
    depend on when the test/caller happens to run rather than solely on its
    `now` argument (CLAUDE.md rule 7 — deterministic, reproducible from
    inputs) — operationally the two coincide (`main()` always passes the
    real `datetime.now(timezone.utc)`), but only one of them is honest about
    that being a choice rather than a hidden dependency.
    """
    events_df = store.as_of("events", now)
    if events_df.is_empty():
        return CaptureDecision(
            window=None,
            outcome="skipped_no_deadline",
            reason="events dataset is empty — nothing to schedule against (is snapshot_bootstrap running?)",
        )
    deadline = next_deadline(events_df, now=now)
    if deadline is None:
        return CaptureDecision(
            window=None,
            outcome="skipped_no_deadline",
            reason="no upcoming deadline found in events (season may be over, or events data is stale)",
        )

    windows = capture_windows_for_deadline(deadline)
    window = find_open_window(windows, now=now)
    if window is None:
        upcoming = [w for w in windows if w.window_start > now]
        if upcoming:
            nxt = min(upcoming, key=lambda w: w.window_start)
            detail = f"next window {nxt.label} opens {nxt.window_start.isoformat()}"
        else:
            detail = "all 4 capture windows for this deadline have already passed"
        return CaptureDecision(
            window=None,
            outcome="skipped_outside_window",
            reason=f"outside every capture window for deadline {deadline.isoformat()} ({detail})",
        )

    attempts = _window_heartbeat_attempts(store, window, now=now)
    succeeded = [a for a in attempts if a["outcome"] in ("written", "skipped_unchanged")]
    if succeeded:
        return CaptureDecision(
            window=None,
            outcome="skipped_already_captured",
            reason=(
                f"window {window.label} for deadline {deadline.isoformat()} was already captured "
                f"successfully this run of the window (last success run_ts={succeeded[-1]['run_ts']})"
            ),
        )
    if len(attempts) >= MAX_ATTEMPTS_PER_WINDOW:
        return CaptureDecision(
            window=None,
            outcome="skipped_max_attempts_exhausted",
            reason=(
                f"window {window.label} for deadline {deadline.isoformat()} has already had "
                f"{len(attempts)} unsuccessful attempt(s) — MAX_ATTEMPTS_PER_WINDOW="
                f"{MAX_ATTEMPTS_PER_WINDOW} reached, not retrying again this window "
                "(credit-safety backstop). This needs investigation, not a silent retry."
            ),
        )
    return CaptureDecision(
        window=window,
        outcome=None,
        reason=f"window {window.label} for deadline {deadline.isoformat()} is OPEN (attempt {len(attempts) + 1})",
    )


# -- heartbeat (session s005, PROGRESS.md E2's long-standing open item) -----


def build_heartbeat_rows(
    *,
    job: str,
    run_ts: datetime,
    skip_outcome: str | None,
    skip_reason: str | None,
    summary: dict | None,
    run_error: Exception | None,
) -> pl.DataFrame:
    """One row per `HEARTBEAT_TARGETS` entry, describing what this run saw.
    Caller contract, checked in this priority order (exactly one branch
    applies per call — `main()` never sets more than one of these):

      1. `skip_outcome is not None` — this run never attempted a capture at
         all (outside every window, already captured, or max attempts
         reached). Both target datasets get `skip_outcome` verbatim as
         `outcome`, `skip_reason` as `error`, everything else NULL. This is
         the branch that fires on almost every hourly invocation — see
         module docstring.
      2. `run_error is not None` — a capture WAS attempted (a window was
         open) but never reached (or didn't survive) the write calls: the
         API key was missing, or `run()` itself raised. Both targets get
         `outcome="failed"` with `run_error`'s own message.
      3. Neither — `run()` returned a real `summary` dict (session s005's
         `run()` return shape: `match_odds_write`, `goal_odds_writes`,
         `goal_odds_team_identity_failed`, `goal_odds_failed`). `odds_
         match_odds` reflects `summary["match_odds_write"]` directly (one
         `WriteResult` per run, exactly like `snapshot_bootstrap.py`'s own
         dataset rows). `odds_player_goal_odds` is AGGREGATED across
         however many fixtures this run touched (one `WriteResult` PER
         FIXTURE, not per run) — `payload_hash` is left NULL for this row
         because no single hash represents a multi-batch aggregate;
         `n_rows` sums every fixture's `WriteResult.n_rows`; `outcome` is
         `"written"` if ANY fixture wrote a new batch, else
         `"skipped_unchanged"` if every fixture wrote successfully but was a
         no-op, else `"failed"` if every fixture failed and none wrote at
         all, else `"no_fixtures"` if there were literally zero events this
         run (an off-season/no-fixtures edge case). Any team-identity or
         other per-fixture failures are summarised (count + names) in
         `error`, whatever the aggregate `outcome` — a partial failure
         alongside a real write is still worth recording.
    """
    run_ts_naive = run_ts.astimezone(timezone.utc).replace(tzinfo=None)

    if skip_outcome is not None:
        return pl.DataFrame(
            [
                {
                    "job": job,
                    "run_ts": run_ts_naive,
                    "target_dataset": name,
                    "outcome": skip_outcome,
                    "payload_hash": None,
                    "n_rows": None,
                    "error": skip_reason,
                }
                for name in HEARTBEAT_TARGETS
            ]
        )

    if run_error is not None:
        return pl.DataFrame(
            [
                {
                    "job": job,
                    "run_ts": run_ts_naive,
                    "target_dataset": name,
                    "outcome": "failed",
                    "payload_hash": None,
                    "n_rows": None,
                    "error": f"{type(run_error).__name__}: {run_error}",
                }
                for name in HEARTBEAT_TARGETS
            ]
        )

    assert summary is not None, "build_heartbeat_rows: exactly one of skip_outcome/run_error/summary must be set"

    rows: list[dict] = []

    match_write: WriteResult | None = summary.get("match_odds_write")
    if match_write is not None:
        rows.append(
            {
                "job": job,
                "run_ts": run_ts_naive,
                "target_dataset": "odds_match_odds",
                "outcome": "written" if match_write.written else "skipped_unchanged",
                "payload_hash": match_write.content_hash,
                "n_rows": match_write.n_rows,
                "error": None,
            }
        )
    else:
        rows.append(
            {
                "job": job,
                "run_ts": run_ts_naive,
                "target_dataset": "odds_match_odds",
                "outcome": "failed",
                "payload_hash": None,
                "n_rows": None,
                "error": "match_odds_write missing from summary — capture did not reach the write call",
            }
        )

    goal_writes: dict = summary.get("goal_odds_writes") or {}
    team_failed: dict = summary.get("goal_odds_team_identity_failed") or {}
    other_failed: dict = summary.get("goal_odds_failed") or {}
    if goal_writes:
        goal_outcome = "written" if any(wr.written for wr in goal_writes.values()) else "skipped_unchanged"
        goal_n_rows: int | None = sum(wr.n_rows for wr in goal_writes.values())
    elif team_failed or other_failed:
        goal_outcome = "failed"
        goal_n_rows = None
    else:
        goal_outcome = "no_fixtures"
        goal_n_rows = 0
    error_parts = []
    if team_failed:
        error_parts.append(f"{len(team_failed)} team-identity failure(s): {sorted(team_failed)}")
    if other_failed:
        error_parts.append(f"{len(other_failed)} other failure(s): {sorted(other_failed)}")
    rows.append(
        {
            "job": job,
            "run_ts": run_ts_naive,
            "target_dataset": "odds_player_goal_odds",
            "outcome": goal_outcome,
            "payload_hash": None,
            "n_rows": goal_n_rows,
            "error": "; ".join(error_parts) if error_parts else None,
        }
    )
    return pl.DataFrame(rows)


def write_heartbeat(store: BitemporalStore, df: pl.DataFrame, *, run_ts: datetime) -> WriteResult:
    """Same contract as `snapshot_bootstrap.py::write_heartbeat` — literal
    `skip_if_unchanged=False`, validated against `CANONICAL_SCHEMAS[JOB_
    HEARTBEAT_RUN]` first. See that function's docstring for why the kwarg
    must be literal (two genuinely-identical runs, e.g. two consecutive
    `skipped_outside_window` no-ops with the exact same reason string, must
    both persist — this dataset's whole purpose is distinguishing "quiet"
    from "never ran", and `skip_if_unchanged=True` would collapse them)."""
    CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN].validate(df)
    return store.write(
        "heartbeat",
        df,
        valid_at=run_ts,
        observed_at=run_ts,
        source="fplai.snapshot_odds:heartbeat",
        skip_if_unchanged=False,
    )


def write_heartbeat_safely(
    store: BitemporalStore,
    *,
    job: str,
    run_ts: datetime,
    skip_outcome: str | None,
    skip_reason: str | None,
    summary: dict | None,
    run_error: Exception | None,
) -> None:
    """Same guarantee as `snapshot_bootstrap.py::write_heartbeat_safely`: a
    monitoring feature must never take down the thing it monitors. Whatever
    goes wrong building or writing the heartbeat batch, this run's own
    capture outcome (already decided by the time this is called) is
    unaffected — deliberately broad `except Exception` for the same reason
    that function's docstring gives."""
    try:
        df = build_heartbeat_rows(
            job=job,
            run_ts=run_ts,
            skip_outcome=skip_outcome,
            skip_reason=skip_reason,
            summary=summary,
            run_error=run_error,
        )
        write_heartbeat(store, df, run_ts=run_ts)
    except Exception as exc:  # noqa: BLE001 — see docstring: must never propagate
        logger.error("heartbeat write failed (odds capture outcome unaffected by this): %s", exc)


def _load_dotenv_into_environ(env_path: Path) -> None:
    """Same tiny inline .env loader as verify_odds_provider.py — see that
    module's copy for the full rationale. Never logs or prints a value."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _earliest_valid_at(rows: pl.DataFrame) -> datetime:
    """`valid_at` anchors a multi-bookmaker batch at the EARLIEST
    `market_last_update` in it — the same convention `fplai.backfill.
    _valid_at_for` uses for fixtures (a batch spanning several real-world
    instants is anchored at the earliest, the conservative direction;
    understating how early a fact became true is merely conservative,
    overstating it is the leakage direction, blueprint §3.2).

    `market_last_update` itself is NAIVE (see `providers/odds.py`'s
    `_parse_iso` docstring for why — a real bug this exact call site
    surfaced on the first live capture run). `store.write()`'s
    `valid_at`/`observed_at` are tz-aware everywhere else in this codebase
    (every provider's `FetchResult.observed_at` is `datetime.now(timezone.
    utc)`), so the naive value read off the column is re-attached UTC here
    — the SAME pattern `fplai.backfill._parse_kickoff_value` uses for PL
    API's equally-naive `kickoff` column. Two different rules for two
    different things: DATA columns inside `rows` are naive; `valid_at`/
    `observed_at` metadata passed to `store.write()` are tz-aware."""
    non_null = rows["market_last_update"].drop_nulls()
    if non_null.len() == 0:
        raise ProviderError("batch has no non-null 'market_last_update' to anchor valid_at")
    naive = non_null.min()
    return naive.replace(tzinfo=timezone.utc)


def run(store: BitemporalStore, provider: OddsProvider) -> dict:
    """Returns a summary dict — used by main() for logging and by tests
    (if any are added later) for assertions, rather than main() inlining
    everything.

    **match.odds@fixture is captured and WRITTEN FIRST, before any
    player.goal_odds@fixture call is made** — the coordinator's explicit
    instruction: match odds is the path that already works (811 rows,
    10/10 fixtures, 2 credits, live-proven) and those prices die at
    kickoff, so it must never be put at risk by a goalscorer-side problem.
    Nothing below this point can un-write it.

    **player.goal_odds@fixture never raises on an unresolved PLAYER name**
    (blueprint §12.5's re-fetchability exception, implemented in
    `OddsProvider._fetch_player_goal_odds` — see that method's docstring).
    Every fixture's goalscorer odds are therefore captured and written,
    including rows with `identity_resolved=false`; nothing is skipped for
    that reason any more. `except IdentityError` below is kept only for
    the (effectively theoretical — 20/20 live-verified) case of an
    unresolved TEAM name, which the exception does NOT cover."""
    summary: dict = {
        "match_odds_write": None,
        "goal_odds_writes": {},  # event_id -> WriteResult
        "goal_odds_team_identity_failed": {},  # event_id -> IdentityError message (team-name only)
        "goal_odds_failed": {},  # event_id -> other error message
        "goal_odds_unresolved_names": set(),  # aggregated across every event this run
        "goal_odds_n_unresolved_rows": 0,
        "goal_odds_n_resolved_rows": 0,
    }

    match_odds = provider.fetch(MATCH_ODDS_FIXTURE, force_refresh=True)
    match_valid_at = _earliest_valid_at(match_odds.rows)
    write_result = store.write(
        "odds_match_odds",
        match_odds.rows,
        valid_at=match_valid_at,
        observed_at=match_odds.observed_at,
        source=f"{SOURCE}:match_odds",
        provider_id=match_odds.provider_id,
        capability=str(match_odds.capability),
        endpoint=match_odds.endpoint,
    )
    summary["match_odds_write"] = write_result
    logger.info(
        "odds_match_odds     %-22s rows=%-5d hash=%s",
        "WROTE" if write_result.written else "unchanged, skipped",
        write_result.n_rows,
        write_result.content_hash[:12],
    )

    events = match_odds.rows.select("provider_event_id", "commence_time").unique().sort("commence_time")
    for event_id, commence_time in events.iter_rows():
        try:
            goal_odds = provider.fetch(PLAYER_GOAL_ODDS_FIXTURE, event_id=event_id, force_refresh=True)
        except IdentityError as exc:
            # NOT the re-fetchability exception's territory — that covers
            # PLAYER names only. This is the rare/theoretical TEAM-name
            # miss (20/20 live-verified so far), which still raises
            # all-or-nothing per §12.5's original rule.
            logger.error("odds_player_goal_odds event=%s SKIPPED (team identity, not player — see §12.5):\n%s", event_id, exc)
            summary["goal_odds_team_identity_failed"][event_id] = str(exc)
            continue
        except (ProviderError, TransportError) as exc:
            logger.error("odds_player_goal_odds event=%s FAILED (non-identity): %s", event_id, exc)
            summary["goal_odds_failed"][event_id] = str(exc)
            continue

        unresolved_names = goal_odds.meta.get("unresolved_player_names", [])
        n_unresolved = goal_odds.meta.get("n_unresolved_rows", 0)
        summary["goal_odds_unresolved_names"].update(unresolved_names)
        summary["goal_odds_n_unresolved_rows"] += n_unresolved
        summary["goal_odds_n_resolved_rows"] += goal_odds.rows.height - n_unresolved
        if unresolved_names:
            # LOUD, place 3 of blueprint §12.5's three ("the capture
            # reports every unresolved entity by name") — per-event here;
            # the run-level aggregate is logged once in main() below.
            logger.warning(
                "odds_player_goal_odds event=%s: %d unresolved player name(s), PRESERVED not "
                "skipped (identity_resolved=false, blueprint §12.5 re-fetchability exception): %s",
                event_id,
                len(unresolved_names),
                unresolved_names,
            )

        goal_valid_at = _earliest_valid_at(goal_odds.rows)
        write_result = store.write(
            "odds_player_goal_odds",
            goal_odds.rows,
            valid_at=goal_valid_at,
            observed_at=goal_odds.observed_at,
            source=f"{SOURCE}:player_goal_odds",
            provider_id=goal_odds.provider_id,
            capability=str(goal_odds.capability),
            endpoint=goal_odds.endpoint,
        )
        summary["goal_odds_writes"][event_id] = write_result
        logger.info(
            "odds_player_goal_odds event=%s %-22s rows=%-5d (%d resolved, %d unresolved) hash=%s",
            event_id,
            "WROTE" if write_result.written else "unchanged, skipped",
            write_result.n_rows,
            goal_odds.rows.height - n_unresolved,
            n_unresolved,
            write_result.content_hash[:12],
        )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store-path", type=Path, default=None, help="override the bitemporal store base path (default: <repo>/data/store)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # SECURITY: found live, 2026-08-21, running this exact script with -v —
    # urllib3's own connection-pool DEBUG log line prints the FULL request
    # URL, which for this provider (unlike FPL/PL API) includes
    # "&apiKey=<key>" in the query string. `-v` sets the ROOT logger to
    # DEBUG, and urllib3's logger propagates to root with no level of its
    # own, so it started emitting the key in plaintext to stdout/the
    # terminal the moment --verbose was passed — nothing in odds.py's own
    # cache/error-message redaction touches this, because the leak is in a
    # THIRD-PARTY library's own logger, not in any string this codebase
    # constructs. Capped here UNCONDITIONALLY (not just when NOT verbose)
    # so a future --verbose run — or -v being added to a script that
    # doesn't have it today — can never reintroduce this. See
    # tests/test_provider_odds.py's redaction test and docs/wiki/
    # provider-framework.md for the live evidence. Set here EARLY
    # (before the FPL API calls below, which also go through
    # urllib3 — harmless for THEM, but this must not depend on call
    # order) as well as inside odds.build_transport() itself (belt and
    # suspenders — every caller of that function gets it regardless of
    # whether a script remembered to set it here first).
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    store = BitemporalStore(base_path=args.store_path) if args.store_path else BitemporalStore()

    # `run_ts` is captured ONCE, before the window decision, and reused for
    # both the decision itself (`decide_capture`'s `now`) and the
    # heartbeat's `run_ts` column below — one run, one timestamp, matching
    # snapshot_bootstrap.py's own convention.
    run_ts = datetime.now(timezone.utc)
    decision = decide_capture(store, now=run_ts)

    if decision.window is None:
        # Outside every capture window, already captured, or max attempts
        # reached — exactly sample_picks.py's "exit cleanly (code 0), this
        # is expected/retryable, not an error" convention. A heartbeat row
        # is STILL written (skip_outcome branch) — see module docstring for
        # why a no-op run must not be silent.
        logger.info("no capture this run: %s", decision.reason)
        write_heartbeat_safely(
            store,
            job=HEARTBEAT_JOB,
            run_ts=run_ts,
            skip_outcome=decision.outcome,
            skip_reason=decision.reason,
            summary=None,
            run_error=None,
        )
        return 0

    logger.info("capture window OPEN: %s", decision.reason)

    _load_dotenv_into_environ(_PROJECT_ROOT / ".env")
    if "THE_ODDS_API_KEY" not in os.environ or not os.environ["THE_ODDS_API_KEY"]:
        # A window IS open — this is a genuine failure to capture, not a
        # skip — so it is heartbeated as `run_error`, not `skip_outcome`
        # (the distinction `_window_heartbeat_attempts` relies on: only a
        # SUCCESSFUL attempt suppresses a retry within this window; a
        # missing-key failure correctly leaves room for one on the next
        # hourly firing, up to MAX_ATTEMPTS_PER_WINDOW).
        msg = "THE_ODDS_API_KEY is not set (checked environment and .env) — cannot capture"
        logger.error(msg)
        write_heartbeat_safely(
            store,
            job=HEARTBEAT_JOB,
            run_ts=run_ts,
            skip_outcome=None,
            skip_reason=None,
            summary=None,
            run_error=RuntimeError(msg),
        )
        return 1
    logger.info("THE_ODDS_API_KEY: found (value never logged)")

    fpl_provider = FPLProvider(client=FPLClient())
    elements = fpl_provider.fetch(PLAYER_ATTRIBUTES_CURRENT).rows
    teams = fpl_provider.fetch(TEAM_ATTRIBUTES_CURRENT).rows
    logger.info("FPL snapshot: %d elements, %d teams", elements.height, teams.height)

    transport = build_transport(_ODDS_CACHE_DIR)
    provider = OddsProvider(transport, season="2026-27", elements=elements, teams=teams)

    summary: dict | None = None
    run_error: Exception | None = None
    try:
        summary = run(store, provider)
    except (ProviderError, IdentityError, TransportError) as exc:
        logger.error("odds capture failed: %s", exc)
        run_error = exc

    # ALWAYS attempted — including when `run()` raised above — and never
    # allowed to affect this function's return value (write_heartbeat_
    # safely never raises; see its own docstring).
    write_heartbeat_safely(
        store,
        job=HEARTBEAT_JOB,
        run_ts=run_ts,
        skip_outcome=None,
        skip_reason=None,
        summary=summary,
        run_error=run_error,
    )

    if run_error is not None:
        return 1

    if transport._last_response_headers:
        logger.info(
            "credit headers after this run's LAST live call: x-requests-remaining=%s x-requests-used=%s",
            transport._last_response_headers.get("x-requests-remaining"),
            transport._last_response_headers.get("x-requests-used"),
        )
    logger.info("CreditTracker.remaining_this_month (reconciled against provider headers): %d", transport._credits.remaining_this_month)

    n_goal_odds_events_written = len(summary["goal_odds_writes"])
    n_goal_odds_team_failed = len(summary["goal_odds_team_identity_failed"])
    n_goal_odds_failed = len(summary["goal_odds_failed"])
    unresolved_names = sorted(summary["goal_odds_unresolved_names"])
    logger.info(
        "SUMMARY: match_odds written=%s | goal_odds: %d/%d fixtures captured "
        "(%d resolved rows, %d unresolved rows preserved), %d team-identity failures, %d other failures",
        summary["match_odds_write"].written,
        n_goal_odds_events_written,
        n_goal_odds_events_written + n_goal_odds_team_failed + n_goal_odds_failed,
        summary["goal_odds_n_resolved_rows"],
        summary["goal_odds_n_unresolved_rows"],
        n_goal_odds_team_failed,
        n_goal_odds_failed,
    )
    if unresolved_names:
        # LOUD, place 3 of blueprint §12.5's three ("the capture reports
        # every unresolved entity by name") — the RUN-LEVEL aggregate,
        # deduplicated across every fixture this run touched. NOT a crash,
        # NOT a silent drop — every one of these rows is sitting in
        # odds_player_goal_odds right now with identity_resolved=false and
        # its raw name intact, ready to be repaired offline.
        logger.warning(
            "%d distinct player name(s) PRESERVED UNRESOLVED across this run (blueprint §12.5 "
            "re-fetchability exception — not dropped, not raised, repairable offline from the "
            "stored raw string): %s",
            len(unresolved_names),
            unresolved_names,
        )
    if n_goal_odds_team_failed:
        logger.warning(
            "%d fixture(s) had a TEAM-identity failure (not covered by the player re-fetchability "
            "exception) — see the ERROR lines above for the full IdentityError per event.",
            n_goal_odds_team_failed,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
