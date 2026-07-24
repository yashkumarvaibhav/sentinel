"""Evidence fusion: four independent axis scores become one named diagnosis.

The fusion is deliberately two readings of one configuration:

* **Crisp.** The ordered rule table is walked top to bottom and the first
  signature whose requirements all hold names the verdict class. This is the
  answer an operator can argue with, because every requirement is a stated fact
  about a measured axis.
* **Soft.** The same signatures are read as likelihoods over the measured
  scores and normalized into a full class distribution, so the runner-up is
  visible instead of hidden behind the winner.

Nothing here proposes: the agents measured, the rules are operator-owned, and
the arithmetic is deterministic. An axis nobody could measure is scored as
maximal uncertainty rather than as good news, and a rule that needs positive
knowledge of quiet cannot be satisfied by an axis that was never seen.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from contracts import (
    AgentAssessment,
    AgentStatus,
    EvidenceAxis,
    EvidenceItem,
    ReasonSubtype,
    RejectedAlternative,
    SymptomKind,
    Verdict,
    VerdictClass,
)
from decision.config import VerdictRuleConfig, VerdictRulesConfig


class FusionStatus(StrEnum):
    """Whether the evidence was enough to name a diagnosis at all."""

    DECIDED = "DECIDED"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class FusionResult:
    """The auditable outcome of one fusion, including a refusal to decide.

    A refusal is a first-class result: it records that the axes were measured
    and that no rule's requirements held, rather than quietly returning the
    most convenient class.
    """

    status: FusionStatus
    verdict: Verdict | None
    note: str

    def __post_init__(self) -> None:
        decided = self.status is FusionStatus.DECIDED
        if decided and self.verdict is None:
            raise ValueError("a decided fusion must carry its verdict")
        if not decided and self.verdict is not None:
            raise ValueError("an undecided fusion must not carry a verdict")


@dataclass(frozen=True, slots=True)
class _AxisReading:
    """One axis as the rule table sees it."""

    score: float
    lit: bool
    calm: bool
    measured: bool


class EvidenceFusion:
    """Fuse independent axis assessments into one verdict with its differential."""

    def __init__(self, *, configuration: VerdictRulesConfig) -> None:
        self._configuration = configuration

    @property
    def configuration(self) -> VerdictRulesConfig:
        """The ordered rule table this fusion answers from."""
        return self._configuration

    def fuse(self, assessments: Sequence[AgentAssessment]) -> FusionResult:
        """Name the diagnosis the evidence supports, or refuse to name one."""
        readings, ts, by_axis = self._readings(assessments)
        distribution = self._distribution(readings)
        rule = self._first_matching_rule(readings)
        if rule is None:
            return FusionResult(
                status=FusionStatus.INSUFFICIENT,
                verdict=None,
                note=(
                    "no rule's requirements held: "
                    + ", ".join(
                        f"{axis.value}="
                        + (f"{reading.score:.2f}" if reading.measured else "unmeasured")
                        for axis, reading in sorted(
                            readings.items(), key=lambda item: item[0].value
                        )
                    )
                ),
            )

        supporting = tuple(rule.lit)
        evidence = _evidence_for(by_axis, supporting)
        corroborating = _corroborating_kinds(by_axis, supporting)
        fit = distribution[rule.verdict_class.value]
        confidence = self._confidence(fit, corroborating)
        subtype = self._subtype(rule.verdict_class, frozenset(corroborating))
        services = tuple(
            sorted({service for axis in supporting for service in by_axis[axis].services})
        )
        verdict = Verdict(
            verdict_id=_verdict_id(ts, rule, distribution, confidence),
            ts=ts,
            verdict_class=rule.verdict_class,
            rule_id=rule.rule_id,
            confidence=confidence,
            reason=rule.reason,
            reason_subtype=subtype,
            distribution=distribution,
            corroborating_kinds=corroborating,
            services=services,
            evidence=evidence,
            assessment_ids=tuple(
                assessment.assessment_id
                for assessment in sorted(assessments, key=lambda item: item.axis.value)
            ),
            rejected_alternatives=self._rejected(rule, readings),
        )
        return FusionResult(
            status=FusionStatus.DECIDED,
            verdict=verdict,
            note=f"{rule.rule_id} matched on {len(evidence)} evidence items",
        )

    def _readings(
        self, assessments: Sequence[AgentAssessment]
    ) -> tuple[
        dict[EvidenceAxis, _AxisReading],
        datetime,
        dict[EvidenceAxis, AgentAssessment],
    ]:
        if not assessments:
            raise ValueError("fusion needs at least one axis assessment")
        by_axis: dict[EvidenceAxis, AgentAssessment] = {}
        for assessment in assessments:
            if not isinstance(assessment, AgentAssessment):
                raise TypeError("fusion consumes AgentAssessment values")
            if assessment.axis in by_axis:
                raise ValueError(f"{assessment.axis.value} was assessed twice for one tick")
            by_axis[assessment.axis] = assessment
        stamps = {assessment.ts for assessment in assessments}
        if len(stamps) != 1:
            raise ValueError("every assessment fused together must share one event time")
        ts = stamps.pop().astimezone(UTC)
        if ts.utcoffset() != timedelta(0):  # defensive: the contract already requires UTC
            raise ValueError("assessment time must be UTC")

        unknown = self._configuration.unknown_axis_score
        readings: dict[EvidenceAxis, _AxisReading] = {}
        for axis in EvidenceAxis:
            scored = by_axis.get(axis)
            measured = scored is not None and scored.status is AgentStatus.SCORED
            threshold = self._configuration.threshold_for(axis)
            score = scored.score if measured and scored is not None else unknown
            readings[axis] = _AxisReading(
                score=score,
                lit=measured and score >= threshold,
                calm=measured and score < threshold,
                measured=measured,
            )
        return readings, ts, by_axis

    def _first_matching_rule(
        self, readings: Mapping[EvidenceAxis, _AxisReading]
    ) -> VerdictRuleConfig | None:
        for rule in self._configuration.rules:
            if self._failed_requirement(rule, readings) is None:
                return rule
        return None

    def _failed_requirement(
        self,
        rule: VerdictRuleConfig,
        readings: Mapping[EvidenceAxis, _AxisReading],
    ) -> str | None:
        """Return the first unmet requirement of a rule, or None if all hold."""
        for axis in rule.lit:
            reading = readings[axis]
            if not reading.lit:
                return (
                    f"{axis.value} is not lit ("
                    + (
                        f"scored {reading.score:.2f}, below the "
                        f"{self._configuration.threshold_for(axis):.2f} threshold"
                        if reading.measured
                        else "never measured"
                    )
                    + ")"
                )
        for axis in rule.calm:
            reading = readings[axis]
            if not reading.calm:
                return (
                    f"{axis.value} is not known to be quiet ("
                    + (f"scored {reading.score:.2f}" if reading.measured else "never measured")
                    + ")"
                )
        for axis in rule.absent:
            reading = readings[axis]
            if reading.lit:
                return f"{axis.value} is lit at {reading.score:.2f}"
        return None

    def _distribution(self, readings: Mapping[EvidenceAxis, _AxisReading]) -> dict[str, float]:
        supports: dict[str, float] = {}
        for rule in self._configuration.rules:
            support = 1.0
            for axis in rule.lit:
                support *= readings[axis].score
            for axis in (*rule.calm, *rule.absent):
                support *= 1.0 - readings[axis].score
            supports[rule.verdict_class.value] = support
        total = sum(supports.values())
        if total <= 0.0:
            # Every signature is impossible under these scores; say so evenly
            # rather than inventing a winner.
            share = 1.0 / len(supports)
            return dict.fromkeys(supports, share)
        distribution = {name: support / total for name, support in supports.items()}
        # Absorb float drift into the largest mass so the contract's sum holds.
        drift = 1.0 - sum(distribution.values())
        largest = max(distribution, key=lambda name: (distribution[name], name))
        distribution[largest] += drift
        return distribution

    def _confidence(self, fit: float, corroborating: Sequence[SymptomKind]) -> float:
        settings = self._configuration.confidence
        saturation = settings.corroboration_saturation
        corroboration = min(len(corroborating) / saturation, 1.0)
        confidence = (
            settings.floor
            + settings.evidence_gain * fit
            + settings.corroboration_gain * corroboration
        )
        return min(max(confidence, 0.0), 1.0)

    def _subtype(
        self,
        verdict_class: VerdictClass,
        kinds: frozenset[SymptomKind],
    ) -> ReasonSubtype | None:
        for refinement in self._configuration.reason_subtypes:
            if refinement.matches(verdict_class, kinds):
                return refinement.subtype
        return None

    def _rejected(
        self,
        winner: VerdictRuleConfig,
        readings: Mapping[EvidenceAxis, _AxisReading],
    ) -> tuple[RejectedAlternative, ...]:
        rejected: list[RejectedAlternative] = []
        for rule in self._configuration.rules:
            if rule.rule_id == winner.rule_id:
                continue
            failure = self._failed_requirement(rule, readings)
            reason = (
                f"ruled out because {failure}"
                if failure is not None
                else f"outranked by the earlier rule {winner.rule_id}"
            )
            rejected.append(RejectedAlternative(verdict_class=rule.verdict_class, reason=reason))
        return tuple(rejected)


def _evidence_for(
    by_axis: Mapping[EvidenceAxis, AgentAssessment],
    axes: Iterable[EvidenceAxis],
) -> tuple[EvidenceItem, ...]:
    items: list[EvidenceItem] = []
    for axis in sorted(axes, key=lambda member: member.value):
        assessment = by_axis.get(axis)
        if assessment is not None:
            items.extend(assessment.evidence)
    return tuple(items)


def _corroborating_kinds(
    by_axis: Mapping[EvidenceAxis, AgentAssessment],
    axes: Iterable[EvidenceAxis],
) -> tuple[SymptomKind, ...]:
    kinds: set[SymptomKind] = set()
    for axis in axes:
        assessment = by_axis.get(axis)
        if assessment is not None:
            kinds.update(assessment.contributing_kinds)
    return tuple(sorted(kinds, key=lambda kind: kind.value))


def _verdict_id(
    ts: datetime,
    rule: VerdictRuleConfig,
    distribution: Mapping[str, float],
    confidence: float,
) -> str:
    identity = {
        "ts": ts.isoformat(),
        "rule_id": rule.rule_id,
        "class": rule.verdict_class.value,
        "confidence": format(confidence, ".12g"),
        "distribution": {name: format(mass, ".12g") for name, mass in distribution.items()},
    }
    rendered = json.dumps(identity, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
