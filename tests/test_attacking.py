"""Tests for fplai.models.attacking — Phase 2, E5, session s004 (blueprint
§4, §7.1, §12.2).

Five groups, mirroring `tests/test_defensive_contribution.py`'s own
structure (the closest sibling module):

  1. Pure functions — PMF sum-to-1 discipline, `p_involved`/`expected_
     count`, the Binomial-share negative-log-likelihood's analytic
     gradient checked against finite differences, and the "no known-absent
     feature masquerading as a real one" guarantee (`KNOWN_ABSENT_
     FEATURES`).
  2. Feature engineering — a temp store, fabricated multi-round rows,
     including the double-gameweek leakage attack, the y>n_trials guard,
     and the xG/xA-era exclusion.
  3. Fit + predict + walk-forward mechanics — small synthetic data with a
     KNOWN team-goal-share signal the model must recover.
  4. Real-store-gated: import-time registration, the §7.1 walk-forward
     gate against the real store, and a composition sanity check
     (`team_goals_marginal=[(0,1.0)]` must force `p_involved()==0`).
  5. Persistence — `write_attacking_pmfs` round-tripping through
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

from fplai.models.attacking import (
    KNOWN_ABSENT_FEATURES,
    NUMERIC_FEATURE_COLUMNS_ATTACKING,
    STATS,
    AttackingFeatureSpec,
    AttackingInvolvementPMF,
    AttackingModelConfig,
    AttackingModelError,
    _binomial_share_neg_log_lik_and_grad,
    _fit_from_table,
    build_training_table,
    fit_attacking_model,
    pmfs_to_rows,
    predict_attacking_pmf,
    walk_forward_validate,
    write_attacking_pmfs,
)
from fplai.derived import CalibrationReference
from fplai.schemas import CANONICAL_SCHEMAS, PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK
from fplai.store import BitemporalStore

UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Pure functions.
# ---------------------------------------------------------------------------


def test_known_absent_features_are_declared_and_never_leak_into_the_feature_set():
    """The core structural requirement of this task's brief: set-piece and
    penalty responsibility are declared known-absent, not silently proxied
    by a biased FPL field."""
    assert "primary_set_piece_taker" in KNOWN_ABSENT_FEATURES
    assert "penalty_taker_duty" in KNOWN_ABSENT_FEATURES
    assert not set(KNOWN_ABSENT_FEATURES) & set(NUMERIC_FEATURE_COLUMNS_ATTACKING)
    assert "penalties_missed" not in NUMERIC_FEATURE_COLUMNS_ATTACKING
    assert "penalties_saved" not in NUMERIC_FEATURE_COLUMNS_ATTACKING


def test_attacking_involvement_pmf_rejects_probabilities_not_summing_to_one():
    with pytest.raises(AttackingModelError):
        AttackingInvolvementPMF(
            element=1, fixture=1, stat="goals", counts=(0, 1), probabilities=(0.3, 0.3),
            mass_before_truncation=1.0,
        )


def test_attacking_involvement_pmf_rejects_unknown_stat():
    with pytest.raises(AttackingModelError):
        AttackingInvolvementPMF(
            element=1, fixture=1, stat="clean_sheets", counts=(0,), probabilities=(1.0,),
            mass_before_truncation=1.0,
        )


def test_attacking_involvement_pmf_p_involved_sums_only_tail_at_or_above_one():
    counts = tuple(range(0, 6))
    probs = tuple(1.0 / len(counts) for _ in counts)
    pmf = AttackingInvolvementPMF(
        element=1, fixture=1, stat="goals", counts=counts, probabilities=probs, mass_before_truncation=1.0,
    )
    expected = sum(1.0 / len(counts) for c in counts if c >= 1)
    assert pmf.p_involved() == pytest.approx(expected)


def test_attacking_involvement_pmf_expected_count_matches_hand_computation():
    pmf = AttackingInvolvementPMF(
        element=1, fixture=1, stat="assists", counts=(0, 1, 2), probabilities=(0.5, 0.3, 0.2),
        mass_before_truncation=1.0,
    )
    assert pmf.expected_count() == pytest.approx(0 * 0.5 + 1 * 0.3 + 2 * 0.2)


def test_binomial_share_neg_log_lik_gradient_matches_finite_differences():
    rng = np.random.default_rng(0)
    n, p = 40, 6
    X = rng.normal(size=(n, p))
    n_trials = rng.integers(0, 4, size=n).astype(np.float64)
    y = np.array([rng.integers(0, int(nt) + 1) for nt in n_trials], dtype=np.float64)
    minutes_frac = rng.uniform(0.1, 1.0, size=n)
    beta = rng.normal(scale=0.3, size=p)

    _, grad_analytic = _binomial_share_neg_log_lik_and_grad(beta, X, y, n_trials, minutes_frac, l2=0.5)

    eps = 1e-6
    grad_numeric = np.zeros_like(beta)
    for i in range(p):
        b_plus = beta.copy()
        b_plus[i] += eps
        b_minus = beta.copy()
        b_minus[i] -= eps
        nll_plus, _ = _binomial_share_neg_log_lik_and_grad(b_plus, X, y, n_trials, minutes_frac, l2=0.5)
        nll_minus, _ = _binomial_share_neg_log_lik_and_grad(b_minus, X, y, n_trials, minutes_frac, l2=0.5)
        grad_numeric[i] = (nll_plus - nll_minus) / (2 * eps)

    assert np.allclose(grad_analytic, grad_numeric, atol=1e-4, rtol=1e-3)


def test_binomial_share_neg_log_lik_zero_trials_row_contributes_nothing():
    """A row where the team scored 0 goals that fixture must contribute
    exactly zero to both loss and gradient — module docstring."""
    X = np.array([[1.0, 0.5]])
    y = np.array([0.0])
    n_trials = np.array([0.0])
    minutes_frac = np.array([1.0])
    nll, grad = _binomial_share_neg_log_lik_and_grad(np.zeros(2), X, y, n_trials, minutes_frac, l2=0.0)
    assert nll == pytest.approx(0.0)
    assert np.allclose(grad, 0.0)


def test_binomial_share_l2_penalty_is_scaled_by_inverse_n():
    """Pins session s005's fix directly: the ridge term must be
    `(l2/n_rows) * sum(beta**2)`, not the unscaled `l2 * sum(beta**2)` this
    module carried before this session (this task's brief; CLAUDE.md rule
    5 — an unscaled penalty against a per-row-AVERAGED likelihood is
    effectively `l2*n_rows`, silently crushing every coefficient toward 0
    for `n_rows` in the thousands — the exact defect
    `AttackingModelConfig.l2_penalty`'s own docstring records having to
    shrink `l2` from `1.0` to `0.01` to work around).

    Same row-doubling construction `tests/test_minutes.py::
    test_softmax_l2_penalty_is_scaled_by_inverse_n` /
    `tests/test_defensive_contribution.py::
    test_nb_neg_log_lik_l2_penalty_is_scaled_by_inverse_n` use: isolate the
    penalty component via the `l2=0.0` subtraction, then compare
    row-doubled-identical-content `n_rows` against the original. **Verified
    by hand computation against a copy of the pre-fix expression before
    trusting this test (CLAUDE.md lesson 5): under the OLD unscaled formula
    (`nll = -sum(ll)/n_rows + l2*sum(beta**2)`) the penalty component was
    IDENTICAL at `n_rows=9` and the row-doubled `n_rows=18` (with `l2=1.5`
    and `sum(beta**2)` computed below, both give exactly `1.5 *
    sum(beta**2)`, independent of `n_rows`) — this test's halving assertion
    would have FAILED against that formula, which is what makes it a real
    pin, not a vacuous one.** Uses only rows with `n_trials > 0` (module
    docstring: an `n_trials=0` row contributes zero to the LIKELIHOOD, but
    the penalty term is a pure function of `beta`, not of the data rows at
    all, so the row-doubling trick works identically regardless of
    `n_trials`'s value — included here with real nonzero trials anyway so
    this test cannot be mistaken for exercising only the degenerate case
    above)."""
    rng = np.random.default_rng(3)
    n_rows, p = 9, 4
    X = rng.normal(size=(n_rows, p))
    X[:, 0] = 1.0
    n_trials = rng.integers(1, 4, size=n_rows).astype(np.float64)
    y = np.array([rng.integers(0, int(nt) + 1) for nt in n_trials], dtype=np.float64)
    minutes_frac = rng.uniform(0.2, 1.0, size=n_rows)
    beta = rng.normal(scale=0.4, size=p)
    l2 = 1.5

    X_doubled = np.vstack([X, X])
    n_trials_doubled = np.concatenate([n_trials, n_trials])
    y_doubled = np.concatenate([y, y])
    minutes_frac_doubled = np.concatenate([minutes_frac, minutes_frac])

    nll_n9, _ = _binomial_share_neg_log_lik_and_grad(beta, X, y, n_trials, minutes_frac, l2=l2)
    nll_n9_nopenalty, _ = _binomial_share_neg_log_lik_and_grad(beta, X, y, n_trials, minutes_frac, l2=0.0)
    penalty_n9 = nll_n9 - nll_n9_nopenalty

    nll_n18, _ = _binomial_share_neg_log_lik_and_grad(
        beta, X_doubled, y_doubled, n_trials_doubled, minutes_frac_doubled, l2=l2
    )
    nll_n18_nopenalty, _ = _binomial_share_neg_log_lik_and_grad(
        beta, X_doubled, y_doubled, n_trials_doubled, minutes_frac_doubled, l2=0.0
    )
    penalty_n18 = nll_n18 - nll_n18_nopenalty

    # The likelihood-only (l2=0) nll is unchanged by row-doubling identical content.
    assert nll_n9_nopenalty == pytest.approx(nll_n18_nopenalty, abs=1e-9)
    # The (fixed) penalty component exactly halves when n_rows doubles --
    # the signature of `(l2/n_rows)*sum(beta**2)`, not `l2*sum(beta**2)`.
    assert penalty_n18 == pytest.approx(penalty_n9 / 2.0, rel=1e-9)

    # And matches the closed-form expression exactly, at the original n_rows.
    expected_penalty_n9 = (l2 / n_rows) * float(np.sum(beta * beta))
    assert penalty_n9 == pytest.approx(expected_penalty_n9, rel=1e-9)


def test_feature_spec_rejects_mismatched_means_stds_length():
    with pytest.raises(AttackingModelError):
        AttackingFeatureSpec(
            numeric_columns=("a", "b"), position_categories=("DEF",),
            numeric_means=(0.0,), numeric_stds=(1.0,),
        )


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
    goals_scored: int = 0,
    assists: int = 0,
    expected_goals: float = 0.1,
    expected_assists: float = 0.1,
    team_h_score: int = 1,
    team_a_score: int = 0,
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
        "was_home": was_home,
        "goals_scored": goals_scored,
        "assists": assists,
        "expected_goals": expected_goals,
        "expected_assists": expected_assists,
        "team_h_score": team_h_score,
        "team_a_score": team_a_score,
    }


@pytest.fixture
def temp_store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def _write_fixture(store, rows, *, observed_at, valid_at=None):
    df = pl.DataFrame(rows)
    store.write("vaastav_player_gameweek_stats", df, valid_at=valid_at or observed_at, observed_at=observed_at, source="test")


def test_build_training_table_requires_timezone_aware_as_of(temp_store):
    _write_fixture(temp_store, [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z")], observed_at=dt(2026, 1, 1))
    with pytest.raises(AttackingModelError):
        build_training_table(temp_store, as_of=datetime(2026, 1, 1))


def test_build_training_table_excludes_rows_with_null_xg_or_xa(temp_store):
    """The xG/xA-era boundary (module docstring) — a row with NULL
    expected_goals/expected_assists (as every pre-2022-23 row genuinely is)
    must never enter the training table."""
    rows = [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z")]
    df = pl.DataFrame(rows).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("expected_goals"),
        pl.lit(None, dtype=pl.Float64).alias("expected_assists"),
    )
    temp_store.write("vaastav_player_gameweek_stats", df, valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    with pytest.raises(AttackingModelError):
        build_training_table(temp_store, as_of=dt(2026, 1, 2))


def test_build_training_table_team_goals_picks_the_players_own_side(temp_store):
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", was_home=True, team_h_score=3, team_a_score=1),
        _row("2022-23", 1, 11, 1, "2022-08-06T14:00:00Z", was_home=False, team_h_score=3, team_a_score=1, team="TeamB"),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    by_element = dict(zip(table["element"].to_list(), table["team_goals"].to_list()))
    assert by_element[10] == 3
    assert by_element[11] == 1


def test_build_training_table_raises_if_a_player_scored_more_than_the_team(temp_store):
    rows = [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", goals_scored=5, team_h_score=1, team_a_score=0)]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    with pytest.raises(AttackingModelError):
        build_training_table(temp_store, as_of=dt(2026, 1, 2))


def test_build_training_table_raises_if_assists_exceed_team_goals(temp_store):
    rows = [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", assists=5, team_h_score=1, team_a_score=0)]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    with pytest.raises(AttackingModelError):
        build_training_table(temp_store, as_of=dt(2026, 1, 2))


def test_player_trailing_feature_never_peeks_at_the_same_round_second_fixture(temp_store):
    """Double-gameweek round-boundary discipline — mirrors fplai.models.
    defensive_contribution's/minutes' own DGW isolation tests."""
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", goals_scored=1, team_h_score=1, team_a_score=0),
        # Round 2: a double gameweek. Second fixture has an enormous goal
        # tally that would corrupt the trailing feature if it leaked.
        _row("2022-23", 2, 10, 2, "2022-08-13T14:00:00Z", goals_scored=0, team_h_score=1, team_a_score=0),
        _row("2022-23", 2, 10, 3, "2022-08-16T14:00:00Z", goals_scored=4, team_h_score=4, team_a_score=0),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    round2 = table.filter(pl.col("round") == 2).sort("fixture")
    assert round2["player_trailing_goals_3"].to_list() == pytest.approx([1.0, 1.0])


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


# ---------------------------------------------------------------------------
# 3. Fit + predict + walk-forward mechanics — synthetic data.
# ---------------------------------------------------------------------------


def _synthetic_table(n_rounds: int = 12, seed: int = 0) -> pl.DataFrame:
    """A hand-built table carrying every column `_fit_from_table`/
    `walk_forward_validate` need, with a KNOWN signal: `HighScorer` always
    has a high trailing goals rate and actually scores often relative to
    team goals; `LowScorer` never does."""
    rng = np.random.default_rng(seed)
    rows = []
    for element, trailing_val, p_true in ((1, 1.5, 0.6), (2, 0.0, 0.02)):
        for round_ in range(1, n_rounds + 1):
            team_goals = int(rng.integers(0, 4))
            goals = int(rng.binomial(team_goals, p_true)) if team_goals > 0 else 0
            rows.append(
                {
                    "season": "2022-23",
                    "round": round_,
                    "element": element,
                    "fixture": round_,
                    "team": "TeamA",
                    "position": "FWD",
                    "minutes": 90,
                    "team_goals": team_goals,
                    "goals_scored": goals,
                    "assists": 0,
                    "player_trailing_goals_3": trailing_val,
                    "player_trailing_goals_5": trailing_val,
                    "player_trailing_goals_10": trailing_val,
                    "player_trailing_assists_3": 0.0,
                    "player_trailing_assists_5": 0.0,
                    "player_trailing_assists_10": 0.0,
                    "player_trailing_xg_3": trailing_val,
                    "player_trailing_xg_5": trailing_val,
                    "player_trailing_xg_10": trailing_val,
                    "player_trailing_xa_3": 0.0,
                    "player_trailing_xa_5": 0.0,
                    "player_trailing_xa_10": 0.0,
                    "games_played_this_season": float(round_ - 1),
                    "cold_start": round_ == 1,
                    "was_home": True,
                    "player_trailing_scored_rate_5": p_true,
                    "player_trailing_assisted_rate_5": 0.0,
                    "_chronological_rank": round_,
                }
            )
    return pl.DataFrame(rows)


def test_fit_from_table_recovers_the_trailing_goals_direction():
    table = _synthetic_table()
    params = _fit_from_table(table, config=AttackingModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    names = params.feature_spec.feature_names
    idx = names.index("player_trailing_goals_10")
    assert params.stats["goals"].beta[idx] > 0


def test_predict_attacking_pmf_rejects_team_goals_marginal_not_summing_to_one():
    table = _synthetic_table()
    params = _fit_from_table(table, config=AttackingModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 0.0 for c in NUMERIC_FEATURE_COLUMNS_ATTACKING}
    fr["position"] = "FWD"
    with pytest.raises(AttackingModelError):
        predict_attacking_pmf(
            params, fr, element=1, fixture=1, stat="goals",
            team_goals_marginal=[(0, 0.5), (1, 0.4)], minute_exposure=[(90.0, 1.0)],
        )


def test_predict_attacking_pmf_rejects_minute_exposure_not_summing_to_one():
    table = _synthetic_table()
    params = _fit_from_table(table, config=AttackingModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 0.0 for c in NUMERIC_FEATURE_COLUMNS_ATTACKING}
    fr["position"] = "FWD"
    with pytest.raises(AttackingModelError):
        predict_attacking_pmf(
            params, fr, element=1, fixture=1, stat="goals",
            team_goals_marginal=[(0, 1.0)], minute_exposure=[(90.0, 0.5)],
        )


def test_predict_attacking_pmf_rejects_unknown_stat():
    table = _synthetic_table()
    params = _fit_from_table(table, config=AttackingModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 0.0 for c in NUMERIC_FEATURE_COLUMNS_ATTACKING}
    fr["position"] = "FWD"
    with pytest.raises(AttackingModelError):
        predict_attacking_pmf(
            params, fr, element=1, fixture=1, stat="clean_sheets",
            team_goals_marginal=[(0, 1.0)], minute_exposure=[(90.0, 1.0)],
        )


def test_predict_attacking_pmf_zero_team_goals_marginal_forces_zero_involvement():
    """The core composition-rule sanity check: if the team_goals_marginal
    is a certain spike at 0, the player's own PMF must be a certain spike
    at 0 too, regardless of his own share rate (Binomial(0, p) == spike at
    0 for any p)."""
    table = _synthetic_table()
    params = _fit_from_table(table, config=AttackingModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 1.5 for c in NUMERIC_FEATURE_COLUMNS_ATTACKING}
    fr["cold_start"] = False
    fr["was_home"] = True
    fr["position"] = "FWD"
    pmf = predict_attacking_pmf(
        params, fr, element=1, fixture=1, stat="goals",
        team_goals_marginal=[(0, 1.0)], minute_exposure=[(90.0, 1.0)],
    )
    assert pmf.counts[0] == 0
    assert pmf.probabilities[0] == pytest.approx(1.0)
    assert pmf.p_involved() == pytest.approx(0.0)


def test_predict_attacking_pmf_zero_minute_exposure_forces_zero_involvement():
    table = _synthetic_table()
    params = _fit_from_table(table, config=AttackingModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 1.5 for c in NUMERIC_FEATURE_COLUMNS_ATTACKING}
    fr["cold_start"] = False
    fr["was_home"] = True
    fr["position"] = "FWD"
    pmf = predict_attacking_pmf(
        params, fr, element=1, fixture=1, stat="goals",
        team_goals_marginal=[(0, 0.2), (2, 0.8)], minute_exposure=[(0.0, 1.0)],
    )
    assert pmf.p_involved() == pytest.approx(0.0, abs=1e-9)


def test_predict_attacking_pmf_output_expands_to_cover_team_goals_marginals_top_end():
    table = _synthetic_table()
    params = _fit_from_table(table, config=AttackingModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 0.0 for c in NUMERIC_FEATURE_COLUMNS_ATTACKING}
    fr["position"] = "FWD"
    pmf = predict_attacking_pmf(
        params, fr, element=1, fixture=1, stat="goals",
        team_goals_marginal=[(g, 1.0 / 9) for g in range(9)], minute_exposure=[(90.0, 1.0)],
    )
    assert pmf.counts[-1] >= 8


def test_walk_forward_validate_raises_when_no_fold_has_enough_train_rows():
    table = _synthetic_table(n_rounds=3)
    with pytest.raises(AttackingModelError):
        walk_forward_validate(table, stat="goals", min_train_rows=10_000)


def test_walk_forward_validate_produces_one_prediction_per_eval_row():
    table = _synthetic_table(n_rounds=12)
    result = walk_forward_validate(table, stat="goals", min_train_rows=8, config=AttackingModelConfig(l2_penalty=0.1))
    assert result.n_folds > 0
    assert len(result.y_true) == len(result.p_model) == len(result.p_baseline_position_rate) == len(result.p_baseline_player_trailing)
    for p in result.p_model:
        assert 0.0 <= p <= 1.0


def test_walk_forward_validate_rejects_unknown_stat():
    table = _synthetic_table()
    with pytest.raises(AttackingModelError):
        walk_forward_validate(table, stat="clean_sheets")


# ---------------------------------------------------------------------------
# 4. Real-store-gated.
# ---------------------------------------------------------------------------


def _real_store_available() -> bool:
    path = Path(__file__).resolve().parents[1] / "data" / "store" / "vaastav_player_gameweek_stats"
    return path.exists() and any(path.glob("**/*.parquet"))


requires_real_store = pytest.mark.skipif(not _real_store_available(), reason="data/store/vaastav_player_gameweek_stats absent")


@pytest.fixture
def registered_capability():
    return CANONICAL_SCHEMAS[PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK]


def test_registering_the_capability_does_not_leak_across_tests(registered_capability):
    assert registered_capability.is_modelled is True
    assert PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK in CANONICAL_SCHEMAS


def test_importing_the_module_registers_the_capability_at_import_time():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import fplai.models.attacking\n"
        "from fplai.schemas import CANONICAL_SCHEMAS, PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK\n"
        "assert PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK in CANONICAL_SCHEMAS\n"
        "schema = CANONICAL_SCHEMAS[PLAYER_ATTACKING_INVOLVEMENT_DISTRIBUTION_GAMEWEEK]\n"
        "assert schema.is_modelled is True\n"
        "assert schema.entity_key == ('season', 'round', 'element', 'fixture', 'stat', 'count')\n"
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
def test_build_training_table_only_covers_the_xg_era_on_the_real_store():
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=dt(2026, 8, 22))
    assert set(table["season"].unique().to_list()) == {"2022-23", "2023-24", "2024-25", "2025-26"}
    assert table.height > 100_000


@pytest.mark.slow
@requires_real_store
def test_walk_forward_validate_beats_both_baselines_on_the_real_store():
    """A bounded live sanity check — the full-window gate numbers this
    task's report cites are produced by scripts/fit_attacking.py and
    recorded in docs/wiki/model-attacking.md; this proves the SAME code
    path against the real store inside the regular suite too."""
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=dt(2026, 8, 22))
    for stat in STATS:
        result = walk_forward_validate(table, stat=stat, min_train_rows=2000)
        assert result.n_folds > 20
        assert result.beats_both_baselines(), f"{stat} failed to beat both §7.1 baselines"


@pytest.mark.slow
@requires_real_store
def test_predict_attacking_pmf_end_to_end_against_a_real_fitted_model():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 22)
    params = fit_attacking_model(real_store, as_of=as_of)
    table = build_training_table(real_store, as_of=as_of)
    row = table.filter(pl.col("position") == "FWD").sort("round").tail(1).to_dicts()[0]
    fr = {c: row[c] for c in NUMERIC_FEATURE_COLUMNS_ATTACKING}
    fr["position"] = row["position"]
    pmf = predict_attacking_pmf(
        params, fr, element=row["element"], fixture=999999, stat="goals",
        team_goals_marginal=[(0, 0.3), (1, 0.4), (2, 0.2), (3, 0.1)],
        minute_exposure=[(90.0, 0.8), (0.0, 0.2)],
    )
    assert sum(pmf.probabilities) == pytest.approx(1.0)
    assert 0.0 <= pmf.p_involved() <= 1.0


# ---------------------------------------------------------------------------
# 5. Persistence.
# ---------------------------------------------------------------------------


def test_write_attacking_pmfs_round_trips_through_write_derived(temp_store, registered_capability):
    table = _synthetic_table()
    params = _fit_from_table(table, config=AttackingModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_ATTACKING}
    fr["cold_start"] = False
    fr["was_home"] = True
    fr["position"] = "FWD"
    pmf_goals = predict_attacking_pmf(
        params, fr, element=1, fixture=1, stat="goals",
        team_goals_marginal=[(0, 0.5), (1, 0.5)], minute_exposure=[(90.0, 1.0)],
    )
    pmf_assists = predict_attacking_pmf(
        params, fr, element=1, fixture=1, stat="assists",
        team_goals_marginal=[(0, 0.5), (1, 0.5)], minute_exposure=[(90.0, 1.0)],
    )
    calibration = {
        "goals": CalibrationReference(reference="test", residual_mean=0.0, residual_std=1.0),
        "assists": CalibrationReference(reference="test", residual_mean=0.0, residual_std=1.0),
    }
    result = write_attacking_pmfs(
        temp_store, [pmf_goals, pmf_assists], season="2022-23", round_=1, valid_at=dt(2026, 1, 1),
        calibration_by_stat=calibration,
    )
    assert set(result) == {"goals", "assists"}
    assert result["goals"].written and result["assists"].written
    out = temp_store.as_of(
        "derived_player_attacking_involvement_distribution", datetime.now(UTC) + __import__("datetime").timedelta(days=1)
    )
    assert out["is_modelled"].all()
    goals_rows = out.filter(pl.col("stat") == "goals")
    assert goals_rows["probability"].sum() == pytest.approx(1.0)
    assists_rows = out.filter(pl.col("stat") == "assists")
    assert assists_rows["probability"].sum() == pytest.approx(1.0)


def test_pmfs_to_rows_raises_on_empty_input():
    with pytest.raises(AttackingModelError):
        pmfs_to_rows([], season="2022-23", round_=1)


def test_write_attacking_pmfs_raises_if_calibration_missing_a_stat(temp_store, registered_capability):
    pmf = AttackingInvolvementPMF(
        element=1, fixture=1, stat="goals", counts=(0, 1), probabilities=(0.5, 0.5), mass_before_truncation=1.0,
    )
    with pytest.raises(AttackingModelError):
        write_attacking_pmfs(
            temp_store, [pmf], season="2022-23", round_=1, valid_at=dt(2026, 1, 1), calibration_by_stat={},
        )
