"""Carry snapshot invalidations between processes over the database.

The stream broker fans events out inside one gateway process. Once the always-on
producer runs as its own process, a commit it makes is invisible to every
browser attached to the gateway, and the UI would sit on stale state until the
next reconnect.

Postgres `LISTEN`/`NOTIFY` is the seam, for one reason beyond convenience: a
`NOTIFY` issued inside a transaction is delivered **only if that transaction
commits**, which is exactly the rule this system already holds itself to —
browsers are told after durable state changed, never before. It adds no
component: the database is already a decided part of the stack.

The payload is a hint, never data. A notification that cannot be parsed is
escalated to a full refetch rather than dropped, because the authoritative
answer always comes back over REST.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, Protocol

from api.invalidation import snapshot_invalidation
from contracts import SnapshotInvalidation, SnapshotResource

LOGGER = logging.getLogger(__name__)

NOTIFY_CHANNEL = "sentinel_snapshot_invalidate"
RECONNECT_SECONDS = 2.0
# A notification is a few resource names. Anything larger is not ours.
MAX_PAYLOAD_BYTES = 512


class NotifyConnection(Protocol):
    """The part of an async database connection a listener needs."""

    async def execute(self, query: Any, parameters: Any = None) -> Any: ...

    async def commit(self) -> None: ...

    async def close(self) -> None: ...

    def notifies(self) -> Any: ...


def encode_notification(resources: tuple[SnapshotResource, ...]) -> str:
    """Render the resources one commit made stale."""
    if not resources:
        raise ValueError("an invalidation must name at least one resource")
    return json.dumps(
        {"resources": [resource.value for resource in resources]},
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_notification(payload: str) -> tuple[SnapshotResource, ...]:
    """Read a notification, falling back to a full refetch rather than guessing."""
    if len(payload.encode()) > MAX_PAYLOAD_BYTES:
        return (SnapshotResource.ALL,)
    try:
        document = json.loads(payload)
        names = document["resources"]
        resources = tuple(SnapshotResource(name) for name in names)
    except (TypeError, ValueError, KeyError):
        return (SnapshotResource.ALL,)
    return resources or (SnapshotResource.ALL,)


class SnapshotNotificationListener:
    """Turn database notifications into this process's own stream events."""

    def __init__(
        self,
        *,
        connect: Callable[[], Awaitable[NotifyConnection]],
        publish: Callable[[SnapshotInvalidation], object],
        channel: str = NOTIFY_CHANNEL,
        retry_seconds: float = RECONNECT_SECONDS,
    ) -> None:
        if retry_seconds <= 0:
            raise ValueError("retry_seconds must be positive")
        self._connect = connect
        self._publish = publish
        self._channel = channel
        self._retry_seconds = retry_seconds

    async def run(self, stop: asyncio.Event) -> None:
        """Listen until shutdown; a dropped connection is retried, not fatal."""
        while not stop.is_set():
            try:
                await self._listen(stop)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("snapshot notification listener lost its connection")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._retry_seconds)

    async def _listen(self, stop: asyncio.Event) -> None:
        connection = await self._connect()
        try:
            await connection.execute(f"LISTEN {self._channel}")
            await connection.commit()
            notifies = connection.notifies()
            if isinstance(notifies, Awaitable):
                notifies = await notifies
            async for notification in _iterate(notifies):
                if stop.is_set():
                    return
                if getattr(notification, "channel", self._channel) != self._channel:
                    continue
                self._publish(
                    snapshot_invalidation(
                        *decode_notification(getattr(notification, "payload", ""))
                    )
                )
        finally:
            with suppress(Exception):
                await connection.close()


class PostgresInvalidationBroker:
    """An invalidation seam that reaches every process, not just this one.

    It is deliberately shaped like the in-process broker so a producer can be
    handed either one. Events are queued by the synchronous publish that the
    incident publisher calls and sent by an explicit drain, so nothing is lost
    to a fire-and-forget task at shutdown.
    """

    def __init__(self, *, pool: Any, channel: str = NOTIFY_CHANNEL) -> None:
        self._pool = pool
        self._channel = channel
        self._pending: list[tuple[SnapshotResource, ...]] = []

    @property
    def pending(self) -> int:
        return len(self._pending)

    def publish(self, event: SnapshotInvalidation) -> int:
        """Queue one committed invalidation for delivery to other processes."""
        self._pending.append(event.resources)
        return len(self._pending)

    async def drain(self) -> int:
        """Send every queued notification; a failure keeps them queued."""
        sent = 0
        while self._pending:
            resources = self._pending[0]
            async with self._pool.connection() as connection:
                await connection.execute(
                    "SELECT pg_notify(%s, %s)",
                    (self._channel, encode_notification(resources)),
                )
            self._pending.pop(0)
            sent += 1
        return sent


async def _iterate(source: Any) -> AsyncIterator[Any]:
    async for item in source:
        yield item
