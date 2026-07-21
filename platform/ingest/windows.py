"""Bounded deterministic event-time feature windows."""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from contracts import Observation

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
type StreamKey = tuple[str, str]
type WindowKey = tuple[str, str, datetime]


@dataclass(frozen=True)
class FeatureWindow:
    """Stable aggregate for one half-open service/signal interval."""

    window_id: str
    service: str
    signal: str
    start: datetime
    end: datetime
    unit: str
    count: int
    value_mean: float
    value_min: float
    value_max: float
    value_last: float
    observation_ids: tuple[str, ...]


@dataclass(frozen=True)
class WindowResult:
    """Outcome of offering one observation to the builder."""

    accepted: bool
    reason: str
    closed_windows: tuple[FeatureWindow, ...] = ()


@dataclass(frozen=True)
class WindowStats:
    accepted: int
    duplicates: int
    late_dropped: int
    windows_closed: int


class EventTimeWindowBuilder:
    """Build fixed windows with per-stream watermarks and bounded id deduplication."""

    def __init__(
        self,
        *,
        window_size: timedelta,
        allowed_lateness: timedelta,
        dedup_capacity: int,
    ) -> None:
        if window_size <= timedelta(0):
            raise ValueError("window_size must be positive")
        if allowed_lateness < timedelta(0):
            raise ValueError("allowed_lateness must not be negative")
        if dedup_capacity < 1:
            raise ValueError("dedup_capacity must be positive")
        self._window_size = window_size
        self._window_micros = window_size // timedelta(microseconds=1)
        self._allowed_lateness = allowed_lateness
        self._dedup_capacity = dedup_capacity
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max_seen: dict[StreamKey, datetime] = {}
        self._open: dict[WindowKey, list[Observation]] = {}
        self._accepted = 0
        self._duplicates = 0
        self._late_dropped = 0
        self._windows_closed = 0

    def add(self, observation: Observation) -> WindowResult:
        """Offer one item, closing only windows behind this stream's watermark."""
        if observation.observation_id in self._seen:
            self._seen.move_to_end(observation.observation_id)
            self._duplicates += 1
            return WindowResult(accepted=False, reason="duplicate")

        stream = (observation.service, observation.signal)
        maximum = self._max_seen.get(stream)
        if maximum is not None and observation.ts < maximum - self._allowed_lateness:
            self._remember(observation.observation_id)
            self._late_dropped += 1
            return WindowResult(accepted=False, reason="late")

        self._remember(observation.observation_id)
        self._accepted += 1
        if maximum is None or observation.ts > maximum:
            maximum = observation.ts
            self._max_seen[stream] = maximum
        start = self._start_for(observation.ts)
        self._open.setdefault((*stream, start), []).append(observation)
        watermark = maximum - self._allowed_lateness
        closed = self._close_stream(stream, watermark)
        return WindowResult(accepted=True, reason="accepted", closed_windows=closed)

    def flush(self) -> tuple[FeatureWindow, ...]:
        """Close every remaining window in stable stream/time order."""
        keys = sorted(self._open)
        windows = tuple(self._materialize(key, self._open.pop(key)) for key in keys)
        self._windows_closed += len(windows)
        return windows

    def stats(self) -> WindowStats:
        return WindowStats(
            accepted=self._accepted,
            duplicates=self._duplicates,
            late_dropped=self._late_dropped,
            windows_closed=self._windows_closed,
        )

    def _start_for(self, timestamp: datetime) -> datetime:
        elapsed = timestamp - _EPOCH
        micros = elapsed // timedelta(microseconds=1)
        return _EPOCH + timedelta(
            microseconds=(micros // self._window_micros) * self._window_micros
        )

    def _close_stream(self, stream: StreamKey, watermark: datetime) -> tuple[FeatureWindow, ...]:
        keys = sorted(
            key
            for key in self._open
            if key[:2] == stream and key[2] + self._window_size <= watermark
        )
        windows = tuple(self._materialize(key, self._open.pop(key)) for key in keys)
        self._windows_closed += len(windows)
        return windows

    def _materialize(self, key: WindowKey, observations: list[Observation]) -> FeatureWindow:
        service, signal, start = key
        ordered = sorted(observations, key=lambda item: (item.ts, item.observation_id))
        units = {item.unit for item in ordered}
        if len(units) != 1:
            raise ValueError(f"mixed units in {service}/{signal} window")
        values = [item.value for item in ordered]
        end = start + self._window_size
        identity = json.dumps(
            {
                "service": service,
                "signal": signal,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return FeatureWindow(
            window_id=hashlib.sha256(identity).hexdigest(),
            service=service,
            signal=signal,
            start=start,
            end=end,
            unit=next(iter(units)),
            count=len(ordered),
            value_mean=math.fsum(sorted(values)) / len(values),
            value_min=min(values),
            value_max=max(values),
            value_last=ordered[-1].value,
            observation_ids=tuple(item.observation_id for item in ordered),
        )

    def _remember(self, observation_id: str) -> None:
        self._seen[observation_id] = None
        self._seen.move_to_end(observation_id)
        while len(self._seen) > self._dedup_capacity:
            self._seen.popitem(last=False)
