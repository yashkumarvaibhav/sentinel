"""Typed snapshot invalidations, independent of any HTTP framework.

The producer process publishes these and serves no HTTP at all, so the fan-out
seam and the wire encoding live here rather than beside the response that
carries them. Keeping the split real is what lets the producer image stay free
of a web framework it never uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from contracts import (
    SnapshotInvalidation,
    SnapshotResource,
    StreamEventKind,
)

HEARTBEAT_SECONDS = 15.0
RETRY_MILLISECONDS = 3_000
MAX_PENDING_EVENTS = 64

type Subscription = asyncio.Queue[SnapshotInvalidation | None]


class StreamBroker:
    """Fan typed invalidations out to bounded in-process subscriber queues."""

    def __init__(self, *, capacity: int = MAX_PENDING_EVENTS) -> None:
        if capacity < 1:
            raise ValueError("stream subscriber capacity must be positive")
        self._capacity = capacity
        self._subscribers: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        """Number of browser connections currently attached."""
        return len(self._subscribers)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[Subscription]:
        """Attach one bounded subscriber and always remove it on disconnect."""
        queue: Subscription = asyncio.Queue(maxsize=self._capacity)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def publish(self, event: SnapshotInvalidation) -> int:
        """Publish without blocking the gateway on a slow browser.

        An overflowing queue is drained, closed, and removed. Dropping only the
        oldest event would leave the browser believing it had a complete event
        history; closing makes EventSource reconnect and receive the mandatory
        `all` invalidation instead.
        """
        delivered = 0
        for queue in tuple(self._subscribers):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
                self._subscribers.discard(queue)
                continue
            queue.put_nowait(event)
            delivered += 1
        return delivered


def snapshot_invalidation(
    *resources: SnapshotResource,
    now: datetime | None = None,
) -> SnapshotInvalidation:
    """Create a live invalidation with a unique wire id."""
    return SnapshotInvalidation(
        event_id=f"stream-{uuid4().hex}",
        ts=now or datetime.now(UTC),
        kind=StreamEventKind.SNAPSHOT_INVALIDATE,
        resources=resources,
    )


def encode_event(event: SnapshotInvalidation) -> bytes:
    """Encode one public contract as an SSE frame."""
    return (
        f"id: {event.event_id}\nevent: {event.kind.value}\ndata: {event.model_dump_json()}\n\n"
    ).encode()


async def event_stream(
    broker: StreamBroker,
    *,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> AsyncGenerator[bytes, None]:
    """Yield retry guidance, typed invalidations, and proxy-safe heartbeats."""
    if heartbeat_seconds <= 0 or heartbeat_seconds > 30:
        raise ValueError("heartbeat_seconds must be within (0, 30]")

    loop = asyncio.get_running_loop()
    async with broker.subscribe() as queue:
        yield f"retry: {RETRY_MILLISECONDS}\n\n".encode()
        yield encode_event(snapshot_invalidation(SnapshotResource.ALL))
        next_heartbeat = loop.time() + heartbeat_seconds

        while True:
            timeout = max(0.0, next_heartbeat - loop.time())
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                next_heartbeat = loop.time() + heartbeat_seconds
                # Cloudflare may close an idle connection. This comment is
                # deliberately not a product event, so no UI refresh is tied
                # to transport keepalive alone.
                yield b": heartbeat\n\n"
                # The heartbeat cadence also bounds how stale the top bar's
                # component-health snapshot may become.
                yield encode_event(snapshot_invalidation(SnapshotResource.HEALTH))
                continue

            if event is None:
                return
            yield encode_event(event)
