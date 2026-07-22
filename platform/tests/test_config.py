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
    assert first.detectors.baseline_update_gate_ratio == 0.25
    assert first.detectors.expected_band_relative_tolerance == 0.1
    assert first.detectors.behavioral_ratios.source_entropy.ratio_ceiling is None
    assert first.detectors.behavioral_ratios.syn_ack.ratio_ceiling == 20.0
    assert first.detectors.behavioral_ratios.interarrival_variation.minimum_points == 5
    assert first.detectors.log_templates.max_clusters == 10_000
    assert first.detectors.log_templates.minimum_template_count == 5
    assert first.detectors.change_point_saturation.pelt_model == "l2"
    assert first.detectors.change_point_saturation.minimum_series_points == 12
    assert first.detectors.change_point_saturation.maximum_headroom_ratio == 0.2
    assert (
        first.detectors.liveness.drop_rules["frontend.request_rate"].minimum_expected_value == 1.5
    )
    assert (
        first.detectors.liveness.silence_rules["frontend.request_rate"].maximum_age_seconds == 120.0
    )
    assert len(first.detectors.edge_degradation.rules) == 4
    assert first.detectors.edge_degradation.rules[0].caller == "frontend"
    assert first.detectors.edge_degradation.rules[0].downstream == "checkout"
    assert first.detectors.edge_degradation.rules[0].minimum_samples == 20
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
        (
            "detector-params.yml",
            lambda document: document.__setitem__("baseline_update_gate_ratio", 0),
            "baseline_update_gate_ratio must be greater than zero",
        ),
        (
            "detector-params.yml",
            lambda document: document["behavioral_ratios"]["source_entropy"].__setitem__(
                "ratio_ceiling", 20.0
            ),
            "source_entropy must not define ratio_ceiling",
        ),
        (
            "detector-params.yml",
            lambda document: document["behavioral_ratios"]["rpc_amplification"].__setitem__(
                "full_score_relative_deformation", 0.5
            ),
            "full_score_relative_deformation",
        ),
        (
            "detector-params.yml",
            lambda document: document["behavioral_ratios"]["crowd_coherence"].__setitem__(
                "minimum_points", 2
            ),
            "minimum_points",
        ),
        (
            "detector-params.yml",
            lambda document: document["log_templates"].__setitem__("similarity_threshold", 0.0),
            "similarity_threshold must be greater than zero",
        ),
        (
            "detector-params.yml",
            lambda document: document["change_point_saturation"].__setitem__(
                "minimum_series_points", 7
            ),
            "at least two minimum-sized segments",
        ),
        (
            "detector-params.yml",
            lambda document: document["change_point_saturation"].__setitem__(
                "full_score_headroom_ratio", 0.2
            ),
            "less than maximum_headroom_ratio",
        ),
        (
            "detector-params.yml",
            lambda document: document["liveness"]["drop_rules"][
                "frontend.request_rate"
            ].__setitem__("trigger_relative_drop", 0.0),
            "trigger_relative_drop must be greater than zero",
        ),
        (
            "detector-params.yml",
            lambda document: document["liveness"]["drop_rules"][
                "frontend.request_rate"
            ].__setitem__("full_score_relative_drop", 0.4),
            "full_score_relative_drop",
        ),
        (
            "detector-params.yml",
            lambda document: document["liveness"]["silence_rules"][
                "frontend.request_rate"
            ].__setitem__("full_score_age_seconds", 60.0),
            "full_score_age_seconds",
        ),
        (
            "detector-params.yml",
            lambda document: document["liveness"]["drop_rules"].__setitem__(
                "missing.request_rate",
                document["liveness"]["drop_rules"]["frontend.request_rate"],
            ),
            "unknown topology service",
        ),
        (
            "detector-params.yml",
            lambda document: document["edge_degradation"]["rules"][0].__setitem__(
                "full_score_relative_latency_rise", 0.25
            ),
            "full_score_relative_latency_rise",
        ),
        (
            "detector-params.yml",
            lambda document: document["edge_degradation"]["rules"][0].__setitem__(
                "error_rate_baseline_floor", 0.0
            ),
            "error_rate_baseline_floor must be greater than zero",
        ),
        (
            "detector-params.yml",
            lambda document: document["edge_degradation"]["rules"][0].__setitem__(
                "downstream", "payment"
            ),
            "is not a configured topology dependency",
        ),
        (
            "detector-params.yml",
            lambda document: document["edge_degradation"]["rules"].append(
                document["edge_degradation"]["rules"][0]
            ),
            "unique caller/downstream pairs",
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
