"""Run deterministic decomposition first, then score it against private labels."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from common.config import DetectorConfig
from contracts import ContextWindow, Observation
from detection.decompose import DecompositionEngine
from lab.scenarios.compiler import ScenarioArtifacts
from lab.scenarios.models import ResidualLabelInterval, ScenarioProfile
from lab.scoring.metrics import BinaryMetrics, binary_metrics, nearest_rank


@dataclass(frozen=True)
class RunScore:
    scenario_id: str
    seed: int
    seed_purpose: str
    sample_count: int
    expected_span_count: int
    actual_span_count: int
    telemetry_completeness: float
    predicted: tuple[bool, ...]
    expected: tuple[bool, ...]
    metrics: BinaryMetrics
    detection_latency_seconds: tuple[float, ...]

    @property
    def detection_latency_p50(self) -> float | None:
        return nearest_rank(self.detection_latency_seconds, percentile=0.5).value

    @property
    def detection_latency_p95(self) -> float | None:
        return nearest_rank(self.detection_latency_seconds, percentile=0.95).value


def build_rate_observations(
    *,
    profile: ScenarioProfile,
    run_id: str,
    span_timestamps: tuple[datetime, ...],
    start_at: datetime | None = None,
) -> tuple[Observation, ...]:
    """Count real ingress spans into fixed ticks anchored at the first evidence timestamp."""
    if not span_timestamps:
        raise ValueError("live run produced no ingress span evidence")
    if any(timestamp.utcoffset() != timedelta(0) for timestamp in span_timestamps):
        raise ValueError("span timestamps must be timezone-aware UTC")
    ordered = tuple(sorted(timestamp.astimezone(UTC) for timestamp in span_timestamps))
    start = ordered[0] if start_at is None else start_at.astimezone(UTC)
    if start > ordered[0]:
        raise ValueError("evidence start cannot be after the first span")
    tick_seconds = profile.telemetry.tick_seconds
    counts = [0] * math.ceil(profile.duration_seconds / tick_seconds)
    for timestamp in ordered:
        offset = math.floor((timestamp - start).total_seconds() / tick_seconds)
        if 0 <= offset < len(counts):
            counts[offset] += 1
    return tuple(
        Observation(
            observation_id=hashlib.sha256(
                f"{run_id}:{profile.scenario_id}:{index}".encode()
            ).hexdigest(),
            ts=start + timedelta(seconds=index * tick_seconds),
            service=profile.telemetry.logical_service,
            signal=profile.telemetry.logical_signal,
            value=count / tick_seconds,
            unit="requests/s",
            attributes={
                "evidence.source": "real_otel_ingress_spans",
                "evidence.service": profile.telemetry.source_service,
            },
        )
        for index, count in enumerate(counts)
    )


def score_observations(
    *,
    profile: ScenarioProfile,
    artifacts: ScenarioArtifacts,
    observations: tuple[Observation, ...],
    detector: DetectorConfig,
    expected_span_count: int,
    actual_span_count: int | None = None,
) -> RunScore:
    if not observations:
        raise ValueError("cannot score an empty observation stream")
    if expected_span_count < 1:
        raise ValueError("expected_span_count must be positive")
    start = observations[0].ts
    contexts = tuple(
        ContextWindow(
            context_id=context.context_id,
            name=context.name,
            event_type=context.event_type,
            source=context.source,
            honesty=context.honesty,
            valid_from=start + timedelta(seconds=context.start_offset_seconds),
            valid_to=start
            + timedelta(seconds=context.start_offset_seconds + context.duration_seconds),
            expected_delta=context.expected_delta,
            trust_score=context.trust_score,
        )
        for context in profile.contexts
    )
    engine = DecompositionEngine(configuration=detector, dedup_capacity=len(observations) + 1)
    predicted: list[bool] = []
    scored_offsets: list[float] = []
    predicted_offsets: list[float] = []
    for observation in observations:
        result = engine.decompose(observation, contexts=contexts)
        if result.frame is None:
            continue
        offset = (observation.ts - start).total_seconds()
        prediction = result.frame.residual_score > 0.0
        scored_offsets.append(offset)
        predicted.append(prediction)
        if prediction:
            predicted_offsets.append(offset)
    labels = _private_labels(artifacts)
    expected = [_is_labeled(offset, labels) for offset in scored_offsets]
    latencies = tuple(
        latency
        for label in labels
        if (
            latency := _first_detection_latency(
                predicted_offsets,
                start_offset=label.start_offset_seconds,
                end_offset=label.end_offset_seconds,
            )
        )
        is not None
    )
    if actual_span_count is None:
        actual_span_count = round(
            sum(observation.value * profile.telemetry.tick_seconds for observation in observations)
        )
    return RunScore(
        scenario_id=profile.scenario_id,
        seed=artifacts.schedule.seed,
        seed_purpose=artifacts.schedule.seed_purpose.value,
        sample_count=len(predicted),
        expected_span_count=expected_span_count,
        actual_span_count=actual_span_count,
        telemetry_completeness=actual_span_count / expected_span_count,
        predicted=tuple(predicted),
        expected=tuple(expected),
        metrics=binary_metrics(predicted=tuple(predicted), expected=tuple(expected)),
        detection_latency_seconds=latencies,
    )


def _private_labels(artifacts: ScenarioArtifacts) -> tuple[ResidualLabelInterval, ...]:
    raw = artifacts.labels.get("intervals")
    if not isinstance(raw, list):
        raise ValueError("private labels intervals must be a list")
    try:
        return tuple(ResidualLabelInterval.model_validate(item) for item in raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("private labels are invalid") from exc


def _is_labeled(offset: float, labels: tuple[ResidualLabelInterval, ...]) -> bool:
    return any(label.start_offset_seconds <= offset < label.end_offset_seconds for label in labels)


def _first_detection_latency(
    predicted_offsets: list[float],
    *,
    start_offset: int,
    end_offset: int,
) -> float | None:
    first = next(
        (offset for offset in predicted_offsets if start_offset <= offset < end_offset),
        None,
    )
    return None if first is None else first - start_offset
