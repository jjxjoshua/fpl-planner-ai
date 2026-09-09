"""Tests for scripts/check_heartbeat.py (session s003, PROGRESS.md E2).

Same importlib-from-path pattern as tests/test_snapshot_bootstrap.py --
`scripts/` is not a package."""

from __future__ import annotations

import importlib.util
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from fplai.store import BitemporalStore

import sys

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_heartbeat.py"
_spec = importlib.util.spec_from_file_location("check_heartbeat", _SCRIPT_PATH)
check_heartbeat = importlib.util.module_from_spec(_spec)
# Registered in sys.modules BEFORE exec_module: the module defines a
# @dataclass, and dataclasses' own field-type resolution looks itself up via
# sys.modules[cls.__module__] -- without this line that lookup finds nothing
# and dataclass() raises AttributeError at class-definition time.
sys.modules["check_heartbeat"] = check_heartbeat
_spec.loader.exec_module(check_heartbeat)


def _heartbeat_row(*, job: str, run_ts: datetime, target_dataset: str = "elements", outcome: str = "written") -> dict:
    return {
        "job": job,
        "run_ts": run_ts.astimezone(timezone.utc).replace(tzinfo=None),
        "target_dataset": target_dataset,
        "outcome": outcome,
        "payload_hash": "h" if outcome != "failed" else None,
        "n_rows": 1 if outcome != "failed" else None,
        "error": None if outcome != "failed" else "boom",
    }


def _write_heartbeat_batch(store: BitemporalStore, *, job: str, run_ts: datetime, target_datasets: list[str], outcome: str = "written") -> None:
    df = pl.DataFrame([_heartbeat_row(job=job, run_ts=run_ts, target_dataset=d, outcome=outcome) for d in target_datasets])
    store.write("heartbeat", df, valid_at=run_ts, observed_at=run_ts, source="test", skip_if_unchanged=False)


# -- compute_gap_report (pure) -----------------------------------------------


def test_compute_gap_report_no_timestamps_is_unhealthy_with_no_gap():
    report = check_heartbeat.compute_gap_report(
        "snapshot_bootstrap", [], as_of=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), window=timedelta(hours=24)
    )
    assert report.run_count == 0
    assert report.largest_gap is None
    assert report.healthy(timedelta(minutes=45)) is False


def test_compute_gap_report_evenly_spaced_healthy():
    as_of = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    ts = [as_of - timedelta(minutes=30 * i) for i in range(10)][::-1]  # every 30 min, last one AT as_of
    report = check_heartbeat.compute_gap_report("snapshot_bootstrap", ts, as_of=as_of, window=timedelta(hours=24))
    assert report.run_count == 10
    assert report.largest_gap == timedelta(minutes=30)
    assert report.healthy(timedelta(minutes=45)) is True


def test_compute_gap_report_catches_a_genuine_gap_in_the_middle():
    as_of = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    ts = [
        as_of - timedelta(hours=4),
        as_of - timedelta(hours=3, minutes=30),
        as_of - timedelta(hours=1),  # a 2.5h gap right before this
        as_of - timedelta(minutes=30),
        as_of,
    ]
    report = check_heartbeat.compute_gap_report("snapshot_bootstrap", ts, as_of=as_of, window=timedelta(hours=24))
    assert report.largest_gap == timedelta(hours=2, minutes=30)
    assert report.largest_gap_start == as_of - timedelta(hours=3, minutes=30)
    assert report.largest_gap_end == as_of - timedelta(hours=1)
    assert report.healthy(timedelta(minutes=45)) is False


def test_compute_gap_report_trailing_gap_catches_a_stalled_scheduler():
    # The critical case: a job with a perfect history that simply stopped.
    # No gap ANYWHERE in its own timestamp history is large -- only the
    # implicit gap to "now" reveals it stopped.
    as_of = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    ts = [as_of - timedelta(hours=6) - timedelta(minutes=30 * i) for i in range(10)][::-1]
    # last real heartbeat is 6 hours before as_of; every prior gap is 30 min.
    report = check_heartbeat.compute_gap_report("snapshot_bootstrap", ts, as_of=as_of, window=timedelta(hours=24))
    assert report.largest_gap == timedelta(hours=6)
    assert report.largest_gap_end == as_of
    assert report.healthy(timedelta(minutes=45)) is False


def test_compute_gap_report_excludes_an_old_gap_entirely_outside_the_window():
    # A genuinely ancient 1-day gap, fully separated from the window by a
    # "buffer" point placed exactly 30 minutes before the window opens --
    # so the gap CROSSING into the window is itself small (30 min, same as
    # every other in-window gap), and the ancient gap's own end (30 min
    # before window_start) is unambiguously excluded. This isolates the
    # "old gap excluded" behaviour from the separate "a gap crossing the
    # window boundary is measured in full" behaviour (covered by the
    # trailing-gap test above), rather than conflating the two.
    as_of = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    window = timedelta(hours=6)
    ts = [
        as_of - timedelta(days=10),
        as_of - timedelta(days=9),  # a 1-day gap, entirely before window_start
        as_of - window - timedelta(minutes=30),  # buffer point, 30 min before window_start
        *[as_of - window + timedelta(minutes=30 * i) for i in range(13)],  # window_start -> as_of, every 30 min
    ]
    report = check_heartbeat.compute_gap_report("snapshot_bootstrap", ts, as_of=as_of, window=window)
    assert report.largest_gap == timedelta(minutes=30)


def test_compute_gap_report_healthy_boundary_is_inclusive():
    as_of = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    ts = [as_of - timedelta(minutes=45)]
    report = check_heartbeat.compute_gap_report("snapshot_bootstrap", ts, as_of=as_of, window=timedelta(hours=24))
    assert report.largest_gap == timedelta(minutes=45)
    assert report.healthy(timedelta(minutes=45)) is True
    assert report.healthy(timedelta(minutes=44)) is False


# -- load_run_timestamps against a real tempdir store ------------------------


def test_load_run_timestamps_dedupes_across_target_datasets_within_one_run():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        run_ts = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        _write_heartbeat_batch(store, job="snapshot_bootstrap", run_ts=run_ts, target_datasets=["elements", "teams", "events"])
        as_of = run_ts + timedelta(minutes=1)
        grouped = check_heartbeat.load_run_timestamps(store, job=None, as_of=as_of)
        assert grouped["snapshot_bootstrap"] == [run_ts]


def test_load_run_timestamps_groups_multiple_jobs_separately():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        t1 = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 22, 12, 5, tzinfo=timezone.utc)
        _write_heartbeat_batch(store, job="snapshot_bootstrap", run_ts=t1, target_datasets=["elements"])
        _write_heartbeat_batch(store, job="snapshot_odds", run_ts=t2, target_datasets=["odds_match_odds"])
        as_of = t2 + timedelta(minutes=1)

        both = check_heartbeat.load_run_timestamps(store, job=None, as_of=as_of)
        assert set(both.keys()) == {"snapshot_bootstrap", "snapshot_odds"}

        filtered = check_heartbeat.load_run_timestamps(store, job="snapshot_odds", as_of=as_of)
        assert set(filtered.keys()) == {"snapshot_odds"}
        assert filtered["snapshot_odds"] == [t2]


def test_load_run_timestamps_empty_store_returns_empty_dict():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        grouped = check_heartbeat.load_run_timestamps(store, job=None, as_of=datetime.now(timezone.utc))
        assert grouped == {}


# -- main() CLI ---------------------------------------------------------------


def test_main_exits_2_when_no_heartbeat_data_exists(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        code = check_heartbeat.main(["--store-path", tmp, "--max-gap-minutes", "45"])
        assert code == 2
        assert "NO HEARTBEAT DATA FOUND" in capsys.readouterr().out


def test_main_exits_0_for_a_healthy_evenly_spaced_history(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        as_of = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        for i in range(20):
            _write_heartbeat_batch(store, job="snapshot_bootstrap", run_ts=as_of - timedelta(minutes=30 * i), target_datasets=["elements"])
        code = check_heartbeat.main(
            ["--store-path", tmp, "--max-gap-minutes", "45", "--as-of", as_of.isoformat()]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "HEALTHY" in out
        assert "UNHEALTHY" not in out


def test_main_exits_1_for_a_stalled_scheduler(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        as_of = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        # Last heartbeat 6 hours ago -- well past a 45-minute threshold.
        for i in range(5):
            _write_heartbeat_batch(
                store, job="snapshot_bootstrap",
                run_ts=as_of - timedelta(hours=6) - timedelta(minutes=30 * i),
                target_datasets=["elements"],
            )
        code = check_heartbeat.main(
            ["--store-path", tmp, "--max-gap-minutes", "45", "--as-of", as_of.isoformat()]
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "UNHEALTHY" in out


def test_main_a_quiet_but_alive_scheduler_is_never_reported_as_an_outage(capsys):
    # THE demonstration this whole feature exists for: 12 hours of perfectly
    # regular 30-minute heartbeats, every single one reporting
    # outcome="skipped_unchanged" for a downstream dataset that hasn't
    # changed at all (the real ~11h `events` gap, reproduced structurally) --
    # must be reported HEALTHY, because check_heartbeat looks at run_ts, not
    # at outcome.
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        as_of = datetime(2026, 8, 22, 17, 32, tzinfo=timezone.utc)
        for i in range(24):  # every 30 min for 12 hours
            run_ts = as_of - timedelta(minutes=30 * i)
            df = pl.DataFrame([_heartbeat_row(job="snapshot_bootstrap", run_ts=run_ts, target_dataset="events", outcome="skipped_unchanged")])
            store.write("heartbeat", df, valid_at=run_ts, observed_at=run_ts, source="test", skip_if_unchanged=False)
        code = check_heartbeat.main(
            ["--store-path", tmp, "--max-gap-minutes", "45", "--as-of", as_of.isoformat()]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "HEALTHY" in out
        assert "UNHEALTHY" not in out


def test_main_job_filter_isolates_a_single_job(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        as_of = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        # snapshot_bootstrap: healthy (recent, regular)
        for i in range(5):
            _write_heartbeat_batch(store, job="snapshot_bootstrap", run_ts=as_of - timedelta(minutes=30 * i), target_datasets=["elements"])
        # snapshot_odds: stalled (last run 1 day ago)
        _write_heartbeat_batch(store, job="snapshot_odds", run_ts=as_of - timedelta(days=1), target_datasets=["odds_match_odds"])

        code = check_heartbeat.main(
            ["--store-path", tmp, "--job", "snapshot_bootstrap", "--max-gap-minutes", "45", "--as-of", as_of.isoformat()]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "snapshot_bootstrap" in out
        assert "snapshot_odds" not in out


def test_main_reports_all_jobs_and_fails_if_any_job_is_unhealthy(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        as_of = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        _write_heartbeat_batch(store, job="snapshot_bootstrap", run_ts=as_of, target_datasets=["elements"])
        _write_heartbeat_batch(store, job="snapshot_odds", run_ts=as_of - timedelta(days=1), target_datasets=["odds_match_odds"])

        code = check_heartbeat.main(["--store-path", tmp, "--max-gap-minutes", "45", "--as-of", as_of.isoformat()])
        out = capsys.readouterr().out
        assert code == 1  # snapshot_odds is unhealthy -> overall failure
        assert "snapshot_bootstrap" in out and "HEALTHY" in out
        assert "snapshot_odds" in out and "UNHEALTHY" in out
