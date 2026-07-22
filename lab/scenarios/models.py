"""Strict declarative contracts for real-testbed evaluation scenarios."""

from __future__ import annotations

from itertools import pairwise
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
type MeasuredOffsetSeconds = Annotated[float, Field(ge=0.0, le=3600.0)]
type SafeRate = Annotated[int, Field(ge=1, le=50)]
type SafeJourneyRate = Annotated[int, Field(ge=1, le=5)]
type SafeAttackRate = Annotated[int, Field(ge=1, le=20)]
type Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
type PositiveMultiplier = Annotated[float, Field(ge=1.0, le=20.0, allow_inf_nan=False)]
type FlagName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9]*$"
    ),
]
type VariantName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9%_.-]*$",
    ),
]
type SignalName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    ),
]
type ScoredSymptomKind = Literal[
    "RESIDUAL_EXCEED",
    "RATIO_DEFORM",
    "LOG_BURST",
    "EDGE_DEGRADED",
    "SATURATION",
    "DROP",
    "SILENCE",
]


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
    stimulus_id: Identifier | None = None
    start_offset_seconds: MeasuredOffsetSeconds
    end_offset_seconds: MeasuredOffsetSeconds

    @model_validator(mode="after")
    def forward_interval(self) -> Self:
        if self.end_offset_seconds <= self.start_offset_seconds:
            raise ValueError("label end offset must be after its start offset")
        return self


class SymptomLabelInterval(LabModel):
    label_id: Identifier
    kind: ScoredSymptomKind
    service: Identifier
    signal: SignalName
    stimulus_id: Identifier | None = None
    start_offset_seconds: MeasuredOffsetSeconds
    end_offset_seconds: MeasuredOffsetSeconds

    @model_validator(mode="after")
    def forward_interval(self) -> Self:
        if self.end_offset_seconds <= self.start_offset_seconds:
            raise ValueError("symptom label end offset must be after its start offset")
        return self


class FlagdStimulus(LabModel):
    stimulus_id: Identifier
    kind: Literal["flagd"]
    start_offset_seconds: OffsetSeconds
    duration_seconds: PositiveSeconds
    flag: FlagName
    variant: VariantName


class ChaosMeshStimulus(LabModel):
    stimulus_id: Identifier
    kind: Literal["chaos_mesh"]
    start_offset_seconds: OffsetSeconds
    duration_seconds: PositiveSeconds
    experiment: Identifier


class K6JourneyStimulus(LabModel):
    stimulus_id: Identifier
    kind: Literal["k6_journey"]
    start_offset_seconds: OffsetSeconds
    duration_seconds: PositiveSeconds
    journey: Literal["checkout"]
    rate_rps: SafeJourneyRate


class K6PathAttackStimulus(LabModel):
    stimulus_id: Identifier
    kind: Literal["k6_path_attack"]
    start_offset_seconds: OffsetSeconds
    duration_seconds: PositiveSeconds
    attack: Literal["single_path"]
    path: Literal["/"]
    rate_rps: SafeAttackRate


class K6RatePhaseStimulus(LabModel):
    stimulus_id: Identifier
    kind: Literal["k6_rate_phase"]
    start_offset_seconds: OffsetSeconds
    duration_seconds: PositiveSeconds
    rate_rps: SafeRate


type Stimulus = (
    FlagdStimulus
    | ChaosMeshStimulus
    | K6JourneyStimulus
    | K6PathAttackStimulus
    | K6RatePhaseStimulus
)


class ScenarioProfile(LabModel):
    version: Literal[1]
    scenario_id: Identifier
    description: HumanText
    honesty: Literal["SIMULATED"]
    telemetry: TelemetrySpec
    seeds: SeedSets
    load_phases: tuple[LoadPhase, ...] = Field(min_length=2)
    contexts: tuple[RelativeContext, ...] = ()
    stimuli: tuple[Stimulus, ...] = ()
    residual_labels: tuple[ResidualLabelInterval, ...] = ()
    symptom_labels: tuple[SymptomLabelInterval, ...] = ()

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
        stimulus_ids = [stimulus.stimulus_id for stimulus in self.stimuli]
        if len(stimulus_ids) != len(set(stimulus_ids)):
            raise ValueError("stimulus ids must be unique")
        label_ids = [label.label_id for label in self.residual_labels]
        label_ids.extend(label.label_id for label in self.symptom_labels)
        if len(label_ids) != len(set(label_ids)):
            raise ValueError("label ids must be unique")
        duration = self.duration_seconds
        for context in self.contexts:
            if context.start_offset_seconds + context.duration_seconds > duration:
                raise ValueError(f"context exceeds scenario duration: {context.context_id}")
        for residual_label in self.residual_labels:
            if residual_label.end_offset_seconds > duration:
                raise ValueError(f"label exceeds scenario duration: {residual_label.label_id}")
            if residual_label.stimulus_id is not None:
                stimulus = next(
                    (
                        item
                        for item in self.stimuli
                        if item.stimulus_id == residual_label.stimulus_id
                    ),
                    None,
                )
                if stimulus is None:
                    raise ValueError(
                        f"residual label references unknown stimulus: {residual_label.stimulus_id}"
                    )
                if (
                    residual_label.start_offset_seconds != stimulus.start_offset_seconds
                    or residual_label.end_offset_seconds
                    != stimulus.start_offset_seconds + stimulus.duration_seconds
                ):
                    raise ValueError(
                        "stimulus-backed residual label must match planned interval: "
                        f"{residual_label.label_id}"
                    )
        for symptom_label in self.symptom_labels:
            if symptom_label.end_offset_seconds > duration:
                raise ValueError(f"label exceeds scenario duration: {symptom_label.label_id}")
            if symptom_label.stimulus_id is not None:
                stimulus = next(
                    (
                        item
                        for item in self.stimuli
                        if item.stimulus_id == symptom_label.stimulus_id
                    ),
                    None,
                )
                if stimulus is None:
                    raise ValueError(
                        f"symptom label references unknown stimulus: {symptom_label.stimulus_id}"
                    )
                if (
                    symptom_label.start_offset_seconds != stimulus.start_offset_seconds
                    or symptom_label.end_offset_seconds
                    != stimulus.start_offset_seconds + stimulus.duration_seconds
                ):
                    raise ValueError(
                        "stimulus-backed label must match planned interval: "
                        f"{symptom_label.label_id}"
                    )
        by_target: dict[str, list[Stimulus]] = {}
        for stimulus in self.stimuli:
            if stimulus.start_offset_seconds + stimulus.duration_seconds > duration:
                raise ValueError(f"stimulus exceeds scenario duration: {stimulus.stimulus_id}")
            by_target.setdefault(stimulus_target(stimulus), []).append(stimulus)
        for target, stimuli in by_target.items():
            ordered = sorted(stimuli, key=lambda item: item.start_offset_seconds)
            for previous, current in pairwise(ordered):
                previous_end = previous.start_offset_seconds + previous.duration_seconds
                if current.start_offset_seconds < previous_end:
                    raise ValueError(f"overlapping stimuli for {target}")
        for stimulus in self.stimuli:
            if not isinstance(stimulus, K6PathAttackStimulus):
                continue
            attack_start = stimulus.start_offset_seconds
            attack_end = attack_start + stimulus.duration_seconds
            for phase_start, phase in _phase_offsets(self.load_phases):
                phase_end = phase_start + phase.duration_seconds
                if (
                    max(attack_start, phase_start) < min(attack_end, phase_end)
                    and stimulus.rate_rps + phase.rate_rps > 50
                ):
                    raise ValueError("combined primary and path-attack rate exceeds 50 rps")
        phase_offsets = _phase_offsets(self.load_phases)
        for stimulus in self.stimuli:
            if not isinstance(stimulus, K6RatePhaseStimulus):
                continue
            if not any(
                stimulus.start_offset_seconds == phase_start
                and stimulus.duration_seconds == phase.duration_seconds
                and stimulus.rate_rps == phase.rate_rps
                for phase_start, phase in phase_offsets
            ):
                raise ValueError("k6 rate-phase stimulus must exactly match one load phase")
        return self


def stimulus_target(stimulus: Stimulus) -> str:
    if isinstance(stimulus, FlagdStimulus):
        return f"flagd:{stimulus.flag}"
    if isinstance(stimulus, ChaosMeshStimulus):
        return f"chaos_mesh:{stimulus.experiment}"
    if isinstance(stimulus, K6JourneyStimulus):
        return f"k6_journey:{stimulus.journey}"
    if isinstance(stimulus, K6PathAttackStimulus):
        return f"k6_path_attack:{stimulus.path}"
    return "k6_rate_phase:primary"


def _phase_offsets(phases: tuple[LoadPhase, ...]) -> tuple[tuple[int, LoadPhase], ...]:
    offset = 0
    values: list[tuple[int, LoadPhase]] = []
    for phase in phases:
        values.append((offset, phase))
        offset += phase.duration_seconds
    return tuple(values)
