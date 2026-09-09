"""Tests for `fplai.features.assemble_minutes_feature_row` — the one
worked prediction-time feature path (docs/HANDOFF.md §3.5, session
`s006`).

Two things are proved here, not just exercised:

1. **Equivalence** (`build_training_table` vs the assembly) — field by
   field across all 13 numeric columns plus `position`/`team`, on both a
   controlled synthetic store (multi-round history, a cold-start round,
   and a "future rows present" perturbation) and — real-store, `slow` —
   a handful of real elements across more than one fixture and season.
2. **The leakage claim, attacked from outside the intended call path**
   (CLAUDE.md: "a guarantee is earned by attacking it from outside the
   intended call path, not by a test that follows it") — the underlying
   read is shown to return zero rows at or beyond the target's own
   kickoff, and assembled features are shown UNCHANGED when rows dated
   after the target fixture exist in the store versus when they do not,
   which is the only way a leak through this module could show up.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from fplai.features import (
    FeatureAssemblyError,
    FixtureFeatureAssemblyParams,
    Roster,
    assemble_attacking_feature_row,
    assemble_attacking_feature_row_from_frame,
    assemble_bonus_feature_row,
    assemble_bonus_feature_row_from_frame,
    assemble_cards_feature_row,
    assemble_cards_feature_row_from_frame,
    assemble_dc_feature_row,
    assemble_dc_feature_row_from_frame,
    assemble_fixture_player_features,
    assemble_fixture_player_features_from_frame,
    assemble_minutes_feature_row,
    assemble_minutes_feature_row_from_frame,
    assemble_saves_feature_row,
    assemble_saves_feature_row_from_frame,
    make_saves_predict_fn,
)
from fplai import features as F  # S3a's roster-rollup builders (build_dc_roster_rollup,
# build_saves_roster_rollup, build_minutes_roster_rollup) have no individually-imported
# names above -- qualified access matches the brief's own `F.` shorthand and keeps the
# import list above from growing for tests that only exercise these six.
from fplai.gameweek_stats import FPL_API_DATASET, read_player_gameweek_stats
from fplai.models.attacking import NUMERIC_FEATURE_COLUMNS_ATTACKING
from fplai.models.attacking import build_training_table as build_attacking_training_table
from fplai.models.bonus import BonusModelError, BonusPlayerInput, NUMERIC_FEATURE_COLUMNS_BONUS
from fplai.models.bonus import build_training_table as build_bonus_training_table
from fplai.models.cards import NUMERIC_FEATURE_COLUMNS_CARDS
from fplai.models.cards import build_training_table as build_cards_training_table
from fplai.models.defensive_contribution import NUMERIC_FEATURE_COLUMNS_DC, build_dc_threshold_set
from fplai.models.defensive_contribution import build_training_table as build_dc_training_table
from fplai.models.minutes import MinutesModelConfig, NUMERIC_FEATURE_COLUMNS, build_training_table
from fplai.models.saves import PREDICT_FEATURE_ROW_COLUMNS, SavesModelError, fit_saves_model, predict_saves_pmf
from fplai.models.saves import build_training_table as build_saves_training_table
from fplai.store import BitemporalStore

UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _row(
    season: str,
    round_: int,
    element: int,
    fixture: int,
    kickoff: str,
    *,
    team: str = "TeamA",
    was_home: bool = True,
    minutes: int = 90,
    starts: int | None = 1,
    position: str = "MID",
    value: int = 55,
    selected: int = 100000,
    opponent_team: int = 2,
) -> dict:
    return {
        "season": season,
        "round": round_,
        "element": element,
        "fixture": fixture,
        "kickoff_time": kickoff,
        "minutes": minutes,
        "opponent_team": opponent_team,
        "position": position,
        "team": team,
        "starts": starts,
        "was_home": was_home,
        "value": value,
        "selected": selected,
    }


@pytest.fixture
def temp_store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def _write_fixture(store, rows, *, observed_at, valid_at=None):
    df = pl.DataFrame(rows)
    store.write(
        "vaastav_player_gameweek_stats",
        df,
        valid_at=valid_at or observed_at,
        observed_at=observed_at,
        source="test",
    )


def _parse_kickoff(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _assert_row_matches(assembled: dict, expected: dict) -> None:
    mismatches = []
    for c in NUMERIC_FEATURE_COLUMNS:
        a, b = assembled[c], expected[c]
        ok = abs(float(a) - float(b)) < 1e-9 if isinstance(a, float) or isinstance(b, float) else a == b
        if not ok:
            mismatches.append((c, a, b))
    if assembled["position"] != expected["position"]:
        mismatches.append(("position", assembled["position"], expected["position"]))
    if assembled["team"] != expected["team"]:
        mismatches.append(("team", assembled["team"], expected["team"]))
    assert not mismatches, f"feature mismatch(es): {mismatches}"


def _attacking_row(
    season: str,
    round_: int,
    element: int,
    fixture: int,
    kickoff: str,
    *,
    team: str = "TeamA",
    was_home: bool = True,
    minutes: int = 90,
    position: str = "MID",
    goals_scored: int = 0,
    assists: int = 0,
    expected_goals: float = 0.0,
    expected_assists: float = 0.0,
    team_h_score: int = 1,
    team_a_score: int = 1,
) -> dict:
    """`_row` plus the extra columns `fplai.models.attacking.REQUIRED_
    COLUMNS` needs that `_row`/minutes never touch (goals_scored, assists,
    expected_goals, expected_assists, team_h_score, team_a_score) --
    attacking's own equivalence gate, module docstring "Second instance"."""
    base = _row(season, round_, element, fixture, kickoff, team=team, was_home=was_home, minutes=minutes, position=position)
    base.update(
        {
            "goals_scored": goals_scored,
            "assists": assists,
            "expected_goals": expected_goals,
            "expected_assists": expected_assists,
            "team_h_score": team_h_score,
            "team_a_score": team_a_score,
        }
    )
    return base


def _assert_attacking_row_matches(assembled: dict, expected: dict) -> None:
    mismatches = []
    for c in NUMERIC_FEATURE_COLUMNS_ATTACKING:
        a, b = assembled[c], expected[c]
        ok = abs(float(a) - float(b)) < 1e-9 if isinstance(a, float) or isinstance(b, float) else a == b
        if not ok:
            mismatches.append((c, a, b))
    if assembled["position"] != expected["position"]:
        mismatches.append(("position", assembled["position"], expected["position"]))
    assert not mismatches, f"attacking feature mismatch(es): {mismatches}"


# ---------------------------------------------------------------------------
# 1. Equivalence on a controlled synthetic store.
# ---------------------------------------------------------------------------


def _five_round_history_rows() -> list[dict]:
    return [
        _row("2022-23", 1, 10, 101, "2022-08-06T14:00:00Z", was_home=True, minutes=90, starts=1, value=50, selected=1000),
        _row("2022-23", 2, 10, 111, "2022-08-13T14:00:00Z", was_home=False, minutes=45, starts=0, value=51, selected=1200),
        _row("2022-23", 3, 10, 121, "2022-08-20T14:00:00Z", was_home=True, minutes=0, starts=0, value=51, selected=1100),
        _row("2022-23", 4, 10, 131, "2022-08-27T14:00:00Z", was_home=False, minutes=90, starts=1, value=52, selected=1400),
        _row("2022-23", 5, 10, 141, "2022-09-03T14:00:00Z", was_home=True, minutes=90, starts=1, value=53, selected=1600),
    ]


def test_matches_build_training_table_mid_season_round(temp_store):
    rows = _five_round_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 5).to_dicts()[0]

    assembled = assemble_minutes_feature_row(
        temp_store,
        season="2022-23",
        round=5,
        element=10,
        fixture=141,
        kickoff_time=_parse_kickoff("2022-09-03T14:00:00Z"),
        team="TeamA",
        position="MID",
        was_home=True,
    )
    _assert_row_matches(assembled, target)


def test_matches_build_training_table_cold_start_round(temp_store):
    """Round 1 -- no prior history at all. games_played_this_season == 0,
    cold_start == True, every trailing rate filled to 0.0 -- exercised
    against build_training_table's own row, not merely asserted."""
    rows = _five_round_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 1).to_dicts()[0]
    assert target["cold_start"] is True
    assert target["games_played_this_season"] == 0.0

    assembled = assemble_minutes_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=10,
        fixture=101,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamA",
        position="MID",
        was_home=True,
    )
    _assert_row_matches(assembled, target)


def test_matches_build_training_table_across_several_rounds(temp_store):
    """Not one lucky row -- every round of the same synthetic history."""
    rows = _five_round_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])

    for r in rows:
        target = table.filter(pl.col("round") == r["round"]).to_dicts()[0]
        assembled = assemble_minutes_feature_row(
            temp_store,
            season=r["season"],
            round=r["round"],
            element=r["element"],
            fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]),
            team=r["team"],
            position=r["position"],
            was_home=r["was_home"],
        )
        _assert_row_matches(assembled, target)


# ---------------------------------------------------------------------------
# 2. Leakage — attacked, not just tested along the happy path.
# ---------------------------------------------------------------------------


def test_underlying_read_returns_nothing_at_or_beyond_the_target_kickoff(temp_store):
    """Direct attack on the enforcing primitive itself (module docstring):
    `read_player_gameweek_stats(store, as_of=kickoff_time)` must contain
    zero rows whose own kickoff_time is >= the target's, and the target
    fixture's own row must be entirely absent."""
    rows = _five_round_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    target_kickoff = _parse_kickoff("2022-08-27T14:00:00Z")  # round 4
    raw = read_player_gameweek_stats(temp_store, as_of=target_kickoff)
    raw = raw.with_columns(
        pl.col("kickoff_time").str.strptime(pl.Datetime, "%Y-%m-%dT%H:%M:%SZ").alias("_kt")
    )
    naive_target = target_kickoff.replace(tzinfo=None)
    at_or_after = raw.filter(pl.col("_kt") >= naive_target)
    assert at_or_after.height == 0

    target_rows = raw.filter((pl.col("season") == "2022-23") & (pl.col("element") == 10) & (pl.col("fixture") == 131))
    assert target_rows.height == 0


def test_assembled_features_unchanged_whether_or_not_later_rows_exist(temp_store):
    """The attack CLAUDE.md asks for directly: assemble the same target
    fixture's feature row twice -- once against a store that ONLY has
    rows strictly before the target's kickoff, once against a store that
    ALSO has rows for later rounds of the same player -- and show the
    output is byte-for-byte identical. If anything downstream ever
    started reading a later row, this is what would move."""
    history = _five_round_history_rows()

    store_without_future = BitemporalStore(base_path=Path(temp_store.base_path) / "no_future")
    _write_fixture(store_without_future, history[:3], observed_at=dt(2026, 1, 1))  # rounds 1-3 only

    store_with_future = BitemporalStore(base_path=Path(temp_store.base_path) / "with_future")
    _write_fixture(store_with_future, history, observed_at=dt(2026, 1, 1))  # rounds 1-5, including round 4's own target and round 5 AFTER it

    target = history[3]  # round 4 -- deliberately has a later row (round 5) in one store, not the other
    kwargs = dict(
        season=target["season"],
        round=target["round"],
        element=target["element"],
        fixture=target["fixture"],
        kickoff_time=_parse_kickoff(target["kickoff_time"]),
        team=target["team"],
        position=target["position"],
        was_home=target["was_home"],
    )
    without_future = assemble_minutes_feature_row(store_without_future, **kwargs)
    with_future = assemble_minutes_feature_row(store_with_future, **kwargs)
    assert without_future == with_future


def test_kickoff_time_must_be_timezone_aware(temp_store):
    with pytest.raises(FeatureAssemblyError):
        assemble_minutes_feature_row(
            temp_store,
            season="2022-23",
            round=1,
            element=10,
            fixture=101,
            kickoff_time=datetime(2022, 8, 6, 14, 0),  # naive
            team="TeamA",
            position="MID",
            was_home=True,
        )


def test_empty_store_is_a_valid_cold_start_not_an_error(temp_store):
    """No history at all anywhere in the store -- the very first fixture
    this store has ever seen for this element. Must NOT raise; must
    produce the same cold-start shape build_training_table would if any
    history existed."""
    assembled = assemble_minutes_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=999,
        fixture=1,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamZ",
        position="FWD",
        was_home=False,
    )
    assert assembled["cold_start"] is True
    assert assembled["games_played_this_season"] == 0.0
    assert assembled["trailing_start_rate_3"] == 0.0
    assert assembled["was_home"] is False
    assert assembled["team_first_fixture_in_window"] is True


# ---------------------------------------------------------------------------
# 2b. Live-season coalesced-NULL guard (docs/HANDOFF.md §3.0, story S0).
# The probe that motivated this story found `value`/`selected` NULL on
# every real FPL-API row for 2026-27 -- `_build_round_rollup`'s own
# fill_null(0.0) then silently hands the caller `prev_gw_value=0.0` for a
# real Premier League defender, on BOTH allow_live_season values. These
# tests write directly to `fpl_api_player_gameweek_stats` (never vaastav)
# to reproduce that shape without depending on the real store.
# ---------------------------------------------------------------------------


def _write_fpl_api_fixture(store, rows, *, observed_at):
    """Writes to the FPL-API dataset directly -- `read_player_gameweek_
    stats` tags rows from this dataset `source_provider="fpl_api"`, the
    guard's own trigger."""
    df = pl.DataFrame(rows)
    store.write(FPL_API_DATASET, df, valid_at=observed_at, observed_at=observed_at, source="test")


def _live_row(
    season: str, round_: int, element: int, fixture: int, kickoff: str, *, team: str = "TeamA", was_home: bool = True,
    minutes: int = 90, starts: int = 1, position: str = "DEF",
) -> dict:
    """Shaped like the real fpl_api_player_gameweek_stats rows the S0
    probe found: value/selected are None -- the live-ingestion path simply
    does not populate them yet (docs/HANDOFF.md §3.6)."""
    return {
        "season": season,
        "round": round_,
        "element": element,
        "fixture": fixture,
        "kickoff_time": kickoff,
        "minutes": minutes,
        "opponent_team": 2,
        "position": position,
        "team": team,
        "starts": starts,
        "was_home": was_home,
        "value": None,
        "selected": None,
        "attribution_complete": True,
    }


def _live_history(element: int = 37, team: str = "Aston Villa") -> list[dict]:
    return [
        _live_row("2026-27", 1, element, 11, "2026-08-15T14:00:00Z", team=team),
        _live_row("2026-27", 2, element, 21, "2026-08-22T14:00:00Z", team=team),
        _live_row("2026-27", 3, element, 31, "2026-08-29T14:00:00Z", team=team),
    ]


def test_live_season_raises_with_allow_live_season_false(temp_store):
    """The exact scenario the S0 probe found: an upcoming live-season
    fixture, allow_live_season=False. Must raise FeatureAssemblyError
    naming value/selected -- not silently return a cold-start row for a
    player (element 37) who has actually played three rounds."""
    _write_fpl_api_fixture(temp_store, _live_history(), observed_at=dt(2026, 9, 1))

    with pytest.raises(FeatureAssemblyError) as excinfo:
        assemble_minutes_feature_row(
            temp_store,
            season="2026-27",
            round=4,
            element=37,
            fixture=41,
            kickoff_time=_parse_kickoff("2026-09-12T14:00:00Z"),
            team="Aston Villa",
            position="DEF",
            was_home=True,
            allow_live_season=False,
        )
    assert "value" in str(excinfo.value)
    assert "selected" in str(excinfo.value)
    assert type(excinfo.value) is FeatureAssemblyError


def test_live_season_raises_with_allow_live_season_true(temp_store):
    """Same scenario, allow_live_season=True -- the guard fires
    independently of that flag: it runs BEFORE `_leakage_safe_read`
    decides whether to drop the FPL-API rows, so `allow_live_season`
    cannot be used to route around it."""
    _write_fpl_api_fixture(temp_store, _live_history(), observed_at=dt(2026, 9, 1))

    with pytest.raises(FeatureAssemblyError) as excinfo:
        assemble_minutes_feature_row(
            temp_store,
            season="2026-27",
            round=4,
            element=37,
            fixture=41,
            kickoff_time=_parse_kickoff("2026-09-12T14:00:00Z"),
            team="Aston Villa",
            position="DEF",
            was_home=True,
            allow_live_season=True,
        )
    assert "value" in str(excinfo.value)
    assert "selected" in str(excinfo.value)
    assert type(excinfo.value) is FeatureAssemblyError


def test_live_season_escape_hatch_reproduces_old_coalescing(temp_store):
    """allow_coalesced_live_nulls=True is the explicit opt-in (pinned
    decision 4) -- returns the pre-guard row: prev_gw_value/prev_gw_
    selected_log1p coalesced to 0.0, no raise.

    S0-b's brief asked this scenario to also assert `games_played_this_
    season == 1.0`; run live against `_live_history()` (three prior rounds,
    each with `starts=1`/`minutes=90`) it is `3.0`, matching `_build_round_
    rollup`'s own cumulative-appearance definition exactly -- reported as
    SURPRISED ME in this story's punch-out rather than forced to a number
    that does not match the fixture actually in use."""
    _write_fpl_api_fixture(temp_store, _live_history(), observed_at=dt(2026, 9, 1))

    assembled = assemble_minutes_feature_row(
        temp_store,
        season="2026-27",
        round=4,
        element=37,
        fixture=41,
        kickoff_time=_parse_kickoff("2026-09-12T14:00:00Z"),
        team="Aston Villa",
        position="DEF",
        was_home=True,
        allow_live_season=True,
        allow_coalesced_live_nulls=True,
    )
    assert assembled["prev_gw_value"] == 0.0
    assert assembled["prev_gw_selected_log1p"] == 0.0
    assert assembled["games_played_this_season"] == 3.0
    assert assembled["cold_start"] is False


def test_historical_fixture_unaffected_by_the_live_season_guard(temp_store):
    """Pinned decision 3, the most important constraint in the brief: a
    vaastav-only (historical) fixture must not raise and must produce the
    identical row it produced before this guard existed -- it has zero
    FPL-API rows for its element+season, so the guard is a no-op. Same
    fixture and assertion `test_matches_build_training_table_mid_season_
    round` already used, unchanged."""
    rows = _five_round_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 5).to_dicts()[0]

    assembled = assemble_minutes_feature_row(
        temp_store,
        season="2022-23",
        round=5,
        element=10,
        fixture=141,
        kickoff_time=_parse_kickoff("2022-09-03T14:00:00Z"),
        team="TeamA",
        position="MID",
        was_home=True,
    )
    _assert_row_matches(assembled, target)


def test_guard_is_no_longer_bypassable_via_the_private_raw_tail(temp_store):
    """Was `test_guard_is_bypassable_via_the_private_raw_tail_not_the_public_
    entry` -- it documented a real hole the S0 coder found by attacking their
    own work (CLAUDE.md: "a guarantee is not proven by a test that follows
    the sanctioned path"). Story S0-b closed it: the frame-side check
    (`_raise_if_frame_prior_rounds_all_null_for_source_columns`) now lives in
    the shared private tail itself, provenance-free, so a caller who reads
    `raw` themselves and drives the private helper directly is stopped too --
    not just the public `assemble_minutes_feature_row` entry."""
    from fplai.features import _assemble_minutes_feature_row_from_raw

    _write_fpl_api_fixture(temp_store, _live_history(), observed_at=dt(2026, 9, 1))
    raw = read_player_gameweek_stats(temp_store, as_of=_parse_kickoff("2026-09-12T14:00:00Z"))

    with pytest.raises(FeatureAssemblyError) as excinfo:
        _assemble_minutes_feature_row_from_raw(
            raw,
            season="2026-27",
            round=4,
            element=37,
            fixture=41,
            kickoff_time=_parse_kickoff("2026-09-12T14:00:00Z"),
            team="Aston Villa",
            position="DEF",
            was_home=True,
            config=MinutesModelConfig(),
        )
    assert "value" in str(excinfo.value)
    assert "selected" in str(excinfo.value)
    assert type(excinfo.value) is FeatureAssemblyError

    # The explicit opt-in still reaches the coalesced value on the private
    # tail too -- the escape hatch works on every path it guards (pinned
    # decision 5), not only on the public entry.
    escaped = _assemble_minutes_feature_row_from_raw(
        raw,
        season="2026-27",
        round=4,
        element=37,
        fixture=41,
        kickoff_time=_parse_kickoff("2026-09-12T14:00:00Z"),
        team="Aston Villa",
        position="DEF",
        was_home=True,
        config=MinutesModelConfig(),
        allow_coalesced_live_nulls=True,
    )
    assert escaped["prev_gw_value"] == 0.0


def test_guard_fires_on_assemble_minutes_feature_row_from_frame_too(temp_store):
    """The other half of the hole: `assemble_minutes_feature_row_from_frame`
    has no store and no `source_provider` to check, but it shares the same
    private tail, so the provenance-free frame check fires here too -- a
    hand-built frame whose `value`/`selected` are NULL for every prior round
    of this element raises, with no store involved at all."""
    hand_built = pl.DataFrame(
        {
            "season": ["2026-27"] * 3,
            "element": [37, 37, 37],
            "round": [1, 2, 3],
            "starts": [1, 1, 1],
            "minutes": [90, 90, 90],
            "value": [None, None, None],
            "selected": [None, None, None],
            "position": ["DEF", "DEF", "DEF"],
            "team": ["Aston Villa"] * 3,
            "fixture": [11, 21, 31],
            "kickoff_time": ["2026-08-15T14:00:00Z", "2026-08-22T14:00:00Z", "2026-08-29T14:00:00Z"],
        }
    )

    with pytest.raises(FeatureAssemblyError) as excinfo:
        assemble_minutes_feature_row_from_frame(
            hand_built,
            season="2026-27",
            round=4,
            element=37,
            fixture=41,
            kickoff_time=_parse_kickoff("2026-09-12T14:00:00Z"),
            team="Aston Villa",
            position="DEF",
            was_home=True,
        )
    assert "value" in str(excinfo.value)
    assert "selected" in str(excinfo.value)
    assert type(excinfo.value) is FeatureAssemblyError

    escaped = assemble_minutes_feature_row_from_frame(
        hand_built,
        season="2026-27",
        round=4,
        element=37,
        fixture=41,
        kickoff_time=_parse_kickoff("2026-09-12T14:00:00Z"),
        team="Aston Villa",
        position="DEF",
        was_home=True,
        allow_coalesced_live_nulls=True,
    )
    assert escaped["prev_gw_value"] == 0.0


def test_frame_side_guard_does_not_over_trigger_on_partial_real_data(temp_store):
    """Attack the other direction (invented, not in the brief's own list):
    a frame where only SOME prior rounds have NULL value/selected and at
    least one carries a real value must NOT raise -- the trigger is "NULL
    in every one", never "NULL in some"."""
    mixed = pl.DataFrame(
        {
            "season": ["2026-27"] * 3,
            "element": [37, 37, 37],
            "round": [1, 2, 3],
            "starts": [1, 1, 1],
            "minutes": [90, 90, 90],
            "value": [None, None, 55],
            "selected": [None, None, 90000],
            "position": ["DEF", "DEF", "DEF"],
            "team": ["Aston Villa"] * 3,
            "fixture": [11, 21, 31],
            "kickoff_time": ["2026-08-15T14:00:00Z", "2026-08-22T14:00:00Z", "2026-08-29T14:00:00Z"],
        }
    )

    assembled = assemble_minutes_feature_row_from_frame(
        mixed,
        season="2026-27",
        round=4,
        element=37,
        fixture=41,
        kickoff_time=_parse_kickoff("2026-09-12T14:00:00Z"),
        team="Aston Villa",
        position="DEF",
        was_home=True,
    )
    assert assembled["prev_gw_value"] == 55.0


# ---------------------------------------------------------------------------
# 3. Equivalence against the real store -- several elements, several
#    fixtures, more than one season. `slow`: reads data/store/.
# ---------------------------------------------------------------------------


def _real_store_available() -> bool:
    path = Path(__file__).resolve().parents[1] / "data" / "store" / "vaastav_player_gameweek_stats"
    return path.exists() and any(path.glob("**/*.parquet"))


requires_real_store = pytest.mark.skipif(
    not _real_store_available(), reason="data/store/vaastav_player_gameweek_stats absent"
)


@pytest.mark.slow
@requires_real_store
def test_matches_build_training_table_on_the_real_store_several_elements_several_fixtures():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])

    # Deterministic selection (CLAUDE.md rule 7 -- no unseeded randomness):
    # first two rounds > 3 for each of six distinct elements across the
    # three seasons pulled above, so this is several elements AND several
    # fixtures AND more than one season, not one lucky row.
    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[::max(1, len(elements) // 6)][:6]
    candidates = (
        table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3))
        .sort(["season", "element", "round"])
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10  # "not one lucky row"

    for target in picked:
        assembled = assemble_minutes_feature_row(
            real_store,
            season=target["season"],
            round=target["round"],
            element=target["element"],
            fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]),
            team=target["team"],
            position=target["position"],
            was_home=bool(target["was_home"]),
        )
        _assert_row_matches(assembled, target)


@pytest.mark.slow
@requires_real_store
def test_frame_side_guard_does_not_fire_on_a_real_historical_fixture():
    """Story S0-b's own required attack: a genuine, settled historical
    fixture (2025-26, round 20, element 116) must not raise and must
    produce the same `prev_gw_value` `build_training_table` does -- vaastav
    supplies real values, so the provenance-free frame check never trips on
    it. Verified live before writing this assertion (this story's punch-out,
    VERIFIED LIVE): `build_training_table` gives `prev_gw_value == 40.0` for
    exactly this key."""
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=datetime.now(UTC), seasons=["2025-26"])
    target = table.filter((pl.col("element") == 116) & (pl.col("round") == 20)).to_dicts()[0]
    assert target["prev_gw_value"] == 40.0

    assembled = assemble_minutes_feature_row(
        real_store,
        season=target["season"],
        round=target["round"],
        element=target["element"],
        fixture=target["fixture"],
        kickoff_time=_parse_kickoff(target["kickoff_time"]),
        team=target["team"],
        position=target["position"],
        was_home=bool(target["was_home"]),
    )
    assert assembled["prev_gw_value"] == 40.0


# ---------------------------------------------------------------------------
# 4. `assemble_attacking_feature_row` -- second instance (module docstring,
#    "Second instance"). Equivalence on a controlled synthetic store, then
#    the leakage claim attacked specifically for this path (not assumed
#    covered by the minutes tests above), then the real store.
# ---------------------------------------------------------------------------


def _five_round_attacking_history_rows() -> list[dict]:
    return [
        _attacking_row(
            "2022-23", 1, 20, 201, "2022-08-06T14:00:00Z",
            was_home=True, minutes=90, goals_scored=1, assists=0,
            expected_goals=0.8, expected_assists=0.1, team_h_score=2, team_a_score=0,
        ),
        _attacking_row(
            "2022-23", 2, 20, 211, "2022-08-13T14:00:00Z",
            was_home=False, minutes=45, goals_scored=0, assists=1,
            expected_goals=0.1, expected_assists=0.3, team_h_score=1, team_a_score=1,
        ),
        _attacking_row(
            "2022-23", 3, 20, 221, "2022-08-20T14:00:00Z",
            was_home=True, minutes=0, goals_scored=0, assists=0,
            expected_goals=0.0, expected_assists=0.0, team_h_score=0, team_a_score=0,
        ),
        _attacking_row(
            "2022-23", 4, 20, 231, "2022-08-27T14:00:00Z",
            was_home=False, minutes=90, goals_scored=1, assists=1,
            expected_goals=0.6, expected_assists=0.4, team_h_score=3, team_a_score=2,
        ),
        _attacking_row(
            "2022-23", 5, 20, 241, "2022-09-03T14:00:00Z",
            was_home=True, minutes=90, goals_scored=2, assists=0,
            expected_goals=1.2, expected_assists=0.05, team_h_score=2, team_a_score=1,
        ),
    ]


def test_attacking_matches_build_training_table_mid_season_round(temp_store):
    rows = _five_round_attacking_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_attacking_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 5).to_dicts()[0]

    assembled = assemble_attacking_feature_row(
        temp_store,
        season="2022-23",
        round=5,
        element=20,
        fixture=241,
        kickoff_time=_parse_kickoff("2022-09-03T14:00:00Z"),
        team="TeamA",
        position="MID",
        was_home=True,
    )
    _assert_attacking_row_matches(assembled, target)


def test_attacking_matches_build_training_table_cold_start_round(temp_store):
    """Round 1 -- no prior history. games_played_this_season == 0,
    cold_start == True, every trailing rate filled to 0.0 -- exercised
    against build_training_table's own row, not merely asserted."""
    rows = _five_round_attacking_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_attacking_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 1).to_dicts()[0]
    assert target["cold_start"] is True
    assert target["games_played_this_season"] == 0.0

    assembled = assemble_attacking_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=20,
        fixture=201,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamA",
        position="MID",
        was_home=True,
    )
    _assert_attacking_row_matches(assembled, target)


def test_attacking_matches_build_training_table_across_several_rounds(temp_store):
    """Not one lucky row -- every round of the same synthetic history."""
    rows = _five_round_attacking_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_attacking_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])

    for r in rows:
        target = table.filter(pl.col("round") == r["round"]).to_dicts()[0]
        assembled = assemble_attacking_feature_row(
            temp_store,
            season=r["season"],
            round=r["round"],
            element=r["element"],
            fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]),
            team=r["team"],
            position=r["position"],
            was_home=r["was_home"],
        )
        _assert_attacking_row_matches(assembled, target)


def test_attacking_assembled_features_unchanged_whether_or_not_later_rows_exist(temp_store):
    """The leakage claim, attacked specifically for the attacking path
    (CLAUDE.md: not assumed covered by the minutes test of the same
    shape above) -- assemble the same target fixture's row twice, once
    against a store holding only rows strictly before the target's
    kickoff, once against a store that ALSO has a later round for the
    same element, and show the output is byte-for-byte identical."""
    history = _five_round_attacking_history_rows()

    store_without_future = BitemporalStore(base_path=Path(temp_store.base_path) / "attacking_no_future")
    _write_fixture(store_without_future, history[:3], observed_at=dt(2026, 1, 1))  # rounds 1-3 only

    store_with_future = BitemporalStore(base_path=Path(temp_store.base_path) / "attacking_with_future")
    _write_fixture(store_with_future, history, observed_at=dt(2026, 1, 1))  # rounds 1-5

    target = history[3]  # round 4 -- has a later row (round 5) in one store, not the other
    kwargs = dict(
        season=target["season"],
        round=target["round"],
        element=target["element"],
        fixture=target["fixture"],
        kickoff_time=_parse_kickoff(target["kickoff_time"]),
        team=target["team"],
        position=target["position"],
        was_home=target["was_home"],
    )
    without_future = assemble_attacking_feature_row(store_without_future, **kwargs)
    with_future = assemble_attacking_feature_row(store_with_future, **kwargs)
    assert without_future == with_future


def test_attacking_kickoff_time_must_be_timezone_aware(temp_store):
    with pytest.raises(FeatureAssemblyError):
        assemble_attacking_feature_row(
            temp_store,
            season="2022-23",
            round=1,
            element=20,
            fixture=201,
            kickoff_time=datetime(2022, 8, 6, 14, 0),  # naive
            team="TeamA",
            position="MID",
            was_home=True,
        )


def test_attacking_empty_store_is_a_valid_cold_start_not_an_error(temp_store):
    """No history at all anywhere in the store -- must NOT raise; must
    produce the same cold-start shape build_training_table would."""
    assembled = assemble_attacking_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=999,
        fixture=1,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamZ",
        position="FWD",
        was_home=False,
    )
    assert assembled["cold_start"] is True
    assert assembled["games_played_this_season"] == 0.0
    assert assembled["player_trailing_goals_3"] == 0.0
    assert assembled["player_trailing_xg_10"] == 0.0
    assert assembled["was_home"] is False
    assert "team" not in assembled  # module docstring: never a key predict_attacking_pmf never reads


@pytest.mark.slow
@requires_real_store
def test_attacking_matches_build_training_table_on_the_real_store_several_elements_several_fixtures():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_attacking_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])

    # Deterministic selection (CLAUDE.md rule 7 -- no unseeded randomness),
    # same pattern the minutes real-store test above uses: first two
    # rounds > 3 for each of six distinct elements across the three
    # seasons pulled above.
    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[::max(1, len(elements) // 6)][:6]
    candidates = (
        table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3))
        .sort(["season", "element", "round"])
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10  # "not one lucky row"

    for target in picked:
        assembled = assemble_attacking_feature_row(
            real_store,
            season=target["season"],
            round=target["round"],
            element=target["element"],
            fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]),
            team=target["team"],
            position=target["position"],
            was_home=bool(target["was_home"]),
        )
        _assert_attacking_row_matches(assembled, target)


# ---------------------------------------------------------------------------
# 5. `assemble_cards_feature_row` -- third instance (module docstring,
#    "Third, fourth, fifth instances"). First to carry a per-team rollup
#    alongside its per-player one.
# ---------------------------------------------------------------------------


def _cards_row(
    season: str,
    round_: int,
    element: int,
    fixture: int,
    kickoff: str,
    *,
    team: str = "TeamA",
    was_home: bool = True,
    minutes: int = 90,
    position: str = "MID",
    yellow_cards: int = 0,
    red_cards: int = 0,
) -> dict:
    base = _row(season, round_, element, fixture, kickoff, team=team, was_home=was_home, minutes=minutes, position=position)
    base.update({"yellow_cards": yellow_cards, "red_cards": red_cards})
    return base


def _assert_cards_row_matches(assembled: dict, expected: dict) -> None:
    mismatches = []
    for c in NUMERIC_FEATURE_COLUMNS_CARDS:
        a, b = assembled[c], expected[c]
        ok = abs(float(a) - float(b)) < 1e-9 if isinstance(a, float) or isinstance(b, float) else a == b
        if not ok:
            mismatches.append((c, a, b))
    if assembled["position"] != expected["position"]:
        mismatches.append(("position", assembled["position"], expected["position"]))
    assert not mismatches, f"cards feature mismatch(es): {mismatches}"


def _five_round_cards_history_rows() -> list[dict]:
    return [
        _cards_row("2022-23", 1, 30, 301, "2022-08-06T14:00:00Z", was_home=True, minutes=90, yellow_cards=0, red_cards=0),
        _cards_row("2022-23", 2, 30, 311, "2022-08-13T14:00:00Z", was_home=False, minutes=90, yellow_cards=1, red_cards=0),
        _cards_row("2022-23", 3, 30, 321, "2022-08-20T14:00:00Z", was_home=True, minutes=0, yellow_cards=0, red_cards=0),
        _cards_row("2022-23", 4, 30, 331, "2022-08-27T14:00:00Z", was_home=False, minutes=90, yellow_cards=0, red_cards=1),
        _cards_row("2022-23", 5, 30, 341, "2022-09-03T14:00:00Z", was_home=True, minutes=90, yellow_cards=0, red_cards=0),
    ]


def test_cards_matches_build_training_table_mid_season_round(temp_store):
    rows = _five_round_cards_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_cards_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 5).to_dicts()[0]

    assembled = assemble_cards_feature_row(
        temp_store,
        season="2022-23",
        round=5,
        element=30,
        fixture=341,
        kickoff_time=_parse_kickoff("2022-09-03T14:00:00Z"),
        team="TeamA",
        position="MID",
        was_home=True,
    )
    _assert_cards_row_matches(assembled, target)


def test_cards_matches_build_training_table_cold_start_round(temp_store):
    rows = _five_round_cards_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_cards_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 1).to_dicts()[0]
    assert target["cold_start"] is True
    assert target["games_played_this_season"] == 0.0

    assembled = assemble_cards_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=30,
        fixture=301,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamA",
        position="MID",
        was_home=True,
    )
    _assert_cards_row_matches(assembled, target)


def test_cards_matches_build_training_table_across_several_rounds(temp_store):
    """Not one lucky row -- every round of the same synthetic history,
    including the round-4 red card and the round-2 yellow, so the
    team_trailing_cards_mean_5 rollup this instance adds is exercised for
    a real non-zero trailing value, not just a cold-start zero. Round 3
    (minutes=0) is skipped here -- cards' own build_training_table applies
    a minutes>0 restriction AFTER the rollups (module docstring of
    fplai.models.cards, "A card without a minute is real, not a data
    error"), so it has no row in `table` to compare against at all; the
    rollup itself (assembled row's own trailing features) still reads
    round 3's real yellow_cards=0/red_cards=0 into round 4/5's trailing
    windows, exercised implicitly by every OTHER round's own equivalence
    check below."""
    rows = _five_round_cards_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_cards_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])

    for r in rows:
        if r["minutes"] == 0:
            continue
        target = table.filter(pl.col("round") == r["round"]).to_dicts()[0]
        assembled = assemble_cards_feature_row(
            temp_store,
            season=r["season"],
            round=r["round"],
            element=r["element"],
            fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]),
            team=r["team"],
            position=r["position"],
            was_home=r["was_home"],
        )
        _assert_cards_row_matches(assembled, target)
    # a real, non-zero team-trailing value was actually exercised above,
    # not just the cold-start zero every round-1 test already covers.
    round5_target = table.filter(pl.col("round") == 5).to_dicts()[0]
    assert round5_target["team_trailing_cards_mean_5"] > 0.0


def test_cards_kickoff_time_must_be_timezone_aware(temp_store):
    with pytest.raises(FeatureAssemblyError):
        assemble_cards_feature_row(
            temp_store,
            season="2022-23",
            round=1,
            element=30,
            fixture=301,
            kickoff_time=datetime(2022, 8, 6, 14, 0),  # naive
            team="TeamA",
            position="MID",
            was_home=True,
        )


def test_cards_empty_store_is_a_valid_cold_start_not_an_error(temp_store):
    assembled = assemble_cards_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=999,
        fixture=1,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamZ",
        position="FWD",
        was_home=False,
    )
    assert assembled["cold_start"] is True
    assert assembled["games_played_this_season"] == 0.0
    assert assembled["player_trailing_card_rate_3"] == 0.0
    assert assembled["team_trailing_cards_mean_5"] == 0.0
    assert assembled["team_cold_start"] is True
    assert assembled["was_home"] is False
    assert "team" not in assembled  # predict_cards_pmf's own design matrix has no team dummy


@pytest.mark.slow
@requires_real_store
def test_cards_matches_build_training_table_on_the_real_store_several_elements_several_fixtures():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_cards_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])

    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 6)][:6]
    candidates = (
        table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3))
        .sort(["season", "element", "round"])
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    for target in picked:
        assembled = assemble_cards_feature_row(
            real_store,
            season=target["season"],
            round=target["round"],
            element=target["element"],
            fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]),
            team=target["team"],
            position=target["position"],
            was_home=bool(target["was_home"]),
        )
        _assert_cards_row_matches(assembled, target)


# ---------------------------------------------------------------------------
# 6. `assemble_dc_feature_row` -- fourth instance. Needs a live
#    game_config (DC-eligibility gate) and is the only assembler that can
#    legitimately refuse a position outright.
# ---------------------------------------------------------------------------


def _write_game_config(store, *, observed_at, points=None):
    points = points or {"DEF": 2, "FWD": 2, "GKP": 0, "MID": 2}
    payload = {"rules": {}, "scoring": {"defensive_contribution": points}, "settings": {}}
    store.write(
        "game_config", pl.DataFrame({"payload": [json.dumps(payload)]}), valid_at=observed_at, observed_at=observed_at, source="test"
    )


def _dc_row(
    season: str,
    round_: int,
    element: int,
    fixture: int,
    kickoff: str,
    *,
    team: str = "TeamB",
    was_home: bool = True,
    minutes: int = 90,
    position: str = "DEF",
    tackles: int = 1,
    cbi: int = 1,
    recoveries: int = 1,
) -> dict:
    dc_total = (tackles + cbi) if position == "DEF" else (tackles + cbi + recoveries)
    if position == "GK":
        dc_total = 0
    base = _row(season, round_, element, fixture, kickoff, team=team, was_home=was_home, minutes=minutes, position=position)
    base.update(
        {
            "tackles": tackles,
            "clearances_blocks_interceptions": cbi,
            "recoveries": recoveries,
            "defensive_contribution": dc_total,
        }
    )
    return base


def _assert_dc_row_matches(assembled: dict, expected: dict) -> None:
    mismatches = []
    for c in NUMERIC_FEATURE_COLUMNS_DC:
        a, b = assembled[c], expected[c]
        ok = abs(float(a) - float(b)) < 1e-9 if isinstance(a, float) or isinstance(b, float) else a == b
        if not ok:
            mismatches.append((c, a, b))
    assert not mismatches, f"DC feature mismatch(es): {mismatches}"
    assert "position" not in assembled  # predict_dc_pmf takes position as a SEPARATE argument
    assert "team" not in assembled


def _five_round_dc_history_rows() -> list[dict]:
    return [
        _dc_row("2022-23", 1, 40, 401, "2022-08-06T14:00:00Z", was_home=True, minutes=90, tackles=2, cbi=3, recoveries=0),
        _dc_row("2022-23", 2, 40, 411, "2022-08-13T14:00:00Z", was_home=False, minutes=90, tackles=1, cbi=1, recoveries=0),
        _dc_row("2022-23", 3, 40, 421, "2022-08-20T14:00:00Z", was_home=True, minutes=0, tackles=0, cbi=0, recoveries=0),
        _dc_row("2022-23", 4, 40, 431, "2022-08-27T14:00:00Z", was_home=False, minutes=90, tackles=3, cbi=4, recoveries=0),
        _dc_row("2022-23", 5, 40, 441, "2022-09-03T14:00:00Z", was_home=True, minutes=90, tackles=2, cbi=2, recoveries=0),
    ]


def test_dc_matches_build_training_table_mid_season_round(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    rows = _five_round_dc_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_dc_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 5).to_dicts()[0]

    assembled = assemble_dc_feature_row(
        temp_store,
        season="2022-23",
        round=5,
        element=40,
        fixture=441,
        kickoff_time=_parse_kickoff("2022-09-03T14:00:00Z"),
        team="TeamB",
        position="DEF",
        was_home=True,
    )
    _assert_dc_row_matches(assembled, target)


def test_dc_matches_build_training_table_cold_start_round(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    rows = _five_round_dc_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_dc_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 1).to_dicts()[0]
    assert target["cold_start"] is True
    assert target["games_played_this_season"] == 0.0

    assembled = assemble_dc_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=40,
        fixture=401,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamB",
        position="DEF",
        was_home=True,
    )
    _assert_dc_row_matches(assembled, target)


def test_dc_matches_build_training_table_across_several_rounds(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    rows = _five_round_dc_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_dc_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])

    for r in rows:
        target = table.filter(pl.col("round") == r["round"]).to_dicts()[0]
        assembled = assemble_dc_feature_row(
            temp_store,
            season=r["season"],
            round=r["round"],
            element=r["element"],
            fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]),
            team=r["team"],
            position=r["position"],
            was_home=r["was_home"],
        )
        _assert_dc_row_matches(assembled, target)
    round5_target = table.filter(pl.col("round") == 5).to_dicts()[0]
    assert round5_target["team_trailing_dc_mean_5"] > 0.0


def test_dc_kickoff_time_must_be_timezone_aware(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    with pytest.raises(FeatureAssemblyError):
        assemble_dc_feature_row(
            temp_store,
            season="2022-23",
            round=1,
            element=40,
            fixture=401,
            kickoff_time=datetime(2022, 8, 6, 14, 0),  # naive
            team="TeamB",
            position="DEF",
            was_home=True,
        )


def test_dc_empty_store_is_a_valid_cold_start_not_an_error(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    assembled = assemble_dc_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=999,
        fixture=1,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamZ",
        position="FWD",
        was_home=False,
    )
    assert assembled["cold_start"] is True
    assert assembled["games_played_this_season"] == 0.0
    assert assembled["player_trailing_count_3"] == 0.0
    assert assembled["team_trailing_dc_mean_5"] == 0.0
    assert assembled["team_cold_start"] is True
    assert assembled["was_home"] is False
    assert assembled["is_forward"] is True


def test_dc_ineligible_position_refuses_to_manufacture_a_feature_row(temp_store):
    """predict_dc_pmf itself never reads feature_row for a DC-ineligible
    position (module docstring, "DC's position-eligibility gate") -- this
    assembler refuses outright rather than silently zero-filling one."""
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    with pytest.raises(FeatureAssemblyError):
        assemble_dc_feature_row(
            temp_store,
            season="2022-23",
            round=1,
            element=999,
            fixture=1,
            kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
            team="TeamZ",
            position="GK",
            was_home=False,
        )


def test_dc_assembled_features_unchanged_whether_or_not_later_rows_exist(temp_store):
    """The leakage claim, attacked specifically for the DC path (CLAUDE.md:
    not assumed covered by the minutes/attacking tests of the same shape)
    -- assemble the same target fixture's row twice, once against a store
    holding only rows strictly before the target's kickoff, once against a
    store that ALSO has a later round for the same element, and show the
    output is byte-for-byte identical."""
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    history = _five_round_dc_history_rows()

    store_without_future = BitemporalStore(base_path=Path(temp_store.base_path) / "dc_no_future")
    _write_game_config(store_without_future, observed_at=dt(2026, 1, 1))
    _write_fixture(store_without_future, history[:3], observed_at=dt(2026, 1, 1))

    store_with_future = BitemporalStore(base_path=Path(temp_store.base_path) / "dc_with_future")
    _write_game_config(store_with_future, observed_at=dt(2026, 1, 1))
    _write_fixture(store_with_future, history, observed_at=dt(2026, 1, 1))

    target = history[3]  # round 4 -- has a later row (round 5) in one store, not the other
    kwargs = dict(
        season=target["season"],
        round=target["round"],
        element=target["element"],
        fixture=target["fixture"],
        kickoff_time=_parse_kickoff(target["kickoff_time"]),
        team=target["team"],
        position=target["position"],
        was_home=target["was_home"],
    )
    without_future = assemble_dc_feature_row(store_without_future, **kwargs)
    with_future = assemble_dc_feature_row(store_with_future, **kwargs)
    assert without_future == with_future


@pytest.mark.slow
@requires_real_store
def test_dc_matches_build_training_table_on_the_real_store_several_elements_several_fixtures():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_dc_training_table(real_store, as_of=as_of, seasons=["2025-26"])

    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 6)][:6]
    candidates = (
        table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3))
        .sort(["season", "element", "round"])
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    for target in picked:
        assembled = assemble_dc_feature_row(
            real_store,
            season=target["season"],
            round=target["round"],
            element=target["element"],
            fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]),
            team=target["team"],
            position=target["position"],
            was_home=bool(target["was_home"]),
        )
        _assert_dc_row_matches(assembled, target)


# ---------------------------------------------------------------------------
# 7. `assemble_bonus_feature_row` -- fifth instance. Zero-minute rows
#    INCLUDED (module docstring of fplai.models.bonus); the minutes_frac
#    exclusion is attacked from both sides (gate 2).
# ---------------------------------------------------------------------------


def _bonus_row(
    season: str,
    round_: int,
    element: int,
    fixture: int,
    kickoff: str,
    *,
    team: str = "TeamC",
    was_home: bool = True,
    minutes: int = 90,
    position: str = "MID",
    bps: int = 20,
    bonus: int = 0,
) -> dict:
    base = _row(season, round_, element, fixture, kickoff, team=team, was_home=was_home, minutes=minutes, position=position)
    base.update({"bps": bps, "bonus": bonus})
    return base


def _assert_bonus_row_matches(assembled: dict, expected: dict) -> None:
    mismatches = []
    for c in NUMERIC_FEATURE_COLUMNS_BONUS:
        if c == "minutes_frac":
            continue
        a, b = assembled[c], expected[c]
        ok = abs(float(a) - float(b)) < 1e-9 if isinstance(a, float) or isinstance(b, float) else a == b
        if not ok:
            mismatches.append((c, a, b))
    assert not mismatches, f"bonus feature mismatch(es): {mismatches}"
    assert "minutes_frac" not in assembled  # pinned decision 1
    assert "position" not in assembled  # BonusPlayerInput carries it on its own field
    assert "team" not in assembled


def _five_round_bonus_history_rows() -> list[dict]:
    return [
        _bonus_row("2022-23", 1, 50, 501, "2022-08-06T14:00:00Z", was_home=True, minutes=90, bps=20, bonus=0),
        _bonus_row("2022-23", 2, 50, 511, "2022-08-13T14:00:00Z", was_home=False, minutes=90, bps=35, bonus=2),
        # Zero-minute round INCLUDED (module docstring of fplai.models.bonus,
        # "Zero-minute rows are INCLUDED") -- unlike cards/DC, this is not a
        # pre-filtered-out round for either the rollup OR the training table.
        _bonus_row("2022-23", 3, 50, 521, "2022-08-20T14:00:00Z", was_home=True, minutes=0, bps=0, bonus=0),
        _bonus_row("2022-23", 4, 50, 531, "2022-08-27T14:00:00Z", was_home=False, minutes=90, bps=28, bonus=1),
        _bonus_row("2022-23", 5, 50, 541, "2022-09-03T14:00:00Z", was_home=True, minutes=90, bps=15, bonus=0),
    ]


def test_bonus_matches_build_training_table_mid_season_round(temp_store):
    rows = _five_round_bonus_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_bonus_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 5).to_dicts()[0]

    assembled = assemble_bonus_feature_row(
        temp_store,
        season="2022-23",
        round=5,
        element=50,
        fixture=541,
        kickoff_time=_parse_kickoff("2022-09-03T14:00:00Z"),
        team="TeamC",
        position="MID",
        was_home=True,
    )
    _assert_bonus_row_matches(assembled, target)


def test_bonus_matches_build_training_table_cold_start_round(temp_store):
    rows = _five_round_bonus_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_bonus_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 1).to_dicts()[0]
    assert target["cold_start"] is True
    assert target["games_played_this_season"] == 0.0

    assembled = assemble_bonus_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=50,
        fixture=501,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamC",
        position="MID",
        was_home=True,
    )
    _assert_bonus_row_matches(assembled, target)


def test_bonus_matches_build_training_table_across_several_rounds_including_the_zero_minute_round(temp_store):
    """Not one lucky row -- every round of the same synthetic history,
    INCLUDING round 3's zero-minute row (module docstring: bonus never
    excludes these, unlike cards/DC)."""
    rows = _five_round_bonus_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_bonus_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    assert table.height == 5  # zero-minute round 3 was NOT dropped

    for r in rows:
        target = table.filter(pl.col("round") == r["round"]).to_dicts()[0]
        assembled = assemble_bonus_feature_row(
            temp_store,
            season=r["season"],
            round=r["round"],
            element=r["element"],
            fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]),
            team=r["team"],
            position=r["position"],
            was_home=r["was_home"],
        )
        _assert_bonus_row_matches(assembled, target)
    round5_target = table.filter(pl.col("round") == 5).to_dicts()[0]
    assert round5_target["team_trailing_bps_mean_5"] > 0.0


def test_bonus_kickoff_time_must_be_timezone_aware(temp_store):
    with pytest.raises(FeatureAssemblyError):
        assemble_bonus_feature_row(
            temp_store,
            season="2022-23",
            round=1,
            element=50,
            fixture=501,
            kickoff_time=datetime(2022, 8, 6, 14, 0),  # naive
            team="TeamC",
            position="MID",
            was_home=True,
        )


def test_bonus_empty_store_is_a_valid_cold_start_not_an_error(temp_store):
    assembled = assemble_bonus_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=999,
        fixture=1,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamZ",
        position="FWD",
        was_home=False,
    )
    assert assembled["cold_start"] is True
    assert assembled["games_played_this_season"] == 0.0
    assert assembled["player_trailing_bps_3"] == 0.0
    assert assembled["team_trailing_bps_mean_5"] == 0.0
    assert assembled["team_cold_start"] is True
    assert assembled["was_home"] is False


def test_bonus_feature_row_never_carries_minutes_frac_and_bonusplayerinput_attacked_from_both_sides(temp_store):
    """Gate 2 (this dispatch's brief): prove the bonus exclusion is real,
    not just an omitted key. Side 1: BonusPlayerInput constructed straight
    from the assembled row does NOT raise. Side 2: adding minutes_frac to
    that SAME row DOES raise -- the guarantee attacked from both
    directions, not merely asserted from one."""
    rows = _five_round_bonus_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    assembled = assemble_bonus_feature_row(
        temp_store,
        season="2022-23",
        round=5,
        element=50,
        fixture=541,
        kickoff_time=_parse_kickoff("2022-09-03T14:00:00Z"),
        team="TeamC",
        position="MID",
        was_home=True,
    )
    assert "minutes_frac" not in assembled
    assert "position" not in assembled

    # Side 1: does NOT raise.
    player_input = BonusPlayerInput(element=50, position="MID", feature_row=assembled, minute_exposure=[(90.0, 1.0)])
    assert player_input.feature_row == assembled

    # Side 2: adding minutes_frac to the SAME row DOES raise.
    leaked = dict(assembled)
    leaked["minutes_frac"] = 1.0
    with pytest.raises(BonusModelError):
        BonusPlayerInput(element=50, position="MID", feature_row=leaked, minute_exposure=[(90.0, 1.0)])


@pytest.mark.slow
@requires_real_store
def test_bonus_matches_build_training_table_on_the_real_store_several_elements_several_fixtures():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_bonus_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])

    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 6)][:6]
    candidates = (
        table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3))
        .sort(["season", "element", "round"])
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    for target in picked:
        assembled = assemble_bonus_feature_row(
            real_store,
            season=target["season"],
            round=target["round"],
            element=target["element"],
            fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]),
            team=target["team"],
            position=target["position"],
            was_home=bool(target["was_home"]),
        )
        _assert_bonus_row_matches(assembled, target)


# ---------------------------------------------------------------------------
# 8. `assemble_saves_feature_row` -- sixth, and last, instance. GK-only,
#    single rollup, built against PREDICT_FEATURE_ROW_COLUMNS (6) not the
#    training constant (7) -- opponent_goals_this_fixture is a fixture
#    OUTCOME, attacked from both sides in gate 2 below (this dispatch's
#    brief). Gate 3 (below, its own section) proves the seam actually
#    binds a real fitted model into fplai.points.simulate_fixture_points_
#    pmfs, not merely that the two signatures read compatibly.
# ---------------------------------------------------------------------------


def _saves_row(
    season: str,
    round_: int,
    element: int,
    fixture: int,
    kickoff: str,
    *,
    team: str = "TeamD",
    was_home: bool = True,
    minutes: int = 90,
    position: str = "GK",
    saves: int = 2,
    team_h_score: int = 1,
    team_a_score: int = 1,
) -> dict:
    base = _row(season, round_, element, fixture, kickoff, team=team, was_home=was_home, minutes=minutes, position=position)
    base.update({"saves": saves, "team_h_score": team_h_score, "team_a_score": team_a_score})
    return base


def _assert_saves_row_matches(assembled: dict, expected: dict) -> None:
    mismatches = []
    for c in PREDICT_FEATURE_ROW_COLUMNS:
        a, b = assembled[c], expected[c]
        ok = abs(float(a) - float(b)) < 1e-9 if isinstance(a, float) or isinstance(b, float) else a == b
        if not ok:
            mismatches.append((c, a, b))
    assert not mismatches, f"saves feature mismatch(es): {mismatches}"
    assert "opponent_goals_this_fixture" not in assembled  # pinned decision 1 -- a fixture OUTCOME, never assembled
    assert "position" not in assembled  # no position one-hot -- saves.py's own population is GK-only
    assert "team" not in assembled


def _five_round_saves_history_rows() -> list[dict]:
    return [
        _saves_row("2022-23", 1, 60, 601, "2022-08-06T14:00:00Z", was_home=True, minutes=90, saves=1, team_h_score=1, team_a_score=0),
        _saves_row("2022-23", 2, 60, 611, "2022-08-13T14:00:00Z", was_home=False, minutes=90, saves=3, team_h_score=2, team_a_score=1),
        _saves_row("2022-23", 3, 60, 621, "2022-08-20T14:00:00Z", was_home=True, minutes=90, saves=2, team_h_score=1, team_a_score=1),
        _saves_row("2022-23", 4, 60, 631, "2022-08-27T14:00:00Z", was_home=False, minutes=90, saves=4, team_h_score=0, team_a_score=2),
        _saves_row("2022-23", 5, 60, 641, "2022-09-03T14:00:00Z", was_home=True, minutes=90, saves=0, team_h_score=3, team_a_score=0),
    ]


def test_saves_matches_build_training_table_mid_season_round(temp_store):
    rows = _five_round_saves_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_saves_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 5).to_dicts()[0]

    assembled = assemble_saves_feature_row(
        temp_store,
        season="2022-23",
        round=5,
        element=60,
        fixture=641,
        kickoff_time=_parse_kickoff("2022-09-03T14:00:00Z"),
        team="TeamD",
        position="GK",
        was_home=True,
    )
    _assert_saves_row_matches(assembled, target)


def test_saves_matches_build_training_table_cold_start_round(temp_store):
    rows = _five_round_saves_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    table = build_saves_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])
    target = table.filter(pl.col("round") == 1).to_dicts()[0]
    assert target["cold_start"] is True
    assert target["games_played_this_season"] == 0.0

    assembled = assemble_saves_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=60,
        fixture=601,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamD",
        position="GK",
        was_home=True,
    )
    _assert_saves_row_matches(assembled, target)


def test_saves_matches_build_training_table_across_several_rounds_several_elements(temp_store):
    """Gate 1's own wording: several GK elements, more than one fixture --
    two independent GKs' full histories in the same store, every round of
    each checked, not one lucky row."""
    rows_a = _five_round_saves_history_rows()
    rows_b = [
        _saves_row("2022-23", 1, 61, 701, "2022-08-06T14:00:00Z", team="TeamE", was_home=False, minutes=90, saves=5, team_h_score=1, team_a_score=1),
        _saves_row("2022-23", 2, 61, 711, "2022-08-13T14:00:00Z", team="TeamE", was_home=True, minutes=90, saves=2, team_h_score=2, team_a_score=0),
        _saves_row("2022-23", 3, 61, 721, "2022-08-20T14:00:00Z", team="TeamE", was_home=False, minutes=90, saves=1, team_h_score=0, team_a_score=0),
    ]
    _write_fixture(temp_store, rows_a + rows_b, observed_at=dt(2026, 1, 1))
    table = build_saves_training_table(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])

    for r in rows_a + rows_b:
        target = table.filter((pl.col("round") == r["round"]) & (pl.col("element") == r["element"])).to_dicts()[0]
        assembled = assemble_saves_feature_row(
            temp_store,
            season=r["season"],
            round=r["round"],
            element=r["element"],
            fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]),
            team=r["team"],
            position=r["position"],
            was_home=r["was_home"],
        )
        _assert_saves_row_matches(assembled, target)


def test_saves_non_gk_position_refuses_to_manufacture_a_feature_row(temp_store):
    """GK-only, structurally (module docstring, 'Sixth, and last,
    instance') -- refuses outright rather than manufacture a meaningless
    row for a position saves.py never trains on."""
    with pytest.raises(FeatureAssemblyError):
        assemble_saves_feature_row(
            temp_store,
            season="2022-23",
            round=1,
            element=999,
            fixture=1,
            kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
            team="TeamZ",
            position="MID",
            was_home=False,
        )


def test_saves_assembled_features_unchanged_whether_or_not_later_rows_exist(temp_store):
    """The leakage claim, attacked specifically for the saves path (CLAUDE.md:
    not assumed covered by the other five assemblers' own tests of the same
    shape)."""
    history = _five_round_saves_history_rows()

    store_without_future = BitemporalStore(base_path=Path(temp_store.base_path) / "saves_no_future")
    _write_fixture(store_without_future, history[:3], observed_at=dt(2026, 1, 1))

    store_with_future = BitemporalStore(base_path=Path(temp_store.base_path) / "saves_with_future")
    _write_fixture(store_with_future, history, observed_at=dt(2026, 1, 1))

    target = history[3]  # round 4 -- has a later row (round 5) in one store, not the other
    kwargs = dict(
        season=target["season"],
        round=target["round"],
        element=target["element"],
        fixture=target["fixture"],
        kickoff_time=_parse_kickoff(target["kickoff_time"]),
        team=target["team"],
        position=target["position"],
        was_home=target["was_home"],
    )
    without_future = assemble_saves_feature_row(store_without_future, **kwargs)
    with_future = assemble_saves_feature_row(store_with_future, **kwargs)
    assert without_future == with_future


def test_saves_kickoff_time_must_be_timezone_aware(temp_store):
    with pytest.raises(FeatureAssemblyError):
        assemble_saves_feature_row(
            temp_store,
            season="2022-23",
            round=1,
            element=60,
            fixture=601,
            kickoff_time=datetime(2022, 8, 6, 14, 0),  # naive
            team="TeamD",
            position="GK",
            was_home=True,
        )


def test_saves_empty_store_is_a_valid_cold_start_not_an_error(temp_store):
    assembled = assemble_saves_feature_row(
        temp_store,
        season="2022-23",
        round=1,
        element=999,
        fixture=1,
        kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
        team="TeamZ",
        position="GK",
        was_home=False,
    )
    assert assembled["cold_start"] is True
    assert assembled["games_played_this_season"] == 0.0
    assert assembled["player_trailing_saves_mean_3"] == 0.0
    assert assembled["player_trailing_saves_mean_5"] == 0.0
    assert assembled["player_trailing_saves_mean_10"] == 0.0
    assert assembled["was_home"] is False


def test_saves_predict_accepts_assembled_row_and_rejects_it_with_opponent_goals_smuggled_in(temp_store):
    """Gate 2 (this dispatch's brief): attacked from both sides, the same
    discipline `test_bonus_feature_row_never_carries_minutes_frac_...`
    establishes. Side 1: `predict_saves_pmf` accepts the assembled row
    as-is. Side 2: adding `opponent_goals_this_fixture` to that SAME row
    makes it raise `SavesModelError` via the specific named mechanism
    (module docstring of `fplai.models.saves`, 'Composition, not import'),
    not an unrelated missing-key error -- watching the actual mechanism
    fire, not merely an omitted key."""
    rows = _five_round_saves_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))

    assembled = assemble_saves_feature_row(
        temp_store,
        season="2022-23",
        round=5,
        element=60,
        fixture=641,
        kickoff_time=_parse_kickoff("2022-09-03T14:00:00Z"),
        team="TeamD",
        position="GK",
        was_home=True,
    )
    assert "opponent_goals_this_fixture" not in assembled

    saves_params = fit_saves_model(temp_store, as_of=dt(2026, 1, 2), seasons=["2022-23"])

    # Side 1: does NOT raise.
    pmf = predict_saves_pmf(
        saves_params,
        assembled,
        element=60,
        fixture=641,
        minute_exposure=[(90.0, 1.0)],
        opponent_goals_marginal=[(1, 1.0)],
    )
    assert abs(sum(pmf.probabilities) - 1.0) < 1e-6

    # Side 2: smuggling opponent_goals_this_fixture in DOES raise -- the
    # specific mechanism, not an omitted key.
    leaked = dict(assembled)
    leaked["opponent_goals_this_fixture"] = 1.0
    with pytest.raises(SavesModelError):
        predict_saves_pmf(
            saves_params,
            leaked,
            element=60,
            fixture=641,
            minute_exposure=[(90.0, 1.0)],
            opponent_goals_marginal=[(1, 1.0)],
        )


@pytest.mark.slow
@requires_real_store
def test_saves_matches_build_training_table_on_the_real_store_several_elements_several_fixtures():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_saves_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])

    # GK is a single-position population (113 unique elements pooled across
    # these 3 seasons, verified live this session) -- ~9x smaller than the
    # ALL-position pool bonus/cards/attacking sample from, so 10 sample
    # elements (not those modules' own 6) is needed to reliably clear 10
    # picked rows; some sampled GK elements are backup keepers with almost
    # no post-round-3 minutes in this 3-season window, a real data shape,
    # not a bug.
    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 10)][:10]
    candidates = (
        table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3))
        .sort(["season", "element", "round"])
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    for target in picked:
        assembled = assemble_saves_feature_row(
            real_store,
            season=target["season"],
            round=target["round"],
            element=target["element"],
            fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]),
            team=target["team"],
            position=target["position"],
            was_home=bool(target["was_home"]),
        )
        _assert_saves_row_matches(assembled, target)


# ---------------------------------------------------------------------------
# 9. Gate 3 (this dispatch's brief): prove the saves seam binds. Fit all
#    six outcome models plus team_strength via their own REAL public
#    fit_*_model entry points (never a stub), assemble every player's
#    feature row for a genuinely settled fixture via THIS module's own six
#    assemblers -- the same ones gates 1/2 above and every earlier section
#    of this file already prove equivalent to each model's own
#    build_training_table -- wire the real saves model into
#    `fplai.points.simulate_fixture_points_pmfs` via `make_saves_predict_
#    fn`, and show it returns real `PointsPMF`s with `saves_status ==
#    SAVES_STATUS_MODELLED` for both GKs. `fplai.points` is READ-ONLY here:
#    this proves the argument its own seam already accepts actually works
#    with the real model, not that points.py changed -- points.py's own
#    module docstring, "The saves seam", had only ever exercised this seam
#    with a hand-built stub predictor before this test.
# ---------------------------------------------------------------------------

_GATE3_SEASON = "2022-23"
_GATE3_TEAM_A = "GateTeamA"
_GATE3_TEAM_B = "GateTeamB"
_GATE3_ROSTER: dict[int, tuple[str, str]] = {
    901: ("GK", _GATE3_TEAM_A),
    902: ("DEF", _GATE3_TEAM_A),
    903: ("MID", _GATE3_TEAM_A),
    904: ("FWD", _GATE3_TEAM_A),
    905: ("GK", _GATE3_TEAM_B),
    906: ("DEF", _GATE3_TEAM_B),
    907: ("MID", _GATE3_TEAM_B),
    908: ("FWD", _GATE3_TEAM_B),
}
_GATE3_N_HISTORY_ROUNDS = 6


def _gate3_kickoff(round_: int) -> str:
    from datetime import timedelta

    d = datetime(2022, 8, 6, 14, 0, 0) + timedelta(days=7 * (round_ - 1))
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _gate3_row(
    round_: int,
    element: int,
    *,
    minutes: int = 90,
    starts: int = 1,
    goals_scored: int = 0,
    assists: int = 0,
    saves: int = 0,
    team_h_score: int = 1,
    team_a_score: int = 1,
) -> dict:
    position, team = _GATE3_ROSTER[element]
    opponent = _GATE3_TEAM_B if team == _GATE3_TEAM_A else _GATE3_TEAM_A
    was_home = team == _GATE3_TEAM_A
    tackles = cbi = recoveries = 0
    if position == "DEF":
        tackles, cbi = 6, 5  # count=11 >= DEF threshold 10 -> DC met
    elif position in ("MID", "FWD"):
        tackles, cbi, recoveries = 4, 4, 5  # count=13 >= MID/FWD threshold 12 -> DC met
    return {
        "season": _GATE3_SEASON, "round": round_, "element": element, "fixture": round_,
        "kickoff_time": _gate3_kickoff(round_), "minutes": minutes, "starts": starts,
        "position": position, "team": team, "opponent_team": opponent, "was_home": was_home,
        "value": 50, "selected": 100000,
        "goals_scored": goals_scored, "assists": assists,
        "expected_goals": 0.05, "expected_assists": 0.03,
        "team_h_score": team_h_score, "team_a_score": team_a_score,
        "tackles": tackles, "clearances_blocks_interceptions": cbi, "recoveries": recoveries,
        "defensive_contribution": 0,
        "yellow_cards": 0, "red_cards": 0,
        "bps": 20, "bonus": 0,
        "saves": saves,
    }


def _gate3_history_rows() -> list[dict]:
    rows: list[dict] = []
    for r in range(1, _GATE3_N_HISTORY_ROUNDS + 1):
        away_scores = r % 2 == 1
        team_h_score = 1
        team_a_score = 1 if away_scores else 0
        for element, (position, team) in _GATE3_ROSTER.items():
            goals_scored = assists = 0
            if element == 904:
                goals_scored = 1
            if element == 903:
                assists = 1
            if element == 908 and away_scores:
                goals_scored = 1
            if element == 907 and away_scores:
                assists = 1
            gk_saves = 2 + (r % 3) if position == "GK" else 0
            rows.append(
                _gate3_row(
                    r, element, goals_scored=goals_scored, assists=assists, saves=gk_saves,
                    team_h_score=team_h_score, team_a_score=team_a_score,
                )
            )
    return rows


_GATE3_GAME_CONFIG_PAYLOAD = {
    "rules": {},
    "settings": {},
    "scoring": {
        "goals_scored": {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4},
        "clean_sheets": {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0},
        "goals_conceded": {"GKP": -1, "DEF": -1, "MID": 0, "FWD": 0},
        "defensive_contribution": {"GKP": 0, "DEF": 2, "MID": 2, "FWD": 2},
        "long_play": 2,
        "short_play": 1,
        "saves": 1,
        "assists": 3,
        "bonus": 1,
        "yellow_cards": -1,
        "red_cards": -3,
        "own_goals": -2,
        "penalties_saved": 5,
        "penalties_missed": -2,
    },
}


@pytest.mark.slow
def test_saves_seam_binds_real_model_into_simulate_fixture_points_pmfs(tmp_path):
    from fplai.models.attacking import fit_attacking_model
    from fplai.models.bonus import fit_bonus_model
    from fplai.models.cards import fit_cards_model
    from fplai.models.defensive_contribution import build_dc_threshold_set, fit_dc_model
    from fplai.models.minutes import fit_minutes_model
    from fplai.models.team_strength import fit_team_strength, predict_scoreline
    from fplai.points import SAVES_STATUS_MODELLED, simulate_fixture_points_pmfs
    from fplai.scoring import load_scoring_config

    store = BitemporalStore(base_path=tmp_path / "gate3_store")
    rows = _gate3_history_rows()
    target_round = _GATE3_N_HISTORY_ROUNDS + 1
    target_kickoff = _parse_kickoff(_gate3_kickoff(target_round))  # strictly after every history kickoff

    _write_fixture(store, rows, observed_at=dt(2026, 1, 1))
    store.write(
        "game_config",
        pl.DataFrame({"payload": [json.dumps(_GATE3_GAME_CONFIG_PAYLOAD)]}),
        valid_at=dt(2026, 1, 1),
        observed_at=dt(2026, 1, 1),
        source="test",
    )

    minutes_params = fit_minutes_model(store, as_of=target_kickoff, seasons=[_GATE3_SEASON])
    attacking_params = fit_attacking_model(store, as_of=target_kickoff, seasons=[_GATE3_SEASON])
    threshold_set = build_dc_threshold_set(store, as_of=target_kickoff)
    dc_params = fit_dc_model(store, as_of=target_kickoff, threshold_set=threshold_set, seasons=[_GATE3_SEASON])
    cards_params = fit_cards_model(store, as_of=target_kickoff, seasons=[_GATE3_SEASON])
    bonus_params = fit_bonus_model(store, as_of=target_kickoff, seasons=[_GATE3_SEASON])
    saves_params = fit_saves_model(store, as_of=target_kickoff, seasons=[_GATE3_SEASON])
    team_params = fit_team_strength(store, as_of=target_kickoff, seasons=[_GATE3_SEASON], teams=[_GATE3_TEAM_A, _GATE3_TEAM_B])
    scoreline = predict_scoreline(team_params, _GATE3_TEAM_A, _GATE3_TEAM_B, max_goals=6)
    # scoring_config is read at DECISION time (game_config is CURRENT-season-
    # only, forward-looking -- fplai.scoring's own module docstring, "FORWARD-
    # ONLY, and why"), never via the leakage-safe historical `as_of` every
    # model fit above uses -- game_config was written observed_at=dt(2026,1,1)
    # above, so it must be read at or after that instant, not at the target
    # fixture's own 2022 kickoff.
    scoring_config = load_scoring_config(store, dt(2026, 1, 2))

    # The fixture-level composition entry point (session s006 follow-up
    # dispatch) -- promotes what used to be an open-coded per-player loop
    # here into `fplai.features.assemble_fixture_player_features`. This IS
    # the promotion actually landing, not a parallel path added beside the
    # old one: the loop that used to build `PlayerFixtureFeatures` by hand,
    # one assembler call at a time, is gone from this test.
    roster: Roster = {
        element: (position, team, team == _GATE3_TEAM_A) for element, (position, team) in _GATE3_ROSTER.items()
    }
    assembly_params = FixtureFeatureAssemblyParams(threshold_set=threshold_set)
    players = assemble_fixture_player_features(
        store,
        season=_GATE3_SEASON,
        round=target_round,
        fixture=target_round,
        kickoff_time=target_kickoff,
        roster=roster,
        params=assembly_params,
    )

    # The two per-player rules this brief's own "PROBE OUTPUT" names,
    # asserted directly -- not merely implied by the composition running
    # without error.
    gk_players = [p for p in players if p.position == "GK"]
    outfield_players = [p for p in players if p.position != "GK"]
    assert len(gk_players) == 2
    for p in gk_players:
        assert p.dc_feature_row == {}
        assert p.saves_feature_row is not None
    for p in outfield_players:
        assert p.dc_feature_row != {}
        assert p.saves_feature_row is None

    results = simulate_fixture_points_pmfs(
        fixture=target_round,
        scoreline=scoreline,
        minutes_params=minutes_params,
        attacking_params=attacking_params,
        dc_params=dc_params,
        cards_params=cards_params,
        bonus_params=bonus_params,
        scoring_config=scoring_config,
        players=players,
        saves_predict_fn=make_saves_predict_fn(saves_params),
    )

    assert len(results) == len(players)
    for pmf in results:
        assert abs(sum(pmf.probabilities) - 1.0) < 1e-6
    gk_results = [r for r in results if r.saves_status == SAVES_STATUS_MODELLED]
    assert len(gk_results) == 2  # both GKs drew from the REAL saves model, not NOT_YET_MODELLED


# ---------------------------------------------------------------------------
# 10. Frame-based entry points -- session `s006`, second dispatch of the E6
#    tail (module docstring of `fplai.features`, "Frame-based feature
#    assembly"). Gate 1 (this dispatch's brief): for the same (season,
#    round, element, fixture), frame-based output is FIELD-BY-FIELD
#    IDENTICAL to store-based output -- proved here for every one of the
#    six assemblers plus the fixture-level composition entry point, on
#    real rows (several elements, more than one fixture, more than one
#    season for the single-player assemblers; the gate-3 roster for the
#    composition entry point).
#
# `history` is built the same way `fplai.backtest.replay._build_view`
# builds `GameweekView.history` for a real `Strategy` -- every row of ONE
# season with `round < target round` -- not merely "whatever `_leakage_
# safe_read` itself would have returned", so this is a genuine simulation
# of the frame a backtest `Strategy` will actually be handed, not a
# tautology against the store path's own internals.
# ---------------------------------------------------------------------------


def _history_before_round(raw_all: pl.DataFrame, *, season: str, round_: int) -> pl.DataFrame:
    """The same shape `fplai.backtest.data.rows_before_round`/`replay.
    _build_view` produce for `GameweekView.history`: one season, every row
    with `round < round_`."""
    return raw_all.filter((pl.col("season") == season) & (pl.col("round") < round_))


@pytest.mark.slow
@requires_real_store
def test_minutes_frame_matches_store_based_on_the_real_store():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])
    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 6)][:6]
    candidates = table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3)).sort(
        ["season", "element", "round"]
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    raw_all = read_player_gameweek_stats(real_store, as_of=as_of)
    for target in picked:
        history = _history_before_round(raw_all, season=target["season"], round_=target["round"])
        kwargs = dict(
            season=target["season"], round=target["round"], element=target["element"], fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]), team=target["team"], position=target["position"],
            was_home=bool(target["was_home"]),
        )
        store_based = assemble_minutes_feature_row(real_store, **kwargs)
        frame_based = assemble_minutes_feature_row_from_frame(history, **kwargs)
        assert store_based == frame_based


@pytest.mark.slow
@requires_real_store
def test_attacking_frame_matches_store_based_on_the_real_store():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_attacking_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])
    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 6)][:6]
    candidates = table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3)).sort(
        ["season", "element", "round"]
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    raw_all = read_player_gameweek_stats(real_store, as_of=as_of)
    for target in picked:
        history = _history_before_round(raw_all, season=target["season"], round_=target["round"])
        kwargs = dict(
            season=target["season"], round=target["round"], element=target["element"], fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]), team=target["team"], position=target["position"],
            was_home=bool(target["was_home"]),
        )
        store_based = assemble_attacking_feature_row(real_store, **kwargs)
        frame_based = assemble_attacking_feature_row_from_frame(history, **kwargs)
        assert store_based == frame_based


@pytest.mark.slow
@requires_real_store
def test_cards_frame_matches_store_based_on_the_real_store():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_cards_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])
    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 6)][:6]
    candidates = table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3)).sort(
        ["season", "element", "round"]
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    raw_all = read_player_gameweek_stats(real_store, as_of=as_of)
    for target in picked:
        history = _history_before_round(raw_all, season=target["season"], round_=target["round"])
        kwargs = dict(
            season=target["season"], round=target["round"], element=target["element"], fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]), team=target["team"], position=target["position"],
            was_home=bool(target["was_home"]),
        )
        store_based = assemble_cards_feature_row(real_store, **kwargs)
        frame_based = assemble_cards_feature_row_from_frame(history, **kwargs)
        assert store_based == frame_based


@pytest.mark.slow
@requires_real_store
def test_dc_frame_matches_store_based_on_the_real_store():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_dc_training_table(real_store, as_of=as_of, seasons=["2025-26"])
    threshold_set = build_dc_threshold_set(real_store, as_of=as_of)
    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 6)][:6]
    candidates = table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3)).sort(
        ["season", "element", "round"]
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    raw_all = read_player_gameweek_stats(real_store, as_of=as_of)
    for target in picked:
        history = _history_before_round(raw_all, season=target["season"], round_=target["round"])
        kwargs = dict(
            season=target["season"], round=target["round"], element=target["element"], fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]), team=target["team"], position=target["position"],
            was_home=bool(target["was_home"]),
        )
        store_based = assemble_dc_feature_row(real_store, threshold_set=threshold_set, **kwargs)
        frame_based = assemble_dc_feature_row_from_frame(history, threshold_set=threshold_set, **kwargs)
        assert store_based == frame_based


@pytest.mark.slow
@requires_real_store
def test_bonus_frame_matches_store_based_on_the_real_store():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_bonus_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])
    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 6)][:6]
    candidates = table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3)).sort(
        ["season", "element", "round"]
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    raw_all = read_player_gameweek_stats(real_store, as_of=as_of)
    for target in picked:
        history = _history_before_round(raw_all, season=target["season"], round_=target["round"])
        kwargs = dict(
            season=target["season"], round=target["round"], element=target["element"], fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]), team=target["team"], position=target["position"],
            was_home=bool(target["was_home"]),
        )
        store_based = assemble_bonus_feature_row(real_store, **kwargs)
        frame_based = assemble_bonus_feature_row_from_frame(history, **kwargs)
        assert store_based == frame_based


@pytest.mark.slow
@requires_real_store
def test_saves_frame_matches_store_based_on_the_real_store():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    table = build_saves_training_table(real_store, as_of=as_of, seasons=["2022-23", "2023-24", "2025-26"])
    elements = table["element"].unique().sort().to_list()
    sample_elements = elements[:: max(1, len(elements) // 10)][:10]
    candidates = table.filter(pl.col("element").is_in(sample_elements) & (pl.col("round") > 3)).sort(
        ["season", "element", "round"]
    )
    picked = []
    for e in sample_elements:
        sub = candidates.filter(pl.col("element") == e)
        if sub.height >= 2:
            picked.extend(sub.head(2).to_dicts())
    assert len(picked) >= 10

    raw_all = read_player_gameweek_stats(real_store, as_of=as_of)
    for target in picked:
        history = _history_before_round(raw_all, season=target["season"], round_=target["round"])
        kwargs = dict(
            season=target["season"], round=target["round"], element=target["element"], fixture=target["fixture"],
            kickoff_time=_parse_kickoff(target["kickoff_time"]), team=target["team"], position=target["position"],
            was_home=bool(target["was_home"]),
        )
        store_based = assemble_saves_feature_row(real_store, **kwargs)
        frame_based = assemble_saves_feature_row_from_frame(history, **kwargs)
        assert store_based == frame_based


# ---------------------------------------------------------------------------
# 11. Fast, synthetic-store equivalence for the frame-based path -- not a
#    substitute for the real-store gate above, but cheap coverage that does
#    not require `data/store/` to be present, run on every `pytest`
#    invocation rather than only the `slow` ones.
# ---------------------------------------------------------------------------


def test_minutes_frame_matches_store_based_on_synthetic_history(temp_store):
    rows = _five_round_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    raw_all = read_player_gameweek_stats(temp_store, as_of=dt(2026, 1, 2))
    for r in rows:
        history = _history_before_round(raw_all, season=r["season"], round_=r["round"])
        kwargs = dict(
            season=r["season"], round=r["round"], element=r["element"], fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]), team=r["team"], position=r["position"],
            was_home=r["was_home"],
        )
        assert assemble_minutes_feature_row(temp_store, **kwargs) == assemble_minutes_feature_row_from_frame(
            history, **kwargs
        )


def test_attacking_frame_matches_store_based_on_synthetic_history(temp_store):
    rows = _five_round_attacking_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    raw_all = read_player_gameweek_stats(temp_store, as_of=dt(2026, 1, 2))
    for r in rows:
        history = _history_before_round(raw_all, season=r["season"], round_=r["round"])
        kwargs = dict(
            season=r["season"], round=r["round"], element=r["element"], fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]), team=r["team"], position=r["position"],
            was_home=r["was_home"],
        )
        assert assemble_attacking_feature_row(temp_store, **kwargs) == assemble_attacking_feature_row_from_frame(
            history, **kwargs
        )


def test_cards_frame_matches_store_based_on_synthetic_history(temp_store):
    rows = _five_round_cards_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    raw_all = read_player_gameweek_stats(temp_store, as_of=dt(2026, 1, 2))
    for r in rows:
        history = _history_before_round(raw_all, season=r["season"], round_=r["round"])
        kwargs = dict(
            season=r["season"], round=r["round"], element=r["element"], fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]), team=r["team"], position=r["position"],
            was_home=r["was_home"],
        )
        assert assemble_cards_feature_row(temp_store, **kwargs) == assemble_cards_feature_row_from_frame(
            history, **kwargs
        )


def test_dc_frame_matches_store_based_on_synthetic_history(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    rows = _five_round_dc_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    threshold_set = build_dc_threshold_set(temp_store, as_of=dt(2026, 1, 2))
    raw_all = read_player_gameweek_stats(temp_store, as_of=dt(2026, 1, 2))
    for r in rows:
        history = _history_before_round(raw_all, season=r["season"], round_=r["round"])
        kwargs = dict(
            season=r["season"], round=r["round"], element=r["element"], fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]), team=r["team"], position=r["position"],
            was_home=r["was_home"],
        )
        assert assemble_dc_feature_row(
            temp_store, threshold_set=threshold_set, **kwargs
        ) == assemble_dc_feature_row_from_frame(history, threshold_set=threshold_set, **kwargs)


def test_bonus_frame_matches_store_based_on_synthetic_history(temp_store):
    rows = _five_round_bonus_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    raw_all = read_player_gameweek_stats(temp_store, as_of=dt(2026, 1, 2))
    for r in rows:
        history = _history_before_round(raw_all, season=r["season"], round_=r["round"])
        kwargs = dict(
            season=r["season"], round=r["round"], element=r["element"], fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]), team=r["team"], position=r["position"],
            was_home=r["was_home"],
        )
        assert assemble_bonus_feature_row(temp_store, **kwargs) == assemble_bonus_feature_row_from_frame(
            history, **kwargs
        )


def test_saves_frame_matches_store_based_on_synthetic_history(temp_store):
    rows = _five_round_saves_history_rows()
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    raw_all = read_player_gameweek_stats(temp_store, as_of=dt(2026, 1, 2))
    for r in rows:
        history = _history_before_round(raw_all, season=r["season"], round_=r["round"])
        kwargs = dict(
            season=r["season"], round=r["round"], element=r["element"], fixture=r["fixture"],
            kickoff_time=_parse_kickoff(r["kickoff_time"]), team=r["team"], position=r["position"],
            was_home=r["was_home"],
        )
        assert assemble_saves_feature_row(temp_store, **kwargs) == assemble_saves_feature_row_from_frame(
            history, **kwargs
        )


def test_frame_based_kickoff_time_must_be_timezone_aware(temp_store):
    """The one guarantee the frame-based path still enforces itself
    (module docstring, "Frame-based feature assembly") -- attacked for
    each of the six `_from_frame` entry points, not assumed shared."""
    empty = pl.DataFrame()
    naive = datetime(2022, 8, 6, 14, 0)
    common = dict(season="2022-23", round=1, element=1, fixture=1, kickoff_time=naive, team="TeamA", was_home=True)
    with pytest.raises(FeatureAssemblyError):
        assemble_minutes_feature_row_from_frame(empty, position="MID", **common)
    with pytest.raises(FeatureAssemblyError):
        assemble_attacking_feature_row_from_frame(empty, position="MID", **common)
    with pytest.raises(FeatureAssemblyError):
        assemble_cards_feature_row_from_frame(empty, position="MID", **common)
    with pytest.raises(FeatureAssemblyError):
        assemble_bonus_feature_row_from_frame(empty, position="MID", **common)
    with pytest.raises(FeatureAssemblyError):
        assemble_saves_feature_row_from_frame(empty, position="GK", **common)


def test_dc_frame_based_ineligible_position_refuses_identically_to_store_based(temp_store):
    """DC's frame-based entry has no store to fall back to for a default
    `threshold_set` -- it is a required argument -- but the eligibility
    refusal itself must fire identically to the store-based path."""
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    threshold_set = build_dc_threshold_set(temp_store, as_of=dt(2026, 1, 2))
    with pytest.raises(FeatureAssemblyError):
        assemble_dc_feature_row_from_frame(
            pl.DataFrame(),
            threshold_set=threshold_set,
            season="2022-23",
            round=1,
            element=999,
            fixture=1,
            kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
            team="TeamZ",
            position="GK",
            was_home=False,
        )


def test_saves_frame_based_non_gk_position_refuses_identically_to_store_based():
    with pytest.raises(FeatureAssemblyError):
        assemble_saves_feature_row_from_frame(
            pl.DataFrame(),
            season="2022-23",
            round=1,
            element=999,
            fixture=1,
            kickoff_time=_parse_kickoff("2022-08-06T14:00:00Z"),
            team="TeamZ",
            position="MID",
            was_home=False,
        )


# ---------------------------------------------------------------------------
# 12. `assemble_fixture_player_features_from_frame` -- the composition
#    entry point the model-stack strategy story that follows this one will
#    actually call. Reuses gate 3's own real-model-fit roster/history so
#    this proves equivalence against `assemble_fixture_player_features`'s
#    OWN real output, not a hand-rolled comparison.
# ---------------------------------------------------------------------------


def test_fixture_player_features_from_frame_matches_store_based(tmp_path):
    store = BitemporalStore(base_path=tmp_path / "frame_composition_store")
    rows = _gate3_history_rows()
    target_round = _GATE3_N_HISTORY_ROUNDS + 1
    target_kickoff = _parse_kickoff(_gate3_kickoff(target_round))

    _write_fixture(store, rows, observed_at=dt(2026, 1, 1))
    store.write(
        "game_config",
        pl.DataFrame({"payload": [json.dumps(_GATE3_GAME_CONFIG_PAYLOAD)]}),
        valid_at=dt(2026, 1, 1),
        observed_at=dt(2026, 1, 1),
        source="test",
    )

    threshold_set = build_dc_threshold_set(store, as_of=target_kickoff)
    roster: Roster = {
        element: (position, team, team == _GATE3_TEAM_A) for element, (position, team) in _GATE3_ROSTER.items()
    }
    assembly_params = FixtureFeatureAssemblyParams(threshold_set=threshold_set)

    store_based = assemble_fixture_player_features(
        store, season=_GATE3_SEASON, round=target_round, fixture=target_round, kickoff_time=target_kickoff,
        roster=roster, params=assembly_params,
    )

    # `history` built the same way `replay._build_view` builds
    # `GameweekView.history` for a real `Strategy` -- one season, every row
    # strictly before the target round.
    raw_all = read_player_gameweek_stats(store, as_of=target_kickoff)
    history = _history_before_round(raw_all, season=_GATE3_SEASON, round_=target_round)
    frame_based = assemble_fixture_player_features_from_frame(
        history, season=_GATE3_SEASON, round=target_round, fixture=target_round, kickoff_time=target_kickoff,
        roster=roster, params=assembly_params,
    )

    assert len(store_based) == len(frame_based) == len(roster)
    for a, b in zip(store_based, frame_based):
        assert a.element == b.element
        assert a.position == b.position
        assert a.team == b.team
        assert a.is_home == b.is_home
        assert a.minutes_feature_row == b.minutes_feature_row
        assert a.attacking_feature_row == b.attacking_feature_row
        assert a.dc_feature_row == b.dc_feature_row
        assert a.cards_feature_row == b.cards_feature_row
        assert a.bonus_feature_row == b.bonus_feature_row
        assert a.saves_feature_row == b.saves_feature_row


# ---------------------------------------------------------------------------
# 13. S3 pilot -- the `schedule` parameter threaded onto the minutes
#    assembler only (`fplai.optimiser.build_decision_calendar`'s consumer
#    side). This story's own brief PROBE (a)/(d) reproduced with synthetic
#    numbers here, and PROBE (c) (only minutes reads it) attacked directly
#    at the composition level.
# ---------------------------------------------------------------------------


def test_schedule_none_matches_todays_stale_gap_and_a_real_schedule_corrects_it():
    """PROBE (a) reproduced synthetically: a `history` frame frozen several
    rounds before the target round sees only a STALE previous fixture for
    the team when `schedule` is `None` -- the exact defect this story's
    brief documents. `schedule=None` must reproduce that stale value
    byte-for-byte (the equivalence half); supplying the real intervening
    fixture corrects it (the fix half) -- both asserted here so the
    equivalence check is not vacuous (CLAUDE.md lesson 5: prove a new test
    can fail before trusting it passes)."""
    season = "2025-26"
    team = "Arsenal"
    element = 999
    # `history` only knows about round 19's Arsenal fixture -- as if this
    # were assembled for a round-19 decision, projecting a candidate out to
    # a round-23 target the receding horizon needs (this story's whole
    # reason for existing).
    history = pl.DataFrame(
        [_row(season, 19, element, 190, "2026-01-01T15:00:00Z", team=team, was_home=True, minutes=90, starts=1)]
    )
    target_kickoff = _parse_kickoff("2026-01-25T16:30:00Z")
    common = dict(
        season=season, round=23, element=element, fixture=230,
        kickoff_time=target_kickoff, team=team, position="GK", was_home=True,
    )

    stale = assemble_minutes_feature_row_from_frame(history, **common)
    # Naive gap: 2026-01-01T15:00 -> 2026-01-25T16:30 = 24 days + 1.5h = 24.0625 days.
    assert abs(stale["days_since_team_previous_fixture"] - 24.0625) < 1e-6

    explicit_none = assemble_minutes_feature_row_from_frame(history, schedule=None, **common)
    assert explicit_none == stale

    # The decision calendar's own shape (`season`, `team`, `fixture`,
    # `kickoff_time`) -- Arsenal's rounds 20-23, matching real fixture
    # cadence (one PL fixture per team per round).
    schedule = pl.DataFrame(
        {
            "season": [season, season, season, season],
            "team": [team, team, team, team],
            "fixture": [200, 210, 220, 230],
            "kickoff_time": [
                "2026-01-04T15:00:00Z",
                "2026-01-11T15:00:00Z",
                "2026-01-17T17:30:00Z",
                "2026-01-25T16:30:00Z",
            ],
        }
    )
    corrected = assemble_minutes_feature_row_from_frame(history, schedule=schedule, **common)
    # Corrected gap: round 23's real previous fixture is round 22's kickoff,
    # 2026-01-17T17:30 -> 2026-01-25T16:30 = 7 days + 23h = 7.958333 days.
    assert abs(corrected["days_since_team_previous_fixture"] - 7.958333) < 1e-4
    assert corrected["days_since_team_previous_fixture"] != stale["days_since_team_previous_fixture"]
    assert corrected["team_first_fixture_in_window"] is False


def test_dgw_second_leg_gap_uses_leg_one_kickoff_once_schedule_supplies_it():
    """PROBE (d) reproduced with the brief's own real numbers (2025-26
    round 33, Bournemouth's double gameweek). Without leg one's own kickoff
    visible, leg two's gap falls back to a stale earlier-round fixture
    (11.3125 days); once `schedule` supplies leg one, the gap becomes leg
    two minus leg one (4.2083 days) -- pinned decision D4: this CHANGES the
    DGW round-t value deliberately, no compatibility flag preserves the old
    number."""
    season = "2025-26"
    team = "Bournemouth"
    element = 555
    history = pl.DataFrame(
        [_row(season, 30, element, 300, "2026-04-11T11:30:00Z", team=team, was_home=True, minutes=90, starts=1)]
    )
    leg2_kickoff = _parse_kickoff("2026-04-22T19:00:00Z")
    common = dict(
        season=season, round=33, element=element, fixture=332,
        kickoff_time=leg2_kickoff, team=team, position="MID", was_home=False,
    )

    stale = assemble_minutes_feature_row_from_frame(history, **common)
    assert abs(stale["days_since_team_previous_fixture"] - 11.3125) < 1e-6

    schedule = pl.DataFrame(
        {"season": [season], "team": [team], "fixture": [328], "kickoff_time": ["2026-04-18T14:00:00Z"]}
    )
    corrected = assemble_minutes_feature_row_from_frame(history, schedule=schedule, **common)
    assert abs(corrected["days_since_team_previous_fixture"] - 4.208333) < 1e-4
    assert corrected["days_since_team_previous_fixture"] != stale["days_since_team_previous_fixture"]


def test_schedule_affects_only_minutes_feature_row_in_composition(tmp_path):
    """PROBE (c) attacked directly at the composition level: the other five
    assemblers (attacking, dc, cards, bonus, saves) have no gap feature at
    all, so threading a `schedule` that meaningfully changes team A's rest
    gap through `assemble_fixture_player_features_from_frame` must change
    ONLY `minutes_feature_row` -- for every element, including team B's,
    which the schedule below never mentions at all (pinned decision D3)."""
    store = BitemporalStore(base_path=tmp_path / "schedule_composition_store")
    rows = _gate3_history_rows()
    target_round = _GATE3_N_HISTORY_ROUNDS + 1
    target_kickoff = _parse_kickoff(_gate3_kickoff(target_round))

    _write_fixture(store, rows, observed_at=dt(2026, 1, 1))
    store.write(
        "game_config",
        pl.DataFrame({"payload": [json.dumps(_GATE3_GAME_CONFIG_PAYLOAD)]}),
        valid_at=dt(2026, 1, 1),
        observed_at=dt(2026, 1, 1),
        source="test",
    )

    threshold_set = build_dc_threshold_set(store, as_of=target_kickoff)
    roster: Roster = {
        element: (position, team, team == _GATE3_TEAM_A) for element, (position, team) in _GATE3_ROSTER.items()
    }
    assembly_params = FixtureFeatureAssemblyParams(threshold_set=threshold_set)

    raw_all = read_player_gameweek_stats(store, as_of=target_kickoff)
    history = _history_before_round(raw_all, season=_GATE3_SEASON, round_=target_round)

    baseline = assemble_fixture_player_features_from_frame(
        history, season=_GATE3_SEASON, round=target_round, fixture=target_round, kickoff_time=target_kickoff,
        roster=roster, params=assembly_params,
    )

    # Team A's real previous fixture in `history` is round `target_round - 1`
    # (one week before, per `_gate3_kickoff`'s weekly cadence). This extra
    # fixture is 2 days before the target -- much closer -- so if it were
    # read by ANY assembler other than minutes, that assembler's output
    # would change too; probe (c) says none of the other five even look.
    from datetime import timedelta

    two_days_before = (target_kickoff - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    schedule = pl.DataFrame(
        {
            "season": [_GATE3_SEASON],
            "team": [_GATE3_TEAM_A],
            "fixture": [target_round + 500],
            "kickoff_time": [two_days_before],
        }
    )

    with_schedule = assemble_fixture_player_features_from_frame(
        history, season=_GATE3_SEASON, round=target_round, fixture=target_round, kickoff_time=target_kickoff,
        roster=roster, params=assembly_params, schedule=schedule,
    )

    assert len(baseline) == len(with_schedule) == len(roster)
    any_minutes_row_changed = False
    for a, b in zip(baseline, with_schedule):
        assert a.element == b.element
        assert a.attacking_feature_row == b.attacking_feature_row
        assert a.dc_feature_row == b.dc_feature_row
        assert a.cards_feature_row == b.cards_feature_row
        assert a.bonus_feature_row == b.bonus_feature_row
        assert a.saves_feature_row == b.saves_feature_row
        if a.team == _GATE3_TEAM_A:
            assert (
                a.minutes_feature_row["days_since_team_previous_fixture"]
                != b.minutes_feature_row["days_since_team_previous_fixture"]
            )
            any_minutes_row_changed = True
        else:
            # Team B is never mentioned in `schedule` -- its own minutes
            # row must be untouched.
            assert a.minutes_feature_row == b.minutes_feature_row
    assert any_minutes_row_changed, "the schedule fixture never changed any team-A minutes row -- test is vacuous"


# ---------------------------------------------------------------------------
# 14. S3a landed as tests (story S3a-b, session s007). S3a shipped twelve
#    new public definitions in this module (the six `build_*_roster_
#    rollup` builders, their `*RosterRollup` dataclasses, and the
#    `precomputed` parameter threaded onto all six `_assemble_*_feature_
#    row_from_raw` functions) with this file untouched -- CLAUDE.md's own
#    standing rule ("a guarantee is not proven by a test that follows the
#    sanctioned path") therefore had nothing behind it for this story until
#    now. The four tests below port the Architect's own hand-run attacks
#    against the real store verbatim (this story's brief); all five
#    described in the brief are covered -- the DC attack is both halves of
#    one test (ineligible-must-raise, eligible-must-succeed), so four test
#    functions, five assertions of the brief's own numbering.
# ---------------------------------------------------------------------------


@pytest.mark.slow
@requires_real_store
def test_dc_roster_rollup_rejects_an_ineligible_element_and_not_vacuously():
    """Ported from the Architect's own real-store attack (this story's
    brief, ATTACK 1/2): `build_dc_roster_rollup` handed a GK -- never DC-
    eligible, `POSITION_GROUP` (fplai.models.defensive_contribution) has no
    "GK" key at all, independent of `threshold_set`/season -- must raise
    `FeatureAssemblyError` naming both the element and its position. The
    non-vacuous half: the identical call shape with a DC-eligible DEF
    roster must SUCCEED, so the raise above is proven to be about
    eligibility specifically, not some unrelated construction failure that
    would fire for any roster at all (CLAUDE.md: a guard proven by an
    attack that fires a different guard is not proven at all)."""
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    threshold_set = build_dc_threshold_set(real_store, as_of=as_of)
    # `seasons=["2025-26"]` pins DATASET SCOPE the same way every other DC
    # real-store test in this file already does (DC counters only exist for
    # 2025-26, module docstring) -- the element/round/team looked up WITHIN
    # that scope is derived below, never a literal.
    minutes_table = build_training_table(real_store, as_of=as_of, seasons=["2025-26"])
    dc_table = build_dc_training_table(real_store, as_of=as_of, seasons=["2025-26"])
    raw_all = read_player_gameweek_stats(real_store, as_of=as_of)

    gk_row = minutes_table.filter(pl.col("position") == "GK").sort(["element", "round"]).row(0, named=True)
    # `dc_table` is already filtered to DC-eligible positions only (module
    # docstring of build_training_table, "for DC-eligible positions only"),
    # so any DEF row it carries is guaranteed eligible under this same
    # `as_of`'s threshold_set -- no need to re-derive eligibility here.
    def_row = dc_table.filter(pl.col("position") == "DEF").sort(["element", "round"]).row(0, named=True)

    gk_history = _history_before_round(raw_all, season=gk_row["season"], round_=gk_row["round"])
    with pytest.raises(FeatureAssemblyError) as excinfo:
        F.build_dc_roster_rollup(
            gk_history,
            threshold_set=threshold_set,
            season=gk_row["season"],
            round=gk_row["round"],
            roster={gk_row["element"]: (gk_row["position"], gk_row["team"], True)},
        )
    assert type(excinfo.value) is FeatureAssemblyError
    assert str(gk_row["element"]) in str(excinfo.value)
    assert gk_row["position"] in str(excinfo.value)

    def_history = _history_before_round(raw_all, season=def_row["season"], round_=def_row["round"])
    result = F.build_dc_roster_rollup(
        def_history,
        threshold_set=threshold_set,
        season=def_row["season"],
        round=def_row["round"],
        roster={def_row["element"]: (def_row["position"], def_row["team"], True)},
    )
    assert result.player_rollup.height >= 1
    assert result.team_rollup.height >= 1


@pytest.mark.slow
@requires_real_store
def test_saves_roster_rollup_rejects_a_non_gk():
    """Ported from the Architect's own real-store attack (this story's
    brief, ATTACK 3): `build_saves_roster_rollup` handed a non-GK must
    raise `FeatureAssemblyError`, mirroring `assemble_saves_feature_row_
    from_frame`'s own per-element GK-only refusal (module docstring,
    "Sixth, and last, instance")."""
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    minutes_table = build_training_table(real_store, as_of=as_of, seasons=["2025-26"])
    def_row = minutes_table.filter(pl.col("position") == "DEF").sort(["element", "round"]).row(0, named=True)
    raw_all = read_player_gameweek_stats(real_store, as_of=as_of)
    history = _history_before_round(raw_all, season=def_row["season"], round_=def_row["round"])

    with pytest.raises(FeatureAssemblyError) as excinfo:
        F.build_saves_roster_rollup(
            history,
            season=def_row["season"],
            round=def_row["round"],
            roster={def_row["element"]: ("DEF", def_row["team"], True)},
        )
    assert type(excinfo.value) is FeatureAssemblyError
    assert str(def_row["element"]) in str(excinfo.value)


def _pick_real_single_fixture_roster(real_store: BitemporalStore, as_of: datetime):
    """One real fixture (season/round/fixture id) with at least 16 rostered
    elements recorded in vaastav's data for it -- both squads' full listed
    players, not just the starting 11 (verified live this session: the
    largest 2025-26 fixture carries 100 distinct elements, one row each),
    so real single fixtures comfortably clear this without padding.

    Picked deterministically -- the single largest fixture in 2025-26 by
    row count, ties broken by (season, round, fixture) -- never a literal
    element/round/fixture id (CLAUDE.md: a test asserting against a literal
    date or gameweek is a scheduled false alarm). `build_minutes_roster_
    rollup` itself takes ONE scalar `fixture`/`kickoff_time` per call
    (S3a's own module comment: it is scoped to one `assemble_fixture_
    player_features_from_frame` call's roster, i.e. one fixture's two
    teams), so "roster" here is deliberately every rostered element of ONE
    real match, not a spread across a gameweek's several fixtures."""
    table = build_training_table(real_store, as_of=as_of, seasons=["2025-26"])
    counts = (
        table.group_by(["season", "round", "fixture"])
        .agg(pl.len().alias("n"))
        .sort(["n", "season", "round", "fixture"], descending=[True, False, False, False])
    )
    top = counts.row(0, named=True)
    fixture_rows = table.filter(
        (pl.col("season") == top["season"]) & (pl.col("round") == top["round"]) & (pl.col("fixture") == top["fixture"])
    ).sort("element")
    assert fixture_rows.height >= 16, (
        f"largest 2025-26 fixture only carries {fixture_rows.height} rostered elements -- picker needs rework"
    )

    raw_all = read_player_gameweek_stats(real_store, as_of=as_of)
    history = _history_before_round(raw_all, season=top["season"], round_=top["round"])
    kickoff_time = _parse_kickoff(fixture_rows.row(0, named=True)["kickoff_time"])

    roster_rows = fixture_rows.head(15).to_dicts()
    outsider_row = fixture_rows.row(15, named=True)
    return top["season"], top["round"], top["fixture"], kickoff_time, history, roster_rows, outsider_row


@pytest.mark.slow
@requires_real_store
def test_minutes_precomputed_rollup_agrees_with_the_default_path_element_by_element():
    """Ported from the Architect's own real-store attack (this story's
    brief, ATTACK 4): 15 real elements from the same real fixture, threaded
    through `build_minutes_roster_rollup` -> `assemble_minutes_feature_row_
    from_frame(precomputed=...)`, must match that same function's own
    per-element default path (`precomputed=None`) EXACTLY, element by
    element -- the equivalence half S3a's own module comment argues from
    `.over()` partitioning alone; CLAUDE.md requires attacking a guarantee
    from outside the sanctioned path rather than trusting the argument, so
    this checks the actual output rather than the reasoning for it."""
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    season, round_, fixture_id, kickoff_time, history, roster_rows, _outsider_row = _pick_real_single_fixture_roster(
        real_store, as_of
    )
    roster: Roster = {r["element"]: (r["position"], r["team"], bool(r["was_home"])) for r in roster_rows}

    pre = F.build_minutes_roster_rollup(
        history, season=season, round=round_, fixture=fixture_id, kickoff_time=kickoff_time, roster=roster
    )

    mismatches = []
    for r in roster_rows:
        common = dict(
            season=season, round=round_, element=r["element"], fixture=fixture_id, kickoff_time=kickoff_time,
            team=r["team"], position=r["position"], was_home=bool(r["was_home"]),
        )
        a = assemble_minutes_feature_row_from_frame(history, **common)
        b = assemble_minutes_feature_row_from_frame(history, precomputed=pre, **common)
        if a != b:
            mismatches.append((r["element"], a, b))
    assert not mismatches, f"precomputed/default mismatch(es): {mismatches}"


@pytest.mark.slow
@requires_real_store
def test_minutes_precomputed_rollup_raises_for_an_element_it_was_never_built_for():
    """THE IMPORTANT ONE (this story's brief, ATTACK 5): a `precomputed`
    rollup asked for an element outside the roster it was built for must
    RAISE, never silently return a wrong row -- the dangerous failure mode
    of any hoist. Ported from the Architect's own real-store attack; see
    this test's own module-level Part-1 rewording for why the raise stays
    correct even though the OLD "structurally impossible" explanation
    (still true for `precomputed=None`) no longer applies on this path."""
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    season, round_, fixture_id, kickoff_time, history, roster_rows, outsider_row = _pick_real_single_fixture_roster(
        real_store, as_of
    )
    roster: Roster = {r["element"]: (r["position"], r["team"], bool(r["was_home"])) for r in roster_rows}
    assert outsider_row["element"] not in roster  # the attack is only meaningful if this holds

    pre = F.build_minutes_roster_rollup(
        history, season=season, round=round_, fixture=fixture_id, kickoff_time=kickoff_time, roster=roster
    )

    outsider_common = dict(
        season=season, round=round_, element=outsider_row["element"], fixture=fixture_id, kickoff_time=kickoff_time,
        team=outsider_row["team"], position=outsider_row["position"], was_home=bool(outsider_row["was_home"]),
    )
    with pytest.raises(FeatureAssemblyError) as excinfo:
        assemble_minutes_feature_row_from_frame(history, precomputed=pre, **outsider_common)
    assert type(excinfo.value) is FeatureAssemblyError
    # After this story's Part 1 reword: assert something STABLE about the
    # message (it names the element) rather than pinning brittle prose.
    assert str(outsider_row["element"]) in str(excinfo.value)
