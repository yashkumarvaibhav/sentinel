"""SSE framing, heartbeat, and slow-client behavior at the gateway boundary."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

from fastapi.responses import StreamingResponse

from api.app import create_app
from api.stream import (
    HEARTBEAT_SECONDS,
    StreamBroker,
    encode_event,
    event_stream,
    stream_response,
)
from contracts import SnapshotInvalidation, SnapshotResource, StreamEventKind


def _event(event_id: str, resource: SnapshotResource) -> SnapshotInvalidation:
    return SnapshotInvalidation(
        event_id=event_id,
        ts=datetime(2026, 7, 26, tzinfo=UTC),
        kind=StreamEventKind.SNAPSHOT_INVALIDATE,
        resources=(resource,),
    )


def _data(frame: bytes) -> dict[str, object]:
    line = next(line for line in frame.decode().splitlines() if line.startswith("data: "))
    return cast(dict[str, object], json.loads(line.removeprefix("data: ")))


def test_event_encoding_names_and_ids_the_typed_payload() -> None:
    frame = encode_event(_event("event-7", SnapshotResource.INCIDENTS))

    text = frame.decode()
    assert "id: event-7\n" in text
    assert "event: snapshot.invalidate\n" in text
    assert text.endswith("\n\n")
    assert _data(frame)["resources"] == ["incidents"]


def test_a_connection_gets_retry_guidance_an_immediate_resnapshot_and_heartbeat() -> None:
    async def collect() -> tuple[list[bytes], int]:
        broker = StreamBroker()
        stream = event_stream(broker, heartbeat_seconds=0.001)
        try:
            frames = [
                await anext(stream),
                await anext(stream),
                await anext(stream),
                await anext(stream),
            ]
        finally:
            await stream.aclose()
        return frames, broker.subscriber_count

    (retry, initial, heartbeat, health), subscribers = asyncio.run(collect())

    assert retry == b"retry: 3000\n\n"
    assert _data(initial)["resources"] == ["all"]
    assert heartbeat == b": heartbeat\n\n"
    assert _data(health)["resources"] == ["health"]
    assert subscribers == 0
    assert 0 < HEARTBEAT_SECONDS <= 30


def test_a_published_invalidation_reaches_a_connected_subscriber() -> None:
    async def collect() -> bytes:
        broker = StreamBroker()
        stream = event_stream(broker, heartbeat_seconds=10)
        try:
            await anext(stream)
            await anext(stream)
            broker.publish(_event("incident-1", SnapshotResource.INCIDENTS))
            return await anext(stream)
        finally:
            await stream.aclose()

    assert _data(asyncio.run(collect()))["resources"] == ["incidents"]


def test_an_overflowing_subscriber_is_closed_so_reconnect_resnapshots() -> None:
    async def overflow() -> tuple[object, int]:
        broker = StreamBroker(capacity=1)
        async with broker.subscribe() as queue:
            broker.publish(_event("first", SnapshotResource.HEALTH))
            broker.publish(_event("second", SnapshotResource.INCIDENTS))
            return await queue.get(), broker.subscriber_count

    item, subscribers = asyncio.run(overflow())

    assert item is None
    assert subscribers == 0


def test_stream_route_has_proxy_safe_headers_without_opening_an_infinite_client() -> None:
    app = create_app(probes={})
    assert any(getattr(route, "path", None) == "/stream/events" for route in app.routes)
    response = stream_response(StreamBroker())

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
