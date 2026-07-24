"""Validation of the seeded synthetic-history training configuration."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from ml.config import (
    AnomalyParamsConfig,
    AutoencoderParamsConfig,
    ForecastParamsConfig,
    MlConfigLoadError,
    MlTrainingConfig,
    load_anomaly_params,
    load_autoencoder_params,
    load_forecast_params,
    load_training_config,
)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "ml-training.yml"
FORECAST_PATH = Path(__file__).resolve().parents[2] / "config" / "ml-forecast.yml"
ANOMALY_PATH = Path(__file__).resolve().parents[2] / "config" / "ml-anomaly.yml"
AUTOENCODER_PATH = Path(__file__).resolve().parents[2] / "config" / "ml-autoencoder.yml"


def _document() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")))


def test_committed_config_loads_and_fingerprint_is_deterministic() -> None:
    first = load_training_config(CONFIG_PATH)
    second = load_training_config(CONFIG_PATH)
    assert first.version == 1
    assert first.signals[0].signal == "frontend.request_rate"
    assert first.fingerprint == second.fingerprint


def _load_document(document: dict[str, Any], tmp_path: Path) -> MlTrainingConfig:
    path = tmp_path / "ml-training.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_training_config(path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["unexpected"] = True
    with pytest.raises(MlConfigLoadError):
        _load_document(document, tmp_path)


def test_event_referencing_unknown_signal_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["events"][0]["signal"] = "cart.request_rate"
    with pytest.raises(MlConfigLoadError):
        _load_document(document, tmp_path)


def test_event_window_crossing_midnight_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["events"][0]["start_hour"] = 23.0
    document["events"][0]["duration_hours"] = 3.0
    with pytest.raises(MlConfigLoadError):
        _load_document(document, tmp_path)


def test_event_day_outside_horizon_is_rejected(tmp_path: Path) -> None:
    document = _document()
    document["events"][0]["day_indices"] = [999]
    with pytest.raises(MlConfigLoadError):
        _load_document(document, tmp_path)


def test_duplicate_seeds_are_rejected(tmp_path: Path) -> None:
    document = _document()
    document["history"]["seeds"] = [40100, 40100]
    with pytest.raises(MlConfigLoadError):
        _load_document(document, tmp_path)


def test_tick_seconds_must_divide_a_day(tmp_path: Path) -> None:
    document = _document()
    document["history"]["tick_seconds"] = 700
    with pytest.raises(MlConfigLoadError):
        _load_document(document, tmp_path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(MlConfigLoadError):
        load_training_config(tmp_path / "absent.yml")


def test_fingerprint_changes_when_a_number_changes(tmp_path: Path) -> None:
    baseline = load_training_config(CONFIG_PATH)
    document = copy.deepcopy(_document())
    document["signals"][0]["base_level"] = 9.0
    mutated = _load_document(document, tmp_path)
    assert mutated.fingerprint != baseline.fingerprint


def _forecast_document() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(FORECAST_PATH.read_text(encoding="utf-8")))


def _load_forecast(document: dict[str, Any], tmp_path: Path) -> ForecastParamsConfig:
    path = tmp_path / "ml-forecast.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_forecast_params(path)


def test_forecast_config_loads_and_fingerprint_is_deterministic() -> None:
    first = load_forecast_params(FORECAST_PATH)
    second = load_forecast_params(FORECAST_PATH)
    assert first.version == 1
    assert first.seasonal_periods_days == (1, 7)
    assert first.fingerprint == second.fingerprint


def test_forecast_even_seasonal_smoother_is_rejected(tmp_path: Path) -> None:
    document = _forecast_document()
    document["seasonal_smoother"] = 8
    with pytest.raises(MlConfigLoadError):
        _load_forecast(document, tmp_path)


def test_forecast_unordered_periods_are_rejected(tmp_path: Path) -> None:
    document = _forecast_document()
    document["seasonal_periods_days"] = [7, 1]
    with pytest.raises(MlConfigLoadError):
        _load_forecast(document, tmp_path)


def test_forecast_holdout_fraction_must_be_a_proper_fraction(tmp_path: Path) -> None:
    document = _forecast_document()
    document["holdout_fraction"] = 1.0
    with pytest.raises(MlConfigLoadError):
        _load_forecast(document, tmp_path)


def _anomaly_document() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(ANOMALY_PATH.read_text(encoding="utf-8")))


def _load_anomaly(document: dict[str, Any], tmp_path: Path) -> AnomalyParamsConfig:
    path = tmp_path / "ml-anomaly.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_anomaly_params(path)


def test_anomaly_config_loads_and_fingerprint_is_deterministic() -> None:
    first = load_anomaly_params(ANOMALY_PATH)
    second = load_anomaly_params(ANOMALY_PATH)
    assert first.version == 1
    assert "source_entropy" in first.features
    assert first.fingerprint == second.fingerprint


def test_anomaly_regimes_must_cover_features(tmp_path: Path) -> None:
    document = _anomaly_document()
    document["simulation"]["regimes"].pop()  # drop a regime so coverage breaks
    with pytest.raises(MlConfigLoadError):
        _load_anomaly(document, tmp_path)


def test_anomaly_duplicate_feature_is_rejected(tmp_path: Path) -> None:
    document = _anomaly_document()
    document["features"] = [*document["features"], "source_entropy"]
    with pytest.raises(MlConfigLoadError):
        _load_anomaly(document, tmp_path)


def _autoencoder_document() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(AUTOENCODER_PATH.read_text(encoding="utf-8")))


def _load_autoencoder(document: dict[str, Any], tmp_path: Path) -> AutoencoderParamsConfig:
    path = tmp_path / "ml-autoencoder.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_autoencoder_params(path)


def test_autoencoder_config_loads_and_fingerprint_is_deterministic() -> None:
    first = load_autoencoder_params(AUTOENCODER_PATH)
    second = load_autoencoder_params(AUTOENCODER_PATH)
    assert first.version == 1
    assert first.features == ("inter_arrival", "is_failure", "is_new_account")
    assert first.fingerprint == second.fingerprint


def test_autoencoder_features_are_fixed(tmp_path: Path) -> None:
    document = _autoencoder_document()
    document["features"] = ["inter_arrival", "is_failure", "something_else"]
    with pytest.raises(MlConfigLoadError):
        _load_autoencoder(document, tmp_path)


def test_autoencoder_rejects_non_probability(tmp_path: Path) -> None:
    document = _autoencoder_document()
    document["simulation"]["attack"]["failure_prob"] = 1.5
    with pytest.raises(MlConfigLoadError):
        _load_autoencoder(document, tmp_path)
