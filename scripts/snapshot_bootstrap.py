#!/usr/bin/env python
"""Snapshot `bootstrap-static/` into the bitemporal store.

This is the urgent script — ownership and price state not captured before the
GW1 deadline (Fri 21 Aug 2026 17:30 UTC) is gone forever (blueprint §3.4).

Persists, as separate append-only datasets:
  - elements      one row per player, every field bootstrap-static returns
                   (id, opta_code, now_cost, selected_by_percent, status,
                   chance_of_playing_this_round/_next_round, news, news_added,
                   transfers_in_event/out_event, ep_this/ep_next, form,
                   penalties_order, direct_freekicks_order,
                   corners_and_indirect_freekicks_order, and everything else —
                   see the module docstring in store.py for why "flatten and
                   keep everything" beats hand-curating a field list)
  - teams         one row per club
  - events        one row per gameweek (chip_plays / overrides flattened to JSON)
  - chips         one row per chip x half (2026/27 rules — blueprint §11)
  - game_config   single-row snapshot of the whole game_config object
  - game_settings single-row snapshot of the whole game_settings object

Usage:
    python scripts/snapshot_bootstrap.py [--store-path PATH] [-v]

Safe to run every 30-60 minutes:
  - Always fetches live (`force_refresh=True` bypasses the client's URL
    cache) — a schedule that silently re-served a cached response would be
    useless.
  - Idempotent: the store skips writing a dataset whose content is byte-for-
    byte identical to its most recent batch (BitemporalStore.write's
    skip_if_unchanged), so a quiet 30 minutes does not bloat the store. Any
    genuine change (a price tick, a news string, a status flip) always gets
    a new row — nothing is ever overwritten.

Fetching and normalisation now go through the provider framework
(`fplai.providers.fpl.FPLProvider`, E2b stories 1-5) instead of doing the
JSON-flattening inline: `FPLProvider.fetch_batch()` makes exactly the same
ONE `bootstrap-static/` call this script always made (see that method's
docstring for why a naive per-capability port would have made six), and
each dataset's `WriteResult` now additionally carries `provider_id` /
`capability` / `endpoint` provenance (blueprint §12.3). Row shape, dataset
names, idempotency semantics and CLI are all unchanged — see
docs/wiki/provider-framework.md for the before/after.

**Heartbeat (session s003, PROGRESS.md E2).** Every run also writes one row
per dataset above to the `heartbeat` dataset (`fplai.schemas.
JOB_HEARTBEAT_RUN`), unconditionally (`skip_if_unchanged=False` — see
`write_heartbeat`'s own docstring for why that must never change) and
regardless of whether the run itself succeeded, partially succeeded, or
raised. This is what makes "no new `elements` row" distinguishable from
"the scheduler never ran": `store.write(..., skip_if_unchanged=True)` (the
correct behaviour for every *real* dataset here) makes those two cases
identical from the data alone otherwise — demonstrated live 2026-08-22, see
docs/wiki/runbook-ingest.md §6. Heartbeat writing is wrapped so it can
NEVER fail the run it is monitoring — see `write_heartbeat_safely`'s
docstring. Read the heartbeat stream with `scripts/check_heartbeat.py`.
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
from fplai.providers.base import ProviderError  # noqa: E402
from fplai.providers.fpl import (  # noqa: E402
    CHIP_WINDOW_SEASON,
    GAME_CONFIG_CURRENT,
    GAME_SETTINGS_CURRENT,
    GAMEWEEK_ATTRIBUTES_SEASON,
    PLAYER_ATTRIBUTES_CURRENT,
    TEAM_ATTRIBUTES_CURRENT,
    FPLProvider,
)
from fplai.schemas import CANONICAL_SCHEMAS, JOB_HEARTBEAT_RUN  # noqa: E402
from fplai.store import BitemporalStore, WriteResult  # noqa: E402

logger = logging.getLogger("fplai.snapshot_bootstrap")

SOURCE = "fpl_api:bootstrap-static"

# (dataset name, capability) — same 6 datasets this script has always written.
DATASET_CAPABILITIES = [
    ("elements", PLAYER_ATTRIBUTES_CURRENT),
    ("teams", TEAM_ATTRIBUTES_CURRENT),
    ("events", GAMEWEEK_ATTRIBUTES_SEASON),
    ("chips", CHIP_WINDOW_SEASON),
    ("game_config", GAME_CONFIG_CURRENT),
    ("game_settings", GAME_SETTINGS_CURRENT),
]

# `job` column value for this script's own heartbeat rows (fplai.schemas.
# JOB_HEARTBEAT_RUN's module comment) — the identifier a future
# snapshot_odds.py / sample_picks.py adoption of the same dataset would use
# a different value for, so runs from different jobs never collide in the
# entity key (job, run_ts, target_dataset).
HEARTBEAT_JOB = "snapshot_bootstrap"


def run(store: BitemporalStore, provider: FPLProvider) -> dict[str, WriteResult]:
    now = datetime.now(timezone.utc)
    capabilities = [capability for _, capability in DATASET_CAPABILITIES]
    # ONE upstream bootstrap-static/ call for all 6 datasets — see
    # FPLProvider.fetch_batch's docstring for why this matters.
    fetched = provider.fetch_batch(capabilities, force_refresh=True)

    results: dict[str, WriteResult] = {}
    for dataset_name, capability in DATASET_CAPABILITIES:
        result = fetched.get(capability)
        if result is None:
            # fetch_batch already logged why (missing payload key, or a
            # schema-validation failure) — nothing further to do here.
            continue
        results[dataset_name] = store.write(
            dataset_name,
            result.rows,
            valid_at=now,
            observed_at=now,
            source=SOURCE,
            provider_id=result.provider_id,
            capability=str(result.capability),
            endpoint=result.endpoint,
        )

    return results


def build_heartbeat_rows(
    *,
    job: str,
    run_ts: datetime,
    dataset_names: list[str],
    results: dict[str, WriteResult],
    run_error: Exception | None,
) -> pl.DataFrame:
    """One row per entry in `dataset_names`, describing what this run saw
    for it — the payload `write_heartbeat` persists. Pure (no I/O), so it's
    tested directly without a store.

    Per dataset name, in priority order:
      1. `name in results` (the normal case) — `outcome` is "written" or
         "skipped_unchanged" from `WriteResult.written`, `payload_hash`/
         `n_rows` copied straight off it. `WriteResult.content_hash` is
         computed and present even when the write was skipped (the store
         hashes before deciding whether to skip) — so "skipped_unchanged"
         rows still carry a real hash to compare across runs.
      2. `run_error is not None` (the whole `run()` call raised, e.g.
         `bootstrap-static/` itself failed) — every dataset name gets
         `outcome="failed"` with `run_error`'s own message; we never
         reached `fetch_batch` far enough to know anything dataset-specific.
      3. Neither — `run()` returned normally but this particular capability
         is missing from its result (fetch_batch's own per-capability
         resilience: a missing payload key or a schema-validation failure
         SKIPS that capability with a logged warning rather than raising).
         Recorded as `outcome="failed"` here too, since from the operator's
         perspective a dataset that silently didn't get captured this run
         is the same class of fact as one that errored outright.

    `run_ts` is stored as a NAIVE UTC column (`FactTableSchema.validate`'s
    tz-aware-column check — every DATA column in this store is naive UTC by
    convention, only the store's own `valid_at`/`observed_at` metadata is
    tz-aware) — the caller's tz-aware `run_ts` is normalised here, once.
    """
    run_ts_naive = run_ts.astimezone(timezone.utc).replace(tzinfo=None)
    rows = []
    for name in dataset_names:
        result = results.get(name)
        if result is not None:
            outcome = "written" if result.written else "skipped_unchanged"
            payload_hash: str | None = result.content_hash
            n_rows: int | None = result.n_rows
            error: str | None = None
        elif run_error is not None:
            outcome = "failed"
            payload_hash = None
            n_rows = None
            error = f"{type(run_error).__name__}: {run_error}"
        else:
            outcome = "failed"
            payload_hash = None
            n_rows = None
            error = (
                "dataset missing from fetch_batch result — a missing payload "
                "key or schema-validation failure for this capability alone "
                "(see the run's own log for which); the other datasets in "
                "this batch were unaffected"
            )
        rows.append(
            {
                "job": job,
                "run_ts": run_ts_naive,
                "target_dataset": name,
                "outcome": outcome,
                "payload_hash": payload_hash,
                "n_rows": n_rows,
                "error": error,
            }
        )
    return pl.DataFrame(rows)


def write_heartbeat(store: BitemporalStore, df: pl.DataFrame, *, run_ts: datetime) -> WriteResult:
    """The one write call for the `heartbeat` dataset.

    `skip_if_unchanged=False` is a LITERAL kwarg, not a variable or a
    default fallen into — the entire feature exists to distinguish "nothing
    changed" from "the scheduler never ran", so a heartbeat batch that gets
    silently skipped because it happens to match a previous batch's content
    defeats the purpose outright. In ordinary operation two DIFFERENT runs
    almost never collide anyway (`run_ts` is baked into the row's own DATA,
    not just store metadata, so an ordinary 30-minutes-apart quiet run
    already has a different content hash purely from its own timestamp) —
    but this function does not rely on that being true. The real case this
    guards is a RETRY or REPLAY that reuses an already-used `run_ts` (a
    caller that captures `now` once and calls this more than once for it,
    a backfill/replay script, or simply two invocations landing in the same
    timestamp-resolution bucket): with `skip_if_unchanged=True` that second,
    genuinely-identical write would be silently dropped, and the operator
    would see one fewer heartbeat than runs actually happened. Guarded by
    `tests/test_snapshot_bootstrap.py::test_write_heartbeat_always_passes_
    skip_if_unchanged_false` (asserts the literal kwarg) and `::test_write_
    heartbeat_two_quiet_runs_both_persist_distinct_rows` (end-to-end against
    a real store, two byte-identical writes of the same batch, both must
    persist) — either would catch a future accidental `skip_if_unchanged=
    True` or an omitted kwarg reverting to `write()`'s own default of `True`.

    Validates against `CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN]` before writing
    — `BitemporalStore.write()` itself does not check `required_fields`
    presence (that is the caller's job, normally done inside a provider's
    `fetch()`; this script builds its own payload directly, so it is the
    caller here) — a malformed heartbeat row should fail loudly at this
    call, not persist a silently incomplete record of what the job saw.
    """
    CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN].validate(df)
    return store.write(
        "heartbeat",
        df,
        valid_at=run_ts,
        observed_at=run_ts,
        source="fplai.snapshot_bootstrap:heartbeat",
        skip_if_unchanged=False,
    )


def write_heartbeat_safely(
    store: BitemporalStore,
    *,
    job: str,
    run_ts: datetime,
    dataset_names: list[str],
    results: dict[str, WriteResult],
    run_error: Exception | None,
) -> None:
    """Build and write the heartbeat batch, catching and logging (never
    raising) ANY exception along the way.

    This is the one guarantee the brief names explicitly: a monitoring
    feature must never take down the thing it monitors. Whatever goes wrong
    here — a schema mismatch introduced by a future edit, a disk-full
    error, a bug in `build_heartbeat_rows` itself — the ownership capture
    this script exists to run must already have happened (or definitively
    failed) by the time this is called; nothing in here may prevent
    `main()` from returning the correct exit code for THAT outcome.
    Deliberately broad `except Exception` for exactly this reason — a
    narrower catch could itself let an unanticipated heartbeat-path failure
    propagate and take the run down with it, exactly the failure mode this
    function exists to rule out.
    """
    try:
        df = build_heartbeat_rows(
            job=job, run_ts=run_ts, dataset_names=dataset_names, results=results, run_error=run_error
        )
        write_heartbeat(store, df, run_ts=run_ts)
    except Exception as exc:  # noqa: BLE001 — see docstring: must never propagate
        logger.error("heartbeat write failed (ownership capture unaffected by this): %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--store-path",
        type=Path,
        default=None,
        help="override the bitemporal store base path (default: <repo>/data/store)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = BitemporalStore(base_path=args.store_path) if args.store_path else BitemporalStore()
    provider = FPLProvider(client=FPLClient())

    # `run_ts` is captured once, before the fetch, and reused for both the
    # ownership-capture writes' own timestamps (inside `run()`) and the
    # heartbeat's `run_ts` column below — one run, one timestamp, even if
    # `run()` itself raises partway through.
    run_ts = datetime.now(timezone.utc)
    results: dict[str, WriteResult] = {}
    run_error: FPLApiError | ProviderError | None = None
    try:
        results = run(store, provider)
    except (FPLApiError, ProviderError) as exc:
        logger.error("bootstrap snapshot failed: %s", exc)
        run_error = exc

    # ALWAYS attempted — including when `run()` raised above — and never
    # allowed to affect this function's return value (write_heartbeat_
    # safely never raises; see its own docstring). This is what makes
    # "the scheduler ran but the fetch failed" distinguishable from
    # "the scheduler never ran at all" too, not just the ordinary
    # nothing-changed case.
    dataset_names = [name for name, _ in DATASET_CAPABILITIES]
    write_heartbeat_safely(
        store,
        job=HEARTBEAT_JOB,
        run_ts=run_ts,
        dataset_names=dataset_names,
        results=results,
        run_error=run_error,
    )

    if run_error is not None:
        return 1

    if not results:
        logger.error("no datasets were written — bootstrap-static payload looked empty/unexpected")
        return 1

    for name, result in results.items():
        status = "WROTE" if result.written else "unchanged, skipped"
        logger.info(
            "%-14s %-22s rows=%-5d hash=%s", name, status, result.n_rows, result.content_hash[:12]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
