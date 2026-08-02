"""Consume the normalized bus and drive it through the live decision runtime.

The architecture's rule is that every downstream processor reads from the bus
only, which is what makes a capture replay able to reproduce a live run. This
service is the first processor to actually do it: it consumes `obs.normalized`,
lets a watermark decide when each event-time window is finished, and hands the
finished window to the runtime that judges and stores it.

Ordering is the whole safety argument. A window is judged, its incidents are
committed, other processes are notified, and only then is the bus offset moved.
Every one of those steps is idempotent under a repeat, so a crash anywhere in
the sequence costs a re-run rather than a lost or duplicated incident.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import ValidationError

from common.config import DetectorConfig
from common.storage.models import LiveProducerCheckpoint
from contracts import ContextWindow, Observation
from decision.runtime import LiveDecisionRuntime
from detection.live import floor_to_tick
from detection.watermark import WatermarkBuffer

LOGGER = logging.getLogger(__name__)

NORMALIZED_TOPIC = "obs.normalized"


@dataclass(frozen=True, slots=True)
class BusRecord:
    """One normalized-bus record, with the position it occupies."""

    topic: str
    partition: int
    offset: int
    value: bytes | None


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """Where a starting producer picks the stream up, and whether it can."""

    anchor_ts: datetime
    checkpoint: LiveProducerCheckpoint | None
    published_baseline: int
    discontinuity_seconds: float | None


def silence_horizon_seconds(detector: DetectorConfig) -> float:
    """How long a stream may go unmeasured before absence becomes a symptom.

    Derived from the committed silence rules rather than offered as a knob:
    the whole point of the bound is that past this horizon an empty window is
    read as the service having gone quiet, so a looser value would be a licence
    to manufacture exactly the symptom it exists to prevent.
    """
    return min(rule.maximum_age_seconds for rule in detector.liveness.silence_rules.values())


def plan_resume(
    checkpoint: LiveProducerCheckpoint | None,
    *,
    now: datetime,
    tick_seconds: int,
    max_gap_seconds: float,
) -> ResumePlan:
    """Resume the recorded stream, or declare a discontinuity and start a new one.

    Replaying a gap the platform was switched off for would walk the detectors
    through thousands of empty windows, and an empty window is precisely what
    the liveness detector reads as silence. Downtime is not evidence about the
    service, so past the horizon this producer says so and re-anchors.
    """
    if checkpoint is None:
        return ResumePlan(
            anchor_ts=floor_to_tick(now, tick_seconds=tick_seconds),
            checkpoint=None,
            published_baseline=0,
            discontinuity_seconds=None,
        )
    gap = (now - checkpoint.tick_ts).total_seconds()
    if gap <= max_gap_seconds:
        return ResumePlan(
            anchor_ts=checkpoint.anchor_ts,
            checkpoint=checkpoint,
            published_baseline=checkpoint.published_incidents,
            discontinuity_seconds=None,
        )
    return ResumePlan(
        anchor_ts=floor_to_tick(now, tick_seconds=tick_seconds),
        checkpoint=None,
        # A lifetime publication count is not a claim about continuity, so it
        # survives the break that the anchor does not.
        published_baseline=checkpoint.published_incidents,
        discontinuity_seconds=gap,
    )


@dataclass(frozen=True, slots=True)
class LiveProducerStats:
    """What this process consumed, judged and had to refuse."""

    consumed: int
    undecodable: int
    judged_ticks: int
    published_incidents: int


class OffsetCommitter(Protocol):
    """Advance the bus position for records this process has finished with."""

    async def commit(self, record: BusRecord) -> None: ...


class ContextSource(Protocol):
    """Whatever the world says is expected right now."""

    async def active(self, *, as_of: datetime) -> tuple[ContextWindow, ...]: ...


class LiveProducerService:
    """Drive one normalized-bus stream into durable, judged incidents."""

    def __init__(
        self,
        *,
        runtime: LiveDecisionRuntime,
        buffer: WatermarkBuffer,
        committer: OffsetCommitter,
        notify: Callable[[], Awaitable[int]] | None = None,
        contexts: ContextSource | None = None,
        context_refresh_seconds: int = 60,
    ) -> None:
        self._runtime = runtime
        self._buffer = buffer
        self._committer = committer
        self._notify = notify
        self._contexts = contexts
        self._context_refresh = context_refresh_seconds
        self._active_contexts: tuple[ContextWindow, ...] = ()
        self._context_read_at: datetime | None = None
        self._consumed = 0
        self._undecodable = 0
        self._judged = 0
        self._published = 0

    def stats(self) -> LiveProducerStats:
        return LiveProducerStats(
            consumed=self._consumed,
            undecodable=self._undecodable,
            judged_ticks=self._judged,
            published_incidents=self._published,
        )

    async def resume(self) -> None:
        """Start from the position this producer durably reached, if any.

        The runtime's detector state is anchored at construction, so a resumed
        producer whose runtime was built on a different anchor would feed its
        processors windows from before their own origin. That is a wiring
        mistake, and it refuses loudly rather than limping.
        """
        checkpoint = await self._runtime.resume()
        if checkpoint is None:
            self._buffer.start_from(self._runtime.anchor_ts)
            return
        if checkpoint.anchor_ts != self._runtime.anchor_ts:
            raise ValueError(
                "a resumed producer must build its runtime on the durable anchor "
                f"{checkpoint.anchor_ts.isoformat()}, not {self._runtime.anchor_ts.isoformat()}"
            )
        self._buffer.start_from(checkpoint.anchor_ts, closed_through=checkpoint.tick_ts)

    async def handle(self, record: BusRecord) -> None:
        """Judge everything this record completes, then advance past it."""
        observation = _decode(record)
        if observation is None:
            self._undecodable += 1
        else:
            self._consumed += 1
            self._buffer.offer(observation)
        await self._drain()
        # Last, and only last: the offset is the statement that this record's
        # consequences are durable.
        await self._committer.commit(record)

    async def flush(self) -> None:
        """Close every buffered window, for shutdown and for tests."""
        await self._drain(flush=True)

    async def _drain(self, *, flush: bool = False) -> None:
        while (closed := self._buffer.next_closed(flush=flush)) is not None:
            published = await self._runtime.advance(
                observations=closed.observations,
                tick_ts=closed.tick_ts,
                contexts=await self._contexts_at(closed.tick_ts),
            )
            self._judged += 1
            self._published += len(published)
            # The runtime may have queued a decomposition invalidation even
            # when no incident changed. Drain after every judged tick; an
            # empty broker is a no-op, and a moving hero chart is not coupled
            # to whether that tick happened to open an incident.
            if self._notify is not None:
                await self._notify()
            # Last, after every consequence of this tick is durable. If any
            # call above fails, the same tick is returned on the record retry.
            self._buffer.acknowledge(closed.tick_ts)

    async def _contexts_at(self, ts: datetime) -> tuple[ContextWindow, ...]:
        if self._contexts is None:
            return ()
        stale = (
            self._context_read_at is None
            or (ts - self._context_read_at).total_seconds() >= self._context_refresh
        )
        if stale:
            try:
                self._active_contexts = await self._contexts.active(as_of=ts)
            except Exception:
                # An unreachable context source must not stop the loop, and it
                # must not silently become "nothing is expected" either: the
                # last known windows stand until they expire on their own.
                LOGGER.exception("context source unavailable; reusing the last known windows")
            self._context_read_at = ts
        return tuple(
            window for window in self._active_contexts if window.valid_from <= ts < window.valid_to
        )


def _decode(record: BusRecord) -> Observation | None:
    if record.topic != NORMALIZED_TOPIC or record.value is None:
        return None
    try:
        return Observation.model_validate_json(record.value)
    except (ValidationError, ValueError, json.JSONDecodeError):
        LOGGER.warning(
            "undecodable normalized record topic=%s partition=%d offset=%d",
            record.topic,
            record.partition,
            record.offset,
        )
        return None


async def run_forever(
    service: LiveProducerService,
    records: object,
    stop: asyncio.Event,
) -> None:
    """Consume until shutdown, retrying a failed record rather than skipping it."""
    async for record in records:  # type: ignore[attr-defined]
        if stop.is_set():
            break
        delay = 1.0
        while True:
            try:
                await service.handle(record)
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "live producer failed on topic=%s partition=%d offset=%d; retrying",
                    record.topic,
                    record.partition,
                    record.offset,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
    await service.flush()
