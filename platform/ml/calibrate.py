"""Conformal calibration of detector/model scores + calibration metrics.

A raw detector or model score is not a calibrated confidence. Split conformal
prediction fixes that distribution-free: given a calibration set of nonconformity
scores from NORMAL behavior, any new score maps to a p-value with a finite-sample
coverage guarantee — under exchangeability, a normal point's p-value is (super-)
uniform, so flagging ``p <= alpha`` controls the false-positive rate at ``alpha``.
That is the honest confidence the decision plane later gates on: propose, then
verify against a guaranteed error budget rather than a magic percentage.

This module also owns the harness's calibration metrics — the reliability curve
and Expected Calibration Error (ECE) — which measure how well ANY set of
confidences matches outcomes. Everything here is pure stdlib and deterministic.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_BUNDLE_VERSION = 1


class CalibrationError(ValueError):
    """A calibrator could not be fit or a metric could not be computed."""


@dataclass(frozen=True)
class ConformalCalibrator:
    """A split-conformal calibrator over nonconformity scores (higher = more anomalous)."""

    version: int
    calibration_scores: tuple[float, ...]  # sorted ascending

    def p_value(self, score: float) -> float:
        """The conformal p-value of a score: (1 + #{cal >= score}) / (n + 1), in (0, 1]."""
        n = len(self.calibration_scores)
        greater_equal = n - bisect_left(self.calibration_scores, score)
        return (1 + greater_equal) / (n + 1)

    def confidence(self, score: float) -> float:
        """Calibrated confidence that a score is anomalous: 1 - p_value."""
        return 1.0 - self.p_value(score)

    def threshold(self, alpha: float) -> float:
        """The smallest score whose p-value is <= alpha (the conformal decision boundary)."""
        if not 0.0 < alpha < 1.0:
            raise CalibrationError("alpha must be strictly between 0 and 1")
        # p(score) <= alpha  <=>  #{cal >= score} <= alpha*(n+1) - 1. The boundary score is
        # the k-th largest calibration score with k = floor(alpha*(n+1)); above it, reject.
        n = len(self.calibration_scores)
        rank = int(alpha * (n + 1))
        if rank < 1:
            return float("inf")  # no budget to reject anything at this alpha
        return self.calibration_scores[n - rank]


def fit_conformal(scores: Sequence[float]) -> ConformalCalibrator:
    """Fit a conformal calibrator from a calibration set of normal-behavior scores."""
    if len(scores) < 1:
        raise CalibrationError("conformal calibration needs at least one calibration score")
    return ConformalCalibrator(
        version=_BUNDLE_VERSION,
        calibration_scores=tuple(sorted(float(score) for score in scores)),
    )


def conformal_coverage(
    calibrator: ConformalCalibrator, scores: Sequence[float], alphas: Sequence[float]
) -> tuple[tuple[float, float], ...]:
    """Empirical coverage per nominal alpha: the fraction of scores with p_value <= alpha.

    On held-out NORMAL scores this should track the diagonal (empirical ~ nominal) — the
    conformal guarantee. Returned as (nominal_alpha, empirical_coverage) pairs.
    """
    if not scores:
        raise CalibrationError("coverage needs at least one score")
    p_values = [calibrator.p_value(score) for score in scores]
    return tuple((alpha, sum(p <= alpha for p in p_values) / len(p_values)) for alpha in alphas)


def coverage_calibration_error(
    calibrator: ConformalCalibrator, scores: Sequence[float], alphas: Sequence[float]
) -> float:
    """Mean |empirical coverage - nominal alpha| over the alpha grid (0 == perfectly calibrated)."""
    pairs = conformal_coverage(calibrator, scores, alphas)
    return sum(abs(empirical - nominal) for nominal, empirical in pairs) / len(pairs)


@dataclass(frozen=True)
class ReliabilityBin:
    """One confidence bin: its confidence mean, empirical positive rate and count."""

    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float  # fraction of positive outcomes in the bin


def reliability_curve(
    confidences: Sequence[float], outcomes: Sequence[int], *, n_bins: int
) -> tuple[ReliabilityBin, ...]:
    """Bin (confidence, outcome) pairs into equal-width bins over [0, 1]."""
    if len(confidences) != len(outcomes):
        raise CalibrationError("confidences and outcomes must be the same length")
    if not confidences:
        raise CalibrationError("reliability needs at least one prediction")
    if n_bins < 1:
        raise CalibrationError("n_bins must be positive")
    grouped: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for confidence, outcome in zip(confidences, outcomes, strict=True):
        if not 0.0 <= confidence <= 1.0:
            raise CalibrationError("confidences must lie in [0, 1]")
        index = min(int(confidence * n_bins), n_bins - 1)
        grouped[index].append((confidence, outcome))
    bins: list[ReliabilityBin] = []
    for index, pairs in enumerate(grouped):
        count = len(pairs)
        mean_confidence = sum(c for c, _ in pairs) / count if count else 0.0
        accuracy = sum(o for _, o in pairs) / count if count else 0.0
        bins.append(
            ReliabilityBin(
                lower=index / n_bins,
                upper=(index + 1) / n_bins,
                count=count,
                mean_confidence=mean_confidence,
                accuracy=accuracy,
            )
        )
    return tuple(bins)


def expected_calibration_error(
    confidences: Sequence[float], outcomes: Sequence[int], *, n_bins: int
) -> float:
    """ECE: count-weighted mean gap between confidence and empirical accuracy (0 == perfect)."""
    total = len(confidences)
    bins = reliability_curve(confidences, outcomes, n_bins=n_bins)
    return sum((bin_.count / total) * abs(bin_.accuracy - bin_.mean_confidence) for bin_ in bins)


def save_conformal_calibrator(path: Path, calibrator: ConformalCalibrator) -> None:
    """Persist the calibrator as JSON (it is just its sorted calibration scores)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": calibrator.version,
        "calibration_scores": list(calibrator.calibration_scores),
    }
    path.write_text(
        json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_conformal_calibrator(path: Path) -> ConformalCalibrator:
    """Load a saved conformal calibrator."""
    if not path.is_file():
        raise CalibrationError(f"no conformal calibrator at {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    scores = tuple(float(score) for score in document["calibration_scores"])
    if list(scores) != sorted(scores):
        raise CalibrationError("saved calibration scores must be sorted ascending")
    return ConformalCalibrator(version=int(document["version"]), calibration_scores=scores)
