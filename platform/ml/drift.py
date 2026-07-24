"""Concept-drift monitoring on features and model predictions (river ADWIN/KSWIN).

A learned detector's world shifts under it — traffic mix, deploys, a season the
model never trained on. Left unwatched, a stale model mis-scores silently. This
board runs a streaming change detector per feature/prediction stream; when a
stream's distribution changes it raises a META-symptom (a `DriftAlarm`) and
alarms the self-health board, so drift is caught as its own signal rather than as
mysterious downstream errors.

Everything is deterministic: river's ADWIN is parameter-only, KSWIN is seeded, and
the injected-drift demonstration draws from a seeded stdlib PRNG.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from river import drift

from ml.config import DriftParamsConfig


class _DriftDetector(Protocol):
    """The slice of river's drift-detector interface this module uses."""

    def update(self, value: float) -> object: ...

    @property
    def drift_detected(self) -> bool: ...


@dataclass(frozen=True)
class DriftAlarm:
    """A META-symptom: a monitored stream's distribution has changed."""

    stream: str
    detector: str
    at_index: int  # the 0-based stream position where drift was flagged
    n_seen: int


def _make_detector(params: DriftParamsConfig) -> _DriftDetector:
    detector: _DriftDetector
    if params.detector == "adwin":
        detector = drift.ADWIN(delta=params.adwin_delta)  # type: ignore[no-untyped-call]
    else:
        detector = drift.KSWIN(
            alpha=params.kswin_alpha,
            window_size=params.kswin_window_size,
            stat_size=params.kswin_stat_size,
            seed=params.kswin_seed,
        )
    return detector


class StreamDriftMonitor:
    """One stream's change detector; each observation may raise a drift alarm."""

    def __init__(self, stream: str, params: DriftParamsConfig) -> None:
        self._stream = stream
        self._detector_kind = params.detector
        self._detector = _make_detector(params)
        self._n_seen = 0

    def observe(self, value: float) -> DriftAlarm | None:
        """Feed the next value; return a DriftAlarm iff drift is flagged this step."""
        index = self._n_seen
        self._detector.update(value)
        self._n_seen += 1
        if self._detector.drift_detected:
            return DriftAlarm(
                stream=self._stream,
                detector=self._detector_kind,
                at_index=index,
                n_seen=self._n_seen,
            )
        return None


class DriftBoard:
    """The self-health board: one monitor per configured stream, accumulating alarms."""

    def __init__(self, params: DriftParamsConfig) -> None:
        self._monitors = {stream: StreamDriftMonitor(stream, params) for stream in params.streams}
        self._alarms: list[DriftAlarm] = []

    def observe(self, values: Mapping[str, float]) -> tuple[DriftAlarm, ...]:
        """Feed a tick of stream values; return any alarms raised this tick.

        Values for unconfigured streams are ignored; streams are visited in a fixed
        (sorted) order so the accumulated alarm log is deterministic.
        """
        fired: list[DriftAlarm] = []
        for stream in sorted(values):
            monitor = self._monitors.get(stream)
            if monitor is None:
                continue
            alarm = monitor.observe(values[stream])
            if alarm is not None:
                fired.append(alarm)
                self._alarms.append(alarm)
        return tuple(fired)

    @property
    def alarms(self) -> tuple[DriftAlarm, ...]:
        """Every alarm raised so far, in order."""
        return tuple(self._alarms)


@dataclass(frozen=True)
class DriftReport:
    """Per-stream outcome of the injected-drift demonstration."""

    stream: str
    detector: str
    stable_false_alarms: int  # alarms during the stable regime (should be 0)
    drift_detected: bool  # drift flagged during the shifted regime (should be True)
    first_drift_index: int | None
    alarms: tuple[DriftAlarm, ...] = field(default_factory=tuple)


def run_drift_demo(params: DriftParamsConfig) -> tuple[DriftReport, ...]:
    """Run each stream through a stable then a shifted regime and report detection.

    A correct detector raises no alarm in the stable half and flags the shift in the
    drifted half — the injected-drift test. Each stream is seeded independently so the
    result is reproducible regardless of stream count.
    """
    demo = params.demo
    reports: list[DriftReport] = []
    for index, stream in enumerate(params.streams):
        rng = random.Random(demo.seed + index)
        monitor = StreamDriftMonitor(stream, params)
        stable_false_alarms = 0
        drift_alarms: list[DriftAlarm] = []
        first_drift_index: int | None = None
        for _ in range(demo.stable_samples):
            if monitor.observe(rng.gauss(demo.stable[0], demo.stable[1])) is not None:
                stable_false_alarms += 1
        for _ in range(demo.drift_samples):
            alarm = monitor.observe(rng.gauss(demo.drift[0], demo.drift[1]))
            if alarm is not None:
                drift_alarms.append(alarm)
                if first_drift_index is None:
                    first_drift_index = alarm.at_index
        reports.append(
            DriftReport(
                stream=stream,
                detector=params.detector,
                stable_false_alarms=stable_false_alarms,
                drift_detected=first_drift_index is not None,
                first_drift_index=first_drift_index,
                alarms=tuple(drift_alarms),
            )
        )
    return tuple(reports)
