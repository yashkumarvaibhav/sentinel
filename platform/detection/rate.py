"""Reconstruct the decomposed request-rate stream from real live ingress spans.

The demo mesh emits spans, not a request-rate metric, so the stream every
decomposition, drop and silence judgement is made on has to be counted from the
server spans themselves. A capture replay does this from its recorded manifest;
a live run does it from committed configuration, on strictly complete
event-time ticks.

A tick with no traffic emits **nothing**. Absence of measured requests is the
evidence the liveness detector exists to interpret, and writing a zero here
would hand it a measurement that was never taken.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from common.config import IngressRateConfig
from contracts import Observation

# The wire shape of one server span, as the normalizer publishes it.
INGRESS_SIGNAL = "span.duration_ms"
SERVER_SPAN_KIND = (2, "2")

type Stream = tuple[str, str]


class IngressRateReconstructor:
    """Count configured server spans into one rate observation per event-time tick."""

    def __init__(self, *, configuration: IngressRateConfig) -> None:
        self._configuration = configuration
        self._streams: tuple[Stream, ...] = tuple(
            sorted((item.service, item.signal) for item in configuration.streams)
        )
        self._stream_by_source: dict[str, Stream] = {
            source: (item.service, item.signal)
            for item in configuration.streams
            for source in item.source_services
        }
        self._sources_by_stream: dict[Stream, tuple[str, ...]] = {
            (item.service, item.signal): tuple(sorted(item.source_services))
            for item in configuration.streams
        }
        self._last_tick: datetime | None = None
        self._last_batch_signature: tuple[str, ...] | None = None
        self._last_results: tuple[Observation, ...] | None = None

    @property
    def advance_seconds(self) -> int:
        return self._configuration.tick_seconds

    @property
    def streams(self) -> tuple[Stream, ...]:
        """Configured decomposed stream identities in deterministic order."""
        return self._streams

    def advance(
        self,
        *,
        observations: tuple[Observation, ...],
        tick_ts: datetime,
    ) -> tuple[Observation, ...]:
        """Reconstruct the rate for the complete window **ending** at ``tick_ts``.

        Every window runner in this plane closes on ``(previous, tick]``, so the
        reconstruction does too and one batch can drive all of them. The
        measurement it emits is stamped at the window's start, which is where a
        capture replay puts it and what the liveness runner ticks on.
        """
        if not isinstance(observations, tuple):
            raise TypeError("observations must be a tuple")
        if any(not isinstance(item, Observation) for item in observations):
            raise TypeError("observations must contain only Observation values")
        tick = _utc(tick_ts, name="tick_ts")
        start = tick - timedelta(seconds=self.advance_seconds)
        signature = tuple(sorted(item.observation_id for item in observations))
        if self._last_tick is not None:
            if tick < self._last_tick:
                raise ValueError("live rate ticks must arrive in event-time order")
            if tick == self._last_tick:
                if signature == self._last_batch_signature and self._last_results is not None:
                    return self._last_results
                raise ValueError("conflicting live rate advance at the same event time")
            expected = self._last_tick + timedelta(seconds=self.advance_seconds)
            if tick != expected:
                raise ValueError("live rate ticks must use the configured tick_seconds")

        counts: OrderedDict[Stream, int] = OrderedDict((stream, 0) for stream in self._streams)
        seen: set[str] = set()
        for observation in observations:
            stream = self._match(observation)
            if stream is None:
                continue
            if not start < observation.ts <= tick:
                raise ValueError("ingress evidence timestamp is outside its rate tick")
            if observation.observation_id in seen:
                continue
            seen.add(observation.observation_id)
            counts[stream] += 1

        results = tuple(
            self._observation(stream, count=count, tick=start)
            for stream, count in counts.items()
            if count > 0
        )
        self._last_tick = tick
        self._last_batch_signature = signature
        self._last_results = results
        return results

    def _match(self, observation: Observation) -> Stream | None:
        if observation.signal != INGRESS_SIGNAL:
            return None
        if observation.attributes.get("span.kind") not in SERVER_SPAN_KIND:
            return None
        return self._stream_by_source.get(observation.service)

    def _observation(self, stream: Stream, *, count: int, tick: datetime) -> Observation:
        service, signal = stream
        sources = self._sources_by_stream[stream]
        # Derived from event time and identity alone, so the same tick replayed
        # after a restart is recognised as the same measurement rather than
        # counted twice.
        observation_id = hashlib.sha256(
            f"ingress-rate:{service}:{signal}:{tick.isoformat()}".encode()
        ).hexdigest()
        return Observation(
            observation_id=observation_id,
            ts=tick,
            service=service,
            signal=signal,
            value=count / self.advance_seconds,
            unit=self._configuration.unit,
            attributes={
                "evidence.source": "live_otel_ingress_spans",
                "evidence.services": ",".join(sources),
                "evidence.span_count": count,
                "evidence.window_seconds": self.advance_seconds,
            },
        )


def _utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)
