"""Turn an unordered live observation stream into complete event-time ticks.

Detection is event-time based: a window belongs to the moment its telemetry
describes, not the moment it arrived. Records reach a bus consumer out of
order, batched, and across partitions, so something has to decide when a window
can be considered finished.

That decision is a watermark: a tick is closed once evidence has been seen far
enough past its end that anything still missing is later than the configured
lateness bound. Evidence that turns up after its tick closed is **counted and
dropped**, never merged into a window that has already been judged — a window
that can silently change after the fact is not a measurement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from contracts import Observation


@dataclass(frozen=True, slots=True)
class ClosedTick:
    """One complete window and every observation that belongs to it."""

    tick_ts: datetime
    observations: tuple[Observation, ...]


@dataclass(frozen=True, slots=True)
class WatermarkStats:
    """What the buffer accepted, closed and had to refuse."""

    accepted: int
    duplicates: int
    late: int
    closed_ticks: int


class WatermarkBuffer:
    """Buffer observations until their tick can be closed, then hand it over."""

    def __init__(
        self,
        *,
        tick_seconds: int,
        lateness_seconds: int,
        capacity: int,
    ) -> None:
        if tick_seconds < 1:
            raise ValueError("tick_seconds must be positive")
        if lateness_seconds < 0:
            raise ValueError("lateness_seconds cannot be negative")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._tick = timedelta(seconds=tick_seconds)
        self._tick_seconds = tick_seconds
        self._lateness = timedelta(seconds=lateness_seconds)
        self._capacity = capacity
        self._anchor: datetime | None = None
        self._closed_through: datetime | None = None
        self._high_water: datetime | None = None
        self._pending: dict[datetime, dict[str, Observation]] = {}
        self._accepted = 0
        self._duplicates = 0
        self._late = 0
        self._closed = 0

    @property
    def anchor_ts(self) -> datetime | None:
        """The tick boundary this stream is measured from, once it is known."""
        return self._anchor

    @property
    def buffered(self) -> int:
        return sum(len(bucket) for bucket in self._pending.values())

    def stats(self) -> WatermarkStats:
        return WatermarkStats(
            accepted=self._accepted,
            duplicates=self._duplicates,
            late=self._late,
            closed_ticks=self._closed,
        )

    def start_from(self, anchor_ts: datetime, *, closed_through: datetime | None = None) -> None:
        """Resume a stream a previous process already judged part of."""
        if self._anchor is not None:
            raise ValueError("the anchor is set once, before any observation is offered")
        anchor = _utc(anchor_ts, name="anchor_ts")
        if (anchor - _EPOCH).total_seconds() % self._tick_seconds:
            raise ValueError("an anchor must fall on a tick boundary")
        self._anchor = anchor
        if closed_through is None:
            return
        resumed = _utc(closed_through, name="closed_through")
        if resumed < anchor:
            raise ValueError("a resumed position cannot precede its anchor")
        if (resumed - anchor).total_seconds() % self._tick_seconds:
            raise ValueError("a resumed position must fall on a tick boundary")
        self._closed_through = resumed

    def offer(self, observation: Observation) -> None:
        """Accept one observation into the window its own timestamp names."""
        if not isinstance(observation, Observation):
            raise TypeError("offer accepts an Observation")
        ts = _utc(observation.ts, name="observation.ts")
        if self._anchor is None:
            self._anchor = _floor(ts, self._tick_seconds)
        tick = _floor(ts, self._tick_seconds, origin=self._anchor) + self._tick
        if self._high_water is None or ts > self._high_water:
            self._high_water = ts
        if self._closed_through is not None and tick <= self._closed_through:
            # The window this belongs to has already been judged. Counting it
            # is the honest response; rewriting a closed window is not.
            self._late += 1
            return
        bucket = self._pending.setdefault(tick, {})
        if observation.observation_id in bucket:
            self._duplicates += 1
            return
        bucket[observation.observation_id] = observation
        self._accepted += 1
        self._evict()

    def drain(self, *, flush: bool = False) -> tuple[ClosedTick, ...]:
        """Close every tick the watermark has passed, in event-time order.

        Ticks with no evidence are still closed and still handed over: an empty
        window is what silence looks like, and the detector that judges it must
        see it rather than have it skipped.

        This eager helper is for bounded/offline callers. The live consumer uses
        :meth:`next_closed` + :meth:`acknowledge` so a failed downstream write
        does not move this buffer past a tick that was never durably judged.
        """
        closed: list[ClosedTick] = []
        while (item := self.next_closed(flush=flush)) is not None:
            closed.append(item)
            self.acknowledge(item.tick_ts)
        return tuple(closed)

    def next_closed(self, *, flush: bool = False) -> ClosedTick | None:
        """Peek at the next closable tick without advancing durable position.

        A bus record is committed only after the detector and incident writes
        finish. Advancing ``_closed_through`` while merely *returning* work
        broke that guarantee: one storage failure made a retry see no work,
        commit the record anyway, and leave the detector permanently one tick
        behind. The next tick then failed as non-consecutive forever.
        """
        if self._anchor is None or self._high_water is None:
            return None
        limit = _floor(
            self._high_water - self._lateness,
            self._tick_seconds,
            origin=self._anchor,
        )
        if flush:
            limit = _floor(self._high_water, self._tick_seconds, origin=self._anchor) + self._tick
        start = self._closed_through if self._closed_through is not None else self._anchor
        tick = start + self._tick
        if tick > limit:
            return None
        bucket = self._pending.get(tick, {})
        return ClosedTick(
            tick_ts=tick,
            observations=tuple(
                sorted(bucket.values(), key=lambda item: (item.ts, item.observation_id))
            ),
        )

    def acknowledge(self, tick_ts: datetime) -> None:
        """Advance past exactly the tick a downstream consumer committed."""
        if self._anchor is None:
            raise ValueError("a watermark tick cannot be acknowledged before its anchor")
        tick = _utc(tick_ts, name="tick_ts")
        previous = self._closed_through if self._closed_through is not None else self._anchor
        expected = previous + self._tick
        if tick != expected:
            raise ValueError(
                f"watermark acknowledgement must be consecutive: expected "
                f"{expected.isoformat()}, got {tick.isoformat()}"
            )
        self._pending.pop(tick, None)
        self._closed_through = tick
        self._closed += 1

    def _evict(self) -> None:
        """Bound evidence memory without pretending an unjudged tick was closed.

        Dropping observations is an explicit loss counted in ``late``. Dropping
        the *tick* as well used to jump ``closed_through`` ahead of the detector,
        which turned one high-volume window into a permanent retry loop. The
        watermark will still emit that tick in order, empty if necessary, so
        every processor's clock stays consecutive and the missing evidence is
        never confused with a successful measurement.
        """
        while self.buffered > self._capacity and self._pending:
            oldest = min(self._pending)
            dropped = self._pending.pop(oldest)
            self._late += len(dropped)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _floor(value: datetime, seconds: int, *, origin: datetime | None = None) -> datetime:
    base = origin if origin is not None else _EPOCH
    elapsed = (value - base).total_seconds()
    return base + timedelta(seconds=math.floor(elapsed / seconds) * seconds)


def _utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)
