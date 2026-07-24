"""Train the LSTM auth-sequence autoencoder and report reconstruction separation.

Generates the SIMULATED auth sequences, trains the autoencoder on normal sessions
(the shipped bundle), then reports ROC-AUC + the normal/attack reconstruction-error
gap on a held-out split trained separately, so the numbers describe generalization.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ml.autoencoder import (
    autoencoder_holdout_metrics,
    fit_autoencoder,
    generate_all_auth_sequences,
    save_autoencoder_model,
    sequences_to_array,
)
from ml.config import load_autoencoder_params

_LABEL_NORMAL = 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ml.autoencoder_train")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    params = load_autoencoder_params(repo_root / "config" / "ml-autoencoder.yml")
    samples = generate_all_auth_sequences(params)
    normal = [sample for sample in samples if sample.label == _LABEL_NORMAL]
    model = fit_autoencoder(sequences_to_array(normal), params=params)
    save_autoencoder_model(args.out.resolve(), model)

    print(f"autoencoder model written to {args.out.resolve()}", flush=True)
    print(f"  params_fingerprint: {model.params_fingerprint}", flush=True)
    print(f"  features: {', '.join(model.feature_names)}", flush=True)
    print(
        f"  seq_len={model.seq_len} hidden={model.hidden_size} latent={model.latent_size}",
        flush=True,
    )
    print(f"  train_rows (normal): {model.train_rows}", flush=True)

    metric = autoencoder_holdout_metrics(params, holdout_fraction=args.holdout_fraction)
    print(f"  held-out metrics (fraction {args.holdout_fraction}):", flush=True)
    print(
        f"    rows={metric.eval_rows} roc_auc={metric.roc_auc:.3f} "
        f"normal_error={metric.normal_mean_error:.4f} attack_error={metric.attack_mean_error:.4f} "
        f"separation={metric.separation:+.4f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
