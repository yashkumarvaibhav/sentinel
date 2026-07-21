"""Deterministic event-time clock for scenarios, captures, and decision paths."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

_MICROSECONDS_PER_DAY = 86_400_000_000


class SeededClock:
    """A manually advanced UTC clock whose origin is a stable function of a seed."""

    __slots__ = ("_current", "_tick_size", "seed")

    def __init__(
        self,
        *,
        seed: int,
        epoch: datetime,
        seed_horizon: timedelta = timedelta(days=365),
        tick_size: timedelta = timedelta(milliseconds=1),
    ) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if epoch.utcoffset() != timedelta(0):
            raise ValueError("epoch must be timezone-aware UTC")
        if seed_horizon <= timedelta(0):
            raise ValueError("seed_horizon must be positive")
        if tick_size <= timedelta(0):
            raise ValueError("tick_size must be positive")

        horizon_us = _timedelta_microseconds(seed_horizon)
        digest = hashlib.sha256(str(seed).encode("ascii")).digest()
        offset_us = int.from_bytes(digest[:8], byteorder="big") % horizon_us

        self.seed = seed
        self._current = epoch.astimezone(UTC) + timedelta(microseconds=offset_us)
        self._tick_size = tick_size

    def now(self) -> datetime:
        """Return the current deterministic event time without advancing it."""
        return self._current

    def tick(self, steps: int = 1) -> datetime:
        """Advance by a whole number of configured ticks and return the new time."""
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 0:
            raise ValueError("steps must be a non-negative integer")
        return self.advance(self._tick_size * steps)

    def advance(self, delta: timedelta) -> datetime:
        """Move event time forward explicitly; backwards replay time is forbidden."""
        if delta < timedelta(0):
            raise ValueError("delta must be non-negative")
        self._current += delta
        return self._current


def _timedelta_microseconds(value: timedelta) -> int:
    return value.days * _MICROSECONDS_PER_DAY + value.seconds * 1_000_000 + value.microseconds
