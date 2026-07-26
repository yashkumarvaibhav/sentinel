"""Materialize and strictly read the per-incident proof snapshot."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from pydantic import ValidationError

from common.storage import IncidentDetailRecord
from contracts import (
    AuditEntry,
    CausalGraph,
    DecompFrame,
    IncidentActionLog,
    IncidentConfidence,
    IncidentConfidenceStatus,
    IncidentDecomposition,
    IncidentDetail,
    IncidentDetailResponse,
    IncidentEvidenceProof,
    IncidentProvenance,
    IncidentVerdictProof,
    SymptomEpisode,
    VerdictClass,
    VerdictProbability,
)
from decision import IncidentOutcome

MAX_DETAIL_FRAMES = 500


class IncidentDetailDataError(ValueError):
    """Durable proof data contradicted its storage identity or strict contract."""


def build_incident_detail(
    outcome: IncidentOutcome,
    *,
    episodes: tuple[SymptomEpisode, ...],
    graph: CausalGraph,
    decomp_frames: tuple[DecompFrame, ...],
    audit_entries: tuple[AuditEntry, ...],
    telemetry_honesty: Literal["REAL", "SIMULATED"],
    stimulus_honesty: Literal["REAL", "SIMULATED"],
    mode: Literal["LIVE", "REPLAY"],
    capture_id: str | None = None,
    seed: int | None = None,
    calibrated_confidence: float | None = None,
    calibration_note: str | None = None,
    decomposition_truncated: bool = False,
) -> IncidentDetail:
    """Project full decision-time evidence without reconstructing from compact rows."""
    incident = outcome.incident
    member_ids = set(incident.episode_ids)
    supplied_ids = [episode.episode_id for episode in episodes]
    if len(supplied_ids) != len(set(supplied_ids)):
        raise ValueError("incident detail episode evidence must be unique")
    if set(supplied_ids) != member_ids:
        missing = sorted(member_ids - set(supplied_ids))
        foreign = sorted(set(supplied_ids) - member_ids)
        raise ValueError(
            f"incident detail requires exact member episodes (missing={missing}, foreign={foreign})"
        )
    if graph.incident_id != incident.incident_id or graph.updated_at != outcome.decision.ts:
        raise ValueError("the causal graph must be the same incident decision revision")

    services = tuple(dict.fromkeys((*incident.services, *incident.implicated_services)))
    decomposition = _decomposition(
        outcome,
        frames=decomp_frames,
        services=set(services),
        upstream_truncated=decomposition_truncated,
    )
    evidence = tuple(
        IncidentEvidenceProof(
            assessment_id=assessment.assessment_id,
            axis=assessment.axis,
            symptom_kinds=assessment.contributing_kinds,
            services=assessment.services,
            feature=item.feature,
            value=item.value,
            baseline=item.baseline,
            direction=item.direction,
            contribution=item.contribution,
            note=_line(item.note),
            evidence_refs=item.evidence_refs,
        )
        for assessment in outcome.assessments
        for item in assessment.evidence
    )
    entries = tuple(sorted(audit_entries, key=lambda entry: entry.sequence))
    if len({entry.sequence for entry in entries}) != len(entries):
        raise ValueError("incident action records must have unique ledger sequences")
    if any(entry.incident_id != incident.incident_id for entry in entries):
        raise ValueError("every action record must belong to the incident")

    verdict = outcome.verdict
    calibration = _calibration(
        calibrated_confidence=calibrated_confidence,
        calibration_note=calibration_note,
    )
    verdict_proof = (
        IncidentVerdictProof(
            status="insufficient",
            verdict_class=None,
            reason_subtype=None,
            distribution=(),
            calibration=calibration,
        )
        if verdict is None
        else IncidentVerdictProof(
            status="decided",
            verdict_class=verdict.verdict_class,
            reason_subtype=verdict.reason_subtype,
            distribution=tuple(
                VerdictProbability(
                    verdict_class=verdict_class,
                    probability=verdict.distribution[verdict_class.value],
                )
                for verdict_class in VerdictClass
            ),
            calibration=calibration,
        )
    )
    return IncidentDetail(
        incident_id=incident.incident_id,
        opened_at=incident.opened_ts,
        updated_at=outcome.decision.ts,
        state=incident.state,
        severity=incident.severity,
        services=services,
        origin_service=incident.origin_service,
        origin_confidence=incident.origin_confidence,
        verdict=verdict_proof,
        reason=_line(outcome.decision.reason if verdict is None else verdict.reason),
        rejected_alternatives=(() if verdict is None else verdict.rejected_alternatives),
        decomposition=decomposition,
        evidence=evidence,
        causal_graph=graph,
        verification=outcome.verification,
        action_log=IncidentActionLog(
            decision=outcome.decision,
            entries=entries,
            detail=(
                f"{len(entries)} immutable action/audit record"
                f"{'' if len(entries) == 1 else 's'} attached."
                if entries
                else "No actuator records are attached to this decision revision."
            ),
        ),
        provenance=IncidentProvenance(
            telemetry=telemetry_honesty,
            stimulus=stimulus_honesty,
            mode=mode,
            capture_id=capture_id,
            seed=seed,
        ),
    )


def incident_detail_record(detail: IncidentDetail) -> IncidentDetailRecord:
    """Encode one strict proof snapshot for the incident-owned table."""
    return IncidentDetailRecord(
        incident_id=detail.incident_id,
        updated_at=detail.updated_at,
        payload=detail.model_dump(mode="json"),
    )


def incident_detail_snapshot(record: IncidentDetailRecord) -> IncidentDetailResponse:
    """Strictly revalidate the durable payload and its storage identity."""
    try:
        detail = IncidentDetail.model_validate_json(
            json.dumps(record.payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise IncidentDetailDataError(f"stored incident detail is invalid: {exc}") from exc
    if detail.incident_id != record.incident_id:
        raise IncidentDetailDataError("stored incident detail id disagrees with its payload")
    if detail.updated_at != record.updated_at:
        raise IncidentDetailDataError("stored incident detail timestamp disagrees with its payload")
    return IncidentDetailResponse(status="ready", detail=detail, message=None)


def unavailable_incident_detail(
    *, status: Literal["not_found", "degraded"], message: str
) -> IncidentDetailResponse:
    """Represent absence and untrustworthy storage as different public facts."""
    return IncidentDetailResponse(status=status, detail=None, message=_line(message))


def _decomposition(
    outcome: IncidentOutcome,
    *,
    frames: Sequence[DecompFrame],
    services: set[str],
    upstream_truncated: bool,
) -> IncidentDecomposition:
    start = outcome.incident.opened_ts
    end = outcome.decision.ts
    if not frames:
        return IncidentDecomposition(
            status="insufficient",
            service=None,
            signal=None,
            start=start,
            end=end,
            frames=(),
            truncated=False,
            detail="No decomposition frames were attached for this incident window.",
        )
    grouped: dict[tuple[str, str], list[DecompFrame]] = defaultdict(list)
    for frame in frames:
        if frame.service not in services:
            raise ValueError(
                f"decomposition frame {frame.frame_id} does not name an incident service"
            )
        if not start <= frame.ts <= end:
            raise ValueError(f"decomposition frame {frame.frame_id} is outside the incident window")
        grouped[(frame.service, frame.signal)].append(frame)
    selected_key = min(
        grouped,
        key=lambda key: (
            -max(frame.residual_score for frame in grouped[key]),
            key[0],
            key[1],
        ),
    )
    selected = sorted(grouped[selected_key], key=lambda frame: (frame.ts, frame.frame_id))
    truncated = upstream_truncated or len(selected) > MAX_DETAIL_FRAMES
    bounded = tuple(selected[:MAX_DETAIL_FRAMES])
    return IncidentDecomposition(
        status="available",
        service=selected_key[0],
        signal=selected_key[1],
        start=start,
        end=end,
        frames=bounded,
        truncated=truncated,
        detail=(
            f"Primary series selected deterministically from {len(grouped)} measured "
            f"incident-window series; {len(bounded)} full-resolution frame"
            f"{'' if len(bounded) == 1 else 's'} attached."
        ),
    )


def _calibration(
    *, calibrated_confidence: float | None, calibration_note: str | None
) -> IncidentConfidence:
    if calibrated_confidence is None:
        return IncidentConfidence(
            status=IncidentConfidenceStatus.INSUFFICIENT,
            value=None,
            note=_line(
                calibration_note
                if calibration_note is not None
                else (
                    "Runtime rule confidence is not calibrated; the distribution is "
                    "deterministic rule support, not calibrated incident probability."
                )
            ),
        )
    if calibration_note is None:
        raise ValueError("calibrated confidence requires a calibration note")
    return IncidentConfidence(
        status=IncidentConfidenceStatus.CALIBRATED,
        value=calibrated_confidence,
        note=_line(calibration_note),
    )


def _line(value: str) -> str:
    line = " ".join(value.split())
    if not line:
        raise ValueError("incident detail text must not be blank")
    return line
