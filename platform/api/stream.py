"""Bounded server-sent events for browser snapshot invalidation.

SSE is a hint channel, not a second source of truth. Every connection starts
with an `all` invalidation, and a slow client is disconnected instead of
silently losing an event. Native EventSource reconnect then creates a fresh
connection and forces a full snapshot refetch.
"""

from __future__ import annotations

from fastapi.responses import StreamingResponse

from api.invalidation import (
    HEARTBEAT_SECONDS,
    MAX_PENDING_EVENTS,
    RETRY_MILLISECONDS,
    StreamBroker,
    Subscription,
    encode_event,
    event_stream,
    snapshot_invalidation,
)

__all__ = [
    "HEARTBEAT_SECONDS",
    "MAX_PENDING_EVENTS",
    "RETRY_MILLISECONDS",
    "StreamBroker",
    "Subscription",
    "encode_event",
    "event_stream",
    "snapshot_invalidation",
    "stream_response",
]


def stream_response(broker: StreamBroker) -> StreamingResponse:
    """Build the proxy-safe HTTP response without consuming its infinite body."""
    return StreamingResponse(
        event_stream(broker),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
