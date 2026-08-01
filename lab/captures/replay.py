"""Re-run the sole OTLP normalizer from verified raw capture bytes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from contracts import Observation
from ingest.normalizer import NormalizationError, RawSignal, normalize_otlp_json
from lab.captures.store import RuntimeCapture

RAW_SIGNALS = {
    "otlp.raw.metrics": RawSignal.METRICS,
    "otlp.raw.logs": RawSignal.LOGS,
    "otlp.raw.traces": RawSignal.TRACES,
}


@dataclass(frozen=True)
class RawReplay:
    capture_id: str
    observations: tuple[Observation, ...]
    dead_letters: tuple[str, ...]

    def canonical_bytes(self) -> bytes:
        value = {
            "capture_id": self.capture_id,
            "observations": [item.model_dump(mode="json") for item in self.observations],
            "dead_letters": self.dead_letters,
        }
        return (
            json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()


def replay_raw(capture: RuntimeCapture) -> RawReplay:
    observations: list[Observation] = []
    dead_letters: list[str] = []
    seen: set[str] = set()
    for record in sorted(
        capture.records,
        key=lambda item: (item.topic, item.partition, item.offset),
    ):
        try:
            normalized = normalize_otlp_json(RAW_SIGNALS[record.topic], record.value)
        except NormalizationError as exc:
            dead_letters.append(
                f"{record.topic}:{record.partition}:{record.offset}:"
                f"{hashlib.sha256(record.value).hexdigest()}:{exc}"
            )
            continue
        for observation in normalized:
            if observation.observation_id not in seen:
                seen.add(observation.observation_id)
                observations.append(observation)
    return RawReplay(
        capture_id=capture.manifest.capture_id,
        observations=tuple(observations),
        dead_letters=tuple(dead_letters),
    )
