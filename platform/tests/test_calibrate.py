"""Conformal calibration + calibration metrics: coverage guarantee, ECE, reliability."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from ml.calibrate import (
    CalibrationError,
    conformal_coverage,
    coverage_calibration_error,
    expected_calibration_error,
    fit_conformal,
    load_conformal_calibrator,
    reliability_curve,
    save_conformal_calibrator,
)


def test_p_value_range_and_monotonicity() -> None:
    calibrator = fit_conformal([float(x) for x in range(10)])
    # a score below every calibration score is the least significant (p == 1)
    assert calibrator.p_value(-1.0) == pytest.approx(1.0)
    # a score above every calibration score is the most significant
    assert calibrator.p_value(100.0) == pytest.approx(1 / 11)
    # higher score (more anomalous) never has a higher p-value
    assert calibrator.p_value(8.0) <= calibrator.p_value(2.0)
    assert 0.0 < calibrator.p_value(5.0) <= 1.0


def test_confidence_is_one_minus_p_value() -> None:
    calibrator = fit_conformal([1.0, 2.0, 3.0])
    assert calibrator.confidence(5.0) == pytest.approx(1.0 - calibrator.p_value(5.0))


def test_marginal_coverage_guarantee_holds() -> None:
    # Exchangeable normal scores: empirical coverage must track the nominal alpha.
    rng = random.Random(20260724)
    scores = [rng.gauss(0.0, 1.0) for _ in range(4000)]
    calibrator = fit_conformal(scores[:2000])
    ((_, empirical),) = conformal_coverage(calibrator, scores[2000:], [0.1])
    assert abs(empirical - 0.1) < 0.03


def test_coverage_calibration_error_is_small_on_exchangeable_data() -> None:
    rng = random.Random(7)
    scores = [rng.gauss(0.0, 1.0) for _ in range(4000)]
    calibrator = fit_conformal(scores[:2000])
    error = coverage_calibration_error(calibrator, scores[2000:], [0.05, 0.1, 0.2])
    assert error < 0.03


def test_threshold_rejects_about_alpha() -> None:
    calibrator = fit_conformal([float(x) for x in range(100)])
    threshold = calibrator.threshold(0.1)
    rejected = sum(score > threshold for score in range(100)) / 100
    assert abs(rejected - 0.1) <= 0.02
    # a score above the threshold is significant at alpha
    assert calibrator.p_value(threshold + 0.5) <= 0.1


def test_threshold_rejects_bad_alpha() -> None:
    calibrator = fit_conformal([1.0, 2.0, 3.0])
    with pytest.raises(CalibrationError):
        calibrator.threshold(1.5)


def test_expected_calibration_error_perfect_is_zero() -> None:
    # each bin's mean confidence equals its empirical accuracy -> ECE 0
    confidences = [0.5, 0.5, 0.5, 0.5]
    outcomes = [1, 0, 1, 0]
    assert expected_calibration_error(confidences, outcomes, n_bins=10) == pytest.approx(0.0)


def test_expected_calibration_error_known_miscalibration() -> None:
    # confident (0.9) but always wrong (0) -> ECE == 0.9
    assert expected_calibration_error([0.9, 0.9], [0, 0], n_bins=10) == pytest.approx(0.9)


def test_reliability_curve_bins() -> None:
    bins = reliability_curve([0.05, 0.15, 0.95], [1, 0, 1], n_bins=10)
    assert len(bins) == 10
    assert bins[0].count == 1
    assert bins[1].count == 1
    assert bins[9].count == 1
    assert bins[0].accuracy == pytest.approx(1.0)


def test_reliability_rejects_out_of_range_confidence() -> None:
    with pytest.raises(CalibrationError):
        reliability_curve([1.5], [1], n_bins=10)


def test_fit_conformal_rejects_empty() -> None:
    with pytest.raises(CalibrationError):
        fit_conformal([])


def test_save_load_round_trip(tmp_path: Path) -> None:
    calibrator = fit_conformal([3.0, 1.0, 2.0])
    path = tmp_path / "cal.json"
    save_conformal_calibrator(path, calibrator)
    loaded = load_conformal_calibrator(path)
    assert loaded.calibration_scores == (1.0, 2.0, 3.0)
    assert loaded.p_value(2.5) == pytest.approx(calibrator.p_value(2.5))


def test_load_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError):
        load_conformal_calibrator(tmp_path / "absent.json")
