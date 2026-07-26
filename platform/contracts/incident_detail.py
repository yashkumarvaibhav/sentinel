"""Public per-incident proof contracts.

The compact feed answers "what needs attention?" This contract answers "prove
it." It therefore retains the full deterministic verdict, decomposition,
evidence, causal collapse, verifier result and immutable action records while
those inputs are still available at the decision tick.
"""

from __future__ import annotations

import math
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from contracts._base import (
    ContractModel,
    HumanText,
    Identifier,
    Probability,
    SignalName,
    UtcDatetime,
    ensure_unique,
)
from contracts.audit import AuditEntry
from contracts.causal_graph import CausalGraph
from contracts.decision import (
    Decision,
    EvidenceAxis,
    EvidenceDirection,
    IncidentSeverity,
    IncidentState,
    ReasonSubtype,
    RejectedAlternative,
    VerdictClass,
    Verification,
)
from contracts.detection import DecompFrame, SymptomKind
from contracts.incident_feed import IncidentConfidence


class VerdictProbability(ContractModel):
    """One member of the complete deterministic verdict distribution."""

    verdict_class: VerdictClass
    probability: Probability


class IncidentVerdictProof(ContractModel):
    """The winning class and complete support distribution, with honest calibration."""

    status: Literal["decided", "insufficient"]
    verdict_class: VerdictClass | None
    reason_subtype: ReasonSubtype | None
    distribution: tuple[VerdictProbability, ...]
    calibration: IncidentConfidence

    @model_validator(mode="after")
    def coherent_verdict(self) -> Self:
        if self.status == "insufficient":
            if (
                self.verdict_class is not None
                or self.reason_subtype is not None
                or self.distribution
            ):
                raise ValueError("an insufficient verdict cannot claim a class or distribution")
            return self
        if self.verdict_class is None:
            raise ValueError("a decided verdict must name its winning class")
        classes = [point.verdict_class for point in self.distribution]
        if len(classes) != len(set(classes)):
            raise ValueError("a verdict class may appear only once in the distribution")
        if set(classes) != set(VerdictClass):
            raise ValueError("a decided distribution must cover every verdict class")
        if not math.isclose(
            sum(point.probability for point in self.distribution),
            1.0,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError("the verdict distribution must sum to one")
        return self


class IncidentDecomposition(ContractModel):
    """The primary full-resolution incident series, or explicit insufficiency."""

    status: Literal["available", "insufficient"]
    service: Identifier | None
    signal: SignalName | None
    start: UtcDatetime
    end: UtcDatetime
    frames: tuple[DecompFrame, ...] = Field(max_length=500)
    truncated: bool
    detail: HumanText

    @model_validator(mode="after")
    def coherent_decomposition(self) -> Self:
        if self.end < self.start:
            raise ValueError("the decomposition window cannot run backwards")
        if self.status == "insufficient":
            if self.service is not None or self.signal is not None or self.frames:
                raise ValueError("an insufficient decomposition cannot claim measured frames")
            if self.truncated:
                raise ValueError("missing decomposition evidence cannot be truncated")
            return self
        if self.service is None or self.signal is None or not self.frames:
            raise ValueError("an available decomposition requires one measured series")
        previous: tuple[UtcDatetime, str] | None = None
        for frame in self.frames:
            if frame.service != self.service or frame.signal != self.signal:
                raise ValueError("every decomposition frame must belong to the named series")
            if not self.start <= frame.ts <= self.end:
                raise ValueError("every decomposition frame must be inside the incident window")
            identity = (frame.ts, frame.frame_id)
            if previous is not None and identity <= previous:
                raise ValueError("decomposition frames must be uniquely ordered by time and id")
            previous = identity
        return self


class IncidentEvidenceProof(ContractModel):
    """One typed, independently scored evidence item behind the verdict."""

    assessment_id: Identifier
    axis: EvidenceAxis
    symptom_kinds: tuple[SymptomKind, ...]
    services: tuple[Identifier, ...]
    feature: SignalName
    value: float
    baseline: float
    direction: EvidenceDirection
    contribution: Probability
    note: HumanText
    evidence_refs: tuple[Identifier, ...]

    @field_validator("services", "evidence_refs")
    @classmethod
    def unique_evidence_members(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique(values, field_name="incident detail evidence members")

    @field_validator("symptom_kinds")
    @classmethod
    def unique_evidence_kinds(cls, values: tuple[SymptomKind, ...]) -> tuple[SymptomKind, ...]:
        ensure_unique(
            tuple(kind.value for kind in values),
            field_name="incident detail symptom kinds",
        )
        return values


class IncidentActionLog(ContractModel):
    """The decision plus every attached immutable action/audit record."""

    decision: Decision
    entries: tuple[AuditEntry, ...]
    detail: HumanText

    @model_validator(mode="after")
    def ordered_entries(self) -> Self:
        sequences = [entry.sequence for entry in self.entries]
        if sequences != sorted(set(sequences)):
            raise ValueError("action log entries must have unique ascending ledger sequences")
        return self


class IncidentProvenance(ContractModel):
    """What was measured and how the stimulus reached the platform."""

    telemetry: Literal["REAL", "SIMULATED"]
    stimulus: Literal["REAL", "SIMULATED"]
    mode: Literal["LIVE", "REPLAY"]
    capture_id: Identifier | None = None
    seed: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def replay_names_capture(self) -> Self:
        if self.mode == "REPLAY" and self.capture_id is None:
            raise ValueError("a replay proof must name its capture")
        return self


class IncidentDetail(ContractModel):
    """One evidence-complete incident revision for the proof screen."""

    incident_id: Identifier
    opened_at: UtcDatetime
    updated_at: UtcDatetime
    state: IncidentState
    severity: IncidentSeverity
    services: tuple[Identifier, ...] = Field(min_length=1)
    origin_service: Identifier | None
    origin_confidence: Probability | None
    verdict: IncidentVerdictProof
    reason: HumanText
    rejected_alternatives: tuple[RejectedAlternative, ...]
    decomposition: IncidentDecomposition
    evidence: tuple[IncidentEvidenceProof, ...]
    causal_graph: CausalGraph
    verification: Verification
    action_log: IncidentActionLog
    provenance: IncidentProvenance

    @field_validator("services")
    @classmethod
    def unique_services(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique(values, field_name="incident detail services")

    @model_validator(mode="after")
    def coherent_detail(self) -> Self:
        if self.updated_at < self.opened_at:
            raise ValueError("the incident detail cannot predate its opening")
        if (self.origin_service is None) != (self.origin_confidence is None):
            raise ValueError("an origin and its confidence are attached together")
        if self.origin_service is not None and self.origin_service not in self.services:
            raise ValueError("the incident detail origin must be one of its named services")
        if self.causal_graph.incident_id != self.incident_id:
            raise ValueError("the causal graph must belong to the incident detail")
        if self.verification.incident_id != self.incident_id:
            raise ValueError("the verification must belong to the incident detail")
        if self.action_log.decision.incident_id != self.incident_id:
            raise ValueError("the action decision must belong to the incident detail")
        for entry in self.action_log.entries:
            if entry.incident_id != self.incident_id:
                raise ValueError("every action record must belong to the incident detail")
        return self


class IncidentDetailResponse(ContractModel):
    """Ready/not-found/degraded response for one stable incident id."""

    status: Literal["ready", "not_found", "degraded"]
    detail: IncidentDetail | None
    message: HumanText | None

    @model_validator(mode="after")
    def coherent_response(self) -> Self:
        if self.status == "ready":
            if self.detail is None or self.message is not None:
                raise ValueError("a ready detail response carries only the proof")
        elif self.detail is not None or self.message is None:
            raise ValueError("a non-ready detail response carries only an explanation")
        return self
