"""Tests for fplai.models.cards — Phase 2, E5, session s004 (blueprint §4,
§7.1, §12.2) — the last model before the Phase 2 (E5) calibration gate.

Five groups, mirroring `tests/test_bonus.py`/`tests/test_defensive_
contribution.py`'s own structure (the closest sibling modules):

  1. Pure functions — the outcome-space collapse, `CardsPMF` validation,
     the multinomial neg-log-lik cross-checked against finite differences.
  2. Feature engineering — a temp store, fabricated multi-round rows,
     including the double-gameweek leakage attack, the 2019-20 exclusion,
     and the minutes>0 fit-time restriction.
  3. Fit + predict + walk-forward mechanics — small synthetic data with a
     KNOWN trailing-card signal the model must recover, plus a break-first
     proof that a smuggled referee feature cannot influence a deployable
     prediction.
  4. Real-store-gated: import-time registration, the outcome-space archive
     verification, the referee join's real coverage, the §7.1 walk-forward
     gate, the referee-value measurement, and a synthetic-DGP check that
     this module's own calibration-slope machinery is not itself buggy.
  5. Persistence — `write_cards_pmfs` round-tripping through `write_derived`.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from fplai.calibration import log_loss as _binary_log_loss
from fplai.models.cards import (
    KNOWN_ABSENT_FEATURES,
    NUMERIC_FEATURE_COLUMNS_CARDS,
    NUMERIC_FEATURE_COLUMNS_CARDS_WITH_REFEREE_NOT_DEPLOYABLE,
    OUTCOME_LABELS,
    OUTCOMES,
    CardsFeatureSpec,
    CardsModelConfig,
    CardsModelError,
    CardsPMF,
    IsotonicCalibrator,
    _add_outcome_column,
    _apply_cards_calibration,
    _build_feature_spec,
    _fit_from_table,
    _fit_isotonic_calibrator,
    _inner_calibration_split,
    _multinomial_neg_log_lik_and_grad,
    attach_referee_feature_NOT_DEPLOYABLE,
    build_referee_trailing_feature_table_NOT_DEPLOYABLE,
    build_training_table,
    fit_cards_model,
    measure_referee_value_NOT_DEPLOYABLE,
    pmfs_to_rows,
    predict_cards_pmf,
    read_card_points,
    verify_yellow_red_mutually_exclusive_against_archive,
    walk_forward_validate,
    write_cards_pmfs,
)
from fplai.derived import CalibrationReference
from fplai.schemas import CANONICAL_SCHEMAS, PLAYER_CARDS_DISTRIBUTION_GAMEWEEK
from fplai.store import BitemporalStore

UTC = timezone.utc


def dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. Pure functions.
# ---------------------------------------------------------------------------


def test_known_absent_features_are_declared_and_never_leak_into_the_feature_set():
    assert "referee_identity" in KNOWN_ABSENT_FEATURES
    assert not set(KNOWN_ABSENT_FEATURES) & set(NUMERIC_FEATURE_COLUMNS_CARDS)


def test_referee_feature_only_lives_in_the_not_deployable_superset():
    assert set(NUMERIC_FEATURE_COLUMNS_CARDS_WITH_REFEREE_NOT_DEPLOYABLE) - set(NUMERIC_FEATURE_COLUMNS_CARDS) == {
        "referee_trailing_cards_per_match_10"
    }


def test_add_outcome_column_derives_none_yellow_red_correctly():
    df = pl.DataFrame({"yellow_cards": [0, 1, 0], "red_cards": [0, 0, 1]})
    out = _add_outcome_column(df)
    assert out["outcome"].to_list() == [0, 1, 2]


def test_add_outcome_column_raises_if_both_one():
    """Break-first proof: the outcome-space guarantee this module's whole
    design rests on is checked, not assumed, even on a hand-fabricated
    frame from outside the real archive."""
    df = pl.DataFrame({"yellow_cards": [1], "red_cards": [1]})
    with pytest.raises(CardsModelError):
        _add_outcome_column(df)


def test_cards_pmf_rejects_wrong_outcomes_tuple():
    with pytest.raises(CardsModelError):
        CardsPMF(element=1, fixture=1, outcomes=(0, 1), probabilities=(0.5, 0.5))


def test_cards_pmf_rejects_probabilities_not_summing_to_one():
    with pytest.raises(CardsModelError):
        CardsPMF(element=1, fixture=1, outcomes=OUTCOMES, probabilities=(0.5, 0.5, 0.5))


def test_cards_pmf_p_carded_p_red_and_expected_points():
    pmf = CardsPMF(element=1, fixture=1, outcomes=OUTCOMES, probabilities=(0.8, 0.15, 0.05))
    assert pmf.p_carded() == pytest.approx(0.2)
    assert pmf.p_red() == pytest.approx(0.05)
    assert pmf.expected_card_points(yellow_points=-1, red_points=-3) == pytest.approx(0.15 * -1 + 0.05 * -3)


def test_feature_spec_rejects_mismatched_means_stds_length():
    with pytest.raises(CardsModelError):
        CardsFeatureSpec(numeric_columns=("a", "b"), position_categories=("DEF",), numeric_means=(0.0,), numeric_stds=(1.0,))


def test_multinomial_neg_log_lik_gradient_matches_finite_differences():
    rng = np.random.default_rng(0)
    n, p = 80, 6
    X = rng.normal(size=(n, p))
    offset = rng.normal(scale=0.1, size=n)
    y_class = rng.integers(0, 3, size=n)
    beta = rng.normal(scale=0.3, size=2 * p)
    _, grad_analytic = _multinomial_neg_log_lik_and_grad(beta, X, y_class, offset, p, l2=0.1)

    eps = 1e-6
    grad_numeric = np.zeros_like(beta)
    for i in range(len(beta)):
        b_plus, b_minus = beta.copy(), beta.copy()
        b_plus[i] += eps
        b_minus[i] -= eps
        loss_plus, _ = _multinomial_neg_log_lik_and_grad(b_plus, X, y_class, offset, p, l2=0.1)
        loss_minus, _ = _multinomial_neg_log_lik_and_grad(b_minus, X, y_class, offset, p, l2=0.1)
        grad_numeric[i] = (loss_plus - loss_minus) / (2 * eps)

    assert np.allclose(grad_analytic, grad_numeric, atol=1e-4, rtol=1e-3)


def test_multinomial_l2_penalty_is_scaled_by_inverse_n():
    """Pins session s005's fix directly: the ridge term must be `(l2/n) *
    (sum(beta1**2) + sum(beta2**2))`, not the unscaled `l2 * (sum(beta1**2)
    + sum(beta2**2))` this module carried before this session (this task's
    brief; CLAUDE.md rule 5 — an unscaled penalty against a per-row-
    AVERAGED likelihood is effectively `l2*n`, silently crushing every
    coefficient toward 0 for `n` in the thousands — the exact defect
    `CardsModelConfig.l2_penalty`'s own docstring records having swept down
    to `0.001` (every `l2 >= 0.005` FAILED the §7.1 gate outright) to work
    around).

    Same row-doubling construction `tests/test_minutes.py::
    test_softmax_l2_penalty_is_scaled_by_inverse_n` /
    `tests/test_defensive_contribution.py::
    test_nb_neg_log_lik_l2_penalty_is_scaled_by_inverse_n` use: isolate the
    penalty component via the `l2=0.0` subtraction, then compare
    row-doubled-identical-content `n` against the original. **Verified by
    hand computation against a copy of the pre-fix expression before
    trusting this test (CLAUDE.md lesson 5): under the OLD unscaled formula
    (`nll = -sum(ll)/n + l2*(sum(beta1**2)+sum(beta2**2))`) the penalty
    component is `l2*(sum(beta1**2)+sum(beta2**2))`, which has no
    `n`-dependence at all -- identical at `n=15` and the row-doubled
    `n=30` -- so this test's halving assertion would have FAILED against
    that formula, which is what makes it a real pin, not a vacuous one.**"""
    rng = np.random.default_rng(6)
    n, p = 15, 5
    X = rng.normal(size=(n, p))
    X[:, 0] = 1.0
    offset = rng.normal(scale=0.1, size=n)
    y_class = rng.integers(0, 3, size=n)
    beta = rng.normal(scale=0.3, size=2 * p)
    l2 = 1.8

    X_doubled = np.vstack([X, X])
    offset_doubled = np.concatenate([offset, offset])
    y_class_doubled = np.concatenate([y_class, y_class])

    nll_n15, _ = _multinomial_neg_log_lik_and_grad(beta, X, y_class, offset, p, l2=l2)
    nll_n15_nopenalty, _ = _multinomial_neg_log_lik_and_grad(beta, X, y_class, offset, p, l2=0.0)
    penalty_n15 = nll_n15 - nll_n15_nopenalty

    nll_n30, _ = _multinomial_neg_log_lik_and_grad(beta, X_doubled, y_class_doubled, offset_doubled, p, l2=l2)
    nll_n30_nopenalty, _ = _multinomial_neg_log_lik_and_grad(
        beta, X_doubled, y_class_doubled, offset_doubled, p, l2=0.0
    )
    penalty_n30 = nll_n30 - nll_n30_nopenalty

    # The likelihood-only (l2=0) nll is unchanged by row-doubling identical content.
    assert nll_n15_nopenalty == pytest.approx(nll_n30_nopenalty, abs=1e-9)
    # The (fixed) penalty component exactly halves when n doubles -- the
    # signature of `(l2/n)*(sum(beta1**2)+sum(beta2**2))`, not the unscaled form.
    assert penalty_n30 == pytest.approx(penalty_n15 / 2.0, rel=1e-9)

    # And matches the closed-form expression exactly, at the original n.
    beta1, beta2 = beta[:p], beta[p:]
    expected_penalty_n15 = (l2 / n) * (float(np.sum(beta1 * beta1)) + float(np.sum(beta2 * beta2)))
    assert penalty_n15 == pytest.approx(expected_penalty_n15, rel=1e-9)


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
    opponent_team: int = 2,
    minutes: int = 90,
    position: str = "MID",
    yellow_cards: int = 0,
    red_cards: int = 0,
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
        "opponent_team": opponent_team,
        "yellow_cards": yellow_cards,
        "red_cards": red_cards,
    }


@pytest.fixture
def temp_store(tmp_path) -> BitemporalStore:
    return BitemporalStore(base_path=tmp_path / "store")


def _write_fixture(store, rows, *, observed_at, valid_at=None):
    df = pl.DataFrame(rows)
    store.write("vaastav_player_gameweek_stats", df, valid_at=valid_at or observed_at, observed_at=observed_at, source="test")


def test_build_training_table_requires_timezone_aware_as_of(temp_store):
    _write_fixture(temp_store, [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z")], observed_at=dt(2026, 1, 1))
    with pytest.raises(CardsModelError):
        build_training_table(temp_store, as_of=datetime(2026, 1, 1))


def test_build_training_table_excludes_2019_20_null_position(temp_store):
    rows = [_row("2019-20", 1, 10, 1, "2019-08-09T14:00:00Z")]
    df = pl.DataFrame(rows).with_columns(
        pl.lit(None, dtype=pl.Utf8).alias("position"), pl.lit(None, dtype=pl.Utf8).alias("team")
    )
    temp_store.write("vaastav_player_gameweek_stats", df, valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    with pytest.raises(CardsModelError):
        build_training_table(temp_store, as_of=dt(2026, 1, 2))


def test_build_training_table_excludes_zero_minute_rows(temp_store):
    """The deliberate deviation from bonus's zero-inclusion — module
    docstring, 'A card without a minute is real, not a data error'."""
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", minutes=90),
        _row("2022-23", 1, 11, 1, "2022-08-06T14:00:00Z", minutes=0, team="TeamB", was_home=False, yellow_cards=1),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    assert set(table["element"].to_list()) == {10}


def test_player_trailing_card_rate_never_peeks_at_the_same_round_second_fixture(temp_store):
    """Double-gameweek round-boundary discipline — mirrors every sibling
    module's own DGW isolation test."""
    rows = [
        _row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", yellow_cards=1),
        # Round 2: a double gameweek. Second fixture has a card that would
        # corrupt the trailing feature if it leaked into the first.
        _row("2022-23", 2, 10, 2, "2022-08-13T14:00:00Z", yellow_cards=0),
        _row("2022-23", 2, 10, 3, "2022-08-16T14:00:00Z", yellow_cards=1),
    ]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    round2 = table.filter(pl.col("round") == 2).sort("fixture")
    # Both round-2 rows must see the SAME pre-round-2 trailing rate (from
    # round 1's card only), never the same round's other fixture.
    assert round2["player_trailing_card_rate_3"].to_list() == pytest.approx([1.0, 1.0])


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


def test_build_training_table_raises_on_yellow_and_red_both_one(temp_store):
    rows = [_row("2022-23", 1, 10, 1, "2022-08-06T14:00:00Z", yellow_cards=1, red_cards=1)]
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    with pytest.raises(CardsModelError):
        build_training_table(temp_store, as_of=dt(2026, 1, 2))


# ---------------------------------------------------------------------------
# 3. Fit + predict + walk-forward mechanics — synthetic data.
# ---------------------------------------------------------------------------


def _synthetic_table(n_rounds: int = 20, seed: int = 0) -> pl.DataFrame:
    """A hand-built table with a KNOWN signal: `Rowdy` always has a high
    trailing-card-rate feature; `Calm` never does."""
    rng = np.random.default_rng(seed)
    rows = []
    for element, trailing_val, p_yellow in ((1, 0.8, 0.5), (2, 0.0, 0.02)):
        for round_ in range(1, n_rounds + 1):
            outcome = 1 if rng.random() < p_yellow else 0
            rows.append(
                {
                    "season": "2022-23",
                    "round": round_,
                    "element": element,
                    "fixture": round_ * 10 + element,
                    "team": "TeamA",
                    "position": "MID",
                    "minutes": 90,
                    "outcome": outcome,
                    "player_trailing_card_rate_3": trailing_val,
                    "player_trailing_card_rate_5": trailing_val,
                    "player_trailing_card_rate_10": trailing_val,
                    "team_trailing_cards_mean_5": 1.0,
                    "games_played_this_season": float(round_ - 1),
                    "cold_start": round_ == 1,
                    "team_cold_start": False,
                    "was_home": True,
                    "player_trailing_cards_class_rate_0_5": 1.0 - trailing_val,
                    "player_trailing_cards_class_rate_1_5": trailing_val,
                    "player_trailing_cards_class_rate_2_5": 0.0,
                    "_chronological_rank": round_,
                }
            )
    return pl.DataFrame(rows)


def test_fit_from_table_recovers_the_trailing_card_rate_direction():
    table = _synthetic_table()
    params = _fit_from_table(table, config=CardsModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    idx = params.feature_spec.feature_names.index("player_trailing_card_rate_10")
    assert params.beta_yellow[idx] > 0


def test_predict_cards_pmf_sums_to_one_and_is_deterministic():
    table = _synthetic_table()
    params = _fit_from_table(table, config=CardsModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))

    def make_row(trailing):
        return {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_CARDS} | {
            "player_trailing_card_rate_3": trailing,
            "player_trailing_card_rate_5": trailing,
            "player_trailing_card_rate_10": trailing,
            "cold_start": False,
            "team_cold_start": False,
            "was_home": True,
            "position": "MID",
        }

    fr_rowdy = make_row(0.8)
    fr_calm = make_row(0.0)
    pmf_a = predict_cards_pmf(params, fr_rowdy, element=1, fixture=1, minute_exposure=[(90.0, 1.0)])
    pmf_b = predict_cards_pmf(params, fr_rowdy, element=1, fixture=1, minute_exposure=[(90.0, 1.0)])
    assert sum(pmf_a.probabilities) == pytest.approx(1.0)
    assert pmf_a.probabilities == pmf_b.probabilities  # deterministic, no randomness anywhere (rule 7)

    pmf_calm = predict_cards_pmf(params, fr_calm, element=2, fixture=1, minute_exposure=[(90.0, 1.0)])
    assert pmf_a.p_carded() > pmf_calm.p_carded()


def test_predict_cards_pmf_zero_minute_exposure_forces_spike_at_none():
    table = _synthetic_table()
    params = _fit_from_table(table, config=CardsModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_CARDS} | {
        "player_trailing_card_rate_3": 0.8,
        "player_trailing_card_rate_5": 0.8,
        "player_trailing_card_rate_10": 0.8,
        "cold_start": False,
        "team_cold_start": False,
        "was_home": True,
        "position": "MID",
    }
    pmf = predict_cards_pmf(params, fr, element=1, fixture=1, minute_exposure=[(0.0, 1.0)])
    assert pmf.probabilities == (1.0, 0.0, 0.0)


def test_predict_cards_pmf_rejects_minute_exposure_not_summing_to_one():
    table = _synthetic_table()
    params = _fit_from_table(table, config=CardsModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_CARDS} | {"cold_start": False, "team_cold_start": False, "was_home": True, "position": "MID"}
    with pytest.raises(CardsModelError):
        predict_cards_pmf(params, fr, element=1, fixture=1, minute_exposure=[(90.0, 0.5)])


def test_predict_cards_pmf_structurally_cannot_be_influenced_by_a_referee_feature_smuggled_into_the_feature_row():
    """Break-first proof of the module docstring's 'Structural guarantee'
    — attacked from outside the sanctioned call path, i.e. a caller reaching
    directly into `feature_row` rather than going through some blessed
    helper. A deployable `CardsModelParams`'s `feature_spec.numeric_columns`
    never includes the referee feature, so `_feature_row_to_vector` has no
    coefficient to multiply a smuggled value against — the PMF is
    bit-identical whether or not the key is present."""
    table = _synthetic_table()
    params = _fit_from_table(table, config=CardsModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    assert "referee_trailing_cards_per_match_10" not in params.feature_spec.numeric_columns

    fr_clean = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_CARDS} | {
        "cold_start": False, "team_cold_start": False, "was_home": True, "position": "MID",
    }
    fr_smuggled = dict(fr_clean)
    fr_smuggled["referee_trailing_cards_per_match_10"] = 999.0  # an absurd, attention-grabbing value

    pmf_clean = predict_cards_pmf(params, fr_clean, element=1, fixture=1, minute_exposure=[(90.0, 1.0)])
    pmf_smuggled = predict_cards_pmf(params, fr_smuggled, element=1, fixture=1, minute_exposure=[(90.0, 1.0)])
    assert pmf_clean.probabilities == pmf_smuggled.probabilities


def test_walk_forward_validate_requires_chronological_rank():
    table = _synthetic_table().drop("_chronological_rank")
    with pytest.raises(CardsModelError):
        walk_forward_validate(table, min_train_rows=1)


def test_multiclass_metrics_over_zero_rows_raise():
    from fplai.models.cards import _multiclass_brier, _multiclass_log_loss

    with pytest.raises(CardsModelError):
        _multiclass_log_loss([], [])
    with pytest.raises(CardsModelError):
        _multiclass_brier([], [])


# ---------------------------------------------------------------------------
# 3b. Nested out-of-sample NONE/YELLOW calibration — session s005, module
# docstring "Nested out-of-sample NONE/YELLOW calibration". This is the E5
# blocking-condition remedy this task exists to close (docs/HANDOFF.md §2:
# cards NONE/YELLOW overconfident, slopes 0.592/0.630).
# ---------------------------------------------------------------------------


def _many_rounds_rows(n_rounds: int, n_players: int, *, seed: int = 0, p_yellow: float = 0.15) -> list[dict]:
    """`n_rounds * n_players` rows, one per (round, player), real-shaped
    minutes>0 rows with randomised (but deterministic, seeded) YELLOW
    outcomes -- enough real structure for `build_training_table`'s own
    rollups/joins to run without error, at whatever volume a given
    calibration test needs."""
    rng = np.random.default_rng(seed)
    base = datetime(2022, 8, 6, 14, tzinfo=timezone.utc)
    rows = []
    for round_ in range(1, n_rounds + 1):
        kickoff = (base + timedelta(days=(round_ - 1) * 4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for p in range(1, n_players + 1):
            yellow = 1 if rng.random() < p_yellow else 0
            rows.append(
                _row(
                    "2022-23",
                    round_,
                    p,
                    round_ * 100 + p,
                    kickoff,
                    team=f"Team{p}",
                    was_home=bool((round_ + p) % 2),
                    opponent_team=((p + 1) % n_players) + 1,
                    minutes=90,
                    position="MID",
                    yellow_cards=yellow,
                    red_cards=0,
                )
            )
    return rows


def test_apply_cards_calibration_holds_red_fixed_renormalises_and_sums_to_one():
    """Direct unit test of `_apply_cards_calibration` (module docstring,
    "Renormalisation — the real design question this task's brief names
    directly"): RED must be returned bit-identical to its input, and the
    three outputs must always sum to 1.0, however the two calibrators
    reshape NONE/YELLOW."""
    none_cal = IsotonicCalibrator(x=(0.0, 0.5, 1.0), y=(0.0, 0.7, 1.0))  # pulls NONE UP (corrects overconfidence)
    yellow_cal = IsotonicCalibrator(x=(0.0, 0.5, 1.0), y=(0.0, 0.3, 1.0))  # pulls YELLOW DOWN

    for p0, p1, p2 in [(0.85, 0.13, 0.02), (0.5, 0.5, 0.0), (0.98, 0.01, 0.01), (0.0, 0.0, 1.0)]:
        p0f, p1f, p2f = _apply_cards_calibration(p0, p1, p2, none_cal, yellow_cal)
        assert p2f == pytest.approx(p2), "RED must be held completely fixed at its raw value"
        assert p0f + p1f + p2f == pytest.approx(1.0)
        assert p0f >= 0.0 and p1f >= 0.0


def test_predict_cards_pmf_applies_calibration_when_present_and_reports_isotonic_v1():
    table = _synthetic_table()
    params = _fit_from_table(table, config=CardsModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    none_cal = IsotonicCalibrator(x=(0.0, 1.0), y=(0.0, 1.0))  # identity -- isolates the code PATH, not the math
    yellow_cal = IsotonicCalibrator(x=(0.0, 1.0), y=(0.0, 1.0))
    calibrated_params = replace(params, p_none_calibrator=none_cal, p_yellow_calibrator=yellow_cal)

    fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_CARDS} | {
        "player_trailing_card_rate_3": 0.8, "player_trailing_card_rate_5": 0.8, "player_trailing_card_rate_10": 0.8,
        "cold_start": False, "team_cold_start": False, "was_home": True, "position": "MID",
    }
    pmf_raw = predict_cards_pmf(params, fr, element=1, fixture=1, minute_exposure=[(90.0, 1.0)])
    pmf_cal = predict_cards_pmf(calibrated_params, fr, element=1, fixture=1, minute_exposure=[(90.0, 1.0)])

    assert pmf_raw.calibration_method == "raw_uncalibrated"
    assert pmf_cal.calibration_method == "isotonic_v1"
    assert sum(pmf_cal.probabilities) == pytest.approx(1.0)
    # Identity calibrators -> no-op renormalisation -> bit-identical to raw.
    assert pmf_cal.probabilities == pytest.approx(pmf_raw.probabilities)


def test_predict_cards_pmf_zero_minute_exposure_bypasses_calibration_entirely():
    """The `(0.0, w)` spike-at-NONE branch is a structural fact, never a
    model output — module docstring — so it must stay `(1.0, 0.0, 0.0)`
    even when calibrators are attached."""
    table = _synthetic_table()
    params = _fit_from_table(table, config=CardsModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    none_cal = IsotonicCalibrator(x=(0.0, 1.0), y=(0.2, 0.9))  # deliberately NON-identity
    yellow_cal = IsotonicCalibrator(x=(0.0, 1.0), y=(0.1, 0.8))
    calibrated_params = replace(params, p_none_calibrator=none_cal, p_yellow_calibrator=yellow_cal)
    fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_CARDS} | {
        "cold_start": False, "team_cold_start": False, "was_home": True, "position": "MID",
    }
    pmf = predict_cards_pmf(calibrated_params, fr, element=1, fixture=1, minute_exposure=[(0.0, 1.0)])
    assert pmf.probabilities == (1.0, 0.0, 0.0)


def test_cards_model_params_rejects_mismatched_calibrators():
    """Break-first-adjacent guard: `CardsModelParams.__post_init__` refuses
    a params object carrying only ONE of the two calibrators — this module
    never fits one without the other (`_apply_cards_calibration` always
    needs both)."""
    table = _synthetic_table()
    params = _fit_from_table(table, config=CardsModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    only_none = IsotonicCalibrator(x=(0.0, 1.0), y=(0.0, 1.0))
    with pytest.raises(CardsModelError):
        replace(params, p_none_calibrator=only_none)


def test_fit_cards_model_calibrate_true_attaches_calibrators_with_enough_data(temp_store):
    rows = _many_rounds_rows(n_rounds=40, n_players=4, seed=1)
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    params = fit_cards_model(
        temp_store, as_of=dt(2026, 1, 2), calibrate=True, min_calibration_holdout_rows=10, min_inner_train_rows=10
    )
    assert params.p_none_calibrator is not None
    assert params.p_yellow_calibrator is not None


def test_fit_cards_model_calibrate_false_stays_raw_even_with_enough_data(temp_store):
    rows = _many_rounds_rows(n_rounds=40, n_players=4, seed=1)
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    params = fit_cards_model(temp_store, as_of=dt(2026, 1, 2), calibrate=False)
    assert params.p_none_calibrator is None
    assert params.p_yellow_calibrator is None


def test_fit_cards_model_calibrate_true_falls_back_gracefully_when_window_too_small(temp_store, caplog):
    rows = _many_rounds_rows(n_rounds=3, n_players=2, seed=1)  # far below the default 200/200 thresholds
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    params = fit_cards_model(temp_store, as_of=dt(2026, 1, 2))  # calibrate=True default
    assert params.p_none_calibrator is None
    assert params.p_yellow_calibrator is None


def test_cards_calibrator_fit_directly_on_eval_data_would_leak_and_look_suspiciously_good():
    """THE LEAKAGE TRAP, constructed by hand and shown to be real -- the
    same attack `fplai.models.minutes`'s own precedent test makes,
    repeated here against THIS module's copy of `_fit_isotonic_calibrator`
    (module docstring, "Attacked, not just asserted"). A calibrator fit on
    the exact data it is then scored against looks strictly better than one
    fit on a disjoint, earlier slice -- even though the disjoint one is the
    only leakage-safe choice. Never touches `walk_forward_validate` or any
    nested-split machinery -- it attacks the FORBIDDEN path directly."""
    rng = np.random.default_rng(20260822)
    n = 400
    true_p = rng.uniform(0.05, 0.95, size=n)
    y = (rng.uniform(size=n) < true_p).astype(np.float64)
    raw_p = 0.5 + (true_p - 0.5) * 0.5  # compressed toward 0.5 -- the overconfidence-in-reverse shape

    fit_idx = np.arange(0, n // 2)
    eval_idx = np.arange(n // 2, n)

    honest_calibrator = _fit_isotonic_calibrator(raw_p[fit_idx], y[fit_idx])
    honest_calibrated_eval = honest_calibrator.apply(raw_p[eval_idx])
    honest_log_loss = _binary_log_loss(y[eval_idx].astype(int).tolist(), honest_calibrated_eval.tolist())

    # THE FORBIDDEN PATH: fit the calibrator directly on eval_idx's own outcomes.
    leaky_calibrator = _fit_isotonic_calibrator(raw_p[eval_idx], y[eval_idx])
    leaky_calibrated_eval = leaky_calibrator.apply(raw_p[eval_idx])
    leaky_log_loss = _binary_log_loss(y[eval_idx].astype(int).tolist(), leaky_calibrated_eval.tolist())

    assert leaky_log_loss < honest_log_loss  # the leak looks better -- proven, not assumed
    raw_log_loss = _binary_log_loss(y[eval_idx].astype(int).tolist(), raw_p[eval_idx].tolist())
    assert honest_log_loss < raw_log_loss  # both still beat raw -- the compression is real and correctable


# `test_isotonic_calibrator_never_saturates_to_exact_zero_or_one`/
# `test_isotonic_calibrator_smoothing_converges_to_raw_rate_at_scale` moved
# to `tests/test_calibration.py` session s005 (consolidation) — they
# attacked the shared `IsotonicCalibrator` mechanism directly (identical
# fixtures to `fplai.models.minutes`'s own precedent versions), not
# anything specific to this module. The measured, model-specific saturation
# CENSUS this test's docstring used to carry (YELLOW: 121/58,461 at exactly
# 0.0, NONE: none) is preserved in `docs/wiki/model-cards.md`, not lost.
# The leakage proof above and the model-integration tests below are
# UNCHANGED and stay here.


def test_walk_forward_validate_calibrate_false_leaves_p_model_identical_to_p_model_raw(temp_store):
    rows = _many_rounds_rows(n_rounds=25, n_players=3, seed=2)
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    result = walk_forward_validate(table, min_train_rows=10, calibrate=False)
    assert result.p_model == result.p_model_raw
    assert result.calibrated is False
    assert result.n_folds_calibrated == 0


def test_walk_forward_validate_calibrate_true_populates_calibrated_folds(temp_store):
    rows = _many_rounds_rows(n_rounds=40, n_players=4, seed=3)
    _write_fixture(temp_store, rows, observed_at=dt(2026, 1, 1))
    table = build_training_table(temp_store, as_of=dt(2026, 1, 2))
    result = walk_forward_validate(
        table, min_train_rows=10, calibrate=True, min_calibration_holdout_rows=10, min_inner_train_rows=10
    )
    assert result.calibrated is True
    assert result.n_folds_calibrated > 0
    assert len(result.p_model) == len(result.p_model_raw) == len(result.y_true_class)
    for row in result.p_model:
        assert sum(row) == pytest.approx(1.0)


def test_walk_forward_validate_calibrate_cannot_see_a_folds_own_or_future_outcomes(tmp_path):
    """The SAME future-fold-isolation attack `fplai.models.minutes`'s own
    precedent test makes for `p_model_calibrated`, repeated here for cards'
    `p_model` (module docstring: this nested-calibration boundary is a
    SEPARATE claim from the raw model's own leakage boundary — the raw
    model's own isolation is proven by `test_player_trailing_card_rate_
    never_peeks_at_the_same_round_second_fixture`'s feature-engineering
    sibling in group 2 plus the fold-boundary construction itself — and
    must be attacked separately here, not assumed to inherit for free."""
    rows_a = _many_rounds_rows(n_rounds=30, n_players=3, seed=4)
    kwargs = dict(min_train_rows=10, calibrate=True, min_calibration_holdout_rows=10, min_inner_train_rows=10)

    store_a = BitemporalStore(base_path=tmp_path / "store_a")
    _write_fixture(store_a, rows_a, observed_at=dt(2026, 1, 1))
    table_a = build_training_table(store_a, as_of=dt(2026, 1, 2))
    result_a = walk_forward_validate(table_a, **kwargs)

    rows_b = [dict(r) for r in rows_a]
    last_round = max(r["round"] for r in rows_b)
    for row in rows_b:
        if row["round"] == last_round:
            row["yellow_cards"] = 1 - row["yellow_cards"]
    store_b = BitemporalStore(base_path=tmp_path / "store_b")
    _write_fixture(store_b, rows_b, observed_at=dt(2026, 1, 1))
    table_b = build_training_table(store_b, as_of=dt(2026, 1, 2))
    result_b = walk_forward_validate(table_b, **kwargs)

    n_players = 3
    n_before_last_round = len(result_a.p_model) - n_players
    assert n_before_last_round > 0
    # pytest.approx does not support nested tuples-of-tuples -- compare via
    # numpy arrays instead (flat pytest.approx of a scalar array works fine).
    a = np.array(result_a.p_model[:n_before_last_round])
    b = np.array(result_b.p_model[:n_before_last_round])
    assert a == pytest.approx(b)


# ---------------------------------------------------------------------------
# 4. Real-store-gated.
# ---------------------------------------------------------------------------


def _real_store_available() -> bool:
    path = Path(__file__).resolve().parents[1] / "data" / "store" / "vaastav_player_gameweek_stats"
    return path.exists() and any(path.glob("**/*.parquet"))


def _real_officials_available() -> bool:
    path = Path(__file__).resolve().parents[1] / "data" / "store" / "pl_match_officials"
    return path.exists() and any(path.glob("**/*.parquet"))


requires_real_store = pytest.mark.skipif(not _real_store_available(), reason="data/store/vaastav_player_gameweek_stats absent")
requires_real_officials = pytest.mark.skipif(not _real_officials_available(), reason="data/store/pl_match_officials absent")


@pytest.fixture
def registered_capability():
    return CANONICAL_SCHEMAS[PLAYER_CARDS_DISTRIBUTION_GAMEWEEK]


def test_registering_the_capability_does_not_leak_across_tests(registered_capability):
    assert registered_capability.is_modelled is True
    assert PLAYER_CARDS_DISTRIBUTION_GAMEWEEK in CANONICAL_SCHEMAS


def test_importing_the_module_registers_the_capability_at_import_time():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import fplai.models.cards\n"
        "from fplai.schemas import CANONICAL_SCHEMAS, PLAYER_CARDS_DISTRIBUTION_GAMEWEEK\n"
        "assert PLAYER_CARDS_DISTRIBUTION_GAMEWEEK in CANONICAL_SCHEMAS\n"
        "schema = CANONICAL_SCHEMAS[PLAYER_CARDS_DISTRIBUTION_GAMEWEEK]\n"
        "assert schema.is_modelled is True\n"
        "assert schema.entity_key == ('season', 'round', 'element', 'fixture', 'outcome')\n"
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
def test_verify_yellow_red_mutually_exclusive_against_archive_on_the_real_store():
    """Break-first-style proof, attacked from outside the sanctioned call
    path — over the ENTIRE real archive, all 7 seasons."""
    real_store = BitemporalStore()
    result = verify_yellow_red_mutually_exclusive_against_archive(real_store, as_of=dt(2026, 8, 29))
    assert result.n_rows > 150_000
    assert result.holds


@pytest.mark.slow
@requires_real_store
def test_build_training_table_excludes_2019_20_on_the_real_store():
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=dt(2026, 8, 29))
    assert "2019-20" not in set(table["season"].unique().to_list())
    assert table.height > 50_000


@pytest.mark.slow
@requires_real_store
def test_walk_forward_validate_beats_both_baselines_on_the_real_store():
    """A bounded live check of the SAME code path
    `scripts/fit_cards.py`/`docs/wiki/model-cards.md` report the full
    numbers from. `calibrate=True` is now the default (session s005) --
    this is therefore a check on the CALIBRATED (shipped) series, `p_model`."""
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=dt(2026, 8, 29))
    result = walk_forward_validate(table, min_train_rows=8000, config=CardsModelConfig(l2_penalty=0.001))
    assert result.n_folds > 20
    assert result.beats_both_baselines(), "cards model failed to beat both §7.1 baselines"


@pytest.mark.slow
@requires_real_store
def test_walk_forward_validate_calibrated_beats_both_baselines_raw_also_beats_both_on_the_real_store():
    """The raw model (pre-s005) already passed this exact gate -- confirm
    it STILL does (calibration must not be a prerequisite for clearing the
    baseline bar), independently of the calibrated check above."""
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=dt(2026, 8, 29))
    result = walk_forward_validate(table, min_train_rows=8000, config=CardsModelConfig(l2_penalty=0.001), calibrate=False)
    assert result.n_folds > 20
    assert result.beats_both_baselines(), "raw cards model failed to beat both §7.1 baselines"


@pytest.mark.slow
@requires_real_store
def test_calibrated_cards_model_is_not_worse_than_raw_on_any_pooled_metric_on_the_real_store():
    """This task's brief, explicit gate condition: the calibrated model
    must not be worse than the raw one on ANY pooled metric. Both series
    come from the SAME walk-forward run (`p_model` vs `p_model_raw`), so
    this is an honest apples-to-apples comparison, not two separate fits
    that could differ for unrelated reasons."""
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=dt(2026, 8, 29))
    result = walk_forward_validate(table, min_train_rows=8000, config=CardsModelConfig(l2_penalty=0.001))
    assert result.n_folds_calibrated > 0, "no fold received a real nested calibrator -- the comparison below would be vacuous"
    assert result.calibrated_not_worse_than_raw(), (
        f"calibrated model log-loss={result.model_log_loss():.4f} (raw {result.model_log_loss_raw():.4f}), "
        f"brier={result.model_brier():.4f} (raw {result.model_brier_raw():.4f})"
    )


@pytest.mark.slow
@requires_real_store
def test_calibrated_none_yellow_slopes_move_toward_one_on_the_real_store():
    """The whole point of session s005, measured directly: the E5 report's
    rejected acceptance was cards NONE/YELLOW's overconfident slopes
    (0.592/0.630). After calibration those slopes must sit closer to 1.0
    than the raw slopes did -- not asserted to land exactly at 1.0 (this
    task's brief allows a documented reason if it cannot), just that the
    fix moves the numbers in the intended direction, on the real store."""
    real_store = BitemporalStore()
    table = build_training_table(real_store, as_of=dt(2026, 8, 29))
    result = walk_forward_validate(table, min_train_rows=8000, config=CardsModelConfig(l2_penalty=0.001))
    for k in (0, 1):  # NONE, YELLOW -- never RED (module docstring, "Scope")
        raw_slope = result.reliability_for_outcome_raw(k).calibration_slope
        cal_slope = result.reliability_for_outcome(k).calibration_slope
        assert abs(cal_slope - 1.0) < abs(raw_slope - 1.0), (
            f"outcome {OUTCOME_LABELS[k]}: calibrated slope {cal_slope:.3f} is not closer to 1.0 "
            f"than raw slope {raw_slope:.3f}"
        )


@pytest.mark.slow
@requires_real_store
def test_predict_cards_pmf_end_to_end_against_a_real_fitted_model():
    real_store = BitemporalStore()
    as_of = dt(2026, 8, 29)
    params = fit_cards_model(real_store, as_of=as_of)
    table = build_training_table(real_store, as_of=as_of)
    row = table.filter(pl.col("minutes") > 0).head(1).to_dicts()[0]
    fr = {c: row[c] for c in NUMERIC_FEATURE_COLUMNS_CARDS} | {"position": row["position"]}
    pmf = predict_cards_pmf(params, fr, element=row["element"], fixture=999999, minute_exposure=[(90.0, 0.8), (0.0, 0.2)])
    assert sum(pmf.probabilities) == pytest.approx(1.0)
    assert 0.0 <= pmf.p_carded() <= 1.0


@pytest.mark.slow
@requires_real_store
@requires_real_officials
def test_referee_trailing_feature_table_resolves_at_least_99_percent_of_fixtures_on_the_real_store():
    """Break-first proof of the join this task's brief specified — this
    module's OWN implementation of it, not merely trusting the brief's
    cited number without re-checking the code that actually runs."""
    real_store = BitemporalStore()
    as_of = datetime.now(UTC)
    ref_table = build_referee_trailing_feature_table_NOT_DEPLOYABLE(real_store, as_of=as_of)
    # Denominator: every was_home=True (season, fixture) pair 2020-21+, the
    # same population the join's own docstring cites (2,280).
    raw = real_store.effective_at("vaastav_player_gameweek_stats", as_of)
    raw = raw.filter(pl.col("team").is_not_null())
    n_fixtures = raw.filter(pl.col("was_home")).select(["season", "fixture"]).unique().height
    assert n_fixtures > 2000
    assert ref_table.height / n_fixtures > 0.99


@pytest.mark.slow
@requires_real_store
@requires_real_officials
def test_measure_referee_value_not_deployable_runs_end_to_end_on_the_real_store():
    """A bounded live run of the size-of-the-prize measurement itself —
    both variants must evaluate the exact same row count (the fairness
    guarantee `measure_referee_value_NOT_DEPLOYABLE` itself asserts)."""
    real_store = BitemporalStore()
    as_of = datetime.now(UTC)
    table = build_training_table(real_store, as_of=as_of)
    ref_table = build_referee_trailing_feature_table_NOT_DEPLOYABLE(real_store, as_of=as_of)
    attached = attach_referee_feature_NOT_DEPLOYABLE(table, ref_table)
    comparison = measure_referee_value_NOT_DEPLOYABLE(attached, min_train_rows=8000, config=CardsModelConfig(l2_penalty=0.001))
    assert comparison.n_rows_compared > 10_000
    assert len(comparison.deployable.y_true_class) == len(comparison.referee_inclusive_variant_NOT_DEPLOYABLE.y_true_class)
    # No claim about the SIGN of the delta here (that is the actual research
    # question) -- only that the measurement runs and is well-formed.
    assert isinstance(comparison.log_loss_delta(), float)
    assert isinstance(comparison.brier_delta(), float)


def test_walk_forward_calibration_slope_recovers_correctly_on_a_known_synthetic_generative_process():
    """Not a real-store check -- a proof this module's own walk-forward +
    calibration machinery is not itself buggy, run against data with a
    KNOWN true generating process (a multinomial logit with the exact
    offset construction this module uses). Regularising ABOVE the true
    generating scale must UNDERconfident the fit (slope > 1) — the textbook
    expected direction — which is what lets `CardsModelConfig.l2_penalty`'s
    own docstring conclude the real store's slope<1 finding is a genuine
    data property, not a bug in this code."""
    rng = np.random.default_rng(0)
    n_players, n_rounds = 200, 100
    rows = []
    for element in range(1, n_players + 1):
        trailing = rng.uniform(0, 0.3)
        for round_ in range(1, n_rounds + 1):
            rows.append(
                {
                    "season": "2022-23", "round": round_, "element": element, "fixture": round_ * 1000 + element,
                    "team": f"Team{element % 10}", "position": "MID", "minutes": 90, "was_home": bool(round_ % 2),
                    "player_trailing_card_rate_3": trailing, "player_trailing_card_rate_5": trailing,
                    "player_trailing_card_rate_10": trailing, "team_trailing_cards_mean_5": 1.0,
                    "games_played_this_season": float(round_ - 1), "cold_start": round_ == 1, "team_cold_start": False,
                    "player_trailing_cards_class_rate_0_5": None, "player_trailing_cards_class_rate_1_5": None,
                    "player_trailing_cards_class_rate_2_5": None, "_chronological_rank": round_,
                }
            )
    table = pl.DataFrame(rows)
    spec = _build_feature_spec(table, NUMERIC_FEATURE_COLUMNS_CARDS)
    from fplai.models.cards import _design_matrix

    X = _design_matrix(table, spec)
    rng2 = np.random.default_rng(1)
    true_beta1 = rng2.normal(scale=0.5, size=spec.n_features)
    true_beta2 = rng2.normal(scale=0.5, size=spec.n_features)
    true_beta1[0], true_beta2[0] = -2.0, -4.0
    offset = np.log(table["minutes"].cast(pl.Float64).to_numpy() / 90.0)
    eta1, eta2 = X @ true_beta1 + offset, X @ true_beta2 + offset
    m = np.maximum(0.0, np.maximum(eta1, eta2))
    denom = np.exp(-m) + np.exp(eta1 - m) + np.exp(eta2 - m)
    p0, p1 = np.exp(-m) / denom, np.exp(eta1 - m) / denom
    u = rng2.random(len(p0))
    y = np.where(u < p0, 0, np.where(u < p0 + p1, 1, 2))
    table = table.with_columns(pl.Series("outcome", y))

    # Regularise ABOVE the true generating scale (beta std 0.5) -> the fit is
    # pulled toward zero relative to truth -> UNDERconfident -> slope > 1.
    # l2=0.01 here is deliberately the SAME value the real-store sweep found
    # itself over-regularised at 200-row-per-fold scale (CardsModelConfig.
    # l2_penalty's own docstring) -- reused here as a known-underconfident
    # reference point, not re-guessed.
    #
    # `calibrate=False` is DELIBERATE here (session s005): this test's whole
    # purpose is diagnosing the RAW multinomial fit's own calibration
    # behaviour under a KNOWN generative process -- it predates, and is
    # unrelated to, the nested NONE/YELLOW isotonic layer session s005 adds
    # (module docstring, "Nested out-of-sample NONE/YELLOW calibration").
    # `walk_forward_validate`'s new default (`calibrate=True`) would apply
    # that layer and correct exactly the deliberate miscalibration this test
    # exists to detect, defeating its own point -- explicitly opting out
    # keeps this test scoring the RAW model, as it always has.
    result = walk_forward_validate(table, min_train_rows=3000, config=CardsModelConfig(l2_penalty=0.01), calibrate=False)
    m0 = result.reliability_for_outcome(0)
    assert m0.calibration_slope > 1.0, (
        f"expected slope > 1.0 (underconfident) when regularising above the true generating scale, "
        f"got {m0.calibration_slope}"
    )


# ---------------------------------------------------------------------------
# 5. Persistence.
# ---------------------------------------------------------------------------


def test_write_cards_pmfs_round_trips_through_write_derived(temp_store, registered_capability):
    table = _synthetic_table()
    params = _fit_from_table(table, config=CardsModelConfig(l2_penalty=0.1), as_of=dt(2026, 1, 1))
    fr = {c: 1.0 for c in NUMERIC_FEATURE_COLUMNS_CARDS} | {"cold_start": False, "team_cold_start": False, "was_home": True, "position": "MID"}
    pmfs = [
        predict_cards_pmf(params, fr, element=1, fixture=1, minute_exposure=[(90.0, 1.0)]),
        predict_cards_pmf(params, fr, element=2, fixture=1, minute_exposure=[(90.0, 1.0)]),
    ]
    calibration = CalibrationReference(reference="test", residual_mean=0.0, residual_std=1.0)
    result = write_cards_pmfs(temp_store, pmfs, season="2022-23", round_=1, valid_at=dt(2026, 1, 1), calibration=calibration)
    assert result.written
    out = temp_store.as_of("derived_player_cards_distribution", datetime.now(UTC) + __import__("datetime").timedelta(days=1))
    assert out["is_modelled"].all()
    for element in (1, 2):
        rows = out.filter(pl.col("element") == element)
        assert rows["probability"].sum() == pytest.approx(1.0)


def test_pmfs_to_rows_rejects_empty_sequence():
    with pytest.raises(CardsModelError):
        pmfs_to_rows([], season="2022-23", round_=1)


def test_read_card_points_reads_live_values(temp_store):
    import json

    payload = {"scoring": {"yellow_cards": -1, "red_cards": -3}}
    df = pl.DataFrame({"payload": [json.dumps(payload)]})
    temp_store.write("game_config", df, valid_at=dt(2026, 1, 1), observed_at=dt(2026, 1, 1), source="test")
    yellow, red = read_card_points(temp_store)
    assert (yellow, red) == (-1, -3)
