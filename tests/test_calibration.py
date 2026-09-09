"""Tests for `fplai.calibration` — the shared reliability machinery built
for the Phase 2 (E5) calibration report, session `s004`.

Every numeric claim here is checked against a HAND-computed value or a
known mathematical identity, never a self-referential re-implementation of
the function under test (blueprint's "a test that cannot fail is worse
than no test" lesson, CLAUDE.md's index of hard-won lessons, #5).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from fplai.calibration import (
    CalibrationError,
    GateVerdict,
    IsotonicCalibrator,
    brier,
    calibration_slope_intercept_se,
    decide_outcome_verdict,
    evaluate_binary_outcome,
    evaluate_multiclass_outcomes,
    expected_calibration_error,
    fit_isotonic_calibrator,
    log_loss,
    multiclass_brier,
    multiclass_log_loss,
    ranked_probability_score,
    reliability_diagram,
    slope_usability,
)


# ---------------------------------------------------------------------------
# Binary scoring rules — hand-computed.
# ---------------------------------------------------------------------------


def test_log_loss_hand_computed():
    y = [1, 0, 1]
    p = [0.8, 0.3, 0.6]
    expected = -(math.log(0.8) + math.log(0.7) + math.log(0.6)) / 3
    assert log_loss(y, p) == pytest.approx(expected)


def test_brier_hand_computed():
    y = [1, 0, 1]
    p = [0.8, 0.3, 0.6]
    expected = ((0.8 - 1) ** 2 + (0.3 - 0) ** 2 + (0.6 - 1) ** 2) / 3
    assert brier(y, p) == pytest.approx(expected)


def test_log_loss_zero_rows_raises():
    with pytest.raises(CalibrationError):
        log_loss([], [])


def test_brier_zero_rows_raises():
    with pytest.raises(CalibrationError):
        brier([], [])


# ---------------------------------------------------------------------------
# Multiclass scoring rules — hand-computed on a tiny 3-class example.
# ---------------------------------------------------------------------------


def test_multiclass_log_loss_hand_computed():
    y = [0, 2]
    p_matrix = [[0.5, 0.3, 0.2], [0.1, 0.1, 0.8]]
    expected = -(math.log(0.5) + math.log(0.8)) / 2
    assert multiclass_log_loss(y, p_matrix) == pytest.approx(expected)


def test_multiclass_brier_hand_computed():
    y = [0, 2]
    p_matrix = [[0.5, 0.3, 0.2], [0.1, 0.1, 0.8]]
    row0 = (0.5 - 1) ** 2 + (0.3 - 0) ** 2 + (0.2 - 0) ** 2
    row1 = (0.1 - 0) ** 2 + (0.1 - 0) ** 2 + (0.8 - 1) ** 2
    assert multiclass_brier(y, p_matrix) == pytest.approx((row0 + row1) / 2)


# ---------------------------------------------------------------------------
# RPS — hand-computed 3-class example, and the algebraic identity at K=2.
# ---------------------------------------------------------------------------


def test_rps_hand_computed_3_class():
    # One row: true class 1 (middle), predicted [0.2, 0.5, 0.3].
    # CumP = [0.2, 0.7, 1.0]; CumO = [0, 1, 1] (true class 1: 1{k>=1}).
    # RPS = 1/(3-1) * [(0.2-0)^2 + (0.7-1)^2] = 0.5 * (0.04 + 0.09) = 0.065
    y = [1]
    p_matrix = [[0.2, 0.5, 0.3]]
    assert ranked_probability_score(y, p_matrix) == pytest.approx(0.065)


def test_rps_perfect_prediction_is_zero():
    y = [0, 1, 2]
    p_matrix = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    assert ranked_probability_score(y, p_matrix) == pytest.approx(0.0)


def test_rps_reduces_to_brier_at_k_equals_2():
    """The identity this module's docstring claims, checked over many
    random (y, p) draws rather than asserted by inspection alone — a wrong
    RPS implementation (e.g. summing squared per-class differences without
    the cumulative construction) would fail this for at least some draws,
    since at K=2 the two constructions only coincide because of the
    specific cumulative-sum structure, not by definition."""
    rng = np.random.default_rng(0)
    for _ in range(20):
        n = rng.integers(5, 50)
        y = rng.integers(0, 2, size=n).tolist()
        p1 = rng.uniform(0.01, 0.99, size=n)
        p_matrix = [[1.0 - float(v), float(v)] for v in p1]
        rps_value = ranked_probability_score(y, p_matrix)
        brier_value = brier(y, p1.tolist())
        assert rps_value == pytest.approx(brier_value, abs=1e-9)


def test_rps_rejects_row_not_summing_to_one():
    with pytest.raises(CalibrationError):
        ranked_probability_score([0], [[0.5, 0.6]])


def test_rps_rejects_fewer_than_two_categories():
    with pytest.raises(CalibrationError):
        ranked_probability_score([0], [[1.0]])


def test_rps_zero_rows_raises():
    with pytest.raises(CalibrationError):
        ranked_probability_score([], [])


# ---------------------------------------------------------------------------
# Isotonic calibrator — moved here session s005 (consolidation) from three
# independently-drifting per-model copies (`fplai.models.minutes`/`cards`/
# `saves`). These six tests attack the PRIMITIVE itself and were previously
# duplicated verbatim across the three model test files (or, for the four
# unique to `tests/test_minutes.py`, existed nowhere else) — deduplicated to
# one copy each here. Each model's own leakage/nesting proof
# (`test_*_calibrator_fit_directly_on_eval_data_would_leak_and_look_
# suspiciously_good`) and `_inner_calibration_split` tests stay in their own
# model test file unmodified — those attack the FOLD boundary, which is each
# model's own responsibility, not this shared fitting primitive's (module
# docstring, "Isotonic calibrator").
# ---------------------------------------------------------------------------


def test_isotonic_calibrator_is_monotonic_and_deterministic():
    rng = np.random.default_rng(1)
    x = rng.uniform(0, 1, size=200)
    y = (rng.uniform(size=200) < x).astype(np.float64)
    cal1 = fit_isotonic_calibrator(x, y)
    cal2 = fit_isotonic_calibrator(x, y)
    assert cal1.x == cal2.x
    assert cal1.y == cal2.y
    ys = list(cal1.y)
    assert all(ys[i] <= ys[i + 1] + 1e-12 for i in range(len(ys) - 1))


def test_isotonic_calibrator_bins_rather_than_memorising_individual_points():
    """The real failure mode this class was built to avoid, attacked
    directly: fed 200 near-unique continuous raw predictions with binary
    outcomes, an UNBINNED per-point PAVA fit degenerates into runs of exact
    0.0/1.0 at the sorted extremes purely from which of the 1-2 most extreme
    points happened to carry which label (`IsotonicCalibrator`'s own
    docstring). The shipped, BINNED fit must not do this: with `n_bins=20`
    on 200 rows (10 points/bin), no fitted value may be an exact 0.0 or 1.0
    unless every single point in that entire bin shares the same label,
    which a genuinely noisy signal (used below) essentially never
    produces."""
    rng = np.random.default_rng(3)
    x = rng.uniform(0, 1, size=200)
    true_p = np.clip(x, 0.05, 0.95)
    y = (rng.uniform(size=200) < true_p).astype(np.float64)
    cal = fit_isotonic_calibrator(x, y, n_bins=20)
    assert len(cal.x) == 20
    ys = list(cal.y)
    assert all(ys[i] <= ys[i + 1] + 1e-12 for i in range(len(ys) - 1))  # still monotonic
    assert not any(v in (0.0, 1.0) for v in ys), (
        f"a bin collapsed to a degenerate 0.0/1.0 fitted value on genuinely noisy data: {ys}"
    )


def test_isotonic_calibrator_n_bins_is_capped_at_available_rows():
    x = np.array([0.1, 0.5, 0.9])
    y = np.array([0.0, 1.0, 1.0])
    cal = fit_isotonic_calibrator(x, y, n_bins=20)
    assert len(cal.x) == 3  # capped at n, not padded/errored


def test_isotonic_calibrator_apply_clips_flat_outside_fitted_range():
    cal = IsotonicCalibrator(x=(0.2, 0.5, 0.8), y=(0.1, 0.5, 0.9))
    out = cal.apply(np.array([-1.0, 0.2, 0.5, 0.8, 2.0]))
    assert out[0] == pytest.approx(0.1)
    assert out[-1] == pytest.approx(0.9)


def test_isotonic_calibrator_never_saturates_to_exact_zero_or_one():
    """Session s005 regression, live-verified against the real store before
    this fix, independently on all three of this function's former
    per-model call sites: `bin_y` was a plain sample mean, so a bin whose
    calibration-holdout outcomes are ALL 0 (routine, not rare -- hundreds of
    rows/bin measured on the real store, still homogeneous) fitted to an
    exact 0.0, and `IsotonicCalibrator.apply()`'s flat extrapolation then
    handed that exact 0.0 to every raw eval prediction beyond the bin's
    mean. Worst-measured case (`fplai.models.minutes`, 114 folds, 86,755 OOS
    rows, post-l2-fix): 2,602 rows (3.0%) calibrated to EXACTLY 0.0, and
    those rows alone accounted for 94.6% of a pooled log-loss regression --
    208 of the 2,602 (8%) were actually `START=1`. See
    `docs/wiki/model-minutes.md`/`model-cards.md`/`model-saves.md` for each
    model's own counts (saves' own population never happened to trigger it
    in practice -- a real, reportable negative, not a gap in the fix).

    Constructed directly, no store needed: 40 rows, 2 bins of 20, the FIRST
    bin entirely y=0 (raw_p in [0, 0.1)) and the SECOND entirely y=1 (raw_p
    in [0.9, 1.0]) -- both bins are, by construction, maximally saturated
    under the OLD (unsmoothed) mechanism. Hand-verified against a pre-fix
    copy: it produced `cal.y == (0.0, 1.0)` and any `apply()` call outside
    [bin0_mean, bin1_mean] returned an exact 0.0 or 1.0 -- this test is the
    fixture that failed before the Jeffreys smoothing was added, not a
    fixture invented to already pass."""
    rng = np.random.default_rng(20260830)
    n_per_bin = 20
    x_lo = rng.uniform(0.0, 0.1, size=n_per_bin)
    x_hi = rng.uniform(0.9, 1.0, size=n_per_bin)
    raw_p = np.concatenate([x_lo, x_hi])
    y_true = np.concatenate([np.zeros(n_per_bin), np.ones(n_per_bin)])

    cal = fit_isotonic_calibrator(raw_p, y_true, n_bins=2)

    assert not any(v == 0.0 for v in cal.y), f"a bin saturated to exact 0.0: {cal.y}"
    assert not any(v == 1.0 for v in cal.y), f"a bin saturated to exact 1.0: {cal.y}"

    # And the FLAT-EXTRAPOLATION path specifically -- this is what actually
    # bit 2,602 real eval rows: predictions outside the fitted bin-mean
    # range must not come back as exact 0/1 either.
    extrapolated = cal.apply(np.array([-1.0, 2.0]))
    assert not np.any(extrapolated == 0.0), f"extrapolated value saturated to exact 0.0: {extrapolated}"
    assert not np.any(extrapolated == 1.0), f"extrapolated value saturated to exact 1.0: {extrapolated}"


def test_isotonic_calibrator_smoothing_converges_to_raw_rate_at_scale():
    """The Jeffreys correction `(successes + 0.5) / (n + 1)` must not
    distort a well-populated bin's fitted value away from its observed
    rate -- the fix should be invisible at scale and only bite the sparse
    extremes. A single bin of 10,000 rows with a genuine, non-degenerate
    45% success rate should fit within a fraction of a percentage point of
    0.45, not sit noticeably off it (the correction's own scale is
    `~0.5/n`, i.e. ~0.00005 here -- far tighter than the assertion below,
    which leaves headroom for the isotonic step itself)."""
    rng = np.random.default_rng(20260830)
    n = 10_000
    true_rate = 0.45
    raw_p = np.full(n, 0.5)  # single bin (n_bins=1) -- isolates the smoothing math alone
    y_true = (rng.uniform(size=n) < true_rate).astype(np.float64)

    cal = fit_isotonic_calibrator(raw_p, y_true, n_bins=1)

    assert len(cal.y) == 1
    empirical_rate = float(y_true.mean())
    assert cal.y[0] == pytest.approx(empirical_rate, abs=0.005)


def test_fit_isotonic_calibrator_zero_rows_raises():
    """The one intentional, non-behaviour-changing difference from the
    three former per-model copies (module docstring, "Session s005
    (consolidation)"): the exception type on this unreachable-in-practice
    guard is now `CalibrationError`, this module's own convention, rather
    than a model-specific `ModelError` subclass. Nothing catches the old
    subclasses and no pre-existing test asserted the exact type (grepped
    before the move) — this test is new, pinning the new contract, not a
    ported assertion."""
    with pytest.raises(CalibrationError):
        fit_isotonic_calibrator(np.array([]), np.array([]))


# ---------------------------------------------------------------------------
# Reliability diagram — width vs quantile binning.
# ---------------------------------------------------------------------------


def test_reliability_diagram_width_bins_hand_computed():
    y = [0, 0, 1, 1]
    p = [0.05, 0.15, 0.85, 0.95]
    bins = reliability_diagram(y, p, n_bins=10, binning="width")
    # bin [0.0,0.1) has one row (p=0.05, y=0); bin [0.1,0.2) has one row
    # (p=0.15, y=0); bin [0.8,0.9) has one row (p=0.85,y=1); bin [0.9,1.0]
    # has one row (p=0.95,y=1).
    by_lo = {round(b.bin_lo, 2): b for b in bins}
    assert by_lo[0.0].observed_rate == pytest.approx(0.0)
    assert by_lo[0.9].observed_rate == pytest.approx(1.0)


def test_reliability_diagram_quantile_bins_have_bounded_counts():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.0, 1.0, size=100).tolist()
    y = [1 if v > 0.5 else 0 for v in p]
    bins = reliability_diagram(y, p, n_bins=5, binning="quantile")
    # Equal-frequency: no bin should carry more than ~2x the naive n/n_bins
    # share for a uniform, tie-free distribution.
    for b in bins:
        assert b.n <= 40


def test_reliability_diagram_unknown_binning_raises():
    with pytest.raises(CalibrationError):
        reliability_diagram([1, 0], [0.5, 0.5], binning="bogus")


def test_expected_calibration_error_zero_when_perfectly_calibrated_by_bin():
    y = [0, 0, 0, 0, 1, 1, 1, 1]
    p = [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
    assert expected_calibration_error(y, p) == pytest.approx(0.0)


def test_quantile_ece_surfaces_a_tail_departure_that_width_ece_dilutes():
    """A synthetic stand-in for the bonus outcome-0/3 situation this
    session's brief named: 950 well-calibrated near-zero predictions and 50
    badly-miscalibrated near-1.0 predictions that are actually never
    positive. Equal-WIDTH ECE is dominated by the 950-row bulk bin;
    equal-frequency (quantile) ECE gives the 50-row miscalibrated tail its
    own bin(s) and reports a materially larger number."""
    rng = np.random.default_rng(2)
    n_bulk = 950
    p_bulk = rng.uniform(0.0, 0.05, size=n_bulk)
    y_bulk = (rng.uniform(size=n_bulk) < p_bulk).astype(int)
    n_tail = 50
    p_tail = np.full(n_tail, 0.9)
    y_tail = np.zeros(n_tail, dtype=int)  # predicted 0.9, never actually happens
    p = np.concatenate([p_bulk, p_tail]).tolist()
    y = np.concatenate([y_bulk, y_tail]).tolist()

    ece_width = expected_calibration_error(y, p, n_bins=10, binning="width")
    ece_quantile = expected_calibration_error(y, p, n_bins=10, binning="quantile")
    assert ece_quantile > ece_width


# ---------------------------------------------------------------------------
# Calibration slope/intercept + standard error — synthetic known-DGP checks.
# ---------------------------------------------------------------------------


def test_slope_recovers_near_one_on_well_calibrated_synthetic_data():
    rng = np.random.default_rng(3)
    n = 20000
    true_p = rng.uniform(0.05, 0.95, size=n)
    y = (rng.uniform(size=n) < true_p).astype(int).tolist()
    slope, intercept, se = calibration_slope_intercept_se(y, true_p.tolist())
    assert slope == pytest.approx(1.0, abs=0.1)
    assert intercept == pytest.approx(0.0, abs=0.1)
    usability = slope_usability(y, true_p.tolist())
    assert usability.usable is True
    assert usability.departs_from_one is False


def test_slope_usability_flags_unusable_when_prediction_carries_no_signal():
    """`p` fixed at the base rate for every row regardless of `y` — the
    textbook "no usable ranking" case (this is closer to cards' RED than a
    contrived edge case: a rare, roughly base-rate-only predictor)."""
    rng = np.random.default_rng(4)
    n = 300
    base_rate = 0.05
    y = (rng.uniform(size=n) < base_rate).astype(int).tolist()
    # A tiny amount of unrelated jitter around the base rate so the design
    # matrix in the logistic fit is not perfectly singular.
    p = (base_rate + rng.normal(0, 1e-6, size=n)).clip(1e-4, 1 - 1e-4).tolist()
    usability = slope_usability(y, p)
    assert usability.usable is False


def test_slope_usability_break_first_a_deliberately_wrong_se_would_flag_the_opposite():
    """Break-first proof: if the standard error were computed from the
    WRONG (e.g. unregularised, near-singular) Hessian instead of the one
    `calibration_slope_intercept_se` actually uses, the near-base-rate case
    above could spuriously read `usable=True` (a tiny denominator inflating
    the apparent precision) — attacked here by checking the CI width is
    large relative to the slope itself, not merely that `usable` came out
    False by coincidence."""
    rng = np.random.default_rng(4)
    n = 300
    base_rate = 0.05
    y = (rng.uniform(size=n) < base_rate).astype(int).tolist()
    p = (base_rate + rng.normal(0, 1e-6, size=n)).clip(1e-4, 1 - 1e-4).tolist()
    usability = slope_usability(y, p)
    assert usability.se > 0.0
    assert (usability.ci_hi - usability.ci_lo) > 2.0 * abs(usability.slope) or usability.ci_lo <= 0.0 <= usability.ci_hi


# ---------------------------------------------------------------------------
# evaluate_binary_outcome / evaluate_multiclass_outcomes — structural checks.
# ---------------------------------------------------------------------------


def test_evaluate_binary_outcome_is_internally_consistent():
    rng = np.random.default_rng(5)
    n = 500
    true_p = rng.uniform(0.1, 0.9, size=n)
    y = (rng.uniform(size=n) < true_p).astype(int).tolist()
    metrics = evaluate_binary_outcome(y, true_p.tolist())
    assert metrics.n == n
    assert metrics.log_loss == pytest.approx(log_loss(y, true_p.tolist()))
    assert metrics.brier == pytest.approx(brier(y, true_p.tolist()))
    assert 0.0 <= metrics.ece <= 1.0
    assert 0.0 <= metrics.ece_quantile <= 1.0
    assert len(metrics.reliability) > 0
    assert len(metrics.reliability_quantile) > 0


def test_evaluate_multiclass_outcomes_covers_every_requested_outcome():
    y = [0, 1, 2, 1, 0]
    p_matrix = [
        [0.6, 0.3, 0.1],
        [0.2, 0.6, 0.2],
        [0.1, 0.2, 0.7],
        [0.3, 0.5, 0.2],
        [0.7, 0.2, 0.1],
    ]
    out = evaluate_multiclass_outcomes(y, p_matrix, outcomes=(0, 1, 2))
    assert set(out.keys()) == {0, 1, 2}
    for k, metrics in out.items():
        assert metrics.n == len(y)


# ---------------------------------------------------------------------------
# decide_outcome_verdict — precedence order, and the "silence is not an
# option" structural guarantee (§7.1 item 3).
# ---------------------------------------------------------------------------


def _fake_metrics(*, slope_usable: bool, departs: bool) -> "object":
    from fplai.calibration import CalibrationMetrics

    return CalibrationMetrics(
        n=100,
        log_loss=0.5,
        brier=0.2,
        ece=0.01,
        ece_quantile=0.02,
        calibration_slope=1.5 if departs else 1.0,
        calibration_intercept=0.0,
        slope_se=0.1,
        slope_ci_lo=0.0 if not slope_usable else (1.2 if departs else 0.8),
        slope_ci_hi=0.1 if not slope_usable else (1.8 if departs else 1.2),
        slope_usable=slope_usable,
        slope_departs_from_one=departs,
        reliability=(),
        reliability_quantile=(),
    )


def test_decide_outcome_verdict_base_rate_only_takes_precedence_over_beats_baselines():
    metrics = _fake_metrics(slope_usable=False, departs=False)
    verdict = decide_outcome_verdict(
        outcome_label="RED",
        beats_baselines=True,  # even though it beats baselines...
        metrics=metrics,
        calibrator_built=False,
        decision_text="rare outcome, base-rate only per §7.1 item 4",
    )
    assert verdict.verdict == GateVerdict.BASE_RATE_ONLY


def test_decide_outcome_verdict_base_rate_only_takes_precedence_over_departure_text_requirement():
    """The exact cards/RED shape, live-verified this session: a slope CI
    that excludes 1.0 (so `slope_departs_from_one=True`) while ALSO
    including 0 (so `slope_usable=False`) -- an empty `decision_text` must
    NOT raise here, because item 4 (base-rate-only) governs unconditionally
    and a stated opinion on "distance from 1.0" is meaningless for a slope
    already shown to carry no usable ranking. This is a regression test for
    a real bug this session: the first draft of `decide_outcome_verdict`
    raised on cards' real RED outcome (CI [-0.183, 0.054]) because the
    decision-text guard ran before the usability check, contradicting the
    function's own documented precedence order."""
    metrics = _fake_metrics(slope_usable=False, departs=True)
    verdict = decide_outcome_verdict(
        outcome_label="RED", beats_baselines=True, metrics=metrics, calibrator_built=False, decision_text=""
    )
    assert verdict.verdict == GateVerdict.BASE_RATE_ONLY


def test_decide_outcome_verdict_fail_when_baselines_not_beaten():
    metrics = _fake_metrics(slope_usable=True, departs=False)
    verdict = decide_outcome_verdict(
        outcome_label="X", beats_baselines=False, metrics=metrics, calibrator_built=False, decision_text=""
    )
    assert verdict.verdict == GateVerdict.FAIL


def test_decide_outcome_verdict_requires_decision_text_on_departure_break_first():
    """Break-first proof: calling with a departed slope and an EMPTY
    decision_text must raise, not silently default to some verdict —
    §7.1 item 3, "silence is not an option"."""
    metrics = _fake_metrics(slope_usable=True, departs=True)
    with pytest.raises(CalibrationError):
        decide_outcome_verdict(
            outcome_label="Y", beats_baselines=True, metrics=metrics, calibrator_built=False, decision_text="   "
        )


def test_decide_outcome_verdict_pass_with_accepted_departure():
    metrics = _fake_metrics(slope_usable=True, departs=True)
    verdict = decide_outcome_verdict(
        outcome_label="Y",
        beats_baselines=True,
        metrics=metrics,
        calibrator_built=False,
        decision_text="departure accepted, no calibrator built this session — see wiki",
    )
    assert verdict.verdict == GateVerdict.PASS_WITH_ACCEPTED_DEPARTURE


def test_decide_outcome_verdict_plain_pass():
    metrics = _fake_metrics(slope_usable=True, departs=False)
    verdict = decide_outcome_verdict(
        outcome_label="Z", beats_baselines=True, metrics=metrics, calibrator_built=False, decision_text=""
    )
    assert verdict.verdict == GateVerdict.PASS
