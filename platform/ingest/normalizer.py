"""Deterministic OTLP JSON to canonical Observation normalization."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from contracts import Observation

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class RawSignal(StrEnum):
    """OTLP signal carried by one raw Kafka topic."""

    METRICS = "metrics"
    LOGS = "logs"
    TRACES = "traces"


class NormalizationError(ValueError):
    """A raw envelope cannot safely enter the canonical telemetry stream."""


def normalize_otlp_json(signal: RawSignal, payload: bytes) -> tuple[Observation, ...]:
    """Normalize one collector OTLP JSON envelope without reading wall-clock state."""
    try:
        document: Any = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizationError("raw message is not valid OTLP JSON") from exc
    if not isinstance(document, dict):
        raise NormalizationError("OTLP JSON root must be an object")

    try:
        if signal is RawSignal.METRICS:
            observations = _metrics(document)
        elif signal is RawSignal.LOGS:
            observations = _logs(document)
        else:
            observations = _traces(document)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        if isinstance(exc, NormalizationError):
            raise
        raise NormalizationError(f"invalid {signal.value} OTLP envelope: {exc}") from exc
    if not observations:
        raise NormalizationError(f"{signal.value} OTLP envelope contains no supported records")
    return tuple(observations)


def _metrics(document: dict[str, Any]) -> list[Observation]:
    observations: list[Observation] = []
    missing_service = False
    for resource_group in _objects(document, "resourceMetrics"):
        resource = _attributes(_object(resource_group, "resource"))
        service = _service_or_none(resource)
        if service is None:
            missing_service = True
            continue
        for scope_group in _objects(resource_group, "scopeMetrics"):
            scope = _scope_attributes(scope_group)
            for metric in _objects(scope_group, "metrics"):
                name = _required_string(metric, "name")
                unit = _optional_string(metric, "unit") or "1"
                if isinstance(metric.get("gauge"), dict):
                    observations.extend(
                        _scalar_metric(
                            service, name, unit, "gauge", metric["gauge"], resource, scope
                        )
                    )
                elif isinstance(metric.get("sum"), dict):
                    observations.extend(
                        _scalar_metric(service, name, unit, "sum", metric["sum"], resource, scope)
                    )
                elif isinstance(metric.get("histogram"), dict):
                    observations.extend(
                        _aggregate_metric(
                            service,
                            name,
                            unit,
                            "histogram",
                            metric["histogram"],
                            resource,
                            scope,
                        )
                    )
                elif isinstance(metric.get("exponentialHistogram"), dict):
                    observations.extend(
                        _aggregate_metric(
                            service,
                            name,
                            unit,
                            "exponential_histogram",
                            metric["exponentialHistogram"],
                            resource,
                            scope,
                        )
                    )
                elif isinstance(metric.get("summary"), dict):
                    observations.extend(
                        _aggregate_metric(
                            service, name, unit, "summary", metric["summary"], resource, scope
                        )
                    )
    return _require_service_if_empty(observations, missing_service)


def _scalar_metric(
    service: str,
    name: str,
    unit: str,
    kind: str,
    body: dict[str, Any],
    resource: dict[str, str | bool | int | float],
    scope: dict[str, str | bool | int | float],
) -> list[Observation]:
    records: list[Observation] = []
    for point in _objects(body, "dataPoints"):
        value = _number(point, ("asDouble", "asInt"))
        attributes = _merged_attributes(resource, scope, point)
        attributes["otel.metric.kind"] = kind
        records.append(
            _make_observation(
                service=service,
                signal=name,
                ts=_timestamp(point, "timeUnixNano"),
                value=value,
                unit=unit,
                attributes=attributes,
            )
        )
    return records


def _aggregate_metric(
    service: str,
    name: str,
    unit: str,
    kind: str,
    body: dict[str, Any],
    resource: dict[str, str | bool | int | float],
    scope: dict[str, str | bool | int | float],
) -> list[Observation]:
    records: list[Observation] = []
    for point in _objects(body, "dataPoints"):
        attributes = _merged_attributes(resource, scope, point)
        attributes["otel.metric.kind"] = kind
        ts = _timestamp(point, "timeUnixNano")
        for suffix, field, output_unit in (
            ("count", "count", "count"),
            ("sum", "sum", unit),
        ):
            if field not in point:
                continue
            records.append(
                _make_observation(
                    service=service,
                    signal=f"{name}.{suffix}",
                    ts=ts,
                    value=_finite_number(point[field], field),
                    unit=output_unit,
                    attributes=attributes,
                )
            )
    return records


def _logs(document: dict[str, Any]) -> list[Observation]:
    observations: list[Observation] = []
    missing_service = False
    for resource_group in _objects(document, "resourceLogs"):
        resource = _attributes(_object(resource_group, "resource"))
        service = _service_or_none(resource)
        if service is None:
            missing_service = True
            continue
        for scope_group in _objects(resource_group, "scopeLogs"):
            scope = _scope_attributes(scope_group)
            for record in _objects(scope_group, "logRecords"):
                attributes = _merged_attributes(resource, scope, record)
                severity = _optional_string(record, "severityText")
                if severity:
                    attributes["log.severity"] = severity
                body = _any_value(record.get("body"))
                if body is not None:
                    attributes["log.body"] = body
                ts = _timestamp(record, "timeUnixNano", fallback="observedTimeUnixNano")
                reference = _digest(
                    {
                        "service": service,
                        "ts": ts.isoformat(),
                        "severity": severity,
                        "body": body,
                        "attributes": attributes,
                    }
                )
                observations.append(
                    _make_observation(
                        service=service,
                        signal="log.record",
                        ts=ts,
                        value=1.0,
                        unit="record",
                        attributes=attributes,
                        log_refs=(reference,),
                    )
                )
    return _require_service_if_empty(observations, missing_service)


def _traces(document: dict[str, Any]) -> list[Observation]:
    observations: list[Observation] = []
    missing_service = False
    for resource_group in _objects(document, "resourceSpans"):
        resource = _attributes(_object(resource_group, "resource"))
        service = _service_or_none(resource)
        if service is None:
            missing_service = True
            continue
        for scope_group in _objects(resource_group, "scopeSpans"):
            scope = _scope_attributes(scope_group)
            for span in _objects(scope_group, "spans"):
                start_ns = _integer(span, "startTimeUnixNano")
                end_ns = _integer(span, "endTimeUnixNano")
                if end_ns < start_ns:
                    raise NormalizationError("span end time precedes start time")
                attributes = _merged_attributes(resource, scope, span)
                attributes["span.name"] = _required_string(span, "name")
                if "kind" in span:
                    attributes["span.kind"] = _scalar(span["kind"], "span.kind")
                status = span.get("status")
                if isinstance(status, dict) and "code" in status:
                    attributes["span.status_code"] = _scalar(status["code"], "span.status_code")
                trace_id = _required_string(span, "traceId")
                observations.append(
                    _make_observation(
                        service=service,
                        signal="span.duration_ms",
                        ts=_nanos_timestamp(start_ns),
                        value=(end_ns - start_ns) / 1_000_000,
                        unit="ms",
                        attributes=attributes,
                        trace_refs=(trace_id,),
                    )
                )
    return _require_service_if_empty(observations, missing_service)


def _make_observation(
    *,
    service: str,
    signal: str,
    ts: datetime,
    value: float,
    unit: str,
    attributes: dict[str, str | bool | int | float],
    log_refs: tuple[str, ...] = (),
    trace_refs: tuple[str, ...] = (),
) -> Observation:
    evidence = {
        "service": service,
        "signal": signal,
        "ts": ts.isoformat(),
        "value": value,
        "unit": unit,
        "attributes": attributes,
        "log_refs": log_refs,
        "trace_refs": trace_refs,
    }
    try:
        return Observation(
            observation_id=_digest(evidence),
            ts=ts,
            service=service,
            signal=signal,
            value=value,
            unit=unit,
            attributes=attributes,
            log_refs=log_refs,
            trace_refs=trace_refs,
        )
    except ValidationError as exc:
        message = (
            "ground-truth attributes are forbidden" if "ground-truth" in str(exc) else str(exc)
        )
        raise NormalizationError(message) from exc


def _objects(container: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = container.get(key, [])
    if not isinstance(value, list):
        raise NormalizationError(f"{key} must be an array")
    if not all(isinstance(item, dict) for item in value):
        raise NormalizationError(f"{key} must contain objects")
    return value


def _object(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key, {})
    if not isinstance(value, dict):
        raise NormalizationError(f"{key} must be an object")
    return value


def _attributes(container: dict[str, Any]) -> dict[str, str | bool | int | float]:
    result: dict[str, str | bool | int | float] = {}
    for attribute in _objects(container, "attributes"):
        key = _required_string(attribute, "key")
        value = _any_value(attribute.get("value"))
        if value is not None:
            result[key] = value
    return result


def _scope_attributes(scope_group: dict[str, Any]) -> dict[str, str | bool | int | float]:
    scope = _object(scope_group, "scope")
    result: dict[str, str | bool | int | float] = {}
    name = _optional_string(scope, "name")
    version = _optional_string(scope, "version")
    if name:
        result["otel.scope.name"] = name
    if version:
        result["otel.scope.version"] = version
    return result


def _merged_attributes(
    resource: dict[str, str | bool | int | float],
    scope: dict[str, str | bool | int | float],
    record: dict[str, Any],
) -> dict[str, str | bool | int | float]:
    return resource | scope | _attributes(record)


def _service_or_none(attributes: dict[str, str | bool | int | float]) -> str | None:
    service = attributes.get("service.name")
    if not isinstance(service, str) or not service.strip():
        return None
    return service


def _require_service_if_empty(
    observations: list[Observation], missing_service: bool
) -> list[Observation]:
    if not observations and missing_service:
        raise NormalizationError("resource is missing service.name")
    return observations


def _any_value(value: Any) -> str | bool | int | float | None:
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if key in value:
            return _scalar(value[key], key)
    return None


def _scalar(value: Any, field: str) -> str | bool | int | float:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NormalizationError(f"{field} must be finite")
        return value
    if isinstance(value, str):
        return value
    raise NormalizationError(f"{field} must be scalar")


def _number(container: dict[str, Any], fields: tuple[str, ...]) -> float:
    for field in fields:
        if field in container:
            return _finite_number(container[field], field)
    raise NormalizationError(f"data point is missing one of {fields}")


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise NormalizationError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise NormalizationError(f"{field} must be finite")
    return number


def _timestamp(container: dict[str, Any], key: str, *, fallback: str | None = None) -> datetime:
    if key in container:
        return _nanos_timestamp(_integer(container, key))
    if fallback is not None and fallback in container:
        return _nanos_timestamp(_integer(container, fallback))
    raise NormalizationError(f"record is missing {key}")


def _integer(container: dict[str, Any], key: str) -> int:
    value = container.get(key)
    if value is None or isinstance(value, bool):
        raise NormalizationError(f"{key} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"{key} must be an integer") from exc


def _nanos_timestamp(nanoseconds: int) -> datetime:
    if nanoseconds < 0:
        raise NormalizationError("timestamp must not be negative")
    return _EPOCH + timedelta(microseconds=nanoseconds // 1_000)


def _required_string(container: dict[str, Any], key: str) -> str:
    value = _optional_string(container, key)
    if value is None:
        raise NormalizationError(f"record is missing {key}")
    return value


def _optional_string(container: dict[str, Any], key: str) -> str | None:
    value = container.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError(f"{key} must be a non-empty string")
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
