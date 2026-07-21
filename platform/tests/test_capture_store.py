"""Capture replay is byte-exact and never opens the private answer key."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lab.captures import (
    CaptureMetadata,
    CaptureSourceRecord,
    TopicBounds,
    load_private_labels,
    load_runtime_capture,
    replay_raw,
    write_capture,
)
from lab.captures.models import CaptureTelemetry


def test_two_raw_replays_are_byte_identical_and_preserve_source_coordinates(
    tmp_path: Path,
) -> None:
    payload = _trace_payload()
    root = tmp_path / "capture"
    manifest = write_capture(
        root,
        metadata=_metadata(),
        topic_bounds=(
            TopicBounds(topic="otlp.raw.metrics", partition=0, start_offset=10, end_offset=10),
            TopicBounds(topic="otlp.raw.logs", partition=0, start_offset=20, end_offset=20),
            TopicBounds(topic="otlp.raw.traces", partition=0, start_offset=30, end_offset=31),
        ),
        records=(
            CaptureSourceRecord(
                topic="otlp.raw.traces",
                partition=0,
                offset=30,
                value=payload,
            ),
        ),
        schedule=b'{"version":1,"phases":[]}\n',
        context_feed=b'{"version":1,"windows":[]}\n',
        private_labels=b'{"version":1,"intervals":[]}\n',
        enrichments={"display-note.json": b'{"note":"not-scored"}\n'},
    )

    first = replay_raw(load_runtime_capture(root))
    second = replay_raw(load_runtime_capture(root))

    assert first.canonical_bytes() == second.canonical_bytes()
    assert len(first.observations) == 1
    assert first.observations[0].attributes["user_agent"] == "sentinel-score/run-capture"
    assert manifest.topics[2].records[0].offset == 30
    assert load_runtime_capture(root).schedule == b'{"version":1,"phases":[]}\n'


def test_runtime_replay_does_not_open_private_labels(tmp_path: Path) -> None:
    root = _write_minimal(tmp_path)
    labels = root / "private" / "labels.json"
    labels.write_text("corrupt-private-answer", encoding="utf-8")

    replay = replay_raw(load_runtime_capture(root))

    assert len(replay.observations) == 1
    with pytest.raises(ValueError, match="private labels checksum"):
        load_private_labels(root)


def test_corrupt_raw_payload_and_non_contiguous_bounds_fail_closed(tmp_path: Path) -> None:
    root = _write_minimal(tmp_path)
    raw_path = next((root / "raw").rglob("*.otlp"))
    raw_path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="raw payload checksum"):
        load_runtime_capture(root)

    with pytest.raises(ValueError, match="contiguous"):
        write_capture(
            tmp_path / "gap",
            metadata=_metadata(),
            topic_bounds=(
                TopicBounds(topic="otlp.raw.traces", partition=0, start_offset=30, end_offset=32),
            ),
            records=(
                CaptureSourceRecord(
                    topic="otlp.raw.traces",
                    partition=0,
                    offset=30,
                    value=_trace_payload(),
                ),
            ),
            schedule=b"{}\n",
            context_feed=b"{}\n",
            private_labels=b"{}\n",
            enrichments={},
        )


def _write_minimal(tmp_path: Path) -> Path:
    root = tmp_path / "capture"
    write_capture(
        root,
        metadata=_metadata(),
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
        schedule=b'{"version":1,"phases":[]}\n',
        context_feed=b'{"version":1,"windows":[]}\n',
        private_labels=b'{"version":1,"intervals":[]}\n',
        enrichments={},
    )
    return root


def _metadata() -> CaptureMetadata:
    return CaptureMetadata(
        capture_id="quiet-day-7901",
        scenario_id="quiet_day",
        seed=7901,
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
    )


def _trace_payload() -> bytes:
    timestamp = int(datetime(2026, 7, 21, 12, 0, tzinfo=UTC).timestamp() * 1_000_000_000)
    document = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "frontend-proxy"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "envoy"},
                        "spans": [
                            {
                                "traceId": "1" * 32,
                                "spanId": "2" * 16,
                                "name": "GET",
                                "kind": 2,
                                "startTimeUnixNano": str(timestamp),
                                "endTimeUnixNano": str(timestamp + 1_000_000),
                                "attributes": [
                                    {
                                        "key": "user_agent",
                                        "value": {"stringValue": "sentinel-score/run-capture"},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
