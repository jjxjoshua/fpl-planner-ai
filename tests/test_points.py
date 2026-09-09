"""Tests for fplai.points — the points-assembly layer, Phase 3 keystone,
session `s005` (blueprint §4.3, §7.1, CLAUDE.md rules 1/2/3/5/7).

Five groups:

  1. Pure numeric helpers (`_normalize`, `_band_marginal`,
     `_histogram_to_pmf`, `_draw_by_band_groups`) — isolated, no models,
     no store.
  2. `PointsPMF`/`PlayerFixtureFeatures` validation — hand-constructed,
     clearly-wrong inputs, expect `PointsError`.
  3. A small, hand-built, multi-model synthetic store — one shared
     fixture, fit ALL FIVE outcome models plus team_strength against it
     (the same public `fit_*_model`/`fit_team_strength` entry points a
     real caller would use, never a private helper), and use it for:
  4. Composition correctness — `simulate_fixture_points_pmfs` end-to-end:
     every PMF sums to 1, the saves seam behaves as documented (NOT_
     APPLICABLE / NOT_YET_MODELLED / MODELLED via a stub predictor),
     caveats present, fixture-input validation (team/is_home mismatch,
     duplicate element, <2 players).
  5. Determinism (CLAUDE.md rule 7) — bit-identical across two runs with
     the same seed and inputs; a genuinely DIFFERENT result with a
     different seed (so this is not the "test that cannot fail" class
     blueprint's lesson 5 warns about).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from fplai.models.attacking import fit_attacking_model
from fplai.models.attacking import build_training_table as build_attacking_table
from fplai.models.bonus import fit_bonus_model
from fplai.models.bonus import build_training_table as build_bonus_table
from fplai.models.cards import fit_cards_model
from fplai.models.cards import build_training_table as build_cards_table
from fplai.models.defensive_contribution import build_dc_threshold_set, fit_dc_model
from fplai.models.defensive_contribution import build_training_table as build_dc_table
from fplai.models.minutes import fit_minutes_model
from fplai.models.minutes import build_training_table as build_minutes_table
from fplai.models.team_strength import fit_team_strength, predict_scoreline
from fplai.points import (
    POSITIONS,
    SAVES_STATUS_MODELLED,
    SAVES_STATUS_NOT_APPLICABLE,
    SAVES_STATUS_NOT_YET_MODELLED,
    PlayerFixtureFeatures,
    PointsError,
    PointsPMF,
    PointsSimulationConfig,
    _band_marginal,
    _draw_by_band_groups,
    _draw_saves_by_band_and_goals,
    _histogram_to_pmf,
    _normalize,
    simulate_fixture_points_pmfs,
)
from fplai.scoring import ScoringConfig, load_scoring_config
from fplai.store import BitemporalStore

UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Pure numeric helpers.
# ---------------------------------------------------------------------------


def test_normalize_scales_to_sum_one():
    out = _normalize(np.array([2.0, 2.0, 4.0]))
    assert np.allclose(out.sum(), 1.0)
    assert np.allclose(out, [0.25, 0.25, 0.5])


def test_normalize_rejects_nonpositive_sum():
    with pytest.raises(PointsError):
        _normalize(np.array([0.0, 0.0, 0.0]))


def test_histogram_to_pmf_is_dense_and_sums_to_one():
    draws = np.array([1, 1, 3, 3, 3, -2], dtype=np.int64)
    support, probs = _histogram_to_pmf(draws)
    assert support == (-2, -1, 0, 1, 2, 3)
    assert abs(sum(probs) - 1.0) < 1e-12
    # -2: 1/6, -1: 0, 0: 0, 1: 2/6, 2: 0, 3: 3/6
    assert probs == (1 / 6, 0.0, 0.0, 2 / 6, 0.0, 3 / 6)


def test_draw_by_band_groups_respects_each_simulations_own_band():
    rng = np.random.default_rng(0)
    band_idx = np.array([0, 0, 1, 1, 1])

    @dataclass
    class _Fake:
        counts: tuple
        probabilities: tuple

    pmfs = [_Fake(counts=(0,), probabilities=(1.0,)), _Fake(counts=(9,), probabilities=(1.0,))]
    out = _draw_by_band_groups(rng, band_idx, pmfs)
    assert list(out) == [0, 0, 9, 9, 9]


def test_draw_saves_by_band_and_goals_conditions_on_both_axes():
    """The corrected two-mixture saves seam (session s005 -- see the
    module docstring, 'The saves seam') -- a simulation's draw must
    depend on BOTH its own band AND its own opponent-goals value, not
    either alone."""
    rng = np.random.default_rng(0)
    band_idx = np.array([0, 0, 1, 1])
    opp_goals_arr = np.array([0, 5, 0, 5])
    calls: list[tuple[float, int]] = []

    def _predict_fn(feature_row, *, element, fixture, minute_exposure, opponent_goals_marginal):
        m = minute_exposure[0][0]
        g = opponent_goals_marginal[0][0]
        calls.append((m, g))

        @dataclass
        class _Fake:
            counts: tuple
            probabilities: tuple

        # A spike whose location encodes (band, goals) so the assertion
        # below can recover which (band, goals) pair actually produced
        # each simulation's draw.
        return _Fake(counts=(int(m * 100 + g),), probabilities=(1.0,))

    out = _draw_saves_by_band_and_goals(
        rng, _predict_fn, {}, element=1, fixture=1, band_idx=band_idx, opp_goals_arr=opp_goals_arr
    )
    assert list(out) == [0, 5, 1500, 1505]
    # Exactly one call per unique (band, goals) pair -- never one per
    # simulation (module docstring, "The n=1 trick" cost argument applies
    # here too: precompute per group, never per draw).
    assert len(calls) == 4


# ---------------------------------------------------------------------------
# 2. PointsPMF / PlayerFixtureFeatures validation.
# ---------------------------------------------------------------------------


def test_player_fixture_features_rejects_unknown_position():
    with pytest.raises(PointsError):
        PlayerFixtureFeatures(
            element=1, position="AM", team="TeamA", is_home=True,
            minutes_feature_row={}, attacking_feature_row={}, dc_feature_row={},
            cards_feature_row={}, bonus_feature_row={},
        )


def test_points_pmf_rejects_non_dense_support():
    with pytest.raises(PointsError):
        PointsPMF(
            element=1, fixture=1, position="MID", points=(0, 2), probabilities=(0.5, 0.5),
            n_simulations=10, seed=0, saves_status=SAVES_STATUS_NOT_APPLICABLE, caveats=(),
        )


def test_points_pmf_rejects_probabilities_not_summing_to_one():
    with pytest.raises(PointsError):
        PointsPMF(
            element=1, fixture=1, position="MID", points=(0, 1), probabilities=(0.5, 0.6),
            n_simulations=10, seed=0, saves_status=SAVES_STATUS_NOT_APPLICABLE, caveats=(),
        )


def test_points_pmf_rejects_unknown_saves_status():
    with pytest.raises(PointsError):
        PointsPMF(
            element=1, fixture=1, position="MID", points=(0, 1), probabilities=(0.5, 0.5),
            n_simulations=10, seed=0, saves_status="BOGUS", caveats=(),
        )


def test_points_pmf_expected_points_and_variance():
    pmf = PointsPMF(
        element=1, fixture=1, position="MID", points=(0, 1, 2), probabilities=(0.2, 0.3, 0.5),
        n_simulations=10, seed=0, saves_status=SAVES_STATUS_NOT_APPLICABLE, caveats=(),
    )
    assert abs(pmf.expected_points() - (0 * 0.2 + 1 * 0.3 + 2 * 0.5)) < 1e-12
    assert pmf.variance() >= 0.0
    assert abs(pmf.p_at_least(1) - 0.8) < 1e-12


# ---------------------------------------------------------------------------
# 3. Synthetic multi-model store — one small fixture history, six rounds,
#    two teams, eight players (GK/DEF/MID/FWD x2), fit via the REAL public
#    fit_*_model entry points every sibling module's own tests already use
#    (tests/test_bonus.py's `_write_fixture` pattern).
# ---------------------------------------------------------------------------

SEASON = "2022-23"
TEAM_A = "TeamA"
TEAM_B = "TeamB"
# element -> (position, team)
ROSTER: dict[int, tuple[str, str]] = {
    1: ("GK", TEAM_A), 2: ("DEF", TEAM_A), 3: ("MID", TEAM_A), 4: ("FWD", TEAM_A),
    5: ("GK", TEAM_B), 6: ("DEF", TEAM_B), 7: ("MID", TEAM_B), 8: ("FWD", TEAM_B),
}
N_HISTORY_ROUNDS = 6


def _kickoff(round_: int) -> str:
    from datetime import timedelta

    d = datetime(2022, 8, 6, 14, 0, 0) + timedelta(days=7 * (round_ - 1))
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(round_: int, element: int, *, minutes=90, starts=1, goals_scored=0, assists=0, tackles=0, cbi=0,
         recoveries=0, yellow_cards=0, red_cards=0, bps=10, bonus=0, expected_goals=0.05, expected_assists=0.03,
         team_h_score=1, team_a_score=1) -> dict:
    position, team = ROSTER[element]
    opponent = TEAM_B if team == TEAM_A else TEAM_A
    was_home = team == TEAM_A
    return {
        "season": SEASON, "round": round_, "element": element, "fixture": round_,
        "kickoff_time": _kickoff(round_), "minutes": minutes, "starts": starts,
        "position": position, "team": team, "opponent_team": opponent, "was_home": was_home,
        "value": 50, "selected": 100000,
        "goals_scored": goals_scored, "assists": assists,
        "expected_goals": expected_goals, "expected_assists": expected_assists,
        "team_h_score": team_h_score, "team_a_score": team_a_score,
        "tackles": tackles, "clearances_blocks_interceptions": cbi, "recoveries": recoveries,
        "defensive_contribution": 0,
        "yellow_cards": yellow_cards, "red_cards": red_cards,
        "bps": bps, "bonus": bonus,
    }


def _build_history_rows() -> list[dict]:
    rows: list[dict] = []
    for r in range(1, N_HISTORY_ROUNDS + 1):
        away_scores = r % 2 == 1  # TeamB scores on odd rounds only
        team_h_score = 1
        team_a_score = 1 if away_scores else 0

        def_a_benched = r == 3  # element 2 (DEF, TeamA) unused this round -> an UNUSED sample
        mid_b_sub = r == 4  # element 7 (MID, TeamB) a late sub -> a SUB sample

        for element, (position, team) in ROSTER.items():
            minutes, starts = 90, 1
            if element == 2 and def_a_benched:
                minutes, starts = 0, 0
            if element == 7 and mid_b_sub:
                minutes, starts = 30, 0

            goals_scored = assists = 0
            expected_goals, expected_assists = 0.05, 0.03
            if element == 4:  # TeamA FWD scores TeamA's goal every round
                goals_scored, expected_goals = 1, 0.6
            if element == 3:  # TeamA MID assists it
                assists, expected_assists = 1, 0.4
            if element == 8 and away_scores:  # TeamB FWD scores when TeamB scores
                goals_scored, expected_goals = 1, 0.6
            if element == 7 and away_scores:  # TeamB MID assists it
                assists, expected_assists = 1, 0.4

            tackles = cbi = recoveries = 0
            if position == "DEF":
                tackles, cbi = 6, 5  # count=11 >= DEF threshold 10 -> DC met
            elif position == "MID":
                tackles, cbi, recoveries = 4, 4, 5  # count=13 >= MID/FWD threshold 12 -> DC met

            yellow_cards = 1 if (element == 2 and r == 2) else 0
            red_cards = 1 if (element == 6 and r == 5) else 0
            bps = 30 if element in (3, 4, 7, 8) else 15
            bonus = 0

            rows.append(
                _row(
                    r, element, minutes=minutes, starts=starts, goals_scored=goals_scored, assists=assists,
                    tackles=tackles, cbi=cbi, recoveries=recoveries, yellow_cards=yellow_cards,
                    red_cards=red_cards, bps=bps, bonus=bonus, expected_goals=expected_goals,
                    expected_assists=expected_assists, team_h_score=team_h_score, team_a_score=team_a_score,
                )
            )
    return rows


def _build_future_row(round_: int) -> list[dict]:
    """One placeholder-target row per player for a FUTURE round (this
    module's own equivalent of "the row we are about to predict") — used
    ONLY to extract each model's own leakage-safe TRAILING feature columns
    via that model's own `build_training_table` (module docstring, "What
    this module does NOT do": points.py builds nothing itself). Every
    trailing column is computed from `.shift(1)` over strictly earlier
    rounds, so this row's own placeholder target values (goals_scored=0,
    etc.) can never leak into the extracted feature columns."""
    return [_row(round_, element) for element in ROSTER]


AS_OF = dt(2022, 12, 1)  # strictly after every synthetic kickoff, including the future round

# Same shape the real 2026/27 game_config payload carries (test_scoring.py's
# own REAL_SCORING_PAYLOAD) -- written into the synthetic store so DC's
# read_dc_points_by_position (LIVE-read, module docstring) and this test's
# own scoring_config both come from the SAME source, never two hand-typed
# copies that could silently drift apart.
_GAME_CONFIG_PAYLOAD = {
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


@pytest.fixture(scope="module")
def synthetic_store(tmp_path_factory) -> BitemporalStore:
    import json

    store = BitemporalStore(base_path=tmp_path_factory.mktemp("points_store") / "store")
    rows = _build_history_rows() + _build_future_row(N_HISTORY_ROUNDS + 1)
    df = pl.DataFrame(rows)
    store.write("vaastav_player_gameweek_stats", df, valid_at=AS_OF, observed_at=AS_OF, source="test")
    store.write(
        "game_config",
        pl.DataFrame({"payload": [json.dumps(_GAME_CONFIG_PAYLOAD)]}),
        valid_at=AS_OF,
        observed_at=AS_OF,
        source="test",
    )
    return store


@pytest.fixture(scope="module")
def fitted(synthetic_store):
    minutes_params = fit_minutes_model(synthetic_store, as_of=AS_OF, seasons=[SEASON])
    attacking_params = fit_attacking_model(synthetic_store, as_of=AS_OF, seasons=[SEASON])
    threshold_set = build_dc_threshold_set(synthetic_store, as_of=AS_OF)
    dc_params = fit_dc_model(synthetic_store, as_of=AS_OF, threshold_set=threshold_set, seasons=[SEASON])
    cards_params = fit_cards_model(synthetic_store, as_of=AS_OF, seasons=[SEASON])
    bonus_params = fit_bonus_model(synthetic_store, as_of=AS_OF, seasons=[SEASON])
    team_params = fit_team_strength(synthetic_store, as_of=AS_OF, seasons=[SEASON], teams=[TEAM_A, TEAM_B])
    return {
        "minutes": minutes_params, "attacking": attacking_params, "dc": dc_params,
        "cards": cards_params, "bonus": bonus_params, "team": team_params,
    }


@pytest.fixture(scope="module")
def scoreline(fitted):
    return predict_scoreline(fitted["team"], TEAM_A, TEAM_B, max_goals=6)


@pytest.fixture(scope="module")
def scoring_config(synthetic_store) -> ScoringConfig:
    return load_scoring_config(synthetic_store, AS_OF)


def _future_feature_row(build_table_fn, store, *, drop_keys: tuple[str, ...] = ()) -> dict[int, dict]:
    table = build_table_fn(store, as_of=AS_OF, seasons=[SEASON])
    future = table.filter(pl.col("round") == N_HISTORY_ROUNDS + 1)
    out: dict[int, dict] = {}
    for row in future.to_dicts():
        d = dict(row)
        for k in drop_keys:
            d.pop(k, None)
        out[int(row["element"])] = d
    return out


@pytest.fixture(scope="module")
def player_features(synthetic_store) -> dict[int, PlayerFixtureFeatures]:
    minutes_rows = _future_feature_row(build_minutes_table, synthetic_store)
    attacking_rows = _future_feature_row(build_attacking_table, synthetic_store)
    dc_rows = _future_feature_row(build_dc_table, synthetic_store)
    cards_rows = _future_feature_row(build_cards_table, synthetic_store)
    # bonus's own BonusPlayerInput forbids 'position'/'minutes_frac' in feature_row
    # (module docstring, "What this module does NOT do" -- caller's job, not ours).
    bonus_rows = _future_feature_row(build_bonus_table, synthetic_store, drop_keys=("position", "minutes_frac"))

    out: dict[int, PlayerFixtureFeatures] = {}
    for element, (position, team) in ROSTER.items():
        out[element] = PlayerFixtureFeatures(
            element=element, position=position, team=team, is_home=(team == TEAM_A),
            minutes_feature_row=minutes_rows[element],
            attacking_feature_row=attacking_rows[element],
            # GK is categorically ineligible for DC (fplai.models.
            # defensive_contribution.POSITION_GROUP has no "GK" entry, so
            # its own build_training_table never emits a GK row at all) --
            # predict_dc_pmf checks eligibility FIRST and never reads
            # feature_row for an ineligible position, so an empty dict is
            # a correct, never-consulted placeholder here, not a guess.
            dc_feature_row=dc_rows.get(element, {}),
            cards_feature_row=cards_rows[element],
            bonus_feature_row=bonus_rows[element],
        )
    return out


def _run(
    fitted, scoreline, scoring_config, player_features, *, seed=0, n_simulations=300,
    bonus_n_simulations=200, saves_predict_fn=None,
):
    config = PointsSimulationConfig(seed=seed, n_simulations=n_simulations, bonus_n_simulations=bonus_n_simulations)
    players = list(player_features.values()) if isinstance(player_features, dict) else list(player_features)
    return simulate_fixture_points_pmfs(
        fixture=999,
        scoreline=scoreline,
        minutes_params=fitted["minutes"],
        attacking_params=fitted["attacking"],
        dc_params=fitted["dc"],
        cards_params=fitted["cards"],
        bonus_params=fitted["bonus"],
        scoring_config=scoring_config,
        players=players,
        config=config,
        saves_predict_fn=saves_predict_fn,
    )


# ---------------------------------------------------------------------------
# 4. Composition correctness.
# ---------------------------------------------------------------------------


def test_every_player_gets_a_valid_dense_pmf(fitted, scoreline, scoring_config, player_features):
    results = _run(fitted, scoreline, scoring_config, player_features)
    assert len(results) == len(ROSTER)
    elements_seen = {pmf.element for pmf in results}
    assert elements_seen == set(ROSTER)
    for pmf in results:
        assert abs(sum(pmf.probabilities) - 1.0) < 1e-9
        assert list(pmf.points) == list(range(pmf.points[0], pmf.points[-1] + 1))
        assert pmf.expected_points() > -10  # sanity floor, not a tight bound


def test_outfield_players_get_saves_not_applicable(fitted, scoreline, scoring_config, player_features):
    results = {pmf.element: pmf for pmf in _run(fitted, scoreline, scoring_config, player_features)}
    for element, (position, _team) in ROSTER.items():
        if position != "GK":
            assert results[element].saves_status == SAVES_STATUS_NOT_APPLICABLE


def test_gk_without_saves_predict_fn_gets_the_documented_hole(fitted, scoreline, scoring_config, player_features):
    results = {pmf.element: pmf for pmf in _run(fitted, scoreline, scoring_config, player_features)}
    for element, (position, _team) in ROSTER.items():
        if position == "GK":
            gk_pmf = results[element]
            assert gk_pmf.saves_status == SAVES_STATUS_NOT_YET_MODELLED
            assert any("saves" in c.lower() and "not yet modelled" in c.lower() for c in gk_pmf.caveats)


def test_gk_with_saves_predict_fn_is_wired_in(fitted, scoreline, scoring_config, player_features):
    """Proves the seam works WITHOUT importing fplai.models.saves (whose
    files landed mid-session but were still forbidden to touch/import --
    module docstring, 'The saves seam') -- a stub matching the CORRECTED,
    two-mixture duck-typed contract (`minute_exposure` AND `opponent_
    goals_marginal`, mirroring `fplai.models.saves.predict_saves_pmf`'s
    real signature, discovered by reading -- not importing -- that
    module's public signature once it appeared on disk)."""

    @dataclass
    class _StubSavesPMF:
        counts: tuple
        probabilities: tuple

    def _stub_predict_fn(feature_row, *, element, fixture, minute_exposure, opponent_goals_marginal):
        # A trivial, deterministic function of the (point-mass) minutes
        # and opponent-goals values -- more minutes, more saves mass, and
        # more opponent goals shifts the distribution up -- just enough to
        # prove BOTH point-mass mixtures are actually threaded through.
        m = minute_exposure[0][0]
        g = opponent_goals_marginal[0][0]
        if m <= 0:
            return _StubSavesPMF(counts=(0,), probabilities=(1.0,))
        if g >= 2:
            return _StubSavesPMF(counts=(0, 1, 2, 3, 4), probabilities=(0.1, 0.2, 0.2, 0.2, 0.3))
        return _StubSavesPMF(counts=(0, 1, 2, 3), probabilities=(0.2, 0.3, 0.3, 0.2))

    # A saves_predict_fn ALONE is not enough (module docstring, "The saves
    # seam") -- the player's own saves_feature_row must also be supplied,
    # so GKs get one here (content is irrelevant to the stub above, which
    # ignores feature_row entirely, but a real fplai.models.saves would
    # read it).
    with_saves_rows = dict(player_features)
    for element, (position, _team) in ROSTER.items():
        if position == "GK":
            p = with_saves_rows[element]
            with_saves_rows[element] = PlayerFixtureFeatures(
                element=p.element, position=p.position, team=p.team, is_home=p.is_home,
                minutes_feature_row=p.minutes_feature_row, attacking_feature_row=p.attacking_feature_row,
                dc_feature_row=p.dc_feature_row, cards_feature_row=p.cards_feature_row,
                bonus_feature_row=p.bonus_feature_row, saves_feature_row={},
            )

    results = {
        pmf.element: pmf
        for pmf in _run(fitted, scoreline, scoring_config, with_saves_rows, saves_predict_fn=_stub_predict_fn)
    }
    for element, (position, _team) in ROSTER.items():
        if position == "GK":
            gk_pmf = results[element]
            assert gk_pmf.saves_status == SAVES_STATUS_MODELLED
            assert not any("not yet modelled" in c.lower() for c in gk_pmf.caveats)


def test_own_goal_and_penalty_caveat_always_present(fitted, scoreline, scoring_config, player_features):
    for pmf in _run(fitted, scoreline, scoring_config, player_features):
        assert any("declared zero" in c.lower() for c in pmf.caveats)


def test_needs_at_least_two_players(fitted, scoreline, scoring_config, player_features):
    with pytest.raises(PointsError):
        _run(fitted, scoreline, scoring_config, {1: player_features[1]})


def test_duplicate_element_rejected(fitted, scoreline, scoring_config, player_features):
    dup = list(player_features.values()) + [player_features[1]]
    with pytest.raises(PointsError):
        _run(fitted, scoreline, scoring_config, dup, n_simulations=50, bonus_n_simulations=100)


def test_team_is_home_mismatch_rejected(fitted, scoreline, scoring_config, player_features):
    bad = dict(player_features)
    p = bad[1]
    bad[1] = PlayerFixtureFeatures(
        element=p.element, position=p.position, team=p.team, is_home=False,  # TeamA is actually home
        minutes_feature_row=p.minutes_feature_row, attacking_feature_row=p.attacking_feature_row,
        dc_feature_row=p.dc_feature_row, cards_feature_row=p.cards_feature_row, bonus_feature_row=p.bonus_feature_row,
    )
    with pytest.raises(PointsError):
        _run(fitted, scoreline, scoring_config, bad)


# ---------------------------------------------------------------------------
# 5. Determinism (CLAUDE.md rule 7).
# ---------------------------------------------------------------------------


def test_simulate_fixture_points_pmfs_is_bit_identical_across_repeated_calls_with_the_same_seed(
    fitted, scoreline, scoring_config, player_features
):
    run_a = _run(fitted, scoreline, scoring_config, player_features, seed=7, n_simulations=400, bonus_n_simulations=300)
    run_b = _run(fitted, scoreline, scoring_config, player_features, seed=7, n_simulations=400, bonus_n_simulations=300)
    by_element_a = {pmf.element: pmf for pmf in run_a}
    by_element_b = {pmf.element: pmf for pmf in run_b}
    assert set(by_element_a) == set(by_element_b)
    for element in by_element_a:
        assert by_element_a[element].points == by_element_b[element].points
        assert by_element_a[element].probabilities == by_element_b[element].probabilities


def test_a_different_seed_changes_the_result(fitted, scoreline, scoring_config, player_features):
    """The other half of the determinism proof (blueprint lesson 5): a
    test that only checks 'same seed -> same output' would still pass if
    this module ignored its seed entirely and used a fixed internal
    stream, or if it were entirely deterministic with no randomness at
    all -- neither of which is true here, and this is what actually
    distinguishes the two."""
    run_a = _run(fitted, scoreline, scoring_config, player_features, seed=1, n_simulations=400, bonus_n_simulations=300)
    run_b = _run(fitted, scoreline, scoring_config, player_features, seed=2, n_simulations=400, bonus_n_simulations=300)
    by_element_a = {pmf.element: pmf for pmf in run_a}
    by_element_b = {pmf.element: pmf for pmf in run_b}
    any_difference = any(
        by_element_a[e].points != by_element_b[e].points or by_element_a[e].probabilities != by_element_b[e].probabilities
        for e in by_element_a
    )
    assert any_difference
