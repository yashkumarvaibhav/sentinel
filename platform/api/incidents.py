"""Durable live-incident snapshots and post-write invalidation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import ValidationError

from api.causal_graph import build_causal_graph, causal_graph_record
from api.incident_detail import build_incident_detail, incident_detail_record
from api.security import build_security_snapshot, security_record
from api.stream import snapshot_invalidation
from common.config import TopologyConfig
from common.storage import (
    IncidentDetailRecord,
    IncidentGraphRecord,
    IncidentRecord,
    IncidentSecurityRecord,
)
from contracts import (
    ActionControlSnapshot,
    AuditEntry,
    DecisionAction,
    DecompFrame,
    IncidentActionState,
    IncidentConfidence,
    IncidentConfidenceStatus,
    IncidentEvidenceValue,
    IncidentFeedItem,
    IncidentFeedResponse,
    SecurityCohort,
    SecurityMeasurement,
    SnapshotInvalidation,
    SnapshotResource,
    SymptomEpisode,
    VerdictClass,
)
from decision import IncidentOutcome

MAX_INCIDENTS = 50
DEFAULT_INCIDENTS = 20


class IncidentFeedReader(Protocol):
    """The bounded runtime-store read used by the gateway."""

    async def list_incidents(self, *, limit: int) -> tuple[IncidentRecord, ...]: ...


class IncidentFeedStore(Protocol):
    """The durable write used by a live decision-output publisher."""

    async def put_incident_bundle(
        self,
        record: IncidentRecord,
        graph: IncidentGraphRecord,
        detail: IncidentDetailRecord,
        security: IncidentSecurityRecord,
        action_control: ActionControlSnapshot | None = None,
    ) -> bool: ...


class InvalidationBroker(Protocol):
    """The in-process fan-out seam owned by the gateway."""

    def publish(self, event: SnapshotInvalidation) -> int: ...


class IncidentFeedDataError(ValueError):
    """A stored runtime row could not support the public contract."""


@dataclass(frozen=True, slots=True)
class IncidentPublishResult:
    """What one decision-output persistence attempt changed."""

    item: IncidentFeedItem
    persisted: bool


class IncidentFeedPublisher:
    """Persist a decision outcome, then and only then invalidate its snapshot."""

    def __init__(self, *, store: IncidentFeedStore, broker: InvalidationBroker) -> None:
        self._store = store
        self._broker = broker

    async def persist(
        self,
        outcome: IncidentOutcome,
        *,
        episodes: tuple[SymptomEpisode, ...],
        topology: TopologyConfig,
        dependency_signal_prefix: str,
        honesty: Literal["REAL", "SIMULATED"],
        stimulus_honesty: Literal["REAL", "SIMULATED"],
        mode: Literal["LIVE", "REPLAY"] = "LIVE",
        capture_id: str | None = None,
        seed: int | None = None,
        decomp_frames: tuple[DecompFrame, ...] = (),
        audit_entries: tuple[AuditEntry, ...] = (),
        decomposition_truncated: bool = False,
        calibrated_confidence: float | None = None,
        calibration_note: str | None = None,
        explained_event: str | None = None,
        action_control: ActionControlSnapshot | None = None,
        security_cohorts: tuple[SecurityCohort, ...] = (),
        protected_cohort_integrity: SecurityMeasurement | None = None,
    ) -> IncidentPublishResult:
        """Write a newer incident revision and signal browsers after commit."""
        item = build_incident_feed_item(
            outcome,
            honesty=honesty,
            calibrated_confidence=calibrated_confidence,
            calibration_note=calibration_note,
            explained_event=explained_event,
        )
        graph = build_causal_graph(
            outcome,
            episodes=episodes,
            topology=topology,
            dependency_signal_prefix=dependency_signal_prefix,
            honesty=honesty,
        )
        detail = build_incident_detail(
            outcome,
            episodes=episodes,
            graph=graph,
            decomp_frames=decomp_frames,
            audit_entries=audit_entries,
            telemetry_honesty=honesty,
            stimulus_honesty=stimulus_honesty,
            mode=mode,
            capture_id=capture_id,
            seed=seed,
            calibrated_confidence=calibrated_confidence,
            calibration_note=calibration_note,
            decomposition_truncated=decomposition_truncated,
        )
        security = build_security_snapshot(
            outcome,
            episodes=episodes,
            decomposition=detail.decomposition,
            honesty=honesty,
            cohorts=security_cohorts,
            action_control=action_control,
            protected_cohort_integrity=protected_cohort_integrity,
        )
        persisted = await self._store.put_incident_bundle(
            incident_record(item),
            causal_graph_record(graph),
            incident_detail_record(detail),
            security_record(security),
            action_control,
        )
        if persisted:
            resources = (
                (
                    SnapshotResource.INCIDENTS,
                    SnapshotResource.ACTIONS,
                    SnapshotResource.SECURITY,
                )
                if action_control is not None
                else (SnapshotResource.INCIDENTS, SnapshotResource.SECURITY)
            )
            self._broker.publish(snapshot_invalidation(*resources))
        return IncidentPublishResult(item=item, persisted=persisted)


def build_incident_feed_item(
    outcome: IncidentOutcome,
    *,
    honesty: Literal["REAL", "SIMULATED"],
    calibrated_confidence: float | None = None,
    calibration_note: str | None = None,
    explained_event: str | None = None,
) -> IncidentFeedItem:
    """Project one full decision outcome into the compact public card."""
    verdict = outcome.verdict
    if explained_event is not None and (
        verdict is None
        or verdict.verdict_class is not VerdictClass.EXPECTED_EVENT
        or outcome.decision.action is not DecisionAction.SUPPRESS
    ):
        raise ValueError("an explained event requires a suppressed EXPECTED_EVENT outcome")
    confidence = (
        IncidentConfidence(
            status=IncidentConfidenceStatus.INSUFFICIENT,
            value=None,
            note=(
                _line(calibration_note)
                if calibration_note is not None
                else "Runtime rule confidence is not calibrated, so no confidence is displayed."
            ),
        )
        if calibrated_confidence is None
        else IncidentConfidence(
            status=IncidentConfidenceStatus.CALIBRATED,
            value=calibrated_confidence,
            note=_line(
                calibration_note
                if calibration_note is not None
                else "A calibration note is required for calibrated confidence."
            ),
        )
    )
    if calibrated_confidence is not None and calibration_note is None:
        raise ValueError("calibrated confidence requires a calibration note")

    evidence: tuple[IncidentEvidenceValue, ...] = ()
    if verdict is not None:
        strongest = sorted(
            verdict.evidence,
            key=lambda item: (-item.contribution, item.feature),
        )[:2]
        evidence = tuple(
            IncidentEvidenceValue(
                feature=item.feature,
                value=item.value,
                baseline=item.baseline,
                direction=item.direction,
                note=_line(item.note),
            )
            for item in strongest
        )
    incident = outcome.incident
    services = tuple(dict.fromkeys((*incident.services, *incident.implicated_services)))
    return IncidentFeedItem(
        incident_id=incident.incident_id,
        opened_at=incident.opened_ts,
        updated_at=outcome.decision.ts,
        state=incident.state,
        severity=incident.severity,
        services=services,
        origin_service=incident.origin_service,
        verdict_class=verdict.verdict_class if verdict is not None else None,
        reason=_line(outcome.decision.reason),
        evidence=evidence,
        action=IncidentActionState(
            decision_action=outcome.decision.action,
            effect_status=None,
            detail=_action_detail(outcome.decision.action),
        ),
        confidence=confidence,
        muted=explained_event is not None,
        explanation=(
            f"Explained by {_line(explained_event)}" if explained_event is not None else None
        ),
        honesty=honesty,
    )


def incident_record(item: IncidentFeedItem) -> IncidentRecord:
    """Encode the strict public item into the existing runtime incident row."""
    return IncidentRecord(
        incident_id=item.incident_id,
        state=item.state.value,
        created_at=item.opened_at,
        updated_at=item.updated_at,
        payload=item.model_dump(mode="json"),
    )


def incident_snapshot(
    records: tuple[IncidentRecord, ...],
    *,
    limit: int,
) -> IncidentFeedResponse:
    """Strictly revalidate durable rows before exposing any of them."""
    try:
        incidents = tuple(_item_from_record(record) for record in records)
    except (ValidationError, ValueError, TypeError) as exc:
        raise IncidentFeedDataError(f"stored incident snapshot is invalid: {exc}") from exc
    return IncidentFeedResponse(
        status="ready",
        incidents=incidents,
        count=len(incidents),
        limit=limit,
        detail=None,
    )


def unavailable_incident_snapshot(*, limit: int, detail: str) -> IncidentFeedResponse:
    """Return no partial rows when the live store cannot be trusted."""
    return IncidentFeedResponse(
        status="degraded",
        incidents=(),
        count=0,
        limit=limit,
        detail=_line(detail),
    )


def _item_from_record(record: IncidentRecord) -> IncidentFeedItem:
    item = IncidentFeedItem.model_validate_json(
        json.dumps(record.payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    )
    if item.incident_id != record.incident_id:
        raise ValueError("stored incident id disagrees with its payload")
    if item.state.value != record.state:
        raise ValueError("stored incident state disagrees with its payload")
    if item.opened_at != record.created_at or item.updated_at != record.updated_at:
        raise ValueError("stored incident timestamps disagree with its payload")
    return item


def _action_detail(action: DecisionAction) -> str:
    if action is DecisionAction.SUPPRESS:
        return "Decision suppressed; no production effect was requested."
    if action is DecisionAction.ALERT:
        return "Alert raised; no production effect was requested."
    if action is DecisionAction.ESCALATE_TO_HUMAN:
        return "Escalated to a person; no production effect was requested."
    if action is DecisionAction.AUTO_CONTAIN_THEN_ESCALATE:
        return "Containment authorized and escalation requested; no actuator outcome is attached."
    return "Action authorized by evidence; no actuator outcome is attached."


def _line(value: str) -> str:
    rendered = " ".join(value.split())
    if not rendered:
        raise ValueError("public incident text cannot be blank")
    return rendered
