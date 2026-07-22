"""Deterministic low-volume and event-time telemetry-silence detection."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from common.config import LivenessConfig
from contracts import Symptom, SymptomKind


@dataclass(frozen=True, slots=True)
class DropEvaluation:
    """Auditable expected-volume comparison, including insufficient evidence."""

    observed_value: float
    expected_value: float | None
    relative_drop: float | None
    symptom: Symptom | None


@dataclass(frozen=True, slots=True)
class SilenceEvaluation:
    """Auditable event-time staleness comparison, including unconfigured streams."""

    reference_ts: datetime | None
    stale_age_seconds: float | None
    symptom: Symptom | None


class LivenessDetector:
    """Detect measured low volume separately from missing telemetry evidence."""

    def __init__(self, *, configuration: LivenessConfig) -> None:
        self._configuration = configuration

    def evaluate_drop(
        self,
        *,
        service: str,
        signal: str,
        onset_ts: datetime,
        observed_value: float,
        expected_value: float | None,
        evidence_refs: tuple[str, ...],
    ) -> DropEvaluation:
        """Compare a measured window with its caller-supplied expected level."""
        normalized_service, normalized_signal = _validated_stream(service, signal)
        normalized_onset = _utc(onset_ts, name="onset_ts")
        observed = _non_negative_number(observed_value, name="observed_value")
        expected = (
            None
            if expected_value is None
            else _non_negative_number(expected_value, name="expected_value")
        )
        refs = _validated_evidence_refs(evidence_refs)
        stream_key = _stream_key(normalized_service, normalized_signal)
        rule = self._configuration.drop_rules.get(stream_key)
        if rule is None or expected is None or expected < rule.minimum_expected_value:
            return DropEvaluation(
                observed_value=observed,
                expected_value=expected,
                relative_drop=None,
                symptom=None,
            )

        relative_drop = max(expected - observed, 0.0) / expected
        symptom = None
        if relative_drop >= rule.trigger_relative_drop:
            score = min(relative_drop / rule.full_score_relative_drop, 1.0)
            note = (
                f"measured volume fell below its expected level: observed={_render(observed)}, "
                f"expected={_render(expected)}, relative_drop={_render(relative_drop)}, "
                f"trigger={_render(rule.trigger_relative_drop)}, "
                f"minimum_expected={_render(rule.minimum_expected_value)}."
            )
            identity = {
                "kind": SymptomKind.DROP.value,
                "service": normalized_service,
                "signal": normalized_signal,
                "onset_ts": normalized_onset.isoformat(),
                "observed": observed,
                "expected": expected,
                "relative_drop": relative_drop,
                "evidence_refs": refs,
            }
            symptom = Symptom(
                symptom_id=_digest(identity),
                kind=SymptomKind.DROP,
                service=normalized_service,
                signal=normalized_signal,
                onset_ts=normalized_onset,
                score=score,
                note=note,
                evidence_refs=refs,
            )
        return DropEvaluation(
            observed_value=observed,
            expected_value=expected,
            relative_drop=relative_drop,
            symptom=symptom,
        )

    def evaluate_silence(
        self,
        *,
        service: str,
        signal: str,
        expected_since_ts: datetime,
        last_seen_ts: datetime | None,
        watermark_ts: datetime,
        evidence_refs: tuple[str, ...],
    ) -> SilenceEvaluation:
        """Detect a stale or never-seen emitter using only supplied event time."""
        normalized_service, normalized_signal = _validated_stream(service, signal)
        expected_since = _utc(expected_since_ts, name="expected_since_ts")
        watermark = _utc(watermark_ts, name="watermark_ts")
        if expected_since > watermark:
            raise ValueError("expected_since_ts cannot be after watermark_ts")
        last_seen = None if last_seen_ts is None else _utc(last_seen_ts, name="last_seen_ts")
        if last_seen is not None and last_seen > watermark:
            raise ValueError("last_seen_ts cannot be after watermark_ts")
        refs = _validated_evidence_refs(evidence_refs)
        stream_key = _stream_key(normalized_service, normalized_signal)
        rule = self._configuration.silence_rules.get(stream_key)
        if rule is None:
            return SilenceEvaluation(reference_ts=None, stale_age_seconds=None, symptom=None)

        reference = max(expected_since, last_seen) if last_seen is not None else expected_since
        stale_age = (watermark - reference).total_seconds()
        symptom = None
        if stale_age >= rule.maximum_age_seconds:
            onset = reference + timedelta(seconds=rule.maximum_age_seconds)
            score = min(stale_age / rule.full_score_age_seconds, 1.0)
            rendered_last_seen = "none" if last_seen is None else last_seen.isoformat()
            note = (
                f"expected telemetry emitter is stale: last_seen={rendered_last_seen}, "
                f"expected_since={expected_since.isoformat()}, watermark={watermark.isoformat()}, "
                f"stale_age_seconds={_render(stale_age)}, "
                f"maximum_age_seconds={_render(rule.maximum_age_seconds)}."
            )
            identity = {
                "kind": SymptomKind.SILENCE.value,
                "service": normalized_service,
                "signal": normalized_signal,
                "expected_since_ts": expected_since.isoformat(),
                "last_seen_ts": None if last_seen is None else last_seen.isoformat(),
                "watermark_ts": watermark.isoformat(),
                "reference_ts": reference.isoformat(),
                "stale_age_seconds": stale_age,
                "evidence_refs": refs,
            }
            symptom = Symptom(
                symptom_id=_digest(identity),
                kind=SymptomKind.SILENCE,
                service=normalized_service,
                signal=normalized_signal,
                onset_ts=onset,
                score=score,
                note=note,
                evidence_refs=refs,
            )
        return SilenceEvaluation(
            reference_ts=reference,
            stale_age_seconds=stale_age,
            symptom=symptom,
        )


def _validated_stream(service: str, signal: str) -> tuple[str, str]:
    normalized: list[str] = []
    for name, value in (("service", service), ("signal", signal)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        normalized.append(value.strip())
    return normalized[0], normalized[1]


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _non_negative_number(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return normalized


def _validated_evidence_refs(values: tuple[str, ...]) -> tuple[str, ...]:
    refs = tuple(values)
    if not refs:
        raise ValueError("at least one evidence reference is required")
    if any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError("evidence references must be non-empty strings")
    normalized = tuple(sorted(ref.strip() for ref in refs))
    if len(normalized) != len(set(normalized)):
        raise ValueError("evidence references must be unique")
    return normalized


def _stream_key(service: str, signal: str) -> str:
    return f"{service}.{signal}"


def _render(value: float) -> str:
    return format(value, ".12g")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
