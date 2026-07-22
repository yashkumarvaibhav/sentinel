"""Dependency-call evidence identifies the degraded edge, not every caller."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest

from contracts import SymptomKind
from detection.edges import DependencyCall, EdgeDegradationDetector
from tests.factories import edge_degradation_config

_TS = datetime(2026, 7, 22, 11, 0, tzinfo=UTC)


def test_injected_downstream_slowdown_degrades_only_the_true_edge() -> None:
    detector = _detector()
    slowed = detector.evaluate(
        _calls(
            caller="frontend",
            downstream="checkout",
            latencies=(100.0, 110.0, 120.0, 300.0, 320.0),
            prefix="checkout",
        ),
        baseline_latency_p95_ms=100.0,
        baseline_error_rate=0.01,
    )
    unrelated = detector.evaluate(
        _calls(
            caller="frontend",
            downstream="cart",
            latencies=(90.0, 95.0, 100.0, 105.0, 110.0),
            prefix="cart",
        ),
        baseline_latency_p95_ms=100.0,
        baseline_error_rate=0.01,
    )

    assert slowed.caller == "frontend"
    assert slowed.downstream == "checkout"
    assert slowed.sample_count == 5
    assert slowed.latency_p95_ms == 320.0
    assert slowed.error_rate == 0.0
    assert slowed.latency_relative_rise == pytest.approx(2.2)
    assert slowed.error_relative_rise == 0.0
    assert slowed.symptom is not None
    assert slowed.symptom.kind is SymptomKind.EDGE_DEGRADED
    assert slowed.symptom.service == "frontend"
    assert slowed.symptom.signal == "dependency.checkout"
    assert slowed.symptom.onset_ts == _TS + timedelta(seconds=3)
    assert slowed.symptom.score == 1.0
    assert slowed.symptom.evidence_refs == tuple(f"checkout-{index}" for index in range(5))
    assert "edge=frontend->checkout" in slowed.symptom.note
    assert "latency_p95_ms=320" in slowed.symptom.note
    assert "latency_relative_rise=2.2" in slowed.symptom.note

    assert unrelated.latency_relative_rise == pytest.approx(0.1)
    assert unrelated.error_relative_rise == 0.0
    assert unrelated.symptom is None


def test_error_rate_rise_can_degrade_an_edge_without_latency_rise() -> None:
    result = _detector().evaluate(
        _calls(
            caller="checkout",
            downstream="payment",
            latencies=(95.0, 98.0, 100.0, 102.0, 105.0),
            failed=(False, True, False, True, False),
            prefix="payment",
        ),
        baseline_latency_p95_ms=100.0,
        baseline_error_rate=0.1,
    )

    assert result.latency_relative_rise == pytest.approx(0.05)
    assert result.error_rate == 0.4
    assert result.error_relative_rise == pytest.approx(3.0)
    assert result.symptom is not None
    assert result.symptom.onset_ts == _TS + timedelta(seconds=1)
    assert result.symptom.score == pytest.approx(0.6)
    assert "error_rate=0.4" in result.symptom.note
    assert "error_relative_rise=3" in result.symptom.note


def test_short_windows_missing_baselines_and_unconfigured_edges_are_insufficient() -> None:
    detector = _detector()
    short = detector.evaluate(
        _calls(
            caller="frontend",
            downstream="checkout",
            latencies=(400.0, 420.0, 440.0, 460.0),
            prefix="short",
        ),
        baseline_latency_p95_ms=100.0,
        baseline_error_rate=0.01,
    )
    missing = detector.evaluate(
        _calls(
            caller="frontend",
            downstream="checkout",
            latencies=(400.0,) * 5,
            prefix="missing",
        ),
        baseline_latency_p95_ms=None,
        baseline_error_rate=0.01,
    )
    unconfigured = detector.evaluate(
        _calls(
            caller="checkout",
            downstream="cart",
            latencies=(400.0,) * 5,
            prefix="unconfigured",
        ),
        baseline_latency_p95_ms=100.0,
        baseline_error_rate=0.01,
    )

    assert short.sample_count == 4
    assert short.latency_relative_rise is None
    assert short.error_relative_rise is None
    assert short.symptom is None
    assert missing.latency_relative_rise is None
    assert missing.error_relative_rise is None
    assert missing.symptom is None
    assert unconfigured.latency_relative_rise is None
    assert unconfigured.error_relative_rise is None
    assert unconfigured.symptom is None


def test_reference_order_does_not_change_statistics_or_identity() -> None:
    calls = _calls(
        caller="frontend",
        downstream="checkout",
        latencies=(100.0, 110.0, 120.0, 300.0, 320.0),
        prefix="stable",
    )

    first = _detector().evaluate(
        calls,
        baseline_latency_p95_ms=100.0,
        baseline_error_rate=0.01,
    )
    second = _detector().evaluate(
        tuple(reversed(calls)),
        baseline_latency_p95_ms=100.0,
        baseline_error_rate=0.01,
    )

    assert first == second


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            lambda: _detector().evaluate(
                _calls(
                    caller="frontend",
                    downstream="checkout",
                    latencies=(100.0,) * 4 + (float("nan"),),
                    prefix="nan",
                ),
                baseline_latency_p95_ms=100.0,
                baseline_error_rate=0.01,
            ),
            "latency_ms must be finite and non-negative",
        ),
        (
            lambda: _detector().evaluate(
                _calls(
                    caller="frontend",
                    downstream="checkout",
                    latencies=(100.0,) * 5,
                    prefix="baseline",
                ),
                baseline_latency_p95_ms=-1.0,
                baseline_error_rate=0.01,
            ),
            "baseline_latency_p95_ms must be finite and non-negative",
        ),
        (
            lambda: _detector().evaluate(
                _calls(
                    caller="frontend",
                    downstream="checkout",
                    latencies=(100.0,) * 5,
                    prefix="error-baseline",
                ),
                baseline_latency_p95_ms=100.0,
                baseline_error_rate=1.1,
            ),
            "baseline_error_rate must be between zero and one",
        ),
        (
            lambda: _detector().evaluate(
                _calls(
                    caller="frontend",
                    downstream="checkout",
                    latencies=(100.0,) * 4,
                    prefix="mixed",
                )
                + _calls(
                    caller="frontend",
                    downstream="cart",
                    latencies=(100.0,),
                    prefix="other",
                    start_index=4,
                ),
                baseline_latency_p95_ms=100.0,
                baseline_error_rate=0.01,
            ),
            "one caller and downstream edge",
        ),
        (
            lambda: _detector().evaluate(
                _calls(
                    caller="frontend",
                    downstream="checkout",
                    latencies=(100.0,) * 5,
                    prefix="duplicate",
                    duplicate_id=True,
                ),
                baseline_latency_p95_ms=100.0,
                baseline_error_rate=0.01,
            ),
            "evidence_id values must be unique",
        ),
        (
            lambda: _detector().evaluate(
                (
                    DependencyCall(
                        evidence_id="local-time",
                        ts=datetime(
                            2026,
                            7,
                            22,
                            12,
                            0,
                            tzinfo=timezone(timedelta(hours=1)),
                        ),
                        caller="frontend",
                        downstream="checkout",
                        latency_ms=100.0,
                        failed=False,
                    ),
                ),
                baseline_latency_p95_ms=100.0,
                baseline_error_rate=0.01,
            ),
            "timezone-aware UTC",
        ),
    ],
)
def test_invalid_edge_evidence_fails_closed(
    operation: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        operation()


def _detector() -> EdgeDegradationDetector:
    return EdgeDegradationDetector(configuration=edge_degradation_config())


def _calls(
    *,
    caller: str,
    downstream: str,
    latencies: tuple[float, ...],
    prefix: str,
    failed: tuple[bool, ...] | None = None,
    start_index: int = 0,
    duplicate_id: bool = False,
) -> tuple[DependencyCall, ...]:
    statuses = failed if failed is not None else (False,) * len(latencies)
    assert len(statuses) == len(latencies)
    return tuple(
        DependencyCall(
            evidence_id=(f"{prefix}-0" if duplicate_id and offset == 1 else f"{prefix}-{offset}"),
            ts=_TS + timedelta(seconds=start_index + offset),
            caller=caller,
            downstream=downstream,
            latency_ms=latency,
            failed=statuses[offset],
        )
        for offset, latency in enumerate(latencies)
    )
