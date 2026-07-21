"""Strict public manifest for raw-bus capture boundaries."""

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
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
type Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
type RelativePath = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1024),
]
type RawTopic = Literal["otlp.raw.metrics", "otlp.raw.logs", "otlp.raw.traces"]


class CaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    @field_validator("*", mode="before")
    @classmethod
    def freeze_json_lists(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class RawRecordDescriptor(CaptureModel):
    offset: int = Field(ge=0)
    path: RelativePath
    size_bytes: int = Field(ge=1)
    sha256: Sha256


class TopicCapture(CaptureModel):
    topic: RawTopic
    partition: int = Field(ge=0)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    records: tuple[RawRecordDescriptor, ...]

    @model_validator(mode="after")
    def contiguous_offsets(self) -> Self:
        if self.end_offset < self.start_offset:
            raise ValueError("topic end_offset must not precede start_offset")
        actual = tuple(record.offset for record in self.records)
        expected = tuple(range(self.start_offset, self.end_offset))
        if actual != expected:
            raise ValueError("raw capture offsets must be contiguous and cover the declared bounds")
        return self


class PublicArtifact(CaptureModel):
    name: Identifier
    path: RelativePath
    sha256: Sha256


class PrivateArtifact(CaptureModel):
    path: RelativePath
    sha256: Sha256


class CaptureTelemetry(CaptureModel):
    target: Identifier
    source_service: Identifier
    logical_service: Identifier
    logical_signal: Identifier
    tick_seconds: int = Field(ge=1, le=60)


class CaptureManifest(CaptureModel):
    version: Literal[1]
    capture_id: Identifier
    scenario_id: Identifier
    seed: int
    seed_purpose: Literal["held_out"]
    telemetry_honesty: Literal["REAL"]
    stimulus_honesty: Literal["SIMULATED"]
    config_fingerprint: Sha256
    correlation_user_agent: Identifier
    anchor_user_agent: Identifier
    telemetry: CaptureTelemetry
    topics: tuple[TopicCapture, ...] = Field(min_length=1)
    schedule: PublicArtifact
    context_feed: PublicArtifact
    enrichments: tuple[PublicArtifact, ...]
    private_labels: PrivateArtifact

    @model_validator(mode="after")
    def unique_sources_and_artifacts(self) -> Self:
        sources = [(topic.topic, topic.partition) for topic in self.topics]
        if sources != sorted(sources) or len(sources) != len(set(sources)):
            raise ValueError("capture topics must be unique and sorted")
        names = [artifact.name for artifact in self.enrichments]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("capture enrichments must be unique and sorted")
        return self
