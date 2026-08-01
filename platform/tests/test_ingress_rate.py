"""Real ingress spans become the decomposed request-rate stream, or nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.config import (
    ConfigLoadError,
    IngressRateConfig,
    IngressRateStreamConfig,
    load_config,
)
from contracts import Observation
from detection.rate import IngressRateReconstructor

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
TICK = START + timedelta(seconds=2)
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_only_configured_server_spans_are_counted_into_their_logical_stream() -> None:
    reconstructor = _reconstructor()

    observations = reconstructor.advance(
        observations=(
            _span("a", START + timedelta(seconds=1)),
            _span("b", TICK),
            # A client span is the caller's view of the same request.
            _span("c", TICK, kind=3),
            # Another service's ingress is not this stream's traffic.
            _span("d", TICK, service="checkout"),
            # A non-span signal cannot be a request.
            _span("e", TICK, signal="container.memory.working_set"),
        ),
        tick_ts=TICK,
    )

    assert len(observations) == 1
    frame = observations[0]
    assert (frame.service, frame.signal, frame.unit) == ("frontend", "request_rate", "requests/s")
    # The measurement is stamped at the start of the window it summarises.
    assert frame.ts == START
    assert frame.value == 1.0
    assert frame.attributes["evidence.source"] == "live_otel_ingress_spans"
    assert frame.attributes["evidence.services"] == "frontend-proxy"
    assert frame.attributes["evidence.span_count"] == 2


def test_a_tick_with_no_traffic_emits_no_measurement() -> None:
    reconstructor = _reconstructor()

    assert reconstructor.advance(observations=(), tick_ts=TICK) == ()
    assert (
        reconstructor.advance(
            observations=(_span("a", TICK + timedelta(seconds=2), service="checkout"),),
            tick_ts=TICK + timedelta(seconds=2),
        )
        == ()
    )


def test_the_same_tick_reconstructs_byte_identically() -> None:
    batch = (_span("a", TICK), _span("b", START + timedelta(seconds=1)))

    first = _reconstructor().advance(observations=batch, tick_ts=TICK)
    second = _reconstructor().advance(observations=tuple(reversed(batch)), tick_ts=TICK)

    assert first == second
    assert first[0].observation_id == second[0].observation_id


def test_evidence_outside_the_tick_is_refused_rather_than_folded_in() -> None:
    reconstructor = _reconstructor()

    with pytest.raises(ValueError, match="tick"):
        reconstructor.advance(observations=(_span("a", START),), tick_ts=TICK)
    with pytest.raises(ValueError, match="tick"):
        reconstructor.advance(
            observations=(_span("a", TICK + timedelta(milliseconds=1)),),
            tick_ts=TICK,
        )


def test_ticks_must_advance_in_configured_event_time_order() -> None:
    reconstructor = _reconstructor()
    reconstructor.advance(observations=(_span("a", TICK),), tick_ts=TICK)

    with pytest.raises(ValueError, match="event-time order"):
        reconstructor.advance(observations=(), tick_ts=TICK - timedelta(seconds=2))
    with pytest.raises(ValueError, match="tick_seconds"):
        reconstructor.advance(observations=(), tick_ts=TICK + timedelta(seconds=3))


def test_a_repeated_tick_returns_its_first_answer_and_refuses_a_different_one() -> None:
    reconstructor = _reconstructor()
    first = reconstructor.advance(observations=(_span("a", TICK),), tick_ts=TICK)

    assert reconstructor.advance(observations=(_span("a", TICK),), tick_ts=TICK) == first
    with pytest.raises(ValueError, match="conflicting"):
        reconstructor.advance(
            observations=(_span("a", TICK), _span("b", TICK)),
            tick_ts=TICK,
        )


def test_a_source_service_may_feed_only_one_stream() -> None:
    with pytest.raises(ValueError, match="one live rate stream"):
        IngressRateConfig(
            tick_seconds=2,
            unit="requests/s",
            streams=(
                IngressRateStreamConfig(
                    service="frontend",
                    signal="request_rate",
                    source_services=("frontend-proxy",),
                ),
                IngressRateStreamConfig(
                    service="checkout",
                    signal="request_rate",
                    source_services=("frontend-proxy",),
                ),
            ),
        )


def test_committed_config_produces_every_stream_liveness_watches() -> None:
    detectors = load_config(REPO_ROOT / "config").detectors

    watched = {(item.service, item.signal) for item in detectors.liveness.streams}
    produced = {(item.service, item.signal) for item in detectors.ingress_rate.streams}

    assert produced == watched
    assert detectors.ingress_rate.tick_seconds == detectors.liveness.window_seconds
    for stream in detectors.ingress_rate.streams:
        assert f"{stream.service}.{stream.signal}" in detectors.absolute_noise_floors


def test_a_stream_nobody_watches_for_silence_is_refused(tmp_path: Path) -> None:
    source = REPO_ROOT / "config"
    for name in ("topology.yml", "event-calendar.yml", "slo.yml", "cohorts.yml"):
        (tmp_path / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    detectors = (source / "detector-params.yml").read_text(encoding="utf-8")
    (tmp_path / "detector-params.yml").write_text(
        detectors.replace(
            "    - service: frontend\n      signal: request_rate\n"
            "      source_services:\n        - frontend-proxy\n",
            "    - service: checkout\n      signal: request_rate\n"
            "      source_services:\n        - frontend-proxy\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigLoadError, match="liveness"):
        load_config(tmp_path)


def _reconstructor() -> IngressRateReconstructor:
    return IngressRateReconstructor(
        configuration=IngressRateConfig(
            tick_seconds=2,
            unit="requests/s",
            streams=(
                IngressRateStreamConfig(
                    service="frontend",
                    signal="request_rate",
                    source_services=("frontend-proxy",),
                ),
            ),
        )
    )


def _span(
    observation_id: str,
    ts: datetime,
    *,
    service: str = "frontend-proxy",
    signal: str = "span.duration_ms",
    kind: int = 2,
) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=ts,
        service=service,
        signal=signal,
        value=12.5,
        unit="ms",
        attributes={"span.kind": kind},
        trace_refs=(f"trace-{observation_id}",),
    )
