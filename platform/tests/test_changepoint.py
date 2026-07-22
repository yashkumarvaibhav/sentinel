"""PELT proposes resource onsets; deterministic evidence gates saturation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest

from contracts import SymptomKind
from detection.changepoint import ResourceSample, SaturationDetector
from tests.factories import change_point_saturation_config

_TS = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)


def test_monotonic_heap_leak_fires_under_flat_traffic_with_raw_evidence() -> None:
    traffic_requests = (100.0,) * 16
    heap_mib = (400.0,) * 8 + (420.0, 450.0, 480.0, 520.0, 560.0, 600.0, 650.0, 700.0)

    result = _detector().evaluate(_samples(heap_mib, capacity=800.0))

    assert len(set(traffic_requests)) == 1
    assert result.change_index == 8
    assert result.change_ts == _TS + timedelta(minutes=8)
    assert result.before_level == pytest.approx(400.0)
    assert result.after_level == pytest.approx(547.5)
    assert result.growth_slope_per_second == pytest.approx(2 / 3)
    assert result.headroom == pytest.approx(100.0)
    assert result.headroom_ratio == pytest.approx(0.125)
    assert result.symptom is not None
    assert result.symptom.kind is SymptomKind.SATURATION
    assert result.symptom.signal == "process_heap_mib"
    assert result.symptom.onset_ts == _TS + timedelta(minutes=8)
    assert result.symptom.score == pytest.approx(0.9)
    assert result.symptom.evidence_refs == tuple(f"heap-{index:02d}" for index in range(16))
    assert "change_index=8" in result.symptom.note
    assert "before_level=400" in result.symptom.note
    assert "after_level=547.5" in result.symptom.note
    assert "headroom_ratio=0.125" in result.symptom.note


def test_monotonic_scrape_plateaus_do_not_hide_low_headroom_growth() -> None:
    working_set = (50.0,) * 8 + (
        58.0,
        62.0,
        62.0,
        69.0,
        72.0,
        72.0,
        76.0,
        76.0,
        81.0,
        81.0,
        85.0,
        94.0,
        94.0,
        97.0,
    )

    result = _detector().evaluate(_samples(working_set, capacity=100.0))

    assert result.change_index is not None
    assert result.growth_slope_per_second is not None
    assert result.growth_slope_per_second > 0.0
    assert result.headroom_ratio == pytest.approx(0.03)
    assert result.symptom is not None
    assert "increasing_fraction=1" in result.symptom.note


def test_flat_resource_and_traffic_only_surge_do_not_emit_saturation() -> None:
    traffic_requests = (100.0,) * 8 + (150.0, 220.0, 350.0, 500.0, 700.0, 900.0, 1_100.0, 1_300.0)
    flat_resource = _samples((400.0,) * len(traffic_requests), capacity=800.0)

    result = _detector().evaluate(flat_resource)

    assert traffic_requests[-1] > traffic_requests[0] * 10
    assert result.change_index is None
    assert result.symptom is None


def test_change_without_low_headroom_or_monotonic_growth_does_not_emit() -> None:
    ample_headroom = (200.0,) * 8 + (220.0, 240.0, 260.0, 280.0, 300.0, 320.0, 340.0, 360.0)
    oscillating = (400.0,) * 8 + (620.0, 500.0, 650.0, 510.0, 680.0, 520.0, 690.0, 530.0)

    ample_result = _detector().evaluate(_samples(ample_headroom, capacity=800.0))
    oscillating_result = _detector().evaluate(_samples(oscillating, capacity=800.0))

    assert ample_result.symptom is None
    assert oscillating_result.symptom is None


def test_short_series_is_explicitly_insufficient() -> None:
    result = _detector().evaluate(_samples((400.0,) * 11, capacity=800.0))

    assert result.sample_count == 11
    assert result.change_index is None
    assert result.before_level is None
    assert result.after_level is None
    assert result.growth_slope_per_second is None
    assert result.headroom is None
    assert result.headroom_ratio is None
    assert result.symptom is None


def test_input_order_does_not_change_evidence_or_identity() -> None:
    samples = _samples(
        (400.0,) * 8 + (420.0, 450.0, 480.0, 520.0, 560.0, 600.0, 650.0, 700.0),
        capacity=800.0,
    )

    first = _detector().evaluate(samples)
    second = _detector().evaluate(tuple(reversed(samples)))

    assert first == second


@pytest.mark.parametrize(
    ("samples_factory", "message"),
    [
        (
            lambda: (
                ResourceSample(
                    evidence_id="local-time",
                    ts=datetime(2026, 7, 22, 10, 0, tzinfo=timezone(timedelta(hours=1))),
                    service="frontend",
                    signal="process_heap_mib",
                    used=400.0,
                    capacity=800.0,
                ),
            ),
            "timezone-aware UTC",
        ),
        (
            lambda: _samples((400.0,) * 11 + (float("nan"),), capacity=800.0),
            "finite",
        ),
        (
            lambda: _samples((400.0,) * 11 + (900.0,), capacity=800.0),
            "cannot exceed capacity",
        ),
        (
            lambda: _samples((400.0,) * 12, capacity=800.0, duplicate_id=True),
            "evidence_id values must be unique",
        ),
        (
            lambda: _samples((400.0,) * 12, capacity=800.0, mixed_service=True),
            "one service and signal",
        ),
    ],
)
def test_invalid_resource_evidence_fails_closed(
    samples_factory: Callable[[], tuple[ResourceSample, ...]],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _detector().evaluate(samples_factory())


def _detector() -> SaturationDetector:
    return SaturationDetector(configuration=change_point_saturation_config())


def _samples(
    values: tuple[float, ...],
    *,
    capacity: float,
    duplicate_id: bool = False,
    mixed_service: bool = False,
) -> tuple[ResourceSample, ...]:
    return tuple(
        ResourceSample(
            evidence_id="heap-00" if duplicate_id and index == 1 else f"heap-{index:02d}",
            ts=_TS + timedelta(minutes=index),
            service="checkout" if mixed_service and index == len(values) - 1 else "frontend",
            signal="process_heap_mib",
            used=value,
            capacity=capacity,
        )
        for index, value in enumerate(values)
    )
