"""Deterministic latency and error degradation evidence for dependency edges."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from common.config import EdgeDegradationConfig, EdgeDegradationRuleConfig
from contracts import Symptom, SymptomKind


@dataclass(frozen=True, slots=True)
class DependencyCall:
    """One label-free caller-to-downstream request derived from trace evidence."""

    evidence_id: str
    ts: datetime
    caller: str
    downstream: str
    latency_ms: float
    failed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _identifier(self.evidence_id, name="evidence_id"))
        object.__setattr__(self, "ts", _utc(self.ts, name="ts"))
        object.__setattr__(self, "caller", _identifier(self.caller, name="caller"))
        object.__setattr__(self, "downstream", _identifier(self.downstream, name="downstream"))
        object.__setattr__(
            self,
            "latency_ms",
            _non_negative_number(self.latency_ms, name="latency_ms"),
        )
        if self.caller == self.downstream:
            raise ValueError("caller and downstream must differ")
        if not isinstance(self.failed, bool):
            raise TypeError("failed must be a boolean")


@dataclass(frozen=True, slots=True)
class EdgeDegradationEvaluation:
    """Auditable edge statistics, including explicit insufficient evidence."""

    caller: str
    downstream: str
    sample_count: int
    latency_p95_ms: float
    error_rate: float
    baseline_latency_p95_ms: float | None
    baseline_error_rate: float | None
    latency_relative_rise: float | None
    error_relative_rise: float | None
    symptom: Symptom | None


class EdgeDegradationDetector:
    """Compare one measured dependency edge with context-blind baselines."""

    def __init__(self, *, configuration: EdgeDegradationConfig) -> None:
        self._rules = {(rule.caller, rule.downstream): rule for rule in configuration.rules}

    def evaluate(
        self,
        calls: tuple[DependencyCall, ...],
        *,
        baseline_latency_p95_ms: float | None,
        baseline_error_rate: float | None,
    ) -> EdgeDegradationEvaluation:
        """Measure and verify latency/error rises for exactly one dependency edge."""
        ordered = _validated_calls(calls)
        caller = ordered[0].caller
        downstream = ordered[0].downstream
        sample_count = len(ordered)
        latency_p95_ms = _nearest_rank_p95(tuple(call.latency_ms for call in ordered))
        error_rate = sum(call.failed for call in ordered) / sample_count
        baseline_latency = (
            None
            if baseline_latency_p95_ms is None
            else _non_negative_number(
                baseline_latency_p95_ms,
                name="baseline_latency_p95_ms",
            )
        )
        baseline_errors = (
            None
            if baseline_error_rate is None
            else _probability(baseline_error_rate, name="baseline_error_rate")
        )
        rule = self._rules.get((caller, downstream))
        if (
            rule is None
            or sample_count < rule.minimum_samples
            or baseline_latency is None
            or baseline_errors is None
        ):
            return EdgeDegradationEvaluation(
                caller=caller,
                downstream=downstream,
                sample_count=sample_count,
                latency_p95_ms=latency_p95_ms,
                error_rate=error_rate,
                baseline_latency_p95_ms=baseline_latency,
                baseline_error_rate=baseline_errors,
                latency_relative_rise=None,
                error_relative_rise=None,
                symptom=None,
            )

        latency_relative_rise = _relative_rise(
            latency_p95_ms,
            baseline_latency,
            floor=rule.latency_baseline_floor_ms,
        )
        error_relative_rise = _relative_rise(
            error_rate,
            baseline_errors,
            floor=rule.error_rate_baseline_floor,
        )
        latency_breached = latency_relative_rise >= rule.trigger_relative_latency_rise
        error_breached = error_relative_rise >= rule.trigger_relative_error_rise
        symptom = None
        if latency_breached or error_breached:
            onset_ts = _breach_onset(
                ordered,
                baseline_latency=baseline_latency,
                latency_breached=latency_breached,
                error_breached=error_breached,
                rule=rule,
            )
            score = min(
                max(
                    latency_relative_rise / rule.full_score_relative_latency_rise,
                    error_relative_rise / rule.full_score_relative_error_rise,
                ),
                1.0,
            )
            refs = tuple(call.evidence_id for call in ordered)
            note = (
                f"dependency edge degraded: edge={caller}->{downstream}, "
                f"samples={sample_count}, latency_p95_ms={_render(latency_p95_ms)}, "
                f"baseline_latency_p95_ms={_render(baseline_latency)}, "
                f"latency_relative_rise={_render(latency_relative_rise)}, "
                f"latency_trigger={_render(rule.trigger_relative_latency_rise)}, "
                f"error_rate={_render(error_rate)}, "
                f"baseline_error_rate={_render(baseline_errors)}, "
                f"error_relative_rise={_render(error_relative_rise)}, "
                f"error_trigger={_render(rule.trigger_relative_error_rise)}."
            )
            identity = {
                "kind": SymptomKind.EDGE_DEGRADED.value,
                "caller": caller,
                "downstream": downstream,
                "onset_ts": onset_ts.isoformat(),
                "sample_count": sample_count,
                "latency_p95_ms": latency_p95_ms,
                "error_rate": error_rate,
                "baseline_latency_p95_ms": baseline_latency,
                "baseline_error_rate": baseline_errors,
                "latency_relative_rise": latency_relative_rise,
                "error_relative_rise": error_relative_rise,
                "evidence_refs": refs,
            }
            symptom = Symptom(
                symptom_id=_digest(identity),
                kind=SymptomKind.EDGE_DEGRADED,
                service=caller,
                signal=f"dependency.{downstream}",
                onset_ts=onset_ts,
                score=score,
                note=note,
                evidence_refs=refs,
            )

        return EdgeDegradationEvaluation(
            caller=caller,
            downstream=downstream,
            sample_count=sample_count,
            latency_p95_ms=latency_p95_ms,
            error_rate=error_rate,
            baseline_latency_p95_ms=baseline_latency,
            baseline_error_rate=baseline_errors,
            latency_relative_rise=latency_relative_rise,
            error_relative_rise=error_relative_rise,
            symptom=symptom,
        )


def _validated_calls(calls: tuple[DependencyCall, ...]) -> tuple[DependencyCall, ...]:
    if not isinstance(calls, tuple):
        raise TypeError("calls must be a tuple")
    if not calls:
        raise ValueError("calls must not be empty")
    if any(not isinstance(call, DependencyCall) for call in calls):
        raise TypeError("calls must contain only DependencyCall evidence")
    edges = {(call.caller, call.downstream) for call in calls}
    if len(edges) != 1:
        raise ValueError("calls must describe one caller and downstream edge")
    evidence_ids = [call.evidence_id for call in calls]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("evidence_id values must be unique")
    return tuple(sorted(calls, key=lambda call: (call.ts, call.evidence_id)))


def _nearest_rank_p95(values: tuple[float, ...]) -> float:
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[rank - 1]


def _relative_rise(current: float, baseline: float, *, floor: float) -> float:
    return max(current - baseline, 0.0) / max(baseline, floor)


def _breach_onset(
    calls: tuple[DependencyCall, ...],
    *,
    baseline_latency: float,
    latency_breached: bool,
    error_breached: bool,
    rule: EdgeDegradationRuleConfig,
) -> datetime:
    candidates: list[datetime] = []
    if latency_breached:
        latency_boundary = baseline_latency + (
            rule.trigger_relative_latency_rise
            * max(baseline_latency, rule.latency_baseline_floor_ms)
        )
        candidates.extend(call.ts for call in calls if call.latency_ms >= latency_boundary)
    if error_breached:
        candidates.extend(call.ts for call in calls if call.failed)
    if not candidates:  # defensive: window statistics already proved one breached dimension
        raise RuntimeError("degraded edge has no breach onset evidence")
    return min(candidates)


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if normalized != value:
        raise ValueError(f"{name} must not contain surrounding whitespace")
    return normalized


def _utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _non_negative_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return normalized


def _probability(value: object, *, name: str) -> float:
    normalized = _non_negative_number(value, name=name)
    if normalized > 1.0:
        raise ValueError(f"{name} must be between zero and one")
    return normalized


def _digest(payload: dict[str, object]) -> str:
    rendered = json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _render(value: float) -> str:
    return format(value, ".12g")
