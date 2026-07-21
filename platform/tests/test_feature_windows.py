"""Feature windows are deterministic event-time state with explicit late-data behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from contracts import Observation
from ingest.windows import EventTimeWindowBuilder

_START = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def test_out_of_order_data_inside_watermark_is_accepted_and_sorted_on_close() -> None:
    builder = _builder()

    assert builder.add(_observation("a", 10, 1.0)).accepted
    assert builder.add(_observation("c", 50, 3.0)).accepted
    assert builder.add(_observation("b", 45, 2.0)).accepted
    result = builder.add(_observation("next", 75, 9.0))

    assert result.accepted
    assert len(result.closed_windows) == 1
    window = result.closed_windows[0]
    assert (window.start, window.end) == (_START, _START + timedelta(minutes=1))
    assert window.observation_ids == ("a", "b", "c")
    assert (window.count, window.value_mean, window.value_min, window.value_max) == (
        3,
        2.0,
        1.0,
        3.0,
    )
    assert window.value_last == 3.0


def test_duplicates_are_counted_before_state_and_late_data_is_dropped() -> None:
    builder = _builder()
    observation = _observation("same", 10, 1.0)

    assert builder.add(observation).reason == "accepted"
    assert builder.add(observation).reason == "duplicate"
    builder.add(_observation("advance", 75, 2.0))
    late = _observation("late", 59, 3.0)
    assert builder.add(late).reason == "late"
    assert builder.add(late).reason == "duplicate"

    stats = builder.stats()
    assert (stats.accepted, stats.duplicates, stats.late_dropped) == (2, 2, 1)


def test_half_open_boundaries_and_watermarks_are_per_stream() -> None:
    builder = _builder()
    builder.add(_observation("front-start", 0, 1.0))
    boundary = builder.add(_observation("front-boundary", 60, 2.0))
    other = builder.add(
        _observation("cart-future", 300, 4.0, service="cart", signal="request_rate")
    )

    assert boundary.closed_windows == ()
    assert other.closed_windows == ()
    flushed = builder.flush()
    assert [(window.service, window.start.second, window.count) for window in flushed] == [
        ("cart", 0, 1),
        ("frontend", 0, 1),
        ("frontend", 0, 1),
    ]
    assert flushed[1].start == _START
    assert flushed[2].start == _START + timedelta(minutes=1)


def test_aggregates_are_identical_for_different_allowed_arrival_orders() -> None:
    points = (
        _observation("a", 10, 1e16),
        _observation("b", 20, 1.0),
        _observation("c", 50, -1e16),
    )
    left = _builder()
    right = _builder()

    for point in points:
        left.add(point)
    for point in (points[1], points[0], points[2]):
        right.add(point)

    left_closed = left.add(_observation("next-left", 75, 0.0)).closed_windows
    right_closed = right.add(_observation("next-right", 75, 0.0)).closed_windows

    assert left_closed == right_closed
    assert left_closed[0].value_mean == 1.0 / 3.0


@given(st.permutations((0, 1, 2)))
def test_all_arrival_orders_inside_lateness_produce_the_same_window(
    order: list[int],
) -> None:
    points = (
        _observation("a", 10, 1e16),
        _observation("b", 20, 1.0),
        _observation("c", 50, -1e16),
    )
    builder = EventTimeWindowBuilder(
        window_size=timedelta(minutes=1),
        allowed_lateness=timedelta(minutes=1),
        dedup_capacity=100,
    )
    for index in order:
        assert builder.add(points[index]).accepted

    closed = builder.add(_observation("advance", 121, 0.0)).closed_windows

    assert closed[0].observation_ids == ("a", "b", "c")
    assert closed[0].value_mean == 1.0 / 3.0


def _builder() -> EventTimeWindowBuilder:
    return EventTimeWindowBuilder(
        window_size=timedelta(minutes=1),
        allowed_lateness=timedelta(seconds=10),
        dedup_capacity=100,
    )


def _observation(
    observation_id: str,
    seconds: int,
    value: float,
    *,
    service: str = "frontend",
    signal: str = "request_rate",
) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=_START + timedelta(seconds=seconds),
        service=service,
        signal=signal,
        value=value,
        unit="requests/s",
        attributes={},
    )
