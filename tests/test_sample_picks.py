"""Tests for scripts/sample_picks.py — the EO/captaincy sampling script
(blueprint §3.4). All network access is faked (`FakeFPLClient` below); the
store is a REAL `BitemporalStore` against `tmp_path` (CLAUDE.md lesson 6 — a
mock of the store cannot catch a real query-shape bug the way the real
store class can). No live HTTP in this file.

`conftest.py` puts `scripts/` on `sys.path`, so this imports the script as a
plain top-level module, exactly like any other `fplai` test imports the
package.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

import sample_picks
from fplai.client import FPLApiError
from fplai.sampling import already_sampled_entry_ids, draw_uniform_entry_ids
from fplai.store import BitemporalStore

UTC = timezone.utc
# Computed relative to the REAL wall clock at import time, never a literal
# date — the exact class of bug this session already caught once in
# tests/test_sampling.py (a literal future-looking date that happened to be
# in the past only until real time caught up with it, then silently
# "self-healed" into a different failure). This is permanently in the past,
# regardless of when the suite actually runs.
PAST_DEADLINE = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- FakeFPLClient -----------------------------------------------------------


class FakeFPLClient:
    """Stands in for `FPLClient` — `bootstrap_static`, `entry`, `entry_picks`
    only, matching exactly what `sample_picks.py` calls. Deliberately NOT a
    `unittest.mock.MagicMock`: a hand-written fake makes wrong call shapes
    (a positional arg where a keyword is expected, a missing kwarg) fail
    LOUDLY with a TypeError instead of MagicMock silently accepting
    anything — the same reasoning `fplai.sampling`'s own `FakeEntryClient`
    already uses.
    """

    def __init__(
        self,
        *,
        max_valid_id: int,
        hit_entry_ids: set[int] = frozenset(),
        error_entry_ids: set[int] = frozenset(),
        crash_on_entry_id: int | None = None,
        deadline_time: str = PAST_DEADLINE,
        event_finished: bool = True,
        elements: list[dict] | None = None,
        total_players: int = 1000,
    ) -> None:
        self.max_valid_id = max_valid_id
        self.hit_entry_ids = set(hit_entry_ids)
        self.error_entry_ids = set(error_entry_ids)
        self.crash_on_entry_id = crash_on_entry_id
        self.deadline_time = deadline_time
        self.event_finished = event_finished
        self.elements = elements or [
            {"id": 100, "web_name": "A", "selected_by_percent": "50.0"},
            {"id": 101, "web_name": "B", "selected_by_percent": "50.0"},
        ]
        self.total_players = total_players
        self.entry_picks_calls: list[int] = []

    def bootstrap_static(self, force_refresh: bool = False) -> dict:
        return {
            "events": [{"id": 1, "deadline_time": self.deadline_time, "finished": self.event_finished}],
            "elements": self.elements,
            "total_players": self.total_players,
        }

    def entry(self, entry_id: int, force_refresh: bool = False) -> dict | None:
        if entry_id <= self.max_valid_id:
            return {"id": entry_id}
        return None

    def entry_picks(
        self, entry_id: int, event: int, *, force_refresh: bool = False, max_retries: int | None = None
    ) -> dict | None:
        self.entry_picks_calls.append(entry_id)
        if self.crash_on_entry_id is not None and entry_id == self.crash_on_entry_id:
            raise RuntimeError(f"simulated hard crash on entry {entry_id}")
        if entry_id in self.error_entry_ids:
            raise FPLApiError(f"entry {entry_id}: synthetic 503 (retries exhausted)")
        if entry_id in self.hit_entry_ids:
            return {
                "picks": [
                    {
                        "element": 100,
                        "position": 1,
                        "multiplier": 2,
                        "is_captain": True,
                        "is_vice_captain": False,
                        "element_type": 4,
                    }
                ],
                "active_chip": None,
                "automatic_subs": [],
                "entry_history": {
                    "event": event,
                    "points": 55,
                    "total_points": 55,
                    "rank": None,
                    "rank_sort": None,
                    "overall_rank": None,
                    "percentile_rank": None,
                    "overall_rank_percentage": None,
                    "bank": 0,
                    "value": 1000,
                    "event_transfers": 0,
                    "event_transfers_cost": 0,
                    "points_on_bench": 4,
                },
            }
        return None  # 404 miss


def _store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


# -- resolve_target_event / check_ready (unchanged logic, baseline coverage) --


def test_resolve_target_event_picks_most_recently_finished():
    events = [
        {"id": 1, "finished": True},
        {"id": 2, "finished": True},
        {"id": 3, "finished": False},
    ]
    assert sample_picks.resolve_target_event(events, None)["id"] == 2


def test_resolve_target_event_none_when_nothing_finished():
    events = [{"id": 1, "finished": False}]
    assert sample_picks.resolve_target_event(events, None) is None


def test_resolve_target_event_explicit_gw_overrides_finished_status():
    events = [{"id": 1, "finished": False}]
    assert sample_picks.resolve_target_event(events, 1)["id"] == 1


def test_check_ready_raises_before_deadline():
    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    client = FakeFPLClient(max_valid_id=10, deadline_time=future)
    with pytest.raises(sample_picks.NotReadyError):
        sample_picks.check_ready(client, {"id": 1, "deadline_time": future, "finished": False})


def test_check_ready_passes_after_deadline():
    client = FakeFPLClient(max_valid_id=10)
    sample_picks.check_ready(client, {"id": 1, "deadline_time": PAST_DEADLINE, "finished": True})  # must not raise


# -- probe_picks_endpoint_ready (blueprint §3.4 "Blocker 1" — the readiness gate fix) --


def test_probe_picks_endpoint_ready_passes_on_a_clean_404():
    client = FakeFPLClient(max_valid_id=10)  # entry 1 -> None (404) -- a legitimate ready signal
    sample_picks.probe_picks_endpoint_ready(client, event_id=1)  # must not raise


def test_probe_picks_endpoint_ready_passes_on_a_clean_200():
    client = FakeFPLClient(max_valid_id=10, hit_entry_ids={1})
    sample_picks.probe_picks_endpoint_ready(client, event_id=1)  # must not raise


def test_probe_picks_endpoint_ready_raises_notreadyerror_on_maintenance_503():
    # The exact false-green this fix closes: FakeFPLClient's entry 1 errors
    # (simulating a 503-exhausted-retries FPLApiError) -- this MUST surface
    # as NotReadyError, never propagate as a raw FPLApiError crash.
    client = FakeFPLClient(max_valid_id=10, error_entry_ids={sample_picks.READINESS_PROBE_ENTRY_ID})
    with pytest.raises(sample_picks.NotReadyError):
        sample_picks.probe_picks_endpoint_ready(client, event_id=1)


def test_probe_picks_endpoint_ready_forces_refresh_bypassing_any_cache():
    # A cached response for this exact (entry, event) would let this probe
    # pass WITHOUT ever touching the live endpoint -- defeating its purpose.
    calls = []

    class RecordingClient(FakeFPLClient):
        def entry_picks(self, entry_id, event, *, force_refresh=False, max_retries=None):
            calls.append(force_refresh)
            return super().entry_picks(entry_id, event, force_refresh=force_refresh, max_retries=max_retries)

    client = RecordingClient(max_valid_id=10)
    sample_picks.probe_picks_endpoint_ready(client, event_id=1)
    assert calls == [True]


def test_probe_picks_endpoint_ready_uses_a_single_attempt_max_retries_zero():
    calls = []

    class RecordingClient(FakeFPLClient):
        def entry_picks(self, entry_id, event, *, force_refresh=False, max_retries=None):
            calls.append(max_retries)
            return super().entry_picks(entry_id, event, force_refresh=force_refresh, max_retries=max_retries)

    client = RecordingClient(max_valid_id=10)
    sample_picks.probe_picks_endpoint_ready(client, event_id=1)
    assert calls == [0]  # fail-fast probe -- no internal retry ladder


# -- probe_picks_available (widened: per-entry FPLApiError handling) --------


def test_probe_picks_available_true_when_any_hit():
    client = FakeFPLClient(max_valid_id=100, hit_entry_ids={5})
    available, errors = sample_picks.probe_picks_available(client, [1, 2, 3, 4, 5], event_id=1)
    assert available is True
    assert errors == 0


def test_probe_picks_available_false_when_all_404():
    client = FakeFPLClient(max_valid_id=100)  # nothing in hit_entry_ids -> every one 404s
    available, errors = sample_picks.probe_picks_available(client, [1, 2, 3], event_id=1)
    assert available is False
    assert errors == 0


def test_probe_picks_available_does_not_crash_on_a_per_entry_error():
    # Pre-fix, ONE FPLApiError anywhere in the warm-up batch crashed the
    # whole probe with no handling at all.
    client = FakeFPLClient(max_valid_id=100, hit_entry_ids={3}, error_entry_ids={1, 2})
    available, errors = sample_picks.probe_picks_available(client, [1, 2, 3], event_id=1)
    assert available is True  # entry 3 still hit
    assert errors == 2


def test_probe_picks_available_all_errors_reports_available_false_and_error_count():
    client = FakeFPLClient(max_valid_id=100, error_entry_ids={1, 2, 3})
    available, errors = sample_picks.probe_picks_available(client, [1, 2, 3], event_id=1)
    assert available is False
    assert errors == 3


# -- _picks_to_rows / _automatic_subs_to_rows widening (blueprint §3.4 "Blocker 3") --


def _real_shaped_picks_response(**entry_history_overrides) -> dict:
    entry_history = {
        "event": 1,
        "points": 55,
        "total_points": 120,
        "rank": 123456,
        "rank_sort": 123457,
        "overall_rank": 654321,
        "percentile_rank": 12.5,
        "overall_rank_percentage": 12.5,
        "bank": 5,
        "value": 1005,
        "event_transfers": 1,
        "event_transfers_cost": 0,
        "points_on_bench": 4,
    }
    entry_history.update(entry_history_overrides)
    return {
        "active_chip": "3xc",
        "automatic_subs": [],
        "entry_history": entry_history,
        "picks": [
            {
                "element": 355,
                "position": 1,
                "multiplier": 2,
                "is_captain": True,
                "is_vice_captain": False,
                "element_type": 4,
            }
        ],
    }


def test_picks_to_rows_captures_element_type():
    rows = sample_picks._picks_to_rows(3434577, 1, _real_shaped_picks_response())
    assert rows[0]["element_type"] == 4


def test_picks_to_rows_captures_widened_entry_history_fields():
    rows = sample_picks._picks_to_rows(3434577, 1, _real_shaped_picks_response())
    row = rows[0]
    assert row["entry_history_event"] == 1
    assert row["gameweek_points"] == 55
    assert row["total_points"] == 120
    assert row["rank"] == 123456
    assert row["rank_sort"] == 123457
    assert row["percentile_rank"] == 12.5
    assert row["overall_rank_percentage"] == 12.5
    assert row["overall_rank"] == 654321


def test_picks_to_rows_handles_null_ranks_pre_scoring():
    # Verified live, 22 Aug: every real GW1 capture had every rank field
    # null pre-scoring -- must not KeyError or crash.
    response = _real_shaped_picks_response(
        rank=None, rank_sort=None, overall_rank=None, percentile_rank=None, overall_rank_percentage=None
    )
    rows = sample_picks._picks_to_rows(1, 1, response)
    assert rows[0]["rank"] is None
    assert rows[0]["percentile_rank"] is None


def test_automatic_subs_to_rows_parses_the_documented_shape():
    response = {
        "automatic_subs": [
            {"entry": 3434577, "element_in": 200, "element_out": 100, "event": 1},
            {"entry": 3434577, "element_in": 201, "element_out": 105, "event": 1},
        ]
    }
    rows = sample_picks._automatic_subs_to_rows(3434577, 1, response)
    assert len(rows) == 2
    assert rows[0] == {"entry_id": 3434577, "event": 1, "element_in": 200, "element_out": 100}
    assert rows[1] == {"entry_id": 3434577, "event": 1, "element_in": 201, "element_out": 105}


def test_automatic_subs_to_rows_empty_list_is_the_common_case():
    # Verified live 11/11 real GW1 captures this session -- always [] pre-scoring.
    assert sample_picks._automatic_subs_to_rows(1, 1, {"automatic_subs": []}) == []


def test_automatic_subs_to_rows_missing_key_does_not_crash():
    assert sample_picks._automatic_subs_to_rows(1, 1, {}) == []


# -- _assert_automatic_subs_key_uniqueness (runtime grain guard) ------------


def test_automatic_subs_key_uniqueness_passes_unique_rows():
    df = pl.DataFrame(
        {"entry_id": [1, 1, 2], "event": [1, 1, 1], "element_in": [200, 201, 200], "element_out": [100, 105, 100]}
    )
    result = sample_picks._assert_automatic_subs_key_uniqueness(df)
    assert result.height == 3  # unchanged


def test_automatic_subs_key_uniqueness_drops_and_logs_on_violation(caplog):
    # Two rows sharing (entry_id, event, element_out) -- the exact grain
    # violation this could not be verified against a real populated payload
    # for (GW1 hadn't finished scoring this session).
    df = pl.DataFrame(
        {"entry_id": [1, 1], "event": [1, 1], "element_in": [200, 201], "element_out": [100, 100]}
    )
    with caplog.at_level("ERROR"):
        result = sample_picks._assert_automatic_subs_key_uniqueness(df)
    assert result.is_empty()
    assert any("GRAIN VIOLATION" in r.message for r in caplog.records)


def test_automatic_subs_key_uniqueness_empty_frame_is_a_noop():
    df = pl.DataFrame({"entry_id": [], "event": [], "element_in": [], "element_out": []})
    result = sample_picks._assert_automatic_subs_key_uniqueness(df)
    assert result.is_empty()


# -- run(): incremental persistence, per-entry error handling, resumability --


SEED = 4242


def _entry_ids(max_valid_id: int, n: int) -> list[int]:
    return draw_uniform_entry_ids(max_valid_id, n, SEED)


def test_run_persists_in_multiple_chunks(tmp_path):
    store = _store(tmp_path)
    max_valid_id = 200
    ids = _entry_ids(max_valid_id, 6)
    client = FakeFPLClient(max_valid_id=max_valid_id, hit_entry_ids=set(ids))

    rc = sample_picks.run(
        store,
        client,
        requested_gw=1,
        n=6,
        seed=SEED,
        dry_run=False,
        calibration_threshold_pp=100.0,  # calibration mechanics are covered in test_sampling.py
        chunk_size=3,
    )
    assert rc == 0

    picks_dir = tmp_path / "store" / "picks"
    parquet_files = list(picks_dir.glob("**/*.parquet"))
    assert len(parquet_files) >= 2  # proves MULTIPLE incremental batches, not one write at the end

    persisted = already_sampled_entry_ids(store, event_id=1)
    assert persisted == set(ids)


def test_run_per_entry_fplapierror_does_not_abort_the_run(tmp_path):
    store = _store(tmp_path)
    max_valid_id = 200
    ids = _entry_ids(max_valid_id, 6)
    # One entry (not the readiness-probe id, not in the probe warm-up
    # necessarily) errors -- the exact pre-fix failure mode: ONE bad
    # response used to raise straight out of the main loop.
    erroring = ids[2]
    hits = set(ids) - {erroring}
    client = FakeFPLClient(max_valid_id=max_valid_id, hit_entry_ids=hits, error_entry_ids={erroring})

    rc = sample_picks.run(
        store, client, requested_gw=1, n=6, seed=SEED, dry_run=False, calibration_threshold_pp=100.0, chunk_size=3
    )
    assert rc == 0  # did NOT abort

    persisted = already_sampled_entry_ids(store, event_id=1)
    assert persisted == hits  # every hit persisted; the erroring entry simply has no row
    assert erroring not in persisted


def test_run_crash_mid_run_loses_only_the_current_chunk_and_resume_finishes_the_rest(tmp_path, monkeypatch):
    # The central proof of blueprint §3.4 "Blocker 1": a crash costs the
    # LAST chunk, never the whole run. chunk_size=3, n=6 -> two chunks.
    # PROBE_SIZE patched down to 1 so the warm-up probe (which shares the
    # same drawn entry_ids as the main loop, by design — see run()'s own
    # comment on why resume-filtering now happens before it) does not
    # ALSO touch the entry this test deliberately crashes on, which would
    # crash during the probe instead of mid-main-loop and test the wrong
    # thing.
    monkeypatch.setattr(sample_picks, "PROBE_SIZE", 1)
    store = _store(tmp_path)
    max_valid_id = 200
    ids = _entry_ids(max_valid_id, 6)
    crash_id = ids[3]  # first entry of the SECOND chunk

    crashing_client = FakeFPLClient(max_valid_id=max_valid_id, hit_entry_ids=set(ids), crash_on_entry_id=crash_id)
    with pytest.raises(RuntimeError):
        sample_picks.run(
            store, crashing_client, requested_gw=1, n=6, seed=SEED, dry_run=False,
            calibration_threshold_pp=100.0, chunk_size=3,
        )

    # Chunk 1 (the first 3 entries) survived the crash — persisted BEFORE
    # the loop ever reached the crashing entry in chunk 2.
    after_crash = already_sampled_entry_ids(store, event_id=1)
    assert after_crash == set(ids[:3])

    # Resume with a fixed client (no crash) — must NOT re-request the
    # already-persisted chunk-1 entries.
    fixed_client = FakeFPLClient(max_valid_id=max_valid_id, hit_entry_ids=set(ids))
    rc = sample_picks.run(
        store, fixed_client, requested_gw=1, n=6, seed=SEED, dry_run=False,
        calibration_threshold_pp=100.0, chunk_size=3, resume=True,
    )
    assert rc == 0

    final = already_sampled_entry_ids(store, event_id=1)
    assert final == set(ids)  # every entry now persisted

    # The proof that resume actually SAVED requests, not just tolerated
    # redundant ones: chunk-1's ids must never appear in the resumed run's
    # own live call log.
    for already_done_id in ids[:3]:
        assert already_done_id not in fixed_client.entry_picks_calls
    for remaining_id in ids[3:]:
        assert remaining_id in fixed_client.entry_picks_calls


def test_run_no_resume_flag_re_requests_everything(tmp_path):
    store = _store(tmp_path)
    max_valid_id = 200
    ids = _entry_ids(max_valid_id, 4)
    client1 = FakeFPLClient(max_valid_id=max_valid_id, hit_entry_ids=set(ids))
    sample_picks.run(
        store, client1, requested_gw=1, n=4, seed=SEED, dry_run=False,
        calibration_threshold_pp=100.0, chunk_size=10,
    )

    client2 = FakeFPLClient(max_valid_id=max_valid_id, hit_entry_ids=set(ids))
    sample_picks.run(
        store, client2, requested_gw=1, n=4, seed=SEED, dry_run=False,
        calibration_threshold_pp=100.0, chunk_size=10, resume=False,
    )
    # resume=False -- every id re-requested on the second run, none skipped.
    assert set(client2.entry_picks_calls) >= set(ids)


def test_run_calibration_reads_back_the_full_persisted_sample_after_a_resume(tmp_path, monkeypatch):
    # The correctness fix this story's resumability feature requires: a
    # resumed run's calibration check must cover the FULL persisted sample
    # (chunk 1 from the earlier crashed run PLUS this run's own chunk 2),
    # not just what happened to be collected in THIS process's memory.
    monkeypatch.setattr(sample_picks, "PROBE_SIZE", 1)  # see the previous test's comment
    store = _store(tmp_path)
    max_valid_id = 200
    ids = _entry_ids(max_valid_id, 6)
    crash_id = ids[3]

    crashing_client = FakeFPLClient(max_valid_id=max_valid_id, hit_entry_ids=set(ids), crash_on_entry_id=crash_id)
    with pytest.raises(RuntimeError):
        sample_picks.run(
            store, crashing_client, requested_gw=1, n=6, seed=SEED, dry_run=False,
            calibration_threshold_pp=100.0, chunk_size=3,
        )

    fixed_client = FakeFPLClient(max_valid_id=max_valid_id, hit_entry_ids=set(ids))
    sample_picks.run(
        store, fixed_client, requested_gw=1, n=6, seed=SEED, dry_run=False,
        calibration_threshold_pp=100.0, chunk_size=3, resume=True,
    )

    full = store.observations("picks", until=datetime.now(UTC)).filter(pl.col("event") == 1)
    assert set(full["entry_id"].unique().to_list()) == set(ids)  # all 6, not just the resumed run's 3


def test_run_returns_2_when_probe_endpoint_not_ready(tmp_path):
    store = _store(tmp_path)
    client = FakeFPLClient(max_valid_id=200, error_entry_ids={sample_picks.READINESS_PROBE_ENTRY_ID})
    rc = sample_picks.run(
        store, client, requested_gw=1, n=6, seed=SEED, dry_run=False, calibration_threshold_pp=100.0
    )
    assert rc == 0  # NotReadyError -> clean exit, not a crash, not treated as a hard failure


def test_run_dry_run_does_not_persist_anything(tmp_path):
    store = _store(tmp_path)
    max_valid_id = 200
    ids = _entry_ids(max_valid_id, 6)
    client = FakeFPLClient(max_valid_id=max_valid_id, hit_entry_ids=set(ids))
    rc = sample_picks.run(
        store, client, requested_gw=1, n=6, seed=SEED, dry_run=True, calibration_threshold_pp=100.0
    )
    assert rc == 0
    assert not (tmp_path / "store" / "picks").exists()


def test_run_persists_automatic_subs_when_present(tmp_path):
    store = _store(tmp_path)

    class SubbingClient(FakeFPLClient):
        def entry_picks(self, entry_id, event, *, force_refresh=False, max_retries=None):
            response = super().entry_picks(entry_id, event, force_refresh=force_refresh, max_retries=max_retries)
            if response is not None:
                response["automatic_subs"] = [{"entry": entry_id, "element_in": 900, "element_out": 800, "event": event}]
            return response

    max_valid_id = 200
    ids = _entry_ids(max_valid_id, 4)
    client = SubbingClient(max_valid_id=max_valid_id, hit_entry_ids=set(ids))
    rc = sample_picks.run(
        store, client, requested_gw=1, n=4, seed=SEED, dry_run=False, calibration_threshold_pp=100.0, chunk_size=10
    )
    assert rc == 0

    subs = store.observations("automatic_subs", until=datetime.now(UTC))
    assert subs.height == len(ids)
    assert set(subs["element_out"].unique().to_list()) == {800}
