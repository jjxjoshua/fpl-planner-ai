#!/usr/bin/env python
"""Sample `entry/{id}/event/{gw}/picks/` to build the effective-ownership
(EO) panel — blueprint §3.4. This is the only source of real captaincy data
and it has NO archive: an unsampled gameweek is a permanent hole.

Method (blueprint §3.4, wiki §2.4): uniform random draw over the entry-id
space, NOT league-314 paging (paging is rank-ordered — clustered, not
independent). The id space is dense (~99.95% hit rate), so a uniform draw
over [1, max_id] is an unbiased sample of the registered population, which
is the same denominator FPL uses for `selected_by_percent` — that gives a
free, mandatory calibration check.

**`picks/` response shape: VERIFIED live, 22 Aug 2026** (docs/HANDOFF.md §1).
Every `_picks_to_rows` assumption held; entity key `(entry_id, event,
element)` verified unique on real data.

**Pre-deadline gate-repair session s003 (docs/HANDOFF.md §1's "three
blockers") — three defects fixed here, all found by live probing against
the real API in the 17:30-17:41 UTC / 01:31-01:41 local maintenance window
on 21-22 Aug and the days around it:**

1. **A single 503 no longer destroys the run.** `entry/{id}/event/{gw}/
   picks/` returns HTTP 503 ("The game is being updated.") for the whole
   post-deadline maintenance window (~27 min, measured live) — a THIRD
   response state this script and `FPLClient` used to model only as
   404-or-200. `FPLClient` now retries 503 (client.py's
   `FPL_BACKOFF_TRIGGER_STATUSES`); this script additionally (a) probes
   `picks/` ITSELF before committing to the full run
   (`probe_picks_endpoint_ready` — checking `bootstrap_static()` alone was
   a FALSE GREEN, verified live: the maintenance window is endpoint-scoped,
   `bootstrap-static/` stayed 200 the whole time `picks/` was 503ing);
   (b) catches `FPLApiError` PER ENTRY in the main loop, never letting one
   bad response end the run; and (c) persists INCREMENTALLY in chunks (see
   `run()`'s docstring) so a crash costs the current chunk, not the whole
   ~83-minute run.
2. **Cached 404s can no longer silently bias the sample frame.**
   `FPLClient`'s default cache now expires a cached 404 after
   `client.ENTRY_404_TTL_SECONDS` (transport.py's `ResponseCache` — see its
   docstring). Verified live against the real (pre-existing) poisoned
   entries this session: all 9 previously-cached `entry/{id}/` 404s are now
   cache MISSES, without touching a single file under `cache/` (read-only
   for this story) — a 404 with no stored timestamp (every file written
   before this fix) is treated as infinitely stale, not infinitely fresh.
3. **Widened `_picks_to_rows`, and `automatic_subs[]` gets its own capability
   and dataset** (`schemas.MANAGER_AUTOMATIC_SUBS_GAMEWEEK`, dataset
   `automatic_subs`) — see that schema's docstring for the full grain
   argument (it is a DIFFERENT GRAIN from `picks[]`, not a column to bolt
   onto a picks row). `automatic_subs` was `[]` in every real payload
   captured this session (11/11, all pre-scoring) — this script therefore
   ALSO enforces its declared entity key's uniqueness AT RUNTIME, on every
   real batch it ever collects (`_assert_automatic_subs_key_uniqueness`),
   because the design could not be verified against a populated real
   payload ahead of time (GW1 had not finished scoring).

Usage:
    python scripts/sample_picks.py --gw 1 [--n 10000] [--seed N]
                                    [--store-path PATH] [--dry-run]
                                    [--chunk-size 500] [--no-resume]

If run before the target gameweek's deadline, before any gameweek has
finished (pre-season), or while the picks/ endpoint is in FPL's post-
deadline maintenance window, this exits cleanly (code 0) with a message —
none of these are treated as errors; all are expected, retryable states.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fplai.client import FPLApiError, FPLClient  # noqa: E402
from fplai.sampling import (  # noqa: E402
    already_sampled_entry_ids,
    calibration_check,
    draw_uniform_entry_ids,
    find_max_valid_entry_id,
)
from fplai.store import BitemporalStore  # noqa: E402

logger = logging.getLogger("fplai.sample_picks")

SOURCE = "fpl_api:entry_picks"
PICKS_DATASET = "picks"
AUTOMATIC_SUBS_DATASET = "automatic_subs"

DEFAULT_N = 10_000
PROBE_SIZE = 10  # small warm-up batch to detect "picks/ not public yet" before spending the full budget

# Both the progress-log cadence AND the incremental-persistence unit
# (blueprint §3.4 "Blocker 1") — a crash costs at most this many entries'
# worth of live requests, not the whole ~83-minute run. 500 entries is
# ~250s (~4 min) of work at 2 req/s.
CHUNK_SIZE = 500

# Same "id 1 always resolves" assumption find_max_valid_entry_id's own
# lower_bound already depends on — not a new, project-specific assumption.
READINESS_PROBE_ENTRY_ID = 1

# Bounded retry count for the MAIN LOOP's per-entry requests — deliberately
# smaller than FPLClient's own default (3, ~7 minutes worst case per
# request). If maintenance genuinely resumes mid-run, retrying every one of
# 10,000 entries for up to 7 minutes each would turn an 83-minute run into
# many hours; 1 retry bounds one failing entry's added latency to
# ~backoff_base_seconds (60s default) while still absorbing a single
# transient blip. See client.py's `_get` docstring for the `max_retries`
# override this relies on.
MAIN_LOOP_MAX_RETRIES = 1


class NotReadyError(Exception):
    """Raised (and caught) when picks are not yet publicly readable, or the
    picks/ endpoint is currently in a maintenance/error state — an
    expected, non-error, RETRYABLE precondition, not a bug."""


class AutomaticSubsGrainError(RuntimeError):
    """Not currently raised — see `_assert_automatic_subs_key_uniqueness`,
    which deliberately logs and DROPS a violating batch rather than raising,
    so a defect in this bonus capability can never take down the
    irreplaceable picks/captaincy capture alongside it. Kept as a named
    exception class (rather than encoding the failure only in a log line)
    so a future caller that wants a raise instead has a type to catch."""


def _parse_deadline(deadline_time: str) -> datetime:
    # FPL emits e.g. "2026-08-22T17:30:00Z"; Python 3.11+ fromisoformat
    # accepts the trailing Z directly.
    dt = datetime.fromisoformat(deadline_time)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_target_event(events: list[dict], requested_gw: int | None) -> dict | None:
    """Pick the event (gameweek) to sample. If `requested_gw` is given, use
    it (even if not yet finished — the deadline check downstream will catch
    that). Otherwise auto-select the most recently finished gameweek, or
    None if none has finished yet (pre-season)."""
    by_id = {e["id"]: e for e in events}
    if requested_gw is not None:
        event = by_id.get(requested_gw)
        if event is None:
            raise ValueError(f"GW{requested_gw} not found in bootstrap-static events")
        return event
    finished = [e for e in events if e.get("finished")]
    if not finished:
        return None
    return max(finished, key=lambda e: e["id"])


def check_ready(client: FPLClient, event: dict) -> None:
    """Raise NotReadyError if the DEADLINE hasn't passed yet — picks 404
    unconditionally before it (verified). This is a NECESSARY but NOT
    SUFFICIENT gate: it says nothing about whether picks/ itself is
    currently serving cleanly (maintenance-window 503s happen strictly
    AFTER the deadline, which this check alone cannot see). See
    `probe_picks_endpoint_ready` below for the gate that actually covers
    that — `run()` calls both, in order."""
    now = datetime.now(timezone.utc)
    deadline = _parse_deadline(event["deadline_time"])
    if now < deadline:
        raise NotReadyError(
            f"GW{event['id']} deadline is {deadline.isoformat()} (UTC), which is in the future. "
            "entry_picks/ 404s for every entry until the deadline passes (verified, "
            "docs/wiki/data-sources.md sec. 2.1). Nothing sampled - this is expected, not an error."
        )
    if not event.get("finished"):
        logger.warning(
            "GW%s deadline has passed but the event is not marked finished yet - "
            "picks may exist but scores/bonus may still be settling. Proceeding anyway; "
            "wiki sec. 2.7 recommends waiting until event-status/ shows bonus added.",
            event["id"],
        )


def probe_picks_endpoint_ready(client: FPLClient, event_id: int) -> None:
    """The gate `check_ready` above cannot provide: does the picks/ endpoint
    ITSELF answer cleanly right now, rather than 503ing through FPL's
    post-deadline maintenance window? Verified live, 21-22 Aug: the window
    is ENDPOINT-SCOPED, not global — `bootstrap_static()` stayed 200 the
    entire time `entry/{id}/event/{gw}/picks/` was returning 503 ("The game
    is being updated."). Gating readiness on bootstrap-static (the previous
    behaviour — `run()` calls it unconditionally at the top for `events`/
    `elements`) is therefore a FALSE GREEN: it passes, the run proceeds,
    and it dies on the first real picks/ request.

    Issues ONE live probe (`force_refresh=True` — a cached 404/200 for this
    entry+event would let this check pass WITHOUT ever touching the live
    endpoint, defeating its whole purpose; `max_retries=0` — a readiness
    probe must fail FAST, not sit through the ~7-minute internal backoff
    ladder for a request already suspected to fail) against
    `entry/{READINESS_PROBE_ENTRY_ID}/event/{event_id}/picks/`.

    A 404 here is a LEGITIMATE ready signal — the endpoint answered
    DEFINITIVELY (entry 1 may simply not have set a squad this gameweek);
    only a raised `FPLApiError` (429/403/503, exhausting the single
    attempt) means "not ready", translated to `NotReadyError` so `run()`
    exits cleanly (code 0) exactly like the pre-deadline case, instead of
    crashing or — worse — proceeding into the same wall the old code hit.
    """
    try:
        client.entry_picks(READINESS_PROBE_ENTRY_ID, event_id, force_refresh=True, max_retries=0)
    except FPLApiError as exc:
        raise NotReadyError(
            f"GW{event_id} picks/ endpoint is not currently serving cleanly ({exc}). This is very "
            "likely FPL's post-deadline maintenance window ('The game is being updated.', verified "
            "live to last ~27 minutes on GW1) — a TEMPORAL state, not a permanent failure. "
            "Nothing sampled this run; re-run later (the scheduled task will retry automatically)."
        ) from exc


def probe_picks_available(
    client: FPLClient, entry_ids: list[int], event_id: int
) -> tuple[bool, int]:
    """Fetch a small warm-up batch. If every single one 404s, treat the
    endpoint as not-yet-serving-picks rather than silently writing a
    near-empty, badly biased sample.

    Widened (blueprint §3.4 "Blocker 1"): catches `FPLApiError` PER ENTRY —
    the original version let one bad response crash the whole probe before
    the main loop was even reached. Returns `(available, n_errors)` rather
    than a bare bool so the caller can distinguish "endpoint answered but
    nobody in the warm-up batch had picks" from "the endpoint itself is
    erroring" — a probe where every entry ERRORS (not 404s) after already
    passing `probe_picks_endpoint_ready` is a strong signal the maintenance
    window has resumed mid-startup, worth its own message.
    """
    hits = 0
    errors = 0
    for entry_id in entry_ids[:PROBE_SIZE]:
        try:
            if client.entry_picks(entry_id, event_id, max_retries=MAIN_LOOP_MAX_RETRIES) is not None:
                hits += 1
        except FPLApiError as exc:
            errors += 1
            logger.warning("probe: entry_id=%d errored (not a 404): %s", entry_id, exc)
    return hits > 0, errors


def _picks_to_rows(entry_id: int, event_id: int, picks_response: dict) -> list[dict]:
    """Widened, blueprint §3.4 "Blocker 3" — fields the payload already
    carries at zero marginal request cost. See schemas.py's
    MANAGER_PICKS_SELECTION_GAMEWEEK docstring for exactly which of these
    are declared `required_fields` (only `overall_rank`, promoted this
    session) versus present-but-optional (everything else added here) —
    the split exists because `fplai.providers.fpl._picks_to_rows`, a
    separate implementation of this same capability, was out of this
    story's owned paths to widen in step; see that schema's description for
    the full account, flagged as a finding for the Architect."""
    entry_history = picks_response.get("entry_history") or {}
    active_chip = picks_response.get("active_chip")
    rows = []
    for pick in picks_response.get("picks", []):
        rows.append(
            {
                "event": event_id,
                "entry_id": entry_id,
                "element": pick.get("element"),
                "element_type": pick.get("element_type"),
                "position": pick.get("position"),
                "multiplier": pick.get("multiplier"),
                "is_captain": pick.get("is_captain"),
                "is_vice_captain": pick.get("is_vice_captain"),
                "active_chip": active_chip,
                "bank": entry_history.get("bank"),
                "team_value": entry_history.get("value"),
                "event_transfers": entry_history.get("event_transfers"),
                "event_transfers_cost": entry_history.get("event_transfers_cost"),
                "points_on_bench": entry_history.get("points_on_bench"),
                "overall_rank": entry_history.get("overall_rank"),
                # entry_history's remaining fields, previously discarded:
                "entry_history_event": entry_history.get("event"),
                "gameweek_points": entry_history.get("points"),
                "total_points": entry_history.get("total_points"),
                "rank": entry_history.get("rank"),
                "rank_sort": entry_history.get("rank_sort"),
                "percentile_rank": entry_history.get("percentile_rank"),
                "overall_rank_percentage": entry_history.get("overall_rank_percentage"),
            }
        )
    return rows


def _automatic_subs_to_rows(entry_id: int, event_id: int, picks_response: dict) -> list[dict]:
    """The TOP-LEVEL `automatic_subs[]` list — a DIFFERENT GRAIN from
    `picks[]`, see `schemas.MANAGER_AUTOMATIC_SUBS_GAMEWEEK`'s docstring.
    Field names (`entry`/`element_in`/`element_out`/`event`) are per the FPL
    API's own documented shape — NOT independently live-verified against a
    populated example this session (GW1 had not finished scoring; every
    real payload captured had `automatic_subs: []`). `entry`/`event` are
    dropped in favour of the caller-supplied `entry_id`/`event_id` (this
    function's own parameters), matching `_picks_to_rows`'s convention of
    trusting the request context over an echoed response field, in case the
    two ever disagree."""
    rows = []
    for sub in picks_response.get("automatic_subs") or []:
        rows.append(
            {
                "entry_id": entry_id,
                "event": event_id,
                "element_in": sub.get("element_in"),
                "element_out": sub.get("element_out"),
            }
        )
    return rows


def _assert_automatic_subs_key_uniqueness(df: pl.DataFrame) -> pl.DataFrame:
    """Runtime enforcement of `MANAGER_AUTOMATIC_SUBS_GAMEWEEK`'s declared
    entity key `(entry_id, event, element_out)`, on EVERY real batch this
    script ever collects — the check the blueprint §3.4 "Blocker 3" brief
    asks for ("verify the key by uniqueness within one observation batch
    against a real payload"), which could NOT be performed ahead of time
    this session because GW1 had not finished scoring (every real
    automatic_subs list captured was empty). This is the substitute: an
    always-on, load-bearing check against whatever the FIRST real populated
    batch actually looks like, rather than a one-off dev-time check that
    was structurally impossible to run yet.

    On a violation: logs the offending rows LOUDLY (never silently) and
    returns an EMPTY frame — this capability is explicitly a free bonus
    (blueprint §3.4: "marginal cost is zero"), and a design defect in it
    must never be allowed to jeopardise the picks/captaincy capture
    alongside it, which is irreplaceable. Does not raise, by design; see
    `AutomaticSubsGrainError`'s own docstring.
    """
    key_cols = ["entry_id", "event", "element_out"]
    if df.is_empty():
        return df
    dup_mask = df.select(key_cols).is_duplicated()
    if dup_mask.any():
        offenders = df.filter(dup_mask)
        logger.error(
            "AUTOMATIC_SUBS GRAIN VIOLATION: %d row(s) violate the declared entity key %s within "
            "one batch — the design assumption in schemas.py's MANAGER_AUTOMATIC_SUBS_GAMEWEEK "
            "docstring does not hold for this real payload. NOT persisting automatic_subs for this "
            "chunk (picks/captaincy data is unaffected and proceeds normally). This needs an "
            "Architect decision before the next real capture, not a silent workaround. Offending "
            "rows: %s",
            offenders.height,
            key_cols,
            offenders.to_dicts(),
        )
        return df.clear()
    return df


def _persist_picks_chunk(
    store: BitemporalStore, rows: list[dict], deadline: datetime, observed_at: datetime
) -> pl.DataFrame | None:
    """One incremental batch write for the `picks` dataset — blueprint §3.4
    "Blocker 1". `valid_at=deadline` stays CONSTANT across every chunk of
    one gameweek's sample: picks are a domain fact about what was locked in
    AT the deadline, genuinely time-invariant across the whole ~83-minute
    collection window, so this is not a fabricated constant the way a
    single global `observed_at` would be. `observed_at`, by contrast, is
    THIS chunk's own real collection time — never one `now()` computed
    after the full loop and smeared across rows actually gathered over
    83 minutes (the append-only store's `write()` stamps one scalar
    `observed_at` per BATCH, which is exactly right applied per CHUNK and
    exactly wrong applied to the whole run at once).

    Returns the chunk's own DataFrame (for the caller's calibration
    bookkeeping) or `None` if there was nothing to write."""
    if not rows:
        return None
    df = pl.DataFrame(rows, infer_schema_length=None)
    result = store.write(
        PICKS_DATASET,
        df,
        valid_at=deadline,
        observed_at=observed_at,
        source=SOURCE,
        skip_if_unchanged=False,  # every sample is a fresh, independent draw — never dedup away a real sample
    )
    logger.info(
        "persisted picks CHUNK: rows=%d written=%s path=%s observed_at=%s",
        result.n_rows,
        result.written,
        result.path,
        observed_at.isoformat(),
    )
    return df


def _persist_automatic_subs_chunk(
    store: BitemporalStore, rows: list[dict], deadline: datetime, observed_at: datetime
) -> None:
    """Same incremental-chunk convention as `_persist_picks_chunk`, for the
    separate `automatic_subs` dataset — see that function's docstring for
    the `valid_at`/`observed_at` reasoning, identical here."""
    if not rows:
        return
    df = pl.DataFrame(rows, infer_schema_length=None)
    df = _assert_automatic_subs_key_uniqueness(df)
    if df.is_empty():
        return
    result = store.write(
        AUTOMATIC_SUBS_DATASET,
        df,
        valid_at=deadline,
        observed_at=observed_at,
        source=SOURCE,
        skip_if_unchanged=False,
    )
    logger.info(
        "persisted automatic_subs CHUNK: rows=%d written=%s path=%s",
        result.n_rows,
        result.written,
        result.path,
    )


def run(
    store: BitemporalStore,
    client: FPLClient,
    *,
    requested_gw: int | None,
    n: int,
    seed: int | None,
    dry_run: bool,
    calibration_threshold_pp: float,
    resume: bool = True,
    chunk_size: int = CHUNK_SIZE,
) -> int:
    """See the module docstring's "three blockers" section for the full
    account of what changed here versus the pre-s003 version.

    **Incremental persistence against an append-only, batch-stamped store —
    the design this function embodies:** `BitemporalStore.write()` stamps
    ONE `valid_at`/`observed_at` pair per call, across the whole batch
    passed to it. The honest reading of "a picks sample" against that
    contract is NOT one call at the end (the pre-fix behaviour — a single
    503 anywhere in ~83 minutes discarded everything) and NOT literally one
    `write()` per entry either (that would spend a whole batch-write, and a
    whole distinct `observed_at`, on every single request — noise, not
    honesty, since consecutive entries a few seconds apart do not represent
    meaningfully different observation instants). The resolution is a
    CHUNK: `chunk_size` (default `CHUNK_SIZE`, ~4 minutes of requests)
    entries' worth of rows accumulate in memory, then get ONE `write()`
    call stamped with THIS chunk's own real completion time — bounding a
    crash's cost to the current chunk, and keeping every `observed_at` an
    honest reflection of when those specific rows were actually collected,
    never a single timestamp smeared across the whole run.

    **Resumability** (`resume=True` by default) is the other half:
    `sampling.already_sampled_entry_ids` queries the store for entry_ids
    already persisted for this event from a PRIOR (crashed or otherwise
    incomplete) run, and this run's deterministic draw (same event, same
    seed) skips them — a restart costs only the entries not yet safely on
    disk, not the whole 10,000 again. A genuine 404 MISS is indistinguishable
    from "never attempted" and will be re-requested on resume (documented,
    deliberate — see that function's own docstring for the trade-off).
    """
    bootstrap = client.bootstrap_static()
    events = bootstrap["events"]
    elements_df = pl.DataFrame(bootstrap["elements"])

    event = resolve_target_event(events, requested_gw)
    if event is None:
        logger.info("no gameweek has finished yet (pre-season) — nothing to sample. Exiting cleanly.")
        return 0

    try:
        check_ready(client, event)
    except NotReadyError as exc:
        logger.info(str(exc))
        return 0

    event_id = event["id"]

    # Blocker 1's central fix: probe the ACTUAL endpoint being sampled, not
    # a proxy for it. Must happen AFTER check_ready (deadline gate) and
    # BEFORE any budget-spending work below.
    try:
        probe_picks_endpoint_ready(client, event_id)
    except NotReadyError as exc:
        logger.info(str(exc))
        return 0

    effective_seed = seed if seed is not None else 1_000_000 + event_id
    logger.info("sampling GW%d picks: n=%d, seed=%d (deterministic)", event_id, n, effective_seed)

    max_id = find_max_valid_entry_id(client)
    logger.info("max valid entry id: %d (bootstrap total_players=%d)", max_id, bootstrap.get("total_players", -1))

    full_entry_ids = draw_uniform_entry_ids(max_id, n, effective_seed)
    deadline = _parse_deadline(event["deadline_time"])

    # Resumability filtering happens BEFORE the warm-up probe below,
    # deliberately — the probe draws its warm-up batch from whatever list
    # it is given, and that list used to be `full_entry_ids` (the UN-
    # filtered draw) regardless of resume state. On a resumed run that
    # meant re-issuing live requests for entries already safely persisted
    # from a prior run, just to run the probe — a small but real, avoidable
    # cost this reordering removes: the probe now only ever touches entries
    # this run will actually need to fetch.
    entry_ids = full_entry_ids
    if resume:
        already_done = already_sampled_entry_ids(store, event_id)
        if already_done:
            before = len(entry_ids)
            entry_ids = [e for e in entry_ids if e not in already_done]
            logger.info(
                "RESUME: %d/%d entries already persisted for GW%d from a prior run — skipping "
                "them (no live request re-issued for a prior HIT, including in the probe below), "
                "%d remaining this run.",
                before - len(entry_ids),
                before,
                event_id,
                len(entry_ids),
            )

    total_hits = total_misses = total_errors = 0
    if not entry_ids:
        logger.info("RESUME: every entry in this draw was already sampled for GW%d — nothing left to request.", event_id)
        if dry_run:
            # Nothing left to probe or fetch either — dry-run's contract is
            # "stop before spending the full budget", trivially satisfied
            # here since there is no budget left to spend. Returning here
            # (rather than falling through to the calibration read-back
            # below) keeps --dry-run's behaviour independent of whatever a
            # PRIOR real run already left in the store.
            return 0
    else:
        available, probe_errors = probe_picks_available(client, entry_ids, event_id)
        if probe_errors >= PROBE_SIZE:
            logger.error(
                "PROBE INCONCLUSIVE: every one of %d warm-up entries ERRORED (not a 404) for GW%d — "
                "likely the maintenance window resumed mid-startup, right after "
                "probe_picks_endpoint_ready passed. Aborting before spending the full request "
                "budget. Re-run later.",
                PROBE_SIZE,
                event_id,
            )
            return 2
        if not available:
            logger.error(
                "PROBE FAILED: all %d warm-up entries 404d on entry_picks for GW%d even though the "
                "deadline has passed. The picks/ endpoint may not be serving yet, or the response "
                "shape has changed. Aborting before spending the full request budget. Re-run later.",
                PROBE_SIZE,
                event_id,
            )
            return 2

        if dry_run:
            logger.info("--dry-run: probe succeeded, stopping before the full %d-entry sample.", n)
            return 0

        picks_chunk: list[dict] = []
        subs_chunk: list[dict] = []
        chunk_errors = 0
        chunk_len = 0

        for i, entry_id in enumerate(entry_ids, start=1):
            chunk_len += 1
            try:
                picks_response = client.entry_picks(entry_id, event_id, max_retries=MAIN_LOOP_MAX_RETRIES)
            except FPLApiError as exc:
                total_errors += 1
                chunk_errors += 1
                logger.warning(
                    "entry_id=%d GW%d errored (NOT persisted; will be re-requested on a resumed "
                    "run): %s",
                    entry_id,
                    event_id,
                    exc,
                )
            else:
                if picks_response is None:
                    total_misses += 1
                else:
                    total_hits += 1
                    picks_chunk.extend(_picks_to_rows(entry_id, event_id, picks_response))
                    subs_chunk.extend(_automatic_subs_to_rows(entry_id, event_id, picks_response))

            is_last = i == len(entry_ids)
            if i % chunk_size == 0 or is_last:
                chunk_now = datetime.now(timezone.utc)  # THIS chunk's own honest collection time
                if chunk_errors == chunk_len and chunk_len > 0:
                    logger.critical(
                        "CIRCUIT BREAKER: chunk ending at entry %d/%d was %d/%d ERRORS with zero "
                        "hits/misses — this may mean maintenance has resumed. Continuing (never "
                        "auto-aborting a run that might recover), but this needs attention.",
                        i,
                        len(entry_ids),
                        chunk_errors,
                        chunk_len,
                    )
                _persist_picks_chunk(store, picks_chunk, deadline, chunk_now)
                _persist_automatic_subs_chunk(store, subs_chunk, deadline, chunk_now)
                logger.info(
                    "progress: %d/%d (hits=%d misses=%d errors=%d, this chunk: %d entries, "
                    "%d errors) — persisted at %s",
                    i,
                    len(entry_ids),
                    total_hits,
                    total_misses,
                    total_errors,
                    chunk_len,
                    chunk_errors,
                    chunk_now.isoformat(),
                )
                picks_chunk = []
                subs_chunk = []
                chunk_errors = 0
                chunk_len = 0

    hit_rate = total_hits / len(entry_ids) if entry_ids else 0.0
    logger.info(
        "sample complete (this run): hits=%d misses=%d errors=%d hit_rate=%.4f",
        total_hits,
        total_misses,
        total_errors,
        hit_rate,
    )

    # -- calibration reads back the FULL persisted sample for this event,
    # not just this invocation's rows — correct in both the ordinary case
    # (one uninterrupted run) and the resumed case (where a prior crashed
    # run's chunks are on disk but not in THIS process's memory). --------
    full_picks_df = store.observations(PICKS_DATASET, until=datetime.now(timezone.utc))
    if not full_picks_df.is_empty():
        full_picks_df = full_picks_df.filter(pl.col("event") == event_id)

    if full_picks_df.is_empty():
        logger.error(
            "zero picks persisted for GW%d out of %d entries targeted — nothing to calibrate "
            "against.",
            event_id,
            len(full_entry_ids),
        )
        return 3

    sampled_ownership = (
        full_picks_df.group_by("element")
        .agg(pl.len().alias("n"))
        .with_columns((pl.col("n") / len(full_entry_ids) * 100.0).alias("sampled_pct"))
        .select(["element", "sampled_pct"])
    )
    calib = calibration_check(
        sampled_ownership,
        elements_df.select(["id", "selected_by_percent"]),
        threshold_pp=calibration_threshold_pp,
    )
    if calib.passed:
        logger.info(
            "CALIBRATION OK: sampled ownership matches selected_by_percent within %.1fpp "
            "(max deviation %.2fpp over %d compared players).",
            calib.threshold_pp,
            calib.max_deviation_pp,
            calib.n_compared,
        )
        return 0

    logger.error(
        "CALIBRATION FAILURE: sampled ownership deviates from selected_by_percent by up to "
        "%.2fpp (threshold %.1fpp). Per blueprint sec. 3.4 this is a HARD FAILURE, not a warning - "
        "the sample is likely biased. Worst offenders (element, bootstrap_pct, sampled_pct, deviation_pp): %s",
        calib.max_deviation_pp,
        calib.threshold_pp,
        calib.worst_offenders,
    )
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gw", type=int, default=None, help="gameweek to sample (default: most recently finished)")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help=f"sample size (default {DEFAULT_N})")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed (default: deterministic from GW number)")
    parser.add_argument("--store-path", type=Path, default=None)
    parser.add_argument("--calibration-threshold-pp", type=float, default=3.0)
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=CHUNK_SIZE,
        help=f"entries per incremental persistence chunk (default {CHUNK_SIZE}) — blueprint §3.4 Blocker 1",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="disable resumability: do NOT skip entry_ids already persisted for this GW from a "
        "prior (e.g. crashed) run. Off by default because resuming is the safe choice for the "
        "irreplaceable EO sample; pass this to force a genuinely fresh independent draw.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve target GW, binary-search max entry id, and probe availability — then stop "
        "without spending the full request budget or writing to the store",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = BitemporalStore(base_path=args.store_path) if args.store_path else BitemporalStore()
    client = FPLClient()

    try:
        return run(
            store,
            client,
            requested_gw=args.gw,
            n=args.n,
            seed=args.seed,
            dry_run=args.dry_run,
            calibration_threshold_pp=args.calibration_threshold_pp,
            resume=not args.no_resume,
            chunk_size=args.chunk_size,
        )
    except FPLApiError as exc:
        logger.error("sample_picks failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
