#!/usr/bin/env python
"""Check the `heartbeat` dataset (`fplai.schemas.JOB_HEARTBEAT_RUN`) for
gaps in a job's own run cadence — session s003, PROGRESS.md E2.

## What this answers, and what it deliberately does NOT answer

This tool answers exactly one question, directly: **did the scheduler
genuinely keep running a job, on schedule, over the requested window?**
It answers that from the `heartbeat` dataset's own `run_ts` column alone —
one timestamp per invocation of the job, written UNCONDITIONALLY
(`skip_if_unchanged=False`, see `scripts/snapshot_bootstrap.py::write_
heartbeat`) regardless of whether that run's per-dataset fetches changed
anything.

It deliberately does **not** look at whether any individual downstream
dataset (`elements`, `events`, ...) got a new row — that is `outcome`/
`payload_hash` information this dataset also carries, but a long run of
`skipped_unchanged` outcomes for a genuinely quiet API is completely
healthy and must never be reported as a gap here. This is the whole
point: a stale `elements` row and a stalled scheduler are indistinguishable
from `elements` alone (demonstrated live 2026-08-22, ~11h against a 30-min
cadence — see docs/wiki/runbook-ingest.md §6) but trivially distinguishable
once the job's OWN execution timestamp is tracked independently of what it
found.

## Exit codes

  0  every job checked is healthy: the largest gap between consecutive
     heartbeats within the window (including the "gap from the last
     heartbeat to right now", which is what actually catches "the
     scheduler stopped and nothing has run since") is <= --max-gap-minutes.
  1  at least one job's largest gap exceeds --max-gap-minutes.
  2  no heartbeat data exists at all for a requested (or, with no --job,
     for the whole dataset) -- this is itself unhealthy, never silently
     treated as "nothing to report".

## Usage

    python scripts/check_heartbeat.py --max-gap-minutes 45
    python scripts/check_heartbeat.py --job snapshot_bootstrap --max-gap-minutes 45 --window-hours 12

`--max-gap-minutes` has no default — passed as an argument on every
invocation, never a hardcoded threshold buried in this module (CLAUDE.md
rule 4's spirit, applied to an operational threshold rather than a domain
constant): what counts as "too long a gap" depends on the job's own
cadence (30 min today; 60 min post-deadline per the runbook), which this
script has no way to infer on its own.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.store import BitemporalStore  # noqa: E402

logger = logging.getLogger("fplai.check_heartbeat")

_LOCAL_TZ = ZoneInfo("Asia/Kuala_Lumpur")  # CLAUDE.md: prose is GMT+8


def _fmt_local(ts: datetime) -> str:
    """`ts` (any tz-aware datetime) rendered local-first, UTC in
    parentheses — CLAUDE.md's "Time zones" convention for anything a human
    reads."""
    local = ts.astimezone(_LOCAL_TZ)
    return f"{local.strftime('%H:%M %a %d %b')} ({ts.astimezone(timezone.utc).strftime('%H:%M UTC')})"


def load_run_timestamps(store: BitemporalStore, *, job: str | None, as_of: datetime) -> dict[str, list[datetime]]:
    """Every distinct `run_ts` in the `heartbeat` dataset, up to `as_of`,
    grouped by `job` and sorted ascending. `job=None` returns every job
    present rather than assuming there is exactly one — a future
    `snapshot_odds.py`/`sample_picks.py` adoption of this same dataset
    (`fplai.schemas.JOB_HEARTBEAT_RUN`'s module comment) must not require
    this script to change.

    Deliberately DISTINCT: one run writes one row per target_dataset (six
    for snapshot_bootstrap today), all sharing one `run_ts` — this
    function's job is cadence of RUNS, not of individual per-dataset rows,
    so those must collapse to one timestamp per run before any gap is
    computed.

    Uses `observations()`, not `as_of()` — this dataset's entity key
    (`job`, `run_ts`, `target_dataset`) already makes every run a distinct
    entity (see fplai.schemas' JOB_HEARTBEAT_RUN module comment), so
    `as_of()` would return the same full set here too, but `observations()`
    is the primitive this file's own docstring precedent (BitemporalStore.
    as_of's docstring) names as the explicit-opt-in for "modelling
    observation cadence" -- exactly what this is.

    Returns `{}` if the dataset has never been written at all (verified by
    `BitemporalStore._dataset_exists`, exposed here indirectly: an empty
    `observations()` result on a store where the dataset directory doesn't
    exist yet).
    """
    df = store.observations("heartbeat", until=as_of)
    if df.is_empty():
        return {}
    if job is not None:
        df = df.filter(df["job"] == job)
        if df.is_empty():
            return {}

    grouped: dict[str, list[datetime]] = {}
    for job_name in sorted(set(df["job"].to_list())):
        job_df = df.filter(df["job"] == job_name)
        ts_values = sorted(set(job_df["run_ts"].to_list()))
        # DuckDB round-trip yields tz-aware UTC (store.py's own
        # _duckdb_type_to_polars convention) -- normalise defensively in
        # case a caller ever hands this a naive-Datetime frame directly
        # (e.g. a hand-built test frame).
        grouped[job_name] = [
            t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc) for t in ts_values
        ]
    return grouped


@dataclass(frozen=True)
class GapReport:
    job: str
    run_count: int
    largest_gap: timedelta | None  # None only when run_count == 0
    largest_gap_start: datetime | None
    largest_gap_end: datetime | None
    last_run_ts: datetime | None

    def healthy(self, max_gap: timedelta) -> bool:
        if self.largest_gap is None:
            return False  # no data at all is never healthy -- see module docstring
        return self.largest_gap <= max_gap


def compute_gap_report(
    job: str,
    run_timestamps: list[datetime],
    *,
    as_of: datetime,
    window: timedelta,
) -> GapReport:
    """The largest gap for `job`, considering only gaps that END inside
    `[as_of - window, as_of]` -- but ALWAYS including the trailing gap from
    the last known heartbeat to `as_of` itself, regardless of window, since
    that is the one check that actually catches "the scheduler stopped
    entirely and nothing has run since": a job with a perfect 30-minute
    cadence for months that simply stopped 6 hours ago has NO large gap
    anywhere in its own history except this trailing one.

    A gap that started before the window but ends inside it is included in
    full (not clipped to the window boundary) -- the window controls which
    gaps are IN SCOPE for reporting, not how large a genuinely large gap is
    allowed to be measured as.
    """
    if not run_timestamps:
        return GapReport(job=job, run_count=0, largest_gap=None, largest_gap_start=None, largest_gap_end=None, last_run_ts=None)

    ts = sorted(run_timestamps)
    augmented = [*ts, as_of]  # sentinel: always check "how long since the last one"
    window_start = as_of - window

    largest: timedelta | None = None
    largest_start: datetime | None = None
    largest_end: datetime | None = None
    for start, end in zip(augmented, augmented[1:]):
        if end < window_start:
            continue  # this gap's end is before the window even opens -- out of scope
        gap = end - start
        if largest is None or gap > largest:
            largest = gap
            largest_start = start
            largest_end = end

    return GapReport(
        job=job,
        run_count=len(ts),
        largest_gap=largest,
        largest_gap_start=largest_start,
        largest_gap_end=largest_end,
        last_run_ts=ts[-1],
    )


def _format_report(report: GapReport, *, max_gap: timedelta) -> str:
    # Plain ASCII "--" in printed output, deliberately, not the "—" em-dash
    # used in this module's own docstrings/comments: this string is what a
    # Task Scheduler log-file redirect actually receives, and Windows'
    # console/file encoding for a redirected stream is not guaranteed to be
    # UTF-8 (verified live: a bare em-dash printed cleanly to an interactive
    # console here but is exactly the class of character that raised
    # UnicodeEncodeError when read back through DuckDB's default printer
    # in this same session's Windows environment) -- operator-facing output
    # should not depend on that.
    if report.run_count == 0:
        return f"job={report.job!r}: NO HEARTBEAT DATA FOUND -- cannot confirm the scheduler ever ran"
    healthy = report.healthy(max_gap)
    verdict = "HEALTHY" if healthy else "UNHEALTHY"
    assert report.largest_gap is not None and report.largest_gap_start is not None and report.largest_gap_end is not None
    gap_minutes = report.largest_gap.total_seconds() / 60.0
    return (
        f"job={report.job!r}: {verdict} -- {report.run_count} run(s) seen; largest gap "
        f"{gap_minutes:.1f} min, from {_fmt_local(report.largest_gap_start)} to "
        f"{_fmt_local(report.largest_gap_end)} (threshold {max_gap.total_seconds() / 60.0:.1f} min); "
        f"last run {_fmt_local(report.last_run_ts)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--max-gap-minutes",
        type=float,
        required=True,
        help="exit non-zero if any job's largest heartbeat gap in the window exceeds this many minutes",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=24.0,
        help="how far back to look for gaps (default: 24 hours). The trailing "
        "'gap to right now' is always checked regardless of this window.",
    )
    parser.add_argument(
        "--job",
        default=None,
        help="restrict to one job's heartbeats (default: report on every job present in the dataset)",
    )
    parser.add_argument(
        "--store-path",
        type=Path,
        default=None,
        help="override the bitemporal store base path (default: <repo>/data/store)",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 UTC instant to evaluate 'now' as (default: the real current time) — "
        "for reproducible checks, not needed in normal operator use",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    as_of = (
        datetime.fromisoformat(args.as_of).astimezone(timezone.utc)
        if args.as_of
        else datetime.now(timezone.utc)
    )
    max_gap = timedelta(minutes=args.max_gap_minutes)
    window = timedelta(hours=args.window_hours)

    store = BitemporalStore(base_path=args.store_path) if args.store_path else BitemporalStore()
    grouped = load_run_timestamps(store, job=args.job, as_of=as_of)

    if not grouped:
        target = f"job={args.job!r}" if args.job else "any job"
        print(f"NO HEARTBEAT DATA FOUND for {target} -- cannot confirm the scheduler ever ran.")
        return 2

    overall_healthy = True
    for job_name, timestamps in sorted(grouped.items()):
        report = compute_gap_report(job_name, timestamps, as_of=as_of, window=window)
        print(_format_report(report, max_gap=max_gap))
        if not report.healthy(max_gap):
            overall_healthy = False

    return 0 if overall_healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
