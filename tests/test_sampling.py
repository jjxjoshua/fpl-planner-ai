"""Tests for fplai.sampling — id-space binary search, uniform draw, and the
mandatory ownership calibration check. Network is mocked via a fake client."""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from fplai.sampling import (
    already_sampled_entry_ids,
    calibration_check,
    draw_uniform_entry_ids,
    find_max_valid_entry_id,
)
from fplai.store import BitemporalStore


class FakeEntryClient:
    """Stands in for FPLClient for `entry()` only — enough for the binary
    search logic, with zero network."""

    def __init__(self, max_valid_id: int):
        self.max_valid_id = max_valid_id
        self.calls = 0

    def entry(self, entry_id: int):
        self.calls += 1
        if entry_id <= self.max_valid_id:
            return {"id": entry_id}
        return None


# -- find_max_valid_entry_id ---------------------------------------------


@pytest.mark.parametrize("true_max", [1, 5, 999, 6_051_747, 10_000_000])
def test_find_max_valid_entry_id_converges(true_max):
    client = FakeEntryClient(true_max)
    found = find_max_valid_entry_id(client, initial_upper=max(2, true_max // 3))
    assert found == true_max


def test_find_max_valid_entry_id_is_request_efficient():
    # blueprint §3.4 / wiki §2.4: "~25 requests" for a range in the low millions.
    client = FakeEntryClient(6_051_747)
    find_max_valid_entry_id(client)
    assert client.calls < 40


def test_find_max_valid_entry_id_raises_if_lower_bound_invalid():
    client = FakeEntryClient(max_valid_id=0)  # even id 1 is invalid
    with pytest.raises(RuntimeError):
        find_max_valid_entry_id(client)


def test_find_max_valid_entry_id_respects_hard_cap():
    client = FakeEntryClient(max_valid_id=10**12)
    with pytest.raises(RuntimeError):
        find_max_valid_entry_id(client, hard_cap=1_000_000)


# -- draw_uniform_entry_ids -----------------------------------------------


def test_draw_uniform_entry_ids_is_deterministic():
    a = draw_uniform_entry_ids(1_000_000, 100, seed=42)
    b = draw_uniform_entry_ids(1_000_000, 100, seed=42)
    assert a == b


def test_draw_uniform_entry_ids_different_seeds_differ():
    a = draw_uniform_entry_ids(1_000_000, 100, seed=1)
    b = draw_uniform_entry_ids(1_000_000, 100, seed=2)
    assert a != b


def test_draw_uniform_entry_ids_are_distinct_and_in_range():
    ids = draw_uniform_entry_ids(1000, 500, seed=7)
    assert len(ids) == len(set(ids)) == 500
    assert all(1 <= i <= 1000 for i in ids)


def test_draw_uniform_entry_ids_rejects_oversized_n():
    with pytest.raises(ValueError):
        draw_uniform_entry_ids(10, 11, seed=1)


# -- calibration_check -----------------------------------------------------


def test_calibration_check_passes_on_matching_distributions():
    sampled = pl.DataFrame({"element": [1, 2, 3], "sampled_pct": [50.0, 20.0, 5.0]})
    bootstrap = pl.DataFrame({"id": [1, 2, 3], "selected_by_percent": ["50.2", "19.8", "5.1"]})
    result = calibration_check(sampled, bootstrap, threshold_pp=3.0)
    assert result.passed
    assert result.max_deviation_pp < 1.0


def test_calibration_check_fails_on_biased_sample():
    sampled = pl.DataFrame({"element": [1, 2], "sampled_pct": [10.0, 5.0]})
    bootstrap = pl.DataFrame({"id": [1, 2], "selected_by_percent": ["50.0", "5.0"]})
    result = calibration_check(sampled, bootstrap, threshold_pp=3.0)
    assert not result.passed
    assert result.max_deviation_pp == pytest.approx(40.0)
    assert result.worst_offenders[0]["element"] == 1


def test_calibration_check_treats_missing_element_in_sample_as_zero():
    # a widely-owned player who happened not to appear in the sample at all
    # is itself a large deviation, not something to silently ignore.
    sampled = pl.DataFrame({"element": [1], "sampled_pct": [50.0]})
    bootstrap = pl.DataFrame({"id": [1, 2], "selected_by_percent": ["50.0", "40.0"]})
    result = calibration_check(sampled, bootstrap, threshold_pp=3.0)
    assert not result.passed


def test_calibration_check_ignores_near_zero_owned_players():
    sampled = pl.DataFrame({"element": [1], "sampled_pct": [0.0]})
    bootstrap = pl.DataFrame({"id": [1], "selected_by_percent": ["0.1"]})
    result = calibration_check(sampled, bootstrap, threshold_pp=3.0, min_selected_by_percent=1.0)
    assert result.n_compared == 0
    assert result.passed


# -- already_sampled_entry_ids (blueprint §3.4 incremental persistence, s003) --
# Uses a REAL BitemporalStore against a tmp_path — CLAUDE.md lesson 6: a
# mock of the store cannot catch a real query-shape bug (a wrong filter
# column, a wrong dataset name) the way exercising the actual DuckDB/Parquet
# path can.

UTC = timezone.utc

# Deliberately injected, never the real wall clock (CLAUDE.md rule 7 —
# already_sampled_entry_ids's own docstring warns against exactly this: a
# test using a fixed fixture date alongside the REAL datetime.now() is only
# correct until that date arrives, then silently "self-heals" into passing
# for the wrong reason — a rule-5 "test that cannot fail" instance caught
# and fixed during this session before it could do that). Fixed, far in the
# future relative to every fixture timestamp below, so `until=` never
# excludes a row this test itself wrote.
FIXED_NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _now_fn():
    return FIXED_NOW


def _picks_row(entry_id: int, event: int, element: int) -> dict:
    return {
        "event": event,
        "entry_id": entry_id,
        "element": element,
        "multiplier": 1,
        "is_captain": False,
        "is_vice_captain": False,
        "overall_rank": None,
    }


def test_already_sampled_entry_ids_empty_store_returns_empty_set(tmp_path):
    store = BitemporalStore(base_path=tmp_path / "store")
    assert already_sampled_entry_ids(store, event_id=1, now_fn=_now_fn) == set()


def test_already_sampled_entry_ids_returns_entries_from_a_prior_chunk(tmp_path):
    store = BitemporalStore(base_path=tmp_path / "store")
    df = pl.DataFrame([_picks_row(10, 1, 100), _picks_row(10, 1, 101), _picks_row(20, 1, 100)])
    store.write(
        "picks",
        df,
        valid_at=datetime(2026, 8, 22, 0, 0, tzinfo=UTC),
        observed_at=datetime(2026, 8, 22, 1, 0, tzinfo=UTC),
        source="fpl_api:entry_picks",
        skip_if_unchanged=False,
    )
    assert already_sampled_entry_ids(store, event_id=1, now_fn=_now_fn) == {10, 20}


def test_already_sampled_entry_ids_scoped_to_the_requested_event(tmp_path):
    # A prior GW's sample must not suppress the current GW's entries — the
    # exact bug this function must not introduce (conflating gameweeks).
    store = BitemporalStore(base_path=tmp_path / "store")
    df_gw1 = pl.DataFrame([_picks_row(10, 1, 100)])
    df_gw2 = pl.DataFrame([_picks_row(30, 2, 100)])
    store.write(
        "picks", df_gw1,
        valid_at=datetime(2026, 8, 22, 0, 0, tzinfo=UTC), observed_at=datetime(2026, 8, 22, 1, 0, tzinfo=UTC),
        source="fpl_api:entry_picks", skip_if_unchanged=False,
    )
    store.write(
        "picks", df_gw2,
        valid_at=datetime(2026, 8, 29, 0, 0, tzinfo=UTC), observed_at=datetime(2026, 8, 29, 1, 0, tzinfo=UTC),
        source="fpl_api:entry_picks", skip_if_unchanged=False,
    )
    assert already_sampled_entry_ids(store, event_id=1, now_fn=_now_fn) == {10}
    assert already_sampled_entry_ids(store, event_id=2, now_fn=_now_fn) == {30}


def test_already_sampled_entry_ids_accumulates_across_multiple_chunks(tmp_path):
    # Simulates exactly the incremental-persistence scenario: several
    # chunk writes for the SAME event, each with its own honest observed_at.
    store = BitemporalStore(base_path=tmp_path / "store")
    for i, entry_id in enumerate((10, 20, 30)):
        store.write(
            "picks",
            pl.DataFrame([_picks_row(entry_id, 1, 100)]),
            valid_at=datetime(2026, 8, 22, 0, 0, tzinfo=UTC),
            observed_at=datetime(2026, 8, 22, 1, i, tzinfo=UTC),
            source="fpl_api:entry_picks",
            skip_if_unchanged=False,
        )
    assert already_sampled_entry_ids(store, event_id=1, now_fn=_now_fn) == {10, 20, 30}


def test_already_sampled_entry_ids_default_now_fn_is_the_real_clock():
    # The production default must still be the real wall clock — this is a
    # deliberate, narrow exception to "tests never touch the real clock":
    # it exists to prove the DEFAULT wiring itself (an unused-in-body
    # default parameter is exactly the bug this fix closes), not to assert
    # anything about specific data.
    import inspect

    from fplai.sampling import already_sampled_entry_ids as fn

    default_now_fn = inspect.signature(fn).parameters["now_fn"].default
    before = datetime.now(timezone.utc)
    produced = default_now_fn()
    after = datetime.now(timezone.utc)
    assert before <= produced <= after
