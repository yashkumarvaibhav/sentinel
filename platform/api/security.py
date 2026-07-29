"""Materialize and strictly read the evidence-only security snapshot."""

from __future__ import annotations

import json
from typing import Literal, Protocol

from pydantic import ValidationError

from common.storage import IncidentSecurityRecord
from contracts import (
    ActionControlSnapshot,
    AgentStatus,
    EvidenceAxis,
    IncidentDecomposition,
    SecurityCohort,
    SecurityFeature,
    SecurityMeasurement,
    SecurityMeasurementStatus,
    SecurityMeasurementUnit,
    SecurityMitigation,
    SecurityResponse,
    SecuritySnapshot,
    SecurityTimelineEvent,
    SymptomEpisode,
    SymptomKind,
)
from decision import IncidentOutcome

_SIGNAL_FEATURES = {
    "path_entropy": SecurityFeature.PATH_ENTROPY,
    "source_entropy": SecurityFeature.SOURCE_ENTROPY,
    "auth_failure_ratio": SecurityFeature.AUTH_FAILURE_RATIO,
    "interarrival_cv": SecurityFeature.MACHINE_TIMING,
}
_TIMELINE_KINDS = {SymptomKind.RESIDUAL_EXCEED, SymptomKind.RATIO_DEFORM}


class SecuritySnapshotReader(Protocol):
    """The current security record and latest server-held action state."""

    async def latest_security_snapshot(self) -> IncidentSecurityRecord | None: ...

    async def get_action_control(
        self,
        incident_id: str,
        *,
        plan_revision: int | None = None,
    ) -> ActionControlSnapshot | None: ...


class SecuritySnapshotDataError(ValueError):
    """Stored security state contradicted its strict public identity."""


def build_security_snapshot(
    outcome: IncidentOutcome,
    *,
    episodes: tuple[SymptomEpisode, ...],
    decomposition: IncidentDecomposition,
    honesty: Literal["REAL", "SIMULATED"],
    cohorts: tuple[SecurityCohort, ...] = (),
    action_control: ActionControlSnapshot | None = None,
    protected_cohort_integrity: SecurityMeasurement | None = None,
) -> SecuritySnapshot:
    """Project only security-axis evidence referenced at the decision tick."""
    incident = outcome.incident
    supplied = {episode.episode_id: episode for episode in episodes}
    if len(supplied) != len(episodes):
        raise ValueError("security snapshot episode evidence must be unique")
    if set(supplied) != set(incident.episode_ids):
        missing = sorted(set(incident.episode_ids) - set(supplied))
        foreign = sorted(set(supplied) - set(incident.episode_ids))
        raise ValueError(
            "security snapshot requires exact member episodes "
            f"(missing={missing}, foreign={foreign})"
        )
    if action_control is not None and (
        action_control.incident_id != incident.incident_id
        or action_control.created_at != outcome.decision.ts
    ):
        raise ValueError("security action evidence must share the incident decision revision")

    security_assessments = tuple(
        assessment
        for assessment in outcome.assessments
        if assessment.axis is EvidenceAxis.SECURITY and assessment.status is AgentStatus.SCORED
    )
    if len(security_assessments) > 1:
        raise ValueError("one incident decision may carry at most one security assessment")
    claimed_refs = (
        {
            reference
            for evidence in security_assessments[0].evidence
            for reference in evidence.evidence_refs
        }
        if security_assessments
        else set()
    )
    claimed = tuple(
        episode
        for episode in episodes
        if episode.episode_id in claimed_refs and episode.kind in _TIMELINE_KINDS
    )
    timeline = tuple(
        _timeline_event(episode)
        for episode in sorted(claimed, key=lambda item: (item.opened_ts, item.episode_id))
    )
    measurements = (
        *(
            _feature_measurement(feature, claimed)
            for feature in SecurityFeature
            if feature is not SecurityFeature.PROTECTED_COHORT_INTEGRITY
        ),
        _protected_integrity(protected_cohort_integrity),
    )
    return SecuritySnapshot(
        incident_id=incident.incident_id,
        opened_at=incident.opened_ts,
        updated_at=outcome.decision.ts,
        state=incident.state,
        honesty=honesty,
        timeline=timeline,
        measurements=measurements,
        suspect_cohorts=tuple(sorted(cohorts, key=lambda cohort: cohort.cohort_id)),
        decomposition=decomposition,
        mitigation=_mitigation(
            action_control,
            integrity=measurements[-1],
        ),
    )


def security_record(snapshot: SecuritySnapshot) -> IncidentSecurityRecord:
    """Encode the immutable evidence projection under its storage identity."""
    return IncidentSecurityRecord(
        incident_id=snapshot.incident_id,
        updated_at=snapshot.updated_at,
        payload=snapshot.model_dump(mode="json"),
    )


def security_snapshot(
    record: IncidentSecurityRecord | None,
    *,
    action_control: ActionControlSnapshot | None = None,
) -> SecurityResponse:
    """Strictly revalidate stored evidence and overlay only latest action state."""
    if record is None:
        return SecurityResponse(
            status="empty",
            snapshot=None,
            detail="No current incident has an evidence-backed security snapshot.",
        )
    try:
        snapshot = SecuritySnapshot.model_validate_json(
            json.dumps(record.payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
        )
        if snapshot.incident_id != record.incident_id:
            raise ValueError("stored security incident id disagrees with its payload")
        if snapshot.updated_at != record.updated_at:
            raise ValueError("stored security update time disagrees with its payload")
        if snapshot.state.value == "RESOLVED":
            raise ValueError("current security reader returned a resolved incident")
        if action_control is not None and action_control.incident_id != snapshot.incident_id:
            raise ValueError("latest action control belongs to another security incident")
        snapshot = snapshot.model_copy(
            update={
                "mitigation": _mitigation(
                    action_control,
                    integrity=snapshot.mitigation.protected_cohort_integrity,
                )
            }
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise SecuritySnapshotDataError(f"stored security snapshot is invalid: {exc}") from exc
    return SecurityResponse(status="ready", snapshot=snapshot, detail=None)


def unavailable_security_snapshot(*, detail: str) -> SecurityResponse:
    """Fail closed instead of returning a partially trusted security view."""
    return SecurityResponse(
        status="degraded",
        snapshot=None,
        detail=_line(detail),
    )


def _feature_measurement(
    feature: SecurityFeature,
    episodes: tuple[SymptomEpisode, ...],
) -> SecurityMeasurement:
    candidates = tuple(
        episode for episode in episodes if _SIGNAL_FEATURES.get(episode.signal) is feature
    )
    if not candidates:
        return _insufficient(feature)
    selected = sorted(
        candidates,
        key=lambda episode: (-episode.peak_score, episode.opened_ts, episode.episode_id),
    )[0]
    return SecurityMeasurement(
        feature=feature,
        status=SecurityMeasurementStatus.MEASURED,
        scope=selected.service,
        value=selected.peak_score,
        baseline=0.0,
        unit=SecurityMeasurementUnit.DEFORMATION_SCORE,
        window_start=selected.opened_ts,
        window_end=selected.closed_ts or selected.last_breach_ts,
        window_count=selected.breach_tick_count,
        evidence_refs=_refs(selected),
        detail=(
            f"Measured {selected.signal} deformation score from "
            f"{selected.breach_tick_count} verified breach windows; this is not "
            "the raw ratio value."
        ),
    )


def _protected_integrity(
    measurement: SecurityMeasurement | None,
) -> SecurityMeasurement:
    if measurement is None:
        return _insufficient(SecurityFeature.PROTECTED_COHORT_INTEGRITY)
    if measurement.feature is not SecurityFeature.PROTECTED_COHORT_INTEGRITY:
        raise ValueError("protected cohort evidence uses the wrong security feature")
    return measurement


def _insufficient(feature: SecurityFeature) -> SecurityMeasurement:
    labels = {
        SecurityFeature.PATH_ENTROPY: "path-entropy",
        SecurityFeature.SOURCE_ENTROPY: "source-entropy",
        SecurityFeature.AUTH_FAILURE_RATIO: "auth-failure",
        SecurityFeature.ASN_REPUTATION: "ASN-reputation",
        SecurityFeature.SESSION_ENTROPY: "session-entropy",
        SecurityFeature.MACHINE_TIMING: "machine-timing",
        SecurityFeature.PROTECTED_COHORT_INTEGRITY: "protected-cohort integrity",
    }
    return SecurityMeasurement(
        feature=feature,
        status=SecurityMeasurementStatus.INSUFFICIENT,
        scope=None,
        value=None,
        baseline=None,
        unit=None,
        window_start=None,
        window_end=None,
        window_count=0,
        evidence_refs=(),
        detail=f"No evidence-backed {labels[feature]} measurement exists for this incident.",
    )


def _timeline_event(episode: SymptomEpisode) -> SecurityTimelineEvent:
    return SecurityTimelineEvent(
        episode_id=episode.episode_id,
        kind=episode.kind,
        service=episode.service,
        signal=episode.signal,
        status=episode.status,
        opened_at=episode.opened_ts,
        last_breach_at=episode.last_breach_ts,
        closed_at=episode.closed_ts,
        peak_deformation_score=episode.peak_score,
        breach_window_count=episode.breach_tick_count,
        evidence_refs=_refs(episode),
        detail=(
            f"{episode.kind.value} on {episode.service}.{episode.signal} remained "
            f"evidence-backed across {episode.breach_tick_count} breach windows."
        ),
    )


def _mitigation(
    action_control: ActionControlSnapshot | None,
    *,
    integrity: SecurityMeasurement,
) -> SecurityMitigation:
    if action_control is None:
        return SecurityMitigation(
            state=None,
            plan_revision=None,
            rung_id=None,
            action_kind=None,
            target_ref=None,
            estimated_blast_fraction=None,
            updated_at=None,
            protected_cohort_integrity=integrity,
            detail="No server-held mitigation plan exists for this incident.",
        )
    return SecurityMitigation(
        state=action_control.state,
        plan_revision=action_control.plan_revision,
        rung_id=action_control.rung.rung_id,
        action_kind=action_control.plan.action_kind,
        target_ref=action_control.plan.target_ref,
        estimated_blast_fraction=action_control.plan.estimated_blast_fraction,
        updated_at=action_control.updated_at,
        protected_cohort_integrity=integrity,
        detail=(
            f"Server-held {action_control.rung.rung_id} plan revision "
            f"{action_control.plan_revision} is {action_control.state.value}."
        ),
    )


def _refs(episode: SymptomEpisode) -> tuple[str, ...]:
    return tuple(dict.fromkeys((episode.episode_id, *episode.evidence_refs)))


def _line(value: str) -> str:
    line = " ".join(value.split())
    if not line:
        raise ValueError("security detail cannot be blank")
    return line
