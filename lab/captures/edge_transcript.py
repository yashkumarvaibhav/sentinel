"""Label-free raw capture replay through edge windows and episode routing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from common.config import DetectorConfig
from contracts import Observation, SymptomEpisode
from detection.runner import EdgeDetectionRunner, EdgeWindowAdvance
from lab.captures.replay import RawReplay, replay_raw
from lab.captures.store import RuntimeCapture
from lab.captures.transcript import CapturedContextFeed, CapturedSchedule


@dataclass(frozen=True)
class EdgeReplayStep:
    """All configured edge advances at one event-time tick."""

    tick_ts: datetime
    results: tuple[EdgeWindowAdvance, ...]

    def canonical_value(self) -> dict[str, object]:
        return {
            "results": [item.canonical_value() for item in self.results],
            "tick_ts": self.tick_ts.isoformat(),
        }


@dataclass(frozen=True)
class EdgeDetectionReplay:
    """Deterministic edge-detector transcript from one public raw capture."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    capture_config_fingerprint: str
    replay_config_fingerprint: str
    raw_replay_sha256: str
    raw_dead_letters: tuple[str, ...]
    anchor_ts: datetime
    steps: tuple[EdgeReplayStep, ...]
    active_episodes: tuple[SymptomEpisode, ...]

    def canonical_bytes(self) -> bytes:
        value = {
            "active_episodes": [
                episode.model_dump(mode="json") for episode in self.active_episodes
            ],
            "anchor_ts": self.anchor_ts.isoformat(),
            "capture_config_fingerprint": self.capture_config_fingerprint,
            "capture_id": self.capture_id,
            "honesty": {"stimulus": "SIMULATED", "telemetry": "REAL"},
            "raw_dead_letters": self.raw_dead_letters,
            "raw_replay_sha256": self.raw_replay_sha256,
            "replay_config_fingerprint": self.replay_config_fingerprint,
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "seed_purpose": self.seed_purpose,
            "steps": [item.canonical_value() for item in self.steps],
            "version": 1,
        }
        return (
            json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()


def replay_edge_detection(
    capture: RuntimeCapture,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> EdgeDetectionReplay:
    """Run normalized trace observations through configured edge windows and episodes."""
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

    runner = EdgeDetectionRunner(
        configuration=detector.edge_degradation,
        episodes=detector.episodes,
    )
    end_ts = anchor_ts + timedelta(seconds=schedule.duration_seconds)
    in_range = tuple(
        sorted(
            (item for item in raw.observations if anchor_ts < item.ts <= end_ts),
            key=lambda item: (item.ts, item.observation_id),
        )
    )
    steps: list[EdgeReplayStep] = []
    previous = anchor_ts
    tick = anchor_ts + timedelta(seconds=runner.advance_seconds)
    while tick <= end_ts:
        batch = tuple(item for item in in_range if previous < item.ts <= tick)
        steps.append(
            EdgeReplayStep(
                tick_ts=tick,
                results=runner.advance(observations=batch, tick_ts=tick),
            )
        )
        previous = tick
        tick += timedelta(seconds=runner.advance_seconds)

    raw_bytes = raw.canonical_bytes()
    return EdgeDetectionReplay(
        capture_id=manifest.capture_id,
        scenario_id=manifest.scenario_id,
        seed=manifest.seed,
        seed_purpose=manifest.seed_purpose,
        capture_config_fingerprint=manifest.config_fingerprint,
        replay_config_fingerprint=replay_config_fingerprint,
        raw_replay_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        raw_dead_letters=raw.dead_letters,
        anchor_ts=anchor_ts,
        steps=tuple(steps),
        active_episodes=runner.active_episodes(),
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
                if item.attributes.get("user_agent") == capture.manifest.correlation_user_agent
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
