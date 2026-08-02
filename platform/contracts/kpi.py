"""Public reliability KPI and held-out score-proof contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from contracts._base import (
    ContractModel,
    FiniteFloat,
    HumanText,
    Identifier,
    UnitName,
    UtcDatetime,
    ensure_unique,
)

DETECTION_LATENCY_HEADLINE_KEY = "detection_latency_p95_seconds"


class KpiStatus(StrEnum):
    """Whether a number was actually measured."""

    OK = "ok"
    INSUFFICIENT = "insufficient"


class KpiKey(StrEnum):
    """The four reliability questions on the command centre."""

    DETECTION_LATENCY = "detection_latency"
    AUTONOMOUS_MTTR = "autonomous_mttr"
    QUIET_DAY_FALSE_ACTS = "quiet_day_false_acts"
    PROTECTED_COHORT_INTEGRITY = "protected_cohort_integrity"


class KpiWindow(ContractModel):
    """The evidence interval behind a KPI, or why no interval exists."""

    start: UtcDatetime | None
    end: UtcDatetime | None
    description: HumanText

    @model_validator(mode="after")
    def coherent_bounds(self) -> Self:
        if (self.start is None) != (self.end is None):
            raise ValueError("KPI window start and end must both be present or both be absent")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("KPI window end must be at or after its start")
        return self


class KpiMetric(ContractModel):
    """One reliability KPI with enough provenance to interpret it."""

    key: KpiKey
    label: HumanText
    definition: HumanText
    status: KpiStatus
    value: FiniteFloat | None
    unit: UnitName
    window: KpiWindow
    sample_count: int = Field(ge=0)
    provenance: HumanText

    @model_validator(mode="after")
    def value_matches_status(self) -> Self:
        if self.status is KpiStatus.OK and (self.value is None or self.sample_count < 1):
            raise ValueError("an ok KPI requires a measured value and at least one sample")
        if self.status is KpiStatus.INSUFFICIENT and self.value is not None:
            raise ValueError("an insufficient KPI cannot carry a value")
        return self


class ScoreHeadline(ContractModel):
    """One compact metric from a score-proof artifact."""

    key: Identifier
    label: HumanText
    status: KpiStatus
    value: FiniteFloat | None
    unit: UnitName
    sample_count: int = Field(ge=0)

    @model_validator(mode="after")
    def value_matches_status(self) -> Self:
        if self.status is KpiStatus.OK and (self.value is None or self.sample_count < 1):
            raise ValueError("an ok score headline requires a value and samples")
        if self.status is KpiStatus.INSUFFICIENT and self.value is not None:
            raise ValueError("an insufficient score headline cannot carry a value")
        return self


class ScoreProof(ContractModel):
    """Immutable output of one held-out scoring invocation."""

    version: Literal[1]
    proof_id: Identifier
    gate_status: Literal["pass", "fail"]
    evidence_start: UtcDatetime
    evidence_end: UtcDatetime
    telemetry_honesty: Literal["REAL"]
    stimulus_honesty: Literal["SIMULATED"]
    seed_purpose: Literal["held_out"]
    capture_ids: tuple[Identifier, ...] = Field(min_length=1)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_path: Identifier
    headline_metrics: tuple[ScoreHeadline, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_proof(self) -> Self:
        if self.evidence_end < self.evidence_start:
            raise ValueError("score-proof evidence end must be at or after its start")
        ensure_unique(self.capture_ids, field_name="capture_ids")
        ensure_unique(
            tuple(metric.key for metric in self.headline_metrics),
            field_name="headline metric keys",
        )
        return self


class ReliabilityMetricEvidence(ContractModel):
    """One measured KPI claim from an executable evidence reader.

    This artifact is deliberately narrower than ``KpiMetric``: it carries only
    measurements. Missing readers stay absent and the API turns their absence
    into the typed insufficient state; a proof file cannot smuggle in a zero by
    calling it unavailable.
    """

    key: Literal[KpiKey.AUTONOMOUS_MTTR, KpiKey.QUIET_DAY_FALSE_ACTS]
    value: FiniteFloat = Field(ge=0.0)
    unit: UnitName
    evidence_start: UtcDatetime
    evidence_end: UtcDatetime
    sample_count: int = Field(ge=1)
    evidence_kind: Literal["held_out_decision_replay", "contained_real_testbed_action"]
    source_ids: tuple[Identifier, ...] = Field(min_length=1)
    telemetry_honesty: Literal["REAL"]
    provenance: HumanText

    @model_validator(mode="after")
    def coherent_evidence(self) -> Self:
        if self.evidence_end < self.evidence_start:
            raise ValueError("reliability evidence end must be at or after its start")
        ensure_unique(self.source_ids, field_name="reliability evidence source IDs")
        expected_unit = {
            KpiKey.AUTONOMOUS_MTTR: "seconds",
            KpiKey.QUIET_DAY_FALSE_ACTS: "actions",
        }[KpiKey(self.key)]
        if self.unit != expected_unit:
            raise ValueError(f"{self.key} reliability evidence must use {expected_unit}")
        return self


class ReliabilityProof(ContractModel):
    """Versioned proof for KPI readers that landed after the symptom score."""

    version: Literal[1]
    proof_id: Identifier
    gate_status: Literal["pass", "fail"]
    metrics: tuple[ReliabilityMetricEvidence, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique_supported_metrics(self) -> Self:
        ensure_unique(
            tuple(str(metric.key) for metric in self.metrics),
            field_name="reliability proof metric keys",
        )
        return self


class KpiResponse(ContractModel):
    """The command centre's one typed reliability snapshot."""

    status: Literal["ready", "degraded"]
    metrics: tuple[KpiMetric, ...]
    latest_score_proof: ScoreProof | None
    detail: HumanText | None = None

    @model_validator(mode="after")
    def exact_metric_set(self) -> Self:
        keys = tuple(metric.key for metric in self.metrics)
        if keys != tuple(KpiKey):
            raise ValueError(
                "KPI response metrics must contain the four KPI keys once in canonical order"
            )
        if self.status == "ready" and self.latest_score_proof is None:
            raise ValueError("a ready KPI response requires a score proof")
        if self.status == "degraded" and self.detail is None:
            raise ValueError("a degraded KPI response requires detail")
        return self
