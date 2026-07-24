"""Conformally calibrate the anomaly detector's scores and report coverage.

Splits the SIMULATED normal behavior three ways — train the detector, fit the
conformal calibrator, then measure that the coverage guarantee holds on a fresh
normal slice (empirical false-positive rate tracks the nominal alpha). Saves the
calibrator as JSON. This is the calibration proof: the confidence the decision
plane later gates on has a stated, verified error budget.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ml.anomaly import fit_anomaly_model, generate_all_behavioral_samples
from ml.calibrate import (
    conformal_coverage,
    coverage_calibration_error,
    fit_conformal,
    save_conformal_calibrator,
)
from ml.config import load_anomaly_params, load_calibration_params

_LABEL_NORMAL = 0
_LABEL_ATTACK = 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ml.calibrate_train")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    anomaly_params = load_anomaly_params(repo_root / "config" / "ml-anomaly.yml")
    calibration = load_calibration_params(repo_root / "config" / "ml-calibration.yml")

    samples = generate_all_behavioral_samples(anomaly_params)
    normal = [sample.features for sample in samples if sample.label == _LABEL_NORMAL]
    attack = [sample.features for sample in samples if sample.label == _LABEL_ATTACK]

    # Half of normal trains the detector; the rest is split into a conformal
    # calibration set and a held-out set that must exhibit the coverage guarantee.
    train_cut = len(normal) // 2
    detector = fit_anomaly_model(normal[:train_cut], params=anomaly_params)
    remainder = normal[train_cut:]
    calib_cut = int(len(remainder) * calibration.calibration_fraction)
    calibration_scores = [detector.score_one(vector) for vector in remainder[:calib_cut]]
    holdout_scores = [detector.score_one(vector) for vector in remainder[calib_cut:]]

    calibrator = fit_conformal(calibration_scores)
    save_conformal_calibrator(args.out.resolve(), calibrator)

    print(f"conformal calibrator written to {args.out.resolve()}", flush=True)
    print(f"  config_fingerprint: {calibration.fingerprint}", flush=True)
    print(f"  calibration_scores: {len(calibration_scores)}", flush=True)
    print("  held-out normal coverage (nominal alpha -> empirical):", flush=True)
    for alpha, empirical in conformal_coverage(calibrator, holdout_scores, calibration.alphas):
        print(f"    alpha={alpha:.2f} -> {empirical:.3f}", flush=True)
    error = coverage_calibration_error(calibrator, holdout_scores, calibration.alphas)
    print(f"  coverage calibration error: {error:.4f}", flush=True)

    attack_confidence = sum(calibrator.confidence(detector.score_one(v)) for v in attack) / len(
        attack
    )
    print(f"  mean attack confidence: {attack_confidence:.3f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
