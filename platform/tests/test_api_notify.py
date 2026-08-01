"""A commit in the producer process reaches browsers attached to the gateway."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest

from api.notify import (
    NOTIFY_CHANNEL,
    NotifyConnection,
    SnapshotNotificationListener,
    decode_notification,
    encode_notification,
)
from api.stream import StreamBroker
from contracts import SnapshotResource


@dataclass
class _Notification:
    channel: str
    payload: str


@dataclass
class _Connection:
    """A recorded psycopg connection: LISTEN, then deliver, then stop."""

    delivered: list[_Notification]
    executed: list[str] = field(default_factory=list)
    committed: int = 0
    closed: bool = False

    async def execute(self, query: object, parameters: object = None) -> _Connection:
        self.executed.append(str(query))
        return self

    async def commit(self) -> None:
        self.committed += 1

    async def close(self) -> None:
        self.closed = True

    async def notifies(self) -> object:
        async def stream() -> object:
            for item in self.delivered:
                yield item
                await asyncio.sleep(0)

        return stream()


def test_a_notification_from_another_process_invalidates_the_same_resources() -> None:
    broker = StreamBroker()
    received: list[tuple[SnapshotResource, ...]] = []
    connection = _Connection(
        delivered=[
            _Notification(
                channel=NOTIFY_CHANNEL,
                payload=encode_notification(
                    (SnapshotResource.INCIDENTS, SnapshotResource.SECURITY)
                ),
            )
        ]
    )

    async def drive() -> None:
        listener = SnapshotNotificationListener(
            connect=_returns(connection),
            publish=lambda event: received.append(event.resources),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(listener.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(drive())

    assert received == [(SnapshotResource.INCIDENTS, SnapshotResource.SECURITY)]
    assert any("LISTEN" in statement for statement in connection.executed)
    assert connection.closed
    assert broker.subscriber_count == 0


def test_an_unreadable_payload_still_forces_a_full_refetch() -> None:
    assert decode_notification("not json at all") == (SnapshotResource.ALL,)
    assert decode_notification(json.dumps({"resources": ["nonsense"]})) == (SnapshotResource.ALL,)
    assert decode_notification(json.dumps({"resources": []})) == (SnapshotResource.ALL,)


def test_a_notification_carries_resource_names_and_nothing_else() -> None:
    payload = encode_notification((SnapshotResource.ACTIONS,))

    assert decode_notification(payload) == (SnapshotResource.ACTIONS,)
    assert json.loads(payload) == {"resources": ["actions"]}
    with pytest.raises(ValueError, match="at least one resource"):
        encode_notification(())


def test_a_listener_that_loses_its_connection_reconnects_rather_than_dying() -> None:
    attempts: list[int] = []

    class _Broken:
        async def execute(self, query: object, parameters: object = None) -> _Broken:
            return self

        async def commit(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def notifies(self) -> object:
            raise ConnectionError("server closed the connection unexpectedly")

    async def connect() -> NotifyConnection:
        attempts.append(len(attempts))
        if len(attempts) < 3:
            return _Broken()
        raise asyncio.CancelledError

    async def drive() -> None:
        listener = SnapshotNotificationListener(
            connect=connect,
            publish=lambda event: None,
            retry_seconds=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(listener.run(stop))
        await asyncio.sleep(0.2)
        stop.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(drive())

    assert len(attempts) >= 2


def _returns(connection: NotifyConnection) -> Callable[[], Awaitable[NotifyConnection]]:
    async def connect() -> NotifyConnection:
        return connection

    return connect
