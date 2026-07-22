"""Label-free raw capture replay through rate reconstruction and decomposition."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import Field, model_validator

from common.config import DetectorConfig
from contracts import ContextWindow, DecompFrame, Observation
from detection.decompose import (
    DecompositionEngine,
    DecompositionStats,
    DecompositionStatus,
)
from lab.captures.models import CaptureModel
from lab.captures.replay import RawReplay, replay_raw
from lab.captures.store import RuntimeCapture
from lab.scenarios.models import K6PathAttackStimulus, RelativeContext, Stimulus


class CapturedLoadPhase(CaptureModel):
    name: str = Field(min_length=1, max_length=128)
    start_offset_seconds: int = Field(ge=0, le=3600)
    duration_seconds: int = Field(ge=1, le=3600)
    rate_rps: int = Field(ge=1, le=50)


class CapturedSchedule(CaptureModel):
    version: Literal[1]
    scenario_id: str = Field(min_length=1, max_length=128)
    honesty: Literal["SIMULATED"]
    seed: int
    seed_purpose: Literal["development", "held_out"]
    request_mix_seed: int = Field(ge=1)
    target: Literal["astronomy-shop/frontend-proxy"]
    phases: tuple[CapturedLoadPhase, ...] = Field(min_length=2)
    stimuli: tuple[Stimulus, ...] = ()

    @model_validator(mode="after")
    def contiguous_phases(self) -> Self:
        offset = 0
        names: set[str] = set()
        for phase in self.phases:
            if phase.name in names:
                raise ValueError(f"captured phase names must be unique: {phase.name}")
            if phase.start_offset_seconds != offset:
                raise ValueError("captured phases must be contiguous and start at zero")
            names.add(phase.name)
            offset += phase.duration_seconds
        stimulus_ids: set[str] = set()
        for stimulus in self.stimuli:
            if stimulus.stimulus_id in stimulus_ids:
                raise ValueError(f"captured stimulus IDs must be unique: {stimulus.stimulus_id}")
            if stimulus.start_offset_seconds + stimulus.duration_seconds > offset:
                raise ValueError(f"captured stimulus exceeds schedule: {stimulus.stimulus_id}")
            stimulus_ids.add(stimulus.stimulus_id)
        return self

    @property
    def duration_seconds(self) -> int:
        return sum(item.duration_seconds for item in self.phases)

    @property
    def expected_request_count(self) -> int:
        primary = sum(item.duration_seconds * item.rate_rps for item in self.phases)
        attack = sum(
            item.duration_seconds * item.rate_rps
            for item in self.stimuli
            if isinstance(item, K6PathAttackStimulus)
        )
        return primary + attack


class CapturedContextFeed(CaptureModel):
    version: Literal[1]
    scenario_id: str = Field(min_length=1, max_length=128)
    honesty: Literal["SIMULATED"]
    windows: tuple[RelativeContext, ...]


@dataclass(frozen=True)
class ReplayStep:
    observation: Observation
    status: DecompositionStatus
    baseline_updated: bool
    frame: DecompFrame | None

    def canonical_value(self) -> dict[str, object]:
        return {
            "baseline_updated": self.baseline_updated,
            "frame": None if self.frame is None else self.frame.model_dump(mode="json"),
            "observation": self.observation.model_dump(mode="json"),
            "status": self.status,
        }


@dataclass(frozen=True)
class DecompositionReplay:
    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    capture_config_fingerprint: str
    replay_config_fingerprint: str
    raw_replay_sha256: str
    raw_dead_letters: tuple[str, ...]
    anchor_ts: datetime
    expected_span_count: int
    actual_span_count: int
    telemetry_completeness: float
    contexts: tuple[ContextWindow, ...]
    steps: tuple[ReplayStep, ...]
    stats: DecompositionStats

    def canonical_bytes(self) -> bytes:
        value = {
            "actual_span_count": self.actual_span_count,
            "anchor_ts": self.anchor_ts.isoformat(),
            "capture_config_fingerprint": self.capture_config_fingerprint,
            "capture_id": self.capture_id,
            "contexts": [item.model_dump(mode="json") for item in self.contexts],
            "expected_span_count": self.expected_span_count,
            "honesty": {"stimulus": "SIMULATED", "telemetry": "REAL"},
            "raw_dead_letters": self.raw_dead_letters,
            "raw_replay_sha256": self.raw_replay_sha256,
            "replay_config_fingerprint": self.replay_config_fingerprint,
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "stats": {
                "baseline_updates": self.stats.baseline_updates,
                "context_blocked_warmup": self.stats.context_blocked_warmup,
                "decomposed": self.stats.decomposed,
                "replays": self.stats.replays,
                "warming": self.stats.warming,
            },
            "steps": [item.canonical_value() for item in self.steps],
            "telemetry_completeness": self.telemetry_completeness,
            "version": 1,
        }
        return (
            json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()


def replay_decomposition(
    capture: RuntimeCapture,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> DecompositionReplay:
    manifest = capture.manifest
    schedule = CapturedSchedule.model_validate_json(capture.schedule)
    context_feed = CapturedContextFeed.model_validate_json(capture.context_feed)
    if (
        schedule.scenario_id != manifest.scenario_id
        or schedule.seed != manifest.seed
        or schedule.seed_purpose != manifest.seed_purpose
        or context_feed.scenario_id != manifest.scenario_id
    ):
        raise ValueError("captured schedule/context identity does not match manifest")
    if schedule.target != manifest.telemetry.target:
        raise ValueError("captured schedule target does not match telemetry routing")

    raw = replay_raw(capture)
    spans, anchors = _scenario_spans(capture, raw)
    if not spans:
        raise ValueError("capture contains no correlated ingress spans")
    if not anchors:
        raise ValueError("capture contains no scenario anchor spans")
    anchor_ts = max(item.ts for item in anchors)
    if anchor_ts > min(item.ts for item in spans):
        raise ValueError("captured scenario anchor is after its first correlated span")
    observations = _rate_observations(
        capture=capture,
        schedule=schedule,
        anchor_ts=anchor_ts,
        span_timestamps=tuple(item.ts for item in spans),
    )
    contexts = _context_windows(
        context_feed=context_feed,
        anchor_ts=anchor_ts,
        duration_seconds=schedule.duration_seconds,
    )
    engine = DecompositionEngine(configuration=detector, dedup_capacity=len(observations) + 1)
    steps: list[ReplayStep] = []
    for observation in observations:
        result = engine.decompose(observation, contexts=contexts)
        steps.append(
            ReplayStep(
                observation=observation,
                status=result.status,
                baseline_updated=result.baseline_updated,
                frame=result.frame,
            )
        )
    raw_bytes = raw.canonical_bytes()
    return DecompositionReplay(
        capture_id=manifest.capture_id,
        scenario_id=manifest.scenario_id,
        seed=manifest.seed,
        seed_purpose=manifest.seed_purpose,
        capture_config_fingerprint=manifest.config_fingerprint,
        replay_config_fingerprint=replay_config_fingerprint,
        raw_replay_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        raw_dead_letters=raw.dead_letters,
        anchor_ts=anchor_ts,
        expected_span_count=schedule.expected_request_count,
        actual_span_count=len(spans),
        telemetry_completeness=len(spans) / schedule.expected_request_count,
        contexts=contexts,
        steps=tuple(steps),
        stats=engine.stats(),
    )


def _scenario_spans(
    capture: RuntimeCapture,
    raw: RawReplay,
) -> tuple[tuple[Observation, ...], tuple[Observation, ...]]:
    telemetry = capture.manifest.telemetry
    ingress = tuple(
        item
        for item in raw.observations
        if item.service == telemetry.source_service
        and item.signal == "span.duration_ms"
        and item.attributes.get("span.kind") == 2
    )
    spans = tuple(
        sorted(
            (
                item
                for item in ingress
                if _matches_correlation_user_agent(
                    item.attributes.get("user_agent"),
                    capture.manifest.correlation_user_agent,
                )
            ),
            key=lambda item: (item.ts, item.observation_id),
        )
    )
    anchors = tuple(
        sorted(
            (
                item
                for item in ingress
                if item.attributes.get("user_agent") == capture.manifest.anchor_user_agent
            ),
            key=lambda item: (item.ts, item.observation_id),
        )
    )
    return spans, anchors


def _matches_correlation_user_agent(value: object, root: str) -> bool:
    return isinstance(value, str) and (value == root or value.startswith(f"{root}/"))


def _rate_observations(
    *,
    capture: RuntimeCapture,
    schedule: CapturedSchedule,
    anchor_ts: datetime,
    span_timestamps: tuple[datetime, ...],
) -> tuple[Observation, ...]:
    telemetry = capture.manifest.telemetry
    tick_seconds = telemetry.tick_seconds
    counts = [0] * math.ceil(schedule.duration_seconds / tick_seconds)
    for timestamp in span_timestamps:
        offset = math.floor((timestamp - anchor_ts).total_seconds() / tick_seconds)
        if 0 <= offset < len(counts):
            counts[offset] += 1
    return tuple(
        Observation(
            observation_id=hashlib.sha256(
                f"{capture.manifest.capture_id}:{schedule.scenario_id}:{index}".encode()
            ).hexdigest(),
            ts=anchor_ts + timedelta(seconds=index * tick_seconds),
            service=telemetry.logical_service,
            signal=telemetry.logical_signal,
            value=count / tick_seconds,
            unit="requests/s",
            attributes={
                "evidence.source": "captured_real_otel_ingress_spans",
                "evidence.service": telemetry.source_service,
            },
        )
        for index, count in enumerate(counts)
        if count > 0
    )


def _context_windows(
    *,
    context_feed: CapturedContextFeed,
    anchor_ts: datetime,
    duration_seconds: int,
) -> tuple[ContextWindow, ...]:
    contexts: list[ContextWindow] = []
    for item in context_feed.windows:
        if item.start_offset_seconds + item.duration_seconds > duration_seconds:
            raise ValueError(f"captured context exceeds schedule: {item.context_id}")
        contexts.append(
            ContextWindow(
                context_id=item.context_id,
                name=item.name,
                event_type=item.event_type,
                source=item.source,
                honesty=item.honesty,
                valid_from=anchor_ts + timedelta(seconds=item.start_offset_seconds),
                valid_to=anchor_ts
                + timedelta(seconds=item.start_offset_seconds + item.duration_seconds),
                expected_delta=item.expected_delta,
                trust_score=item.trust_score,
            )
        )
    return tuple(sorted(contexts, key=lambda item: item.context_id))
