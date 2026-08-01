"""Out-of-order live telemetry becomes complete, ordered, event-time windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contracts import Observation
from detection.watermark import WatermarkBuffer

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def test_a_tick_closes_only_once_the_watermark_has_passed_it() -> None:
    buffer = _buffer()
    buffer.start_from(START)

    buffer.offer(_observation("a", START + timedelta(seconds=1)))
    assert buffer.drain() == ()

    # Evidence at +13 with a 10 s bound makes everything up to +3 safe, so the
    # window ending at +2 closes and the one ending at +4 does not.
    buffer.offer(_observation("b", START + timedelta(seconds=13)))
    closed = buffer.drain()

    assert [item.tick_ts for item in closed] == [START + timedelta(seconds=2)]
    assert [item.observation_id for item in closed[0].observations] == ["a"]

    buffer.offer(_observation("c", START + timedelta(seconds=17)))
    assert [item.tick_ts for item in buffer.drain()] == [
        START + timedelta(seconds=4),
        START + timedelta(seconds=6),
    ]


def test_the_anchor_defaults_to_the_first_window_evidence_actually_arrived_in() -> None:
    buffer = _buffer()

    buffer.offer(_observation("first", START + timedelta(seconds=15)))

    assert buffer.anchor_ts == START + timedelta(seconds=14)
    # Nothing before the first measurement is invented as an empty window.
    assert buffer.drain() == ()


def test_out_of_order_arrival_within_the_bound_still_lands_in_its_own_window() -> None:
    buffer = _buffer()
    buffer.start_from(START)

    buffer.offer(_observation("late-but-legal", START + timedelta(seconds=3)))
    buffer.offer(_observation("early", START + timedelta(seconds=1)))
    buffer.offer(_observation("head", START + timedelta(seconds=15)))

    closed = {item.tick_ts: item for item in buffer.drain()}

    assert [item.observation_id for item in closed[START + timedelta(seconds=2)].observations] == [
        "early"
    ]
    assert [item.observation_id for item in closed[START + timedelta(seconds=4)].observations] == [
        "late-but-legal"
    ]


def test_evidence_arriving_after_its_window_closed_is_counted_and_refused() -> None:
    buffer = _buffer()
    buffer.start_from(START)
    buffer.offer(_observation("head", START + timedelta(seconds=15)))
    buffer.drain()

    buffer.offer(_observation("too-late", START + timedelta(seconds=1)))
    reopened = buffer.drain()

    assert reopened == ()
    assert buffer.stats().late == 1
    assert buffer.stats().accepted == 1
    assert buffer.stats().closed_ticks == 2


def test_empty_windows_are_closed_rather_than_skipped() -> None:
    buffer = _buffer()
    buffer.start_from(START)

    buffer.offer(_observation("only", START + timedelta(seconds=31)))
    closed = buffer.drain()

    # Silence is what the liveness detector reads, so an empty window has to
    # reach it rather than be skipped over.
    assert len(closed) == 10
    assert all(item.observations == () for item in closed)
    assert closed[-1].tick_ts == START + timedelta(seconds=20)


def test_a_repeated_record_is_counted_once() -> None:
    buffer = _buffer()
    buffer.start_from(START)

    buffer.offer(_observation("a", START + timedelta(seconds=1)))
    buffer.offer(_observation("a", START + timedelta(seconds=1)))
    buffer.offer(_observation("head", START + timedelta(seconds=15)))

    closed = buffer.drain()

    assert sum(len(item.observations) for item in closed) == 1
    assert buffer.stats().duplicates == 1


def test_a_resumed_producer_never_rejudges_a_window_it_already_judged() -> None:
    buffer = _buffer()
    buffer.start_from(START, closed_through=START + timedelta(seconds=10))

    buffer.offer(_observation("already-judged", START + timedelta(seconds=5)))
    buffer.offer(_observation("fresh", START + timedelta(seconds=11)))
    buffer.offer(_observation("head", START + timedelta(seconds=27)))

    closed = buffer.drain()

    assert buffer.stats().late == 1
    assert closed[0].tick_ts == START + timedelta(seconds=12)
    assert [item.observation_id for item in closed[0].observations] == ["fresh"]


def test_the_buffer_bounds_its_memory_by_closing_the_oldest_window() -> None:
    buffer = _buffer(capacity=4)

    for index in range(10):
        buffer.offer(_observation(f"o{index}", START + timedelta(seconds=1 + 2 * index)))

    assert buffer.buffered <= 4
    assert buffer.stats().late > 0


def test_an_anchor_must_be_a_tick_boundary_and_is_set_once() -> None:
    buffer = _buffer()

    with pytest.raises(ValueError, match="tick boundary"):
        buffer.start_from(START + timedelta(seconds=1))

    buffer.start_from(START)
    with pytest.raises(ValueError, match="set once"):
        buffer.start_from(START)


def _buffer(*, capacity: int = 10_000) -> WatermarkBuffer:
    return WatermarkBuffer(tick_seconds=2, lateness_seconds=10, capacity=capacity)


def _observation(observation_id: str, ts: datetime) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=ts,
        service="frontend-proxy",
        signal="span.duration_ms",
        value=10.0,
        unit="ms",
        attributes={"span.kind": 2},
        trace_refs=(f"trace-{observation_id}",),
    )
