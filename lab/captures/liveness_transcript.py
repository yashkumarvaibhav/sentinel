"""Label-free decomposition replay through drop and silence episode routing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from common.config import DetectorConfig
from contracts import SymptomEpisode
from detection.liveness_runner import LivenessDetectionRunner, LivenessWindowAdvance
from lab.captures.store import RuntimeCapture
from lab.captures.transcript import CapturedSchedule, replay_decomposition


@dataclass(frozen=True)
class LivenessReplayStep:
    """All configured liveness advances at one complete decomposition tick."""

    tick_ts: datetime
    results: tuple[LivenessWindowAdvance, ...]

    def canonical_value(self) -> dict[str, object]:
        return {
            "results": [item.canonical_value() for item in self.results],
            "tick_ts": self.tick_ts.isoformat(),
        }


@dataclass(frozen=True)
class LivenessDetectionReplay:
    """Deterministic liveness transcript from one public raw capture."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    capture_config_fingerprint: str
    replay_config_fingerprint: str
    raw_replay_sha256: str
    raw_dead_letters: tuple[str, ...]
    anchor_ts: datetime
    steps: tuple[LivenessReplayStep, ...]
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


def replay_liveness_detection(
    capture: RuntimeCapture,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> LivenessDetectionReplay:
    """Run public rate reconstruction/decomposition through exact liveness ticks."""
    manifest = capture.manifest
    if manifest.telemetry.tick_seconds != detector.liveness.window_seconds:
        raise ValueError("capture tick size does not match configured liveness window")
    decomposition = replay_decomposition(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    runner = LivenessDetectionRunner(
        configuration=detector.liveness,
        episodes=detector.episodes,
        expected_since_ts=decomposition.anchor_ts,
    )
    schedule = CapturedSchedule.model_validate_json(capture.schedule)
    frames_by_tick = {
        step.frame.ts: step.frame for step in decomposition.steps if step.frame is not None
    }
    steps = tuple(
        LivenessReplayStep(
            tick_ts=tick_ts,
            results=runner.advance(
                frames=(frames_by_tick[tick_ts],) if tick_ts in frames_by_tick else (),
                tick_ts=tick_ts,
            ),
        )
        for tick_ts in (
            decomposition.anchor_ts + timedelta(seconds=offset)
            for offset in range(
                0,
                schedule.duration_seconds,
                detector.liveness.window_seconds,
            )
        )
    )
    return LivenessDetectionReplay(
        capture_id=manifest.capture_id,
        scenario_id=manifest.scenario_id,
        seed=manifest.seed,
        seed_purpose=manifest.seed_purpose,
        capture_config_fingerprint=manifest.config_fingerprint,
        replay_config_fingerprint=replay_config_fingerprint,
        raw_replay_sha256=decomposition.raw_replay_sha256,
        raw_dead_letters=decomposition.raw_dead_letters,
        anchor_ts=decomposition.anchor_ts,
        steps=steps,
        active_episodes=runner.active_episodes(),
    )
