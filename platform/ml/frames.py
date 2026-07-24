"""The labeled training-frame contract and its canonical, hashable serialization.

A dataset is an ordered set of frames; its identity is the sha256 of the
canonical newline-delimited JSON below. Floats are rounded to a fixed precision
and signed zero is normalized so the same inputs always produce the same hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from ml.features import AWARE_FEATURES

type FrameSource = Literal["synthetic", "capture"]
type FrameHonesty = Literal["SIMULATED", "REAL"]
type FramePurpose = Literal["synthetic", "development"]

FRAME_FLOAT_DECIMALS = 6


def _canonical_float(value: float) -> float:
    """Round to fixed precision and fold signed zero, for a stable hash."""
    rounded = round(float(value), FRAME_FLOAT_DECIMALS)
    return 0.0 if rounded == 0.0 else rounded


@dataclass(frozen=True)
class TrainingFrame:
    """One (features -> value) example with full provenance and honesty labels."""

    signal_key: str
    ts: datetime
    value: float
    features: Mapping[str, float]
    source: FrameSource
    honesty: FrameHonesty
    origin_seed: int
    scenario_id: str
    seed_purpose: FramePurpose

    def __post_init__(self) -> None:
        if set(self.features) != set(AWARE_FEATURES):
            raise ValueError("training frame features must match the aware feature schema exactly")
        if self.ts.tzinfo is None or self.ts.utcoffset() != timedelta(0):
            raise ValueError("training frame timestamp must be timezone-aware UTC")

    def row(self) -> dict[str, object]:
        """A JSON-able canonical row with deterministic float formatting."""
        return {
            "signal_key": self.signal_key,
            "ts": self.ts.isoformat(),
            "value": _canonical_float(self.value),
            "features": {name: _canonical_float(self.features[name]) for name in AWARE_FEATURES},
            "source": self.source,
            "honesty": self.honesty,
            "origin_seed": self.origin_seed,
            "scenario_id": self.scenario_id,
            "seed_purpose": self.seed_purpose,
        }


def _sort_key(frame: TrainingFrame) -> tuple[str, str, str, int]:
    return (frame.source, frame.signal_key, frame.ts.isoformat(), frame.origin_seed)


def sorted_frames(frames: Iterable[TrainingFrame]) -> tuple[TrainingFrame, ...]:
    """Deterministically order frames independent of assembly order."""
    return tuple(sorted(frames, key=_sort_key))


def canonical_frames_bytes(frames: Iterable[TrainingFrame]) -> bytes:
    """The exact bytes a dataset hashes over: one canonical JSON row per line."""
    lines = (
        json.dumps(frame.row(), allow_nan=False, separators=(",", ":"), sort_keys=True)
        for frame in sorted_frames(frames)
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def frames_data_hash(frames: Iterable[TrainingFrame]) -> str:
    """The content hash identifying a dataset built from these frames."""
    return hashlib.sha256(canonical_frames_bytes(frames)).hexdigest()
