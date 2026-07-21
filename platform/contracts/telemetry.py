"""Normalized public-telemetry contracts."""

from __future__ import annotations

import re

from pydantic import Field, ValidationInfo, field_validator

from contracts._base import (
    ContractModel,
    FiniteFloat,
    Identifier,
    SignalName,
    UnitName,
    UtcDatetime,
    ensure_unique,
)

type TelemetryScalar = str | bool | int | float

_FORBIDDEN_ATTRIBUTE_KEYS = frozenset(
    {
        "attack_flag",
        "expected_action",
        "expected_verdict",
        "ground_truth",
        "injected_fault_id",
        "scenario_label",
    }
)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


class Observation(ContractModel):
    """One event-time telemetry value, containing evidence but never answer-key data."""

    observation_id: Identifier
    ts: UtcDatetime
    service: Identifier
    signal: SignalName
    value: FiniteFloat
    unit: UnitName = "1"
    attributes: dict[SignalName, TelemetryScalar] = Field(default_factory=dict)
    flow_refs: tuple[Identifier, ...] = ()
    log_refs: tuple[Identifier, ...] = ()
    trace_refs: tuple[Identifier, ...] = ()

    @field_validator("attributes")
    @classmethod
    def reject_ground_truth_attributes(
        cls, attributes: dict[str, TelemetryScalar]
    ) -> dict[str, TelemetryScalar]:
        """Keep scenario answer keys out of the normalized runtime contract."""
        forbidden = sorted(
            key for key in attributes if _normalized_key(key) in _FORBIDDEN_ATTRIBUTE_KEYS
        )
        if forbidden:
            names = ", ".join(forbidden)
            raise ValueError(f"ground-truth attributes are forbidden: {names}")
        return attributes

    @field_validator("flow_refs", "log_refs", "trace_refs")
    @classmethod
    def unique_references(cls, values: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        """Prevent duplicate evidence links inside one observation."""
        field_name = info.field_name or "references"
        return ensure_unique(values, field_name=field_name)
