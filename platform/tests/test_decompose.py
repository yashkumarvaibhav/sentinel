"""The decomposition oracle protects baseline integrity and unexplained residuals."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from common.config import DetectorConfig
from contracts import ContextWindow, DecompFrame, Observation
from detection.decompose import (
    DecompositionEngine,
    DecompositionWorker,
    UnconfiguredSignalError,
)
from tests.factories import behavioral_ratio_config, log_template_config

_START = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def test_sustained_spike_never_contaminates_anomaly_gated_baseline() -> None:
    engine = _engine()
    _warm(engine, "frontend", "request_rate", (99.0, 100.0, 101.0))

    frames = [
        engine.decompose(_observation(f"spike-{index}", 10 + index, 400.0)).frame
        for index in range(8)
    ]

    assert all(frame is not None for frame in frames)
    assert all(frame.explained_base == 100.0 for frame in frames if frame is not None)
    assert all(frame.residual == 300.0 for frame in frames if frame is not None)
    assert engine.baseline_for(service="frontend", signal="request_rate") == 100.0

    normal = engine.decompose(_observation("normal", 30, 110.0))
    assert normal.frame is not None
    assert normal.frame.explained_base == 100.0
    assert normal.baseline_updated
    assert engine.baseline_for(service="frontend", signal="request_rate") == 105.0


def test_event_explains_only_the_signal_it_names() -> None:
    context = _context("match", {"frontend.request_rate": 2.5})
    request_engine = _engine()
    _warm(request_engine, "frontend", "request_rate", (100.0, 100.0, 100.0))

    covered = request_engine.decompose(
        _observation("covered", 10, 250.0), contexts=(context,)
    ).frame

    assert covered is not None
    assert (covered.explained_base, covered.explained_event, covered.residual) == (
        100.0,
        150.0,
        0.0,
    )
    assert covered.context_ids == ("match",)
    assert covered.residual_score == 0.0

    ratio_engine = _engine()
    _warm(ratio_engine, "frontend", "auth_fail_ratio", (0.02, 0.02, 0.02))
    uncovered = ratio_engine.decompose(
        _observation("uncovered", 10, 0.2, signal="auth_fail_ratio", unit="ratio"),
        contexts=(context,),
    ).frame

    assert uncovered is not None
    assert uncovered.explained_event == 0.0
    assert uncovered.residual == pytest.approx(0.18)
    assert uncovered.context_ids == ()
    assert uncovered.residual_score == 1.0


def test_overlapping_trusted_event_lifts_add_and_keep_both_evidence_ids() -> None:
    engine = _engine()
    _warm(engine, "frontend", "request_rate", (100.0, 100.0, 100.0))
    first = _context("z-half-trust", {"frontend.request_rate": 2.0}, trust=0.5)
    second = _context("a-full-trust", {"frontend.request_rate": 1.5}, trust=1.0)

    frame = engine.decompose(_observation("overlap", 10, 200.0), contexts=(first, second)).frame

    assert frame is not None
    assert frame.explained_event == 100.0
    assert frame.residual == 0.0
    assert frame.context_ids == ("a-full-trust", "z-half-trust")


def test_frame_identity_is_stable_when_context_input_order_changes() -> None:
    first_engine = _engine()
    second_engine = _engine()
    _warm(first_engine, "frontend", "request_rate", (100.0, 100.0, 100.0))
    _warm(second_engine, "frontend", "request_rate", (100.0, 100.0, 100.0))
    first_context = _context("first", {"frontend.request_rate": 1.25})
    second_context = _context("second", {"frontend.request_rate": 1.5})
    observation = _observation("stable", 10, 175.0)

    first = first_engine.decompose(observation, contexts=(first_context, second_context)).frame
    second = second_engine.decompose(observation, contexts=(second_context, first_context)).frame

    assert first is not None
    assert second is not None
    assert first == second


def test_absolute_floor_suppresses_fractional_jitter_without_hiding_arithmetic() -> None:
    engine = _engine()
    _warm(engine, "frontend", "auth_fail_ratio", (0.02, 0.02, 0.02))

    frame = engine.decompose(
        _observation("jitter", 10, 0.024, signal="auth_fail_ratio", unit="ratio")
    ).frame

    assert frame is not None
    assert frame.residual == pytest.approx(0.004)
    assert (frame.band_low, frame.band_high) == pytest.approx((0.015, 0.025))
    assert frame.residual_score == 0.0
    assert frame.observed == pytest.approx(
        frame.explained_base + frame.explained_event + frame.residual
    )


@pytest.mark.parametrize(("observed", "residual"), [(120.0, 20.0), (80.0, -20.0)])
def test_residual_score_is_bounded_for_positive_and_negative_band_breaches(
    observed: float,
    residual: float,
) -> None:
    engine = _engine()
    _warm(engine, "frontend", "request_rate", (100.0, 100.0, 100.0))

    frame = engine.decompose(_observation(f"breach-{observed}", 10, observed)).frame

    assert frame is not None
    assert frame.residual == residual
    assert frame.residual_score == 1.0


def test_warmup_is_explicit_and_event_points_cannot_seed_the_baseline() -> None:
    engine = _engine()
    event = _context("match", {"frontend.request_rate": 2.5})

    blocked = engine.decompose(_observation("event-first", 0, 250.0), contexts=(event,))
    assert blocked.status == "context_blocked_warmup"
    assert blocked.frame is None
    assert engine.baseline_for(service="frontend", signal="request_rate") is None

    results = [
        engine.decompose(_observation(f"warm-{index}", index + 1, value))
        for index, value in enumerate((99.0, 100.0, 101.0))
    ]
    assert all(result.status == "warming" and result.frame is None for result in results)
    assert engine.baseline_for(service="frontend", signal="request_rate") == 100.0


def test_unconfigured_signal_fails_closed() -> None:
    engine = _engine()

    with pytest.raises(UnconfiguredSignalError, match=r"checkout\.request_rate"):
        engine.decompose(
            _observation(
                "unknown",
                0,
                1.0,
                service="checkout",
                signal="request_rate",
            )
        )


def test_full_resolution_sink_retry_reuses_the_same_frame_and_state_update() -> None:
    asyncio.run(_sink_retry_case())


async def _sink_retry_case() -> None:
    engine = _engine()
    _warm(engine, "frontend", "request_rate", (100.0, 100.0, 100.0))
    sink = _Sink(fail=True)
    worker = DecompositionWorker(engine=engine, sink=sink)
    observation = _observation("retry", 10, 110.0)

    with pytest.raises(RuntimeError, match="sink failed"):
        await worker.handle(observation)
    baseline_after_first = engine.baseline_for(service="frontend", signal="request_rate")

    sink.fail = False
    result = await worker.handle(observation)

    assert result.replayed
    assert result.frame is not None
    assert sink.writes == [(result.frame,)]
    assert baseline_after_first == 105.0
    assert engine.baseline_for(service="frontend", signal="request_rate") == 105.0


class _Sink:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.writes: list[tuple[DecompFrame, ...]] = []

    async def write_decomp_frames(self, records: Sequence[DecompFrame]) -> None:
        if self.fail:
            raise RuntimeError("sink failed")
        self.writes.append(tuple(records))


def _engine() -> DecompositionEngine:
    return DecompositionEngine(configuration=_configuration(), dedup_capacity=1_000)


def _configuration() -> DetectorConfig:
    return DetectorConfig(
        version=1,
        feature_window_seconds=60,
        watermark_lateness_seconds=10,
        ewma_alpha=0.5,
        baseline_warmup_points=3,
        baseline_update_gate_ratio=0.25,
        expected_band_relative_tolerance=0.1,
        absolute_noise_floors={
            "frontend.request_rate": 1.0,
            "frontend.auth_fail_ratio": 0.005,
        },
        behavioral_ratios=behavioral_ratio_config(),
        log_templates=log_template_config(),
    )


def _warm(
    engine: DecompositionEngine,
    service: str,
    signal: str,
    values: tuple[float, ...],
) -> None:
    for index, value in enumerate(values):
        result = engine.decompose(
            _observation(
                f"warm-{service}-{signal}-{index}",
                index,
                value,
                service=service,
                signal=signal,
            )
        )
        assert result.frame is None


def _context(
    context_id: str,
    expected_delta: dict[str, float],
    *,
    trust: float = 1.0,
) -> ContextWindow:
    return ContextWindow(
        context_id=context_id,
        name=context_id,
        event_type="sports_fixture",
        source="test",
        honesty="SIMULATED",
        valid_from=_START,
        valid_to=_START + timedelta(hours=1),
        expected_delta=expected_delta,
        trust_score=trust,
    )


def _observation(
    observation_id: str,
    seconds: int,
    value: float,
    *,
    service: str = "frontend",
    signal: str = "request_rate",
    unit: str = "requests/s",
) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=_START + timedelta(seconds=seconds),
        service=service,
        signal=signal,
        value=value,
        unit=unit,
        attributes={},
    )
