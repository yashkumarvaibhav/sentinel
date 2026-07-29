"""Evidence-only contracts for the current security view."""

from __future__ import annotations

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
from contracts.action import ActionKind
from contracts.action_control import ActionControlState
from contracts.decision import IncidentState
from contracts.detection import EpisodeStatus, SymptomKind
from contracts.incident_detail import IncidentDecomposition


class SecurityFeature(StrEnum):
    """The security-view signals, whether measured or explicitly unavailable."""

    PATH_ENTROPY = "PATH_ENTROPY"
    SOURCE_ENTROPY = "SOURCE_ENTROPY"
    AUTH_FAILURE_RATIO = "AUTH_FAILURE_RATIO"
    ASN_REPUTATION = "ASN_REPUTATION"
    SESSION_ENTROPY = "SESSION_ENTROPY"
    MACHINE_TIMING = "MACHINE_TIMING"
    PROTECTED_COHORT_INTEGRITY = "PROTECTED_COHORT_INTEGRITY"


SECURITY_FEATURE_ORDER = tuple(SecurityFeature)
COHORT_FEATURE_ORDER = (
    SecurityFeature.SOURCE_ENTROPY,
    SecurityFeature.AUTH_FAILURE_RATIO,
    SecurityFeature.ASN_REPUTATION,
    SecurityFeature.SESSION_ENTROPY,
    SecurityFeature.MACHINE_TIMING,
)


class SecurityMeasurementStatus(StrEnum):
    """Whether telemetry supports a numeric security claim."""

    MEASURED = "MEASURED"
    INSUFFICIENT = "INSUFFICIENT"


class SecurityMeasurementUnit(StrEnum):
    """What a measured numeric security value actually represents."""

    DEFORMATION_SCORE = "DEFORMATION_SCORE"
    RATIO = "RATIO"
    ENTROPY = "ENTROPY"
    REPUTATION_SCORE = "REPUTATION_SCORE"
    COEFFICIENT_OF_VARIATION = "COEFFICIENT_OF_VARIATION"
    INTEGRITY_RATIO = "INTEGRITY_RATIO"


_FEATURE_UNITS = {
    SecurityFeature.PATH_ENTROPY: {
        SecurityMeasurementUnit.ENTROPY,
        SecurityMeasurementUnit.DEFORMATION_SCORE,
    },
    SecurityFeature.SOURCE_ENTROPY: {
        SecurityMeasurementUnit.ENTROPY,
        SecurityMeasurementUnit.DEFORMATION_SCORE,
    },
    SecurityFeature.AUTH_FAILURE_RATIO: {
        SecurityMeasurementUnit.RATIO,
        SecurityMeasurementUnit.DEFORMATION_SCORE,
    },
    SecurityFeature.ASN_REPUTATION: {
        SecurityMeasurementUnit.REPUTATION_SCORE,
    },
    SecurityFeature.SESSION_ENTROPY: {
        SecurityMeasurementUnit.ENTROPY,
        SecurityMeasurementUnit.DEFORMATION_SCORE,
    },
    SecurityFeature.MACHINE_TIMING: {
        SecurityMeasurementUnit.COEFFICIENT_OF_VARIATION,
        SecurityMeasurementUnit.DEFORMATION_SCORE,
    },
    SecurityFeature.PROTECTED_COHORT_INTEGRITY: {
        SecurityMeasurementUnit.INTEGRITY_RATIO,
    },
}
_PROBABILITY_UNITS = {
    SecurityMeasurementUnit.DEFORMATION_SCORE,
    SecurityMeasurementUnit.RATIO,
    SecurityMeasurementUnit.REPUTATION_SCORE,
    SecurityMeasurementUnit.INTEGRITY_RATIO,
}


class SecurityMeasurement(ContractModel):
    """One scoped security measurement with evidence-window provenance."""

    feature: SecurityFeature
    status: SecurityMeasurementStatus
    scope: Identifier | None
    value: FiniteFloat | None
    baseline: FiniteFloat | None
    unit: SecurityMeasurementUnit | None
    window_start: UtcDatetime | None
    window_end: UtcDatetime | None
    window_count: int = Field(ge=0)
    evidence_refs: tuple[Identifier, ...]
    detail: HumanText

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique(values, field_name="security measurement evidence refs")

    @model_validator(mode="after")
    def coherent_measurement(self) -> Self:
        numeric = self.value is not None and self.baseline is not None
        windowed = self.window_start is not None and self.window_end is not None
        if self.status is SecurityMeasurementStatus.INSUFFICIENT:
            if (
                numeric
                or self.value is not None
                or self.baseline is not None
                or self.unit is not None
                or windowed
                or self.window_start is not None
                or self.window_end is not None
                or self.window_count != 0
                or self.evidence_refs
            ):
                raise ValueError(
                    "an insufficient security measurement cannot carry a numeric claim "
                    "or evidence window"
                )
            return self
        if (
            not numeric
            or self.unit is None
            or self.scope is None
            or not windowed
            or self.window_count < 1
            or not self.evidence_refs
        ):
            raise ValueError(
                "a measured security value requires scope, value, baseline, unit, "
                "a non-empty evidence window and evidence refs"
            )
        assert self.window_start is not None
        assert self.window_end is not None
        if self.window_end < self.window_start:
            raise ValueError("a security measurement window cannot move backwards")
        assert self.unit is not None
        assert self.value is not None
        assert self.baseline is not None
        if self.unit not in _FEATURE_UNITS[self.feature]:
            raise ValueError("the security measurement unit does not match its feature")
        if self.unit in _PROBABILITY_UNITS and (
            not 0.0 <= self.value <= 1.0 or not 0.0 <= self.baseline <= 1.0
        ):
            raise ValueError("ratio and score security measurements must stay within [0, 1]")
        if self.unit not in _PROBABILITY_UNITS and (self.value < 0.0 or self.baseline < 0.0):
            raise ValueError("entropy and variation measurements cannot be negative")
        return self


class SecurityCohort(ContractModel):
    """One suspect cohort explicitly named by public telemetry evidence."""

    cohort_id: Identifier
    request_count: int = Field(ge=1)
    window_start: UtcDatetime
    window_end: UtcDatetime
    measurements: tuple[SecurityMeasurement, ...] = Field(
        min_length=len(COHORT_FEATURE_ORDER),
        max_length=len(COHORT_FEATURE_ORDER),
    )
    evidence_refs: tuple[Identifier, ...] = Field(min_length=1)
    detail: HumanText

    @field_validator("evidence_refs")
    @classmethod
    def unique_cohort_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique(values, field_name="security cohort evidence refs")

    @model_validator(mode="after")
    def coherent_cohort(self) -> Self:
        if self.window_end < self.window_start:
            raise ValueError("a security cohort window cannot move backwards")
        features = tuple(measurement.feature for measurement in self.measurements)
        if features != COHORT_FEATURE_ORDER:
            raise ValueError("a cohort must cover every table feature in canonical order")
        if any(measurement.scope != self.cohort_id for measurement in self.measurements):
            raise ValueError("every cohort measurement must name the evidence-owned cohort id")
        for measurement in self.measurements:
            if measurement.status is SecurityMeasurementStatus.INSUFFICIENT:
                continue
            assert measurement.window_start is not None
            assert measurement.window_end is not None
            if (
                measurement.window_start < self.window_start
                or measurement.window_end > self.window_end
            ):
                raise ValueError("a cohort measurement must stay inside its evidence window")
        return self


class SecurityTimelineEvent(ContractModel):
    """One security-axis episode referenced by the exact incident assessment."""

    episode_id: Identifier
    kind: SymptomKind
    service: Identifier
    signal: SignalName
    status: EpisodeStatus
    opened_at: UtcDatetime
    last_breach_at: UtcDatetime
    closed_at: UtcDatetime | None
    peak_deformation_score: Probability
    breach_window_count: int = Field(ge=1)
    evidence_refs: tuple[Identifier, ...] = Field(min_length=1)
    detail: HumanText

    @field_validator("evidence_refs")
    @classmethod
    def unique_timeline_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique(values, field_name="security timeline evidence refs")

    @model_validator(mode="after")
    def coherent_timeline(self) -> Self:
        if self.kind not in {SymptomKind.RESIDUAL_EXCEED, SymptomKind.RATIO_DEFORM}:
            raise ValueError("a security timeline contains only residual or ratio evidence")
        if self.last_breach_at < self.opened_at:
            raise ValueError("a security episode cannot breach before it opens")
        if self.closed_at is not None and self.closed_at < self.last_breach_at:
            raise ValueError("a security episode cannot close before its last breach")
        return self


class SecurityMitigation(ContractModel):
    """Latest server-held surgical action plus independent cohort integrity."""

    state: ActionControlState | None
    plan_revision: int | None = Field(default=None, ge=1)
    rung_id: Identifier | None
    action_kind: ActionKind | None
    target_ref: Identifier | None
    estimated_blast_fraction: Probability | None
    updated_at: UtcDatetime | None
    protected_cohort_integrity: SecurityMeasurement
    detail: HumanText

    @model_validator(mode="after")
    def coherent_mitigation(self) -> Self:
        fields = (
            self.plan_revision,
            self.rung_id,
            self.action_kind,
            self.target_ref,
            self.estimated_blast_fraction,
            self.updated_at,
        )
        if (self.state is not None) != all(value is not None for value in fields):
            raise ValueError("mitigation safety fields travel together from one action control")
        if (
            self.protected_cohort_integrity.feature
            is not SecurityFeature.PROTECTED_COHORT_INTEGRITY
        ):
            raise ValueError("mitigation integrity must use the protected-cohort feature")
        return self


class SecuritySnapshot(ContractModel):
    """One evidence-only security projection for an unresolved incident revision."""

    incident_id: Identifier
    opened_at: UtcDatetime
    updated_at: UtcDatetime
    state: IncidentState
    honesty: Literal["REAL", "SIMULATED"]
    timeline: tuple[SecurityTimelineEvent, ...]
    measurements: tuple[SecurityMeasurement, ...] = Field(
        min_length=len(SECURITY_FEATURE_ORDER),
        max_length=len(SECURITY_FEATURE_ORDER),
    )
    suspect_cohorts: tuple[SecurityCohort, ...]
    decomposition: IncidentDecomposition
    mitigation: SecurityMitigation

    @model_validator(mode="after")
    def coherent_snapshot(self) -> Self:
        if self.updated_at < self.opened_at:
            raise ValueError("a security snapshot cannot predate its incident")
        features = tuple(measurement.feature for measurement in self.measurements)
        if features != SECURITY_FEATURE_ORDER:
            raise ValueError("security measurements must cover every feature in canonical order")
        timeline_ids = tuple(event.episode_id for event in self.timeline)
        ensure_unique(timeline_ids, field_name="security timeline episode ids")
        timeline_order = tuple(
            sorted(
                self.timeline,
                key=lambda event: (event.opened_at, event.episode_id),
            )
        )
        if self.timeline != timeline_order:
            raise ValueError("security timeline events must be ordered by event time and id")
        cohort_ids = tuple(cohort.cohort_id for cohort in self.suspect_cohorts)
        ensure_unique(cohort_ids, field_name="security cohort ids")
        if cohort_ids != tuple(sorted(cohort_ids)):
            raise ValueError("security cohorts must be ordered by evidence-owned id")
        return self


class SecurityResponse(ContractModel):
    """The latest unresolved security snapshot or an explicit absence."""

    status: Literal["ready", "empty", "degraded"]
    snapshot: SecuritySnapshot | None
    detail: HumanText | None

    @model_validator(mode="after")
    def coherent_response(self) -> Self:
        if self.status == "ready" and (self.snapshot is None or self.detail is not None):
            raise ValueError("a ready security response requires only a snapshot")
        if self.status in {"empty", "degraded"} and (
            self.snapshot is not None or self.detail is None
        ):
            raise ValueError("an unavailable security response requires only detail")
        return self
