"""Tests for fplai.backfill — the backfill orchestrator (E2b story 11).

No network. `FakeProvider` stands in for a real `providers.pl.PLProvider` /
`providers.vaastav.VaastavProvider` — it satisfies the `Provider` protocol
(`provider_id`, `policy`, `supports`, `fetch`) and is configured per-test
with a `{(capability, sorted_params): outcome}` map, where `outcome` is
either a `FetchResult` or an `Exception` instance to raise. This lets each
test assert exactly which requests were (and were not) made — the load-
bearing property for resumability."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from fplai.backfill import (
    GRAIN_PLANS,
    BackfillError,
    BackfillHalted,
    BackfillOrchestrator,
    BackfillSpec,
    CheckpointStore,
    UnitOutcome,
    WorkUnit,
    _season_start_date,
    _valid_at_for,
    dry_run,
    enumerate_static_units,
    estimate_dependent_counts,
)
from fplai.identity import IdentityError
from fplai.providers.base import FetchResult, ProviderError
from fplai.schemas import (
    MATCH_FIXTURES_MATCHWEEK,
    MATCH_LINEUPS_MATCH,
    MATCH_SUBSTITUTIONS_MATCH,
    PLAYER_ATTRIBUTES_CURRENT,
    PLAYER_GAMEWEEK_STATS_GAMEWEEK,
    PLAYER_IDENTITY_SEASON,
    PLAYER_SEASON_STATS_SEASON,
    TEAM_IDENTITY_SEASON,
    TEAM_MATCH_STATS_MATCH,
)
from fplai.store import BitemporalStore
from fplai.transport import BulkFilePolicy, CreditPolicy, DailyQuotaPolicy, RatePolicy, TransportError

UTC = timezone.utc


def _fr(capability, rows: pl.DataFrame, *, provider_id="fake", endpoint="ep", content_hash="h", observed_at=None) -> FetchResult:
    return FetchResult(
        rows=rows, capability=capability, provider_id=provider_id, endpoint=endpoint,
        observed_at=observed_at if observed_at is not None else datetime.now(UTC), content_hash=content_hash,
    )


class FakeProvider:
    provider_id = "fake"
    policy = RatePolicy(requests_per_second=1000.0, jitter_fraction=0.0)

    def __init__(self, handlers: dict[tuple, object]):
        self.handlers = handlers
        self.calls: list[tuple] = []

    def supports(self, capability, *, season=None, competition=None) -> bool:
        return True

    def fetch(self, capability, **params):
        key = (capability, tuple(sorted(params.items())))
        self.calls.append(key)
        if key not in self.handlers:
            raise AssertionError(f"unexpected fetch: {key}")
        outcome = self.handlers[key]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _fixtures_df(season: str, matchweek: int, match_ids: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "match_id": mid, "season": season, "matchweek": matchweek,
                "kickoff": datetime(2025, 8, 15 + matchweek, 15, 0), "home_team_code": 1, "away_team_code": 2,
            }
            for mid in match_ids
        ]
    )


def _lineup_df(match_id: str) -> pl.DataFrame:
    return pl.DataFrame(
        [{"match_id": match_id, "team_code": 1, "player_element_id": 10, "player_code": 100, "role": "start", "position": "GK", "shirt_number": 1, "is_captain": False}]
    )


# -- WorkUnit -----------------------------------------------------------------


def test_workunit_key_is_deterministic_regardless_of_kwarg_order():
    a = WorkUnit.make(MATCH_FIXTURES_MATCHWEEK, "pl_api", "2025", season="2025", matchweek=1)
    b = WorkUnit.make(MATCH_FIXTURES_MATCHWEEK, "pl_api", "2025", matchweek=1, season="2025")
    assert a.key == b.key


def test_workunit_key_differs_on_params():
    a = WorkUnit.make(MATCH_FIXTURES_MATCHWEEK, "pl_api", "2025", season="2025", matchweek=1)
    b = WorkUnit.make(MATCH_FIXTURES_MATCHWEEK, "pl_api", "2025", season="2025", matchweek=2)
    assert a.key != b.key


def test_workunit_meta_never_reaches_fetch_kwargs():
    unit = WorkUnit.make(MATCH_LINEUPS_MATCH, "pl_api", "2025", match_id="7", meta={"kickoff": "2025-08-16T15:00:00+00:00"})
    assert unit.fetch_kwargs == {"match_id": "7"}
    assert unit.meta_dict == {"kickoff": "2025-08-16T15:00:00+00:00"}


# -- BackfillSpec validation ----------------------------------------------------


def test_spec_rejects_empty_capabilities():
    with pytest.raises(BackfillError):
        BackfillSpec(provider_id="pl_api", capabilities=(), seasons=("2025",))


def test_spec_rejects_empty_seasons():
    with pytest.raises(BackfillError):
        BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=())


def test_spec_rejects_live_only_capability_with_a_helpful_hint():
    with pytest.raises(BackfillError, match="LIVE-ONLY"):
        BackfillSpec(provider_id="fpl_api", capabilities=(PLAYER_ATTRIBUTES_CURRENT,), seasons=("2025-26",))


# -- static enumeration ----------------------------------------------------------


def test_enumerate_static_units_exact_count_for_fixtures():
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025", "2024"), matchweeks_per_season=5)
    units = enumerate_static_units(spec)
    assert len(units) == 10  # 2 seasons x 5 matchweeks
    assert {u.season for u in units} == {"2025", "2024"}
    assert units[0].fetch_kwargs["matchweek"] == 1


def test_enumerate_static_units_skips_dependent_capabilities():
    spec = BackfillSpec(
        provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK, MATCH_LINEUPS_MATCH),
        seasons=("2025",), matchweeks_per_season=3,
    )
    units = enumerate_static_units(spec)
    assert len(units) == 3  # only the fixtures units — lineups is "dependent"
    assert all(u.capability == MATCH_FIXTURES_MATCHWEEK for u in units)


def test_gameweek_stats_units_use_effective_gameweeks_per_season():
    spec = BackfillSpec(
        provider_id="vaastav_archive", capabilities=(PLAYER_GAMEWEEK_STATS_GAMEWEEK,),
        seasons=("2025-26",), matchweeks_per_season=38, gameweeks_per_season=4,
    )
    units = enumerate_static_units(spec)
    assert len(units) == 4


def test_player_identity_units_one_per_season_no_gameweek_param():
    spec = BackfillSpec(provider_id="vaastav_archive", capabilities=(PLAYER_IDENTITY_SEASON,), seasons=("2016-17", "2025-26"))
    units = enumerate_static_units(spec)
    assert len(units) == 2
    assert all(u.fetch_kwargs == {"season": u.season} for u in units)


# -- team.identity@season GrainPlan — E2b story 10b, bundled fix ------------
#
# Before this fix, TEAM_IDENTITY_SEASON had no entry in GRAIN_PLANS at all
# (logged as an open bug: PROGRESS.md E2, docs/HANDOFF.md §3) — every one
# of these three tests would have raised (the first two via BackfillSpec.
# __post_init__'s "unknown capability" check, the third via a bare
# KeyError on GRAIN_PLANS[TEAM_IDENTITY_SEASON]) before the fix. Confirmed
# live: reverting the GRAIN_PLANS entry and re-running this file reproduces
# exactly that failure (see this story's report for what was actually
# broken and restored).


def test_team_identity_season_has_a_static_grain_plan():
    assert TEAM_IDENTITY_SEASON in GRAIN_PLANS
    plan = GRAIN_PLANS[TEAM_IDENTITY_SEASON]
    assert plan.kind == "static"
    assert plan.dataset == "vaastav_team_identity"


def test_team_identity_units_one_per_season_no_gameweek_param():
    spec = BackfillSpec(provider_id="vaastav_archive", capabilities=(TEAM_IDENTITY_SEASON,), seasons=("2019-20", "2025-26"))
    units = enumerate_static_units(spec)
    assert len(units) == 2
    assert all(u.fetch_kwargs == {"season": u.season} for u in units)
    assert all(u.dataset == "vaastav_team_identity" for u in units)


# -- dependent estimation ---------------------------------------------------------


def test_estimate_dependent_counts_matches_2015_seasons_matchweeks_matches_formula():
    spec = BackfillSpec(
        provider_id="pl_api", capabilities=(MATCH_LINEUPS_MATCH,), seasons=("2025", "2024"),
        matchweeks_per_season=38, matches_per_matchweek=10,
    )
    counts = estimate_dependent_counts(spec)
    assert counts[MATCH_LINEUPS_MATCH] == 2 * 38 * 10


def test_estimate_player_season_stats_uses_players_per_season_estimate():
    spec = BackfillSpec(provider_id="pl_api", capabilities=(PLAYER_SEASON_STATS_SEASON,), seasons=("2025",), players_per_season_estimate=123)
    counts = estimate_dependent_counts(spec)
    assert counts[PLAYER_SEASON_STATS_SEASON] == 123


# -- dry run ------------------------------------------------------------------------


def test_dry_run_ten_season_lineups_only_matches_the_briefs_headline_figure():
    """10 seasons x 38 matchweeks x 10 matches = 3,800 — the story brief's
    quoted '~3,800 PL API requests' for a 10-season backfill. Confirms the
    enumeration model is calibrated the same way the brief's figure was."""
    seasons = tuple(str(y) for y in range(2015, 2025))
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_LINEUPS_MATCH,), seasons=seasons)
    report = dry_run(spec, RatePolicy(requests_per_second=2.0))
    assert report.n_requests_total == 3800


def test_dry_run_rate_policy_wall_clock():
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=10)
    report = dry_run(spec, RatePolicy(requests_per_second=2.0))
    assert report.n_requests_total == 10
    assert report.wall_clock_seconds == pytest.approx(5.0)


def test_dry_run_bulk_file_policy_has_no_enforced_ceiling_but_still_gives_a_rough_total():
    spec = BackfillSpec(provider_id="vaastav_archive", capabilities=(PLAYER_GAMEWEEK_STATS_GAMEWEEK,), seasons=("2025-26",), gameweeks_per_season=5)
    report = dry_run(spec, BulkFilePolicy())
    assert report.n_requests_total == 5
    assert report.wall_clock_seconds is not None
    assert "NO rate ceiling" in report.wall_clock_note


def test_dry_run_daily_quota_policy_reports_days_needed():
    spec = BackfillSpec(provider_id="x", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=250)
    report = dry_run(spec, DailyQuotaPolicy(requests_per_day=100))
    assert report.n_requests_total == 250
    assert "3 day" in report.wall_clock_note  # ceil(250/100) == 3


def test_dry_run_credit_policy_gives_no_wall_clock_number():
    spec = BackfillSpec(provider_id="x", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=2)
    report = dry_run(spec, CreditPolicy(monthly_credits=500, cost_per_call_description="regions x markets"))
    assert report.wall_clock_seconds is None
    assert "credit" in report.wall_clock_note.lower()


def test_dry_run_render_is_a_nonempty_string_and_lists_every_capability():
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK, MATCH_LINEUPS_MATCH), seasons=("2025",), matchweeks_per_season=3)
    text = dry_run(spec, RatePolicy(requests_per_second=2.0)).render()
    assert "match.fixtures@matchweek" in text
    assert "match.lineups@match" in text
    assert "TOTAL" in text


# -- checkpoint store -----------------------------------------------------------


def test_checkpoint_round_trips(tmp_path):
    cp = CheckpointStore(tmp_path / "ckpt.jsonl")
    outcome = UnitOutcome(unit_key="k1", capability="c", season="2025", status="done", reason=None, content_hash="h", n_rows=5, observed_at="t")
    cp.record(outcome)
    resolved = cp.load_resolved()
    assert resolved == {"k1": outcome}


def test_checkpoint_is_append_only_never_rewritten(tmp_path):
    path = tmp_path / "ckpt.jsonl"
    cp = CheckpointStore(path)
    cp.record(UnitOutcome("k1", "c", "2025", "done", None, "h", 1, "t"))
    size_after_first = path.stat().st_size
    cp.record(UnitOutcome("k2", "c", "2025", "done", None, "h", 1, "t"))
    text = path.read_text(encoding="utf-8")
    assert text.count("\n") == 2
    assert path.stat().st_size > size_after_first


def test_checkpoint_last_write_wins_on_a_duplicate_key(tmp_path):
    cp = CheckpointStore(tmp_path / "ckpt.jsonl")
    cp.record(UnitOutcome("k1", "c", "2025", "absent", "first", None, None, "t1"))
    cp.record(UnitOutcome("k1", "c", "2025", "done", None, "h", 5, "t2"))
    resolved = cp.load_resolved()
    assert resolved["k1"].status == "done"


def test_checkpoint_load_resolved_on_missing_file_is_empty(tmp_path):
    cp = CheckpointStore(tmp_path / "does_not_exist.jsonl")
    assert cp.load_resolved() == {}


# -- valid_at conventions -----------------------------------------------------------


def test_season_start_date():
    assert _season_start_date("2025-26") == datetime(2025, 8, 1, tzinfo=UTC)


def test_valid_at_for_fixtures_uses_min_kickoff():
    rows = pl.DataFrame({"kickoff": [datetime(2025, 8, 20), datetime(2025, 8, 18)]})
    unit = WorkUnit.make(MATCH_FIXTURES_MATCHWEEK, "pl_api", "2025", season="2025", matchweek=1)
    assert _valid_at_for(unit, rows) == datetime(2025, 8, 18, tzinfo=UTC)


def test_valid_at_for_match_grain_reads_kickoff_from_meta_not_rows():
    unit = WorkUnit.make(MATCH_LINEUPS_MATCH, "pl_api", "2025", match_id="7", meta={"kickoff": "2025-08-16T15:00:00"})
    assert _valid_at_for(unit, pl.DataFrame({"x": [1]})) == datetime(2025, 8, 16, 15, 0, tzinfo=UTC)


def test_valid_at_for_match_grain_raises_without_kickoff_meta():
    unit = WorkUnit.make(MATCH_LINEUPS_MATCH, "pl_api", "2025", match_id="7")
    with pytest.raises(BackfillError):
        _valid_at_for(unit, pl.DataFrame({"x": [1]}))


def test_valid_at_for_gameweek_stats_uses_min_kickoff_time():
    rows = pl.DataFrame({"kickoff_time": ["2026-08-21T19:00:00Z", "2026-08-20T15:00:00Z"]})
    unit = WorkUnit.make(PLAYER_GAMEWEEK_STATS_GAMEWEEK, "vaastav_archive", "2025-26", season="2025-26", gameweek=1)
    assert _valid_at_for(unit, rows) == datetime(2026, 8, 20, 15, 0, tzinfo=UTC)


def test_valid_at_for_player_identity_season_uses_season_start():
    unit = WorkUnit.make(PLAYER_IDENTITY_SEASON, "vaastav_archive", "2016-17", season="2016-17")
    assert _valid_at_for(unit, pl.DataFrame({"x": [1]})) == datetime(2016, 8, 1, tzinfo=UTC)


def test_valid_at_for_team_identity_season_uses_season_start():
    # Bundled fix, E2b story 10b — same convention as player identity;
    # without this branch, run() would raise BackfillError the moment it
    # tried to write a resolved team.identity@season unit to the store.
    unit = WorkUnit.make(TEAM_IDENTITY_SEASON, "vaastav_archive", "2019-20", season="2019-20")
    assert _valid_at_for(unit, pl.DataFrame({"x": [1]})) == datetime(2019, 8, 1, tzinfo=UTC)


def test_run_resolves_team_identity_season_units_end_to_end(tmp_path):
    # End-to-end proof of the bundled fix: before it, this whole test would
    # have raised BackfillError at BackfillSpec construction — team.
    # identity@season could not be enumerated by the orchestrator at all.
    spec = BackfillSpec(provider_id="vaastav_archive", capabilities=(TEAM_IDENTITY_SEASON,), seasons=("2019-20",))
    teams_df = pl.DataFrame({"season": ["2019-20"], "id": [1], "code": [3], "name": ["Arsenal"], "short_name": ["ARS"]})
    provider = FakeProvider({
        (TEAM_IDENTITY_SEASON, (("season", "2019-20"),)): _fr(TEAM_IDENTITY_SEASON, teams_df),
    })
    store = BitemporalStore(base_path=tmp_path / "store")
    checkpoint = CheckpointStore(tmp_path / "ckpt.jsonl")
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=checkpoint, store=store)

    summary = orch.run()

    assert summary.n_done == 1
    assert summary.n_absent == 0
    assert not summary.stopped_early
    written = store.latest("vaastav_team_identity")
    assert written.height == 1
    assert written["code"][0] == 3


# -- orchestrator: static-only, happy path ---------------------------------------


def test_run_static_only_writes_store_and_checkpoints_every_unit(tmp_path):
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=2)
    provider = FakeProvider({
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 1, ["m1"])),
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 2), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 2, ["m2"])),
    })
    store = BitemporalStore(base_path=tmp_path / "store")
    checkpoint = CheckpointStore(tmp_path / "ckpt.jsonl")
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=checkpoint, store=store)

    summary = orch.run()

    assert summary.n_done == 2
    assert summary.n_absent == 0
    assert summary.n_already_resolved_on_entry == 0
    assert not summary.stopped_early
    written = store.latest("pl_match_fixtures")
    assert set(written["match_id"].to_list()) == {"m1", "m2"}
    assert len(checkpoint.load_resolved()) == 2


def test_run_stores_valid_at_anchored_at_earliest_kickoff_not_now(tmp_path):
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=1)
    rows = _fixtures_df("2025", 1, ["m1", "m2"])
    provider = FakeProvider({(MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, rows)})
    store = BitemporalStore(base_path=tmp_path / "store")
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=CheckpointStore(tmp_path / "ckpt.jsonl"), store=store)
    orch.run()
    written = store.observations("pl_match_fixtures", until=datetime.now(UTC))
    # NOTE: store.py types every timestamp column (naive-in-storage included)
    # as pl.Datetime("us", "UTC") on read. Materialising one to a Python
    # object needs the IANA tzdata this dev machine doesn't have installed
    # (a real, separately-flagged finding — see fplai.backfill's
    # _strip_tz_for_safe_row_iteration docstring) — so this assertion stays
    # entirely inside polars rather than calling .to_list()/.item().
    assert written.select(pl.col("valid_at").dt.replace_time_zone(None).n_unique()).item() == 1
    assert written.select((pl.col("valid_at").dt.replace_time_zone(None) == datetime(2025, 8, 16, 15, 0)).all()).item()


def test_run_persists_the_providers_own_observed_at_not_the_run_time(tmp_path):
    # Coordinator-directed correction, E2b story 10b (2026-08-21): handle()
    # previously discarded result.observed_at and stamped datetime.now()
    # at write time instead -- silently defeating any provider (e.g.
    # providers/olbauday.py's corrected imputation) that computes a real,
    # non-now() observed_at. observed_at is the PROVIDER's own provenance
    # (when IT learned the fact) -- the orchestrator has no standing to
    # overwrite it (blueprint §12.3, §3.2). A distinct, recognisable
    # HISTORICAL instant -- nowhere near "now" -- proves the stored value
    # is the provider's, not the run's own timestamp.
    distinct_historical_instant = datetime(2019, 3, 14, 9, 26, tzinfo=UTC)
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=1)
    rows = _fixtures_df("2025", 1, ["m1"])
    provider = FakeProvider({
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): _fr(
            MATCH_FIXTURES_MATCHWEEK, rows, observed_at=distinct_historical_instant,
        ),
    })
    store = BitemporalStore(base_path=tmp_path / "store")
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=CheckpointStore(tmp_path / "ckpt.jsonl"), store=store)

    orch.run()

    written = store.observations("pl_match_fixtures", until=datetime.now(UTC))
    # Same tzdata-materialisation caution as the valid_at test just above.
    assert written.select(pl.col("observed_at").dt.replace_time_zone(None).n_unique()).item() == 1
    assert written.select((pl.col("observed_at").dt.replace_time_zone(None) == datetime(2019, 3, 14, 9, 26)).all()).item()


# -- orchestrator: resumability -----------------------------------------------------


def test_run_skips_already_checkpointed_units_never_refetches_them(tmp_path):
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=3)
    checkpoint_path = tmp_path / "ckpt.jsonl"
    store = BitemporalStore(base_path=tmp_path / "store")

    # First run: only answer matchweeks 1-2 (simulate an interruption via max_units).
    provider1 = FakeProvider({
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 1, ["m1"])),
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 2), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 2, ["m2"])),
    })
    orch1 = BackfillOrchestrator(spec, lambda season: provider1, checkpoint=CheckpointStore(checkpoint_path), store=store)
    summary1 = orch1.run(max_units=2)
    assert summary1.stopped_early
    assert summary1.n_done == 2
    assert len(provider1.calls) == 2

    # Second run, FRESH provider that would AssertionError if asked for
    # matchweeks 1 or 2 again — only matchweek 3 is in its handler map.
    provider2 = FakeProvider({
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 3), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 3, ["m3"])),
    })
    orch2 = BackfillOrchestrator(spec, lambda season: provider2, checkpoint=CheckpointStore(checkpoint_path), store=store)
    summary2 = orch2.run()

    assert not summary2.stopped_early
    assert summary2.n_already_resolved_on_entry == 2
    assert summary2.n_done == 3  # cumulative total (2 from the first run + 1 new) — see RunSummary's docstring
    assert provider2.calls == [(MATCH_FIXTURES_MATCHWEEK, (("matchweek", 3), ("season", "2025")))]  # ONLY the new one
    all_resolved = CheckpointStore(checkpoint_path).load_resolved()
    assert len(all_resolved) == 3
    written = store.latest("pl_match_fixtures")
    assert set(written["match_id"].to_list()) == {"m1", "m2", "m3"}


# -- orchestrator: the two failure paths (module docstring §2) ----------------------


def test_identity_error_halts_the_run_and_checkpoints_nothing_for_that_unit(tmp_path):
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=2)
    checkpoint_path = tmp_path / "ckpt.jsonl"
    provider = FakeProvider({
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 1, ["m1"])),
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 2), ("season", "2025"))): IdentityError("wrong snapshot for this season"),
    })
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=CheckpointStore(checkpoint_path), store=BitemporalStore(base_path=tmp_path / "store"))

    with pytest.raises(BackfillHalted) as excinfo:
        orch.run()
    assert excinfo.value.reason == "identity"

    resolved = CheckpointStore(checkpoint_path).load_resolved()
    assert len(resolved) == 1  # matchweek 1 only — nothing recorded for the failing unit
    assert all(o.status == "done" for o in resolved.values())


def test_provider_error_is_absence_recorded_and_the_run_continues(tmp_path):
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=2)
    provider = FakeProvider({
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): ProviderError("404 — no matches for this matchweek"),
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 2), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 2, ["m2"])),
    })
    checkpoint = CheckpointStore(tmp_path / "ckpt.jsonl")
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=checkpoint, store=BitemporalStore(base_path=tmp_path / "store"))

    summary = orch.run()  # must NOT raise

    assert summary.n_done == 1
    assert summary.n_absent == 1
    resolved = checkpoint.load_resolved()
    absent = [o for o in resolved.values() if o.status == "absent"][0]
    assert "no matches" in absent.reason


def test_transport_error_halts_the_run(tmp_path):
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=1)
    provider = FakeProvider({
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): TransportError("retries exhausted, last status 429"),
    })
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=CheckpointStore(tmp_path / "ckpt.jsonl"), store=BitemporalStore(base_path=tmp_path / "store"))
    with pytest.raises(BackfillHalted) as excinfo:
        orch.run()
    assert excinfo.value.reason == "transport"


# -- orchestrator: dependent (match-grain) expansion -----------------------------------


def test_dependent_match_grain_expands_from_real_fixtures_in_the_store(tmp_path):
    spec = BackfillSpec(
        provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK, MATCH_LINEUPS_MATCH),
        seasons=("2025",), matchweeks_per_season=1,
    )
    provider = FakeProvider({
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 1, ["m1", "m2"])),
        (MATCH_LINEUPS_MATCH, (("match_id", "m1"),)): _fr(MATCH_LINEUPS_MATCH, _lineup_df("m1")),
        (MATCH_LINEUPS_MATCH, (("match_id", "m2"),)): _fr(MATCH_LINEUPS_MATCH, _lineup_df("m2")),
    })
    store = BitemporalStore(base_path=tmp_path / "store")
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=CheckpointStore(tmp_path / "ckpt.jsonl"), store=store)

    summary = orch.run()

    assert summary.n_done == 3  # 1 fixtures unit + 2 real match_id lineup units (NOT matches_per_matchweek's estimate of 10)
    written = store.latest("pl_match_lineups")
    assert set(written["match_id"].to_list()) == {"m1", "m2"}


def test_dependent_capability_without_fixtures_in_spec_raises_backfill_error(tmp_path):
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_LINEUPS_MATCH,), seasons=("2025",), matchweeks_per_season=1)
    orch = BackfillOrchestrator(spec, lambda season: FakeProvider({}), checkpoint=CheckpointStore(tmp_path / "ckpt.jsonl"), store=BitemporalStore(base_path=tmp_path / "store"))
    with pytest.raises(BackfillError, match="match.fixtures@matchweek"):
        orch.run()


def test_dependent_capability_without_store_raises_backfill_error(tmp_path):
    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK, MATCH_LINEUPS_MATCH), seasons=("2025",), matchweeks_per_season=1)
    provider = FakeProvider({(MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 1, ["m1"]))})
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=CheckpointStore(tmp_path / "ckpt.jsonl"), store=None)
    with pytest.raises(BackfillError, match="needs a `store`"):
        orch.run()


def test_non_executable_dependent_capability_refuses_to_run(tmp_path):
    spec = BackfillSpec(
        provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK, PLAYER_SEASON_STATS_SEASON),
        seasons=("2025",), matchweeks_per_season=1,
    )
    provider = FakeProvider({(MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 1, ["m1"]))})
    store = BitemporalStore(base_path=tmp_path / "store")
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=CheckpointStore(tmp_path / "ckpt.jsonl"), store=store)
    with pytest.raises(BackfillError, match="no wired execution path"):
        orch.run()
    assert GRAIN_PLANS[PLAYER_SEASON_STATS_SEASON].executable is False


def test_no_fixtures_on_file_for_a_matchweek_skips_expansion_without_crashing(tmp_path):
    """A matchweek whose fixtures fetch came back ABSENT (e.g. season ended
    before matchweek 2 existed) must not crash the dependent phase — there
    is simply nothing to expand for it."""
    spec = BackfillSpec(
        provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK, MATCH_LINEUPS_MATCH),
        seasons=("2025",), matchweeks_per_season=2,
    )
    provider = FakeProvider({
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 1), ("season", "2025"))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df("2025", 1, ["m1"])),
        (MATCH_FIXTURES_MATCHWEEK, (("matchweek", 2), ("season", "2025"))): ProviderError("season ended before matchweek 2"),
        (MATCH_LINEUPS_MATCH, (("match_id", "m1"),)): _fr(MATCH_LINEUPS_MATCH, _lineup_df("m1")),
    })
    store = BitemporalStore(base_path=tmp_path / "store")
    orch = BackfillOrchestrator(spec, lambda season: provider, checkpoint=CheckpointStore(tmp_path / "ckpt.jsonl"), store=store)
    summary = orch.run()
    assert summary.n_absent == 1  # matchweek 2's fixtures
    assert summary.n_done == 2  # matchweek 1's fixtures + its one lineup unit


# -- provider caching per season ------------------------------------------------------


def test_provider_factory_is_memoised_per_season(tmp_path):
    calls = []

    def factory(season):
        calls.append(season)
        return FakeProvider({
            (MATCH_FIXTURES_MATCHWEEK, (("matchweek", mw), ("season", season))): _fr(MATCH_FIXTURES_MATCHWEEK, _fixtures_df(season, mw, [f"m{mw}"]))
            for mw in (1, 2, 3)
        })

    spec = BackfillSpec(provider_id="pl_api", capabilities=(MATCH_FIXTURES_MATCHWEEK,), seasons=("2025",), matchweeks_per_season=3)
    orch = BackfillOrchestrator(spec, factory, checkpoint=CheckpointStore(tmp_path / "ckpt.jsonl"), store=BitemporalStore(base_path=tmp_path / "store"))
    orch.run()
    assert calls == ["2025"]  # not called once per matchweek


# --- match.officials@match GrainPlan + lazy player identity (session s004) ---


def test_match_officials_has_a_grain_plan_so_the_orchestrator_can_enumerate_it():
    # The capability shipped built, tested and live-verified but with NO
    # GrainPlan, so `run()` refused it and ZERO rows reached the store — the
    # capability existed and its data did not. GRAIN_PLANS lives here in
    # fplai.backfill, not in fplai.registry.
    from fplai.schemas import MATCH_OFFICIALS_MATCH

    assert MATCH_OFFICIALS_MATCH in GRAIN_PLANS
    plan = GRAIN_PLANS[MATCH_OFFICIALS_MATCH]
    assert plan.dataset == "pl_match_officials"
    assert plan.kind == "dependent"
    assert plan.parent_capability == MATCH_FIXTURES_MATCHWEEK
    assert plan.executable is True


def test_lazy_player_identity_map_is_not_built_until_a_player_is_resolved():
    # Found live 2026-08-28: vaastav's archive has NO opta_code column at all
    # for 2019-20..2023-24 (verified in the real store — 666/713/737/778/865
    # rows, 100% null), so PlayerIdentityMap.build() raises for the whole
    # season, correctly refusing to guess. But the PL factory built it
    # EAGERLY per season regardless of which capabilities were requested, so
    # capabilities needing no player identity at all — fixtures, team stats,
    # and officials, whose payload carries only names — were blocked by a map
    # nothing had asked for. That is what limited the first real PL ingest to
    # a single season.
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("backfill_cli", root / "scripts" / "backfill.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    built = []

    def build():
        built.append(1)
        raise RuntimeError("no opta_code for this season")

    lazy = cli._LazyPlayerIdentityMap(build)
    assert built == [], "constructing the proxy must not build the map"

    # …and the failure is DEFERRED, not suppressed: a capability that really
    # does resolve a player still gets the same loud error it always would.
    with pytest.raises(RuntimeError, match="no opta_code"):
        lazy.resolve(1)
    assert built == [1]

