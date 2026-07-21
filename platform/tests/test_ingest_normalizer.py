"""The normalizer is the sole, deterministic OTLP JSON to Observation mapping."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ingest.normalizer import NormalizationError, RawSignal, normalize_otlp_json

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def test_real_collector_metric_shape_maps_without_field_or_time_loss() -> None:
    payload = _metric_payload()

    observations = normalize_otlp_json(RawSignal.METRICS, _encode(payload))

    assert len(observations) == 1
    observation = observations[0]
    assert observation.service == "frontend-proxy"
    assert observation.signal == "httpcheck.duration"
    assert observation.value == 4.0
    assert observation.unit == "ms"
    assert observation.ts == _EPOCH + timedelta(microseconds=1_784_620_052_569_160)
    assert observation.attributes["http.url"] == "http://10.42.0.59:8080"
    assert observation.attributes["k8s.namespace.name"] == "otel-demo"
    assert observation.attributes["otel.metric.kind"] == "gauge"
    assert len(observation.observation_id) == 64


def test_ids_are_stable_across_json_key_order_and_change_with_evidence() -> None:
    payload = _metric_payload()
    reordered = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    first = normalize_otlp_json(RawSignal.METRICS, _encode(payload))[0]
    second = normalize_otlp_json(RawSignal.METRICS, reordered)[0]
    payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][0][
        "asInt"
    ] = "5"
    changed = normalize_otlp_json(RawSignal.METRICS, _encode(payload))[0]

    assert first.observation_id == second.observation_id
    assert changed.observation_id != first.observation_id


@given(st.floats(min_value=-1e12, max_value=1e12, allow_nan=False, allow_infinity=False))
def test_metric_id_is_a_pure_function_of_supported_evidence(value: float) -> None:
    payload = _metric_payload()
    point = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][0]
    point.pop("asInt")
    point["asDouble"] = value

    first = normalize_otlp_json(RawSignal.METRICS, _encode(payload))[0]
    second = normalize_otlp_json(
        RawSignal.METRICS,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    )[0]

    assert first == second


def test_real_collector_log_and_span_shapes_map_to_evidence_references() -> None:
    log = normalize_otlp_json(RawSignal.LOGS, _encode(_log_payload()))[0]
    span = normalize_otlp_json(RawSignal.TRACES, _encode(_span_payload()))[0]

    assert (log.service, log.signal, log.value, log.unit) == (
        "recommendation",
        "log.record",
        1.0,
        "record",
    )
    assert log.attributes["log.severity"] == "INFO"
    assert log.attributes["log.body"] == "Recommendation service started"
    assert len(log.log_refs) == 1

    assert span.service == "flagd"
    assert span.signal == "span.duration_ms"
    assert span.value == pytest.approx(6.13239)
    assert span.attributes["span.name"] == "flagSync"
    assert span.trace_refs == ("532e994d312db29b6e1efa4c57177acf",)


def test_histogram_emits_explicit_count_and_sum_observations() -> None:
    payload = _metric_payload()
    metric = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
    metric.pop("gauge")
    metric["histogram"] = {
        "dataPoints": [
            {
                "timeUnixNano": "1784620052569160134",
                "count": "3",
                "sum": 12.5,
            }
        ]
    }

    observations = normalize_otlp_json(RawSignal.METRICS, _encode(payload))

    assert [(item.signal, item.value) for item in observations] == [
        ("httpcheck.duration.count", 3.0),
        ("httpcheck.duration.sum", 12.5),
    ]


def test_missing_service_malformed_json_and_ground_truth_never_enter_runtime() -> None:
    missing_service = _metric_payload()
    missing_service["resourceMetrics"][0]["resource"]["attributes"] = []
    with pytest.raises(NormalizationError, match=r"service\.name"):
        normalize_otlp_json(RawSignal.METRICS, _encode(missing_service))

    with pytest.raises(NormalizationError, match="JSON"):
        normalize_otlp_json(RawSignal.METRICS, b"not-json")

    labelled = _metric_payload()
    point = labelled["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][
        0
    ]
    point["attributes"].append({"key": "ground_truth", "value": {"stringValue": "ATTACK"}})
    with pytest.raises(NormalizationError, match="ground-truth"):
        normalize_otlp_json(RawSignal.METRICS, _encode(labelled))


def test_unowned_collector_resource_does_not_discard_owned_service_metrics() -> None:
    payload = _metric_payload()
    payload["resourceMetrics"].insert(
        0,
        {
            "resource": {"attributes": []},
            "scopeMetrics": payload["resourceMetrics"][0]["scopeMetrics"],
        },
    )

    observations = normalize_otlp_json(RawSignal.METRICS, _encode(payload))

    assert len(observations) == 1
    assert observations[0].service == "frontend-proxy"


def _metric_payload() -> dict[str, Any]:
    # Shape captured from otlp.raw.metrics on 2026-07-21; values are real testbed evidence.
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "frontend-proxy"}},
                        {
                            "key": "k8s.namespace.name",
                            "value": {"stringValue": "otel-demo"},
                        },
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "httpcheckreceiver", "version": "0.151.0"},
                        "metrics": [
                            {
                                "name": "httpcheck.duration",
                                "unit": "ms",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "attributes": [
                                                {
                                                    "key": "http.url",
                                                    "value": {
                                                        "stringValue": "http://10.42.0.59:8080"
                                                    },
                                                }
                                            ],
                                            "timeUnixNano": "1784620052569160134",
                                            "asInt": "4",
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _log_payload() -> dict[str, Any]:
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "recommendation"}}
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "main"},
                        "logRecords": [
                            {
                                "timeUnixNano": "1784620052007969024",
                                "severityText": "INFO",
                                "body": {"stringValue": "Recommendation service started"},
                                "attributes": [
                                    {"key": "code.line.number", "value": {"intValue": "172"}}
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _span_payload() -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "flagd"}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "jsonEvaluator"},
                        "spans": [
                            {
                                "traceId": "532e994d312db29b6e1efa4c57177acf",
                                "spanId": "4cc4795d73e55343",
                                "name": "flagSync",
                                "kind": 1,
                                "startTimeUnixNano": "1784620052302783279",
                                "endTimeUnixNano": "1784620052308915669",
                                "attributes": [],
                                "status": {},
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()
