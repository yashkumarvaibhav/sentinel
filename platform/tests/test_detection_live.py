"""One live event-time tick drives every detector path, and claims only those."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.config import DetectorConfig, load_config
from contracts import DecompFrame, Observation, SymptomKind
from detection.live import LiveDetectionProcessors, covered_symptom_kinds

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_covered_kinds_are_exactly_the_kinds_whose_processors_are_running() -> None:
    processors = _processors()

    assert processors.covered_kinds == frozenset(
        {
            SymptomKind.RESIDUAL_EXCEED,
            SymptomKind.RATIO_DEFORM,
            SymptomKind.LOG_BURST,
            SymptomKind.EDGE_DEGRADED,
            SymptomKind.SATURATION,
            SymptomKind.DROP,
            SymptomKind.SILENCE,
        }
    )


def test_coverage_is_derived_from_processors_rather_than_asserted() -> None:
    processors = _processors()

    # Nothing running covers nothing: the claim is computed, not constant.
    assert covered_symptom_kinds(()) == frozenset()

    for processor in processors.monitored_processors:
        watched = covered_symptom_kinds((processor,))
        assert watched
        assert watched <= processors.covered_kinds

    assert processors.covered_kinds == covered_symptom_kinds(processors.monitored_processors) | {
        SymptomKind.RESIDUAL_EXCEED
    }


def test_a_service_is_covered_only_once_its_telemetry_actually_arrives() -> None:
    processors = _processors()

    assert processors.covered_services == frozenset()

    first = processors.advance(
        observations=(_ingress("a", START + timedelta(seconds=1)),),
        tick_ts=START + timedelta(seconds=2),
    )

    # The raw name and the name detectors judge it under are both true.
    assert first.covered_services == frozenset({"frontend-proxy", "frontend"})
    assert processors.covered_services == first.covered_services

    second = processors.advance(observations=(), tick_ts=START + timedelta(seconds=4))
    assert second.covered_services == first.covered_services


def test_every_configured_window_runner_advances_on_its_own_period() -> None:
    processors = _processors()

    ticks = [START + timedelta(seconds=2 * step) for step in range(1, 31)]
    advanced: list[frozenset[str]] = []
    for tick in ticks:
        result = processors.advance(observations=(_ingress(f"o{tick}", tick),), tick_ts=tick)
        advanced.append(result.advanced_processors)

    assert advanced[0] == frozenset({"rate", "liveness", "edge"})
    assert advanced[4] == frozenset({"rate", "liveness", "edge", "resource"})
    assert advanced[29] == frozenset({"rate", "liveness", "edge", "resource", "ratio", "log"})


def test_ticks_must_be_complete_and_ordered() -> None:
    processors = _processors()
    processors.advance(observations=(), tick_ts=START + timedelta(seconds=2))

    processors.advance(observations=(), tick_ts=START + timedelta(seconds=4))

    with pytest.raises(ValueError, match="event-time order"):
        processors.advance(observations=(), tick_ts=START + timedelta(seconds=2))
    with pytest.raises(ValueError, match="complete"):
        processors.advance(observations=(), tick_ts=START + timedelta(seconds=8))
    with pytest.raises(ValueError, match="align"):
        processors.advance(observations=(), tick_ts=START + timedelta(seconds=5))
    with pytest.raises(ValueError, match="anchor"):
        LiveDetectionProcessors(detector=_detector(), anchor_ts=START).advance(
            observations=(), tick_ts=START
        )


def test_a_repeated_tick_yields_the_same_evidence_without_advancing_twice() -> None:
    processors = _processors()
    batch = (_ingress("a", START + timedelta(seconds=1)),)

    first = processors.advance(observations=batch, tick_ts=START + timedelta(seconds=2))
    again = processors.advance(observations=batch, tick_ts=START + timedelta(seconds=2))

    assert first == again


def test_real_traffic_produces_the_decomposed_frames_liveness_watches() -> None:
    processors = _processors()
    frames: list[DecompFrame] = []
    for step in range(1, 41):
        tick = START + timedelta(seconds=2 * step)
        result = processors.advance(
            observations=tuple(
                _ingress(f"{step}-{index}", tick - timedelta(milliseconds=index + 1))
                for index in range(20)
            ),
            tick_ts=tick,
        )
        frames.extend(result.frames)

    # The first ticks are the configured context-blind baseline warmup, and a
    # warming stream produces no frame rather than an unbacked one.
    assert len(frames) == 40 - _detector().baseline_warmup_points
    assert {(frame.service, frame.signal) for frame in frames} == {("frontend", "request_rate")}
    # The rate is the count over the window, not the raw span count.
    assert frames[0].observed == 10.0


def _processors() -> LiveDetectionProcessors:
    return LiveDetectionProcessors(detector=_detector(), anchor_ts=START)


def _detector() -> DetectorConfig:
    return load_config(REPO_ROOT / "config").detectors


def _ingress(observation_id: str, ts: datetime) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=ts,
        service="frontend-proxy",
        signal="span.duration_ms",
        value=11.0,
        unit="ms",
        attributes={"span.kind": 2, "http.route": "/", "http.status_code": 200},
        trace_refs=(f"trace-{observation_id}",),
    )
