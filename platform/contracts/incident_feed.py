"""Public live-incident feed contracts.

The feed is a compact snapshot, not a second incident model. It exposes only
the evidence needed to scan the command centre and preserves the two absences
that matter most: no actuator outcome is not an applied action, and a raw rule
score is not calibrated confidence.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from contracts._base import (
    ContractModel,
    FiniteFloat,
    HumanText,
    Identifier,
    Probability,
    SignalName,
    UtcDatetime,
    ensure_unique,
)
from contracts.action import ActionStatus
from contracts.decision import (
    DecisionAction,
    EvidenceDirection,
    IncidentSeverity,
    IncidentState,
    VerdictClass,
)


class IncidentConfidenceStatus(StrEnum):
    """Whether the displayed confidence has a calibration basis."""

    CALIBRATED = "calibrated"
    INSUFFICIENT = "insufficient"


class IncidentConfidence(ContractModel):
    """A calibrated confidence or an explicit refusal to display one."""

    status: IncidentConfidenceStatus
    value: Probability | None
    note: HumanText

    @model_validator(mode="after")
    def value_matches_status(self) -> Self:
        if self.status is IncidentConfidenceStatus.CALIBRATED and self.value is None:
            raise ValueError("calibrated confidence requires a value")
        if self.status is IncidentConfidenceStatus.INSUFFICIENT and self.value is not None:
            raise ValueError("insufficient confidence cannot carry a value")
        return self


class IncidentEvidenceValue(ContractModel):
    """One measured value compact enough for an incident card."""

    feature: SignalName
    value: FiniteFloat
    baseline: FiniteFloat
    direction: EvidenceDirection
    note: HumanText

    @model_validator(mode="after")
    def direction_matches_values(self) -> Self:
        measured = (
            EvidenceDirection.ABOVE_BASELINE
            if self.value > self.baseline
            else EvidenceDirection.BELOW_BASELINE
            if self.value < self.baseline
            else EvidenceDirection.AT_BASELINE
        )
        if self.direction is not measured:
            raise ValueError("evidence direction contradicts value and baseline")
        return self


class IncidentActionState(ContractModel):
    """What was decided, separately from what an actuator proved it did."""

    decision_action: DecisionAction
    effect_status: ActionStatus | None
    detail: HumanText


class IncidentFeedItem(ContractModel):
    """One evidence-first card in the authoritative incident snapshot."""

    incident_id: Identifier
    opened_at: UtcDatetime
    updated_at: UtcDatetime
    state: IncidentState
    severity: IncidentSeverity
    services: tuple[Identifier, ...] = Field(min_length=1)
    origin_service: Identifier | None
    verdict_class: VerdictClass | None
    reason: HumanText
    evidence: tuple[IncidentEvidenceValue, ...] = Field(max_length=2)
    action: IncidentActionState
    confidence: IncidentConfidence
    muted: bool
    explanation: HumanText | None
    honesty: Literal["REAL", "SIMULATED"]

    @model_validator(mode="after")
    def coherent_card(self) -> Self:
        if self.updated_at < self.opened_at:
            raise ValueError("incident card update cannot precede its opening")
        ensure_unique(self.services, field_name="incident card services")
        if self.origin_service is not None and self.origin_service not in self.services:
            raise ValueError("incident card origin must be one of its named services")
        if self.muted != (self.explanation is not None):
            raise ValueError("a muted incident card requires an explanation and only then")
        if self.muted and (
            self.verdict_class is not VerdictClass.EXPECTED_EVENT
            or self.action.decision_action is not DecisionAction.SUPPRESS
        ):
            raise ValueError("only a suppressed EXPECTED_EVENT may be muted")
        return self


class IncidentFeedResponse(ContractModel):
    """One bounded latest-first REST snapshot."""

    status: Literal["ready", "degraded"]
    incidents: tuple[IncidentFeedItem, ...]
    count: int = Field(ge=0)
    limit: int = Field(ge=1, le=50)
    detail: HumanText | None = None

    @model_validator(mode="after")
    def coherent_snapshot(self) -> Self:
        if self.count != len(self.incidents):
            raise ValueError("incident snapshot count must match its items")
        if self.count > self.limit:
            raise ValueError("incident snapshot cannot exceed its stated limit")
        ensure_unique(
            tuple(item.incident_id for item in self.incidents),
            field_name="incident snapshot ids",
        )
        if self.status == "ready" and self.detail is not None:
            raise ValueError("a ready incident snapshot does not carry an error detail")
        if self.status == "degraded" and self.detail is None:
            raise ValueError("a degraded incident snapshot requires detail")
        if self.status == "degraded" and self.incidents:
            raise ValueError("a degraded incident snapshot must not return untrusted partial data")
        return self
