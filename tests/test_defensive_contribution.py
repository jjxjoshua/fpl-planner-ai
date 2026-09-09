"""Tests for fplai.models.defensive_contribution — Phase 2, E5, session
s003 (blueprint §4, §7.1, §11, §12.2; docs/wiki/defensive-contribution.md).

Five groups:
  1. Pure functions — composition rule, threshold provenance, PMF sum-to-1
     discipline, the NB negative-log-likelihood's analytic gradient
     checked against finite differences (this module's own version of the
     "prove it before trusting it" standing rule, applied to a hand-derived
     gradient rather than a leakage boundary).
  2. Feature engineering — a temp store, fabricated multi-round rows,
     including the double-gameweek leakage attack for BOTH the player-level
     AND team-level trailing features (the team-level one is a genuine bug
     class caught during this module's own design — see its docstring).
  3. Fit + predict + walk-forward mechanics — small synthetic data with a
     KNOWN team-style signal the model must recover.
  4. Real-store-gated: composition-rule/GK-zero live cross-checks, derived-
     capability round trip, import-time registration, a bounded live
     walk-forward sanity check (the full gate numbers are
     `scripts/fit_defensive_contribution.py`'s job, reported in
     docs/wiki/model-defensive-contribution.md).
  5. `scripts/pin_dc_thresholds.py` — settlement-check logic and the
     bounds/pin inference logic against a fabricated `event_live` payload
     via a duck-typed fake client (no real network calls in this file).
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from fplai.models.defensive_contribution import (
    DC_GROUPS,
    DC_THRESHOLD_OBSERVATION_DATASET,
    DC_THRESHOLD_OBSERVATION_SCHEMA,
    GROUP_POSITIONS,
    NUMERIC_FEATURE_COLUMNS_DC,
    POSITION_GROUP,
    PRESS_DC_COUNT_THRESHOLDS,
    DCFeatureSpec,
    DCModelConfig,
    DCModelError,
    DCPMF,
    DCThresholdProvenance,
    DCThresholdSet,
    _design_matrix_dc,
    _fit_groups_from_table,
    _nb_neg_log_lik_and_grad,
    _nb_pmf,
    build_dc_threshold_set,
    build_training_table,
    dc_component_count,
    dc_threshold_observation_rows,
    fit_dc_model,
    predict_dc_pmf,
    pmfs_to_rows,
    read_dc_points_by_position,
    resolve_dc_threshold_observations,
    walk_forward_validate,
    write_dc_pmfs,
    write_dc_threshold_observations,
)
from fplai.derived import CalibrationReference
from fplai.schemas import CANONICAL_SCHEMAS, PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK
from fplai.store import BitemporalStore

UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Pure functions.
# ---------------------------------------------------------------------------


def test_dc_component_count_def_formula_excludes_recoveries():
    assert dc_component_count("DEF", tackles=3, cbi=4, recoveries=100) == 7


def test_dc_component_count_mid_fwd_formula_includes_recoveries():
    assert dc_component_count("MID", tackles=3, cbi=4, recoveries=5) == 12
    assert dc_component_count("FWD", tackles=1, cbi=1, recoveries=1) == 3


def test_dc_component_count_raises_for_ineligible_position():
    with pytest.raises(DCModelError):
        dc_component_count("GK", tackles=0, cbi=0, recoveries=18)


def test_dc_threshold_provenance_rejects_non_positive_threshold():
    with pytest.raises(DCModelError):
        DCThresholdProvenance(group="DEF_CBIT", count_threshold=0, source="x", source_date="2026-01-01", verified=False)


def test_dc_threshold_provenance_rejects_empty_source():
    with pytest.raises(DCModelError):
        DCThresholdProvenance(group="DEF_CBIT", count_threshold=10, source="  ", source_date="2026-01-01", verified=False)


def test_press_dc_count_thresholds_are_unverified_by_default():
    """The core structural-labelling requirement of this task's brief:
    every default threshold is verified=False and carries a citation."""
    for group in DC_GROUPS:
        prov = PRESS_DC_COUNT_THRESHOLDS[group]
        assert prov.verified is False
        assert "premierleague.com" in prov.source
        assert prov.count_threshold > 0


def test_press_thresholds_match_the_wiki_10_and_12():
    assert PRESS_DC_COUNT_THRESHOLDS["DEF_CBIT"].count_threshold == 10
    assert PRESS_DC_COUNT_THRESHOLDS["MID_FWD_CBIRT"].count_threshold == 12


def test_dc_pmf_rejects_probabilities_not_summing_to_one():
    with pytest.raises(DCModelError):
        DCPMF(
            element=1, fixture=1, group="DEF_CBIT", eligible=True, count_threshold=10,
            threshold_verified=False, threshold_source="x", points=2,
            counts=(0, 1), probabilities=(0.3, 0.3), mass_before_truncation=1.0,
        )


def test_dc_pmf_p_dc_awarded_sums_only_tail_at_or_above_threshold():
    counts = tuple(range(0, 15))
    probs = tuple(1.0 / len(counts) for _ in counts)
    pmf = DCPMF(
        element=1, fixture=1, group="DEF_CBIT", eligible=True, count_threshold=10,
        threshold_verified=False, threshold_source="x", points=2,
        counts=counts, probabilities=probs, mass_before_truncation=1.0,
    )
    expected = sum(1.0 / len(counts) for c in counts if c >= 10)
    assert pmf.p_dc_awarded() == pytest.approx(expected)
    assert pmf.expected_dc_points() == pytest.approx(expected * 2)


def test_dc_pmf_ineligible_has_zero_p_dc_awarded_regardless_of_counts():
    pmf = DCPMF(
        element=1, fixture=1, group="INELIGIBLE", eligible=False, count_threshold=None,
        threshold_verified=True, threshold_source="game_config", points=0,
        counts=(0,), probabilities=(1.0,), mass_before_truncation=1.0,
    )
    assert pmf.p_dc_awarded() == 0.0
    assert pmf.expected_dc_points() == 0.0


def test_nb_pmf_sums_to_one_over_a_wide_enough_range():
    pmf, mass = _nb_pmf(mu=5.0, r=3.0, max_count=60)
    assert pmf.sum() == pytest.approx(1.0, abs=1e-9)
    assert mass == pytest.approx(1.0, abs=1e-6)


def test_nb_pmf_mu_le_zero_is_a_degenerate_spike_at_zero():
    pmf, mass = _nb_pmf(mu=0.0, r=3.0, max_count=10)
    assert pmf[0] == pytest.approx(1.0)
    assert pmf[1:].sum() == pytest.approx(0.0)
    assert mass == pytest.approx(1.0)


def test_nb_pmf_truncation_reports_less_than_full_mass_for_a_high_mean():
    """A mean well above max_count leaves real, trackable truncated mass —
    same convention `fplai.models.team_strength.ScorelinePMF` uses."""
    pmf, mass = _nb_pmf(mu=20.0, r=5.0, max_count=10)
    assert mass < 0.999


def test_nb_neg_log_lik_gradient_matches_finite_differences():
    """This module's own version of CLAUDE.md lesson 5 ("prove it before
    trusting it"), applied to the hand-derived NB gradient (module
    docstring's dL/d(eta), dL/dr derivation) rather than a leakage
    boundary — a wrong analytic gradient would silently misfit every group
    while looking like ordinary L-BFGS-B convergence."""
    rng = np.random.default_rng(0)
    n, n_features = 40, 4
    X = rng.normal(size=(n, n_features))
    X[:, 0] = 1.0  # intercept column
    true_beta = np.array([0.2, 0.1, -0.3, 0.05])
    offset = rng.normal(scale=0.1, size=n)
    mu_true = np.exp(X @ true_beta + offset)
    y = rng.negative_binomial(n=3.0, p=3.0 / (3.0 + mu_true)).astype(np.float64)

    params0 = np.concatenate([true_beta * 0.7, [0.8]])
    nll0, grad_analytic = _nb_neg_log_lik_and_grad(params0, X, y, offset, n_features, l2=0.5)

    eps = 1e-6
    grad_numeric = np.zeros_like(params0)
    for i in range(len(params0)):
        p_plus = params0.copy(); p_plus[i] += eps
        p_minus = params0.copy(); p_minus[i] -= eps
        nll_plus, _ = _nb_neg_log_lik_and_grad(p_plus, X, y, offset, n_features, l2=0.5)
        nll_minus, _ = _nb_neg_log_lik_and_grad(p_minus, X, y, offset, n_features, l2=0.5)
        grad_numeric[i] = (nll_plus - nll_minus) / (2 * eps)

    assert np.allclose(grad_analytic, grad_numeric, atol=1e-4, rtol=1e-3)


def test_nb_neg_log_lik_l2_penalty_is_scaled_by_inverse_n():
    """Pins session s005's fix directly: the ridge term must be `(l2/n) *
    sum(beta**2)`, matching `fplai.models.saves`'s own already-corrected
    copy of this exact formula, not the unscaled `l2 * sum(beta**2)` this
    module carried before this session (this task's brief; CLAUDE.md rule
    5 — an unscaled penalty against a per-row-AVERAGED likelihood is
    effectively `l2*n`, silently crushing every coefficient including the
    intercept for `n` in the thousands).

    Same construction `tests/test_minutes.py::
    test_softmax_l2_penalty_is_scaled_by_inverse_n` uses for the sibling
    softmax formula: isolate the penalty component via the `l2=0.0`
    subtraction, then compare row-doubled-identical-content `n` against the
    original. **Verified, by hand computation against a copy of the pre-fix
    expression before trusting this test (CLAUDE.md lesson 5): under the
    OLD unscaled formula the penalty component was IDENTICAL at `n=12` and
    the row-doubled `n=24` (0.0756 vs 0.0756 — no `n` dependence at all);
    under THIS fixed formula the penalty component exactly HALVES
    (0.00630 -> 0.00315).** The halving assertion below is what actually
    pins the fix and fails hard against a regression to the unscaled
    form."""
    rng = np.random.default_rng(2)
    n_features = 3
    X = rng.normal(size=(12, n_features))
    X[:, 0] = 1.0
    offset = rng.normal(scale=0.1, size=12)
    true_beta = np.array([0.3, 0.1, -0.2])
    mu_true = np.exp(X @ true_beta + offset)
    y = rng.negative_binomial(n=3.0, p=3.0 / (3.0 + mu_true)).astype(np.float64)
    X_doubled = np.vstack([X, X])
    y_doubled = np.concatenate([y, y])
    offset_doubled = np.concatenate([offset, offset])
    params = np.concatenate([true_beta * 0.6, [0.5]])
    l2 = 1.5

    nll_n12, _ = _nb_neg_log_lik_and_grad(params, X, y, offset, n_features, l2=l2)
    nll_n12_nopenalty, _ = _nb_neg_log_lik_and_grad(params, X, y, offset, n_features, l2=0.0)
    penalty_n12 = nll_n12 - nll_n12_nopenalty

    nll_n24, _ = _nb_neg_log_lik_and_grad(params, X_doubled, y_doubled, offset_doubled, n_features, l2=l2)
    nll_n24_nopenalty, _ = _nb_neg_log_lik_and_grad(
        params, X_doubled, y_doubled, offset_doubled, n_features, l2=0.0
    )
    penalty_n24 = nll_n24 - nll_n24_nopenalty

    assert nll_n12_nopenalty == pytest.approx(nll_n24_nopenalty, abs=1e-9)
    assert penalty_n24 == pytest.approx(penalty_n12 / 2.0, rel=1e-9)

    n = X.shape[0]
    expected_penalty_n12 = (l2 / n) * float(np.sum(params[:n_features] ** 2))
    assert penalty_n12 == pytest.approx(expected_penalty_n12, rel=1e-9)


# ---------------------------------------------------------------------------
# 2. Feature engineering — temp store, leakage attacks.
# ---------------------------------------------------------------------------


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
    position: str = "DEF",
    tackles: int = 1,
    cbi: int = 1,
    recoveries: int = 1,
    dc_total: int | None = None,
    value: int = 50,
    selected: int = 10000,
    opponent_team: int = 2,
) -> dict:
    if dc_total is None:
        dc_total = tackles + cbi if position == "DEF" else tackles + cbi + recoveries
    if position == "GK":
        dc_total = 0
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
        "was_home": was_home,
        "value": value,
        "selected": selected,
        "tackles": tackles,
        "clearances_blocks_interceptions": cbi,
        "recoveries": recoveries,
        "defensive_contribution": dc_total,
    }


@pytest.fixture
def temp_store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def _write_fixture(store, rows, *, observed_at, valid_at=None):
    df = pl.DataFrame(rows)
    store.write("vaastav_player_gameweek_stats", df, valid_at=valid_at or observed_at, observed_at=observed_at, source="test")


def _write_game_config(store, *, observed_at, points=None):
    points = points or {"DEF": 2, "FWD": 2, "GKP": 0, "MID": 2}
    payload = {"rules": {}, "scoring": {"defensive_contribution": points}, "settings": {}}
    store.write("game_config", pl.DataFrame({"payload": [json.dumps(payload)]}), valid_at=observed_at, observed_at=observed_at, source="test")


def _default_threshold_set() -> DCThresholdSet:
    return DCThresholdSet(count_thresholds=PRESS_DC_COUNT_THRESHOLDS, points_by_position={"DEF": 2, "MID": 2, "FWD": 2, "GK": 0})


def test_read_dc_points_by_position_reads_the_live_scoring_block(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    points = read_dc_points_by_position(temp_store)
    assert points == {"DEF": 2, "FWD": 2, "GKP": 0, "MID": 2}


def test_read_dc_points_by_position_raises_if_scoring_block_missing(temp_store):
    payload = {"rules": {}, "scoring": {}, "settings": {}}
    temp_store.write("game_config", pl.DataFrame({"payload": [json.dumps(payload)]}), valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    with pytest.raises(DCModelError):
        read_dc_points_by_position(temp_store)


def test_build_dc_threshold_set_remaps_gkp_to_gk_and_gates_on_zero_points(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    threshold_set = build_dc_threshold_set(temp_store)
    assert threshold_set.is_eligible("DEF")
    assert threshold_set.is_eligible("MID")
    assert threshold_set.is_eligible("FWD")
    assert not threshold_set.is_eligible("GK")
    assert threshold_set.points("GK") == 0


def test_build_dc_threshold_set_raises_if_group_positions_disagree_on_points(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1), points={"DEF": 2, "FWD": 1, "GKP": 0, "MID": 2})
    with pytest.raises(DCModelError):
        build_dc_threshold_set(temp_store)


def test_build_training_table_requires_timezone_aware_as_of(temp_store):
    _write_fixture(temp_store, [_row("2025-26", 1, 10, 1, "2025-08-06T14:00:00Z")], observed_at=dt(2026, 1, 1))
    with pytest.raises(DCModelError):
        build_training_table(temp_store, as_of=datetime(2026, 1, 1), threshold_set=_default_threshold_set())


def test_build_training_table_excludes_rows_with_null_defensive_contribution(temp_store):
    """No pre-2025-26 backfill (module docstring) — a row with a NULL
    defensive_contribution (as every pre-2025-26 season's row genuinely is
    in the real store) must never enter the training table."""
    rows = [_row("2025-26", 1, 10, 1, "2025-08-06T14:00:00Z")]
    df = pl.DataFrame(rows).with_columns(pl.lit(None, dtype=pl.Int64).alias("defensive_contribution"))
    temp_store.write("vaastav_player_gameweek_stats", df, valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    with pytest.raises(DCModelError):
        build_training_table(temp_store, as_of=dt(2026, 1, 1), threshold_set=_default_threshold_set())


def test_build_training_table_excludes_gk_rows_entirely(temp_store):
    rows = [
        _row("2025-26", 1, 10, 1, "2025-08-06T14:00:00Z", position="GK", recoveries=8, tackles=0, cbi=0),
        _row("2025-26", 1, 11, 1, "2025-08-06T14:00:00Z", position="DEF"),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2), threshold_set=_default_threshold_set())
    assert set(table["element"].to_list()) == {11}


def test_player_trailing_feature_never_peeks_at_the_same_round_second_fixture(temp_store):
    """Double-gameweek round-boundary discipline for the PLAYER trailing
    feature — mirrors fplai.models.minutes' own DGW isolation test."""
    rows = [
        _row("2025-26", 1, 10, 1, "2025-08-06T14:00:00Z", tackles=5, cbi=5, recoveries=0),  # round 1, count=10
        # Round 2: TWO fixtures, a double gameweek. Second fixture has an
        # enormous count -- if the first fixture's trailing feature ever
        # picked it up, this test would catch it.
        _row("2025-26", 2, 10, 2, "2025-08-13T14:00:00Z", tackles=1, cbi=1, recoveries=0),
        _row("2025-26", 2, 10, 3, "2025-08-16T14:00:00Z", tackles=20, cbi=20, recoveries=0),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2), threshold_set=_default_threshold_set())
    round2 = table.filter(pl.col("round") == 2).sort("fixture")
    # Both round-2 fixtures' trailing feature must reflect ONLY round 1
    # (count=10), never round 2's own second (huge) fixture.
    assert round2["player_trailing_count_3"].to_list() == pytest.approx([10.0, 10.0])


def test_team_trailing_feature_never_peeks_at_the_same_round_second_fixture(temp_store):
    """The team-level analogue of the test above -- caught during this
    module's own design (module docstring, "_build_team_round_rollup"): a
    fixture-grain team rollup (mirroring fplai.models.minutes'
    `_build_team_fixture_gap`, which is legitimately fixture-grain because
    it rolls PUBLIC schedule data) would leak an OUTCOME (defensive_
    contribution) across a double gameweek's own two fixtures. This test
    would fail against that design."""
    rows = [
        _row("2025-26", 1, 10, 1, "2025-08-06T14:00:00Z", team="TeamA", tackles=5, cbi=5, recoveries=0),  # team round total = 10
        _row("2025-26", 2, 10, 2, "2025-08-13T14:00:00Z", team="TeamA", tackles=1, cbi=1, recoveries=0),
        _row("2025-26", 2, 10, 3, "2025-08-16T14:00:00Z", team="TeamA", tackles=25, cbi=25, recoveries=0),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2), threshold_set=_default_threshold_set())
    round2 = table.filter(pl.col("round") == 2).sort("fixture")
    assert round2["team_trailing_dc_mean_5"].to_list() == pytest.approx([10.0, 10.0])


def test_cold_start_flag_true_for_a_players_first_labelled_round(temp_store):
    rows = [_row("2025-26", 1, 10, 1, "2025-08-06T14:00:00Z")]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2), threshold_set=_default_threshold_set())
    assert table["cold_start"][0] is True
    assert table["games_played_this_season"][0] == pytest.approx(0.0)


def test_team_cold_start_flag_true_for_a_teams_first_round_in_window(temp_store):
    rows = [_row("2025-26", 1, 10, 1, "2025-08-06T14:00:00Z", team="TeamA")]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2), threshold_set=_default_threshold_set())
    assert table["team_cold_start"][0] is True
    assert table["team_trailing_dc_mean_5"][0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. Fit + predict + walk-forward mechanics — synthetic data.
# ---------------------------------------------------------------------------


def _synthetic_table(n_rounds: int = 10, seed: int = 0) -> pl.DataFrame:
    """A hand-built table carrying every column `_fit_groups_from_table`/
    `walk_forward_validate` need, with a KNOWN team-style signal: TeamHigh
    always has a high `team_trailing_dc_mean_5`, TeamLow always has a low
    one, and each team's players' actual counts are drawn to be
    consistent with that -- the model should learn a positive coefficient
    on team_trailing_dc_mean_5 and predict TeamHigh's players higher."""
    rng = np.random.default_rng(seed)
    rows = []
    for team, base_rate in (("TeamHigh", 14.0), ("TeamLow", 4.0)):
        for element in range(1, 4):
            for round_ in range(1, n_rounds + 1):
                count = int(rng.poisson(base_rate))
                rows.append(
                    {
                        "season": "2025-26",
                        "round": round_,
                        "element": element + (100 if team == "TeamHigh" else 0),
                        "fixture": round_,
                        "team": team,
                        "position": "MID",
                        "minutes": 90,
                        "group": "MID_FWD_CBIRT",
                        "count": count,
                        "awarded": count >= 12,
                        "player_trailing_count_3": float(base_rate),
                        "player_trailing_count_5": float(base_rate),
                        "player_trailing_count_10": float(base_rate),
                        "team_trailing_dc_mean_5": base_rate * 3,  # 3 players/team, same rate
                        "games_played_this_season": float(round_ - 1),
                        "cold_start": round_ == 1,
                        "team_cold_start": round_ == 1,
                        "was_home": True,
                        "is_forward": False,
                        "player_trailing_awarded_rate_5": None,
                        "_chronological_rank": round_,
                    }
                )
    return pl.DataFrame(rows)


def test_fit_groups_from_table_recovers_the_team_style_direction():
    table = _synthetic_table()
    threshold_set = _default_threshold_set()
    groups, seasons_used = _fit_groups_from_table(table, threshold_set=threshold_set, config=DCModelConfig())
    assert seasons_used == ("2025-26",)
    beta = groups["MID_FWD_CBIRT"].beta
    spec = groups["MID_FWD_CBIRT"].feature_spec
    idx = spec.numeric_columns.index("team_trailing_dc_mean_5")
    assert beta[idx + 1] > 0  # +1 for the intercept column


def test_predict_dc_pmf_ineligible_position_returns_degenerate_pmf():
    table = _synthetic_table()
    threshold_set = _default_threshold_set()
    groups, seasons_used = _fit_groups_from_table(table, threshold_set=threshold_set, config=DCModelConfig())
    from fplai.models.defensive_contribution import DCModelParams

    params = DCModelParams(groups=groups, threshold_set=threshold_set, as_of=dt(2026, 1, 1), seasons_used=seasons_used)
    feature_row = {c: 0.0 for c in NUMERIC_FEATURE_COLUMNS_DC}
    pmf = predict_dc_pmf(params, feature_row, element=1, fixture=1, position="GK", minute_exposure=[(90.0, 1.0)])
    assert pmf.eligible is False
    assert pmf.p_dc_awarded() == 0.0
    assert pmf.counts == (0,)


def test_predict_dc_pmf_rejects_minute_exposure_not_summing_to_one():
    table = _synthetic_table()
    threshold_set = _default_threshold_set()
    groups, seasons_used = _fit_groups_from_table(table, threshold_set=threshold_set, config=DCModelConfig())
    from fplai.models.defensive_contribution import DCModelParams

    params = DCModelParams(groups=groups, threshold_set=threshold_set, as_of=dt(2026, 1, 1), seasons_used=seasons_used)
    feature_row = {c: 0.0 for c in NUMERIC_FEATURE_COLUMNS_DC}
    with pytest.raises(DCModelError):
        predict_dc_pmf(params, feature_row, element=1, fixture=1, position="MID", minute_exposure=[(90.0, 0.5)])


def test_predict_dc_pmf_zero_minutes_exposure_is_all_mass_at_zero():
    table = _synthetic_table()
    threshold_set = _default_threshold_set()
    groups, seasons_used = _fit_groups_from_table(table, threshold_set=threshold_set, config=DCModelConfig())
    from fplai.models.defensive_contribution import DCModelParams

    params = DCModelParams(groups=groups, threshold_set=threshold_set, as_of=dt(2026, 1, 1), seasons_used=seasons_used)
    feature_row = {c: 5.0 for c in NUMERIC_FEATURE_COLUMNS_DC}
    pmf = predict_dc_pmf(params, feature_row, element=1, fixture=1, position="MID", minute_exposure=[(0.0, 1.0)])
    assert pmf.probabilities[0] == pytest.approx(1.0)
    assert pmf.p_dc_awarded() == pytest.approx(0.0)


def test_predict_dc_pmf_team_style_swap_changes_p_dc_awarded_in_the_learned_direction():
    """Direct proof this module CAN express the Elliot Anderson case
    (module docstring): holding a player's OWN features fixed, swapping
    ONLY the team-style feature between the synthetic table's two known
    team profiles must move P(DC awarded) in the same direction the
    training signal was built with."""
    table = _synthetic_table()
    threshold_set = _default_threshold_set()
    groups, seasons_used = _fit_groups_from_table(table, threshold_set=threshold_set, config=DCModelConfig())
    from fplai.models.defensive_contribution import DCModelParams

    params = DCModelParams(groups=groups, threshold_set=threshold_set, as_of=dt(2026, 1, 1), seasons_used=seasons_used)
    base_feature_row = {
        "player_trailing_count_3": 8.0, "player_trailing_count_5": 8.0, "player_trailing_count_10": 8.0,
        "games_played_this_season": 5.0, "cold_start": False, "team_cold_start": False,
        "was_home": True, "is_forward": False, "team_trailing_dc_mean_5": 0.0,
    }
    exposure = [(90.0, 1.0)]
    low = dict(base_feature_row, team_trailing_dc_mean_5=12.0)   # TeamLow's real value (4.0*3)
    high = dict(base_feature_row, team_trailing_dc_mean_5=42.0)  # TeamHigh's real value (14.0*3)
    pmf_low = predict_dc_pmf(params, low, element=1, fixture=1, position="MID", minute_exposure=exposure)
    pmf_high = predict_dc_pmf(params, high, element=1, fixture=1, position="MID", minute_exposure=exposure)
    assert pmf_high.p_dc_awarded() > pmf_low.p_dc_awarded()


def test_walk_forward_validate_raises_when_no_fold_has_enough_train_rows():
    table = _synthetic_table(n_rounds=3)
    with pytest.raises(DCModelError):
        walk_forward_validate(table, group="MID_FWD_CBIRT", threshold_set=_default_threshold_set(), min_train_rows=10_000)


def test_walk_forward_validate_produces_one_prediction_per_eligible_eval_row():
    table = _synthetic_table(n_rounds=10)
    result = walk_forward_validate(table, group="MID_FWD_CBIRT", threshold_set=_default_threshold_set(), min_train_rows=6)
    assert result.n_folds > 0
    assert len(result.y_true) == len(result.p_model) == len(result.count_true) == len(result.mu_model)
    for p in result.p_model:
        assert 0.0 <= p <= 1.0


# ---------------------------------------------------------------------------
# 4. Real-store-gated.
# ---------------------------------------------------------------------------


def _real_store_available() -> bool:
    path = Path(__file__).resolve().parents[1] / "data" / "store" / "vaastav_player_gameweek_stats"
    return path.exists() and any(path.glob("**/*.parquet"))


requires_real_store = pytest.mark.skipif(not _real_store_available(), reason="data/store/vaastav_player_gameweek_stats absent")


@pytest.fixture
def registered_capability():
    return CANONICAL_SCHEMAS[PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK]


def test_registering_the_capability_does_not_leak_across_tests(registered_capability):
    assert registered_capability.is_modelled is True
    assert PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK in CANONICAL_SCHEMAS


def test_importing_the_module_registers_the_capability_at_import_time():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import fplai.models.defensive_contribution\n"
        "from fplai.schemas import CANONICAL_SCHEMAS, PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK\n"
        "assert PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK in CANONICAL_SCHEMAS\n"
        "schema = CANONICAL_SCHEMAS[PLAYER_DEFENSIVE_CONTRIBUTION_DISTRIBUTION_GAMEWEEK]\n"
        "assert schema.is_modelled is True\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_write_dc_pmfs_round_trips_through_write_derived(temp_store, registered_capability):
    table = _synthetic_table()
    threshold_set = _default_threshold_set()
    groups, seasons_used = _fit_groups_from_table(table, threshold_set=threshold_set, config=DCModelConfig())
    from fplai.models.defensive_contribution import DCModelParams

    params = DCModelParams(groups=groups, threshold_set=threshold_set, as_of=dt(2026, 1, 1), seasons_used=seasons_used)
    feature_row = {c: 5.0 for c in NUMERIC_FEATURE_COLUMNS_DC}
    feature_row["cold_start"] = False
    feature_row["team_cold_start"] = False
    feature_row["is_forward"] = False
    pmf = predict_dc_pmf(params, feature_row, element=1, fixture=1, position="MID", minute_exposure=[(90.0, 1.0)])
    calibration = CalibrationReference(reference="test", residual_mean=0.0, residual_std=1.0)
    result = write_dc_pmfs(
        temp_store, [pmf], season="2025-26", round_=1, valid_at=dt(2026, 1, 1),
        calibration_by_group={"MID_FWD_CBIRT": calibration},
    )
    assert "MID_FWD_CBIRT" in result
    assert result["MID_FWD_CBIRT"].written
    out = temp_store.as_of("derived_player_defensive_contribution_distribution", datetime.now(UTC) + __import__("datetime").timedelta(days=1))
    assert out["is_modelled"].all()
    assert out["probability"].sum() == pytest.approx(1.0)


def test_pmfs_to_rows_refuses_ineligible_pmfs():
    pmf = DCPMF(
        element=1, fixture=1, group="INELIGIBLE", eligible=False, count_threshold=None,
        threshold_verified=True, threshold_source="x", points=0, counts=(0,), probabilities=(1.0,),
        mass_before_truncation=1.0,
    )
    with pytest.raises(DCModelError):
        pmfs_to_rows([pmf], season="2025-26", round_=1)


@pytest.mark.slow
@requires_real_store
def test_composition_rule_holds_across_the_real_store_2025_26_rows():
    real_store = BitemporalStore()
    threshold_set = build_dc_threshold_set(real_store)
    table = build_training_table(real_store, as_of=dt(2026, 8, 22), threshold_set=threshold_set)
    def_rows = table.filter(pl.col("group") == "DEF_CBIT")
    mid_fwd_rows = table.filter(pl.col("group") == "MID_FWD_CBIRT")
    assert def_rows.height > 5000
    assert mid_fwd_rows.height > 5000
    assert (def_rows["count"] == def_rows["tackles"] + def_rows["clearances_blocks_interceptions"]).all()
    assert (
        mid_fwd_rows["count"]
        == mid_fwd_rows["tackles"] + mid_fwd_rows["clearances_blocks_interceptions"] + mid_fwd_rows["recoveries"]
    ).all()


@pytest.mark.slow
@requires_real_store
def test_gk_position_never_appears_in_the_training_table():
    real_store = BitemporalStore()
    threshold_set = build_dc_threshold_set(real_store)
    table = build_training_table(real_store, as_of=dt(2026, 8, 22), threshold_set=threshold_set)
    assert "GK" not in table["position"].unique().to_list()


@pytest.mark.slow
@requires_real_store
def test_dc_threshold_string_is_absent_from_the_live_game_config_payload():
    real_store = BitemporalStore()
    df = real_store.latest("game_config")
    assert "threshold" not in df["payload"][0].lower()


@pytest.mark.slow
@requires_real_store
def test_walk_forward_validate_beats_both_baselines_on_the_real_store():
    """A bounded live sanity check -- the full-window gate numbers this
    task's report cites are produced by scripts/fit_defensive_
    contribution.py and recorded in docs/wiki/model-defensive-
    contribution.md; this proves the SAME code path against the real store
    inside the regular suite too."""
    real_store = BitemporalStore()
    threshold_set = build_dc_threshold_set(real_store)
    table = build_training_table(real_store, as_of=dt(2026, 8, 22), threshold_set=threshold_set)
    for group in DC_GROUPS:
        result = walk_forward_validate(table, group=group, threshold_set=threshold_set, min_train_rows=500)
        assert result.n_folds > 10
        assert result.beats_both_baselines(), f"{group} failed to beat both §7.1 baselines"


# ---------------------------------------------------------------------------
# 4b. Pinned-threshold observations (session s004) — write, resolve,
#     bitemporal correctness, and the end-to-end "threshold_verified stops
#     lying" proof.
# ---------------------------------------------------------------------------


def _pinned_results(def_threshold=10, mid_fwd_threshold=12) -> dict:
    return {
        "DEF_CBIT": {
            "count_threshold": def_threshold,
            "lower_bound": def_threshold,
            "upper_bound": (def_threshold - 1) if def_threshold is not None else 5,
            "n_observations": 108,
            "n_unattributable": 0,
            "contradiction": False,
        },
        "MID_FWD_CBIRT": {
            "count_threshold": mid_fwd_threshold,
            "lower_bound": mid_fwd_threshold,
            "upper_bound": (mid_fwd_threshold - 1) if mid_fwd_threshold is not None else 11,
            "n_observations": 182,
            "n_unattributable": 0,
            "contradiction": False,
        },
    }


def _inconclusive_results() -> dict:
    return {
        "DEF_CBIT": {
            "count_threshold": None, "lower_bound": 15, "upper_bound": 5,
            "n_observations": 5, "n_unattributable": 0, "contradiction": False,
        },
        "MID_FWD_CBIRT": {
            "count_threshold": None, "lower_bound": 20, "upper_bound": 10,
            "n_observations": 5, "n_unattributable": 0, "contradiction": False,
        },
    }


def test_dc_threshold_observation_rows_builds_expected_shape_and_validates():
    df = dc_threshold_observation_rows(_pinned_results(), season="2026-27", round_=1)
    DC_THRESHOLD_OBSERVATION_SCHEMA.validate(df)  # must not raise
    assert set(df["group"].to_list()) == set(DC_GROUPS)
    assert df.filter(pl.col("group") == "DEF_CBIT")["count_threshold"][0] == 10


def test_dc_threshold_observation_rows_raises_if_missing_a_group():
    with pytest.raises(DCModelError):
        dc_threshold_observation_rows(
            {"DEF_CBIT": _pinned_results()["DEF_CBIT"]}, season="2026-27", round_=1
        )


def test_write_dc_threshold_observations_round_trips_via_observations(temp_store):
    result = write_dc_threshold_observations(
        temp_store, _pinned_results(mid_fwd_threshold=None), season="2026-27", round_=1, observed_at=dt(2026, 8, 27)
    )
    assert result.written
    out = temp_store.observations(DC_THRESHOLD_OBSERVATION_DATASET, until=dt(2026, 8, 28))
    assert out.height == 2
    def_row = out.filter(pl.col("group") == "DEF_CBIT")
    assert def_row["count_threshold"][0] == 10
    mid_row = out.filter(pl.col("group") == "MID_FWD_CBIRT")
    assert mid_row["count_threshold"][0] is None


def test_resolve_dc_threshold_observations_returns_empty_dict_when_nothing_written(temp_store):
    assert resolve_dc_threshold_observations(temp_store, as_of=dt(2026, 8, 28)) == {}


def test_resolve_dc_threshold_observations_requires_timezone_aware_as_of(temp_store):
    with pytest.raises(DCModelError):
        resolve_dc_threshold_observations(temp_store, as_of=datetime(2026, 8, 28))


def test_resolve_dc_threshold_observations_never_treats_an_inconclusive_row_as_pinned(temp_store):
    """Break-first proof (part 1): a group with ONLY an inconclusive
    (count_threshold IS NULL) observation on file must resolve to nothing,
    so build_dc_threshold_set falls back to the press default,
    verified=False. Attacked directly: a naive implementation that checks
    'does a row exist for this group' instead of 'is count_threshold
    non-null' would wrongly treat this as pinned -- reverting the
    `.filter(pl.col("count_threshold").is_not_null())` line in
    `resolve_dc_threshold_observations` (leaving only the group filter)
    was checked to make this test fail before the real implementation was
    restored, confirming this test actually exercises that line."""
    write_dc_threshold_observations(
        temp_store, _inconclusive_results(), season="2026-27", round_=1, observed_at=dt(2026, 8, 27)
    )
    resolved = resolve_dc_threshold_observations(temp_store, as_of=dt(2026, 8, 28))
    assert resolved == {}


def test_resolve_dc_threshold_observations_never_leaks_a_pin_observed_after_as_of(temp_store):
    """Break-first proof (part 2), CLAUDE.md rule 2: a pin physically
    present in the store with a LATER observed_at must not be visible to
    a query resolved at an EARLIER as_of, even though the row already
    exists on disk by the time this test queries it. Attacked directly:
    dropping the `until=as_of` bitemporal filter (reading the dataset's
    full history unconditionally) was checked to make the 'before' half
    of this assertion fail before the real filter was restored."""
    write_dc_threshold_observations(
        temp_store, _pinned_results(), season="2026-27", round_=1, observed_at=dt(2026, 8, 27, 12)
    )
    before = resolve_dc_threshold_observations(temp_store, as_of=dt(2026, 8, 27, 11))
    assert before == {}
    after = resolve_dc_threshold_observations(temp_store, as_of=dt(2026, 8, 27, 13))
    assert after["DEF_CBIT"].verified is True
    assert after["DEF_CBIT"].count_threshold == 10


def test_resolve_dc_threshold_observations_latest_pin_wins_over_an_earlier_one(temp_store):
    """A genuine mid-season rule change (a later round pinning a DIFFERENT
    value) must be picked up, not stuck on the first-ever pin forever."""
    write_dc_threshold_observations(
        temp_store, _pinned_results(def_threshold=10), season="2026-27", round_=1, observed_at=dt(2026, 8, 27)
    )
    write_dc_threshold_observations(
        temp_store, _pinned_results(def_threshold=11), season="2026-27", round_=10, observed_at=dt(2026, 10, 20)
    )
    resolved = resolve_dc_threshold_observations(temp_store, as_of=dt(2026, 10, 21))
    assert resolved["DEF_CBIT"].count_threshold == 11


def test_resolve_dc_threshold_observations_keeps_earlier_pin_when_a_later_round_is_inconclusive(temp_store):
    """The other half of the same policy: a later round that individually
    failed to reconfirm the threshold must never silently WITHDRAW an
    earlier real pin."""
    write_dc_threshold_observations(
        temp_store, _pinned_results(def_threshold=10), season="2026-27", round_=1, observed_at=dt(2026, 8, 27)
    )
    write_dc_threshold_observations(
        temp_store, _inconclusive_results(), season="2026-27", round_=10, observed_at=dt(2026, 10, 20)
    )
    resolved = resolve_dc_threshold_observations(temp_store, as_of=dt(2026, 10, 21))
    assert resolved["DEF_CBIT"].count_threshold == 10


def test_build_dc_threshold_set_stays_press_sourced_when_as_of_is_none(temp_store):
    """Backward compatibility: a caller that does not pass `as_of` (the
    only behaviour that existed before this session) is completely
    unaffected by a pin sitting in the store."""
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    write_dc_threshold_observations(
        temp_store, _pinned_results(), season="2026-27", round_=1, observed_at=dt(2026, 8, 27)
    )
    threshold_set = build_dc_threshold_set(temp_store)
    assert threshold_set.threshold("DEF_CBIT").verified is False


def test_build_dc_threshold_set_resolves_pinned_verified_true_per_group_when_as_of_given(temp_store):
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    write_dc_threshold_observations(
        temp_store, _pinned_results(mid_fwd_threshold=None), season="2026-27", round_=1, observed_at=dt(2026, 8, 27)
    )
    threshold_set = build_dc_threshold_set(temp_store, as_of=dt(2026, 8, 28))
    assert threshold_set.threshold("DEF_CBIT").verified is True
    assert threshold_set.threshold("DEF_CBIT").count_threshold == 10
    # never pinned (this run was inconclusive) -- stays press-sourced, unverified
    assert threshold_set.threshold("MID_FWD_CBIRT").verified is False
    assert threshold_set.threshold("MID_FWD_CBIRT").count_threshold == 12


def _dc_training_rows() -> list[dict]:
    return [
        _row("2025-26", 1, 10, 1, "2025-08-06T14:00:00Z", position="DEF", tackles=5, cbi=5, recoveries=0),
        _row("2025-26", 1, 11, 2, "2025-08-06T14:00:00Z", position="DEF", tackles=2, cbi=2, recoveries=0),
        _row("2025-26", 2, 10, 3, "2025-08-13T14:00:00Z", position="DEF", tackles=3, cbi=3, recoveries=0),
        _row("2025-26", 1, 20, 4, "2025-08-06T14:00:00Z", position="MID", tackles=4, cbi=4, recoveries=4),
        _row("2025-26", 1, 21, 5, "2025-08-06T14:00:00Z", position="MID", tackles=1, cbi=1, recoveries=1),
        _row("2025-26", 2, 20, 6, "2025-08-13T14:00:00Z", position="MID", tackles=2, cbi=2, recoveries=2),
    ]


def test_fit_dc_model_default_threshold_set_picks_up_a_real_pin_end_to_end(temp_store):
    """The end-to-end proof this task exists for: threshold_verified must
    stop lying once a real pin is on file, through the SAME call path
    every real production caller uses (fit_dc_model's own default,
    threshold_set=None)."""
    _write_fixture(temp_store, _dc_training_rows(), observed_at=dt(2026, 1, 1))
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    write_dc_threshold_observations(
        temp_store, _pinned_results(), season="2025-26", round_=1, observed_at=dt(2026, 1, 2)
    )
    params = fit_dc_model(temp_store, as_of=dt(2026, 1, 3))
    assert params.threshold_set.threshold("DEF_CBIT").verified is True
    assert params.groups["DEF_CBIT"].threshold.verified is True
    assert params.groups["MID_FWD_CBIRT"].threshold.verified is True


def test_fit_dc_model_before_the_pin_was_observed_stays_press_sourced_and_unverified(temp_store):
    """The bitemporal half of the same proof: an as_of BEFORE the pin was
    ever observed must never see it, even though the row exists on disk
    by the time this test runs."""
    _write_fixture(temp_store, _dc_training_rows(), observed_at=dt(2026, 1, 1))
    _write_game_config(temp_store, observed_at=dt(2026, 1, 1))
    write_dc_threshold_observations(
        temp_store, _pinned_results(), season="2025-26", round_=1, observed_at=dt(2026, 1, 2)
    )
    params = fit_dc_model(temp_store, as_of=dt(2026, 1, 1, 13))  # before the pin's own observed_at (Jan 2)
    assert params.threshold_set.threshold("DEF_CBIT").verified is False
    assert params.threshold_set.threshold("DEF_CBIT").count_threshold == PRESS_DC_COUNT_THRESHOLDS["DEF_CBIT"].count_threshold


# ---------------------------------------------------------------------------
# 5. scripts/pin_dc_thresholds.py — duck-typed fake client, no real network.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _add_scripts_to_path():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    yield


class _FakeClient:
    def __init__(self, *, event_live_payload, bootstrap_payload, event_status_payload=None):
        self._event_live_payload = event_live_payload
        self._bootstrap_payload = bootstrap_payload
        self._event_status_payload = event_status_payload or {"status": [{"bonus_added": True, "date": "2026-08-21", "event": 1}]}

    def event_live(self, gw, **kwargs):
        return self._event_live_payload

    def bootstrap_static(self, **kwargs):
        return self._bootstrap_payload

    def event_status(self, **kwargs):
        return self._event_status_payload


_BOOTSTRAP = {
    "element_types": [
        {"id": 1, "singular_name_short": "GKP"},
        {"id": 2, "singular_name_short": "DEF"},
        {"id": 3, "singular_name_short": "MID"},
        {"id": 4, "singular_name_short": "FWD"},
    ],
    "elements": [
        {"id": 1, "element_type": 2},  # DEF
        {"id": 2, "element_type": 2},  # DEF
        {"id": 3, "element_type": 3},  # MID
    ],
    "events": [{"id": 1, "finished": True, "data_checked": True}],
}


def _explain_row(element_id, fixture_points):
    return {
        "id": element_id,
        "explain": [
            {"fixture": fx, "stats": [{"identifier": "defensive_contribution", "points": pts, "value": val}]}
            for fx, (pts, val) in enumerate(fixture_points)
        ],
    }


def test_pin_thresholds_pins_exact_threshold_from_clean_bounds():
    import pin_dc_thresholds as script

    payload = {
        "elements": [
            _explain_row(1, [(2, 10), (0, 9)]),  # DEF: min-with-2=10, max-with-0=9 -> pinned 10
            _explain_row(2, [(0, 8), (2, 11)]),
            _explain_row(3, [(2, 12), (0, 11)]),  # MID: pinned 12
        ]
    }
    client = _FakeClient(event_live_payload=payload, bootstrap_payload=_BOOTSTRAP)
    results = script.pin_thresholds(client, gw=1)
    assert results["DEF_CBIT"]["count_threshold"] == 10
    assert results["DEF_CBIT"]["contradiction"] is False
    assert results["MID_FWD_CBIRT"]["count_threshold"] == 12


def test_pin_thresholds_reports_inconclusive_when_bounds_have_a_gap():
    payload = {"elements": [_explain_row(1, [(2, 15), (0, 5)])]}  # DEF: gap between 5 and 15
    client = _FakeClient(event_live_payload=payload, bootstrap_payload=_BOOTSTRAP)
    import pin_dc_thresholds as script

    results = script.pin_thresholds(client, gw=1)
    assert results["DEF_CBIT"]["count_threshold"] is None
    assert results["DEF_CBIT"]["lower_bound"] == 15
    assert results["DEF_CBIT"]["upper_bound"] == 5


def test_pin_thresholds_flags_a_contradiction_without_resolving_it():
    payload = {"elements": [_explain_row(1, [(2, 10), (0, 10)])]}  # same count, both outcomes
    client = _FakeClient(event_live_payload=payload, bootstrap_payload=_BOOTSTRAP)
    import pin_dc_thresholds as script

    results = script.pin_thresholds(client, gw=1)
    assert results["DEF_CBIT"]["contradiction"] is True
    assert results["DEF_CBIT"]["count_threshold"] is None


def test_pin_thresholds_raises_on_an_unexpected_points_value():
    payload = {"elements": [_explain_row(1, [(1, 10)])]}  # points=1 is not 0 or 2 -- capped at 2
    client = _FakeClient(event_live_payload=payload, bootstrap_payload=_BOOTSTRAP)
    import pin_dc_thresholds as script

    with pytest.raises(script.PinDCThresholdsError):
        script.pin_thresholds(client, gw=1)


def test_gw_settled_per_store_false_when_not_finished(temp_store):
    import pin_dc_thresholds as script

    events = pl.DataFrame({"id": [1], "finished": [False], "data_checked": [False]})
    temp_store.write("events", events, valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    settled, reason = script._gw_settled_per_store(temp_store, 1)
    assert settled is False


def test_gw_settled_per_store_true_when_finished_and_data_checked(temp_store):
    import pin_dc_thresholds as script

    events = pl.DataFrame({"id": [1], "finished": [True], "data_checked": [True]})
    temp_store.write("events", events, valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    settled, reason = script._gw_settled_per_store(temp_store, 1)
    assert settled is True


def test_gw_settled_per_live_status_false_when_bonus_not_added():
    import pin_dc_thresholds as script

    client = _FakeClient(
        event_live_payload={"elements": []}, bootstrap_payload=_BOOTSTRAP,
        event_status_payload={"status": [{"bonus_added": False, "date": "2026-08-21", "event": 1}]},
    )
    settled, reason = script._gw_settled_per_live_status(client, 1)
    assert settled is False


def test_gw_settled_per_live_status_true_when_everything_agrees():
    import pin_dc_thresholds as script

    client = _FakeClient(
        event_live_payload={"elements": []}, bootstrap_payload=_BOOTSTRAP,
        event_status_payload={"status": [{"bonus_added": True, "date": "2026-08-21", "event": 1}]},
    )
    settled, reason = script._gw_settled_per_live_status(client, 1)
    assert settled is True


@pytest.mark.slow
@requires_real_store
def test_pin_dc_thresholds_script_exits_cleanly_for_an_unsettled_gameweek():
    """LIVE VERIFICATION: the 'not settled yet' path, against the real
    store AND the real script entry point, not a mock.

    Originally written (s003) against a hardcoded GW1, which was unsettled
    at the time. That premise EXPIRED the moment GW1 settled on 25 Aug and
    the test began failing for a reason that had nothing to do with the
    behaviour it guards -- a test whose truth decays with the calendar.
    It now resolves the target gameweek from the real store instead, so it
    keeps testing the same thing every week of the season.
    """
    root = Path(__file__).resolve().parents[1]
    from fplai.store import BitemporalStore

    events = BitemporalStore().latest("events")
    unsettled = events.filter(~(pl.col("finished") & pl.col("data_checked"))).sort("id")
    if unsettled.is_empty():
        pytest.skip("every gameweek in the real store has settled -- no unsettled path to exercise")
    gw = int(unsettled["id"][0])

    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "pin_dc_thresholds.py"), "--gw", str(gw), "--season", "2026-27"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = (result.stdout + result.stderr).lower()
    assert "not settled" in output or "expected state" in output


def test_main_persists_a_pin_attempt_end_to_end_with_a_fake_client_and_temp_store(tmp_path, monkeypatch):
    """The full `main()` wiring, session s004: a settled gameweek's fake
    payload must actually reach the store via `write_dc_threshold_
    observations`, not merely print. No real network call (a duck-typed
    fake client, same as every other test in this section); the store is
    a real BitemporalStore at a temp path, monkeypatched in place of the
    script's own `BitemporalStore()` construction."""
    import pin_dc_thresholds as script

    temp_store = BitemporalStore(base_path=tmp_path / "store")
    events = pl.DataFrame({"id": [1], "finished": [True], "data_checked": [True]})
    temp_store.write("events", events, valid_at=dt(2026, 8, 27), observed_at=dt(2026, 8, 27), source="test")

    payload = {
        "elements": [
            _explain_row(1, [(2, 10), (0, 9)]),  # DEF: pinned 10
            _explain_row(2, [(0, 8), (2, 11)]),
            _explain_row(3, [(2, 12), (0, 11)]),  # MID: pinned 12
        ]
    }
    fake_client = _FakeClient(event_live_payload=payload, bootstrap_payload=_BOOTSTRAP)

    monkeypatch.setattr(script, "BitemporalStore", lambda *a, **k: temp_store)
    monkeypatch.setattr(script, "FPLClient", lambda *a, **k: fake_client)
    monkeypatch.setattr(sys, "argv", ["pin_dc_thresholds.py", "--gw", "1", "--season", "2026-27"])

    rc = script.main()
    assert rc == 0

    # Cutoff computed from the clock, never hardcoded: `main()` stamps
    # `observed_at` with the real now, so a literal date here silently stops
    # matching the moment the calendar passes it. This test was written on
    # 2026-08-28 with `until=dt(2026, 8, 28)` and began failing at midnight —
    # the third calendar-decaying test found in this session alone, and the
    # only one that broke with no code change at all.
    out = temp_store.observations(
        "dc_threshold_observations", until=datetime.now(timezone.utc) + timedelta(days=1)
    )
    assert out.height == 2
    assert out.filter(pl.col("group") == "DEF_CBIT")["count_threshold"][0] == 10
    assert out.filter(pl.col("group") == "MID_FWD_CBIRT")["count_threshold"][0] == 12


def test_main_skip_persist_writes_nothing(tmp_path, monkeypatch):
    import pin_dc_thresholds as script

    temp_store = BitemporalStore(base_path=tmp_path / "store")
    events = pl.DataFrame({"id": [1], "finished": [True], "data_checked": [True]})
    temp_store.write("events", events, valid_at=dt(2026, 8, 27), observed_at=dt(2026, 8, 27), source="test")

    payload = {"elements": [_explain_row(1, [(2, 10), (0, 9)])]}
    fake_client = _FakeClient(event_live_payload=payload, bootstrap_payload=_BOOTSTRAP)

    monkeypatch.setattr(script, "BitemporalStore", lambda *a, **k: temp_store)
    monkeypatch.setattr(script, "FPLClient", lambda *a, **k: fake_client)
    monkeypatch.setattr(
        sys, "argv", ["pin_dc_thresholds.py", "--gw", "1", "--season", "2026-27", "--skip-persist"]
    )

    rc = script.main()
    assert rc == 0
    out = temp_store.observations("dc_threshold_observations", until=dt(2026, 8, 28))
    assert out.is_empty()

# Deliberately no automated suite test runs `scripts/pin_dc_thresholds.py`
# against the real live API AND the real `data/store/` for the SETTLED
# path (unlike the unsettled path above, which only reads): that would
# make every `pytest` run write a new batch into the project's production
# store and hit the live API on every invocation. This session's live
# verification for the settled/persist path is a one-off manual run
# (see this session's punch-out for the exact command and its output),
# not a standing suite member -- the fake-client `main()` tests above
# already cover the same code path deterministically.
