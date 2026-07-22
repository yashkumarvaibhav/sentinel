"""A verified capture produces a byte-stable, label-free decomposition transcript."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lab.captures import (
    CaptureMetadata,
    CaptureSourceRecord,
    TopicBounds,
    load_runtime_capture,
    replay_decomposition,
    write_capture,
)
from lab.captures.models import CaptureTelemetry
from lab.scoring.capture import score_decomposition_replay

from common.config import DetectorConfig
from tests.factories import (
    behavioral_ratio_config,
    change_point_saturation_config,
    log_template_config,
)


def test_two_full_capture_replays_are_byte_identical_without_private_labels(
    tmp_path: Path,
) -> None:
    root = tmp_path / "capture"
    write_capture(
        root,
        metadata=CaptureMetadata(
            capture_id="match-capture",
            scenario_id="match_night",
            seed=8923,
            seed_purpose="held_out",
            telemetry_honesty="REAL",
            stimulus_honesty="SIMULATED",
            config_fingerprint="a" * 64,
            correlation_user_agent="sentinel-score/run-capture",
            anchor_user_agent="sentinel-score-anchor/run-capture",
            telemetry=CaptureTelemetry(
                target="astronomy-shop/frontend-proxy",
                source_service="frontend-proxy",
                logical_service="frontend",
                logical_signal="request_rate",
                tick_seconds=2,
            ),
        ),
        topic_bounds=(
            TopicBounds(topic="otlp.raw.traces", partition=0, start_offset=30, end_offset=31),
        ),
        records=(
            CaptureSourceRecord(
                topic="otlp.raw.traces",
                partition=0,
                offset=30,
                value=_trace_payload(),
            ),
        ),
        schedule=_json_bytes(
            {
                "version": 1,
                "scenario_id": "match_night",
                "honesty": "SIMULATED",
                "seed": 8923,
                "seed_purpose": "held_out",
                "request_mix_seed": 42,
                "target": "astronomy-shop/frontend-proxy",
                "phases": [
                    {
                        "name": "warmup",
                        "start_offset_seconds": 0,
                        "duration_seconds": 4,
                        "rate_rps": 1,
                    },
                    {
                        "name": "observe",
                        "start_offset_seconds": 4,
                        "duration_seconds": 4,
                        "rate_rps": 1,
                    },
                ],
            }
        ),
        context_feed=_json_bytes(
            {
                "version": 1,
                "scenario_id": "match_night",
                "honesty": "SIMULATED",
                "windows": [],
            }
        ),
        private_labels=b"corrupt-private-labels-are-never-opened",
        enrichments={"phase-1.json": b'{"items":[],"version":1}\n'},
    )

    first = replay_decomposition(
        load_runtime_capture(root),
        detector=_detector(),
        replay_config_fingerprint="b" * 64,
    )
    second = replay_decomposition(
        load_runtime_capture(root),
        detector=_detector(),
        replay_config_fingerprint="b" * 64,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.actual_span_count == 4
    assert first.expected_span_count == 8
    assert first.stats.warming == 2
    assert first.stats.decomposed == 2
    assert len(first.steps) == 4

    scored = score_decomposition_replay(
        first,
        private_labels=_json_bytes(
            {
                "version": 1,
                "scenario_id": "match_night",
                "seed": 8923,
                "seed_purpose": "held_out",
                "intervals": [
                    {
                        "label_id": "expected-residual",
                        "start_offset_seconds": 4,
                        "end_offset_seconds": 8,
                    }
                ],
            }
        ),
    )
    assert scored.metrics.false_negative == 2
    assert scored.metrics.true_positive == 0


def _detector() -> DetectorConfig:
    return DetectorConfig(
        version=1,
        feature_window_seconds=60,
        watermark_lateness_seconds=15,
        ewma_alpha=0.15,
        baseline_warmup_points=2,
        baseline_update_gate_ratio=0.25,
        expected_band_relative_tolerance=0.1,
        absolute_noise_floors={"frontend.request_rate": 1.0},
        behavioral_ratios=behavioral_ratio_config(),
        log_templates=log_template_config(),
        change_point_saturation=change_point_saturation_config(),
    )


def _trace_payload() -> bytes:
    start = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    spans: list[dict[str, object]] = []
    for index in range(10):
        spans.append(
            _span(
                start + timedelta(milliseconds=index),
                user_agent="sentinel-score-anchor/run-capture",
                identity=f"a{index:015x}",
            )
        )
    for index in range(4):
        spans.append(
            _span(
                start + timedelta(seconds=index * 2, milliseconds=100),
                user_agent="sentinel-score/run-capture",
                identity=f"c{index:015x}",
            )
        )
    return _json_bytes(
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "frontend-proxy"},
                            }
                        ]
                    },
                    "scopeSpans": [{"scope": {"name": "envoy"}, "spans": spans}],
                }
            ]
        }
    )


def _span(timestamp: datetime, *, user_agent: str, identity: str) -> dict[str, object]:
    nanos = int(timestamp.timestamp() * 1_000_000_000)
    return {
        "traceId": "1" * 32,
        "spanId": identity,
        "name": "GET",
        "kind": 2,
        "startTimeUnixNano": str(nanos),
        "endTimeUnixNano": str(nanos + 1_000_000),
        "attributes": [{"key": "user_agent", "value": {"stringValue": user_agent}}],
    }


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
