"""Tests for `scripts/run_e6_gate.py` -- the E6 gate harness (session
`s006`, task `e6-gate-runner`).

Fast unit tests against synthetic/fabricated inputs (resumability
bookkeeping, the DC-neutralisation derivation, season truncation,
scoring-config zeroing). The one test that touches the real store
(fitting a real `DCModelParams`) is marked `@pytest.mark.slow` per this
task's brief.

`scripts/` is not a package -- imported by path, the same pattern
`tests/test_check_heartbeat.py` and `tests/test_source_integrity.py`
already use.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_SCRIPT_PATH = _REPO_ROOT / "scripts" / "run_e6_gate.py"
_spec = importlib.util.spec_from_file_location("run_e6_gate", _SCRIPT_PATH)
run_e6_gate = importlib.util.module_from_spec(_spec)
sys.modules["run_e6_gate"] = run_e6_gate
_spec.loader.exec_module(run_e6_gate)

from fplai.backtest.data import SeasonCoverage, SeasonData  # noqa: E402
from fplai.scoring import ScoringConfig  # noqa: E402


def _season_data(frame: pl.DataFrame, *, season: str = "2099-00", rounds_present: tuple[int, ...] = (1, 2, 3)) -> SeasonData:
    coverage = SeasonCoverage(
        season=season, rounds_present=rounds_present, expected_gameweeks=38, missing_rounds=()
    )
    return SeasonData(season=season, frame=frame, coverage=coverage)


def _scoring_config(**dc: int) -> ScoringConfig:
    now = datetime.now(timezone.utc)
    return ScoringConfig(
        valid_as_of=now,
        observed_at=now,
        goals_scored={"GK": 6, "DEF": 6, "MID": 5, "FWD": 4},
        clean_sheets={"GK": 4, "DEF": 4, "MID": 1, "FWD": 0},
        goals_conceded={"GK": -1, "DEF": -1, "MID": 0, "FWD": 0},
        defensive_contribution=dc or {"GK": 0, "DEF": 2, "MID": 2, "FWD": 2},
        long_play=2,
        short_play=1,
        saves=1,
        assists=3,
        bonus=1,
        yellow_cards=-1,
        red_cards=-3,
        own_goals=-2,
        penalties_saved=5,
        penalties_missed=-2,
    )


# ---------------------------------------------------------------------------
# _needs_dc_neutralisation -- derived from the data, never a season list
# (pinned decision 3, explicit).
# ---------------------------------------------------------------------------


def test_needs_dc_neutralisation_true_when_column_absent():
    frame = pl.DataFrame({"round": [1, 2], "element": [10, 11]})
    assert run_e6_gate._needs_dc_neutralisation(_season_data(frame)) is True


def test_needs_dc_neutralisation_true_when_column_wholly_null():
    frame = pl.DataFrame({"round": [1, 2], "defensive_contribution": [None, None]}, schema={"round": pl.Int64, "defensive_contribution": pl.Int64})
    assert run_e6_gate._needs_dc_neutralisation(_season_data(frame)) is True


def test_needs_dc_neutralisation_false_when_any_non_null_present():
    frame = pl.DataFrame({"round": [1, 2], "defensive_contribution": [None, 3]})
    assert run_e6_gate._needs_dc_neutralisation(_season_data(frame)) is False


# ---------------------------------------------------------------------------
# _neutralise_dc -- zeroes every position via dataclasses.replace, and
# leaves the rest of the config (and the original object) untouched.
# ---------------------------------------------------------------------------


def test_neutralise_dc_zeroes_every_position():
    cfg = _scoring_config(GK=0, DEF=2, MID=2, FWD=2)
    zeroed = run_e6_gate._neutralise_dc(cfg)
    assert zeroed.defensive_contribution == {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0}
    # Original untouched -- dataclasses.replace never mutates in place.
    assert cfg.defensive_contribution == {"GK": 0, "DEF": 2, "MID": 2, "FWD": 2}
    # Every other field carried through unchanged.
    assert zeroed.assists == cfg.assists
    assert zeroed.saves == cfg.saves


def test_neutralise_dc_is_a_real_dataclasses_replace_not_a_new_object_type():
    cfg = _scoring_config()
    zeroed = run_e6_gate._neutralise_dc(cfg)
    assert isinstance(zeroed, ScoringConfig)
    assert zeroed is not cfg


# ---------------------------------------------------------------------------
# _truncate_season -- smoke-test aid, keeps the EARLIEST N rounds and never
# changes the leakage boundary for any round that survives.
# ---------------------------------------------------------------------------


def test_truncate_season_keeps_earliest_n_rounds():
    frame = pl.DataFrame({"round": [1, 1, 2, 2, 3, 3], "element": [1, 2, 1, 2, 1, 2]})
    data = _season_data(frame, rounds_present=(1, 2, 3))
    truncated = run_e6_gate._truncate_season(data, 2)
    assert truncated.coverage.rounds_present == (1, 2)
    assert sorted(truncated.frame["round"].unique().to_list()) == [1, 2]


def test_truncate_season_none_returns_the_same_object():
    frame = pl.DataFrame({"round": [1, 2], "element": [1, 2]})
    data = _season_data(frame, rounds_present=(1, 2))
    assert run_e6_gate._truncate_season(data, None) is data


def test_truncate_season_does_not_alter_history_for_a_surviving_round():
    """The leakage guarantee this helper claims: round 2's own history
    (round < 2) is identical whether round 3 was ever loaded or not."""
    frame = pl.DataFrame({"round": [1, 2, 3], "element": [1, 1, 1], "total_points": [5, 6, 7]})
    full = _season_data(frame, rounds_present=(1, 2, 3))
    truncated = run_e6_gate._truncate_season(full, 2)
    history_full = full.frame.filter(pl.col("round") < 2)
    history_truncated = truncated.frame.filter(pl.col("round") < 2)
    assert history_full.equals(history_truncated)


# ---------------------------------------------------------------------------
# Resumability -- attacked, not assumed (gate item 2 mirrors this at the
# CLI level; this is the unit-level guarantee behind it).
# ---------------------------------------------------------------------------


def test_load_completed_seasons_empty_when_file_absent(tmp_path: Path):
    assert run_e6_gate._load_completed_seasons(tmp_path / "does_not_exist.jsonl") == set()


def test_load_completed_seasons_reads_back_written_seasons(tmp_path: Path):
    out = tmp_path / "results.jsonl"
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"season": "2022-23", "x": 1}) + "\n")
        f.write(json.dumps({"season": "2023-24", "x": 2}) + "\n")
    assert run_e6_gate._load_completed_seasons(out) == {"2022-23", "2023-24"}


def test_load_completed_seasons_skips_malformed_lines_without_raising(tmp_path: Path):
    out = tmp_path / "results.jsonl"
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"season": "2022-23"}) + "\n")
        f.write("{not valid json\n")
        f.write("\n")  # blank line
        f.write(json.dumps({"season": "2023-24"}) + "\n")
    assert run_e6_gate._load_completed_seasons(out) == {"2022-23", "2023-24"}


def test_load_completed_seasons_ignores_lines_with_no_season_key(tmp_path: Path):
    out = tmp_path / "results.jsonl"
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"not_season": "2022-23"}) + "\n")
    assert run_e6_gate._load_completed_seasons(out) == set()


# ---------------------------------------------------------------------------
# Real-store integration -- slow. Fits the real shared DC bundle
# (`_fit_shared_dc_bundle`) exactly the way `run_season` does for a
# DC-neutralised season, and checks it against the real store's own
# measured column-nullness pattern this task's docstring cites.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_fit_shared_dc_bundle_against_the_real_store():
    from fplai.store import BitemporalStore

    store = BitemporalStore()
    threshold_set, dc_params = run_e6_gate._fit_shared_dc_bundle(store)
    assert dc_params.groups, "expected at least one fitted DC group from the real store's 2025-26 data"
    assert "2025-26" in dc_params.seasons_used


@pytest.mark.slow
def test_needs_dc_neutralisation_against_the_real_store_seasons():
    from fplai.backtest.data import load_season
    from fplai.store import BitemporalStore

    store = BitemporalStore()
    # 2022-23 has zero non-null defensive_contribution rows in the real
    # store (verified live this task, see script module docstring).
    old_season = load_season(store, "2022-23")
    assert run_e6_gate._needs_dc_neutralisation(old_season) is True
    recent_season = load_season(store, "2025-26")
    assert run_e6_gate._needs_dc_neutralisation(recent_season) is False


# ---------------------------------------------------------------------------
# Coordinator correction (session s006): the cold-start fallback for a
# single in-season gameweek (2025-26 GW1) must NEVER reuse the
# whole-season shared DC bundle (fitted at as_of=now(), which has seen
# the whole of 2025-26 and 2026-27) while that season's ScoringConfig is
# still live -- that is leakage under CLAUDE.md rule 2, not the
# provably-inert substitution the whole-season case gets. These tests
# attack the fix, not merely assert it: the `get_shared_dc` passed in
# below raises if it is EVER called, so a regression back to the old
# behaviour fails loudly rather than silently passing.
# ---------------------------------------------------------------------------


def _forbidden_get_shared_dc():
    raise AssertionError(
        "get_shared_dc() must never be called for a non-neutralised season's cold-start "
        "gameweek -- that would be exactly the leakage the Architect flagged."
    )


@pytest.mark.slow
def test_cold_start_gameweek_dc_contributes_zero_end_to_end():
    """Gate item 1: attack, do not assert. Fits 2025-26 (NOT
    scoring-neutralised) for real, confirms gameweek 1 hits the
    cold-start branch, then calls the EXACT function `fplai.points.
    simulate_fixture_points_pmfs` uses per player
    (`predict_dc_pmf`) with the real params this gameweek's bundle
    carries and shows the result is the degenerate ineligible PMF for
    every DC-eligible position -- DC contributes zero for every
    simulated draw, by construction, not by chance."""
    from fplai.backtest.data import load_season
    from fplai.models.defensive_contribution import predict_dc_pmf
    from fplai.store import BitemporalStore

    store = BitemporalStore()
    season_data = load_season(store, "2025-26")
    truncated = run_e6_gate._truncate_season(season_data, 1)  # GW1 only -- keeps this fast
    params_by_gw, _fit_wall, cold_start = run_e6_gate._fit_params_by_gameweek(
        store, truncated, needs_dc_neutralisation=False, get_shared_dc=_forbidden_get_shared_dc
    )
    assert cold_start == [1], (
        "expected GW1 of 2025-26 to hit the cold-start fallback (it is DC's first-ever "
        "recorded gameweek in the store) -- if this fails, the store's DC data shape changed "
        "and this test's own premise needs re-checking, not silencing"
    )
    bundle = params_by_gw[1]
    assert bundle.dc_params.groups == {}
    assert bundle.dc_params.seasons_used == ()
    for position in ("DEF", "MID", "FWD"):
        assert bundle.assembly_params.threshold_set.is_eligible(position) is False
        pmf = predict_dc_pmf(
            bundle.dc_params,
            {},
            element=999999,
            fixture=1,
            position=position,
            minute_exposure=[(90.0, 1.0)],
        )
        assert pmf.eligible is False
        assert pmf.count_threshold is None
        assert pmf.points == 0
        assert pmf.counts == (0,)
        assert pmf.probabilities == (1.0,)


@pytest.mark.slow
def test_normal_2025_26_gameweek_still_gets_real_dc_params():
    """Gate item 2: the fix must not neutralise the other 37 gameweeks --
    gameweek 2 onward must still fit a REAL DCModelParams off that
    season's own settled history, unaffected by GW1's fallback."""
    from fplai.backtest.data import load_season
    from fplai.store import BitemporalStore

    store = BitemporalStore()
    season_data = load_season(store, "2025-26")
    truncated = run_e6_gate._truncate_season(season_data, 2)  # GW1-2 only -- keeps this fast
    params_by_gw, _fit_wall, cold_start = run_e6_gate._fit_params_by_gameweek(
        store, truncated, needs_dc_neutralisation=False, get_shared_dc=_forbidden_get_shared_dc
    )
    assert cold_start == [1]
    gw2 = params_by_gw[2]
    assert gw2.dc_params.groups, "GW2 should carry real fitted DC groups, not the degenerate stand-in"
    assert gw2.dc_params.seasons_used == ("2025-26",)
    assert gw2.assembly_params.threshold_set.is_eligible("DEF") is True
