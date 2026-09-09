"""Shared reliability machinery — blueprint §7.1 (amended 2026-08-29),
E5's calibration gate. Session `s004`.

Every model in this suite (`fplai.models.minutes`/`bonus`/`cards`/
`defensive_contribution`) independently DUPLICATED the same reliability
math — `log_loss`, `brier`, `reliability_diagram`, `expected_calibration_
error`, `_calibration_slope_intercept`, `CalibrationMetrics` — because each
module's own "no cross-model import" convention (stated in every one of
their docstrings) forbids importing a SIBLING model. That convention is
about not coupling one player-outcome model's fit to another's; it does not
apply here. This module is not a model — it fits nothing, predicts
nothing, touches no store — it is pure, stateless scoring-rule math over
whatever `(y_true, p)` arrays a caller already produced. `scripts/
calibration_report.py` is the one place that is allowed, and needs, to read
every model's output in one report, so THIS is where the duplicated math
converges instead of being re-copied a sixth time. The four existing
per-model copies are left exactly as they are (this task's OWNED PATHS
mark `minutes.py`/`bonus.py`/`cards.py`/`defensive_contribution.py`/
`team_strength.py` READ-ONLY) — this module is additive, not a refactor of
them.

## What is new here, not just consolidated

The five existing per-model implementations compute `log_loss`, `brier`,
`ece`, `calibration_slope`, `calibration_intercept` — but NONE of them
compute a calibration-slope STANDARD ERROR, so none of them can say whether
a departed slope is a real signal or noise from a small out-of-sample cell.
Cards' own module docstring states RED's slope (-0.081) is "no usable
ranking" by eyeballing that it is near zero, not from a computed
confidence interval. This module makes that judgement mechanical and
attacks the boundary explicitly: `slope_usability` returns a 95% CI (via
the Newton-Raphson fit's own Hessian, i.e. the observed Fisher information
at convergence — the same quantity `statsmodels.Logit` would report as
`bse`) and flags `usable=False` when that CI contains 0. This is blueprint
§7.1 item 4's "base-rate-only declaration" made a computed fact rather than
a stated impression.

Also new: `ranked_probability_score` (RPS) — no model in this suite
computed it before this session (grepped, zero hits). §7.1 names it
explicitly ("Brier, log-loss, RPS ... reliability diagrams") and the Phase 2
gate row names it too. Implemented once, generically, for any ordinal K-class
outcome (Epstein 1969's cumulative-probability construction) — proven here,
not assumed, to algebraically reduce to `brier` at K=2
(`tests/test_calibration.py::test_rps_reduces_to_brier_at_k_equals_2`), so a
genuinely-binary model's own "RPS" column is honestly reported as identical
to its Brier column, not fabricated as new information.

Session `s005` (consolidation): `IsotonicCalibrator`/`fit_isotonic_calibrator`
moved here from three independently-drifting copies —
`fplai.models.minutes`/`cards`/`saves` each carried their own, because each
module's "no cross-model import" convention forbids importing a SIBLING
model, and this fitting primitive predates this module's own existence.
Diffed byte-for-byte before moving (module-level vs a function-local
`from scipy.optimize import isotonic_regression` import in `cards.py`'s
copy, and the three models' own `ModelError` subclass raised on a zero-row
call — otherwise identical): no drift, so this is a pure refactor, not a
behaviour change. The zero-row error is now `CalibrationError`, this
module's own convention, in place of `MinutesModelError`/`CardsModelError`/
`SavesModelError` — nothing catches the specific subclass (grepped) and no
test asserts the exact exception type, so this is not observable from any
call site; `_inner_calibration_split` in each model already refuses to hand
this function an empty holdout before it is ever called. The nesting
discipline — fitting this calibrator strictly out-of-sample within each
walk-forward fold — stays OUT of this module and in each model, where the
fold boundary actually lives; this module only fits whatever `(raw_p,
y_true)` pairs it is handed, exactly as it did before the move.

Also new: quantile (equal-frequency) binned ECE/reliability, alongside the
existing equal-width version every sibling module already has. This is the
"next step" `docs/HANDOFF.md` named for bonus's own unresolved slope
question (ECE 0.0002-0.0091 says fine, slope 1.417/2.218 on outcomes 0/3
says not) — an equal-width reliability diagram concentrates almost all of a
rare class's rows into the bottom one or two bins (most predicted
probabilities for a rare outcome sit near 0), so ECE's per-bin weighting is
dominated by the well-calibrated bulk and can be numerically small even
while the FEW higher-probability predictions are badly off. Equal-frequency
(quantile) binning forces every bin to carry the same row count regardless
of where probabilities cluster, so a departure concentrated in the sparse
high-probability tail gets equal voting weight instead of being diluted.
See `docs/wiki/calibration-report.md` §"Settling bonus's slope/ECE
disagreement" for what this actually found on the real bonus predictions.

## What this module does NOT do

Decide anything. `slope_usability`/`decide_outcome_verdict` compute the
INPUTS to a decision (a confidence interval, a beats-baselines boolean) —
the actual "calibrator built, or departure accepted with reasoning" call
§7.1 item 3 requires a human to make (Architect/XL-Coder), stated in prose,
is supplied by the CALLER as `decision_text`/`calibrator_built`, never
inferred from a threshold. A gate is not something a p-value should be
allowed to silently pass or fail on its own recognisance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

import numpy as np
from scipy.optimize import isotonic_regression


class CalibrationError(ValueError):
    """Misuse or a genuine degenerate input this module refuses to guess
    past — zero rows, a PMF row that does not sum to 1, an unknown binning
    mode, an outcome index outside a supplied PMF's own width."""


# ---------------------------------------------------------------------------
# Binary proper scoring rules — same formulas every sibling module's own
# `_log_loss`/`_brier` duplicate; kept private-free (no leading underscore)
# here because THIS module's whole purpose is being imported.
# ---------------------------------------------------------------------------


def log_loss(y_true: Sequence[int], p: Sequence[float], eps: float = 1e-12) -> float:
    n = len(y_true)
    if n == 0:
        raise CalibrationError("log_loss over zero rows is undefined")
    total = 0.0
    for y, prob in zip(y_true, p):
        prob = min(max(prob, eps), 1.0 - eps)
        total += -(y * math.log(prob) + (1 - y) * math.log(1 - prob))
    return total / n


def brier(y_true: Sequence[int], p: Sequence[float]) -> float:
    n = len(y_true)
    if n == 0:
        raise CalibrationError("brier score over zero rows is undefined")
    return sum((prob - y) ** 2 for y, prob in zip(y_true, p)) / n


# ---------------------------------------------------------------------------
# Multiclass / ordinal proper scoring rules.
# ---------------------------------------------------------------------------


def multiclass_log_loss(y_true_class: Sequence[int], p_matrix: Sequence[Sequence[float]], eps: float = 1e-12) -> float:
    n = len(y_true_class)
    if n == 0:
        raise CalibrationError("multiclass log_loss over zero rows is undefined")
    total = 0.0
    for y, row in zip(y_true_class, p_matrix):
        prob = min(max(row[y], eps), 1.0 - eps)
        total += -math.log(prob)
    return total / n


def multiclass_brier(y_true_class: Sequence[int], p_matrix: Sequence[Sequence[float]]) -> float:
    n = len(y_true_class)
    if n == 0:
        raise CalibrationError("multiclass brier over zero rows is undefined")
    total = 0.0
    for y, row in zip(y_true_class, p_matrix):
        for k, prob in enumerate(row):
            onehot = 1.0 if k == y else 0.0
            total += (prob - onehot) ** 2
    return total / n


def ranked_probability_score(y_true_class: Sequence[int], p_matrix: Sequence[Sequence[float]]) -> float:
    """Epstein (1969) RPS for `K` ORDERED categories `0..K-1`. Every model
    in this suite's outcome space is genuinely ordinal (NONE < YELLOW < RED;
    bonus/DC/attacking are literal counts; a scoreline's home/draw/away is
    conventionally ordered away<draw<home for this exact purpose in
    football forecasting) — this function does not check ordinality itself
    (it cannot; ordering is a property of what the caller's column INDICES
    mean, not of the numbers), so a caller applying this to a genuinely
    NOMINAL outcome space would get a number that means nothing. Every call
    site in `scripts/calibration_report.py` states, in a comment, why that
    model's outcome index order is a genuine severity/count/goal-difference
    ordering.

    `RPS_i = (1/(K-1)) * sum_{k=0}^{K-2} (CumP_i,k - CumO_i,k)^2`, averaged
    over rows -- `CumP` is the model's cumulative PMF up to and including
    category `k`, `CumO` is the true cumulative one-hot (`1.0` for every
    `k >= y_true_i`, `0.0` before it). Reduces algebraically to `brier` at
    `K=2` -- proved as a test
    (`tests/test_calibration.py::test_rps_reduces_to_brier_at_k_equals_2`),
    not merely asserted here."""
    n = len(y_true_class)
    if n == 0:
        raise CalibrationError("ranked_probability_score over zero rows is undefined")
    k_classes = len(p_matrix[0]) if n > 0 else 0
    if k_classes < 2:
        raise CalibrationError(f"ranked_probability_score needs >=2 ordered categories, got {k_classes}")
    total = 0.0
    for y, row in zip(y_true_class, p_matrix):
        if len(row) != k_classes:
            raise CalibrationError(f"every p_matrix row must have the same width; got {len(row)} vs {k_classes}")
        row_total = sum(row)
        if abs(row_total - 1.0) > 1e-6:
            raise CalibrationError(f"p_matrix row does not sum to 1.0 (got {row_total}) for y_true={y}")
        cum_p = 0.0
        cum_o = 0.0
        row_score = 0.0
        for k in range(k_classes - 1):
            cum_p += row[k]
            cum_o += 1.0 if k == y else 0.0  # delta, matches cum_p's own accumulation shape
            row_score += (cum_p - cum_o) ** 2
        total += row_score / (k_classes - 1)
    return total / n


# ---------------------------------------------------------------------------
# Isotonic calibrator — a fitting PRIMITIVE, moved here session `s005` from
# three drifting per-model copies (module docstring, "Session s005
# (consolidation)"). Fitting it strictly out-of-sample within a walk-forward
# fold is each MODEL's responsibility, not this module's — no fold/nesting
# logic lives here, only the raw (x, y) -> monotonic-remap fit.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IsotonicCalibrator:
    """A monotonic (non-decreasing) probability remapping, fitted via
    QUANTILE-BINNED PAVA (`scipy.optimize.isotonic_regression` -- itself
    deterministic, O(n), no randomness, CLAUDE.md rule 7) on
    (raw_prediction, binary_outcome) pairs. `x`/`y`: `x` the mean raw
    prediction within each equal-COUNT bin (strictly increasing by
    construction -- bins are contiguous chunks of the sorted order), `y` the
    fitted, monotonic calibrated value for that bin. `apply()` predicts by
    linear interpolation between bins, clipped flat beyond the fitted range
    (no extrapolation past the most extreme calibration-holdout evidence).

    **Binned, not per-point PAVA on raw floats — a real failure mode found
    and fixed in `fplai.models.minutes` (session s004/s005), before this
    class existed here.** With continuous, near-unique raw predictions and
    BINARY outcomes, per-point PAVA pools points only where monotonicity is
    violated; for a small or moderate holdout this degenerates into runs of
    exact 0/1 fitted values at the extremes purely because the two or three
    most extreme points happened to share a label. Binning first
    (equal-count, so a skewed prediction distribution doesn't starve the
    tails of resolution) bounds how few points can back any one fitted
    value -- see `fplai.models.minutes`'s own walk-forward numbers,
    `docs/wiki/model-minutes.md`, for the live-measured before/after."""

    x: tuple[float, ...]
    y: tuple[float, ...]

    def apply(self, p_raw: np.ndarray) -> np.ndarray:
        return np.interp(p_raw, self.x, self.y, left=self.y[0], right=self.y[-1])


def fit_isotonic_calibrator(raw_p: np.ndarray, y_true: np.ndarray, *, n_bins: int = 20) -> IsotonicCalibrator:
    """Fit an `IsotonicCalibrator` on out-of-sample (raw_p, y_true) pairs.
    Caller's responsibility to ensure these are genuinely out-of-sample --
    this function has no notion of folds or leakage, it only fits binned
    PAVA on whatever it is handed (the nesting discipline lives in each
    calling model, e.g. `fplai.models.minutes._inner_calibration_split`, not
    here -- module docstring, "Isotonic calibrator"). See
    `IsotonicCalibrator`'s docstring for why binning (not raw per-point PAVA)
    is the shipped mechanism. `n_bins` is capped at the number of rows
    available -- a calibration-holdout smaller than `n_bins` still fits,
    just at coarser resolution, rather than raising.

    **Jeffreys `Beta(0.5, 0.5)` continuity correction on each bin's fitted
    rate, before the isotonic fit — session s005, found and fixed
    independently in all three of this function's former per-model copies
    before they were consolidated here.** `bin_y[b]` is a plain sample mean
    without it, exactly `0.0` or `1.0` whenever every row in a bin shares an
    outcome -- routine at realistic bin sizes (hundreds of rows), not just a
    small-n corner case. `isotonic_regression` preserves an already-
    monotonic extreme bin unchanged, and `IsotonicCalibrator.apply()`
    extrapolates FLAT beyond the fitted range (`np.interp(..., left=y[0],
    right=y[-1])`), so every raw eval prediction beyond that bin's mean
    inherited the exact 0/1 with full, unearned confidence. Live-verified
    against the real store on `fplai.models.minutes` (114 folds, 86,755 OOS
    rows, post-l2-fix): 2,602 rows (3.0%) calibrated to EXACTLY 0.0, and
    those rows alone accounted for 94.6% of a pooled log-loss regression --
    208 of the 2,602 (8%) were actually `START=1`. `fplai.models.cards`'
    YELLOW calibrator and `fplai.models.saves`' ge3 calibrator each carried
    the identical defect (saves' own population never happened to trigger
    it in practice, but the mechanism is shared and the fix applies
    unconditionally) -- see `docs/wiki/model-minutes.md`,
    `docs/wiki/model-cards.md`, `docs/wiki/model-saves.md` for the exact
    per-model counts.

    The fix is at THIS layer -- the fitted calibration curve itself -- not
    `log_loss`'s `eps` parameter above, which is a numerical-safety guard for
    the METRIC (it stops `log(0)` blowing up a score computation) and makes
    no claim about what a MODEL should ever assert. A model asserting `p=0`
    or `p=1` exactly is asserting infinite certainty; a finite calibration
    holdout, however large, never earns that.

    `bin_y` is smoothed with `(successes + 0.5) / (n + 1)` before fitting --
    the posterior mean of a Bernoulli proportion under a Beta(0.5, 0.5)
    (Jeffreys, non-informative) prior, i.e. one "half event" of continuity
    correction. This is deliberately NOT a flat epsilon clip on the fitted
    output: clipping to a single global floor derived from the fold's
    SMALLEST bin (`eps = 1/(2*bin_w.min())`) was measured to collapse a
    3-row/1-per-bin fixture to a single degenerate point (`eps=0.5` at `n=1`
    forces every fitted value to exactly 0.5, discarding the fit entirely).
    The per-bin Jeffreys correction scales with THAT bin's own weight
    instead of the fold's minimum: a 1-row bin lands at 0.25/0.75 (still
    informative, not degenerate), while a 282-row bin (the smallest real bin
    measured on `fplai.models.minutes` this session) lands at 0.00177 --
    indistinguishable in practice from the flat-eps candidate at realistic
    scale (`1/(2*282)=0.00177`, identical to 4 decimal places), converging
    exactly as bin weight grows. Verified this way, not assumed --
    `tests/test_calibration.py::
    test_isotonic_calibrator_never_saturates_to_exact_zero_or_one` and
    `::test_isotonic_calibrator_smoothing_converges_to_raw_rate_at_scale`."""
    raw_p = np.asarray(raw_p, dtype=np.float64)
    y_true = np.asarray(y_true, dtype=np.float64)
    n = len(raw_p)
    if n == 0:
        raise CalibrationError("fit_isotonic_calibrator called with zero rows")
    order = np.argsort(raw_p, kind="stable")
    x_sorted = raw_p[order]
    y_sorted = y_true[order]

    effective_bins = max(1, min(n_bins, n))
    # Equal-COUNT bins by sorted-order index (not equal-width by value) --
    # contiguous chunks of `x_sorted`, so bin means are non-decreasing by
    # construction and a skewed raw-prediction distribution still gets
    # resolution where the data actually is.
    bin_ids = np.minimum((np.arange(n) * effective_bins) // n, effective_bins - 1)
    bin_x = np.zeros(effective_bins, dtype=np.float64)
    bin_y = np.zeros(effective_bins, dtype=np.float64)
    bin_w = np.zeros(effective_bins, dtype=np.float64)
    for b in range(effective_bins):
        mask = bin_ids == b
        bin_x[b] = x_sorted[mask].mean()
        bin_y[b] = y_sorted[mask].mean()
        bin_w[b] = float(mask.sum())

    # Jeffreys (Beta(0.5, 0.5)) continuity correction, applied to EACH bin's
    # own rate using THAT bin's own weight -- see the docstring above for why
    # this is not a flat epsilon clip. `bin_y[b] * bin_w[b]` recovers the
    # integer success count exactly (bin_y is a plain mean of 0/1 outcomes)
    # without needing to keep the raw counts around separately.
    successes = bin_y * bin_w
    bin_y_smoothed = (successes + 0.5) / (bin_w + 1.0)

    fit = isotonic_regression(bin_y_smoothed, weights=bin_w, increasing=True)
    return IsotonicCalibrator(x=tuple(float(v) for v in bin_x), y=tuple(float(v) for v in fit.x))


# ---------------------------------------------------------------------------
# Reliability diagram + ECE — equal-width (every sibling module's existing
# convention) AND equal-frequency/quantile (new here — module docstring,
# "What is new here").
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReliabilityBin:
    bin_lo: float
    bin_hi: float
    n: int
    mean_predicted: float
    observed_rate: float


def _quantile_edges(p: Sequence[float], n_bins: int) -> list[float]:
    arr = np.asarray(p, dtype=np.float64)
    qs = np.linspace(0.0, 1.0, n_bins + 1)
    edges = sorted(set(float(v) for v in np.quantile(arr, qs)))
    edges[0] = 0.0
    edges[-1] = 1.0
    if len(edges) < 2:
        edges = [0.0, 1.0]
    return edges


def reliability_diagram(
    y_true: Sequence[int], p: Sequence[float], n_bins: int = 10, *, binning: str = "width"
) -> tuple[ReliabilityBin, ...]:
    """`binning="width"` (default, matches every sibling module's own
    implementation): `n_bins` equal-width `[0,1]` bins, some possibly empty
    (skipped). `binning="quantile"`: `n_bins` equal-FREQUENCY bins from the
    predicted `p` values themselves -- module docstring, "What is new
    here". A bin that lands on a run of tied predicted values can end up
    with more or fewer than `n/n_bins` rows (edges are deduplicated, never
    forced apart), which is the honest behaviour, not a bug: forcing exactly
    equal counts through a tie would need to split identically-scored rows
    across two bins arbitrarily."""
    n = len(y_true)
    if n == 0:
        raise CalibrationError("reliability_diagram over zero rows is undefined")
    if binning == "width":
        edges = [i / n_bins for i in range(n_bins + 1)]
    elif binning == "quantile":
        edges = _quantile_edges(p, n_bins)
    else:
        raise CalibrationError(f"binning must be 'width' or 'quantile', got {binning!r}")

    bins: list[ReliabilityBin] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, prob in enumerate(p) if (prob >= lo and (prob < hi or hi >= 1.0))]
        if not idx:
            continue
        preds = [p[i] for i in idx]
        actuals = [y_true[i] for i in idx]
        bins.append(
            ReliabilityBin(
                bin_lo=lo,
                bin_hi=hi,
                n=len(idx),
                mean_predicted=sum(preds) / len(preds),
                observed_rate=sum(actuals) / len(actuals),
            )
        )
    return tuple(bins)


def expected_calibration_error(
    y_true: Sequence[int], p: Sequence[float], n_bins: int = 10, *, binning: str = "width"
) -> float:
    n = len(y_true)
    if n == 0:
        raise CalibrationError("expected_calibration_error over zero rows is undefined")
    bins = reliability_diagram(y_true, p, n_bins=n_bins, binning=binning)
    return sum(b.n * abs(b.mean_predicted - b.observed_rate) for b in bins) / n


# ---------------------------------------------------------------------------
# Calibration slope/intercept (Cox, 1958) — Newton-Raphson logistic
# regression of the true outcome on the predicted log-odds, PLUS the slope's
# standard error from the converged Hessian (new here — module docstring).
# ---------------------------------------------------------------------------


def calibration_slope_intercept_se(y_true: Sequence[int], p: Sequence[float]) -> tuple[float, float, float]:
    """Returns `(slope, intercept, slope_standard_error)`. The point
    estimate is the same Newton-Raphson fit every sibling module's own
    `_calibration_slope_intercept` runs (`y ~ logit(p)`, i.e. Cox's
    calibration slope/intercept); the standard error is the sqrt of the
    (2,2) entry of the inverse of the converged Hessian -- the observed
    Fisher information, the textbook logistic-regression `bse`."""
    n = len(y_true)
    if n == 0:
        raise CalibrationError("calibration_slope_intercept_se over zero rows is undefined")
    p_arr = np.clip(np.asarray(p, dtype=np.float64), 1e-6, 1.0 - 1e-6)
    x = np.log(p_arr / (1.0 - p_arr))
    y = np.asarray(y_true, dtype=np.float64)
    X = np.column_stack([np.ones_like(x), x])
    beta = np.zeros(2, dtype=np.float64)
    hessian = np.eye(2)
    for _ in range(50):
        eta = X @ beta
        pi = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(pi * (1.0 - pi), 1e-10, None)
        grad = X.T @ (y - pi)
        hessian = (X * w[:, None]).T @ X + np.eye(2) * 1e-8
        delta = np.linalg.solve(hessian, grad)
        beta = beta + delta
        if np.max(np.abs(delta)) < 1e-10:
            break
    cov = np.linalg.inv(hessian)
    slope_se = float(math.sqrt(max(cov[1, 1], 0.0)))
    return float(beta[1]), float(beta[0]), slope_se


def calibration_slope_intercept(y_true: Sequence[int], p: Sequence[float]) -> tuple[float, float]:
    """Point estimate only — the same signature every sibling module's own
    `_calibration_slope_intercept` exposes, kept here for a caller that
    only wants the two numbers without the standard error."""
    slope, intercept, _se = calibration_slope_intercept_se(y_true, p)
    return slope, intercept


@dataclass(frozen=True)
class SlopeUsability:
    """Blueprint §7.1 item 4, made mechanical: a slope's 95% Wald CI
    (`slope +/- 1.959963985 * se`, the standard-normal 97.5th percentile).
    `usable=False` when that CI contains 0 -- the model's ranking is
    statistically indistinguishable from carrying no information at all,
    the textbook definition of "no usable ranking" cards' own module
    docstring names by eye for RED. `departs_from_one=True` when the CI
    excludes 1 -- flags item 3's "explicit stated decision" is actually
    required, separately from whether the slope is usable at all (a slope
    can be significantly non-zero AND significantly different from 1 at
    the same time, e.g. bonus's 1.417/2.218)."""

    slope: float
    se: float
    ci_lo: float
    ci_hi: float
    usable: bool
    departs_from_one: bool


_Z_975 = 1.959963985


def slope_usability(y_true: Sequence[int], p: Sequence[float], *, z: float = _Z_975) -> SlopeUsability:
    slope, _intercept, se = calibration_slope_intercept_se(y_true, p)
    ci_lo, ci_hi = slope - z * se, slope + z * se
    usable = not (ci_lo <= 0.0 <= ci_hi)
    departs = not (ci_lo <= 1.0 <= ci_hi)
    return SlopeUsability(slope=slope, se=se, ci_lo=ci_lo, ci_hi=ci_hi, usable=usable, departs_from_one=departs)


# ---------------------------------------------------------------------------
# The per-outcome report §7.1 asks for, in one dataclass.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationMetrics:
    n: int
    log_loss: float
    brier: float
    ece: float
    ece_quantile: float
    calibration_slope: float
    calibration_intercept: float
    slope_se: float
    slope_ci_lo: float
    slope_ci_hi: float
    slope_usable: bool
    slope_departs_from_one: bool
    reliability: tuple[ReliabilityBin, ...]
    reliability_quantile: tuple[ReliabilityBin, ...]


def evaluate_binary_outcome(y_true: Sequence[int], p: Sequence[float], *, n_bins: int = 10) -> CalibrationMetrics:
    """The full §7.1 per-outcome report for one one-vs-rest binary series:
    log-loss, Brier, ECE (both binning modes), calibration slope/intercept
    with its standard error and usability verdict, and both reliability
    diagrams."""
    slope, intercept, se = calibration_slope_intercept_se(y_true, p)
    usability = slope_usability(y_true, p)
    return CalibrationMetrics(
        n=len(y_true),
        log_loss=log_loss(y_true, p),
        brier=brier(y_true, p),
        ece=expected_calibration_error(y_true, p, n_bins=n_bins, binning="width"),
        ece_quantile=expected_calibration_error(y_true, p, n_bins=n_bins, binning="quantile"),
        calibration_slope=slope,
        calibration_intercept=intercept,
        slope_se=se,
        slope_ci_lo=usability.ci_lo,
        slope_ci_hi=usability.ci_hi,
        slope_usable=usability.usable,
        slope_departs_from_one=usability.departs_from_one,
        reliability=reliability_diagram(y_true, p, n_bins=n_bins, binning="width"),
        reliability_quantile=reliability_diagram(y_true, p, n_bins=n_bins, binning="quantile"),
    )


def evaluate_multiclass_outcomes(
    y_true_class: Sequence[int], p_matrix: Sequence[Sequence[float]], outcomes: Sequence[int], *, n_bins: int = 10
) -> dict[int, CalibrationMetrics]:
    """One `evaluate_binary_outcome` per outcome value in `outcomes`, each
    against its own one-vs-rest series `P(class == k)` vs `1{class == k}` —
    the same decomposition `bonus.BonusWalkForwardResult.reliability_for_
    outcome`/`cards.CardsWalkForwardResult.reliability_for_outcome` already
    do per-model; this is the shared, model-agnostic version."""
    out: dict[int, CalibrationMetrics] = {}
    for k in outcomes:
        y_bin = [1 if y == k else 0 for y in y_true_class]
        p_bin = [row[k] for row in p_matrix]
        out[k] = evaluate_binary_outcome(y_bin, p_bin, n_bins=n_bins)
    return out


# ---------------------------------------------------------------------------
# Gate verdict — blueprint §7.1 items 1-4 collapsed into one enum per
# outcome. The DECISION (calibrator built vs accepted) is supplied by the
# caller (module docstring, "What this module does NOT do") — never
# inferred from a threshold.
# ---------------------------------------------------------------------------


class GateVerdict(str, Enum):
    PASS = "PASS"
    PASS_WITH_ACCEPTED_DEPARTURE = "PASS-with-accepted-departure"
    BASE_RATE_ONLY = "BASE-RATE-ONLY"
    FAIL = "FAIL"


@dataclass(frozen=True)
class OutcomeVerdict:
    outcome_label: str
    beats_baselines: bool
    metrics: CalibrationMetrics
    calibrator_built: bool
    decision_text: str
    verdict: GateVerdict


def decide_outcome_verdict(
    *,
    outcome_label: str,
    beats_baselines: bool,
    metrics: CalibrationMetrics,
    calibrator_built: bool,
    decision_text: str,
) -> OutcomeVerdict:
    """§7.1 items 1-4 as one decision, in this fixed precedence order:

    1. Item 4 first — an outcome whose slope carries no usable ranking
       (`metrics.slope_usable is False`) is BASE-RATE-ONLY regardless of
       what the proper scoring rules say (this IS the cards-RED case the
       amendment was written from: excellent log-loss/Brier/ECE, unusable
       slope). Does not count as passing.
    2. Item 1 — an outcome that does not beat both baselines is a FAIL,
       whatever its reliability looks like (a well-calibrated model that is
       simply worse than a naive baseline is not something to ship).
    3. Item 3 — a usable, baseline-beating outcome whose slope
       significantly departs from 1.0 requires the caller to have STATED a
       decision (`decision_text` non-empty); if no calibrator was built,
       the verdict is PASS-with-accepted-departure, never a silent PASS.
    4. Otherwise PASS.

    `decision_text` is REQUIRED (non-empty) whenever `slope_departs_from_
    one` is True AND `slope_usable` is True — §7.1's "silence is not an
    option" enforced structurally, not just by convention.

    The `AND slope_usable` qualifier is not a loophole; it is item 4's own
    precedence, made structural rather than merely documented above. A
    slope whose CI excludes 1.0 while ALSO including 0 (cards' own RED: CI
    `[-0.183, 0.054]` excludes 1, includes 0) is simultaneously "departs
    from 1" AND "not usable" by these two independent tests — real,
    live-verified this session (this exact case crashed the first draft of
    `scripts/calibration_report.py`'s real run against cards, which is
    exactly the kind of bug a fabricated-metrics unit test alone would not
    have caught, and did not, until the real store supplied it). Asking a
    caller for a stated opinion on "how far the slope sits from 1.0" is
    meaningless once the slope has already been shown to carry no usable
    ranking at all -- item 4 governs, unconditionally, and the
    decision_text requirement below only applies once usability is
    established."""
    if not decision_text.strip() and metrics.slope_departs_from_one and metrics.slope_usable:
        raise CalibrationError(
            f"outcome {outcome_label!r}: calibration slope significantly departs from 1.0 "
            f"(CI [{metrics.slope_ci_lo:.3f}, {metrics.slope_ci_hi:.3f}]) but decision_text is empty — "
            "blueprint §7.1 item 3: 'silence is not an option'."
        )
    if not metrics.slope_usable:
        return OutcomeVerdict(
            outcome_label=outcome_label,
            beats_baselines=beats_baselines,
            metrics=metrics,
            calibrator_built=calibrator_built,
            decision_text=decision_text,
            verdict=GateVerdict.BASE_RATE_ONLY,
        )
    if not beats_baselines:
        return OutcomeVerdict(
            outcome_label=outcome_label,
            beats_baselines=beats_baselines,
            metrics=metrics,
            calibrator_built=calibrator_built,
            decision_text=decision_text,
            verdict=GateVerdict.FAIL,
        )
    if metrics.slope_departs_from_one and not calibrator_built:
        return OutcomeVerdict(
            outcome_label=outcome_label,
            beats_baselines=beats_baselines,
            metrics=metrics,
            calibrator_built=calibrator_built,
            decision_text=decision_text,
            verdict=GateVerdict.PASS_WITH_ACCEPTED_DEPARTURE,
        )
    return OutcomeVerdict(
        outcome_label=outcome_label,
        beats_baselines=beats_baselines,
        metrics=metrics,
        calibrator_built=calibrator_built,
        decision_text=decision_text,
        verdict=GateVerdict.PASS,
    )
