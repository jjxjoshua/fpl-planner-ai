"""Tests for fplai.scoring — Phase 3 prerequisite 1 of 2, session `s005`
(blueprint §7.1/§7.2, CLAUDE.md rules 4/5/7).

Four groups:

  1. `RealisedOutcome` validation — non-negative counts, the bonus 0..3
     bound.
  2. `load_scoring_config` — parsing, missing-key errors, the GKP->GK
     remap, the naive-datetime guard, and the forward-only guard (an
     `as_of` for which no `game_config` observation exists must raise,
     never silently fall back).
  3. `score_outcome` — pure-arithmetic unit tests isolating ONE identifier
     at a time (so a future regression points at exactly which coefficient
     broke), the unknown-position guard, and the clean-sheet/minutes
     consistency guard.
  4. Real-settled-GW1 regression fixtures — 19 real rows from
     `event/1/live/` (GW1 2026/27, `finished=True, data_checked=True`),
     hand-copied from a live verification run this session (module
     docstring, "Verification"), each asserted to reproduce FPL's own
     `total_points` exactly. Pinned as literal data so this proof does not
     require a live network call to re-run — the live call itself (610/610
     exact match across the WHOLE gameweek, not just these 19) is recorded
     in `.punchcard/s005.jsonl`, not repeated here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from fplai.gameweek_stats import read_player_gameweek_stats
from fplai.models.defensive_contribution import POSITION_GROUP, build_dc_threshold_set
from fplai.scoring import (
    GOALS_CONCEDED_POINTS_DIVISOR,
    MINUTE_CLIFF,
    POSITIONS,
    SAVES_POINTS_DIVISOR,
    RealisedOutcome,
    ScoringConfig,
    ScoringError,
    load_scoring_config,
    score_outcome,
)
from fplai.store import BitemporalStore

UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Real 2026/27 scoring config — same shape `game_config`'s real payload
# carries (verified live this session), used everywhere below instead of a
# hand-invented smaller one so unit tests exercise the actual rule set.
# ---------------------------------------------------------------------------

REAL_SCORING_PAYLOAD = {
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


@pytest.fixture
def temp_store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def _write_game_config(store: BitemporalStore, *, observed_at: datetime, payload: dict | None = None) -> None:
    import json

    payload = payload if payload is not None else REAL_SCORING_PAYLOAD
    store.write(
        "game_config",
        pl.DataFrame({"payload": [json.dumps(payload)]}),
        valid_at=observed_at,
        observed_at=observed_at,
        source="test",
    )


@pytest.fixture
def real_config() -> ScoringConfig:
    """A `ScoringConfig` built directly from `REAL_SCORING_PAYLOAD` without
    going through the store — used by group 3/4's pure-arithmetic tests,
    which care about `score_outcome`'s math, not the loader."""
    scoring = REAL_SCORING_PAYLOAD["scoring"]
    return ScoringConfig(
        valid_as_of=dt(2026, 8, 29),
        observed_at=dt(2026, 8, 28, hour=23),
        goals_scored={"GK": 10, "DEF": 6, "MID": 5, "FWD": 4},
        clean_sheets={"GK": 4, "DEF": 4, "MID": 1, "FWD": 0},
        goals_conceded={"GK": -1, "DEF": -1, "MID": 0, "FWD": 0},
        defensive_contribution={"GK": 0, "DEF": 2, "MID": 2, "FWD": 2},
        long_play=scoring["long_play"],
        short_play=scoring["short_play"],
        saves=scoring["saves"],
        assists=scoring["assists"],
        bonus=scoring["bonus"],
        yellow_cards=scoring["yellow_cards"],
        red_cards=scoring["red_cards"],
        own_goals=scoring["own_goals"],
        penalties_saved=scoring["penalties_saved"],
        penalties_missed=scoring["penalties_missed"],
    )


# ---------------------------------------------------------------------------
# 1. RealisedOutcome validation
# ---------------------------------------------------------------------------


def test_realised_outcome_defaults_to_a_zero_outcome():
    o = RealisedOutcome()
    assert o.minutes == 0
    assert o.clean_sheet is False
    assert o.defensive_contribution_met is False


@pytest.mark.parametrize(
    "field",
    ["minutes", "goals_scored", "assists", "goals_conceded", "own_goals", "penalties_saved", "penalties_missed", "yellow_cards", "red_cards", "saves", "bonus"],
)
def test_realised_outcome_rejects_negative_counts(field):
    with pytest.raises(ScoringError):
        RealisedOutcome(**{field: -1})


def test_realised_outcome_rejects_bonus_above_three():
    with pytest.raises(ScoringError):
        RealisedOutcome(bonus=4)


def test_realised_outcome_accepts_bonus_of_three():
    RealisedOutcome(bonus=3)  # must not raise


# ---------------------------------------------------------------------------
# 2. load_scoring_config
# ---------------------------------------------------------------------------


def test_load_scoring_config_reads_live_values_and_remaps_gkp_to_gk(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 8, 20))
    cfg = load_scoring_config(temp_store, dt(2026, 8, 29))
    assert cfg.goals_scored == {"GK": 10, "DEF": 6, "MID": 5, "FWD": 4}
    assert cfg.clean_sheets == {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}
    assert cfg.goals_conceded == {"GK": -1, "DEF": -1, "MID": 0, "FWD": 0}
    assert cfg.defensive_contribution == {"GK": 0, "DEF": 2, "MID": 2, "FWD": 2}
    assert (cfg.long_play, cfg.short_play, cfg.saves, cfg.assists, cfg.bonus) == (2, 1, 1, 3, 1)
    assert (cfg.yellow_cards, cfg.red_cards, cfg.own_goals) == (-1, -3, -2)
    assert (cfg.penalties_saved, cfg.penalties_missed) == (5, -2)


def test_load_scoring_config_rejects_naive_datetime(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 8, 20))
    with pytest.raises(ScoringError):
        load_scoring_config(temp_store, datetime(2026, 8, 29))  # naive, no tzinfo


def test_load_scoring_config_raises_when_no_game_config_observation_exists_at_or_before_as_of(temp_store):
    # Nothing written at all -- store.as_of("game_config", ...) returns
    # empty regardless of the timestamp. This is the forward-only guard
    # (module docstring): a caller cannot silently get a wrong-season
    # answer because there is nothing to read before this project's own
    # game_config ingest history begins.
    with pytest.raises(ScoringError, match="forward-only"):
        load_scoring_config(temp_store, dt(2026, 8, 29))


def test_load_scoring_config_forward_only_guard_is_relative_to_the_earliest_observation_not_a_literal_date(temp_store):
    # Derived from the fixture's own write, never a hardcoded season
    # boundary (CLAUDE.md lesson 8): whatever `observed_at` this test
    # writes, an as_of strictly before it must raise, and an as_of at or
    # after it must succeed.
    written_at = dt(2026, 8, 20)
    _write_game_config(temp_store, observed_at=written_at)
    before = written_at.replace(hour=written_at.hour - 1)
    with pytest.raises(ScoringError):
        load_scoring_config(temp_store, before)
    load_scoring_config(temp_store, written_at)  # must not raise


def test_load_scoring_config_raises_on_missing_scoring_block(temp_store):
    import json

    store = temp_store
    store.write(
        "game_config",
        pl.DataFrame({"payload": [json.dumps({"rules": {}, "settings": {}})]}),
        valid_at=dt(2026, 8, 20),
        observed_at=dt(2026, 8, 20),
        source="test",
    )
    with pytest.raises(ScoringError):
        load_scoring_config(store, dt(2026, 8, 29))


def test_load_scoring_config_raises_on_missing_key(temp_store):
    payload = {"rules": {}, "settings": {}, "scoring": dict(REAL_SCORING_PAYLOAD["scoring"])}
    del payload["scoring"]["saves"]
    _write_game_config(temp_store, observed_at=dt(2026, 8, 20), payload=payload)
    with pytest.raises(ScoringError, match="saves"):
        load_scoring_config(temp_store, dt(2026, 8, 29))


def test_load_scoring_config_raises_on_missing_position_in_a_per_position_key(temp_store):
    payload = {"rules": {}, "settings": {}, "scoring": dict(REAL_SCORING_PAYLOAD["scoring"])}
    payload["scoring"]["goals_scored"] = {"DEF": 6, "MID": 5, "FWD": 4}  # no GKP
    _write_game_config(temp_store, observed_at=dt(2026, 8, 20), payload=payload)
    with pytest.raises(ScoringError, match="GKP"):
        load_scoring_config(temp_store, dt(2026, 8, 29))


# ---------------------------------------------------------------------------
# 3. score_outcome -- pure arithmetic, one identifier at a time
# ---------------------------------------------------------------------------


def test_score_outcome_zero_outcome_is_zero_points(real_config):
    assert score_outcome(RealisedOutcome(), "MID", real_config) == 0


def test_score_outcome_short_play_below_cliff(real_config):
    assert score_outcome(RealisedOutcome(minutes=MINUTE_CLIFF - 1), "MID", real_config) == real_config.short_play


def test_score_outcome_long_play_at_cliff(real_config):
    assert score_outcome(RealisedOutcome(minutes=MINUTE_CLIFF), "MID", real_config) == real_config.long_play


def test_score_outcome_zero_minutes_pays_nothing(real_config):
    assert score_outcome(RealisedOutcome(minutes=0), "MID", real_config) == 0


def test_score_outcome_goals_scored_is_position_specific(real_config):
    o = RealisedOutcome(minutes=90, goals_scored=1)
    assert score_outcome(o, "GK", real_config) == real_config.long_play + 10
    assert score_outcome(o, "FWD", real_config) == real_config.long_play + 4


def test_score_outcome_assists_use_the_scalar_value(real_config):
    o = RealisedOutcome(minutes=90, assists=2)
    assert score_outcome(o, "MID", real_config) == real_config.long_play + 2 * real_config.assists


def test_score_outcome_clean_sheet_position_specific_and_zero_for_fwd(real_config):
    o = RealisedOutcome(minutes=90, clean_sheet=True)
    assert score_outcome(o, "GK", real_config) == real_config.long_play + 4
    assert score_outcome(o, "MID", real_config) == real_config.long_play + 1
    assert score_outcome(o, "FWD", real_config) == real_config.long_play + 0


def test_score_outcome_goals_conceded_floor_divides_by_two(real_config):
    # 0,1 conceded -> 0 units; 2,3 -> 1 unit; 4,5 -> 2 units.
    for conceded, expected_units in [(0, 0), (1, 0), (2, 1), (3, 1), (4, 2), (5, 2)]:
        o = RealisedOutcome(minutes=90, goals_conceded=conceded)
        expected = real_config.long_play + expected_units * real_config.goals_conceded["DEF"]
        assert score_outcome(o, "DEF", real_config) == expected, conceded


def test_score_outcome_goals_conceded_pays_nothing_for_mid_fwd(real_config):
    o = RealisedOutcome(minutes=90, goals_conceded=6)
    assert score_outcome(o, "MID", real_config) == real_config.long_play
    assert score_outcome(o, "FWD", real_config) == real_config.long_play


def test_score_outcome_saves_floor_divides_by_three(real_config):
    for saves, expected_units in [(0, 0), (1, 0), (2, 0), (3, 1), (5, 1), (6, 2)]:
        o = RealisedOutcome(minutes=90, saves=saves)
        expected = real_config.long_play + expected_units * real_config.saves
        assert score_outcome(o, "GK", real_config) == expected, saves


def test_score_outcome_own_goals(real_config):
    o = RealisedOutcome(minutes=90, own_goals=1)
    assert score_outcome(o, "DEF", real_config) == real_config.long_play + real_config.own_goals


def test_score_outcome_penalties_saved_and_missed(real_config):
    saved = RealisedOutcome(minutes=90, penalties_saved=1)
    missed = RealisedOutcome(minutes=90, penalties_missed=1)
    assert score_outcome(saved, "GK", real_config) == real_config.long_play + real_config.penalties_saved
    assert score_outcome(missed, "FWD", real_config) == real_config.long_play + real_config.penalties_missed


def test_score_outcome_yellow_and_red_cards(real_config):
    yellow = RealisedOutcome(minutes=90, yellow_cards=1)
    red = RealisedOutcome(minutes=90, red_cards=1)
    assert score_outcome(yellow, "DEF", real_config) == real_config.long_play + real_config.yellow_cards
    assert score_outcome(red, "DEF", real_config) == real_config.long_play + real_config.red_cards


def test_score_outcome_bonus_is_paid_directly(real_config):
    for bonus in (0, 1, 2, 3):
        o = RealisedOutcome(minutes=90, bonus=bonus)
        assert score_outcome(o, "MID", real_config) == real_config.long_play + bonus * real_config.bonus


def test_score_outcome_defensive_contribution_is_position_specific_and_zero_for_gk(real_config):
    o = RealisedOutcome(minutes=90, defensive_contribution_met=True)
    assert score_outcome(o, "GK", real_config) == real_config.long_play + 0
    assert score_outcome(o, "DEF", real_config) == real_config.long_play + 2
    assert score_outcome(o, "MID", real_config) == real_config.long_play + 2
    assert score_outcome(o, "FWD", real_config) == real_config.long_play + 2


def test_score_outcome_rejects_unknown_position(real_config):
    with pytest.raises(ScoringError):
        score_outcome(RealisedOutcome(minutes=90), "GKP", real_config)  # config vocabulary, not the public one


def test_score_outcome_rejects_clean_sheet_below_the_minute_cliff(real_config):
    o = RealisedOutcome(minutes=MINUTE_CLIFF - 1, clean_sheet=True)
    with pytest.raises(ScoringError):
        score_outcome(o, "DEF", real_config)


def test_score_outcome_clean_sheet_exactly_at_the_cliff_is_allowed(real_config):
    o = RealisedOutcome(minutes=MINUTE_CLIFF, clean_sheet=True)
    assert score_outcome(o, "DEF", real_config) == real_config.long_play + real_config.clean_sheets["DEF"]


def test_positions_and_divisor_constants_are_the_documented_values():
    # Pinning the constants themselves, not just their effect -- a silent
    # change to any of these three is exactly the class of regression this
    # module's docstring exists to prevent (see "Two point values...").
    assert POSITIONS == ("GK", "DEF", "MID", "FWD")
    assert MINUTE_CLIFF == 60
    assert SAVES_POINTS_DIVISOR == 3
    assert GOALS_CONCEDED_POINTS_DIVISOR == 2


# ---------------------------------------------------------------------------
# 4. Real settled GW1 2026/27 regression fixtures
#
# Hand-copied from a live `event/1/live/` verification run this session
# (module docstring, "Verification" -- 610/610 exact match across the
# WHOLE gameweek). `defensive_contribution_met` was resolved against the
# real, pinned thresholds (DEF >= 10, MID/FWD >= 12, both `verified=True`
# as of this session -- HANDOFF §2) via
# `fplai.models.defensive_contribution.build_dc_threshold_set`, exactly
# the boundary this module's docstring says is out of its own scope.
# `penalties_saved` had zero real occurrences in GW1 2026/27 -- covered by
# a separate, clearly-labelled synthetic case instead (test above).
# ---------------------------------------------------------------------------

REAL_GW1_ROWS = [
    # (element_id, position, outcome kwargs, expected total_points)
    (1, "GK", dict(minutes=90, clean_sheet=True, saves=1), 6),
    (29, "GK", dict(minutes=90, clean_sheet=False, goals_conceded=4, saves=3), 1),
    (57, "GK", dict(minutes=90, clean_sheet=False, goals_conceded=2, saves=4), 2),
    (82, "GK", dict(minutes=90, clean_sheet=True, goals_conceded=0, saves=4), 7),
    (2, "GK", dict(minutes=0), 0),
    (4, "DEF", dict(minutes=90, clean_sheet=True, yellow_cards=1), 5),
    (8, "DEF", dict(minutes=80, assists=1, clean_sheet=True), 9),
    (10, "DEF", dict(minutes=90, assists=1, clean_sheet=True, bonus=2), 11),
    (32, "DEF", dict(minutes=90, clean_sheet=False, goals_conceded=4, yellow_cards=1), -1),
    (37, "DEF", dict(minutes=90, clean_sheet=False, goals_conceded=4, own_goals=1), -2),
    (87, "DEF", dict(minutes=90, clean_sheet=True, defensive_contribution_met=True), 8),
    (151, "DEF", dict(minutes=90, clean_sheet=False, goals_conceded=2, defensive_contribution_met=True), 3),
    (7, "MID", dict(minutes=90, clean_sheet=True), 3),
    (12, "MID", dict(minutes=67, goals_scored=1, clean_sheet=True, bonus=1), 9),
    (15, "MID", dict(minutes=75, goals_scored=1, clean_sheet=True, bonus=3), 11),
    (40, "MID", dict(minutes=81, goals_scored=1, clean_sheet=False, goals_conceded=2, bonus=1), 8),
    (54, "MID", dict(minutes=39, clean_sheet=False, goals_conceded=4, red_cards=1), -2),
    (98, "MID", dict(minutes=90, goals_scored=1, clean_sheet=True, yellow_cards=1, defensive_contribution_met=True), 9),
    (106, "FWD", dict(minutes=82, clean_sheet=True, penalties_missed=1), 0),
]


@pytest.mark.parametrize("element_id,position,outcome_kwargs,expected_points", REAL_GW1_ROWS, ids=[str(r[0]) for r in REAL_GW1_ROWS])
def test_score_outcome_matches_real_settled_gw1_total_points(real_config, element_id, position, outcome_kwargs, expected_points):
    outcome = RealisedOutcome(**outcome_kwargs)
    assert score_outcome(outcome, position, real_config) == expected_points


# ---------------------------------------------------------------------------
# Real-store-gated: the loader against the actual live game_config capture.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_load_scoring_config_reads_the_real_stores_current_game_config():
    real_store = BitemporalStore()
    latest = real_store.latest("game_config")
    if latest.is_empty():
        pytest.skip("no game_config captured in the real store yet")
    cfg = load_scoring_config(real_store, datetime.now(UTC))
    # 2026/27 values, blueprint §11 -- GKP goals is 10, not the pre-2026/27 6.
    assert cfg.goals_scored["GK"] == 10
    assert cfg.defensive_contribution == {"GK": 0, "DEF": 2, "MID": 2, "FWD": 2}
    assert cfg.clean_sheets["MID"] == 1


@pytest.mark.slow
def test_load_scoring_config_forward_only_guard_on_the_real_store_before_its_earliest_observation():
    real_store = BitemporalStore()
    obs = real_store.observations("game_config", until=datetime.now(UTC))
    if obs.is_empty():
        pytest.skip("no game_config captured in the real store yet")
    earliest = obs["observed_at"].min()
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=UTC)
    before_ingest = earliest - __import__("datetime").timedelta(days=1)
    with pytest.raises(ScoringError, match="forward-only"):
        load_scoring_config(real_store, before_ingest)


# ---------------------------------------------------------------------------
# 5. Standing scoring-reproduction invariant — session s006, story "Pin the
# scoring-reproduction measurement as a standing invariant".
#
# `game_config` only ever carries the CURRENT season's rules (module
# docstring, "FORWARD-ONLY"). Before this story, that read like a design
# gap for backtesting: scoring a HISTORICAL gameweek's realised outcome
# with today's rules looks like it must apply rules that never governed
# that gameweek. Measured instead of argued: `score_outcome`, composed
# with the CURRENT `game_config` and the CURRENT DC threshold set, against
# every scoreable row this store has ever ingested (2020-21 through
# 2026-27), reproduces stored `total_points` **163,671/163,672 times
# exactly**. The one residual is a single GK goal (Alisson, 2020-21 round
# 36) scored under a config that pays a GK goal 10 points where 2020-21's
# real rule paid 6 — see docs/wiki/scoring-validation.md for the full
# table and both caveats. This test pins that measurement so it fails
# loudly the instant it stops holding — the exact event the module
# docstring's forward-only guard exists to anticipate.
#
# Season list is derived from the store, never a literal (CLAUDE.md's
# "scheduled false alarm" lesson) — this must keep working as new seasons
# land without anyone touching this file.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_score_outcome_reproduces_stored_total_points_across_the_whole_scoreable_archive():
    real_store = BitemporalStore()
    now = datetime.now(UTC)

    df = read_player_gameweek_stats(real_store, as_of=now)
    if df.is_empty():
        pytest.skip("no player gameweek stats in the real store yet")
    df = df.drop_nulls(["position", "total_points"])
    if df.is_empty():
        pytest.skip("no scoreable (non-null position/total_points) rows in the real store yet")

    cfg = load_scoring_config(real_store, now)
    threshold_set = build_dc_threshold_set(real_store, as_of=now)

    n_total = 0
    n_match = 0
    mismatches: list[dict] = []

    for row in df.iter_rows(named=True):
        minutes = int(row["minutes"] or 0)
        position = "GK" if row["position"] == "GKP" else row["position"]
        if position not in POSITIONS:
            # e.g. the Assistant Manager chip's "AM" position, or any
            # future code this module's POSITIONS tuple does not cover --
            # genuinely outside score_outcome's scope, not a mismatch.
            continue
        group = POSITION_GROUP.get(position)
        dc_met = bool(
            group is not None
            and int(row["defensive_contribution"] or 0) >= threshold_set.threshold(group).count_threshold
        )
        outcome = RealisedOutcome(
            minutes=minutes,
            goals_scored=int(row["goals_scored"] or 0),
            assists=int(row["assists"] or 0),
            clean_sheet=bool(row["clean_sheets"] or 0) and minutes >= MINUTE_CLIFF,
            goals_conceded=int(row["goals_conceded"] or 0),
            own_goals=int(row["own_goals"] or 0),
            penalties_saved=int(row["penalties_saved"] or 0),
            penalties_missed=int(row["penalties_missed"] or 0),
            yellow_cards=int(row["yellow_cards"] or 0),
            red_cards=int(row["red_cards"] or 0),
            saves=int(row["saves"] or 0),
            bonus=int(row["bonus"] or 0),
            defensive_contribution_met=dc_met,
        )
        predicted = score_outcome(outcome, position, cfg)
        actual = int(row["total_points"])
        n_total += 1
        if predicted == actual:
            n_match += 1
        else:
            mismatches.append(
                {
                    "season": row["season"],
                    "round": row["round"],
                    "element": row["element"],
                    "position": position,
                    "goals_scored": outcome.goals_scored,
                    "predicted": predicted,
                    "actual": actual,
                }
            )

    assert n_total > 150_000, f"expected the full multi-season archive, got only {n_total} scoreable rows"

    # The exception is expressed BY ITS CAUSE, never by its count (pinned
    # decision, this story's brief) -- a hardcoded "exactly 1 mismatch"
    # would break the next time a GK scores, for no good reason. Every
    # mismatch, whatever its count, must be a GK goal under the CURRENT
    # config paying GKP goals differently than the season that actually
    # produced the row (module docstring's "Two point values genuinely
    # absent... and a third" is unrelated; this is the forward-only gap
    # itself, not an unpublished constant).
    non_gk_goal_mismatches = [m for m in mismatches if not (m["position"] == "GK" and m["goals_scored"] > 0)]
    assert not non_gk_goal_mismatches, (
        f"{len(non_gk_goal_mismatches)} mismatch(es) against stored total_points are NOT explained by "
        f"the known GK-goal residual (module docstring, 'FORWARD-ONLY') -- this is a real scoring-rule "
        f"drift the forward-only guard exists to catch: {non_gk_goal_mismatches[:10]}"
    )
