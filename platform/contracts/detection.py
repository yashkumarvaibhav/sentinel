"""Contracts emitted by decomposition and deterministic detection."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Self

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


class DecompFrame(ContractModel):
    """Full-resolution split of observed telemetry into explained and residual parts."""

    frame_id: Identifier
    observation_id: Identifier
    ts: UtcDatetime
    service: Identifier
    signal: SignalName
    observed: FiniteFloat
    explained_base: FiniteFloat
    explained_event: FiniteFloat
    residual: FiniteFloat
    band_low: FiniteFloat
    band_high: FiniteFloat
    residual_score: Probability
    context_ids: tuple[Identifier, ...] = ()

    @field_validator("context_ids")
    @classmethod
    def unique_context_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """A context window contributes at most once to a frame."""
        return ensure_unique(values, field_name="context_ids")

    @model_validator(mode="after")
    def validate_decomposition(self) -> Self:
        """Enforce the product's numeric identity and a well-formed expected band."""
        if self.band_low > self.band_high:
            raise ValueError("band_low must be less than or equal to band_high")

        explained = self.explained_base + self.explained_event + self.residual
        if not math.isclose(self.observed, explained, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                "decomposition invariant failed: observed must equal "
                "explained_base + explained_event + residual"
            )
        return self


class SymptomKind(StrEnum):
    """Deterministic evidence categories understood by the decision plane."""

    RESIDUAL_EXCEED = "RESIDUAL_EXCEED"
    RATIO_DEFORM = "RATIO_DEFORM"
    LOG_BURST = "LOG_BURST"
    EDGE_DEGRADED = "EDGE_DEGRADED"
    SATURATION = "SATURATION"
    DEPLOY_MARKER = "DEPLOY_MARKER"
    DROP = "DROP"
    SILENCE = "SILENCE"


class Symptom(ContractModel):
    """One scored, human-legible piece of evidence about abnormal behavior."""

    symptom_id: Identifier
    kind: SymptomKind
    service: Identifier
    signal: SignalName
    onset_ts: UtcDatetime
    score: Probability
    note: HumanText
    evidence_refs: tuple[Identifier, ...] = ()

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Avoid counting the same evidence link twice downstream."""
        return ensure_unique(values, field_name="evidence_refs")
