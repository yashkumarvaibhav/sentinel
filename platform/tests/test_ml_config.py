"""Validation of the seeded synthetic-history training configuration."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from ml.config import MlConfigLoadError, MlTrainingConfig, load_training_config

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "ml-training.yml"


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
