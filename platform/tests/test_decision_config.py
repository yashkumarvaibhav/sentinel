"""Validation of the committed evidence-agent configuration."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from contracts import EvidenceAxis, SymptomKind
from decision.config import (
    ChangePressureConfig,
    DecisionConfigLoadError,
    DeploymentLedgerConfig,
    EvidenceAgentsConfig,
    EvidenceAxisConfig,
    EvidenceClaimConfig,
    load_deployment_ledger,
    load_evidence_agents,
)

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
AGENTS_PATH = CONFIG_ROOT / "decision-agents.yml"
LEDGER_PATH = CONFIG_ROOT / "deployments.yml"


def _document() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(AGENTS_PATH.read_text(encoding="utf-8")))


def test_committed_configuration_loads_with_a_stable_fingerprint() -> None:
    first = load_evidence_agents(AGENTS_PATH)
    second = load_evidence_agents(AGENTS_PATH)

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_committed_configuration_covers_all_four_axes() -> None:
    config = load_evidence_agents(AGENTS_PATH)

    assert {axis.axis for axis in config.axes} == set(EvidenceAxis)


def test_security_and_reliability_claim_no_symptom_kind_in_common() -> None:
    config = load_evidence_agents(AGENTS_PATH)
    security = config.axis(EvidenceAxis.SECURITY).claimed_kinds
    reliability = config.axis(EvidenceAxis.RELIABILITY).claimed_kinds

    assert not security & reliability


def test_security_claims_behavioral_signals_but_not_conversion() -> None:
    security = load_evidence_agents(AGENTS_PATH).axis(EvidenceAxis.SECURITY)

    assert security.weight_for(SymptomKind.RATIO_DEFORM, "path_entropy") == 0.85
    assert security.weight_for(SymptomKind.RATIO_DEFORM, "conversion_ratio") is None
    assert security.weight_for(SymptomKind.RESIDUAL_EXCEED, "request_rate") == 0.45
    assert security.weight_for(SymptomKind.SATURATION, "container_memory") is None


def test_reliability_claims_whole_kinds_regardless_of_signal() -> None:
    reliability = load_evidence_agents(AGENTS_PATH).axis(EvidenceAxis.RELIABILITY)

    assert reliability.weight_for(SymptomKind.EDGE_DEGRADED, "dependency.payment") == 0.85
    assert reliability.weight_for(SymptomKind.SATURATION, "container_memory") == 0.80
    assert reliability.weight_for(SymptomKind.RATIO_DEFORM, "path_entropy") is None


def test_requesting_an_unconfigured_axis_fails_closed() -> None:
    document = _document()
    document["axes"] = [
        axis for axis in document["axes"] if axis["axis"] != EvidenceAxis.BUSINESS_IMPACT.value
    ]
    config = EvidenceAgentsConfig.model_validate(document)

    with pytest.raises(DecisionConfigLoadError, match="BUSINESS_IMPACT"):
        config.axis(EvidenceAxis.BUSINESS_IMPACT)


def test_a_duplicated_axis_is_rejected() -> None:
    document = _document()
    document["axes"].append(copy.deepcopy(document["axes"][0]))

    with pytest.raises(ValueError, match="only once"):
        EvidenceAgentsConfig.model_validate(document)


def test_claiming_a_kind_both_whole_and_per_signal_is_rejected() -> None:
    with pytest.raises(ValueError, match="double counting"):
        EvidenceAxisConfig(
            axis=EvidenceAxis.SECURITY,
            minimum_contribution=0.05,
            trend_deadband=0.05,
            claims=(
                EvidenceClaimConfig(kind=SymptomKind.RATIO_DEFORM, weight=0.5),
                EvidenceClaimConfig(
                    kind=SymptomKind.RATIO_DEFORM,
                    weight=0.9,
                    signals=("path_entropy",),
                ),
            ),
        )


def test_the_same_signal_claimed_twice_is_rejected() -> None:
    with pytest.raises(ValueError, match="claimed twice"):
        EvidenceAxisConfig(
            axis=EvidenceAxis.SECURITY,
            minimum_contribution=0.05,
            trend_deadband=0.05,
            claims=(
                EvidenceClaimConfig(
                    kind=SymptomKind.RATIO_DEFORM,
                    weight=0.5,
                    signals=("path_entropy",),
                ),
                EvidenceClaimConfig(
                    kind=SymptomKind.RATIO_DEFORM,
                    weight=0.9,
                    signals=("path_entropy", "source_entropy"),
                ),
            ),
        )


def test_the_same_whole_kind_claimed_twice_is_rejected() -> None:
    with pytest.raises(ValueError, match="claimed twice"):
        EvidenceAxisConfig(
            axis=EvidenceAxis.RELIABILITY,
            minimum_contribution=0.05,
            trend_deadband=0.05,
            claims=(
                EvidenceClaimConfig(kind=SymptomKind.SATURATION, weight=0.5),
                EvidenceClaimConfig(kind=SymptomKind.SATURATION, weight=0.9),
            ),
        )


def test_an_empty_signal_list_is_rejected_rather_than_claiming_everything() -> None:
    with pytest.raises(ValueError, match="claims nothing"):
        EvidenceClaimConfig(kind=SymptomKind.RATIO_DEFORM, weight=0.5, signals=())


def test_duplicate_signals_within_one_claim_are_rejected() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        EvidenceClaimConfig(
            kind=SymptomKind.RATIO_DEFORM,
            weight=0.5,
            signals=("path_entropy", "path_entropy"),
        )


@pytest.mark.parametrize("weight", [0.0, -0.1, 1.1])
def test_a_weight_outside_the_unit_interval_is_rejected(weight: float) -> None:
    with pytest.raises(ValueError):
        EvidenceClaimConfig(kind=SymptomKind.SATURATION, weight=weight)


def test_an_axis_without_claims_is_rejected() -> None:
    with pytest.raises(ValueError):
        EvidenceAxisConfig(
            axis=EvidenceAxis.SECURITY,
            minimum_contribution=0.05,
            trend_deadband=0.05,
            claims=(),
        )


def test_change_config_without_change_pressure_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires change_pressure"):
        EvidenceAxisConfig(
            axis=EvidenceAxis.CHANGE_CONFIG,
            minimum_contribution=0.05,
            trend_deadband=0.05,
            claims=(EvidenceClaimConfig(kind=SymptomKind.DEPLOY_MARKER, weight=0.9),),
        )


def test_an_axis_that_scores_detectors_may_not_configure_change_pressure() -> None:
    with pytest.raises(ValueError, match="must not configure change_pressure"):
        EvidenceAxisConfig(
            axis=EvidenceAxis.SECURITY,
            minimum_contribution=0.05,
            trend_deadband=0.05,
            claims=(EvidenceClaimConfig(kind=SymptomKind.RESIDUAL_EXCEED, weight=0.6),),
            change_pressure=ChangePressureConfig(
                correlation_window_seconds=900.0,
                unrelated_service_factor=0.25,
                kind_weights={"DEPLOY": 0.9},
            ),
        )


def test_business_impact_without_criticality_weights_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires criticality_weights"):
        EvidenceAxisConfig(
            axis=EvidenceAxis.BUSINESS_IMPACT,
            minimum_contribution=0.05,
            trend_deadband=0.05,
            claims=(EvidenceClaimConfig(kind=SymptomKind.DROP, weight=0.8),),
        )


def test_an_unknown_change_kind_weight_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown change kinds"):
        ChangePressureConfig(
            correlation_window_seconds=900.0,
            unrelated_service_factor=0.25,
            kind_weights={"REDEPLOY": 0.9},
        )


@pytest.mark.parametrize("weight", [0.0, 1.5])
def test_a_change_weight_outside_the_unit_interval_is_rejected(weight: float) -> None:
    with pytest.raises(ValueError, match=r"must lie in \(0, 1]"):
        ChangePressureConfig(
            correlation_window_seconds=900.0,
            unrelated_service_factor=0.25,
            kind_weights={"DEPLOY": weight},
        )


def test_the_committed_ledger_loads_with_a_stable_fingerprint() -> None:
    first = load_deployment_ledger(LEDGER_PATH)
    second = load_deployment_ledger(LEDGER_PATH)

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != load_evidence_agents(AGENTS_PATH).fingerprint


def test_a_ledger_naming_an_unknown_service_fails_closed() -> None:
    ledger = load_deployment_ledger(LEDGER_PATH)

    with pytest.raises(DecisionConfigLoadError, match="unknown topology services"):
        ledger.validate_services(frozenset({"frontend"}))


def test_a_ledger_with_duplicate_change_ids_is_rejected() -> None:
    record = {
        "change_id": "deploy-1",
        "kind": "DEPLOY",
        "service": "checkout",
        "ts": datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        "summary": "shipped checkout v2",
        "honesty": "SIMULATED",
    }

    with pytest.raises(ValueError, match="change ids must be unique"):
        DeploymentLedgerConfig.model_validate(
            {"version": 1, "changes": [record, copy.deepcopy(record)]}
        )


def test_an_unknown_configuration_key_is_rejected() -> None:
    document = _document()
    document["axes"][0]["unexpected"] = 1

    with pytest.raises(ValueError):
        EvidenceAgentsConfig.model_validate(document)


def test_a_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(DecisionConfigLoadError, match="missing"):
        load_evidence_agents(tmp_path / "decision-agents.yml")


def test_a_non_mapping_document_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "decision-agents.yml"
    path.write_text("- not a mapping\n", encoding="utf-8")

    with pytest.raises(DecisionConfigLoadError, match="must be a mapping"):
        load_evidence_agents(path)


def test_an_invalid_document_fails_closed(tmp_path: Path) -> None:
    document = _document()
    document["version"] = 2
    path = tmp_path / "decision-agents.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(DecisionConfigLoadError):
        load_evidence_agents(path)


def test_the_configuration_is_immutable() -> None:
    config = load_evidence_agents(AGENTS_PATH)

    with pytest.raises(ValueError):
        config.axes[0].minimum_contribution = 0.9
