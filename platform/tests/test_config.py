"""Versioned YAML configuration is strict, cross-checked, and boot-critical."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app
from common.config import ConfigLoadError, load_config
from common.settings import Settings

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


def test_committed_config_loads_with_cross_file_references_and_stable_fingerprint() -> None:
    first = load_config(CONFIG_ROOT)
    second = load_config(CONFIG_ROOT)

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
    assert {service.service for service in first.topology.services} >= {
        "frontend",
        "checkout",
    }
    assert first.events.events[0].honesty == "SIMULATED"
    assert first.events.events[0].enabled is False
    assert first.events.sports_connector is not None
    assert first.events.sports_connector.enabled is False
    assert first.events.sports_connector.provider == "football-data.org"
    assert any(cohort.protected for cohort in first.cohorts.cohorts)
    assert {slo.service for slo in first.slos.slos} <= {
        service.service for service in first.topology.services
    }


def test_config_models_are_immutable() -> None:
    config = load_config(CONFIG_ROOT)

    with pytest.raises(ValidationError, match="frozen"):
        config.detectors.ewma_alpha = 0.9


@pytest.mark.parametrize(
    ("filename", "mutate", "message"),
    [
        (
            "slo.yml",
            lambda document: document["slos"][0].__setitem__("service", "missing-service"),
            "unknown topology service",
        ),
        (
            "event-calendar.yml",
            lambda document: document["events"][0].__setitem__(
                "valid_to", document["events"][0]["valid_from"]
            ),
            "valid_to",
        ),
        (
            "event-calendar.yml",
            lambda document: document["sports_connector"]["expected_delta"].__setitem__(
                "unknown.request_rate", 2.0
            ),
            "unknown topology service",
        ),
        (
            "cohorts.yml",
            lambda document: document["cohorts"][0]["match"].__setitem__("ground_truth", "ATTACK"),
            "ground-truth",
        ),
    ],
)
def test_invalid_or_label_bearing_config_fails_with_file_context(
    tmp_path: Path,
    filename: str,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    config_dir = _copy_config(tmp_path)
    path = config_dir / filename
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigLoadError) as caught:
        load_config(config_dir)

    assert filename in str(caught.value)
    assert message in str(caught.value)


def test_duplicate_yaml_keys_are_rejected_instead_of_silently_overwritten(
    tmp_path: Path,
) -> None:
    config_dir = _copy_config(tmp_path)
    path = config_dir / "topology.yml"
    path.write_text(path.read_text(encoding="utf-8") + "\nversion: 1\n", encoding="utf-8")

    with pytest.raises(ConfigLoadError, match="duplicate key") as caught:
        load_config(config_dir)

    assert "topology.yml" in str(caught.value)


def test_missing_required_config_file_is_named(tmp_path: Path) -> None:
    config_dir = _copy_config(tmp_path)
    (config_dir / "cohorts.yml").unlink()

    with pytest.raises(ConfigLoadError, match=r"cohorts\.yml"):
        load_config(config_dir)


def test_gateway_refuses_to_start_with_invalid_runtime_config(tmp_path: Path) -> None:
    config_dir = _copy_config(tmp_path)
    (config_dir / "detector-params.yml").write_text(
        "version: 1\nfeature_window_seconds: 0\n",
        encoding="utf-8",
    )
    app = create_app(
        config=Settings(SENTINEL_CONFIG_DIR=config_dir),
        probes={},
    )

    with pytest.raises(ConfigLoadError, match=r"detector-params\.yml"), TestClient(app):
        pass


def _copy_config(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG_ROOT, target)
    return target
