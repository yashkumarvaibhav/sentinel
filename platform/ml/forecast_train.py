"""Fit the seasonal forecast baselines and report held-out point-forecast quality.

Builds the versioned training dataset (so the bundle traces to the same data hash
as the envelopes), fits one seasonal forecaster per signal on all of the regular
synthetic history (the shipped bundle), then reports MAE/RMSE/NRMSE/bias on a
temporal holdout fit separately, so the printed numbers describe generalization
rather than fit.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from common.config import load_config
from ml.config import load_forecast_params, load_training_config
from ml.data import build_training_dataset, discover_baseline_captures
from ml.evaluate import forecast_holdout_metrics
from ml.forecast import fit_forecasters, save_forecast_model, seasonal_history_frames


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ml.forecast_train")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--captures-root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--holdout-fraction", type=float)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    training_config = load_training_config(repo_root / "config" / "ml-training.yml")
    forecast_params = load_forecast_params(repo_root / "config" / "ml-forecast.yml")
    config = load_config(repo_root / "config")
    capture_roots = (
        discover_baseline_captures(args.captures_root.resolve(), repo_root)
        if args.captures_root is not None
        else ()
    )
    dataset = build_training_dataset(
        repo_root=repo_root,
        training_config=training_config,
        config=config,
        capture_roots=capture_roots,
    )
    model = fit_forecasters(
        seasonal_history_frames(dataset.frames),
        params=forecast_params,
        training_data_hash=dataset.manifest.data_hash,
        training_config_fingerprint=dataset.manifest.training_config_fingerprint,
        detector_config_fingerprint=dataset.manifest.detector_config_fingerprint,
    )
    save_forecast_model(args.out.resolve(), model)

    print(f"forecast model written to {args.out.resolve()}", flush=True)
    print(f"  params_fingerprint: {model.params_fingerprint}", flush=True)
    print(f"  training_data_hash: {model.training_data_hash}", flush=True)
    print(
        "  signals: "
        + ", ".join(
            f"{key}({fc.method},rows={fc.train_rows})" for key, fc in sorted(model.signals.items())
        ),
        flush=True,
    )

    holdout_fraction = (
        args.holdout_fraction
        if args.holdout_fraction is not None
        else forecast_params.holdout_fraction
    )
    print(f"  held-out metrics (fraction {holdout_fraction}):", flush=True)
    for metric in forecast_holdout_metrics(
        dataset.frames, params=forecast_params, holdout_fraction=holdout_fraction
    ):
        print(
            f"    {metric.signal_key}: rows={metric.eval_rows} "
            f"mae={metric.mae:.3f} rmse={metric.rmse:.3f} "
            f"nrmse={metric.nrmse:.3f} bias={metric.bias:+.3f}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
