"""Strict declarative contracts for real-testbed evaluation scenarios."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

type Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[a-z0-9_-]+$"
    ),
]
type HumanText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]
type PositiveSeconds = Annotated[int, Field(ge=1, le=3600)]
type OffsetSeconds = Annotated[int, Field(ge=0, le=3600)]
type SafeRate = Annotated[int, Field(ge=1, le=50)]
type Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
type PositiveMultiplier = Annotated[float, Field(ge=1.0, le=20.0, allow_inf_nan=False)]


class LabModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    @field_validator("*", mode="before")
    @classmethod
    def freeze_yaml_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class SeedSets(LabModel):
    development: tuple[int, ...] = Field(min_length=1)
    held_out: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_seed_sets(self) -> Self:
        if len(self.development) != len(set(self.development)):
            raise ValueError("development seeds must be unique")
        if len(self.held_out) != len(set(self.held_out)):
            raise ValueError("held_out seeds must be unique")
        if set(self.development) & set(self.held_out):
            raise ValueError("development and held_out seeds must be disjoint")
        return self


class TelemetrySpec(LabModel):
    target: Literal["astronomy-shop/frontend-proxy"]
    source_service: Literal["frontend-proxy"]
    logical_service: Literal["frontend"]
    logical_signal: Literal["request_rate"]
    tick_seconds: Literal[2]


class LoadPhase(LabModel):
    name: Identifier
    duration_seconds: PositiveSeconds
    rate_rps: SafeRate


class RelativeContext(LabModel):
    context_id: Identifier
    name: HumanText
    event_type: Identifier
    source: Identifier
    honesty: Literal["REAL", "SIMULATED"]
    start_offset_seconds: OffsetSeconds
    duration_seconds: PositiveSeconds
    expected_delta: dict[str, PositiveMultiplier] = Field(min_length=1)
    trust_score: Probability


class ResidualLabelInterval(LabModel):
    label_id: Identifier
    start_offset_seconds: OffsetSeconds
    end_offset_seconds: PositiveSeconds

    @model_validator(mode="after")
    def forward_interval(self) -> Self:
        if self.end_offset_seconds <= self.start_offset_seconds:
            raise ValueError("label end offset must be after its start offset")
        return self


class ScenarioProfile(LabModel):
    version: Literal[1]
    scenario_id: Identifier
    description: HumanText
    honesty: Literal["SIMULATED"]
    telemetry: TelemetrySpec
    seeds: SeedSets
    load_phases: tuple[LoadPhase, ...] = Field(min_length=2)
    contexts: tuple[RelativeContext, ...] = ()
    residual_labels: tuple[ResidualLabelInterval, ...] = ()

    @property
    def duration_seconds(self) -> int:
        return sum(phase.duration_seconds for phase in self.load_phases)

    @model_validator(mode="after")
    def validate_ranges_and_names(self) -> Self:
        phase_names = [phase.name for phase in self.load_phases]
        if len(phase_names) != len(set(phase_names)):
            raise ValueError("load phase names must be unique")
        context_ids = [context.context_id for context in self.contexts]
        if len(context_ids) != len(set(context_ids)):
            raise ValueError("context ids must be unique")
        label_ids = [label.label_id for label in self.residual_labels]
        if len(label_ids) != len(set(label_ids)):
            raise ValueError("label ids must be unique")
        duration = self.duration_seconds
        for context in self.contexts:
            if context.start_offset_seconds + context.duration_seconds > duration:
                raise ValueError(f"context exceeds scenario duration: {context.context_id}")
        for label in self.residual_labels:
            if label.end_offset_seconds > duration:
                raise ValueError(f"label exceeds scenario duration: {label.label_id}")
        return self
