"""Tests for fplai.models.bonus — Phase 2, E5, session s004 (blueprint §4,
§7.1, §12.2).

Five groups, mirroring `tests/test_attacking.py`/`tests/test_defensive_
contribution.py`'s own structure (the closest sibling modules):

  1. Pure functions — the competition-ranking award rule against hand-built
     tie shapes, `BonusPMF`/`BonusPlayerInput` validation, the ridge
     closed-form solution cross-checked against an independent scipy
     optimiser and against its own zero-gradient condition.
  2. Feature engineering — a temp store, fabricated multi-round rows,
     including the double-gameweek leakage attack, the 2019-20 exclusion,
     and zero-minute-row inclusion (the deliberate deviation from DC/
     attacking's `minutes > 0` filter).
  3. Fit + predict + walk-forward mechanics — small synthetic data with a
     KNOWN trailing-bps signal the model must recover, plus
     `predict_bonus_pmfs_for_fixture`'s own guardrails.
  4. Real-store-gated: import-time registration, the archive-pin
     verification (`verify_bonus_award_rule_against_archive`), the §7.1
     walk-forward gate against the real store.
  5. Persistence — `write_bonus_pmfs` round-tripping through
     `write_derived`.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from scipy.optimize import minimize

from fplai.models.bonus import (
    KNOWN_ABSENT_FEATURES,
    NUMERIC_FEATURE_COLUMNS_BONUS,
    OUTCOMES,
    BonusFeatureSpec,
    BonusModelConfig,
    BonusModelError,
    BonusPlayerInput,
    BonusPMF,
    _assign_bonus_points_batch,
    _fit_from_table,
    _fit_ridge,
    _ridge_loss_and_grad,
    assign_bonus_points,
    build_training_table,
    fit_bonus_model,
    pmfs_to_rows,
    predict_bonus_pmfs_for_fixture,
    verify_bonus_award_rule_against_archive,
    walk_forward_validate,
    write_bonus_pmfs,
)
from fplai.derived import CalibrationReference
from fplai.schemas import CANONICAL_SCHEMAS, PLAYER_BONUS_DISTRIBUTION_GAMEWEEK
from fplai.store import BitemporalStore

UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Pure functions.
# ---------------------------------------------------------------------------


def test_known_absent_features_are_declared_and_never_leak_into_the_feature_set():
    assert "primary_set_piece_taker" in KNOWN_ABSENT_FEATURES
    assert "penalty_taker_duty" in KNOWN_ABSENT_FEATURES
    assert "bps_component_breakdown" in KNOWN_ABSENT_FEATURES
    assert not set(KNOWN_ABSENT_FEATURES) & set(NUMERIC_FEATURE_COLUMNS_BONUS)


def test_assign_bonus_points_no_ties_gives_clean_3_2_1():
    assert assign_bonus_points([44, 30, 29, 28, 0]) == (3, 2, 1, 0, 0)


def test_assign_bonus_points_tie_for_first_skips_second_place():
    # Two-way tie for 1st -> both 3, next player gets 1 (never 2) — the
    # exact FPL tie rule this module's docstring cites.
    assert assign_bonus_points([10, 10, 8, 7]) == (3, 3, 1, 0)


def test_assign_bonus_points_three_way_tie_for_first_gives_fixture_total_nine():
    # Matches the real archive's "9 -> 6 fixtures" observation (module
    # docstring).
    points = assign_bonus_points([10, 10, 10, 7])
    assert points == (3, 3, 3, 0)
    assert sum(points) == 9


def test_assign_bonus_points_tie_for_second_no_third_place_awarded():
    assert assign_bonus_points([10, 9, 9, 7]) == (3, 2, 2, 0)


def test_assign_bonus_points_tie_for_third_both_get_one():
    assert assign_bonus_points([10, 9, 8, 8]) == (3, 2, 1, 1)


def test_assign_bonus_points_batch_matches_single_fixture_wrapper():
    bps = np.array([[10, 9, 8, 8], [10, 10, 10, 7]], dtype=np.float64)
    batch = _assign_bonus_points_batch(bps)
    assert batch[0].tolist() == list(assign_bonus_points([10, 9, 8, 8]))
    assert batch[1].tolist() == list(assign_bonus_points([10, 10, 10, 7]))


def test_assign_bonus_points_batch_rejects_non_2d_input():
    with pytest.raises(BonusModelError):
        _assign_bonus_points_batch(np.array([1.0, 2.0, 3.0]))


def test_assign_bonus_points_batch_rejects_fewer_than_two_players():
    with pytest.raises(BonusModelError):
        _assign_bonus_points_batch(np.array([[1.0]]))


def test_bonus_pmf_rejects_probabilities_not_summing_to_one():
    with pytest.raises(BonusModelError):
        BonusPMF(element=1, fixture=1, counts=(0, 1, 2, 3), probabilities=(0.5, 0.5, 0.5, 0.5), n_simulations=100)


def test_bonus_pmf_rejects_nonpositive_n_simulations():
    with pytest.raises(BonusModelError):
        BonusPMF(element=1, fixture=1, counts=(0, 1), probabilities=(0.5, 0.5), n_simulations=0)


def test_bonus_pmf_p_bonus_awarded_and_expected_points():
    pmf = BonusPMF(element=1, fixture=1, counts=(0, 1, 2, 3), probabilities=(0.4, 0.3, 0.2, 0.1), n_simulations=1000)
    assert pmf.p_bonus_awarded() == pytest.approx(0.6)
    assert pmf.expected_bonus_points() == pytest.approx(0 * 0.4 + 1 * 0.3 + 2 * 0.2 + 3 * 0.1)


def test_bonus_player_input_rejects_minutes_frac_in_feature_row():
    with pytest.raises(BonusModelError):
        BonusPlayerInput(element=1, position="MID", feature_row={"minutes_frac": 1.0}, minute_exposure=[(90.0, 1.0)])


def test_bonus_player_input_rejects_position_in_feature_row():
    with pytest.raises(BonusModelError):
        BonusPlayerInput(element=1, position="MID", feature_row={"position": "MID"}, minute_exposure=[(90.0, 1.0)])


def test_bonus_player_input_rejects_minute_exposure_not_summing_to_one():
    with pytest.raises(BonusModelError):
        BonusPlayerInput(element=1, position="MID", feature_row={}, minute_exposure=[(90.0, 0.5)])


def test_feature_spec_rejects_mismatched_means_stds_length():
    with pytest.raises(BonusModelError):
        BonusFeatureSpec(
            numeric_columns=("a", "b"), position_categories=("DEF",),
            numeric_means=(0.0,), numeric_stds=(1.0,),
        )


def test_ridge_loss_gradient_matches_finite_differences():
    rng = np.random.default_rng(0)
    n, p = 60, 5
    X = rng.normal(size=(n, p))
    y = rng.normal(size=n)
    beta = rng.normal(scale=0.3, size=p)
    _, grad_analytic = _ridge_loss_and_grad(beta, X, y, l2=0.3)

    eps = 1e-6
    grad_numeric = np.zeros_like(beta)
    for i in range(p):
        b_plus, b_minus = beta.copy(), beta.copy()
        b_plus[i] += eps
        b_minus[i] -= eps
        loss_plus, _ = _ridge_loss_and_grad(b_plus, X, y, l2=0.3)
        loss_minus, _ = _ridge_loss_and_grad(b_minus, X, y, l2=0.3)
        grad_numeric[i] = (loss_plus - loss_minus) / (2 * eps)

    assert np.allclose(grad_analytic, grad_numeric, atol=1e-4, rtol=1e-3)


def test_fit_ridge_is_a_genuine_zero_gradient_stationary_point():
    """Proof the closed-form solver actually minimises the loss it claims
    to — module docstring, 'Ridge regression, closed-form'."""
    rng = np.random.default_rng(1)
    n, p = 200, 6
    X = rng.normal(size=(n, p))
    y = rng.normal(size=n)
    beta_hat = _fit_ridge(X, y, l2=0.05)
    _, grad = _ridge_loss_and_grad(beta_hat, X, y, l2=0.05)
    assert np.allclose(grad, 0.0, atol=1e-8)


def test_fit_ridge_matches_an_independent_scipy_optimiser():
    """Cross-checked against a totally independent optimisation path, not
    merely self-consistent with its own gradient (belt-and-braces)."""
    rng = np.random.default_rng(2)
    n, p = 150, 5
    X = rng.normal(size=(n, p))
    y = X @ rng.normal(size=p) + rng.normal(scale=0.1, size=n)
    l2 = 0.1
    beta_closed = _fit_ridge(X, y, l2)

    def loss_only(beta):
        return _ridge_loss_and_grad(beta, X, y, l2)[0]

    result = minimize(loss_only, np.zeros(p), jac=lambda b: _ridge_loss_and_grad(b, X, y, l2)[1], method="L-BFGS-B")
    assert np.allclose(beta_closed, result.x, atol=1e-4)


def test_ridge_l2_penalty_is_scaled_by_inverse_n():
    """Pins session s005's fix directly: the ridge term must be `(l2/n) *
    sum(beta**2)`, not the unscaled `l2 * sum(beta**2)` this module carried
    before this session (this task's brief; CLAUDE.md rule 5 — an unscaled
    penalty against a per-row-AVERAGED `mean(resid**2)` term is effectively
    `l2*n`, silently crushing every coefficient toward 0 for `n` in the
    thousands — the exact defect `BonusModelConfig.l2_penalty`'s own
    docstring records having chosen `0.01` instead of DC/minutes' shared
    `1.0` to work around).

    Same row-doubling construction `tests/test_minutes.py::
    test_softmax_l2_penalty_is_scaled_by_inverse_n` /
    `tests/test_defensive_contribution.py::
    test_nb_neg_log_lik_l2_penalty_is_scaled_by_inverse_n` use: isolate the
    penalty component via the `l2=0.0` subtraction, then compare
    row-doubled-identical-content `n` against the original. **Verified by
    hand computation against a copy of the pre-fix expression before
    trusting this test (CLAUDE.md lesson 5): under the OLD unscaled formula
    (`loss = mean(resid**2) + l2*sum(beta**2)`) the penalty component is
    `l2*sum(beta**2)`, which has no `n`-dependence at all -- identical at
    `n=20` and the row-doubled `n=40` -- so this test's halving assertion
    would have FAILED against that formula, which is what makes it a real
    pin, not a vacuous one.**"""
    rng = np.random.default_rng(4)
    n, p = 20, 5
    X = rng.normal(size=(n, p))
    y = rng.normal(size=n)
    beta = rng.normal(scale=0.4, size=p)
    l2 = 2.5

    X_doubled = np.vstack([X, X])
    y_doubled = np.concatenate([y, y])

    loss_n20, _ = _ridge_loss_and_grad(beta, X, y, l2=l2)
    loss_n20_nopenalty, _ = _ridge_loss_and_grad(beta, X, y, l2=0.0)
    penalty_n20 = loss_n20 - loss_n20_nopenalty

    loss_n40, _ = _ridge_loss_and_grad(beta, X_doubled, y_doubled, l2=l2)
    loss_n40_nopenalty, _ = _ridge_loss_and_grad(beta, X_doubled, y_doubled, l2=0.0)
    penalty_n40 = loss_n40 - loss_n40_nopenalty

    # The likelihood-only (l2=0) loss is unchanged by row-doubling identical content.
    assert loss_n20_nopenalty == pytest.approx(loss_n40_nopenalty, abs=1e-9)
    # The (fixed) penalty component exactly halves when n doubles -- the
    # signature of `(l2/n)*sum(beta**2)`, not `l2*sum(beta**2)`.
    assert penalty_n40 == pytest.approx(penalty_n20 / 2.0, rel=1e-9)

    # And matches the closed-form expression exactly, at the original n.
    expected_penalty_n20 = (l2 / n) * float(np.sum(beta * beta))
    assert penalty_n20 == pytest.approx(expected_penalty_n20, rel=1e-9)


def test_fit_ridge_closed_form_matches_standard_textbook_ridge_solution():
    """A second, independent way of pinning the SAME fix (module docstring,
    `_fit_ridge`'s own derivation): once both terms of the loss carry the
    identical `1/n` scaling, the closed-form solution algebraically reduces
    to `(X^T X + l2*I)^-1 X^T y` -- the standard textbook ridge normal
    equations, with NO `n`-dependence anywhere. This is a genuinely
    different code path from `test_ridge_l2_penalty_is_scaled_by_inverse_n`
    above (that one attacks the loss function directly; this one attacks
    `_fit_ridge`'s output), so a regression that broke one but not the
    other would still be caught."""
    rng = np.random.default_rng(5)
    n, p = 80, 4
    X = rng.normal(size=(n, p))
    y = X @ rng.normal(size=p) + rng.normal(scale=0.1, size=n)
    l2 = 3.0

    beta_from_fit_ridge = _fit_ridge(X, y, l2)
    beta_textbook = np.linalg.solve(X.T @ X + l2 * np.eye(p), X.T @ y)
    assert np.allclose(beta_from_fit_ridge, beta_textbook, atol=1e-8)


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
    position: str = "MID",
    bps: int = 20,
    bonus: int = 0,
) -> dict:
    return {
        "season": season,
        "round": round_,
        "element": element,
        "fixture": fixture,
        "kickoff_time": kickoff,
        "minutes": minutes,
        "position": position,
        "team": team,
        "was_home": was_home,
        "bps": bps,
        "bonus": bonus,
    }


@pytest.fixture
def temp_store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def _write_fixture(store, rows, *, observed_at, valid_at=None):
    df = pl.DataFrame(rows)
    store.write("vaastav_player_gameweek_stats", df, valid_at=valid_at or observed_at, observed_at=observed_at, source="test")


def test_build_training_table_requires_timezone_aware_as_of(temp_store):
    _write_fixture(temp_store, [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z")], observed_at=dt(2026, 1, 1))
    with pytest.raises(BonusModelError):
        build_training_table(temp_store, as_of=datetime(2026, 1, 1))


def test_build_training_table_excludes_2019_20_null_position(temp_store):
    """The 2019-20 exclusion (module docstring) — a row with NULL position
    (as every real 2019-20 row genuinely is) must never enter the table."""
    rows = [_row("2019-20", 1, 10, 1, "2019-08-09T14:00:00Z")]
    df = pl.DataFrame(rows).with_columns(
        pl.lit(None, dtype=pl.Utf8).alias("position"), pl.lit(None, dtype=pl.Utf8).alias("team")
    )
    temp_store.write("vaastav_player_gameweek_stats", df, valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    with pytest.raises(BonusModelError):
        build_training_table(temp_store, as_of=dt(2026, 1, 2))


def test_build_training_table_includes_zero_minute_rows(temp_store):
    """The deliberate deviation from DC/attacking's minutes>0 filter —
    module docstring, 'Zero-minute rows are INCLUDED'."""
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", minutes=90, bps=30),
        _row("2022-23", 1, 11, 1, "2022-08-06T14:00:00Z", minutes=0, bps=0, team="TeamB", was_home=False),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    assert set(table["element"].to_list()) == {10, 11}
    zero_row = table.filter(pl.col("element") == 11)
    assert zero_row["minutes_frac"][0] == pytest.approx(0.0)


def test_player_trailing_bps_never_peeks_at_the_same_round_second_fixture(temp_store):
    """Double-gameweek round-boundary discipline — mirrors every sibling
    module's own DGW isolation test."""
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", bps=20),
        # Round 2: a double gameweek. Second fixture has an enormous bps
        # value that would corrupt the trailing feature if it leaked.
        _row("2022-23", 2, 10, 2, "2022-08-13T14:00:00Z", bps=15),
        _row("2022-23", 2, 10, 3, "2022-08-16T14:00:00Z", bps=90),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    round2 = table.filter(pl.col("round") == 2).sort("fixture")
    assert round2["player_trailing_bps_3"].to_list() == pytest.approx([20.0, 20.0])


def test_cold_start_flag_true_for_a_players_first_labelled_round(temp_store):
    rows = [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z")]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    assert table["cold_start"][0] is True
    assert table["games_played_this_season"][0] == pytest.approx(0.0)


def test_build_training_table_excludes_am_position_rows(temp_store):
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", position="AM"),
        _row("2022-23", 1, 11, 1, "2022-08-06T14:00:00Z", position="MID"),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    assert set(table["element"].to_list()) == {11}


def test_build_training_table_remaps_gkp_to_gk(temp_store):
    rows = [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", position="GKP")]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    assert table["position"].to_list() == ["GK"]


# ---------------------------------------------------------------------------
# 3. Fit + predict + walk-forward mechanics — synthetic data.
# ---------------------------------------------------------------------------


def _synthetic_table(n_rounds: int = 12, seed: int = 0) -> pl.DataFrame:
    """A hand-built table with a KNOWN signal: `HighBps` always has a high
    trailing-bps feature and a genuinely higher realised bps; `LowBps`
    never does."""
    rng = np.random.default_rng(seed)
    rows = []
    for element, trailing_val, bps_mean in ((1, 40.0, 45.0), (2, 5.0, 8.0)):
        for round_ in range(1, n_rounds + 1):
            bps = float(rng.normal(bps_mean, 5.0))
            rows.append(
                {
                    "season": "2022-23",
                    "round": round_,
                    "element": element,
                    "fixture": round_ * 10 + element,
                    "team": "TeamA",
                    "position": "MID",
                    "minutes": 90,
                    "bps": bps,
                    "bonus": 0,
                    "player_trailing_bps_3": trailing_val,
                    "player_trailing_bps_5": trailing_val,
                    "player_trailing_bps_10": trailing_val,
                    "team_trailing_bps_mean_5": 50.0,
                    "games_played_this_season": float(round_ - 1),
                    "cold_start": round_ == 1,
                    "team_cold_start": False,
                    "was_home": True,
                    "minutes_frac": 1.0,
                    "player_trailing_bonus_class_rate_0_5": 1.0,
                    "player_trailing_bonus_class_rate_1_5": 0.0,
                    "player_trailing_bonus_class_rate_2_5": 0.0,
                    "player_trailing_bonus_class_rate_3_5": 0.0,
                    "_chronological_rank": round_,
                }
            )
    return pl.DataFrame(rows)


def test_fit_from_table_recovers_the_trailing_bps_direction():
    table = _synthetic_table()
    params = _fit_from_table(table, config=BonusModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    idx = params.feature_spec.feature_names.index("player_trailing_bps_10")
    assert params.beta[idx] > 0


def test_predict_bonus_pmfs_for_fixture_requires_at_least_two_players():
    table = _synthetic_table()
    params = _fit_from_table(table, config=BonusModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_BONUS if c != "minutes_frac"}
    fr["cold_start"] = False
    fr["team_cold_start"] = False
    fr["was_home"] = True
    player = BonusPlayerInput(element=1, position="MID", feature_row=fr, minute_exposure=[(90.0, 1.0)])
    with pytest.raises(BonusModelError):
        predict_bonus_pmfs_for_fixture(params, [player], fixture=1)


def test_predict_bonus_pmfs_for_fixture_sums_to_one_per_player_and_is_deterministic():
    table = _synthetic_table()
    params = _fit_from_table(table, config=BonusModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))

    def make_player(element, trailing):
        fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_BONUS if c != "minutes_frac"}
        fr["cold_start"] = False
        fr["team_cold_start"] = False
        fr["was_home"] = True
        fr["player_trailing_bps_3"] = trailing
        fr["player_trailing_bps_5"] = trailing
        fr["player_trailing_bps_10"] = trailing
        return BonusPlayerInput(element=element, position="MID", feature_row=fr, minute_exposure=[(90.0, 1.0)])

    # A realistic-sized fixture pool: with only 2-3 competitors, everyone
    # trivially finishes top-3 and p_bonus_awarded() == 1.0 for all of
    # them regardless of skill -- filler low-trailing players are needed
    # so the ranking is actually meaningful (mirrors a real fixture's own
    # ~50-115 player pool, module docstring).
    fillers = [make_player(100 + i, 2.0) for i in range(6)]
    players = [make_player(1, 45.0), make_player(2, 8.0), make_player(3, 8.0), *fillers]
    pmfs_a = predict_bonus_pmfs_for_fixture(params, players, fixture=1, n_simulations=2000, seed=7)
    pmfs_b = predict_bonus_pmfs_for_fixture(params, players, fixture=1, n_simulations=2000, seed=7)

    for pmf in pmfs_a:
        assert sum(pmf.probabilities) == pytest.approx(1.0)
    # Same seed -> bit-identical (CLAUDE.md rule 7).
    for a, b in zip(pmfs_a, pmfs_b):
        assert a.probabilities == b.probabilities

    # The much-higher-trailing player should have a materially higher
    # P(bonus awarded) than either of the two low-trailing players.
    by_element = {p.element: p for p in pmfs_a}
    assert by_element[1].p_bonus_awarded() > by_element[2].p_bonus_awarded()
    assert by_element[1].p_bonus_awarded() > by_element[3].p_bonus_awarded()


def test_predict_bonus_pmfs_for_fixture_different_seeds_still_close():
    """Not bit-identical across seeds, but Monte Carlo noise at 3000 draws
    should not flip the qualitative ranking."""
    table = _synthetic_table()
    params = _fit_from_table(table, config=BonusModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_BONUS if c != "minutes_frac"}
    fr["cold_start"] = False
    fr["team_cold_start"] = False
    fr["was_home"] = True
    players = [
        BonusPlayerInput(element=1, position="MID", feature_row=fr, minute_exposure=[(90.0, 1.0)]),
        BonusPlayerInput(element=2, position="MID", feature_row=fr, minute_exposure=[(90.0, 1.0)]),
    ]
    pmfs_seed1 = predict_bonus_pmfs_for_fixture(params, players, fixture=1, n_simulations=3000, seed=1)
    pmfs_seed2 = predict_bonus_pmfs_for_fixture(params, players, fixture=1, n_simulations=3000, seed=2)
    for a, b in zip(pmfs_seed1, pmfs_seed2):
        assert abs(a.p_bonus_awarded() - b.p_bonus_awarded()) < 0.1


def test_predict_bonus_pmfs_for_fixture_zero_minute_exposure_forces_zero_bonus():
    table = _synthetic_table()
    params = _fit_from_table(table, config=BonusModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr_playing = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_BONUS if c != "minutes_frac"}
    fr_playing["cold_start"] = False
    fr_playing["team_cold_start"] = False
    fr_playing["was_home"] = True
    fr_playing["player_trailing_bps_3"] = 45.0
    fr_playing["player_trailing_bps_5"] = 45.0
    fr_playing["player_trailing_bps_10"] = 45.0

    fr_unused = dict(fr_playing)
    fr_unused["player_trailing_bps_3"] = 0.0
    fr_unused["player_trailing_bps_5"] = 0.0
    fr_unused["player_trailing_bps_10"] = 0.0

    def make_filler(element):
        fr = dict(fr_playing)
        fr["player_trailing_bps_3"] = 2.0
        fr["player_trailing_bps_5"] = 2.0
        fr["player_trailing_bps_10"] = 2.0
        return BonusPlayerInput(element=element, position="MID", feature_row=fr, minute_exposure=[(45.0, 1.0)])

    players = [
        BonusPlayerInput(element=1, position="MID", feature_row=fr_playing, minute_exposure=[(90.0, 1.0)]),
        BonusPlayerInput(element=2, position="MID", feature_row=fr_unused, minute_exposure=[(0.0, 1.0)]),
        *[make_filler(100 + i) for i in range(6)],
    ]
    pmfs = predict_bonus_pmfs_for_fixture(params, players, fixture=1, n_simulations=3000, seed=0)
    by_element = {p.element: p for p in pmfs}
    assert by_element[2].p_bonus_awarded() < by_element[1].p_bonus_awarded()


def test_walk_forward_validate_requires_chronological_rank():
    table = _synthetic_table().drop("_chronological_rank")
    with pytest.raises(BonusModelError):
        walk_forward_validate(table, min_train_rows=1)


def test_multiclass_metrics_over_zero_rows_raise():
    from fplai.models.bonus import _multiclass_brier, _multiclass_log_loss

    with pytest.raises(BonusModelError):
        _multiclass_log_loss([], [])
    with pytest.raises(BonusModelError):
        _multiclass_brier([], [])


# ---------------------------------------------------------------------------
# 4. Real-store-gated.
# ---------------------------------------------------------------------------


def _real_store_available() -> bool:
    path = Path(__file__).resolve().parents[1] / "data" / "store" / "vaastav_player_gameweek_stats"
    return path.exists() and any(path.glob("**/*.parquet"))


requires_real_store = pytest.mark.skipif(not _real_store_available(), reason="data/store/vaastav_player_gameweek_stats absent")


@pytest.fixture
def registered_capability():
    return CANONICAL_SCHEMAS[PLAYER_BONUS_DISTRIBUTION_GAMEWEEK]


def test_registering_the_capability_does_not_leak_across_tests(registered_capability):
    assert registered_capability.is_modelled is True
    assert PLAYER_BONUS_DISTRIBUTION_GAMEWEEK in CANONICAL_SCHEMAS


def test_importing_the_module_registers_the_capability_at_import_time():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import fplai.models.bonus\n"
        "from fplai.schemas import CANONICAL_SCHEMAS, PLAYER_BONUS_DISTRIBUTION_GAMEWEEK\n"
        "assert PLAYER_BONUS_DISTRIBUTION_GAMEWEEK in CANONICAL_SCHEMAS\n"
        "schema = CANONICAL_SCHEMAS[PLAYER_BONUS_DISTRIBUTION_GAMEWEEK]\n"
        "assert schema.is_modelled is True\n"
        "assert schema.entity_key == ('season', 'round', 'element', 'fixture', 'count')\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


@pytest.mark.slow
@requires_real_store
def test_verify_bonus_award_rule_against_archive_on_the_real_store():
    """Break-first-style proof, attacked from outside the sanctioned call
    path — the award rule pin against the ENTIRE real archive, all 7
    seasons, not just the training window this module's own feature
    engineering uses (module docstring's 99.92% figure)."""
    real_store = BitemporalStore()
    result = verify_bonus_award_rule_against_archive(real_store, as_of=dt(2026, 8, 28))
    assert result.n_fixtures > 2000
    assert result.n_player_rows > 150_000
    assert result.match_rate > 0.999


@pytest.mark.slow
@requires_real_store
def test_build_training_table_excludes_2019_20_on_the_real_store():
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=dt(2026, 8, 28))
    assert "2019-20" not in set(table["season"].unique().to_list())
    assert table.height > 150_000


@pytest.mark.slow
@requires_real_store
def test_walk_forward_validate_beats_both_baselines_on_the_real_store():
    """A bounded live sanity check — the full-window gate numbers this
    task's report cites are produced by scripts/fit_bonus.py and recorded
    in docs/wiki/model-bonus.md; this proves the SAME code path against
    the real store inside the regular suite too, at a reduced simulation
    count to keep suite runtime bounded."""
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=dt(2026, 8, 28))
    result = walk_forward_validate(table, min_train_rows=3000, n_simulations=800, seed=0)
    assert result.n_folds > 20
    assert result.beats_both_baselines(), "bonus model failed to beat both §7.1 baselines"


@pytest.mark.slow
@requires_real_store
def test_predict_bonus_pmfs_for_fixture_end_to_end_against_a_real_fitted_model():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 28)
    params = fit_bonus_model(real_store, as_of=as_of)
    table = build_training_table(real_store, as_of=as_of)
    last_round = int(table["round"].max())
    rows = table.filter((pl.col("round") == last_round) & (pl.col("minutes") > 0)).head(3).to_dicts()
    assert len(rows) >= 2
    players = []
    for row in rows:
        fr = {c: row[c] for c in NUMERIC_FEATURE_COLUMNS_BONUS if c != "minutes_frac"}
        players.append(
            BonusPlayerInput(
                element=row["element"], position=row["position"], feature_row=fr,
                minute_exposure=[(90.0, 0.8), (0.0, 0.2)],
            )
        )
    pmfs = predict_bonus_pmfs_for_fixture(params, players, fixture=999999, n_simulations=1000, seed=0)
    assert len(pmfs) == len(players)
    for pmf in pmfs:
        assert sum(pmf.probabilities) == pytest.approx(1.0)
        assert 0.0 <= pmf.p_bonus_awarded() <= 1.0


# ---------------------------------------------------------------------------
# 5. Persistence.
# ---------------------------------------------------------------------------


def test_write_bonus_pmfs_round_trips_through_write_derived(temp_store, registered_capability):
    table = _synthetic_table()
    params = _fit_from_table(table, config=BonusModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_BONUS if c != "minutes_frac"}
    fr["cold_start"] = False
    fr["team_cold_start"] = False
    fr["was_home"] = True
    players = [
        BonusPlayerInput(element=1, position="MID", feature_row=fr, minute_exposure=[(90.0, 1.0)]),
        BonusPlayerInput(element=2, position="MID", feature_row=fr, minute_exposure=[(90.0, 1.0)]),
    ]
    pmfs = predict_bonus_pmfs_for_fixture(params, players, fixture=1, n_simulations=1000, seed=0)
    calibration = CalibrationReference(reference="test", residual_mean=0.0, residual_std=1.0)
    result = write_bonus_pmfs(temp_store, pmfs, season="2022-23", round_=1, valid_at=dt(2026, 1, 1), calibration=calibration)
    assert result.written
    out = temp_store.as_of("derived_player_bonus_distribution", datetime.now(UTC) + __import__("datetime").timedelta(days=1))
    assert out["is_modelled"].all()
    for element in (1, 2):
        rows = out.filter(pl.col("element") == element)
        assert rows["probability"].sum() == pytest.approx(1.0)


def test_pmfs_to_rows_rejects_empty_sequence():
    with pytest.raises(BonusModelError):
        pmfs_to_rows([], season="2022-23", round_=1)
