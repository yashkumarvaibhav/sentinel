"""The business-impact evidence agent and its topology-criticality scaling."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from common.config import load_config
from contracts import AgentStatus, EvidenceAxis, SymptomEpisode, SymptomKind
from decision import AgentEvidenceWindow, BusinessImpactEvidenceAgent, SecurityEvidenceAgent
from decision.config import load_evidence_agents
from tests.factories import EPOCH, symptom_episode

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
AGENTS_PATH = CONFIG_ROOT / "decision-agents.yml"
COVERED = frozenset(SymptomKind)


def _criticality() -> dict[str, str]:
    topology = load_config(CONFIG_ROOT).topology
    return {service.service: service.criticality for service in topology.services}


def _agent(criticality: dict[str, str] | None = None) -> BusinessImpactEvidenceAgent:
    config = load_evidence_agents(AGENTS_PATH)
    return BusinessImpactEvidenceAgent(
        configuration=config.axis(EvidenceAxis.BUSINESS_IMPACT),
        criticality=_criticality() if criticality is None else criticality,
    )


def _window(*episodes: SymptomEpisode, offset_seconds: float = 60.0) -> AgentEvidenceWindow:
    return AgentEvidenceWindow(
        ts=EPOCH + timedelta(seconds=offset_seconds),
        episodes=episodes,
        covered_kinds=COVERED,
    )


def test_a_drop_on_the_critical_edge_outweighs_the_same_drop_on_a_lesser_service() -> None:
    edge = _agent().assess(
        _window(symptom_episode(kind=SymptomKind.DROP, service="frontend", peak_score=0.8))
    )
    inner = _agent().assess(
        _window(symptom_episode(kind=SymptomKind.DROP, service="cart", peak_score=0.8))
    )

    assert edge.score == pytest.approx(0.80 * 0.8 * 1.00)
    assert inner.score == pytest.approx(0.80 * 0.8 * 0.70)
    assert edge.score > inner.score


def test_the_impact_scaling_is_named_in_the_evidence_note() -> None:
    assessment = _agent().assess(
        _window(symptom_episode(kind=SymptomKind.DROP, service="cart", peak_score=0.8))
    )

    assert "impact scaling 0.70" in assessment.evidence[0].note


def test_a_service_missing_from_topology_contributes_nothing() -> None:
    assessment = _agent().assess(
        _window(symptom_episode(kind=SymptomKind.DROP, service="quote", peak_score=1.0))
    )

    assert assessment.score == 0.0
    assert assessment.evidence == ()


def test_business_impact_claims_conversion_but_not_the_attack_ratios() -> None:
    conversion = _agent().assess(
        _window(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                service="frontend",
                signal="conversion_ratio",
                peak_score=0.9,
            )
        )
    )
    entropy = _agent().assess(
        _window(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                service="frontend",
                signal="path_entropy",
                peak_score=0.9,
            )
        )
    )

    assert conversion.score == pytest.approx(0.85 * 0.9)
    assert entropy.score == 0.0


def test_an_internal_saturation_is_not_yet_user_visible_impact() -> None:
    assessment = _agent().assess(
        _window(
            symptom_episode(
                kind=SymptomKind.SATURATION,
                service="email",
                signal="container_memory",
                peak_score=1.0,
            )
        )
    )

    assert assessment.score == 0.0


def test_a_quiet_attack_axis_cannot_shrink_measured_user_impact() -> None:
    config = load_evidence_agents(AGENTS_PATH)
    security = SecurityEvidenceAgent(configuration=config.axis(EvidenceAxis.SECURITY))
    window = _window(symptom_episode(kind=SymptomKind.SILENCE, service="frontend", peak_score=1.0))

    impact = _agent().assess(window)
    hostility = security.assess(window)

    assert hostility.score == 0.0
    assert impact.score == pytest.approx(0.85)
    assert impact.status is AgentStatus.SCORED


def test_an_unknown_criticality_level_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown service criticality"):
        _agent({"frontend": "urgent"})


def test_impact_scoring_needs_the_topology_criticality_index() -> None:
    with pytest.raises(ValueError, match="topology criticality"):
        _agent({})
