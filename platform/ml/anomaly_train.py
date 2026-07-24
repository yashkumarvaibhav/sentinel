"""Fit the multivariate isolation-forest anomaly model and report separation.

Generates the SIMULATED behavioral dataset, fits an Isolation Forest on the normal
region (the shipped bundle), then reports ROC-AUC + the normal/attack score gap on
a held-out split fit separately, so the printed numbers describe generalization.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ml.anomaly import (
    anomaly_holdout_metrics,
    fit_anomaly_model,
    generate_all_behavioral_samples,
    save_anomaly_model,
)
from ml.config import load_anomaly_params

_LABEL_NORMAL = 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ml.anomaly_train")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    params = load_anomaly_params(repo_root / "config" / "ml-anomaly.yml")
    samples = generate_all_behavioral_samples(params)
    normal = [sample.features for sample in samples if sample.label == _LABEL_NORMAL]
    model = fit_anomaly_model(normal, params=params)
    save_anomaly_model(args.out.resolve(), model)

    print(f"anomaly model written to {args.out.resolve()}", flush=True)
    print(f"  params_fingerprint: {model.params_fingerprint}", flush=True)
    print(f"  features: {', '.join(model.feature_names)}", flush=True)
    print(f"  train_rows (normal): {model.train_rows}", flush=True)

    metric = anomaly_holdout_metrics(params, holdout_fraction=args.holdout_fraction)
    print(f"  held-out metrics (fraction {args.holdout_fraction}):", flush=True)
    print(
        f"    rows={metric.eval_rows} roc_auc={metric.roc_auc:.3f} "
        f"normal_score={metric.normal_mean_score:.3f} attack_score={metric.attack_mean_score:.3f} "
        f"separation={metric.separation:+.3f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
