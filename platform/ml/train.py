"""Train the context-conditioned quantile envelopes and report held-out quality.

Builds the training dataset (synthetic history + clean development baselines),
trains one envelope per signal on all of it (the shipped bundle), then reports
calibration/sharpness on a temporal holdout trained separately, so the printed
numbers describe generalization rather than fit.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from common.config import load_config
from ml.config import load_envelope_params, load_training_config
from ml.data import build_training_dataset, discover_baseline_captures
from ml.envelopes import save_envelope_model, train_envelopes
from ml.evaluate import holdout_metrics


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ml.train")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--captures-root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    training_config = load_training_config(repo_root / "config" / "ml-training.yml")
    envelope_params = load_envelope_params(repo_root / "config" / "ml-envelopes.yml")
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
    model = train_envelopes(
        dataset.frames,
        params=envelope_params,
        training_data_hash=dataset.manifest.data_hash,
        training_config_fingerprint=dataset.manifest.training_config_fingerprint,
        detector_config_fingerprint=dataset.manifest.detector_config_fingerprint,
    )
    save_envelope_model(args.out.resolve(), model)

    print(f"envelope model written to {args.out.resolve()}", flush=True)
    print(f"  params_fingerprint: {model.params_fingerprint}", flush=True)
    print(f"  training_data_hash: {model.training_data_hash}", flush=True)
    print(
        "  signals: "
        + ", ".join(f"{key}({env.train_rows})" for key, env in sorted(model.signals.items())),
        flush=True,
    )
    print(f"  held-out metrics (fraction {args.holdout_fraction}):", flush=True)
    for metrics in holdout_metrics(
        dataset.frames, params=envelope_params, holdout_fraction=args.holdout_fraction
    ):
        coverage = ", ".join(
            f"p{int(q.quantile * 100)}={q.coverage:.3f}" for q in metrics.per_quantile
        )
        print(
            f"    {metrics.signal_key}: rows={metrics.eval_rows} "
            f"interval_coverage={metrics.interval_coverage:.3f} [{coverage}]",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
