"""Decision time is deterministic and never obtained from the host wall clock."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

import common.clock as clock_module
from common.clock import SeededClock


def test_equal_seeds_produce_byte_identical_utc_sequences() -> None:
    left = _clock(seed=20260721)
    right = _clock(seed=20260721)

    left_sequence = [left.now(), left.tick(), left.tick(3), left.advance(timedelta(seconds=7))]
    right_sequence = [
        right.now(),
        right.tick(),
        right.tick(3),
        right.advance(timedelta(seconds=7)),
    ]

    assert left_sequence == right_sequence
    assert all(value.tzinfo is UTC for value in left_sequence)
    assert _render(left_sequence) == _render(right_sequence)


def test_different_seeds_choose_different_deterministic_origins() -> None:
    assert _clock(seed=1).now() != _clock(seed=2).now()


def test_clock_rejects_non_utc_origins_and_backwards_motion() -> None:
    with pytest.raises(ValueError, match="UTC"):
        SeededClock(
            seed=1,
            epoch=datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        )

    clock = _clock(seed=1)
    with pytest.raises(ValueError, match="non-negative"):
        clock.tick(-1)
    with pytest.raises(ValueError, match="non-negative"):
        clock.advance(timedelta(microseconds=-1))


def test_clock_implementation_contains_no_wall_clock_reads() -> None:
    source = inspect.getsource(clock_module)

    assert "datetime.now" not in source
    assert "datetime.utcnow" not in source
    assert "time.time" not in source
    assert "time_ns" not in source


def _clock(seed: int) -> SeededClock:
    return SeededClock(
        seed=seed,
        epoch=datetime(2026, 1, 1, tzinfo=UTC),
        seed_horizon=timedelta(days=365),
        tick_size=timedelta(milliseconds=250),
    )


def _render(values: list[datetime]) -> bytes:
    return json.dumps([value.isoformat() for value in values], separators=(",", ":")).encode()
