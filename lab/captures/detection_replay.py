"""Shared label-free capture bounds for deterministic detector replays."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from contracts import Observation
from lab.captures.replay import RawReplay, replay_raw
from lab.captures.store import RuntimeCapture
from lab.captures.transcript import CapturedContextFeed, CapturedSchedule


@dataclass(frozen=True)
class DetectionReplayTimeline:
    """Validated public capture timeline bounded to complete scenario time."""

    raw: RawReplay
    anchor_ts: datetime
    end_ts: datetime
    observations: tuple[Observation, ...]


def detection_replay_timeline(capture: RuntimeCapture) -> DetectionReplayTimeline:
    """Validate public metadata and return event-time-ordered scenario evidence."""
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
