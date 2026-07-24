"""Contracts emitted by the decision plane's independent evidence agents."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

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
    contributing_kinds: tuple[SymptomKind, ...] = ()
    services: tuple[Identifier, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    note: HumanText

    @field_validator("services")
    @classmethod
    def unique_services(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """A service is named at most once per assessment."""
        return ensure_unique(values, field_name="services")

    @field_validator("covered_kinds", "contributing_kinds")
    @classmethod
    def unique_kinds(cls, values: tuple[SymptomKind, ...]) -> tuple[SymptomKind, ...]:
        """Coverage and contribution are sets of kinds, not multisets."""
        ensure_unique(tuple(kind.value for kind in values), field_name="kinds")
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
            if self.contributing_kinds:
                raise ValueError("an insufficient assessment must not claim contributing kinds")
            return self
        if not self.covered_kinds:
            raise ValueError("a scored assessment must record the kinds it had coverage for")
        if self.score > 0.0 and not self.evidence:
            raise ValueError("a positive score must be justified by evidence")
        if self.score == 0.0 and self.evidence:
            raise ValueError("a zero score must not claim contributing evidence")
        if self.contributing_kinds and not self.evidence:
            raise ValueError("a kind cannot contribute without an evidence item")
        unclaimed = sorted(set(self.contributing_kinds) - set(self.covered_kinds))
        if unclaimed:
            names = ", ".join(kind.value for kind in unclaimed)
            raise ValueError(f"contributing kinds must have had coverage: {names}")
        return self


class VerdictClass(StrEnum):
    """What the fused evidence says is actually happening."""

    EXPECTED_EVENT = "EXPECTED_EVENT"
    ATTACK = "ATTACK"
    OPERATIONAL_FAULT = "OPERATIONAL_FAULT"
    CODE_CONFIG_FAULT = "CODE_CONFIG_FAULT"
    COMBINATION = "COMBINATION"


class ReasonSubtype(StrEnum):
    """A refinement that changes what a sane response looks like.

    ``CAPACITY_SHORTAGE`` exists so under-provisioning is answered with
    "scale", never with "block": the traffic is real and the system is simply
    too small for it.
    """

    CAPACITY_SHORTAGE = "CAPACITY_SHORTAGE"


class RejectedAlternative(ContractModel):
    """One diagnosis that was considered and the evidence that ruled it out."""

    verdict_class: VerdictClass
    reason: HumanText

    @model_validator(mode="after")
    def validate_alternative(self) -> Self:
        """A rejection is only useful if it says what failed."""
        if not self.reason.strip():
            raise ValueError("a rejected alternative must state what ruled it out")
        return self


class Verdict(ContractModel):
    """The fused, evidence-backed answer, with its differential diagnosis.

    ``verdict_class`` comes from the ordered rule table - crisp, operator-owned
    and explainable. ``distribution`` is the same signatures read softly, so the
    runner-up is visible rather than hidden. Every class that lost is recorded
    in ``rejected_alternatives`` with the requirement it failed, so the answer
    can be argued with rather than merely believed.
    """

    verdict_id: Identifier
    ts: UtcDatetime
    verdict_class: VerdictClass
    rule_id: Identifier
    confidence: Probability
    reason: HumanText
    reason_subtype: ReasonSubtype | None = None
    distribution: dict[str, Probability] = Field(min_length=1)
    corroborating_kinds: tuple[SymptomKind, ...] = ()
    services: tuple[Identifier, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    assessment_ids: tuple[Identifier, ...] = ()
    rejected_alternatives: tuple[RejectedAlternative, ...] = ()

    @field_validator("services", "assessment_ids")
    @classmethod
    def unique_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Services and the assessments behind a verdict are ordered sets."""
        return ensure_unique(values, field_name="references")

    @field_validator("corroborating_kinds")
    @classmethod
    def unique_corroborating_kinds(cls, values: tuple[SymptomKind, ...]) -> tuple[SymptomKind, ...]:
        """Corroboration counts distinct kinds, so a kind appears once."""
        ensure_unique(tuple(kind.value for kind in values), field_name="corroborating_kinds")
        return values

    @model_validator(mode="after")
    def validate_verdict(self) -> Self:
        """Enforce a complete, normalized distribution and a full differential."""
        classes = {member.value for member in VerdictClass}
        if set(self.distribution) != classes:
            missing = sorted(classes - set(self.distribution))
            unknown = sorted(set(self.distribution) - classes)
            raise ValueError(
                "distribution must cover exactly the verdict classes "
                f"(missing: {missing}, unknown: {unknown})"
            )
        total = sum(self.distribution.values())
        if not math.isclose(total, 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f"distribution must sum to 1, got {total}")
        rejected = [alternative.verdict_class for alternative in self.rejected_alternatives]
        if len(rejected) != len(set(rejected)):
            raise ValueError("each alternative may be rejected only once")
        if self.verdict_class in rejected:
            raise ValueError("the winning class cannot also be a rejected alternative")
        if self.reason_subtype is ReasonSubtype.CAPACITY_SHORTAGE and (
            self.verdict_class is not VerdictClass.OPERATIONAL_FAULT
        ):
            raise ValueError("CAPACITY_SHORTAGE only refines an operational fault")
        return self


class IncidentState(StrEnum):
    """Where an incident is in its life, from first symptom to resolved."""

    OPEN = "OPEN"
    MITIGATING = "MITIGATING"
    MONITORING = "MONITORING"
    RESOLVED = "RESOLVED"


class IncidentSeverity(StrEnum):
    """How much this incident matters, in the language a responder pages on."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Incident(ContractModel):
    """One real-world problem, assembled from the storm of symptoms it caused.

    A single fault lights many detectors across many services. An incident is
    the collapse of that storm into the one thing an on-call person is actually
    dealing with: co-occurring episodes on services that are near each other in
    the topology become one incident, not twelve alerts.

    Identity is anchored to the earliest episode in the cluster, so the incident
    keeps its id as the storm grows around it.
    """

    incident_id: Identifier
    anchor_episode_id: Identifier
    opened_ts: UtcDatetime
    last_activity_ts: UtcDatetime
    state: IncidentState
    severity: IncidentSeverity
    services: tuple[Identifier, ...] = Field(min_length=1)
    kinds: tuple[SymptomKind, ...] = Field(min_length=1)
    episode_ids: tuple[Identifier, ...] = Field(min_length=1)
    business_impact: Probability | None = None
    merged_incident_ids: tuple[Identifier, ...] = ()
    revision: int = Field(ge=1)
    note: HumanText

    @field_validator("services", "episode_ids", "merged_incident_ids")
    @classmethod
    def unique_members(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Every membership list on an incident is an ordered set."""
        return ensure_unique(values, field_name="members")

    @field_validator("kinds")
    @classmethod
    def unique_incident_kinds(cls, values: tuple[SymptomKind, ...]) -> tuple[SymptomKind, ...]:
        """An incident names each symptom kind once, however many episodes carry it."""
        ensure_unique(tuple(kind.value for kind in values), field_name="kinds")
        return values

    @model_validator(mode="after")
    def validate_incident(self) -> Self:
        """Keep the timeline forward-moving and the anchor inside the cluster."""
        if self.last_activity_ts < self.opened_ts:
            raise ValueError("last_activity_ts must not precede opened_ts")
        if self.anchor_episode_id not in self.episode_ids:
            raise ValueError("the anchoring episode must be a member of the incident")
        if self.incident_id in self.merged_incident_ids:
            raise ValueError("an incident cannot record itself as merged away")
        return self
