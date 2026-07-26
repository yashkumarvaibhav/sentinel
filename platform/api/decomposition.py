"""What the command centre's hero chart reads.

The decomposition is the product's thesis, so this is the one endpoint whose
response shape is worth arguing about. Three rules:

* **The frames are returned whole, not pre-summarised.** `observed`,
  `explained_base`, `explained_event` and `residual` satisfy an exact identity
  the contract enforces, and a server that helpfully returned only "residual"
  would be asking the client to trust an arithmetic it cannot check. The chart
  re-derives its own bands from the same four numbers a person could read off
  the API.
* **An empty window is 200 with zero frames, never 404.** "Nothing was recorded
  for this service in this window" is a true and useful answer; a 404 would
  claim the *route* does not exist and send a reader looking for a typo.
* **The window is bounded and the bound is stated in the response.** A client
  that asked for more than it got must be able to tell, or it will draw a
  truncated chart and believe it is complete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from contracts import DecompFrame

# The widest window one request may draw. A full-resolution frame per tick over
# a day is a large read, and the zoom interaction the hero chart needs is a
# series of narrow requests rather than one enormous one.
MAX_FRAMES = 5_000
MAX_WINDOW = timedelta(hours=6)
DEFAULT_WINDOW = timedelta(minutes=30)


class DecompositionReader(Protocol):
    """The store read this endpoint needs, and nothing else.

    A Protocol rather than the ClickHouse repository itself so the route is
    testable without a database - the same seam every other plane uses.
    """

    async def list_decomp_frames(
        self,
        *,
        service: str,
        signal: str,
        start: datetime,
        end: datetime,
        limit: int = 1_000,
    ) -> tuple[DecompFrame, ...]:
        """Frames for one service and signal across a window, in event order."""


class DecompositionRangeError(ValueError):
    """The window asked for is not one this endpoint will serve."""


def resolve_window(
    start: datetime | None, end: datetime | None, *, now: datetime
) -> tuple[datetime, datetime]:
    """Turn the optional query bounds into a window, or refuse to.

    ``now`` is passed in rather than read, so a test pins the default window
    instead of racing the clock.
    """
    resolved_end = end if end is not None else now
    resolved_start = start if start is not None else resolved_end - DEFAULT_WINDOW
    if resolved_start.tzinfo is None or resolved_end.tzinfo is None:
        raise DecompositionRangeError(
            "start and end must carry a timezone; an offsetless timestamp means "
            "different moments to the client and the store"
        )
    if resolved_end < resolved_start:
        raise DecompositionRangeError("end must be at or after start")
    if resolved_end - resolved_start > MAX_WINDOW:
        raise DecompositionRangeError(
            f"a window may span at most {MAX_WINDOW}; zooming is a series of "
            "narrow reads rather than one enormous one"
        )
    return resolved_start, resolved_end


def serialize_window(
    frames: tuple[DecompFrame, ...],
    *,
    service: str,
    signal: str,
    start: datetime,
    end: datetime,
    limit: int,
) -> dict[str, Any]:
    """Render one window, saying honestly whether it is complete."""
    return {
        "service": service,
        "signal": signal,
        "start": start.astimezone(UTC).isoformat(),
        "end": end.astimezone(UTC).isoformat(),
        "count": len(frames),
        # A client that asked for more than it got must be able to tell, or it
        # will draw a truncated chart and believe it is the whole story.
        "truncated": len(frames) >= limit,
        "limit": limit,
        "frames": [frame.model_dump(mode="json") for frame in frames],
    }
