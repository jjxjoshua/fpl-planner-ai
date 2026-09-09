"""Tests for scripts/snapshot_bootstrap.py's heartbeat feature (session
s003, PROGRESS.md E2 — "heartbeat row so 'nothing changed' != 'scheduler
was down'").

`scripts/` is not a package (no `__init__.py`, not on the normal import
path) and this is the FIRST test file to exercise anything under it, so the
module is loaded directly from its file path via `importlib` rather than
assuming a package layout that doesn't exist yet. The script's own
`sys.path.insert(0, .../src)` line runs unconditionally at module import
time (not gated behind `if __name__ == "__main__"`), so `fplai` imports
resolve correctly once loaded this way.

**No network in this file.** `FPLProvider`/`FPLClient` are mocked exactly
as `tests/test_provider_fpl.py` mocks them (`FPLClient` as a `MagicMock`
with `bootstrap_static.return_value` set) — the ownership-capture path
itself (`run()`) is exercised only as far as needed to prove the heartbeat
behaves correctly around it; `run()`'s own per-dataset write logic is
already implicitly covered by the live-verified runbook record.
"""

from __future__ import annotations

import importlib.util
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import polars as pl
import pytest

from fplai.client import FPLApiError
from fplai.providers.base import ProviderError
from fplai.schemas import CANONICAL_SCHEMAS, JOB_HEARTBEAT_RUN
from fplai.store import BitemporalStore, WriteResult

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_bootstrap.py"
_spec = importlib.util.spec_from_file_location("snapshot_bootstrap", _SCRIPT_PATH)
snapshot_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snapshot_bootstrap)


def _wr(*, written: bool, content_hash: str = "abc123", n_rows: int = 5) -> WriteResult:
    return WriteResult(
        written=written,
        dataset="elements",
        content_hash=content_hash,
        batch_id="batch1" if written else None,
        path=None,
        n_rows=n_rows,
    )


# -- build_heartbeat_rows (pure) --------------------------------------------


def test_build_heartbeat_rows_written_and_skipped_outcomes():
    run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    results = {
        "elements": _wr(written=True, content_hash="hash-e", n_rows=595),
        "teams": _wr(written=False, content_hash="hash-t", n_rows=20),
    }
    df = snapshot_bootstrap.build_heartbeat_rows(
        job="snapshot_bootstrap",
        run_ts=run_ts,
        dataset_names=["elements", "teams"],
        results=results,
        run_error=None,
    )
    rows = df.sort("target_dataset").to_dicts()
    assert rows[0]["target_dataset"] == "elements"
    assert rows[0]["outcome"] == "written"
    assert rows[0]["payload_hash"] == "hash-e"
    assert rows[0]["n_rows"] == 595
    assert rows[0]["error"] is None
    assert rows[1]["target_dataset"] == "teams"
    assert rows[1]["outcome"] == "skipped_unchanged"
    assert rows[1]["payload_hash"] == "hash-t"


def test_build_heartbeat_rows_run_ts_column_is_naive_utc():
    # FactTableSchema.validate rejects any tz-aware Datetime column —
    # the caller's tz-aware run_ts must be normalised before it reaches the
    # DataFrame.
    run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    df = snapshot_bootstrap.build_heartbeat_rows(
        job="snapshot_bootstrap",
        run_ts=run_ts,
        dataset_names=["elements"],
        results={"elements": _wr(written=True)},
        run_error=None,
    )
    dtype = df.schema["run_ts"]
    assert isinstance(dtype, pl.Datetime)
    assert dtype.time_zone is None
    assert df["run_ts"][0] == datetime(2026, 8, 22, 12, 0)


def test_build_heartbeat_rows_whole_run_failed_marks_every_dataset_failed():
    # run() itself raised (e.g. bootstrap-static/ failed outright) — no
    # per-dataset information exists, every dataset name in the batch is
    # marked failed with the run's own error.
    run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    df = snapshot_bootstrap.build_heartbeat_rows(
        job="snapshot_bootstrap",
        run_ts=run_ts,
        dataset_names=["elements", "teams", "events"],
        results={},
        run_error=FPLApiError("connection reset"),
    )
    assert set(df["outcome"]) == {"failed"}
    assert all("connection reset" in e for e in df["error"])
    assert all(h is None for h in df["payload_hash"])
    assert all(n is None for n in df["n_rows"])


def test_build_heartbeat_rows_one_dataset_missing_from_results_is_failed_others_unaffected():
    # run() returned normally (no run_error) but fetch_batch's own
    # per-capability resilience skipped ONE capability (missing payload key
    # / schema failure) — only that dataset is "failed"; the rest reflect
    # their real outcome.
    run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    df = snapshot_bootstrap.build_heartbeat_rows(
        job="snapshot_bootstrap",
        run_ts=run_ts,
        dataset_names=["elements", "teams"],
        results={"elements": _wr(written=True)},  # "teams" missing
        run_error=None,
    )
    rows = {r["target_dataset"]: r for r in df.to_dicts()}
    assert rows["elements"]["outcome"] == "written"
    assert rows["teams"]["outcome"] == "failed"
    assert rows["teams"]["error"] is not None


def test_build_heartbeat_rows_validates_against_the_canonical_schema():
    run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    df = snapshot_bootstrap.build_heartbeat_rows(
        job="snapshot_bootstrap",
        run_ts=run_ts,
        dataset_names=["elements", "teams", "events", "chips", "game_config", "game_settings"],
        results={},
        run_error=FPLApiError("boom"),
    )
    CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN].validate(df)  # must not raise


def test_build_heartbeat_rows_entity_key_unique_within_one_batch():
    # CLAUDE.md lesson 2 — attack the entity key's uniqueness directly
    # against this script's own real DATASET_CAPABILITIES list, not a
    # hand-picked smaller example.
    run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    dataset_names = [name for name, _ in snapshot_bootstrap.DATASET_CAPABILITIES]
    df = snapshot_bootstrap.build_heartbeat_rows(
        job=snapshot_bootstrap.HEARTBEAT_JOB,
        run_ts=run_ts,
        dataset_names=dataset_names,
        results={n: _wr(written=True) for n in dataset_names},
        run_error=None,
    )
    keys = list(zip(df["job"], df["run_ts"], df["target_dataset"]))
    assert len(keys) == len(dataset_names)
    assert len(keys) == len(set(keys))


# -- write_heartbeat / write_heartbeat_safely -------------------------------


def test_write_heartbeat_always_passes_skip_if_unchanged_false():
    # The literal regression guard the module docstring promises: a future
    # accidental omission or flip of skip_if_unchanged would make two
    # quiet-but-distinct runs collapse into one heartbeat row, silently
    # defeating the entire feature.
    store = MagicMock(spec=BitemporalStore)
    run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    df = snapshot_bootstrap.build_heartbeat_rows(
        job="snapshot_bootstrap",
        run_ts=run_ts,
        dataset_names=["elements"],
        results={"elements": _wr(written=True)},
        run_error=None,
    )
    snapshot_bootstrap.write_heartbeat(store, df, run_ts=run_ts)
    assert store.write.call_args.args[0] == "heartbeat"
    assert store.write.call_args.kwargs["skip_if_unchanged"] is False


def test_write_heartbeat_against_a_real_tempdir_store_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        df = snapshot_bootstrap.build_heartbeat_rows(
            job="snapshot_bootstrap",
            run_ts=run_ts,
            dataset_names=["elements", "teams"],
            results={"elements": _wr(written=True), "teams": _wr(written=False)},
            run_error=None,
        )
        result = snapshot_bootstrap.write_heartbeat(store, df, run_ts=run_ts)
        assert result.written is True

        read_back = store.observations("heartbeat", until=run_ts + timedelta(minutes=1))
        assert read_back.height == 2
        assert set(read_back["target_dataset"]) == {"elements", "teams"}


def test_write_heartbeat_two_quiet_runs_both_persist_distinct_rows():
    # The TRUE collision case skip_if_unchanged=False protects against:
    # since run_ts is baked into the row itself (not just store metadata),
    # two ordinary quiet runs at DIFFERENT wall-clock times never actually
    # hash-collide regardless of this kwarg -- their run_ts differs, so their
    # content_hash differs too. The scenario that DOES collide is two writes
    # sharing the exact same run_ts (a retry of the same run, a backfill/
    # replay script re-deriving an already-used timestamp, or a future
    # caller that batches multiple write_heartbeat calls off one captured
    # `now`) -- constructed here directly rather than relying on wall-clock
    # timestamps to differ by luck.
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        same_result = {"elements": _wr(written=False, content_hash="same-hash")}

        df = snapshot_bootstrap.build_heartbeat_rows(
            job="snapshot_bootstrap", run_ts=run_ts, dataset_names=["elements"],
            results=same_result, run_error=None,
        )
        # Two writes of the genuinely IDENTICAL batch (same run_ts, same
        # content) -- simulates a retry/replay, not two different runs.
        r1 = snapshot_bootstrap.write_heartbeat(store, df, run_ts=run_ts)
        r2 = snapshot_bootstrap.write_heartbeat(store, df, run_ts=run_ts)
        assert r1.written is True
        assert r2.written is True  # NOT skipped, despite byte-identical content and run_ts

        stream = store.observations("heartbeat", until=run_ts + timedelta(minutes=1))
        assert stream.height == 2  # both persisted -- skip_if_unchanged never suppressed the second


def test_write_heartbeat_safely_swallows_a_store_write_failure():
    store = MagicMock(spec=BitemporalStore)
    store.write.side_effect = RuntimeError("disk full")
    run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    # Must not raise -- this is the core guarantee.
    snapshot_bootstrap.write_heartbeat_safely(
        store,
        job="snapshot_bootstrap",
        run_ts=run_ts,
        dataset_names=["elements"],
        results={"elements": _wr(written=True)},
        run_error=None,
    )


def test_write_heartbeat_safely_swallows_a_build_failure_from_a_bad_dataset_name_list():
    # Attack from outside the sanctioned path (CLAUDE.md's "no path is
    # unsafe" standard): pass a dataset_names entry that is not a string,
    # forcing build_heartbeat_rows/validate to blow up in an unanticipated
    # way, and confirm the safe wrapper still does not raise.
    store = MagicMock(spec=BitemporalStore)
    run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    snapshot_bootstrap.write_heartbeat_safely(
        store,
        job="snapshot_bootstrap",
        run_ts=run_ts,
        dataset_names=[object()],  # deliberately malformed
        results={},
        run_error=None,
    )
    store.write.assert_not_called()  # never got far enough to write


# -- main() integration: heartbeat write cannot be blocked by a run() failure --


def _patch_provider(monkeypatch, *, fetch_batch_side_effect=None, fetch_batch_return=None):
    provider_instance = MagicMock()
    if fetch_batch_side_effect is not None:
        provider_instance.fetch_batch.side_effect = fetch_batch_side_effect
    else:
        provider_instance.fetch_batch.return_value = fetch_batch_return or {}
    monkeypatch.setattr(snapshot_bootstrap, "FPLProvider", MagicMock(return_value=provider_instance))
    monkeypatch.setattr(snapshot_bootstrap, "FPLClient", MagicMock())
    return provider_instance


def test_main_writes_heartbeat_even_when_run_raises_and_returns_exit_1(monkeypatch):
    _patch_provider(monkeypatch, fetch_batch_side_effect=ProviderError("bootstrap-static/ unreachable"))
    with tempfile.TemporaryDirectory() as tmp:
        exit_code = snapshot_bootstrap.main(["--store-path", tmp])
        assert exit_code == 1

        store = BitemporalStore(base_path=Path(tmp))
        heartbeat = store.observations("heartbeat", until=datetime.now(timezone.utc) + timedelta(minutes=1))
        assert heartbeat.height == len(snapshot_bootstrap.DATASET_CAPABILITIES)
        assert set(heartbeat["outcome"]) == {"failed"}
        assert all("unreachable" in e for e in heartbeat["error"])


def test_main_writes_heartbeat_on_a_successful_run_and_returns_exit_0(monkeypatch):
    from fplai.providers.base import FetchResult

    def _fetch_batch(capabilities, force_refresh=True):
        out = {}
        for cap in capabilities:
            rows = pl.DataFrame({"id": [1], "web_name": ["X"], "team": [1], "element_type": [1], "now_cost": [50], "selected_by_percent": ["1.0"], "status": ["a"]}) \
                if cap.entity == "player" else \
                pl.DataFrame({"id": [1], "name": ["A"], "short_name": ["A"]}) if cap.entity == "team" else \
                pl.DataFrame({"id": [1], "deadline_time": ["2026-08-21T17:30:00Z"]}) if cap.entity == "gameweek" else \
                pl.DataFrame({"id": [1], "name": ["wildcard"]}) if cap.entity == "chip" else \
                pl.DataFrame({"payload": ["{}"]})
            out[cap] = FetchResult(
                capability=cap, rows=rows, provider_id="fpl_api", endpoint="bootstrap-static/",
                observed_at=datetime.now(timezone.utc), content_hash="h",
            )
        return out

    _patch_provider(monkeypatch, fetch_batch_side_effect=_fetch_batch)
    with tempfile.TemporaryDirectory() as tmp:
        exit_code = snapshot_bootstrap.main(["--store-path", tmp])
        assert exit_code == 0

        store = BitemporalStore(base_path=Path(tmp))
        heartbeat = store.observations("heartbeat", until=datetime.now(timezone.utc) + timedelta(minutes=1))
        assert heartbeat.height == len(snapshot_bootstrap.DATASET_CAPABILITIES)
        assert set(heartbeat["outcome"]) == {"written"}
