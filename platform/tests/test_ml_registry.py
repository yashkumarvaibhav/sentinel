"""The lightweight local model registry index."""

from __future__ import annotations

from pathlib import Path

from ml.envelopes import EnvelopeModel
from ml.evaluate import EnvelopeMetrics, QuantileMetric
from ml.registry import load_registry, model_version, register_model


def _model(*, params_fp: str = "p" * 64, data_hash: str = "d" * 64) -> EnvelopeModel:
    # A registry entry is built purely from provenance + declared signals, so a
    # trained-signal-free model exercises the index without any LightGBM cost.
    return EnvelopeModel(
        version=1,
        params_fingerprint=params_fp,
        training_data_hash=data_hash,
        training_config_fingerprint="t" * 64,
        detector_config_fingerprint="c" * 64,
        quantiles=(0.1, 0.5, 0.9),
        blind_quantile=0.9,
        signals={},
    )


def _metrics() -> tuple[EnvelopeMetrics, ...]:
    return (
        EnvelopeMetrics(
            signal_key="frontend.request_rate",
            eval_rows=2420,
            per_quantile=(QuantileMetric(quantile=0.9, coverage=0.89, pinball_loss=0.08),),
            interval_coverage=0.77,
        ),
    )


def test_version_is_deterministic_from_fingerprints() -> None:
    model = _model()
    assert model_version(model) == f"{'p' * 12}-{'d' * 12}"
    # different data hash ⇒ different version
    assert model_version(_model(data_hash="e" * 64)) != model_version(model)


def test_register_writes_and_reloads(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    version = register_model(
        registry_path, model=_model(), metrics=_metrics(), bundle_path=tmp_path / "envelopes"
    )
    document = load_registry(registry_path)
    assert document["version"] == 1
    models = document["models"]
    assert isinstance(models, dict)
    entry = models[version]
    assert entry["training_data_hash"] == "d" * 64
    assert entry["signals"] == []
    assert entry["metrics"][0]["interval_coverage"] == 0.77


def test_reregistering_same_version_overwrites_in_place(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    register_model(registry_path, model=_model(), metrics=(), bundle_path=tmp_path / "envelopes")
    register_model(
        registry_path, model=_model(), metrics=_metrics(), bundle_path=tmp_path / "envelopes"
    )
    models = load_registry(registry_path)["models"]
    assert isinstance(models, dict)
    assert len(models) == 1  # same version, updated once


def test_distinct_models_coexist(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    register_model(
        registry_path, model=_model(data_hash="d" * 64), metrics=(), bundle_path=tmp_path / "a"
    )
    register_model(
        registry_path, model=_model(data_hash="e" * 64), metrics=(), bundle_path=tmp_path / "b"
    )
    models = load_registry(registry_path)["models"]
    assert isinstance(models, dict)
    assert len(models) == 2


def test_load_missing_registry_is_empty(tmp_path: Path) -> None:
    assert load_registry(tmp_path / "absent.json") == {}
