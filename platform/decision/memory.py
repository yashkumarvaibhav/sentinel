"""Incident memory: "have we seen this shape before?"

An incident's signature is the four independent axis scores at its peak, in a
fixed order. That is deliberately not an embedding: the four numbers are what
the evidence agents already measured, so a similar signature means the same
*balance* of hostility, degradation, self-inflicted change and user harm - not
merely similar text in a description.

Two rules keep this honest:

* A signature is only recorded when **all four axes were actually scored**. An
  incident we could only half-measure is not a memory worth matching against,
  and padding the gaps with a neutral value would let ignorance masquerade as
  resemblance.
* A match only ever **nudges** confidence, within a configured bound, and only
  above a configured similarity. Recognition is corroboration; it is never
  allowed to become the reason for a verdict.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from contracts import (
    AgentAssessment,
    AgentStatus,
    EvidenceAxis,
    EvidenceDirection,
    EvidenceItem,
    Incident,
    Verdict,
)
from decision.config import IncidentMemoryConfig

# The fixed order of the signature's dimensions. Changing this invalidates
# every stored signature, so it is a constant rather than configuration.
SIGNATURE_AXES: tuple[EvidenceAxis, ...] = (
    EvidenceAxis.SECURITY,
    EvidenceAxis.RELIABILITY,
    EvidenceAxis.CHANGE_CONFIG,
    EvidenceAxis.BUSINESS_IMPACT,
)

# Furthest two points can be in the unit hypercube the signature lives in.
_MAX_DISTANCE = math.sqrt(len(SIGNATURE_AXES))


@dataclass(frozen=True, slots=True)
class IncidentSignature:
    """One incident reduced to the four numbers that characterise its shape."""

    incident_id: str
    recorded_at: datetime
    vector: tuple[float, ...]
    severity: str
    origin_service: str | None = None
    verdict_class: str | None = None

    def __post_init__(self) -> None:
        if len(self.vector) != len(SIGNATURE_AXES):
            raise ValueError(f"a signature has exactly {len(SIGNATURE_AXES)} dimensions")
        for value in self.vector:
            if not 0.0 <= value <= 1.0 or math.isnan(value):
                raise ValueError("every signature dimension is a probability")
        if self.recorded_at.utcoffset() != timedelta(0):
            raise ValueError("recorded_at must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class SimilarIncident:
    """A remembered incident and how closely it resembles the current one."""

    signature: IncidentSignature
    similarity: float


def build_signature(
    incident: Incident,
    *,
    assessments: Sequence[AgentAssessment],
    verdict: Verdict | None = None,
) -> IncidentSignature | None:
    """Reduce an incident to its signature, or refuse when an axis went unmeasured."""
    by_axis = {assessment.axis: assessment for assessment in assessments}
    if len(by_axis) != len(assessments):
        raise ValueError("an axis may contribute to a signature only once")
    scores: list[float] = []
    for axis in SIGNATURE_AXES:
        assessment = by_axis.get(axis)
        if assessment is None or assessment.status is not AgentStatus.SCORED:
            return None
        scores.append(assessment.score)
    return IncidentSignature(
        incident_id=incident.incident_id,
        recorded_at=incident.last_activity_ts.astimezone(UTC),
        vector=tuple(scores),
        severity=incident.severity.value,
        origin_service=incident.origin_service,
        verdict_class=None if verdict is None else verdict.verdict_class.value,
    )


def similarity(left: IncidentSignature, right: IncidentSignature) -> float:
    """How alike two incident shapes are, on a 0-1 scale."""
    distance = math.dist(left.vector, right.vector)
    return min(max(1.0 - distance / _MAX_DISTANCE, 0.0), 1.0)


def similarity_from_distance(distance: float) -> float:
    """Convert a stored euclidean distance into the same 0-1 similarity."""
    if distance < 0.0:
        raise ValueError("a distance cannot be negative")
    return min(max(1.0 - distance / _MAX_DISTANCE, 0.0), 1.0)


def nearest(
    signature: IncidentSignature,
    memory: Sequence[IncidentSignature],
    *,
    configuration: IncidentMemoryConfig,
) -> tuple[SimilarIncident, ...]:
    """Rank remembered incidents by resemblance, keeping only credible matches.

    The incident itself is never its own precedent, and a resemblance below the
    configured threshold is discarded rather than reported weakly - a bad match
    surfaced as a match is worse than no match at all.
    """
    matches = [
        SimilarIncident(signature=remembered, similarity=similarity(signature, remembered))
        for remembered in memory
        if remembered.incident_id != signature.incident_id
    ]
    credible = [match for match in matches if match.similarity >= configuration.minimum_similarity]
    ranked = sorted(
        credible,
        key=lambda match: (-match.similarity, match.signature.incident_id),
    )
    return tuple(ranked[: configuration.neighbours])


def recognized(
    verdict: Verdict,
    matches: Sequence[SimilarIncident],
    *,
    configuration: IncidentMemoryConfig,
) -> Verdict:
    """Nudge a verdict's confidence by how strongly this shape is recognised.

    The nudge is bounded by configuration and scaled by how far past the
    similarity floor the best match sits, so recognition can corroborate a
    verdict but can never carry one. With no credible match the verdict is
    returned untouched.
    """
    if not matches:
        return verdict
    best = max(matches, key=lambda match: match.similarity)
    floor = configuration.minimum_similarity
    span = 1.0 - floor
    strength = 1.0 if span <= 0.0 else min((best.similarity - floor) / span, 1.0)
    nudged = min(verdict.confidence + configuration.confidence_nudge * strength, 1.0)
    direction = (
        EvidenceDirection.ABOVE_BASELINE
        if best.similarity > floor
        else EvidenceDirection.AT_BASELINE
    )
    item = EvidenceItem(
        feature="incident.memory_similarity",
        value=best.similarity,
        baseline=floor,
        direction=direction,
        contribution=nudged - verdict.confidence,
        note=(
            f"this shape resembles incident {best.signature.incident_id} at "
            f"{best.similarity:.3f} similarity"
            + (
                ""
                if best.signature.origin_service is None
                else f", which originated in {best.signature.origin_service}"
            )
        ),
        evidence_refs=(best.signature.incident_id,),
    )
    return verdict.model_copy(update={"confidence": nudged, "evidence": (*verdict.evidence, item)})
