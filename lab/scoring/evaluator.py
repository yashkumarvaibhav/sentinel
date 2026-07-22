"""Run deterministic decomposition first, then score it against private labels."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from common.config import DetectorConfig
from contracts import ContextWindow, Observation, SymptomEpisode, SymptomKind
from detection.decompose import DecompositionEngine
from lab.scenarios.compiler import ScenarioArtifacts
from lab.scenarios.models import ResidualLabelInterval, ScenarioProfile, SymptomLabelInterval
from lab.scoring.metrics import (
    BinaryMetrics,
    EpisodeMetrics,
    binary_metrics,
    episode_metrics,
    nearest_rank,
)

SCORED_SYMPTOM_KINDS = (
    SymptomKind.RESIDUAL_EXCEED,
    SymptomKind.RATIO_DEFORM,
    SymptomKind.LOG_BURST,
    SymptomKind.EDGE_DEGRADED,
    SymptomKind.SATURATION,
    SymptomKind.DROP,
    SymptomKind.SILENCE,
)


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


@dataclass(frozen=True)
class EpisodeMatch:
    """One deterministic one-to-one prediction/label pairing."""

    episode_id: str
    label_id: str
    detection_latency_seconds: float


@dataclass(frozen=True)
class SymptomKindScore:
    """Episode-level metrics for one symptom kind in one capture."""

    kind: SymptomKind
    predicted_count: int
    expected_count: int
    metrics: EpisodeMetrics
    matches: tuple[EpisodeMatch, ...]

    @property
    def detection_latency_p50(self) -> float | None:
        samples = tuple(item.detection_latency_seconds for item in self.matches)
        return nearest_rank(samples, percentile=0.5).value

    @property
    def detection_latency_p95(self) -> float | None:
        samples = tuple(item.detection_latency_seconds for item in self.matches)
        return nearest_rank(samples, percentile=0.95).value


@dataclass(frozen=True)
class EpisodeRunScore:
    """All scored deterministic symptom kinds for one completed replay."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    evaluation_start_ts: datetime
    evaluation_end_ts: datetime
    by_kind: tuple[SymptomKindScore, ...]

    def score_for(self, kind: SymptomKind) -> SymptomKindScore:
        """Return one kind's explicit score, including its insufficient state."""
        return next(item for item in self.by_kind if item.kind is kind)


@dataclass(frozen=True)
class _ExpectedEpisode:
    label_id: str
    kind: SymptomKind
    service: str
    signal: str
    start_ts: datetime
    end_ts: datetime


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


def score_symptom_episodes(
    *,
    capture_id: str,
    scenario_id: str,
    seed: int,
    seed_purpose: Literal["development", "held_out"],
    anchor_ts: datetime,
    evaluation_end_ts: datetime,
    episodes: tuple[SymptomEpisode, ...],
    labels: tuple[SymptomLabelInterval, ...],
) -> EpisodeRunScore:
    """Score final episode identities against private half-open label intervals.

    Runtime replay must finish before this scorer is called. Repeated revisions of
    one durable episode collapse to its latest revision; distinct predictions are
    matched one-to-one, so several alerts cannot claim the same ground-truth event.
    """
    anchor = _utc(anchor_ts, name="anchor_ts")
    evaluation_end = _utc(evaluation_end_ts, name="evaluation_end_ts")
    if evaluation_end <= anchor:
        raise ValueError("evaluation_end_ts must be after anchor_ts")
    latest_episodes = _latest_episode_revisions(episodes)
    _validate_episode_bounds(latest_episodes, start=anchor, end=evaluation_end)
    expected = tuple(
        _ExpectedEpisode(
            label_id=label.label_id,
            kind=SymptomKind(label.kind),
            service=label.service,
            signal=label.signal,
            start_ts=anchor + timedelta(seconds=label.start_offset_seconds),
            end_ts=anchor + timedelta(seconds=label.end_offset_seconds),
        )
        for label in labels
    )
    if any(item.end_ts > evaluation_end for item in expected):
        raise ValueError("symptom label exceeds the completed replay interval")

    scores: list[SymptomKindScore] = []
    for kind in SCORED_SYMPTOM_KINDS:
        predicted_for_kind = tuple(item for item in latest_episodes if item.kind is kind)
        expected_for_kind = tuple(item for item in expected if item.kind is kind)
        matches = _match_episodes(
            predicted_for_kind,
            expected_for_kind,
            evaluation_end=evaluation_end,
        )
        scores.append(
            SymptomKindScore(
                kind=kind,
                predicted_count=len(predicted_for_kind),
                expected_count=len(expected_for_kind),
                metrics=episode_metrics(
                    matched_count=len(matches),
                    predicted_count=len(predicted_for_kind),
                    expected_count=len(expected_for_kind),
                ),
                matches=matches,
            )
        )
    return EpisodeRunScore(
        capture_id=capture_id,
        scenario_id=scenario_id,
        seed=seed,
        seed_purpose=seed_purpose,
        evaluation_start_ts=anchor,
        evaluation_end_ts=evaluation_end,
        by_kind=tuple(scores),
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


def _latest_episode_revisions(
    episodes: tuple[SymptomEpisode, ...],
) -> tuple[SymptomEpisode, ...]:
    latest: dict[str, SymptomEpisode] = {}
    for episode in episodes:
        if episode.kind not in SCORED_SYMPTOM_KINDS:
            raise ValueError(f"unscored symptom kind in episode predictions: {episode.kind.value}")
        previous = latest.get(episode.episode_id)
        if previous is None:
            latest[episode.episode_id] = episode
            continue
        if _episode_identity(previous) != _episode_identity(episode):
            raise ValueError("episode_id was reused with a different episode identity")
        if previous.revision == episode.revision and previous != episode:
            raise ValueError("episode revision has conflicting payloads")
        if episode.revision > previous.revision:
            latest[episode.episode_id] = episode
    return tuple(
        sorted(
            latest.values(),
            key=lambda item: (item.kind.value, item.opened_ts, item.episode_id),
        )
    )


def _episode_identity(episode: SymptomEpisode) -> tuple[object, ...]:
    return (
        episode.episode_id,
        episode.kind,
        episode.service,
        episode.signal,
        episode.opened_ts,
        episode.confirmed_ts,
        episode.opening_symptom_id,
    )


def _validate_episode_bounds(
    episodes: tuple[SymptomEpisode, ...],
    *,
    start: datetime,
    end: datetime,
) -> None:
    for episode in episodes:
        predicted_end = episode.closed_ts or end
        if episode.opened_ts < start or predicted_end > end:
            raise ValueError("episode prediction exceeds the completed replay interval")
        if predicted_end <= episode.opened_ts:
            raise ValueError("episode prediction interval must be non-empty")
        if episode.last_breach_ts > end:
            raise ValueError("episode evidence exceeds the completed replay interval")


def _match_episodes(
    episodes: tuple[SymptomEpisode, ...],
    labels: tuple[_ExpectedEpisode, ...],
    *,
    evaluation_end: datetime,
) -> tuple[EpisodeMatch, ...]:
    ordered_predictions = tuple(
        sorted(episodes, key=lambda item: (item.opened_ts, item.episode_id))
    )
    ordered_labels = tuple(sorted(labels, key=lambda item: (item.start_ts, item.label_id)))
    candidates: tuple[tuple[int, ...], ...] = tuple(
        tuple(
            sorted(
                (
                    index
                    for index, label in enumerate(ordered_labels)
                    if _can_match(episode, label, evaluation_end=evaluation_end)
                ),
                key=lambda index: (
                    abs((episode.opened_ts - ordered_labels[index].start_ts).total_seconds()),
                    ordered_labels[index].start_ts,
                    ordered_labels[index].label_id,
                ),
            )
        )
        for episode in ordered_predictions
    )
    label_to_prediction: dict[int, int] = {}

    def assign(prediction_index: int, seen_labels: set[int]) -> bool:
        for label_index in candidates[prediction_index]:
            if label_index in seen_labels:
                continue
            seen_labels.add(label_index)
            previous = label_to_prediction.get(label_index)
            if previous is None or assign(previous, seen_labels):
                label_to_prediction[label_index] = prediction_index
                return True
        return False

    for prediction_index in range(len(ordered_predictions)):
        assign(prediction_index, set())

    matches = []
    for label_index, prediction_index in sorted(label_to_prediction.items()):
        episode = ordered_predictions[prediction_index]
        label = ordered_labels[label_index]
        matches.append(
            EpisodeMatch(
                episode_id=episode.episode_id,
                label_id=label.label_id,
                detection_latency_seconds=max(
                    0.0,
                    (episode.confirmed_ts - label.start_ts).total_seconds(),
                ),
            )
        )
    return tuple(matches)


def _can_match(
    episode: SymptomEpisode,
    label: _ExpectedEpisode,
    *,
    evaluation_end: datetime,
) -> bool:
    if (episode.service, episode.signal) != (label.service, label.signal):
        return False
    predicted_end = episode.closed_ts or evaluation_end
    return episode.opened_ts < label.end_ts and label.start_ts < predicted_end


def _utc(value: datetime, *, name: str) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)
