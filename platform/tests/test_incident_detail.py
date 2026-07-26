"""The incident proof is materialized from full evidence, never compact cards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.causal_graph import build_causal_graph
from api.gate import SECRET_HEADER
from api.incident_detail import build_incident_detail, incident_detail_record
from audit.chain import AuditChain
from common.config import TopologyConfig, TopologyService
from common.settings import Settings
from common.storage import IncidentDetailRecord
from contracts import (
    AgentAssessment,
    AgentStatus,
    AgentTrend,
    AuditEventKind,
    CheckOutcome,
    Decision,
    DecisionAction,
    DecompFrame,
    EpisodeStatus,
    EvidenceAxis,
    EvidenceDirection,
    EvidenceItem,
    Incident,
    IncidentSeverity,
    IncidentState,
    RejectedAlternative,
    SymptomEpisode,
    SymptomKind,
    Verdict,
    VerdictClass,
    Verification,
    VerificationCheck,
)
from decision import IncidentOutcome

TICK = datetime(2026, 7, 26, 5, 0, tzinfo=UTC)


def _evidence() -> EvidenceItem:
    return EvidenceItem(
        feature="auth.failure_ratio",
        value=0.74,
        baseline=0.05,
        direction=EvidenceDirection.ABOVE_BASELINE,
        contribution=0.82,
        note="Authentication failures rose against the measured baseline.",
        evidence_refs=("metric-auth-failure",),
    )


def _outcome() -> IncidentOutcome:
    incident = Incident(
        incident_id="incident-proof-1",
        anchor_episode_id="episode-ratio-1",
        opened_ts=TICK - timedelta(minutes=3),
        last_activity_ts=TICK - timedelta(seconds=5),
        state=IncidentState.OPEN,
        severity=IncidentSeverity.HIGH,
        services=("checkout",),
        implicated_services=("payment",),
        kinds=(SymptomKind.RATIO_DEFORM,),
        episode_ids=("episode-ratio-1",),
        business_impact=0.42,
        origin_service="payment",
        origin_confidence=0.91,
        revision=4,
        note="One evidence-backed incident.",
    )
    evidence = _evidence()
    verdict = Verdict(
        verdict_id="verdict-proof-1",
        ts=TICK,
        verdict_class=VerdictClass.ATTACK,
        rule_id="hostile-behaviour",
        confidence=0.91,
        reason="Machine-regular failures survived the event explanation.",
        distribution={
            "EXPECTED_EVENT": 0.02,
            "ATTACK": 0.82,
            "OPERATIONAL_FAULT": 0.04,
            "CODE_CONFIG_FAULT": 0.02,
            "COMBINATION": 0.10,
        },
        corroborating_kinds=(SymptomKind.RATIO_DEFORM,),
        services=("checkout",),
        evidence=(evidence,),
        assessment_ids=("assessment-security-1",),
        rejected_alternatives=(
            RejectedAlternative(
                verdict_class=VerdictClass.EXPECTED_EVENT,
                reason="The auth-failure ratio deformed after the event contribution was removed.",
            ),
        ),
    )
    checks = tuple(
        VerificationCheck(name=name, outcome=CheckOutcome.PASSED, detail=f"{name} passed.")
        for name in (
            "temporal_causality",
            "trace_coverage",
            "dependency_validity",
            "memory_similarity",
        )
    )
    verification = Verification(
        verification_id="verification-proof-1",
        ts=TICK,
        incident_id=incident.incident_id,
        confirmed=True,
        checks=checks,
    )
    decision = Decision(
        decision_id="decision-proof-1",
        ts=TICK,
        incident_id=incident.incident_id,
        action=DecisionAction.AUTO_CONTAIN_THEN_ESCALATE,
        rule_id="contain-verified-attack",
        reason="Contain the verified hostile residual and notify a person.",
        evidence_ts=TICK,
        severity=incident.severity,
        confirmed=True,
        verification_id=verification.verification_id,
        requires_human_approval=False,
        verdict_class=verdict.verdict_class,
        verdict_id=verdict.verdict_id,
        confidence=verdict.confidence,
        target_service="payment",
        escalation_reasons=("A verified attack always pages the incident commander.",),
        guards_applied=("protected-service-target",),
    )
    assessment = AgentAssessment(
        assessment_id="assessment-security-1",
        axis=EvidenceAxis.SECURITY,
        ts=TICK,
        status=AgentStatus.SCORED,
        score=0.82,
        trend=AgentTrend.RISING,
        covered_kinds=(SymptomKind.RATIO_DEFORM,),
        contributing_kinds=(SymptomKind.RATIO_DEFORM,),
        services=("checkout",),
        evidence=(evidence,),
        note="Security evidence was independently scored.",
    )
    return IncidentOutcome(
        incident=incident,
        verdict=verdict,
        verification=verification,
        decision=decision,
        matches=(),
        signature=None,
        assessments=(assessment,),
        fusion=None,
    )


def _episode() -> SymptomEpisode:
    return SymptomEpisode(
        episode_id="episode-ratio-1",
        kind=SymptomKind.RATIO_DEFORM,
        service="checkout",
        signal="auth.failure_ratio",
        status=EpisodeStatus.ACTIVE,
        opened_ts=TICK - timedelta(minutes=3),
        confirmed_ts=TICK - timedelta(minutes=2, seconds=50),
        last_breach_ts=TICK - timedelta(seconds=5),
        peak_score=0.86,
        breach_tick_count=4,
        revision=3,
        opening_symptom_id="symptom-open",
        peak_symptom_id="symptom-peak",
        latest_symptom_id="symptom-latest",
        evidence_refs=("metric-auth-failure",),
    )


def _topology() -> TopologyConfig:
    return TopologyConfig(
        version=1,
        services=(
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


def _frame() -> DecompFrame:
    return DecompFrame(
        frame_id="frame-proof-1",
        observation_id="observation-proof-1",
        ts=TICK - timedelta(minutes=1),
        service="checkout",
        signal="auth.failure_ratio",
        observed=84.0,
        explained_base=5.0,
        explained_event=60.0,
        residual=19.0,
        band_low=60.0,
        band_high=70.0,
        residual_score=0.88,
        context_ids=("cup-final",),
    )


def test_detail_preserves_every_proof_layer_and_provenance() -> None:
    outcome = _outcome()
    graph = build_causal_graph(
        outcome,
        episodes=(_episode(),),
        topology=_topology(),
        dependency_signal_prefix="dependency.",
        honesty="REAL",
    )
    chain = AuditChain()
    applied = chain.append(
        ts=TICK + timedelta(seconds=1),
        kind=AuditEventKind.ACTION_APPLIED,
        actor="sentinel",
        summary="Applied the bounded canary.",
        body={"target": "payment", "rung": "canary-throttle"},
        incident_id=outcome.incident.incident_id,
        decision_id=outcome.decision.decision_id,
        plan_id="plan-proof-1",
        honesty="SIMULATED",
    )

    detail = build_incident_detail(
        outcome,
        episodes=(_episode(),),
        graph=graph,
        decomp_frames=(_frame(),),
        audit_entries=(applied,),
        telemetry_honesty="REAL",
        stimulus_honesty="SIMULATED",
        mode="LIVE",
    )

    assert detail.incident_id == "incident-proof-1"
    assert detail.verdict.status == "decided"
    assert detail.verdict.calibration.status == "insufficient"
    assert {point.verdict_class for point in detail.verdict.distribution} == set(VerdictClass)
    assert detail.rejected_alternatives[0].verdict_class is VerdictClass.EXPECTED_EVENT
    assert detail.decomposition.status == "available"
    assert detail.decomposition.frames[0].residual == pytest.approx(19.0)
    assert detail.evidence[0].axis is EvidenceAxis.SECURITY
    assert detail.causal_graph.origin_service == "payment"
    assert len(detail.verification.checks) == 4
    assert detail.action_log.entries == (applied,)
    assert detail.provenance.telemetry == "REAL"
    assert detail.provenance.stimulus == "SIMULATED"
    assert incident_detail_record(detail).updated_at == outcome.decision.ts


def test_detail_refuses_foreign_frames_and_audit_entries() -> None:
    outcome = _outcome()
    graph = build_causal_graph(
        outcome,
        episodes=(_episode(),),
        topology=_topology(),
        dependency_signal_prefix="dependency.",
        honesty="REAL",
    )
    foreign_frame = _frame().model_copy(update={"service": "not-in-this-incident"})
    with pytest.raises(ValueError, match="incident service"):
        build_incident_detail(
            outcome,
            episodes=(_episode(),),
            graph=graph,
            decomp_frames=(foreign_frame,),
            audit_entries=(),
            telemetry_honesty="REAL",
            stimulus_honesty="SIMULATED",
            mode="LIVE",
        )


class _DetailReader:
    def __init__(self, record: IncidentDetailRecord | None) -> None:
        self.record = record
        self.ids: list[str] = []

    async def get_incident_detail(self, incident_id: str) -> IncidentDetailRecord | None:
        self.ids.append(incident_id)
        return self.record


def _detail_record() -> IncidentDetailRecord:
    outcome = _outcome()
    graph = build_causal_graph(
        outcome,
        episodes=(_episode(),),
        topology=_topology(),
        dependency_signal_prefix="dependency.",
        honesty="REAL",
    )
    detail = build_incident_detail(
        outcome,
        episodes=(_episode(),),
        graph=graph,
        decomp_frames=(_frame(),),
        audit_entries=(),
        telemetry_honesty="REAL",
        stimulus_honesty="SIMULATED",
        mode="LIVE",
    )
    return incident_detail_record(detail)


def test_per_id_route_distinguishes_ready_not_found_and_corrupt_storage() -> None:
    ready_reader = _DetailReader(_detail_record())
    with TestClient(create_app(probes={}, incident_detail_reader=ready_reader)) as client:
        ready = client.get("/api/incidents/incident-proof-1")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["detail"]["incident_id"] == "incident-proof-1"
    assert ready_reader.ids == ["incident-proof-1"]

    with TestClient(create_app(probes={}, incident_detail_reader=_DetailReader(None))) as client:
        missing = client.get("/api/incidents/missing")
    assert missing.status_code == 404
    assert missing.json()["status"] == "not_found"

    corrupt = _detail_record().model_copy(update={"payload": {"incident_id": "broken"}})
    with TestClient(create_app(probes={}, incident_detail_reader=_DetailReader(corrupt))) as client:
        invalid = client.get("/api/incidents/incident-proof-1")
    assert invalid.status_code == 503
    assert invalid.json()["status"] == "degraded"


def test_per_id_route_requires_the_interim_secret_when_configured() -> None:
    config = Settings(SENTINEL_SHARED_SECRET="proof-secret")
    with TestClient(
        create_app(
            config=config,
            probes={},
            incident_detail_reader=_DetailReader(_detail_record()),
        )
    ) as client:
        denied = client.get("/api/incidents/incident-proof-1")
        allowed = client.get(
            "/api/incidents/incident-proof-1",
            headers={SECRET_HEADER: "proof-secret"},
        )
        public_feed = client.get("/api/incidents")

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert public_feed.status_code != 401
