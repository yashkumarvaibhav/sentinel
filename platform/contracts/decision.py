"""Contracts emitted by the decision plane's independent evidence agents."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import field_validator, model_validator

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
from contracts.detection import SymptomKind


class EvidenceAxis(StrEnum):
    """The independent axes a surge is judged on, one agent per axis.

    Each axis is scored from its own claimed evidence only. An attacker who
    keeps one axis calm cannot thereby quiet another, because no agent ever
    reads another agent's score.
    """

    SECURITY = "SECURITY"
    RELIABILITY = "RELIABILITY"
    CHANGE_CONFIG = "CHANGE_CONFIG"
    BUSINESS_IMPACT = "BUSINESS_IMPACT"


class EvidenceDirection(StrEnum):
    """Which way a measured value sits relative to its stated baseline."""

    ABOVE_BASELINE = "ABOVE_BASELINE"
    BELOW_BASELINE = "BELOW_BASELINE"
    AT_BASELINE = "AT_BASELINE"


class EvidenceItem(ContractModel):
    """One measured value that justifies part of an axis score.

    An agent never reports a bare number: every point of score is traceable to
    an item naming the feature, what was measured, what it is compared with,
    which way it moved and how much of the score it contributed.
    """

    feature: SignalName
    value: FiniteFloat
    baseline: FiniteFloat
    direction: EvidenceDirection
    contribution: Probability
    note: HumanText
    evidence_refs: tuple[Identifier, ...] = ()

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """The links backing one item are an ordered set."""
        return ensure_unique(values, field_name="evidence_refs")

    @model_validator(mode="after")
    def validate_direction(self) -> Self:
        """Reject an item whose stated direction contradicts its own numbers."""
        if self.value > self.baseline:
            measured = EvidenceDirection.ABOVE_BASELINE
        elif self.value < self.baseline:
            measured = EvidenceDirection.BELOW_BASELINE
        else:
            measured = EvidenceDirection.AT_BASELINE
        if self.direction is not measured:
            raise ValueError(
                f"direction {self.direction.value} contradicts value {self.value} "
                f"against baseline {self.baseline}"
            )
        return self


class ChangeKind(StrEnum):
    """How the system was changed by its operators, not by its callers."""

    DEPLOY = "DEPLOY"
    ROLLOUT = "ROLLOUT"
    FLAG = "FLAG"
    CONFIG = "CONFIG"


class ChangeEvent(ContractModel):
    """One recorded operator change, the raw material of deploy-correlated pressure.

    A change is a fact about the system's own history, never a conclusion about
    an incident. It carries its own honesty label because the MVP feed mixes an
    operator-maintained ledger with changes observed from the cluster.
    """

    change_id: Identifier
    kind: ChangeKind
    service: Identifier
    ts: UtcDatetime
    summary: HumanText
    source: Identifier
    honesty: Literal["REAL", "SIMULATED"]
    revision: Identifier | None = None
    evidence_refs: tuple[Identifier, ...] = ()

    @field_validator("evidence_refs")
    @classmethod
    def unique_change_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """The links backing one change are an ordered set."""
        return ensure_unique(values, field_name="evidence_refs")


class AgentStatus(StrEnum):
    """Whether an agent had the coverage to score its axis at all."""

    SCORED = "SCORED"
    INSUFFICIENT = "INSUFFICIENT"


class AgentTrend(StrEnum):
    """Direction of an axis score against the agent's previous assessment."""

    RISING = "RISING"
    FALLING = "FALLING"
    STEADY = "STEADY"
    UNKNOWN = "UNKNOWN"


class AgentAssessment(ContractModel):
    """One agent's evidence-backed verdict about one axis at one event time.

    A calm axis (``SCORED`` at ``0.0`` with no evidence) is a genuine finding:
    the claimed detectors ran and saw nothing. An axis whose detectors produced
    no coverage is ``INSUFFICIENT`` instead, so absence of evidence is never
    silently reported as evidence of absence.
    """

    assessment_id: Identifier
    axis: EvidenceAxis
    ts: UtcDatetime
    status: AgentStatus
    score: Probability
    trend: AgentTrend
    covered_kinds: tuple[SymptomKind, ...] = ()
    services: tuple[Identifier, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    note: HumanText

    @field_validator("services")
    @classmethod
    def unique_services(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """A service is named at most once per assessment."""
        return ensure_unique(values, field_name="services")

    @field_validator("covered_kinds")
    @classmethod
    def unique_covered_kinds(cls, values: tuple[SymptomKind, ...]) -> tuple[SymptomKind, ...]:
        """Coverage is a set of kinds, not a multiset."""
        ensure_unique(tuple(kind.value for kind in values), field_name="covered_kinds")
        return values

    @model_validator(mode="after")
    def validate_assessment(self) -> Self:
        """Keep insufficiency total and require evidence behind any positive score."""
        if self.status is AgentStatus.INSUFFICIENT:
            if self.score != 0.0:
                raise ValueError("an insufficient assessment must not carry a score")
            if self.trend is not AgentTrend.UNKNOWN:
                raise ValueError("an insufficient assessment must not claim a trend")
            if self.covered_kinds or self.evidence or self.services:
                raise ValueError("an insufficient assessment must not carry coverage or evidence")
            return self
        if not self.covered_kinds:
            raise ValueError("a scored assessment must record the kinds it had coverage for")
        if self.score > 0.0 and not self.evidence:
            raise ValueError("a positive score must be justified by evidence")
        if self.score == 0.0 and self.evidence:
            raise ValueError("a zero score must not claim contributing evidence")
        return self
