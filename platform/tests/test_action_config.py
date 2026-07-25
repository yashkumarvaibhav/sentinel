"""Validation of the committed action-plane configuration."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from action.config import (
    FORCE_DRY_RUN_ENV,
    ActionConfig,
    ActionConfigLoadError,
    load_action_config,
    resolve_dry_run,
)
from contracts import ActuatorKind

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "action.yml"


def _document() -> dict[str, Any]:
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return copy.deepcopy(loaded)


def test_the_committed_configuration_is_valid_and_fingerprinted() -> None:
    configuration = load_action_config(CONFIG_PATH)
    assert configuration.version == 1
    assert len(configuration.fingerprint) == 64
    assert load_action_config(CONFIG_PATH).fingerprint == configuration.fingerprint


def test_the_platform_ships_touching_nothing() -> None:
    """The committed posture is dry-run; going live is a reviewed edit, not a default."""
    assert load_action_config(CONFIG_PATH).execution.dry_run is True


def test_only_the_adapters_that_exist_are_permitted() -> None:
    enabled = load_action_config(CONFIG_PATH).enabled_actuators()
    assert enabled == {ActuatorKind.SIMULATED}


def test_an_unknown_adapter_name_is_refused_by_name() -> None:
    document = _document()
    document["actuators"] = [{"actuator": "TERRAFORM", "enabled": True}]
    with pytest.raises(ValueError, match="unknown actuator 'TERRAFORM'"):
        ActionConfig.model_validate(document)


def test_an_adapter_may_be_configured_only_once() -> None:
    document = _document()
    document["actuators"] = [
        {"actuator": "SIMULATED", "enabled": True},
        {"actuator": "SIMULATED", "enabled": False},
    ]
    with pytest.raises(ValueError, match="at most once"):
        ActionConfig.model_validate(document)


def test_an_unknown_key_is_refused_rather_than_ignored() -> None:
    document = _document()
    document["execution"]["dry_run_really"] = False
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ActionConfig.model_validate(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [("lease_ttl_seconds", 0.0), ("lease_ttl_seconds", -1.0), ("journal_capacity", 0)],
)
def test_execution_settings_are_bounded(field: str, value: float) -> None:
    document = _document()
    document["execution"][field] = value
    with pytest.raises(ValueError):
        ActionConfig.model_validate(document)


def test_a_missing_file_says_so_rather_than_defaulting() -> None:
    with pytest.raises(ActionConfigLoadError, match="required configuration file is missing"):
        load_action_config(CONFIG_PATH.with_name("action-does-not-exist.yml"))


def test_the_fingerprint_moves_when_the_posture_does() -> None:
    document = _document()
    baseline = ActionConfig.model_validate(document).fingerprint
    document["execution"]["dry_run"] = False
    assert ActionConfig.model_validate(document).fingerprint != baseline


def test_the_environment_may_only_tighten_the_committed_posture() -> None:
    document = _document()
    document["execution"]["dry_run"] = False
    live = ActionConfig.model_validate(document)
    committed = load_action_config(CONFIG_PATH)
    assert not resolve_dry_run(live, environ={})
    assert not resolve_dry_run(live, environ={FORCE_DRY_RUN_ENV: "no"})
    assert resolve_dry_run(live, environ={FORCE_DRY_RUN_ENV: "yes"})
    for value in ("0", "false", "", "off"):
        assert resolve_dry_run(committed, environ={FORCE_DRY_RUN_ENV: value}), (
            "nothing in the environment may make a pretending system act"
        )
