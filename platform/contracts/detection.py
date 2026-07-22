"""Contracts emitted by decomposition and deterministic detection."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Self

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


class EpisodeStatus(StrEnum):
    """Lifecycle state of an anti-flapping symptom episode."""

    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class SymptomEpisode(ContractModel):
    """A hysteresis-guarded lifecycle over repeated symptoms of one key.

    An episode groups the recurring symptoms of a single ``(kind, service,
    signal)`` into one durable incident-precursor. It opens only after a symptom
    persists (breach persistence + a score deadband) and closes only after it
    clears for long enough, so a momentary blip can never open one and a key can
    never flap. Raw ``Symptom`` evidence is untouched; an episode only points at
    it via the opening, peak and latest symptom ids.
    """

    episode_id: Identifier
    kind: SymptomKind
    service: Identifier
    signal: SignalName
    status: EpisodeStatus
    opened_ts: UtcDatetime
    confirmed_ts: UtcDatetime
    last_breach_ts: UtcDatetime
    closed_ts: UtcDatetime | None = None
    peak_score: Probability
    breach_tick_count: int = Field(ge=1)
    revision: int = Field(ge=1)
    opening_symptom_id: Identifier
    peak_symptom_id: Identifier
    latest_symptom_id: Identifier
    evidence_refs: tuple[Identifier, ...] = ()

    @field_validator("evidence_refs")
    @classmethod
    def unique_episode_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """The peak-moment evidence links are an ordered set."""
        return ensure_unique(values, field_name="evidence_refs")

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        """Enforce a monotone onset→confirm→last-breach→close timeline."""
        if self.confirmed_ts < self.opened_ts:
            raise ValueError("confirmed_ts must be greater than or equal to opened_ts")
        if self.last_breach_ts < self.confirmed_ts:
            raise ValueError("last_breach_ts must be greater than or equal to confirmed_ts")
        if self.status is EpisodeStatus.CLOSED:
            if self.closed_ts is None:
                raise ValueError("a closed episode must record closed_ts")
            if self.closed_ts < self.last_breach_ts:
                raise ValueError("closed_ts must be greater than or equal to last_breach_ts")
        elif self.closed_ts is not None:
            raise ValueError("an active episode must not record closed_ts")
        return self
