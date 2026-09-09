"""Tests for fplai.models.saves — Phase 3 prerequisite, session s005
(blueprint §4, §7.1, §12.2), the seventh and last outcome model.

Five groups, mirroring `tests/test_cards.py`/`tests/test_defensive_
contribution.py`'s own structure (the closest sibling modules):

  1. Pure functions — NB2/Poisson PMF invariants, `SavesPMF` validation,
     the analytic NB2 gradient cross-checked against finite differences
     (including the `l2/n` scaling fix, module docstring's own
     `_nb_neg_log_lik_and_grad` finding).
  2. Feature engineering — a temp store, fabricated multi-round rows,
     the 2019-20 exclusion, the non-GK/minutes>0 filters, the double-
     gameweek trailing-feature leakage attack, `opponent_goals_this_
     fixture`'s was_home-gated construction.
  3. Fit + predict + walk-forward mechanics — small synthetic data with a
     KNOWN trailing/opponent-goals signal the model must recover, the
     nested double mixture (minutes x opponent goals), the "cannot smuggle
     opponent_goals_this_fixture into feature_row" break-first proof, and
     the nested-calibration leak-vs-honest attack.
  4. Real-store-gated: import-time registration, the data-shape
     verification, the §7.1 walk-forward gate on the SHIPPED (raw) series,
     and the measured "calibration does not help here" finding.
  5. Persistence — `write_saves_pmfs` round-tripping through `write_derived`.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from fplai.models.saves import (
    NUMERIC_FEATURE_COLUMNS_SAVES,
    PREDICT_FEATURE_ROW_COLUMNS,
    SAVES_POINTS_DIVISOR,
    IsotonicCalibrator,
    SavesFeatureSpec,
    SavesModelConfig,
    SavesModelError,
    SavesPMF,
    _apply_ge3_calibration,
    _build_feature_spec,
    _fit_from_table,
    _fit_isotonic_calibrator,
    _inner_calibration_split,
    _nb_neg_log_lik_and_grad,
    _nb_pmf,
    _p_ge,
    _poisson_pmf,
    build_training_table,
    fit_saves_model,
    pmfs_to_rows,
    predict_saves_pmf,
    verify_saves_data_shape_against_archive,
    walk_forward_validate,
    write_saves_pmfs,
)
from fplai.derived import CalibrationReference
from fplai.schemas import CANONICAL_SCHEMAS, PLAYER_SAVES_DISTRIBUTION_GAMEWEEK
from fplai.store import BitemporalStore

UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Pure functions.
# ---------------------------------------------------------------------------


def test_nb_pmf_sums_to_one_and_degenerates_at_zero_mu():
    pmf, mass = _nb_pmf(3.0, 5.0, 20)
    assert pmf.sum() == pytest.approx(1.0)
    assert 0.0 < mass <= 1.0
    pmf0, mass0 = _nb_pmf(0.0, 5.0, 20)
    assert pmf0[0] == 1.0
    assert pmf0.sum() == pytest.approx(1.0)
    assert mass0 == 1.0


def test_poisson_pmf_sums_to_one_and_degenerates_at_zero_lambda():
    pmf = _poisson_pmf(2.5, 20)
    assert pmf.sum() == pytest.approx(1.0)
    pmf0 = _poisson_pmf(0.0, 20)
    assert pmf0[0] == 1.0


def test_p_ge_sums_the_tail_correctly():
    row = (0.1, 0.2, 0.3, 0.4)
    assert _p_ge(row, 2) == pytest.approx(0.7)
    assert _p_ge(row, 0) == pytest.approx(1.0)
    assert _p_ge(row, 10) == pytest.approx(0.0)


def test_apply_ge3_calibration_preserves_shape_and_hits_target():
    raw = np.array([0.1, 0.2, 0.3, 0.25, 0.1, 0.05])  # threshold=3: head={0,1,2}=0.6, tail={3,4,5}=0.4
    out = _apply_ge3_calibration(raw, calibrated_p_ge3=0.7, threshold=3)
    assert out.sum() == pytest.approx(1.0)
    assert out[3:].sum() == pytest.approx(0.7)
    assert out[:3].sum() == pytest.approx(0.3)
    # shape preserved WITHIN each block: ratios among {0,1,2} and among {3,4,5} unchanged.
    assert out[0] / out[1] == pytest.approx(raw[0] / raw[1])
    assert out[3] / out[4] == pytest.approx(raw[3] / raw[4])


def test_saves_pmf_rejects_mismatched_lengths():
    with pytest.raises(SavesModelError):
        SavesPMF(element=1, fixture=1, counts=(0, 1), probabilities=(1.0,), mass_before_truncation=1.0)


def test_saves_pmf_rejects_probabilities_not_summing_to_one():
    with pytest.raises(SavesModelError):
        SavesPMF(element=1, fixture=1, counts=(0, 1), probabilities=(0.5, 0.4), mass_before_truncation=1.0)


def test_saves_pmf_expected_count_p_at_least_and_expected_points():
    pmf = SavesPMF(element=1, fixture=1, counts=(0, 1, 2, 3), probabilities=(0.4, 0.3, 0.2, 0.1), mass_before_truncation=1.0)
    assert pmf.expected_count() == pytest.approx(0 * 0.4 + 1 * 0.3 + 2 * 0.2 + 3 * 0.1)
    assert pmf.p_at_least(3) == pytest.approx(0.1)
    # floor(0/3)=0, floor(1/3)=0, floor(2/3)=0, floor(3/3)=1 -> only the k=3 cell earns a point.
    assert pmf.expected_save_points(points_per_unit=1, divisor=SAVES_POINTS_DIVISOR) == pytest.approx(0.1)


def test_feature_spec_rejects_mismatched_means_stds_length():
    with pytest.raises(SavesModelError):
        SavesFeatureSpec(numeric_columns=("a", "b"), numeric_means=(0.0,), numeric_stds=(1.0,))


def test_feature_spec_opponent_goals_index_matches_column_position():
    spec = SavesFeatureSpec(
        numeric_columns=NUMERIC_FEATURE_COLUMNS_SAVES,
        numeric_means=tuple(0.0 for _ in NUMERIC_FEATURE_COLUMNS_SAVES),
        numeric_stds=tuple(1.0 for _ in NUMERIC_FEATURE_COLUMNS_SAVES),
    )
    assert spec.numeric_columns[spec.opponent_goals_index] == "opponent_goals_this_fixture"


def test_nb_neg_log_lik_gradient_matches_finite_differences():
    """Re-checked after this session's l2/n scaling fix (module docstring,
    `_nb_neg_log_lik_and_grad`) — the analytic gradient must still agree
    with finite differences on the CORRECTED objective, not just the
    original DC formula it was duplicated from."""
    rng = np.random.default_rng(0)
    n, p = 60, 5
    X = rng.normal(size=(n, p))
    offset = rng.normal(scale=0.1, size=n)
    y = rng.poisson(3.0, size=n).astype(np.float64)
    params = np.concatenate([rng.normal(scale=0.2, size=p), [1.0]])
    _, grad_analytic = _nb_neg_log_lik_and_grad(params, X, y, offset, p, l2=0.5)

    eps = 1e-6
    grad_numeric = np.zeros_like(params)
    for i in range(len(params)):
        p_plus, p_minus = params.copy(), params.copy()
        p_plus[i] += eps
        p_minus[i] -= eps
        loss_plus, _ = _nb_neg_log_lik_and_grad(p_plus, X, y, offset, p, l2=0.5)
        loss_minus, _ = _nb_neg_log_lik_and_grad(p_minus, X, y, offset, p, l2=0.5)
        grad_numeric[i] = (loss_plus - loss_minus) / (2 * eps)

    assert np.allclose(grad_analytic, grad_numeric, atol=1e-4, rtol=1e-3)


def test_l2_penalty_is_scaled_by_n_not_left_raw():
    """Break-first proof of this session's own fix: fitting the SAME data
    with the SAME nominal l2 but very different `n` (by duplicating rows)
    must produce a near-identical intercept once the penalty is correctly
    `l2/n` — the bug this replaces made the effective penalty scale with
    `n`, which this test would catch if the fix were ever reverted."""
    rng = np.random.default_rng(1)
    n0, p = 200, 3
    X0 = rng.normal(size=(n0, p))
    y0 = rng.poisson(3.0, size=n0).astype(np.float64)
    offset0 = np.zeros(n0)
    X1 = np.concatenate([X0] * 5)  # n = 1000, same underlying distribution
    y1 = np.concatenate([y0] * 5)
    offset1 = np.zeros(1000)

    from fplai.models.saves import _fit_negative_binomial

    cfg = SavesModelConfig(l2_penalty=1.0)
    beta0, _ = _fit_negative_binomial(X0, y0, offset0, cfg)
    beta1, _ = _fit_negative_binomial(X1, y1, offset1, cfg)
    assert beta0[0] == pytest.approx(beta1[0], abs=0.05)


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
    team_h_score: int = 1,
    team_a_score: int = 1,
    minutes: int = 90,
    position: str = "GK",
    saves: int = 2,
    goals_conceded: int = 1,
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
        "team_h_score": team_h_score,
        "team_a_score": team_a_score,
        "saves": saves,
        "goals_conceded": goals_conceded,
    }


@pytest.fixture
def temp_store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def _write_fixture(store, rows, *, observed_at, valid_at=None):
    df = pl.DataFrame(rows)
    store.write("vaastav_player_gameweek_stats", df, valid_at=valid_at or observed_at, observed_at=observed_at, source="test")


def test_build_training_table_requires_timezone_aware_as_of(temp_store):
    _write_fixture(temp_store, [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z")], observed_at=dt(2026, 1, 1))
    with pytest.raises(SavesModelError):
        build_training_table(temp_store, as_of=datetime(2026, 1, 1))


def test_build_training_table_excludes_2019_20_null_position(temp_store):
    rows = [_row("2019-20", 1, 10, 1, "2019-08-09T14:00:00Z")]
    df = pl.DataFrame(rows).with_columns(
        pl.lit(None, dtype=pl.Utf8).alias("position"), pl.lit(None, dtype=pl.Utf8).alias("team")
    )
    temp_store.write("vaastav_player_gameweek_stats", df, valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    with pytest.raises(SavesModelError):
        build_training_table(temp_store, as_of=dt(2026, 1, 2))


def test_build_training_table_excludes_non_gk_and_zero_minute_rows(temp_store):
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", position="GK", minutes=90),
        _row("2022-23", 1, 11, 1, "2022-08-06T14:00:00Z", position="DEF", minutes=90),
        _row("2022-23", 1, 12, 1, "2022-08-06T14:00:00Z", position="GK", minutes=0, saves=0),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    assert set(table["element"].to_list()) == {10}


def test_opponent_goals_this_fixture_gated_on_was_home(temp_store):
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", was_home=True, team_h_score=1, team_a_score=3),
        _row("2022-23", 1, 11, 2, "2022-08-06T14:00:00Z", was_home=False, team_h_score=1, team_a_score=3, team="TeamB"),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2)).sort("element")
    # was_home=True keeper faces the AWAY score (3); was_home=False keeper faces the HOME score (1).
    assert table["opponent_goals_this_fixture"].to_list() == pytest.approx([3.0, 1.0])


def test_player_trailing_saves_mean_never_peeks_at_the_same_round_second_fixture(temp_store):
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", saves=4),
        # Round 2: a double gameweek. Second fixture has a save count that
        # would corrupt the trailing feature if it leaked into the first.
        _row("2022-23", 2, 10, 2, "2022-08-13T14:00:00Z", saves=0),
        _row("2022-23", 2, 10, 3, "2022-08-16T14:00:00Z", saves=8),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    round2 = table.filter(pl.col("round") == 2).sort("fixture")
    assert round2["player_trailing_saves_mean_3"].to_list() == pytest.approx([4.0, 4.0])


def test_cold_start_flag_true_for_a_players_first_labelled_round(temp_store):
    rows = [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z")]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    assert table["cold_start"][0] is True
    assert table["games_played_this_season"][0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. Fit + predict + walk-forward mechanics.
# ---------------------------------------------------------------------------


def _synthetic_table(n_rounds: int = 40, seed: int = 0) -> pl.DataFrame:
    """A small synthetic training table with a KNOWN opponent-goals signal
    (higher opponent_goals_this_fixture -> higher saves rate) the model
    must recover — same "small synthetic data with a KNOWN signal" pattern
    every sibling module's own fit+predict test group uses."""
    rng = np.random.default_rng(seed)
    rows = []
    for r in range(1, n_rounds + 1):
        opp_goals = rng.integers(0, 4)
        true_mu = math.exp(0.8 + 0.3 * (opp_goals - 1.5))
        saves = rng.poisson(true_mu)
        rows.append(
            {
                "season": "2022-23",
                "round": r,
                "element": 10,
                "fixture": r,
                "kickoff_time": f"2022-08-{(r % 28) + 1:02d}T14:00:00Z",
                "minutes": 90,
                "position": "GK",
                "team": "TeamA",
                "was_home": True,
                "team_h_score": 1,
                "team_a_score": int(opp_goals),
                "saves": int(saves),
                "goals_conceded": int(opp_goals),
            }
        )
    df = pl.DataFrame(rows)
    return df


def _build_table_via_store(tmp_path, rows: pl.DataFrame) -> pl.DataFrame:
    store = BitemporalStore(base_path=tmp_path / "store2")
    store.write("vaastav_player_gameweek_stats", rows, valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    return build_training_table(store, as_of=dt(2026, 1, 2))


def test_fit_saves_model_recovers_a_known_opponent_goals_signal(tmp_path):
    table = _build_table_via_store(tmp_path, _synthetic_table(n_rounds=60, seed=1))
    beta, log_r, spec = _fit_from_table(table, config=SavesModelConfig())
    opp_beta = beta[1 + spec.opponent_goals_index]
    assert opp_beta > 0, "the model must recover a POSITIVE opponent-goals coefficient from a known-positive DGP"


def test_predict_saves_pmf_rejects_non_normalised_mixtures(tmp_path):
    table = _build_table_via_store(tmp_path, _synthetic_table(n_rounds=60, seed=2))
    beta, log_r, spec = _fit_from_table(table, config=SavesModelConfig())
    from fplai.models.saves import SavesModelParams

    params = SavesModelParams(
        feature_spec=spec, beta=beta, log_r=np.array(log_r), config=SavesModelConfig(),
        as_of=dt(2026, 1, 2), seasons_used=("2022-23",), n_rows_used=table.height,
    )
    fr = {c: 0.0 for c in PREDICT_FEATURE_ROW_COLUMNS}
    with pytest.raises(SavesModelError):
        predict_saves_pmf(params, fr, element=10, fixture=1, minute_exposure=[(90.0, 0.5)], opponent_goals_marginal=[(1, 1.0)])
    with pytest.raises(SavesModelError):
        predict_saves_pmf(params, fr, element=10, fixture=1, minute_exposure=[(90.0, 1.0)], opponent_goals_marginal=[(1, 0.5)])


def test_predict_saves_pmf_rejects_smuggled_opponent_goals_in_feature_row(tmp_path):
    """Break-first proof, module docstring 'Composition, not import' — the
    same 'never smuggle the externally-supplied exposure dimension into
    feature_row' discipline every sibling module enforces."""
    table = _build_table_via_store(tmp_path, _synthetic_table(n_rounds=60, seed=3))
    beta, log_r, spec = _fit_from_table(table, config=SavesModelConfig())
    from fplai.models.saves import SavesModelParams

    params = SavesModelParams(
        feature_spec=spec, beta=beta, log_r=np.array(log_r), config=SavesModelConfig(),
        as_of=dt(2026, 1, 2), seasons_used=("2022-23",), n_rows_used=table.height,
    )
    fr = {c: 0.0 for c in PREDICT_FEATURE_ROW_COLUMNS}
    fr["opponent_goals_this_fixture"] = 2.0
    with pytest.raises(SavesModelError):
        predict_saves_pmf(params, fr, element=10, fixture=1, minute_exposure=[(90.0, 1.0)], opponent_goals_marginal=[(1, 1.0)])


def test_predict_saves_pmf_zero_minutes_is_a_spike_at_zero(tmp_path):
    table = _build_table_via_store(tmp_path, _synthetic_table(n_rounds=60, seed=4))
    beta, log_r, spec = _fit_from_table(table, config=SavesModelConfig())
    from fplai.models.saves import SavesModelParams

    params = SavesModelParams(
        feature_spec=spec, beta=beta, log_r=np.array(log_r), config=SavesModelConfig(),
        as_of=dt(2026, 1, 2), seasons_used=("2022-23",), n_rows_used=table.height,
    )
    fr = {c: 0.0 for c in PREDICT_FEATURE_ROW_COLUMNS}
    pmf = predict_saves_pmf(
        params, fr, element=10, fixture=1, minute_exposure=[(0.0, 1.0)], opponent_goals_marginal=[(0, 0.5), (2, 0.5)]
    )
    assert pmf.probabilities[0] == pytest.approx(1.0)


def test_predict_saves_pmf_nested_mixture_matches_manual_computation(tmp_path):
    """Attacked directly: build the double mixture BY HAND (outside
    predict_saves_pmf) for a 2x2 grid of (minutes, opponent_goals) and
    confirm the module's own nested loop produces the identical PMF."""
    table = _build_table_via_store(tmp_path, _synthetic_table(n_rounds=60, seed=5))
    beta, log_r, spec = _fit_from_table(table, config=SavesModelConfig())
    from fplai.models.saves import SavesModelParams

    cfg = SavesModelConfig(max_count=15)
    params = SavesModelParams(
        feature_spec=spec, beta=beta, log_r=np.array(log_r), config=cfg,
        as_of=dt(2026, 1, 2), seasons_used=("2022-23",), n_rows_used=table.height,
    )
    fr = {c: 0.0 for c in PREDICT_FEATURE_ROW_COLUMNS}
    minute_exposure = [(90.0, 0.7), (45.0, 0.3)]
    opp_marginal = [(0, 0.4), (2, 0.6)]

    pmf = predict_saves_pmf(params, fr, element=10, fixture=1, minute_exposure=minute_exposure, opponent_goals_marginal=opp_marginal)

    r = math.exp(log_r)
    opp_idx = 1 + spec.opponent_goals_index
    opp_mean = spec.numeric_means[spec.opponent_goals_index]
    opp_std = spec.numeric_stds[spec.opponent_goals_index]
    # `base` must go through the SAME standardization predict_saves_pmf
    # itself applies (fr's raw 0.0 values are NOT standardized 0.0 for
    # every column) -- reusing _feature_row_to_vector here checks the
    # nested MIXTURE LOOP this test targets, not a hand-reimplementation
    # of standardization the module already owns.
    from fplai.models.saves import _feature_row_to_vector

    base = _feature_row_to_vector({**fr, "opponent_goals_this_fixture": 0.0}, spec)
    manual = np.zeros(cfg.max_count + 1)
    for m_val, w_m in minute_exposure:
        offset = math.log(m_val / 90.0)
        for g_val, w_g in opp_marginal:
            x = base.copy()
            x[opp_idx] = (g_val - opp_mean) / opp_std
            eta = float(x @ beta) + offset
            mu = math.exp(eta)
            pmf_k, _ = _nb_pmf(mu, r, cfg.max_count)
            manual += w_m * w_g * pmf_k
    manual = manual / manual.sum()
    assert np.allclose(np.array(pmf.probabilities), manual, atol=1e-9)


def test_walk_forward_validate_never_sees_a_folds_own_or_future_outcomes(tmp_path):
    """Fold-isolation attack — same class of proof `fplai.models.minutes`/
    `fplai.models.cards` each run for their own walk-forward. Perturbing a
    LATER round's outcomes must not change an EARLIER fold's p_model."""
    table = _build_table_via_store(tmp_path, _synthetic_table(n_rounds=60, seed=6))
    result_a = walk_forward_validate(table, min_train_rows=20, calibrate=False)

    perturbed = table.with_columns(
        pl.when(pl.col("_chronological_rank") == table["_chronological_rank"].max())
        .then(pl.lit(13))
        .otherwise(pl.col("saves"))
        .alias("saves")
    )
    result_b = walk_forward_validate(perturbed, min_train_rows=20, calibrate=False)

    # Every fold's prediction EXCEPT possibly the very last (perturbed) one
    # must be bit-identical between the two runs.
    n_check = len(result_a.y_true) - 1
    assert result_a.p_model_raw[:n_check] == result_b.p_model_raw[:n_check]


def test_saves_calibrator_fit_directly_on_eval_data_would_leak_and_look_suspiciously_good():
    """The FORBIDDEN path, constructed by hand: fit the P(>=3) isotonic
    calibrator directly on the eval fold's OWN predictions/outcomes
    (leakage) and show it produces a strictly better log-loss than the
    honest nested version — same attack `fplai.models.minutes`/`fplai.
    models.cards` each run for their own nested calibrators."""
    rng = np.random.default_rng(7)
    n = 300
    # A genuinely overconfident raw model: raw_p clusters away from 0.5,
    # true outcome rate is closer to 0.5 than raw_p implies.
    raw_p = np.clip(rng.beta(2, 2, size=n), 0.02, 0.98)
    true_rate = 0.3 + 0.4 * raw_p  # compressed toward the middle -> raw is overconfident
    y = (rng.uniform(size=n) < true_rate).astype(np.float64)

    from fplai.calibration import log_loss as binary_log_loss

    # LEAKY: calibrator fit on the SAME (raw_p, y) it is then scored against.
    leaky_calibrator = _fit_isotonic_calibrator(raw_p, y)
    leaky_calibrated = leaky_calibrator.apply(raw_p)
    leaky_ll = binary_log_loss(y.tolist(), leaky_calibrated.tolist())

    # HONEST: calibrator fit on an INDEPENDENT draw from the SAME process,
    # scored on the ORIGINAL (raw_p, y) — genuinely out-of-sample.
    raw_p_indep = np.clip(rng.beta(2, 2, size=n), 0.02, 0.98)
    true_rate_indep = 0.3 + 0.4 * raw_p_indep
    y_indep = (rng.uniform(size=n) < true_rate_indep).astype(np.float64)
    honest_calibrator = _fit_isotonic_calibrator(raw_p_indep, y_indep)
    honest_calibrated = honest_calibrator.apply(raw_p)
    honest_ll = binary_log_loss(y.tolist(), honest_calibrated.tolist())

    raw_ll = binary_log_loss(y.tolist(), raw_p.tolist())
    assert leaky_ll < honest_ll, "the leaky (same-data) calibrator should look suspiciously better than the honest one"
    assert honest_ll < raw_ll, "the honest, genuinely-out-of-sample calibrator should still improve on the raw, overconfident predictions"


# `test_isotonic_calibrator_never_saturates_to_exact_zero_or_one`/
# `test_isotonic_calibrator_smoothing_converges_to_raw_rate_at_scale` moved
# to `tests/test_calibration.py` session s005 (consolidation) — they
# attacked the shared `IsotonicCalibrator` mechanism directly (identical
# fixtures to `fplai.models.minutes`'s own precedent versions), not
# anything specific to this module. This module's own measured NEGATIVE --
# zero of 3,104 calibrated ge3 predictions landed at exactly 0.0/1.0, at the
# eval level or the per-bin level, before this fix -- is preserved in
# `docs/wiki/model-saves.md`, not lost; it is why `calibrate=False` ships
# regardless (measured harm unrelated to saturation, module docstring). The
# leakage proof above and `_inner_calibration_split` test below are
# UNCHANGED and stay here.


def test_inner_calibration_split_returns_none_when_not_enough_data():
    small = pl.DataFrame({"_chronological_rank": [1, 2, 3]})
    assert _inner_calibration_split(small, holdout_frac=0.2, min_holdout_rows=100, min_inner_train_rows=500) is None


# ---------------------------------------------------------------------------
# 4. Real-store-gated.
# ---------------------------------------------------------------------------


def _real_store_available() -> bool:
    path = Path(__file__).resolve().parents[1] / "data" / "store" / "vaastav_player_gameweek_stats"
    return path.exists() and any(path.glob("**/*.parquet"))


requires_real_store = pytest.mark.skipif(not _real_store_available(), reason="data/store/vaastav_player_gameweek_stats absent")


def test_saves_capability_registered_at_import_time():
    assert PLAYER_SAVES_DISTRIBUTION_GAMEWEEK in CANONICAL_SCHEMAS
    assert CANONICAL_SCHEMAS[PLAYER_SAVES_DISTRIBUTION_GAMEWEEK].is_modelled


@pytest.mark.slow
@requires_real_store
def test_verify_saves_data_shape_against_archive_on_the_real_store():
    real_store = BitemporalStore()
    v = verify_saves_data_shape_against_archive(real_store, as_of=datetime.now(UTC))
    assert v.n_gk_appearances > 4000
    assert v.n_full90_gk_rows_checked > 4000
    # Near-exact agreement (module docstring): at most a small, named handful
    # of mismatches, never a large fraction of the population.
    assert v.n_opponent_goals_vs_goals_conceded_mismatches < 10
    assert v.corr_opponent_goals_saves > 0.0


@pytest.mark.slow
@requires_real_store
def test_build_training_table_excludes_2019_20_on_the_real_store():
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=datetime.now(UTC))
    assert "2019-20" not in set(table["season"].unique().to_list())
    assert table.height > 4000
    assert set(table["position"].unique().to_list()) == {"GK"}


@pytest.mark.slow
@requires_real_store
def test_walk_forward_validate_beats_both_baselines_on_the_shipped_raw_series_on_the_real_store():
    """A bounded live check of the SAME code path `scripts/fit_saves.py`/
    `docs/wiki/model-saves.md` report the full numbers from — the RAW
    series, since `fit_saves_model`'s own default is `calibrate=False`
    (module docstring, measured this session: calibration does not help
    on this dataset's size)."""
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=datetime.now(UTC))
    result = walk_forward_validate(table, min_train_rows=1500, calibrate=False)
    assert result.n_folds > 50
    assert result.model_log_loss_raw() < result.baseline_group_rate_log_loss()
    assert result.model_log_loss_raw() < result.baseline_player_trailing_log_loss()
    assert result.model_brier_raw() < result.baseline_group_rate_brier()
    assert result.model_brier_raw() < result.baseline_player_trailing_brier()


@pytest.mark.slow
@requires_real_store
def test_calibration_measurably_does_not_help_on_the_real_store():
    """The measured negative result this session's `fit_saves_model`
    docstring reports, re-verified live rather than merely asserted in
    prose (CLAUDE.md: 'a test that has never failed is not evidence') —
    this IS the evidence for `calibrate=False` shipping as the default."""
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=datetime.now(UTC))
    result = walk_forward_validate(table, min_train_rows=1500, calibrate=True)
    assert result.n_folds_calibrated > 0
    assert not result.calibrated_not_worse_than_raw()


@pytest.mark.slow
@requires_real_store
def test_predict_saves_pmf_end_to_end_against_a_real_fitted_model():
    real_store = BitemporalStore()
    as_of = datetime.now(UTC)
    params = fit_saves_model(real_store, as_of=as_of)
    table = build_training_table(real_store, as_of=as_of)
    row = table.filter(pl.col("minutes") > 0).head(1).to_dicts()[0]
    fr = {c: row[c] for c in PREDICT_FEATURE_ROW_COLUMNS}
    pmf = predict_saves_pmf(
        params, fr, element=row["element"], fixture=row["fixture"],
        minute_exposure=[(90.0, 1.0)], opponent_goals_marginal=[(1, 1.0)],
    )
    assert sum(pmf.probabilities) == pytest.approx(1.0)
    assert pmf.calibration_method == "raw_uncalibrated"


# ---------------------------------------------------------------------------
# 5. Persistence.
# ---------------------------------------------------------------------------


def test_pmfs_to_rows_shape():
    pmf = SavesPMF(element=1, fixture=1, counts=(0, 1, 2), probabilities=(0.5, 0.3, 0.2), mass_before_truncation=1.0)
    rows = pmfs_to_rows([pmf], season="2025-26", round_=1)
    assert rows.height == 3
    assert rows["probability"].sum() == pytest.approx(1.0)
    assert set(rows["season"].unique().to_list()) == {"2025-26"}


def test_write_saves_pmfs_round_trips_through_write_derived(tmp_path):
    store = BitemporalStore(base_path=tmp_path / "store3")
    pmf = SavesPMF(element=1, fixture=1, counts=(0, 1, 2), probabilities=(0.5, 0.3, 0.2), mass_before_truncation=1.0)
    calibration = CalibrationReference(reference="test", residual_mean=0.0, residual_std=1.0)
    result = write_saves_pmfs(store, [pmf], season="2025-26", round_=1, valid_at=dt(2026, 1, 2), calibration=calibration)
    assert result.written
    # observed_at is stamped by write_derived as real "now" (never valid_at)
    # -- read back as_of the real clock, not a fictional past literal
    # (CLAUDE.md lesson 8: derive bounds from the clock, never a literal).
    read_back = store.as_of("derived_player_saves_distribution", datetime.now(UTC))
    assert read_back.height == 3
    assert read_back["is_modelled"].all()
    assert read_back["probability"].sum() == pytest.approx(1.0)


def test_write_saves_pmfs_rejects_empty_list(tmp_path):
    store = BitemporalStore(base_path=tmp_path / "store4")
    calibration = CalibrationReference(reference="test", residual_mean=0.0, residual_std=1.0)
    with pytest.raises(SavesModelError):
        write_saves_pmfs(store, [], season="2025-26", round_=1, valid_at=dt(2026, 1, 2), calibration=calibration)
