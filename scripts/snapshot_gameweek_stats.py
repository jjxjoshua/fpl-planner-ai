#!/usr/bin/env python
"""Snapshot the FPL API's own `player.gameweek_stats@gameweek` — session
s005, second provider of an existing capability (vaastav's archive is the
first; `fplai.gameweek_stats.read_player_gameweek_stats` is the reader
that unions both). Closes the gap CLAUDE.md's line 116 names in passing:
"nothing persists `event/{gw}/live/` to the store" — which is the reason
the system could produce a points PMF for a historical fixture but not for
the upcoming gameweek.

See `fplai.providers.fpl`'s module docstring for the full fixture-
attribution design (single-fixture-gameweek fast path vs the degraded
double-gameweek rows) and `fplai.gameweek_stats`'s for the union reader.

**Session s005, continued: `position`/`team` join.** This script is the
ONE live entry point (`scripts/backfill.py`'s CLI does not wire
`--provider fpl_api`) that produces `fpl_api_player_gameweek_stats` rows,
so it is where the ingest-time `position`/`team` resolution actually has
to live. Before writing, it resolves `elements`/`teams` STATE as of this
gameweek's own deadline (`fplai.gameweek_stats.
resolve_elements_and_teams_as_of_deadline` — never "today's" state,
CLAUDE.md rule 2 / lesson 4) and hands both snapshots to the provider as
plain, already-resolved DataFrames — see `providers/fpl.py`'s module
docstring for why the provider itself never reads the store.

House shape: `--gw` and `--season` are BOTH required and never inferred
(CLAUDE.md rule 4 — the same convention `scripts/pin_dc_thresholds.py`
already established for this exact live payload: FPL's API carries no
season field anywhere in it). Gated on `finished && data_checked`, same
signal `pin_dc_thresholds.py` already uses — an UNSETTLED gameweek exits
CLEANLY (code 0), the `scripts/sample_picks.py` convention: a clean exit
means "retry later", not "failure". A settled gameweek is IMMUTABLE (FPL
never revises `event/{gw}/live/` once `data_checked=True`), so re-
fetching an already-ingested (season, gw) is pure waste — this script
skips it (clean exit) BEFORE making any live call, unless `--force-
refetch` is passed.

**Sweep mode (session s007, story S0b).** `--gw`/`--season` above is
unchanged — this adds a second, OPT-IN mode for a scheduled, argument-free
wrapper (`run_snapshot_gameweek_stats.bat`, mirroring `run_snapshot_
bootstrap.bat`/`run_snapshot_odds.bat`'s "dumb frequent interval, script
decides on each firing" shape): `--sweep` makes ONE `bootstrap-static()`
call, reads every gameweek id in its `events` list that is
`finished && data_checked` (the exact same signal `_gw_settled` already
uses, applied once across the whole list rather than per-candidate — a
sweep over ~5 settled gameweeks must not turn into ~5 bootstrap calls),
and ingests whichever of those this store does not already have — reusing
the existing already-ingested short-circuit (`_already_ingested`) and
settlement check UNCHANGED, per candidate, so the sweep is exactly as
cheap and safe to fire hourly as a single already-ingested `--gw` call is
today. `--sweep` takes no `--gw` (mutually exclusive). Season is still
never guessed from thin air (CLAUDE.md rule 4): pass `--season` explicitly
to pin it, or omit it and it is inferred from this store's OWN existing
`fpl_api_player_gameweek_stats` rows (`_infer_season_from_store` — the
lexicographically-latest `season` string present, "2026-27" > "2025-26");
if the dataset has no rows yet, `--sweep` refuses to guess and exits
cleanly (code 0) explaining why, rather than inventing a season.

**Season cross-check (story S0b-a).** Neither path above validated `season`
against anything live — proven by attack (2026-09-05): `--sweep --season
2025-26` against a store that actually held 2026-27 data wrote 1,236 rows
of CURRENT-season live data under the WRONG season label, and exited 0.
This script's only live source is `event/{gw}/live/`, which always answers
for the CURRENT season, so there is no such thing as a legitimate `--season`
that disagrees with the bootstrap this run just fetched — every disagreement
is a mislabelling bug. `_check_season_matches_bootstrap` therefore runs,
on BOTH paths, immediately after every successful bootstrap-static() fetch
(before any candidate is touched): it reads gameweek 1's own `deadline_time`
off that same payload and requires its year to equal `int(season[:4])` (the
house `"YYYY-YY"` convention — a season starts in August of its first year;
a MID-season gameweek's deadline falls in `YYYY+1` and would invert this
check, hence anchoring on GW1 specifically). A mismatch raises
`SeasonMismatchError` and the run ends non-zero — deliberately NOT a clean
exit like the "not yet settled" case, because a season mismatch is never
transient and a clean exit would let an hourly scheduled job hide a season
rollover forever.

Usage:
    python scripts/snapshot_gameweek_stats.py --gw 1 --season 2026-27 [-v]
                                               [--store-path PATH] [--force-refetch]
    python scripts/snapshot_gameweek_stats.py --sweep [--season 2026-27] [-v]
                                               [--store-path PATH] [--force-refetch]
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import polars as pl  # noqa: E402

from fplai.client import FPLApiError, FPLClient  # noqa: E402
from fplai.gameweek_stats import resolve_elements_and_teams_as_of_deadline  # noqa: E402
from fplai.providers.base import ProviderError  # noqa: E402
from fplai.providers.fpl import FPLProvider, PLAYER_GAMEWEEK_STATS_GAMEWEEK  # noqa: E402
from fplai.schemas import CANONICAL_SCHEMAS, JOB_HEARTBEAT_RUN  # noqa: E402
from fplai.store import BitemporalStore  # noqa: E402

logger = logging.getLogger("fplai.snapshot_gameweek_stats")

DATASET = "fpl_api_player_gameweek_stats"
SOURCE = "fpl_api:event_live"
KICKOFF_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# `job` column value for this script's own heartbeat rows (fplai.schemas.
# JOB_HEARTBEAT_RUN's module comment) -- distinct from snapshot_bootstrap's
# and snapshot_odds' own values so runs from different jobs never collide
# in the entity key (job, run_ts, target_dataset). Unlike those two jobs,
# this one only ever writes to ONE downstream dataset (DATASET itself), so
# every heartbeat row this script writes -- one per invocation, --gw/
# --season or --sweep alike -- names DATASET as its target_dataset.
HEARTBEAT_JOB = "snapshot_gameweek_stats"


def _gw_settled(bootstrap: dict, gw: int) -> tuple[bool, str]:
    """Settlement check against an ALREADY-FETCHED bootstrap payload — the
    same `finished && data_checked` signal `scripts/pin_dc_thresholds.py`
    already uses for this identical payload. Split from the live call
    itself (see `main()`) so `deadline_time` can be read off the SAME
    payload without a second live request — this script already needed
    `events` for the settlement check; asking bootstrap-static() a second
    time just to also get `deadline_time` would be a needless extra hit
    against FPL's API (CLAUDE.md's request discipline)."""
    events = {e["id"]: e for e in bootstrap.get("events", [])}
    event = events.get(gw)
    if event is None:
        return False, f"gameweek {gw} not found in a fresh bootstrap-static() events list"
    finished = bool(event.get("finished"))
    data_checked = bool(event.get("data_checked"))
    if finished and data_checked:
        return True, "finished=True, data_checked=True"
    return False, f"finished={finished}, data_checked={data_checked}"


def _deadline_time_for(bootstrap: dict, gw: int) -> str:
    """`events[].deadline_time` for `gw`, off the same bootstrap payload
    `_gw_settled` already checked — raises `ProviderError` rather than
    returning `None`, since a settled gameweek (the only state this is
    ever called for) has always carried this field live so far; a caller
    reaching this with it missing is a genuine anomaly, not an expected
    absence."""
    events = {e["id"]: e for e in bootstrap.get("events", [])}
    event = events.get(gw)
    deadline_time = event.get("deadline_time") if event else None
    if not deadline_time:
        raise ProviderError(f"gameweek {gw}: bootstrap-static events has no deadline_time — genuine anomaly")
    return deadline_time


class SeasonMismatchError(RuntimeError):
    """Raised by `_check_season_matches_bootstrap` when the `season`
    passed via `--season` (or inferred by `--sweep` from the store)
    contradicts the LIVE bootstrap-static() payload this run just
    fetched — story S0b-a. This script's only live source is
    `event/{gw}/live/`, which always answers for the CURRENT season;
    there is therefore no such thing as a legitimate `--season` that
    disagrees with it — every mismatch is a mislabelling bug, never a
    data condition to tolerate. Deliberately NOT a clean exit, unlike
    `_gw_settled`'s "not yet settled" case: a season mismatch never
    resolves by retrying, so treating it as retry-later would let an
    hourly scheduled job hide a season rollover forever (see module
    docstring, "Season cross-check", and the Architect's 2026-09-05
    attack that motivated this: `--sweep --season 2025-26` silently
    wrote 1,236 rows of a DIFFERENT season's live data under the wrong
    label)."""


def _check_season_matches_bootstrap(bootstrap: dict, season: str) -> None:
    """Refuses a `season` that disagrees with THIS bootstrap-static()
    payload's own gameweek-1 `deadline_time` — the one anchor a season
    string can be checked against without inventing a field FPL's API
    doesn't carry (story S0b-a, brief decision 2). Reuses
    `_deadline_time_for` rather than a second deadline reader. Anchored
    on GW1 specifically, never a mid-season gameweek: a Premier League
    season starts in August of `season[:4]` under the house "YYYY-YY"
    convention, so GW1's deadline year must equal `int(season[:4])` — a
    mid-season gameweek's deadline falls in `YYYY+1` and would invert
    this check. Raises `SeasonMismatchError` on a mismatch (never
    returns a verdict a caller could ignore); returns `None` silently
    when `season` agrees."""
    deadline_time = _deadline_time_for(bootstrap, 1)
    actual_year = datetime.strptime(deadline_time, KICKOFF_FORMAT).year
    expected_year = int(season[:4])
    if actual_year != expected_year:
        implied_season = f"{actual_year}-{str(actual_year + 1)[-2:]}"
        raise SeasonMismatchError(
            f"season mismatch: season={season!r} (passed or inferred) implies gameweek 1 falls in "
            f"{expected_year}, but this run's live bootstrap-static() payload has gameweek 1's own "
            f"deadline_time={deadline_time!r} (year {actual_year}), which implies season "
            f"{implied_season!r} instead. Refusing to ingest under the wrong season label -- this is "
            "never transient, retrying will not fix it."
        )


def _already_ingested(store: BitemporalStore, season: str, gw: int) -> int:
    """Number of rows this store already has for (season, gw) in the FPL
    API's own dataset — 0 if the dataset doesn't exist yet or this
    gameweek hasn't been ingested. `store.latest()` (not `effective_at()`)
    is deliberate here: this is an operational "have I already done this"
    check, not a bitemporal training read."""
    existing = store.latest(DATASET)
    if existing.is_empty():
        return 0
    matched = existing.filter((existing["season"] == season) & (existing["round"] == gw))
    return matched.height


def _infer_season_from_store(store: BitemporalStore) -> str | None:
    """`--sweep`'s season, when `--season` is not passed: the
    lexicographically-latest `season` string already present in THIS
    dataset (`fpl_api_player_gameweek_stats`) — season strings are the
    house `"YYYY-YY"` convention (e.g. `"2026-27"`), which sorts correctly
    as a plain string max (`"2026-27" > "2025-26"`), so this needs no date
    parsing. Deliberately reads THIS dataset, not `events` or `elements` —
    neither bootstrap-static capability carries a season field anywhere
    (the same fact `--gw`/`--season`'s own required-ness rests on); the
    only place a season string already lives as DATA is a prior run's own
    write here. Returns `None` if the dataset has never been written at
    all — the caller's job to refuse guessing in that case, not this
    function's."""
    existing = store.latest(DATASET)
    if existing.is_empty() or "season" not in existing.columns:
        return None
    seasons = existing["season"].drop_nulls()
    if seasons.len() == 0:
        return None
    return seasons.max()


def _candidate_gameweeks(bootstrap: dict) -> list[int]:
    """Every gameweek id in a fresh bootstrap-static() payload's `events`
    list that is settled (`finished && data_checked`) — the same signal
    `_gw_settled` checks for one gw at a time, applied once across the
    whole list so `--sweep` never needs a second bootstrap fetch per
    candidate. Sorted ascending; whether ingestion order matters is moot
    (`_ingest_settled_gameweek` is independently idempotent per gw), but
    ascending is the natural read order for a run's own log."""
    settled: list[int] = []
    for event in bootstrap.get("events", []):
        gw = event.get("id")
        if gw is None:
            continue
        if bool(event.get("finished")) and bool(event.get("data_checked")):
            settled.append(gw)
    return sorted(settled)


@dataclass(frozen=True)
class IngestOutcome:
    """What one gameweek's ingest attempt did — the shape both `--gw`/
    `--season` (one call) and `--sweep` (one call per candidate, then
    aggregated by `_aggregate_sweep_outcomes`) build a heartbeat row from.
    `heartbeat_outcome` is one of: "written", "skipped_unchanged",
    "skipped_already_ingested", "skipped_not_settled", "failed"."""

    exit_code: int
    heartbeat_outcome: str
    n_rows: int | None
    error: str | None


def _ingest_settled_gameweek(
    store: BitemporalStore, client: FPLClient, bootstrap: dict, *, gw: int, season: str, force_refetch: bool
) -> IngestOutcome:
    """The per-gameweek worker shared by both modes: given an ALREADY-
    FETCHED bootstrap payload, checks idempotence and settlement (both
    against data already in hand — no live call either way), then resolves
    position/team as of this gameweek's own deadline and writes
    `DATASET`. Identical logic and identical print/log messages to what
    `main()` used to inline directly for the `--gw`/`--season` path — moved
    here unchanged so `--sweep` can call it once per candidate without
    duplicating it."""
    if not force_refetch:
        n_existing = _already_ingested(store, season, gw)
        if n_existing:
            print(
                f"Gameweek {gw} ({season}) already has {n_existing} row(s) in {DATASET!r} -- "
                "a settled gameweek is immutable, nothing to do. Pass --force-refetch to override."
            )
            return IngestOutcome(0, "skipped_already_ingested", None, None)

    settled, reason = _gw_settled(bootstrap, gw)
    logger.info("settlement check: gw=%d settled=%s (%s)", gw, settled, reason)
    if not settled:
        print(f"Gameweek {gw} has not settled yet: {reason}")
        print("This is an EXPECTED state, not an error. Nothing to ingest yet.")
        return IngestOutcome(0, "skipped_not_settled", None, None)

    try:
        deadline_time = _deadline_time_for(bootstrap, gw)
    except ProviderError as exc:
        logger.error("gw=%d: cannot resolve deadline: %s", gw, exc)
        return IngestOutcome(1, "failed", None, str(exc))

    snapshots = resolve_elements_and_teams_as_of_deadline(store, deadline_time)
    logger.info(
        "resolved elements/teams as of gameweek %d's deadline %s (%d elements, %d teams known by then)",
        gw, snapshots.deadline.isoformat(), snapshots.elements.height, snapshots.teams.height,
    )
    if snapshots.elements.is_empty() or snapshots.teams.is_empty():
        logger.warning(
            "gw=%d: no elements/teams observation exists at or before %s -- position/team will be NULL "
            "on every row this ingest produces. This is honest degradation (never guessed), but "
            "means the load-bearing gap this session closed is NOT closed for this particular "
            "gameweek. Check snapshot_bootstrap.py's own cadence around this deadline.",
            gw, snapshots.deadline.isoformat(),
        )

    provider = FPLProvider(client=client)
    try:
        result = provider.fetch(
            PLAYER_GAMEWEEK_STATS_GAMEWEEK, force_refresh=True, gameweek=gw, season=season,
            elements_as_of=snapshots.elements, teams_as_of=snapshots.teams,
        )
    except ProviderError as exc:
        logger.error("gw=%d: fetch failed: %s", gw, exc)
        return IngestOutcome(1, "failed", None, str(exc))

    kickoffs = result.rows["kickoff_time"].drop_nulls()
    if kickoffs.len() == 0:
        logger.error("gw=%d: fetched batch has no non-null kickoff_time -- cannot anchor valid_at, refusing to write", gw)
        return IngestOutcome(1, "failed", None, "fetched batch has no non-null kickoff_time")
    valid_at = datetime.strptime(kickoffs.min(), KICKOFF_FORMAT).replace(tzinfo=timezone.utc)

    write_result = store.write(
        DATASET,
        result.rows,
        valid_at=valid_at,
        observed_at=result.observed_at,
        source=SOURCE,
        provider_id=result.provider_id,
        capability=str(result.capability),
        endpoint=result.endpoint,
    )

    n_dgw = result.meta.get("n_dgw_elements", 0)
    n_blank = result.meta.get("n_blank_elements", 0)
    n_pt_unresolved = result.meta.get("n_position_team_unresolved", 0)
    n_ho_unresolved = result.meta.get("n_was_home_opponent_unresolved", 0)
    print(
        f"{'Wrote' if write_result.written else 'Unchanged, skipped'} {write_result.n_rows} row(s) to "
        f"{DATASET!r} for season={season} gw={gw}. "
        f"{n_dgw} double-gameweek element(s) (degraded per-fixture attribution), "
        f"{n_blank} blank-gameweek element(s) skipped, "
        f"{n_pt_unresolved} element(s) with position/team unresolved as of the deadline, "
        f"{n_ho_unresolved} element(s) with was_home/opponent_team unresolved as of the deadline."
    )
    return IngestOutcome(0, "written" if write_result.written else "skipped_unchanged", write_result.n_rows, None)


def _aggregate_sweep_outcomes(outcomes: list[tuple[int, IngestOutcome]]) -> IngestOutcome:
    """Collapse one `--sweep` run's per-gameweek `IngestOutcome`s into the
    ONE heartbeat row this job writes per invocation (this job has a single
    target dataset, unlike `snapshot_odds.py`'s two — no per-target split
    needed, just per-run aggregation across however many candidates this
    run considered). `outcome="written"` if ANY candidate wrote a new
    batch (even alongside some already-ingested or failed); `"failed"` if
    none wrote but at least one failed; `"skipped_unchanged"` if every
    candidate was already-ingested/settled-but-unchanged and none failed —
    the common, expected, hourly no-op. `error` concatenates every failed
    candidate's own message so a partial failure is never silent even
    when the overall run still wrote something."""
    written = [(gw, o) for gw, o in outcomes if o.heartbeat_outcome == "written"]
    failed = [(gw, o) for gw, o in outcomes if o.heartbeat_outcome == "failed"]
    error = "; ".join(f"gw={gw}: {o.error}" for gw, o in failed) if failed else None
    if written:
        n_rows = sum(o.n_rows or 0 for _, o in written)
        return IngestOutcome(1 if failed else 0, "written", n_rows, error)
    if failed:
        return IngestOutcome(1, "failed", None, error)
    return IngestOutcome(0, "skipped_unchanged", None, None)


def build_heartbeat_row(*, job: str, run_ts: datetime, outcome: str, n_rows: int | None, error: str | None) -> pl.DataFrame:
    """One row — this job has exactly one target dataset (`DATASET`
    itself), unlike `snapshot_bootstrap.py`'s six or `snapshot_odds.py`'s
    two, so there is no per-target split to build. `run_ts` normalised to
    naive UTC before writing — `FactTableSchema.validate`'s tz-aware-column
    check, same convention every other heartbeat writer in this codebase
    already follows."""
    run_ts_naive = run_ts.astimezone(timezone.utc).replace(tzinfo=None)
    return pl.DataFrame(
        [
            {
                "job": job,
                "run_ts": run_ts_naive,
                "target_dataset": DATASET,
                "outcome": outcome,
                "payload_hash": None,
                "n_rows": n_rows,
                "error": error,
            }
        ]
    )


def write_heartbeat(store: BitemporalStore, df: pl.DataFrame, *, run_ts: datetime) -> None:
    """Same literal `skip_if_unchanged=False` contract as `snapshot_
    bootstrap.py::write_heartbeat` / `snapshot_odds.py::write_heartbeat` —
    see either docstring for why this must never become `True` or fall
    into `store.write()`'s own default: two genuinely-identical no-op
    firings (e.g. two consecutive `skipped_already_ingested` sweeps an
    hour apart) must both persist, or a stalled scheduler and a quiet API
    become indistinguishable from the heartbeat stream alone."""
    CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN].validate(df)
    store.write(
        "heartbeat",
        df,
        valid_at=run_ts,
        observed_at=run_ts,
        source="fplai.snapshot_gameweek_stats:heartbeat",
        skip_if_unchanged=False,
    )


def write_heartbeat_safely(
    store: BitemporalStore, *, run_ts: datetime, outcome: str, n_rows: int | None, error: str | None
) -> None:
    """A monitoring feature must never take down the thing it monitors —
    same guarantee, same deliberately broad `except Exception`, as
    `snapshot_bootstrap.py::write_heartbeat_safely`. Whatever goes wrong
    building or writing the heartbeat row, this run's own ingest outcome
    (already decided by the time this is called) is unaffected."""
    try:
        df = build_heartbeat_row(job=HEARTBEAT_JOB, run_ts=run_ts, outcome=outcome, n_rows=n_rows, error=error)
        write_heartbeat(store, df, run_ts=run_ts)
    except Exception as exc:  # noqa: BLE001 -- see docstring: must never propagate
        logger.error("heartbeat write failed (ingest outcome unaffected by this): %s", exc)


def _main_single(store: BitemporalStore, client: FPLClient, *, gw: int, season: str, force_refetch: bool, run_ts: datetime) -> int:
    """The original `--gw`/`--season` path, unchanged in behaviour: checks
    idempotence against the store FIRST, before any live call at all
    (preserved exactly — see module docstring), then fetches bootstrap-
    static() once and delegates the settlement check + fetch + write to
    `_ingest_settled_gameweek`. Writes exactly one heartbeat row before
    returning, on every path (already-ingested, not-yet-settled, or a real
    attempt), which `--gw`/`--season` never did before this story."""
    if not force_refetch:
        n_existing = _already_ingested(store, season, gw)
        if n_existing:
            print(
                f"Gameweek {gw} ({season}) already has {n_existing} row(s) in {DATASET!r} -- "
                "a settled gameweek is immutable, nothing to do. Pass --force-refetch to override."
            )
            write_heartbeat_safely(store, run_ts=run_ts, outcome="skipped_already_ingested", n_rows=None, error=None)
            return 0

    try:
        bootstrap = client.bootstrap_static(force_refresh=True)
    except FPLApiError as exc:
        logger.info("bootstrap-static() call failed (%s) -- treating as not-yet-settled, not an error", exc)
        print(f"Gameweek {gw} has not settled yet: bootstrap-static() call failed ({exc})")
        print("This is an EXPECTED state, not an error. Nothing to ingest yet.")
        write_heartbeat_safely(
            store, run_ts=run_ts, outcome="skipped_not_settled", n_rows=None,
            error=f"bootstrap-static() call failed: {exc}",
        )
        return 0

    _check_season_matches_bootstrap(bootstrap, season)

    outcome = _ingest_settled_gameweek(store, client, bootstrap, gw=gw, season=season, force_refetch=force_refetch)
    write_heartbeat_safely(
        store, run_ts=run_ts, outcome=outcome.heartbeat_outcome, n_rows=outcome.n_rows, error=outcome.error
    )
    return outcome.exit_code


def _main_sweep(store: BitemporalStore, client: FPLClient, *, season: str | None, force_refetch: bool, run_ts: datetime) -> int:
    """`--sweep`: one bootstrap-static() call, every settled gameweek this
    store lacks gets one `_ingest_settled_gameweek` attempt, one aggregated
    heartbeat row per invocation regardless of how many candidates were
    considered (module docstring "Sweep mode" has the full design)."""
    if season is None:
        season = _infer_season_from_store(store)
    if season is None:
        msg = (
            f"--sweep could not infer a season: {DATASET!r} has no existing rows to infer one from, "
            "and --season was not supplied. Run one manual `--gw <n> --season <s>` ingest first "
            "(never guessed -- CLAUDE.md rule 4)."
        )
        print(msg)
        write_heartbeat_safely(store, run_ts=run_ts, outcome="skipped_no_season", n_rows=None, error=msg)
        return 0

    try:
        bootstrap = client.bootstrap_static(force_refresh=True)
    except FPLApiError as exc:
        msg = f"bootstrap-static() call failed: {exc}"
        logger.info("%s -- treating as not-yet-settled, not an error", msg)
        print(f"--sweep: {msg}. Nothing to ingest this run -- this is an EXPECTED state, not an error.")
        write_heartbeat_safely(store, run_ts=run_ts, outcome="skipped_not_settled", n_rows=None, error=msg)
        return 0

    _check_season_matches_bootstrap(bootstrap, season)

    candidates = _candidate_gameweeks(bootstrap)
    if not candidates:
        print(f"--sweep: no settled gameweek found for season={season} in this bootstrap-static() payload.")
        write_heartbeat_safely(store, run_ts=run_ts, outcome="skipped_nothing_to_ingest", n_rows=0, error=None)
        return 0

    outcomes: list[tuple[int, IngestOutcome]] = []
    for gw in candidates:
        outcomes.append((gw, _ingest_settled_gameweek(store, client, bootstrap, gw=gw, season=season, force_refetch=force_refetch)))

    aggregate = _aggregate_sweep_outcomes(outcomes)
    write_heartbeat_safely(
        store, run_ts=run_ts, outcome=aggregate.heartbeat_outcome, n_rows=aggregate.n_rows, error=aggregate.error
    )

    n_written = sum(1 for _, o in outcomes if o.heartbeat_outcome == "written")
    n_already = sum(1 for _, o in outcomes if o.heartbeat_outcome == "skipped_already_ingested")
    n_failed = sum(1 for _, o in outcomes if o.heartbeat_outcome == "failed")
    print(
        f"--sweep: processed {len(candidates)} settled gameweek(s) for season={season}: "
        f"{n_written} written, {n_already} already ingested, {n_failed} failed."
    )
    return aggregate.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gw", type=int, default=None, help="Gameweek to ingest. Required unless --sweep is passed; mutually exclusive with --sweep.")
    parser.add_argument(
        "--season",
        default=None,
        help="Season string this gameweek belongs to (e.g. '2026-27'). Required for the --gw path, never "
        "inferred there: FPL's own live API carries no season field anywhere (same convention "
        "scripts/pin_dc_thresholds.py already established for this identical payload). Optional for "
        "--sweep: if omitted, inferred from this store's own existing rows (see module docstring).",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Ingest every SETTLED gameweek this store does not yet have, one bootstrap-static() call "
        "for the whole sweep -- for a scheduled, argument-free wrapper. Mutually exclusive with --gw.",
    )
    parser.add_argument("--store-path", type=Path, default=None, help="override the bitemporal store base path")
    parser.add_argument(
        "--force-refetch",
        action="store_true",
        help="Re-fetch and re-write even if this store already has rows for this (season, gw) -- "
        "for correcting a bad ingest only; a settled gameweek is otherwise immutable.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.sweep and args.gw is not None:
        parser.error("--sweep and --gw are mutually exclusive")
    if not args.sweep and (args.gw is None or args.season is None):
        parser.error("--gw and --season are both required unless --sweep is passed")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = BitemporalStore(base_path=args.store_path) if args.store_path else BitemporalStore()
    client = FPLClient()
    # `run_ts` captured once, before either mode's own work, and reused for
    # that mode's single heartbeat write -- one run, one timestamp, same
    # convention snapshot_bootstrap.py/snapshot_odds.py already use.
    run_ts = datetime.now(timezone.utc)

    if args.sweep:
        return _main_sweep(store, client, season=args.season, force_refetch=args.force_refetch, run_ts=run_ts)
    return _main_single(store, client, gw=args.gw, season=args.season, force_refetch=args.force_refetch, run_ts=run_ts)


if __name__ == "__main__":
    raise SystemExit(main())
