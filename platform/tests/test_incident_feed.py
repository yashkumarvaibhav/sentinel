"""The live incident snapshot: evidence in, durable invalidation out."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pytest

from api.causal_graph import build_causal_graph
from api.incidents import IncidentFeedPublisher, build_incident_feed_item
from common.config import TopologyConfig, TopologyService
from common.storage import IncidentGraphRecord, IncidentRecord
from contracts import (
    CheckOutcome,
    Decision,
    DecisionAction,
    EpisodeStatus,
    EvidenceDirection,
    EvidenceItem,
    Incident,
    IncidentConfidenceStatus,
    IncidentSeverity,
    IncidentState,
    SnapshotInvalidation,
    SnapshotResource,
    SymptomEpisode,
    SymptomKind,
    Verdict,
    VerdictClass,
    Verification,
    VerificationCheck,
)
from decision import IncidentOutcome

TICK = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)


def _evidence(feature: str, value: float, baseline: float, contribution: float) -> EvidenceItem:
    return EvidenceItem(
        feature=feature,
        value=value,
        baseline=baseline,
        direction=(
            EvidenceDirection.ABOVE_BASELINE
            if value > baseline
            else EvidenceDirection.BELOW_BASELINE
        ),
        contribution=contribution,
        note=f"{feature} moved against its measured baseline",
        evidence_refs=(f"evidence-{feature}",),
    )


def _outcome(
    *,
    verdict_class: VerdictClass = VerdictClass.ATTACK,
    action: DecisionAction = DecisionAction.ACT,
) -> IncidentOutcome:
    incident = Incident(
        incident_id="incident-live-1",
        anchor_episode_id="episode-1",
        opened_ts=TICK - timedelta(minutes=2),
        last_activity_ts=TICK - timedelta(seconds=5),
        state=IncidentState.OPEN,
        severity=IncidentSeverity.HIGH,
        services=("frontend",),
        kinds=(SymptomKind.RATIO_DEFORM,),
        episode_ids=("episode-1",),
        business_impact=0.42,
        origin_service="frontend",
        origin_confidence=0.93,
        revision=3,
        note="one live incident",
    )
    evidence = (
        _evidence("auth.failure_ratio", 0.74, 0.05, 0.75),
        _evidence("source.entropy", 0.12, 0.91, 0.92),
        _evidence("path.entropy", 0.20, 0.84, 0.61),
    )
    verdict = Verdict(
        verdict_id="verdict-live-1",
        ts=TICK,
        verdict_class=verdict_class,
        rule_id="hostile-behaviour",
        confidence=0.91,
        reason="credential failures concentrated into machine-regular sources",
        distribution={
            "EXPECTED_EVENT": 0.01,
            "ATTACK": 0.91 if verdict_class is VerdictClass.ATTACK else 0.01,
            "OPERATIONAL_FAULT": 0.03,
            "CODE_CONFIG_FAULT": 0.02,
            "COMBINATION": 0.03,
        }
        if verdict_class is VerdictClass.ATTACK
        else {
            "EXPECTED_EVENT": 0.91,
            "ATTACK": 0.01,
            "OPERATIONAL_FAULT": 0.03,
            "CODE_CONFIG_FAULT": 0.02,
            "COMBINATION": 0.03,
        },
        corroborating_kinds=(SymptomKind.RATIO_DEFORM,),
        services=("frontend",),
        evidence=evidence,
        assessment_ids=("assessment-1",),
        rejected_alternatives=(),
    )
    checks = tuple(
        VerificationCheck(name=name, outcome=CheckOutcome.PASSED, detail=f"{name} passed")
        for name in (
            "temporal_causality",
            "trace_coverage",
            "dependency_validity",
            "memory_similarity",
        )
    )
    verification = Verification(
        verification_id="verification-live-1",
        ts=TICK,
        incident_id=incident.incident_id,
        confirmed=True,
        checks=checks,
    )
    decision = Decision(
        decision_id="decision-live-1",
        ts=TICK,
        incident_id=incident.incident_id,
        action=action,
        rule_id="handle-live-incident",
        reason="the verified incident requires a bounded response",
        evidence_ts=TICK,
        severity=incident.severity,
        confirmed=True,
        verification_id=verification.verification_id,
        requires_human_approval=False,
        verdict_class=verdict.verdict_class,
        verdict_id=verdict.verdict_id,
        confidence=verdict.confidence,
        target_service="frontend" if action is DecisionAction.ACT else None,
    )
    return IncidentOutcome(
        incident=incident,
        verdict=verdict,
        verification=verification,
        decision=decision,
        matches=(),
        signature=None,
        assessments=(),
        fusion=None,
    )


def _topology() -> TopologyConfig:
    return TopologyConfig(
        version=1,
        services=(
            TopologyService(
                service="frontend",
                tier="edge",
                criticality="critical",
                dependencies=("checkout",),
            ),
            TopologyService(
                service="checkout",
                tier="application",
                criticality="critical",
                dependencies=("payment",),
            ),
            TopologyService(
                service="payment",
                tier="application",
                criticality="critical",
            ),
        ),
    )


def _causal_outcome() -> IncidentOutcome:
    outcome = _outcome()
    incident = outcome.incident.model_copy(
        update={
            "services": ("checkout",),
            "implicated_services": ("payment",),
            "kinds": (SymptomKind.EDGE_DEGRADED,),
            "episode_ids": ("edge-1",),
            "anchor_episode_id": "edge-1",
            "origin_service": "payment",
            "origin_confidence": 0.88,
        }
    )
    return replace(
        outcome,
        incident=incident,
        decision=outcome.decision.model_copy(update={"target_service": "payment"}),
    )


def _edge_episode() -> SymptomEpisode:
    return SymptomEpisode(
        episode_id="edge-1",
        kind=SymptomKind.EDGE_DEGRADED,
        service="checkout",
        signal="dependency.payment",
        status=EpisodeStatus.ACTIVE,
        opened_ts=TICK - timedelta(minutes=2),
        confirmed_ts=TICK - timedelta(minutes=2),
        last_breach_ts=TICK - timedelta(seconds=5),
        peak_score=0.81,
        breach_tick_count=3,
        revision=2,
        opening_symptom_id="edge-1-open",
        peak_symptom_id="edge-1-peak",
        latest_symptom_id="edge-1-latest",
        evidence_refs=("trace-1",),
    )


def test_causal_graph_reverses_dependency_edges_and_marks_only_measured_propagation() -> None:
    graph = build_causal_graph(
        _causal_outcome(),
        episodes=(_edge_episode(),),
        topology=_topology(),
        dependency_signal_prefix="dependency.",
        honesty="REAL",
    )

    assert graph.incident_id == "incident-live-1"
    assert graph.origin_service == "payment"
    payment = next(node for node in graph.nodes if node.service == "payment")
    checkout = next(node for node in graph.nodes if node.service == "checkout")
    frontend = next(node for node in graph.nodes if node.service == "frontend")
    assert payment.is_origin is True
    assert payment.implicated is True
    assert payment.symptom_heat == 0.0
    assert checkout.symptom_heat == pytest.approx(0.81)
    assert checkout.active_episode_count == 1
    assert frontend.symptom_heat == 0.0
    assert [(edge.source_service, edge.target_service, edge.active) for edge in graph.edges] == [
        ("checkout", "frontend", False),
        ("payment", "checkout", True),
    ]
    assert graph.edges[1].evidence_episode_ids == ("edge-1",)


def test_causal_graph_refuses_missing_or_uncommitted_member_evidence() -> None:
    with pytest.raises(ValueError, match="missing incident episode"):
        build_causal_graph(
            _causal_outcome(),
            episodes=(),
            topology=_topology(),
            dependency_signal_prefix="dependency.",
            honesty="REAL",
        )

    malformed = _edge_episode().model_copy(update={"signal": "dependency.unknown"})
    with pytest.raises(ValueError, match="committed topology"):
        build_causal_graph(
            _causal_outcome(),
            episodes=(malformed,),
            topology=_topology(),
            dependency_signal_prefix="dependency.",
            honesty="REAL",
        )


def test_feed_uses_only_two_strongest_evidence_values_and_refuses_raw_confidence() -> None:
    item = build_incident_feed_item(_outcome(), honesty="REAL")

    assert [evidence.feature for evidence in item.evidence] == [
        "source.entropy",
        "auth.failure_ratio",
    ]
    assert item.confidence.status is IncidentConfidenceStatus.INSUFFICIENT
    assert item.confidence.value is None
    assert "not calibrated" in item.confidence.note.lower()
    assert item.action.effect_status is None
    assert "no actuator outcome" in item.action.detail.lower()
    assert item.muted is False
    assert item.explanation is None


def test_a_calibrated_value_and_event_explanation_must_be_explicit() -> None:
    expected = _outcome(
        verdict_class=VerdictClass.EXPECTED_EVENT,
        action=DecisionAction.SUPPRESS,
    )
    item = build_incident_feed_item(
        expected,
        honesty="REAL",
        calibrated_confidence=0.87,
        calibration_note="Conformal calibrator v1 over 500 normal windows.",
        explained_event="Cup final",
    )

    assert item.confidence.status is IncidentConfidenceStatus.CALIBRATED
    assert item.confidence.value == pytest.approx(0.87)
    assert item.muted is True
    assert item.explanation == "Explained by Cup final"

    with pytest.raises(ValueError, match="EXPECTED_EVENT"):
        build_incident_feed_item(_outcome(), honesty="REAL", explained_event="Cup final")


class _Store:
    def __init__(self, *, changed: bool = True) -> None:
        self.changed = changed
        self.records: list[IncidentRecord] = []
        self.graphs: list[IncidentGraphRecord] = []

    async def put_incident_bundle(
        self,
        record: IncidentRecord,
        graph: IncidentGraphRecord,
    ) -> bool:
        self.records.append(record)
        self.graphs.append(graph)
        return self.changed


class _BrokenStore:
    async def put_incident_bundle(
        self,
        record: IncidentRecord,
        graph: IncidentGraphRecord,
    ) -> NoReturn:
        del record, graph
        raise RuntimeError("database unavailable")


class _Broker:
    def __init__(self) -> None:
        self.events: list[SnapshotInvalidation] = []

    def publish(self, event: SnapshotInvalidation) -> int:
        self.events.append(event)
        return 1


def test_invalidation_happens_only_after_a_durable_new_revision() -> None:
    async def exercise() -> None:
        broker = _Broker()
        store = _Store()
        publisher = IncidentFeedPublisher(store=store, broker=broker)

        result = await publisher.persist(
            _outcome(),
            episodes=(
                SymptomEpisode(
                    episode_id="episode-1",
                    kind=SymptomKind.RATIO_DEFORM,
                    service="frontend",
                    signal="auth.failure_ratio",
                    status=EpisodeStatus.ACTIVE,
                    opened_ts=TICK - timedelta(minutes=2),
                    confirmed_ts=TICK - timedelta(minutes=2),
                    last_breach_ts=TICK - timedelta(seconds=5),
                    peak_score=0.92,
                    breach_tick_count=3,
                    revision=3,
                    opening_symptom_id="episode-1-open",
                    peak_symptom_id="episode-1-peak",
                    latest_symptom_id="episode-1-latest",
                ),
            ),
            topology=_topology(),
            dependency_signal_prefix="dependency.",
            honesty="REAL",
        )

        assert result.persisted is True
        assert len(store.records) == 1
        assert len(store.graphs) == 1
        assert store.records[0].payload["incident_id"] == "incident-live-1"
        assert store.graphs[0].payload["incident_id"] == "incident-live-1"
        assert len(broker.events) == 1
        assert broker.events[0].resources == (SnapshotResource.INCIDENTS,)

        stale_broker = _Broker()
        stale = IncidentFeedPublisher(store=_Store(changed=False), broker=stale_broker)
        stale_result = await stale.persist(
            _causal_outcome(),
            episodes=(_edge_episode(),),
            topology=_topology(),
            dependency_signal_prefix="dependency.",
            honesty="REAL",
        )
        assert stale_result.persisted is False
        assert stale_broker.events == []

        broken_broker = _Broker()
        broken = IncidentFeedPublisher(store=_BrokenStore(), broker=broken_broker)
        with pytest.raises(RuntimeError, match="database unavailable"):
            await broken.persist(
                _causal_outcome(),
                episodes=(_edge_episode(),),
                topology=_topology(),
                dependency_signal_prefix="dependency.",
                honesty="REAL",
            )
        assert broken_broker.events == []

    asyncio.run(exercise())
