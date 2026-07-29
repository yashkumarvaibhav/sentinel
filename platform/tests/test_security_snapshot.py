"""Security snapshots expose only incident-time evidence and explicit cohorts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from api.security import build_security_snapshot
from contracts import (
    AgentAssessment,
    AgentStatus,
    AgentTrend,
    CheckOutcome,
    Decision,
    DecisionAction,
    EpisodeStatus,
    EvidenceAxis,
    EvidenceDirection,
    EvidenceItem,
    Incident,
    IncidentDecomposition,
    IncidentSeverity,
    IncidentState,
    SecurityCohort,
    SecurityFeature,
    SecurityMeasurement,
    SecurityMeasurementStatus,
    SecurityMeasurementUnit,
    SymptomEpisode,
    SymptomKind,
    Verdict,
    VerdictClass,
    Verification,
    VerificationCheck,
)
from decision import IncidentOutcome

TICK = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)


def _outcome() -> IncidentOutcome:
    incident = Incident(
        incident_id="incident-security-1",
        anchor_episode_id="ratio-path-1",
        opened_ts=TICK - timedelta(minutes=3),
        last_activity_ts=TICK - timedelta(seconds=5),
        state=IncidentState.OPEN,
        severity=IncidentSeverity.HIGH,
        services=("frontend",),
        kinds=(SymptomKind.RATIO_DEFORM,),
        episode_ids=("ratio-path-1",),
        business_impact=0.31,
        origin_service="frontend",
        origin_confidence=0.89,
        revision=2,
        note="A measured behavioral deformation survived the event explanation.",
    )
    evidence = EvidenceItem(
        feature="frontend.path_entropy",
        value=0.84,
        baseline=0.0,
        direction=EvidenceDirection.ABOVE_BASELINE,
        contribution=0.71,
        note="The path-entropy episode remained deformed.",
        evidence_refs=("ratio-path-1",),
    )
    assessment = AgentAssessment(
        assessment_id="assessment-security-1",
        axis=EvidenceAxis.SECURITY,
        ts=TICK,
        status=AgentStatus.SCORED,
        score=0.71,
        trend=AgentTrend.RISING,
        covered_kinds=(SymptomKind.RATIO_DEFORM,),
        contributing_kinds=(SymptomKind.RATIO_DEFORM,),
        services=("frontend",),
        evidence=(evidence,),
        note="Security evidence was independently scored.",
    )
    verdict = Verdict(
        verdict_id="verdict-security-1",
        ts=TICK,
        verdict_class=VerdictClass.ATTACK,
        rule_id="hostile-behaviour",
        confidence=0.84,
        reason="A behavioral deformation survived the explained surge.",
        distribution={
            "EXPECTED_EVENT": 0.04,
            "ATTACK": 0.84,
            "OPERATIONAL_FAULT": 0.04,
            "CODE_CONFIG_FAULT": 0.03,
            "COMBINATION": 0.05,
        },
        corroborating_kinds=(SymptomKind.RATIO_DEFORM,),
        services=("frontend",),
        evidence=(evidence,),
        assessment_ids=(assessment.assessment_id,),
        rejected_alternatives=(),
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
        verification_id="verification-security-1",
        ts=TICK,
        incident_id=incident.incident_id,
        confirmed=True,
        checks=checks,
    )
    decision = Decision(
        decision_id="decision-security-1",
        ts=TICK,
        incident_id=incident.incident_id,
        action=DecisionAction.AUTO_CONTAIN_THEN_ESCALATE,
        rule_id="contain-verified-attack",
        reason="Contain the verified hostile residual.",
        evidence_ts=TICK,
        severity=incident.severity,
        confirmed=True,
        verification_id=verification.verification_id,
        requires_human_approval=False,
        verdict_class=verdict.verdict_class,
        verdict_id=verdict.verdict_id,
        confidence=verdict.confidence,
        target_service="frontend",
        escalation_reasons=("A verified attack pages the incident commander.",),
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
        episode_id="ratio-path-1",
        kind=SymptomKind.RATIO_DEFORM,
        service="frontend",
        signal="path_entropy",
        status=EpisodeStatus.ACTIVE,
        opened_ts=TICK - timedelta(minutes=3),
        confirmed_ts=TICK - timedelta(minutes=2, seconds=50),
        last_breach_ts=TICK - timedelta(seconds=5),
        peak_score=0.84,
        breach_tick_count=4,
        revision=2,
        opening_symptom_id="symptom-path-open",
        peak_symptom_id="symptom-path-peak",
        latest_symptom_id="symptom-path-latest",
        evidence_refs=("trace-public-1",),
    )


def _decomposition() -> IncidentDecomposition:
    return IncidentDecomposition(
        status="insufficient",
        service=None,
        signal=None,
        start=TICK - timedelta(minutes=3),
        end=TICK,
        frames=(),
        truncated=False,
        detail="No decision-window decomposition frames were supplied.",
    )


def _insufficient(feature: SecurityFeature, *, scope: str | None = None) -> SecurityMeasurement:
    return SecurityMeasurement(
        feature=feature,
        status=SecurityMeasurementStatus.INSUFFICIENT,
        scope=scope,
        value=None,
        baseline=None,
        unit=None,
        window_start=None,
        window_end=None,
        window_count=0,
        evidence_refs=(),
        detail=f"No {feature.value.lower()} evidence was supplied.",
    )


def test_unavailable_security_measurements_cannot_masquerade_as_zero() -> None:
    with pytest.raises(ValidationError, match="insufficient"):
        SecurityMeasurement.model_validate(
            _insufficient(SecurityFeature.AUTH_FAILURE_RATIO).model_dump()
            | {
                "value": 0.0,
                "baseline": 0.0,
                "unit": SecurityMeasurementUnit.DEFORMATION_SCORE,
            }
        )

    with pytest.raises(ValidationError, match="unit"):
        SecurityMeasurement(
            feature=SecurityFeature.ASN_REPUTATION,
            status=SecurityMeasurementStatus.MEASURED,
            scope="source-prefix:203.0.113.0/24",
            value=0.2,
            baseline=0.8,
            unit=SecurityMeasurementUnit.ENTROPY,
            window_start=TICK - timedelta(minutes=1),
            window_end=TICK,
            window_count=1,
            evidence_refs=("asn-query-1",),
            detail="An invalid unit must not make ASN evidence look measured.",
        )


def test_materializer_measures_only_referenced_runtime_evidence() -> None:
    snapshot = build_security_snapshot(
        _outcome(),
        episodes=(_episode(),),
        decomposition=_decomposition(),
        honesty="REAL",
    )

    measurements = {measurement.feature: measurement for measurement in snapshot.measurements}
    assert measurements[SecurityFeature.PATH_ENTROPY].status is SecurityMeasurementStatus.MEASURED
    assert measurements[SecurityFeature.PATH_ENTROPY].value == pytest.approx(0.84)
    assert measurements[SecurityFeature.PATH_ENTROPY].unit is (
        SecurityMeasurementUnit.DEFORMATION_SCORE
    )
    assert measurements[SecurityFeature.PATH_ENTROPY].evidence_refs == (
        "ratio-path-1",
        "trace-public-1",
    )
    for feature in (
        SecurityFeature.AUTH_FAILURE_RATIO,
        SecurityFeature.ASN_REPUTATION,
        SecurityFeature.SESSION_ENTROPY,
        SecurityFeature.MACHINE_TIMING,
        SecurityFeature.PROTECTED_COHORT_INTEGRITY,
    ):
        assert measurements[feature].status is SecurityMeasurementStatus.INSUFFICIENT
        assert measurements[feature].value is None
    assert snapshot.suspect_cohorts == ()
    assert [event.episode_id for event in snapshot.timeline] == ["ratio-path-1"]

    foreign = _episode().model_copy(update={"episode_id": "not-assessment-evidence"})
    outcome = replace(
        _outcome(),
        incident=_outcome().incident.model_copy(
            update={
                "anchor_episode_id": foreign.episode_id,
                "episode_ids": (foreign.episode_id,),
            }
        ),
    )
    without_claim = build_security_snapshot(
        outcome,
        episodes=(foreign,),
        decomposition=_decomposition(),
        honesty="REAL",
    )
    assert without_claim.timeline == ()
    assert all(
        measurement.status is SecurityMeasurementStatus.INSUFFICIENT
        for measurement in without_claim.measurements
    )


def test_cohort_rows_require_an_explicit_evidence_identity() -> None:
    cohort = SecurityCohort(
        cohort_id="source-prefix:203.0.113.0/24",
        request_count=97,
        window_start=TICK - timedelta(minutes=1),
        window_end=TICK,
        measurements=(
            _insufficient(
                SecurityFeature.SOURCE_ENTROPY,
                scope="source-prefix:203.0.113.0/24",
            ),
            _insufficient(
                SecurityFeature.AUTH_FAILURE_RATIO,
                scope="source-prefix:203.0.113.0/24",
            ),
            _insufficient(
                SecurityFeature.ASN_REPUTATION,
                scope="source-prefix:203.0.113.0/24",
            ),
            _insufficient(
                SecurityFeature.SESSION_ENTROPY,
                scope="source-prefix:203.0.113.0/24",
            ),
            _insufficient(
                SecurityFeature.MACHINE_TIMING,
                scope="source-prefix:203.0.113.0/24",
            ),
        ),
        evidence_refs=("cohort-query-1",),
        detail="An explicit public-telemetry cohort query named this source prefix.",
    )
    snapshot = build_security_snapshot(
        _outcome(),
        episodes=(_episode(),),
        decomposition=_decomposition(),
        honesty="REAL",
        cohorts=(cohort,),
    )
    assert snapshot.suspect_cohorts == (cohort,)

    with pytest.raises(ValidationError, match="evidence"):
        SecurityCohort.model_validate(cohort.model_dump() | {"evidence_refs": ()})
