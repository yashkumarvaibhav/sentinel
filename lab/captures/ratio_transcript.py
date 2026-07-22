"""Label-free raw capture replay through ingress ratio windows and episodes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from common.config import DetectorConfig
from contracts import SymptomEpisode
from detection.ratio_runner import IngressRatioDetectionRunner, RatioWindowAdvance
from lab.captures.detection_replay import detection_replay_timeline
from lab.captures.store import RuntimeCapture


@dataclass(frozen=True)
class IngressRatioReplayStep:
    """All configured ingress ratio advances at one complete event-time window."""

    tick_ts: datetime
    results: tuple[RatioWindowAdvance, ...]

    def canonical_value(self) -> dict[str, object]:
        return {
            "results": [item.canonical_value() for item in self.results],
            "tick_ts": self.tick_ts.isoformat(),
        }


@dataclass(frozen=True)
class IngressRatioReplay:
    """Deterministic ingress-ratio transcript from one public raw capture."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    capture_config_fingerprint: str
    replay_config_fingerprint: str
    raw_replay_sha256: str
    raw_dead_letters: tuple[str, ...]
    anchor_ts: datetime
    steps: tuple[IngressRatioReplayStep, ...]
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


def replay_ingress_ratios(
    capture: RuntimeCapture,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> IngressRatioReplay:
    """Run normalized public ingress spans through fixed ratio windows and episodes."""
    manifest = capture.manifest
    timeline = detection_replay_timeline(capture)
    raw = timeline.raw
    anchor_ts = timeline.anchor_ts
    runner = IngressRatioDetectionRunner(
        configuration=detector.behavioral_ratios,
        episodes=detector.episodes,
    )

    steps: list[IngressRatioReplayStep] = []
    previous = anchor_ts
    tick = anchor_ts + timedelta(seconds=runner.advance_seconds)
    while tick <= timeline.end_ts:
        batch = tuple(item for item in timeline.observations if previous < item.ts <= tick)
        steps.append(
            IngressRatioReplayStep(
                tick_ts=tick,
                results=runner.advance(observations=batch, tick_ts=tick),
            )
        )
        previous = tick
        tick += timedelta(seconds=runner.advance_seconds)

    return IngressRatioReplay(
        capture_id=manifest.capture_id,
        scenario_id=manifest.scenario_id,
        seed=manifest.seed,
        seed_purpose=manifest.seed_purpose,
        capture_config_fingerprint=manifest.config_fingerprint,
        replay_config_fingerprint=replay_config_fingerprint,
        raw_replay_sha256=hashlib.sha256(raw.canonical_bytes()).hexdigest(),
        raw_dead_letters=raw.dead_letters,
        anchor_ts=anchor_ts,
        steps=tuple(steps),
        active_episodes=runner.active_episodes(),
    )
