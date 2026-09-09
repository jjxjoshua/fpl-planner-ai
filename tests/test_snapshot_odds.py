"""Tests for scripts/snapshot_odds.py's deadline-relative capture scheduling
and heartbeat (session s005).

Same loading convention as tests/test_snapshot_bootstrap.py: `scripts/` is
not a package, so the module is loaded directly from its file path via
`importlib`. Unlike snapshot_bootstrap.py, this module defines its own
dataclasses (`CaptureWindow`, `CaptureDecision`) at module scope — under
Python 3.11, `@dataclass` inspects `sys.modules[cls.__module__]` while
building the class, which is `None` unless the freshly-loaded module is
registered in `sys.modules` *before* `exec_module` runs (verified live: the
import raises `AttributeError: 'NoneType' object has no attribute
'__dict__'` without this line — a real gap in test_snapshot_bootstrap.py's
own pattern that never surfaced there only because that script has no
module-level dataclass of its own).

**No network in this file.** Every test either exercises the pure window
functions directly, drives a real `BitemporalStore` against a `tempfile`
directory (no network — this is the store's own on-disk contract, not an
API), or monkeypatches `FPLProvider`/`FPLClient`/`build_transport`/
`OddsProvider`/`run` so `main()`'s integration path never reaches a socket.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import polars as pl
import pytest

from fplai.providers.base import ProviderError
from fplai.schemas import CANONICAL_SCHEMAS, JOB_HEARTBEAT_RUN
from fplai.store import BitemporalStore, WriteResult

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_odds.py"
_spec = importlib.util.spec_from_file_location("snapshot_odds", _SCRIPT_PATH)
snapshot_odds = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = snapshot_odds  # see module docstring: required for the module-level @dataclass
_spec.loader.exec_module(snapshot_odds)


# -- helpers ------------------------------------------------------------


def _wr(*, written: bool, content_hash: str = "abc123", n_rows: int = 5) -> WriteResult:
    return WriteResult(
        written=written, dataset="odds_match_odds", content_hash=content_hash,
        batch_id="batch1" if written else None, path=None, n_rows=n_rows,
    )


def _write_events(store: BitemporalStore, *, event_id: int, deadline_iso: str, as_of: datetime) -> None:
    df = pl.DataFrame({"id": [event_id], "deadline_time": [deadline_iso]})
    store.write("events", df, valid_at=as_of, observed_at=as_of, source="test")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary(*, match_written: bool, goal_writes: dict | None = None, team_failed: dict | None = None, other_failed: dict | None = None) -> dict:
    return {
        "match_odds_write": _wr(written=match_written),
        "goal_odds_writes": goal_writes or {},
        "goal_odds_team_identity_failed": team_failed or {},
        "goal_odds_failed": other_failed or {},
        "goal_odds_unresolved_names": set(),
        "goal_odds_n_unresolved_rows": 0,
        "goal_odds_n_resolved_rows": 0,
    }


# -- next_deadline / capture_windows_for_deadline / find_open_window (pure) --


def test_next_deadline_picks_earliest_strictly_future_deadline():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    events_df = pl.DataFrame(
        {
            "id": [1, 2, 3],
            "deadline_time": [
                _iso(now - timedelta(days=10)),  # already passed
                _iso(now + timedelta(hours=40)),  # nearest future
                _iso(now + timedelta(days=8)),
            ],
        }
    )
    deadline = snapshot_odds.next_deadline(events_df, now=now)
    assert deadline == now + timedelta(hours=40)


def test_next_deadline_none_when_every_deadline_has_passed():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    events_df = pl.DataFrame({"id": [1], "deadline_time": [_iso(now - timedelta(days=1))]})
    assert snapshot_odds.next_deadline(events_df, now=now) is None


def test_next_deadline_none_on_empty_events():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert snapshot_odds.next_deadline(pl.DataFrame(), now=now) is None


def test_next_deadline_skips_unparseable_rows_without_raising():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    events_df = pl.DataFrame({"id": [1, 2], "deadline_time": ["not-a-date", _iso(now + timedelta(hours=5))]})
    assert snapshot_odds.next_deadline(events_df, now=now) == now + timedelta(hours=5)


def test_capture_windows_for_deadline_four_offsets_correct_centres():
    deadline = datetime(2026, 9, 4, 17, 30, tzinfo=timezone.utc)
    windows = snapshot_odds.capture_windows_for_deadline(deadline)
    assert [w.label for w in windows] == ["T-78h", "T-26h", "T-6h", "T-2h"]
    half = snapshot_odds.WINDOW_WIDTH / 2
    for w, hours in zip(windows, snapshot_odds.CAPTURE_OFFSETS_HOURS):
        target = deadline - timedelta(hours=hours)
        assert w.window_start == target - half
        assert w.window_end == target + half
        assert w.deadline == deadline


def test_find_open_window_matches_inside_boundary_and_none_outside():
    deadline = datetime(2026, 9, 4, 17, 30, tzinfo=timezone.utc)
    windows = snapshot_odds.capture_windows_for_deadline(deadline)
    t2h = next(w for w in windows if w.label == "T-2h")
    assert snapshot_odds.find_open_window(windows, now=t2h.window_start) is t2h  # inclusive start
    assert snapshot_odds.find_open_window(windows, now=t2h.window_end - timedelta(seconds=1)) is t2h
    assert snapshot_odds.find_open_window(windows, now=t2h.window_end) is None  # exclusive end
    assert snapshot_odds.find_open_window(windows, now=t2h.window_start - timedelta(hours=1)) is None


def test_windows_for_all_offsets_never_overlap():
    # Smallest gap between adjacent offsets in CAPTURE_OFFSETS_HOURS is
    # 6-2=4h vs a 1h-wide window — attacked directly rather than assumed
    # (also true for the pre-correction 72/24/6/2 offsets, but stated
    # generically here so it stays correct if the offsets change again).
    deadline = datetime(2026, 9, 4, 17, 30, tzinfo=timezone.utc)
    windows = snapshot_odds.capture_windows_for_deadline(deadline)
    for a, b in zip(windows, windows[1:]):
        assert a.window_end <= b.window_start


def test_capture_windows_land_in_waking_hours_for_a_1730_utc_deadline():
    """Coordinator correction, session s005: an offset that is a whole
    multiple of 24h inherits the deadline's own LOCAL time-of-day, so
    T-72h/T-24h from FPL's standard 01:30 local deadline landed at 01:00
    local — inside the documented overnight machine-off window (27 Aug
    heartbeat hole, 00:45->18:45 local). This test encodes the constraint
    directly so a future change to CAPTURE_OFFSETS_HOURS cannot silently
    reintroduce an overnight-only window: every one of the 4 windows for a
    17:30-UTC-deadline gameweek (FPL's standard slot) must have its target
    instant (window centre) OUTSIDE 00:00-08:00 Asia/Kuala_Lumpur.

    The deadline is derived from a fabricated store, never a literal date
    (CLAUDE.md lesson 8) — `next_deadline` resolves it from a `now` that is
    itself computed relative to a fixed reference instant, not read off the
    real clock or a real gameweek."""
    from zoneinfo import ZoneInfo

    local_tz = ZoneInfo("Asia/Kuala_Lumpur")
    now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(days=3, hours=17, minutes=30)  # a 17:30 UTC deadline, comfortably in the future
    assert deadline.hour == 17 and deadline.minute == 30  # sanity: this IS the standard FPL slot this test targets

    windows = snapshot_odds.capture_windows_for_deadline(deadline)
    for w in windows:
        centre = w.window_start + (w.window_end - w.window_start) / 2  # the target instant this window is centred on
        local_hour = centre.astimezone(local_tz).hour
        assert not (0 <= local_hour < 8), (
            f"window {w.label} centres at {centre.astimezone(local_tz)} local — inside the "
            "overnight machine-off window this offset set exists to avoid"
        )


# -- decide_capture against a real temp store ----------------------------


def test_decide_capture_no_events_dataset_at_all():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        decision = snapshot_odds.decide_capture(store, now=datetime.now(timezone.utc))
        assert decision.window is None
        assert decision.outcome == "skipped_no_deadline"


def test_decide_capture_window_open_no_prior_heartbeat_returns_window():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=2)  # sits exactly on the T-2h target
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)
        decision = snapshot_odds.decide_capture(store, now=now)
        assert decision.window is not None
        assert decision.window.label == "T-2h"


def test_decide_capture_outside_every_window():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=40)  # nowhere near any of the 4 offsets
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)
        decision = snapshot_odds.decide_capture(store, now=now)
        assert decision.window is None
        assert decision.outcome == "skipped_outside_window"


def test_decide_capture_already_captured_this_window_skips():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=2)
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)

        # Simulate a PRIOR successful capture inside this same window.
        df = snapshot_odds.build_heartbeat_rows(
            job=snapshot_odds.HEARTBEAT_JOB, run_ts=now - timedelta(minutes=5),
            skip_outcome=None, skip_reason=None, summary=_summary(match_written=True), run_error=None,
        )
        snapshot_odds.write_heartbeat(store, df, run_ts=now - timedelta(minutes=5))

        decision = snapshot_odds.decide_capture(store, now=now)
        assert decision.window is None
        assert decision.outcome == "skipped_already_captured"


def test_decide_capture_retries_after_a_failed_attempt_within_the_window():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=2)
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)

        # One prior FAILED attempt inside the window -- must NOT suppress a retry.
        df = snapshot_odds.build_heartbeat_rows(
            job=snapshot_odds.HEARTBEAT_JOB, run_ts=now - timedelta(minutes=5),
            skip_outcome=None, skip_reason=None, summary=None, run_error=RuntimeError("network blip"),
        )
        snapshot_odds.write_heartbeat(store, df, run_ts=now - timedelta(minutes=5))

        decision = snapshot_odds.decide_capture(store, now=now)
        assert decision.window is not None
        assert decision.window.label == "T-2h"


def test_decide_capture_max_attempts_exhausted_stops_retrying():
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        deadline = now + timedelta(hours=2)
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)

        for i in range(snapshot_odds.MAX_ATTEMPTS_PER_WINDOW):
            ts = now - timedelta(minutes=5 * (i + 1))
            df = snapshot_odds.build_heartbeat_rows(
                job=snapshot_odds.HEARTBEAT_JOB, run_ts=ts,
                skip_outcome=None, skip_reason=None, summary=None, run_error=RuntimeError("boom"),
            )
            snapshot_odds.write_heartbeat(store, df, run_ts=ts)

        decision = snapshot_odds.decide_capture(store, now=now)
        assert decision.window is None
        assert decision.outcome == "skipped_max_attempts_exhausted"


def test_decide_capture_fires_exactly_once_per_window_over_a_full_gameweek_cycle():
    """Break-first proof of the at-most-once guarantee: simulate hourly
    firings across an entire gameweek cycle (well before T-78h to well after
    the deadline), and on every firing where a window is open, persist a
    SUCCESSFUL capture heartbeat exactly as main() would. Assert each of the
    4 windows is entered exactly once across the whole simulated timeline."""
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        sim_start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        deadline = sim_start + timedelta(hours=80)  # T-78h falls 2h after sim_start
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=sim_start)

        captured_labels: list[str] = []
        # Hourly firings from sim_start to well past the deadline.
        for hour in range(0, 90):
            now = sim_start + timedelta(hours=hour)
            decision = snapshot_odds.decide_capture(store, now=now)
            if decision.window is not None:
                captured_labels.append(decision.window.label)
                df = snapshot_odds.build_heartbeat_rows(
                    job=snapshot_odds.HEARTBEAT_JOB, run_ts=now,
                    skip_outcome=None, skip_reason=None,
                    summary=_summary(match_written=True), run_error=None,
                )
                snapshot_odds.write_heartbeat(store, df, run_ts=now)
            else:
                df = snapshot_odds.build_heartbeat_rows(
                    job=snapshot_odds.HEARTBEAT_JOB, run_ts=now,
                    skip_outcome=decision.outcome, skip_reason=decision.reason,
                    summary=None, run_error=None,
                )
                snapshot_odds.write_heartbeat(store, df, run_ts=now)

        assert captured_labels == ["T-78h", "T-26h", "T-6h", "T-2h"]


# -- build_heartbeat_rows (pure) -----------------------------------------


def test_build_heartbeat_rows_skip_branch():
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    df = snapshot_odds.build_heartbeat_rows(
        job="snapshot_odds", run_ts=run_ts,
        skip_outcome="skipped_outside_window", skip_reason="not yet",
        summary=None, run_error=None,
    )
    rows = {r["target_dataset"]: r for r in df.to_dicts()}
    assert set(rows) == set(snapshot_odds.HEARTBEAT_TARGETS)
    for r in rows.values():
        assert r["outcome"] == "skipped_outside_window"
        assert r["error"] == "not yet"
        assert r["payload_hash"] is None
        assert r["n_rows"] is None


def test_build_heartbeat_rows_run_error_branch():
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    df = snapshot_odds.build_heartbeat_rows(
        job="snapshot_odds", run_ts=run_ts,
        skip_outcome=None, skip_reason=None,
        summary=None, run_error=ProviderError("boom"),
    )
    assert set(df["outcome"]) == {"failed"}
    assert all("boom" in e for e in df["error"])


def test_build_heartbeat_rows_summary_branch_written_and_no_failures():
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    summary = _summary(
        match_written=True,
        goal_writes={"e1": _wr(written=True, n_rows=10), "e2": _wr(written=False, n_rows=8)},
    )
    df = snapshot_odds.build_heartbeat_rows(
        job="snapshot_odds", run_ts=run_ts,
        skip_outcome=None, skip_reason=None, summary=summary, run_error=None,
    )
    rows = {r["target_dataset"]: r for r in df.to_dicts()}
    assert rows["odds_match_odds"]["outcome"] == "written"
    assert rows["odds_player_goal_odds"]["outcome"] == "written"  # any() written -> written
    assert rows["odds_player_goal_odds"]["n_rows"] == 18
    assert rows["odds_player_goal_odds"]["error"] is None


def test_build_heartbeat_rows_summary_branch_all_goal_odds_skipped_unchanged():
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    summary = _summary(
        match_written=False,
        goal_writes={"e1": _wr(written=False, n_rows=10)},
    )
    df = snapshot_odds.build_heartbeat_rows(
        job="snapshot_odds", run_ts=run_ts,
        skip_outcome=None, skip_reason=None, summary=summary, run_error=None,
    )
    rows = {r["target_dataset"]: r for r in df.to_dicts()}
    assert rows["odds_match_odds"]["outcome"] == "skipped_unchanged"
    assert rows["odds_player_goal_odds"]["outcome"] == "skipped_unchanged"


def test_build_heartbeat_rows_summary_branch_goal_odds_all_failed():
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    summary = _summary(match_written=True, team_failed={"e1": "unresolved team X"})
    df = snapshot_odds.build_heartbeat_rows(
        job="snapshot_odds", run_ts=run_ts,
        skip_outcome=None, skip_reason=None, summary=summary, run_error=None,
    )
    rows = {r["target_dataset"]: r for r in df.to_dicts()}
    assert rows["odds_player_goal_odds"]["outcome"] == "failed"
    assert "team-identity failure" in rows["odds_player_goal_odds"]["error"]


def test_build_heartbeat_rows_summary_branch_no_fixtures_at_all():
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    summary = _summary(match_written=True)  # empty goal_writes, no failures either
    df = snapshot_odds.build_heartbeat_rows(
        job="snapshot_odds", run_ts=run_ts,
        skip_outcome=None, skip_reason=None, summary=summary, run_error=None,
    )
    rows = {r["target_dataset"]: r for r in df.to_dicts()}
    assert rows["odds_player_goal_odds"]["outcome"] == "no_fixtures"
    assert rows["odds_player_goal_odds"]["n_rows"] == 0


def test_build_heartbeat_rows_run_ts_column_is_naive_utc():
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    df = snapshot_odds.build_heartbeat_rows(
        job="snapshot_odds", run_ts=run_ts,
        skip_outcome="skipped_outside_window", skip_reason="x", summary=None, run_error=None,
    )
    dtype = df.schema["run_ts"]
    assert isinstance(dtype, pl.Datetime)
    assert dtype.time_zone is None


def test_build_heartbeat_rows_validates_against_the_canonical_schema():
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    df = snapshot_odds.build_heartbeat_rows(
        job="snapshot_odds", run_ts=run_ts,
        skip_outcome=None, skip_reason=None, summary=_summary(match_written=True), run_error=None,
    )
    CANONICAL_SCHEMAS[JOB_HEARTBEAT_RUN].validate(df)  # must not raise


def test_build_heartbeat_rows_entity_key_unique_within_one_batch():
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    df = snapshot_odds.build_heartbeat_rows(
        job=snapshot_odds.HEARTBEAT_JOB, run_ts=run_ts,
        skip_outcome=None, skip_reason=None, summary=_summary(match_written=True), run_error=None,
    )
    keys = list(zip(df["job"], df["run_ts"], df["target_dataset"]))
    assert len(keys) == len(snapshot_odds.HEARTBEAT_TARGETS)
    assert len(keys) == len(set(keys))


# -- write_heartbeat / write_heartbeat_safely ------------------------------


def test_write_heartbeat_always_passes_skip_if_unchanged_false():
    store = MagicMock(spec=BitemporalStore)
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    df = snapshot_odds.build_heartbeat_rows(
        job="snapshot_odds", run_ts=run_ts,
        skip_outcome="skipped_outside_window", skip_reason="x", summary=None, run_error=None,
    )
    snapshot_odds.write_heartbeat(store, df, run_ts=run_ts)
    assert store.write.call_args.args[0] == "heartbeat"
    assert store.write.call_args.kwargs["skip_if_unchanged"] is False


def test_write_heartbeat_two_identical_skip_runs_both_persist_distinct_rows():
    # Two consecutive "skipped_outside_window" no-ops with the exact same
    # reason string are the realistic collision case for this job (unlike
    # snapshot_bootstrap's per-dataset payload, there's no natural per-run
    # variation in a skip's content besides run_ts itself).
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        run_ts1 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        run_ts2 = datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)
        for ts in (run_ts1, run_ts2):
            df = snapshot_odds.build_heartbeat_rows(
                job="snapshot_odds", run_ts=ts,
                skip_outcome="skipped_outside_window", skip_reason="same reason every time",
                summary=None, run_error=None,
            )
            snapshot_odds.write_heartbeat(store, df, run_ts=ts)
        stream = store.observations("heartbeat", until=run_ts2 + timedelta(minutes=1))
        assert stream.height == 2 * len(snapshot_odds.HEARTBEAT_TARGETS)


def test_write_heartbeat_safely_swallows_a_store_write_failure():
    store = MagicMock(spec=BitemporalStore)
    store.write.side_effect = RuntimeError("disk full")
    run_ts = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    snapshot_odds.write_heartbeat_safely(
        store, job="snapshot_odds", run_ts=run_ts,
        skip_outcome="skipped_outside_window", skip_reason="x", summary=None, run_error=None,
    )  # must not raise


# -- main() integration: window gate short-circuits before any network work --


def _patch_capture_stack(monkeypatch, *, run_return=None, run_side_effect=None):
    """Patches every symbol main() would otherwise use to reach the network,
    so a test proving 'the skip path never touches these' can assert
    `.assert_not_called()`, and a test proving 'the open-window path calls
    run() once' can control its return without any real HTTP call."""
    fpl_provider_instance = MagicMock()
    fpl_provider_instance.fetch.return_value.rows = pl.DataFrame({"id": [1]})
    fpl_provider_cls = MagicMock(return_value=fpl_provider_instance)
    fpl_client_cls = MagicMock()
    build_transport_fn = MagicMock()
    odds_provider_cls = MagicMock()
    run_fn = MagicMock()
    if run_side_effect is not None:
        run_fn.side_effect = run_side_effect
    else:
        run_fn.return_value = run_return if run_return is not None else _summary(match_written=True)

    monkeypatch.setattr(snapshot_odds, "FPLProvider", fpl_provider_cls)
    monkeypatch.setattr(snapshot_odds, "FPLClient", fpl_client_cls)
    monkeypatch.setattr(snapshot_odds, "build_transport", build_transport_fn)
    monkeypatch.setattr(snapshot_odds, "OddsProvider", odds_provider_cls)
    monkeypatch.setattr(snapshot_odds, "run", run_fn)
    return {"fpl_provider_cls": fpl_provider_cls, "build_transport_fn": build_transport_fn, "run_fn": run_fn}


def test_main_outside_window_never_touches_any_capture_stack_symbol(monkeypatch):
    mocks = _patch_capture_stack(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=40)  # outside every window
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)

        exit_code = snapshot_odds.main(["--store-path", tmp])
        assert exit_code == 0

        mocks["fpl_provider_cls"].assert_not_called()
        mocks["build_transport_fn"].assert_not_called()
        mocks["run_fn"].assert_not_called()

        heartbeat = store.observations("heartbeat", until=datetime.now(timezone.utc) + timedelta(minutes=1))
        assert heartbeat.height == len(snapshot_odds.HEARTBEAT_TARGETS)
        assert set(heartbeat["outcome"]) == {"skipped_outside_window"}


def test_main_missing_api_key_inside_window_writes_failed_heartbeat_and_returns_1(monkeypatch):
    mocks = _patch_capture_stack(monkeypatch)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    # Prevent a real .env file (if any) from supplying the key during this test.
    monkeypatch.setattr(snapshot_odds, "_load_dotenv_into_environ", lambda _path: None)
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=2)  # inside T-2h
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)

        exit_code = snapshot_odds.main(["--store-path", tmp])
        assert exit_code == 1
        mocks["fpl_provider_cls"].assert_not_called()  # never got past the key check

        heartbeat = store.observations("heartbeat", until=datetime.now(timezone.utc) + timedelta(minutes=1))
        assert set(heartbeat["outcome"]) == {"failed"}
        assert all("THE_ODDS_API_KEY" in e for e in heartbeat["error"])


def test_main_inside_window_with_key_calls_run_once_and_writes_success_heartbeat(monkeypatch):
    mocks = _patch_capture_stack(monkeypatch, run_return=_summary(match_written=True, goal_writes={"e1": _wr(written=True)}))
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key-not-real")
    monkeypatch.setattr(snapshot_odds, "_load_dotenv_into_environ", lambda _path: None)
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=2)
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)

        exit_code = snapshot_odds.main(["--store-path", tmp])
        assert exit_code == 0
        mocks["run_fn"].assert_called_once()

        heartbeat = store.observations("heartbeat", until=datetime.now(timezone.utc) + timedelta(minutes=1))
        assert set(heartbeat["outcome"]) == {"written"}


def test_main_a_second_firing_in_the_same_window_after_success_does_not_call_run_again(monkeypatch):
    mocks = _patch_capture_stack(monkeypatch, run_return=_summary(match_written=True))
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key-not-real")
    monkeypatch.setattr(snapshot_odds, "_load_dotenv_into_environ", lambda _path: None)
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=2)
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)

        exit_code_1 = snapshot_odds.main(["--store-path", tmp])
        assert exit_code_1 == 0
        assert mocks["run_fn"].call_count == 1

        # A "second firing" a few minutes later, still inside the same window.
        exit_code_2 = snapshot_odds.main(["--store-path", tmp])
        assert exit_code_2 == 0
        assert mocks["run_fn"].call_count == 1  # NOT called again -- the at-most-once guarantee


# -- secret redaction on the scheduled path (CLAUDE.md: verify, don't assume) --


def test_main_sets_urllib3_to_warning_even_on_the_skip_path(monkeypatch):
    # The redaction fix (module docstring, scripts/snapshot_odds.py's own
    # SECURITY comment) must not depend on reaching the capture stack at
    # all -- it is set at the very top of main(), before the window
    # decision. Proven here by driving main() down the skip path (which
    # never imports build_transport) and asserting the level regardless.
    import logging

    logging.getLogger("urllib3").setLevel(logging.NOTSET)
    _patch_capture_stack(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        store = BitemporalStore(base_path=Path(tmp))
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=40)
        _write_events(store, event_id=3, deadline_iso=_iso(deadline), as_of=now)
        snapshot_odds.main(["--store-path", tmp, "-v"])
    assert logging.getLogger("urllib3").level == logging.WARNING
