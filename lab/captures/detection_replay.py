"""Shared label-free capture bounds for deterministic detector replays."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from contracts import Observation
from ingest.normalizer import NormalizationError, normalize_otlp_json
from lab.captures.replay import RAW_SIGNALS, RawReplay, replay_raw
from lab.captures.store import RuntimeCapture
from lab.captures.transcript import CapturedContextFeed, CapturedSchedule


@dataclass(frozen=True)
class DetectionReplayTimeline:
    """Validated public capture timeline bounded to complete scenario time."""

    raw: RawReplay
    anchor_ts: datetime
    end_ts: datetime
    observations: tuple[Observation, ...]


def _validated_schedule(capture: RuntimeCapture) -> CapturedSchedule:
    """The capture's own schedule, once it agrees with the manifest about itself."""
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
    return schedule


def detection_replay_timeline(capture: RuntimeCapture) -> DetectionReplayTimeline:
    """Validate public metadata and return event-time-ordered scenario evidence."""
    schedule = _validated_schedule(capture)
    raw = replay_raw(capture)
    spans, anchors = _scenario_spans(capture, raw)
    if not spans:
        raise ValueError("capture contains no correlated ingress spans")
    if not anchors:
        raise ValueError("capture contains no scenario anchor spans")
    anchor_ts = max(item.ts for item in anchors)
    if anchor_ts > min(item.ts for item in spans):
        raise ValueError("captured scenario anchor is after its first correlated span")

    end_ts = anchor_ts + timedelta(seconds=schedule.duration_seconds)
    observations = tuple(
        sorted(
            (item for item in raw.observations if anchor_ts < item.ts <= end_ts),
            key=lambda item: (item.ts, item.observation_id),
        )
    )
    return DetectionReplayTimeline(
        raw=raw,
        anchor_ts=anchor_ts,
        end_ts=end_ts,
        observations=observations,
    )


def scenario_bounds(capture: RuntimeCapture) -> tuple[datetime, datetime]:
    """When a recorded run started and ended, without materializing the replay.

    ``detection_replay_timeline`` keeps every normalized observation because the
    detectors need them. A caller that only wants to know *when* the run
    happened does not, and holding a few hundred megabytes of decoded telemetry
    to read two timestamps is how a scoring tool gets itself killed on a shared
    box. This decodes the same records one at a time and keeps four numbers.
    """
    schedule = _validated_schedule(capture)
    telemetry = capture.manifest.telemetry
    anchor_ts: datetime | None = None
    first_span: datetime | None = None
    for observation in _stream_observations(capture):
        if not _is_ingress_span(observation, telemetry.source_service):
            continue
        user_agent = observation.attributes.get("user_agent")
        if user_agent == capture.manifest.anchor_user_agent:
            if anchor_ts is None or observation.ts > anchor_ts:
                anchor_ts = observation.ts
        elif _matches_correlation_user_agent(
            user_agent, capture.manifest.correlation_user_agent
        ) and (first_span is None or observation.ts < first_span):
            first_span = observation.ts
    if first_span is None:
        raise ValueError("capture contains no correlated ingress spans")
    if anchor_ts is None:
        raise ValueError("capture contains no scenario anchor spans")
    if anchor_ts > first_span:
        raise ValueError("captured scenario anchor is after its first correlated span")
    return anchor_ts, anchor_ts + timedelta(seconds=schedule.duration_seconds)


def _stream_observations(capture: RuntimeCapture) -> Iterator[Observation]:
    """Decode the capture's raw records one at a time, holding none of them."""
    for record in sorted(
        capture.records,
        key=lambda item: (item.topic, item.partition, item.offset),
    ):
        try:
            normalized = normalize_otlp_json(RAW_SIGNALS[record.topic], record.value)
        except NormalizationError:
            # A record the normalizer refuses carries no timestamp anyone can
            # trust; the full replay records it as a dead letter and this
            # bounds read has nothing to say about it either way.
            continue
        yield from normalized


def _is_ingress_span(observation: Observation, source_service: str) -> bool:
    return (
        observation.service == source_service
        and observation.signal == "span.duration_ms"
        and observation.attributes.get("span.kind") == 2
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
