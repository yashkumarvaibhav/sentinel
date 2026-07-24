"""A lightweight local model registry index over the trained envelope bundles.

A bundle already carries its provenance fingerprints; the registry is the index
over bundles. It maps a deterministic model version (params + data hash) to the
bundle path and its measured held-out quality, so `make train` records what it
produced and any caller can look up the current model. This is the
shared-box-friendly local stand-in for the full MLflow registry + acceptance
gates, which Phase 9 (MLOps) owns — no server or heavyweight dependency here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from ml.envelopes import EnvelopeModel
from ml.evaluate import EnvelopeMetrics

REGISTRY_VERSION = 1


def model_version(model: EnvelopeModel) -> str:
    """A deterministic version id: same params + same data ⇒ same version."""
    return f"{model.params_fingerprint[:12]}-{model.training_data_hash[:12]}"


def _metrics_entry(metrics: EnvelopeMetrics) -> dict[str, object]:
    return {
        "signal_key": metrics.signal_key,
        "eval_rows": metrics.eval_rows,
        "interval_coverage": metrics.interval_coverage,
        "coverage": {repr(q.quantile): q.coverage for q in metrics.per_quantile},
        "pinball_loss": {repr(q.quantile): q.pinball_loss for q in metrics.per_quantile},
    }


def load_registry(registry_path: Path) -> dict[str, object]:
    """Load the registry index, or an empty document when none exists yet."""
    if not registry_path.is_file():
        return {}
    loaded = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"registry at {registry_path} is not a JSON object")
    return cast("dict[str, object]", loaded)


def register_model(
    registry_path: Path,
    *,
    model: EnvelopeModel,
    metrics: Sequence[EnvelopeMetrics],
    bundle_path: Path,
) -> str:
    """Record a trained bundle's version, provenance and held-out metrics; idempotent."""
    version = model_version(model)
    existing = load_registry(registry_path)
    models_obj = existing.get("models", {})
    models: dict[str, object] = dict(models_obj) if isinstance(models_obj, dict) else {}
    models[version] = {
        "params_fingerprint": model.params_fingerprint,
        "training_data_hash": model.training_data_hash,
        "training_config_fingerprint": model.training_config_fingerprint,
        "detector_config_fingerprint": model.detector_config_fingerprint,
        "quantiles": list(model.quantiles),
        "blind_quantile": model.blind_quantile,
        "signals": sorted(model.signals),
        "bundle_path": str(bundle_path),
        "metrics": [_metrics_entry(item) for item in metrics],
    }
    document = {"version": REGISTRY_VERSION, "models": models}
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return version
