"""Contracts emitted by the context intelligence plane."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from contracts._base import (
    ContractModel,
    FiniteFloat,
    HumanText,
    Identifier,
    Probability,
    SignalName,
    UtcDatetime,
)


class ContextWindow(ContractModel):
    """A trusted external event and the signals whose volume it may explain."""

    context_id: Identifier
    name: HumanText
    event_type: Identifier
    source: Identifier
    valid_from: UtcDatetime
    valid_to: UtcDatetime
    expected_delta: dict[SignalName, FiniteFloat] = Field(min_length=1)
    trust_score: Probability

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        """A context must cover a real, forward-moving event-time interval."""
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self
