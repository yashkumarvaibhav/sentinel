"""Deterministic, scale-free behavioral ratio monitors."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from common.config import BehavioralRatioConfig, BehavioralRatioRuleConfig
from contracts import Symptom, SymptomKind


class BehavioralRatio(StrEnum):
    """Behavioral invariants that should survive a legitimate volume surge."""

    SOURCE_ENTROPY = "source_entropy"
    AUTH_FAILURE = "auth_failure"
    SYN_ACK = "syn_ack"
    RPC_AMPLIFICATION = "rpc_amplification"


@dataclass(frozen=True, slots=True)
class RatioEvaluation:
    """Auditable result for one ratio window, including insufficient evidence."""

    metric: BehavioralRatio
    current: float | None
    baseline: float
    relative_deformation: float | None
    symptom: Symptom | None


_SIGNALS = {
    BehavioralRatio.SOURCE_ENTROPY: "source_entropy",
    BehavioralRatio.AUTH_FAILURE: "auth_failure_ratio",
    BehavioralRatio.SYN_ACK: "syn_ack_ratio",
    BehavioralRatio.RPC_AMPLIFICATION: "rpc_retry_amplification",
}
_LOWER_IS_ABNORMAL = frozenset({BehavioralRatio.SOURCE_ENTROPY})


class BehavioralRatioMonitor:
    """Compute configured deformations and propose evidence-rich ratio symptoms."""

    def __init__(self, *, configuration: BehavioralRatioConfig) -> None:
        self._configuration = configuration

    def evaluate_source_entropy(
        self,
        *,
        service: str,
        onset_ts: datetime,
        source_counts: Mapping[str, int],
        baseline_entropy: float,
        evidence_refs: tuple[str, ...],
    ) -> RatioEvaluation:
        """Compare source-IP Shannon entropy with its context-blind baseline."""
        service, onset_ts, baseline, refs = _validate_common(
            service=service,
            onset_ts=onset_ts,
            baseline=baseline_entropy,
            evidence_refs=evidence_refs,
        )
        counts = _validated_source_counts(source_counts)
        total = sum(counts.values())
        current = shannon_entropy(counts)
        active_sources = sum(count > 0 for count in counts.values())
        return self._evaluate(
            metric=BehavioralRatio.SOURCE_ENTROPY,
            service=service,
            onset_ts=onset_ts,
            current=current,
            baseline=baseline,
            evidence_refs=refs,
            details=f"active_sources={active_sources}, total_samples={total}",
        )

    def evaluate_auth_failure(
        self,
        *,
        service: str,
        onset_ts: datetime,
        failures: int,
        attempts: int,
        baseline_ratio: float,
        evidence_refs: tuple[str, ...],
    ) -> RatioEvaluation:
        """Compare failed authentication attempts with all authentication attempts."""
        failures = _validate_count(failures, name="failures")
        attempts = _validate_count(attempts, name="attempts")
        if failures > attempts:
            raise ValueError("failures cannot exceed attempts")
        service, onset_ts, baseline, refs = _validate_common(
            service=service,
            onset_ts=onset_ts,
            baseline=baseline_ratio,
            evidence_refs=evidence_refs,
        )
        if baseline > 1.0:
            raise ValueError("auth-failure baseline must be less than or equal to 1")
        current = None if attempts == 0 else failures / attempts
        return self._evaluate(
            metric=BehavioralRatio.AUTH_FAILURE,
            service=service,
            onset_ts=onset_ts,
            current=current,
            baseline=baseline,
            evidence_refs=refs,
            details=f"failures={failures}, attempts={attempts}",
        )

    def evaluate_syn_ack(
        self,
        *,
        service: str,
        onset_ts: datetime,
        syn_count: int,
        ack_count: int,
        baseline_ratio: float,
        evidence_refs: tuple[str, ...],
    ) -> RatioEvaluation:
        """Compare initiated TCP handshakes with observed acknowledgements."""
        return self._evaluate_capped_ratio(
            metric=BehavioralRatio.SYN_ACK,
            service=service,
            onset_ts=onset_ts,
            numerator=_validate_count(syn_count, name="syn_count"),
            denominator=_validate_count(ack_count, name="ack_count"),
            baseline=baseline_ratio,
            evidence_refs=evidence_refs,
            numerator_name="syn_count",
            denominator_name="ack_count",
        )

    def evaluate_rpc_amplification(
        self,
        *,
        service: str,
        onset_ts: datetime,
        retries: int,
        requests: int,
        baseline_ratio: float,
        evidence_refs: tuple[str, ...],
    ) -> RatioEvaluation:
        """Compare RPC retries with original requests to expose retry storms."""
        return self._evaluate_capped_ratio(
            metric=BehavioralRatio.RPC_AMPLIFICATION,
            service=service,
            onset_ts=onset_ts,
            numerator=_validate_count(retries, name="retries"),
            denominator=_validate_count(requests, name="requests"),
            baseline=baseline_ratio,
            evidence_refs=evidence_refs,
            numerator_name="retries",
            denominator_name="requests",
        )

    def _evaluate_capped_ratio(
        self,
        *,
        metric: BehavioralRatio,
        service: str,
        onset_ts: datetime,
        numerator: int,
        denominator: int,
        baseline: float,
        evidence_refs: tuple[str, ...],
        numerator_name: str,
        denominator_name: str,
    ) -> RatioEvaluation:
        service, onset_ts, baseline, refs = _validate_common(
            service=service,
            onset_ts=onset_ts,
            baseline=baseline,
            evidence_refs=evidence_refs,
        )
        rule = self._rule(metric)
        ceiling = rule.ratio_ceiling
        if ceiling is None:  # protected by BehavioralRatioConfig validation
            raise RuntimeError(f"{metric.value} has no configured ratio ceiling")
        if baseline > ceiling:
            raise ValueError(f"{metric.value} baseline cannot exceed its ratio ceiling")
        current, capped = _capped_ratio(numerator, denominator, ceiling=ceiling)
        cap_note = f", capped_at={_render(ceiling)}" if capped else ""
        return self._evaluate(
            metric=metric,
            service=service,
            onset_ts=onset_ts,
            current=current,
            baseline=baseline,
            evidence_refs=refs,
            details=(f"{numerator_name}={numerator}, {denominator_name}={denominator}{cap_note}"),
        )

    def _evaluate(
        self,
        *,
        metric: BehavioralRatio,
        service: str,
        onset_ts: datetime,
        current: float | None,
        baseline: float,
        evidence_refs: tuple[str, ...],
        details: str,
    ) -> RatioEvaluation:
        if current is None:
            return RatioEvaluation(
                metric=metric,
                current=None,
                baseline=baseline,
                relative_deformation=None,
                symptom=None,
            )
        if not math.isfinite(current) or current < 0.0:
            raise ValueError("ratio value must be finite and non-negative")

        rule = self._rule(metric)
        directed_delta = baseline - current if metric in _LOWER_IS_ABNORMAL else current - baseline
        deformation = max(directed_delta, 0.0) / max(baseline, rule.baseline_floor)
        if deformation < rule.trigger_relative_deformation:
            return RatioEvaluation(
                metric=metric,
                current=current,
                baseline=baseline,
                relative_deformation=deformation,
                symptom=None,
            )

        score = min(deformation / rule.full_score_relative_deformation, 1.0)
        direction = "fell" if metric in _LOWER_IS_ABNORMAL else "rose"
        signal = _SIGNALS[metric]
        note = (
            f"{metric.value} {direction} beyond baseline: current={_render(current)}, "
            f"baseline={_render(baseline)}, relative_deformation={_render(deformation)}, "
            f"trigger={_render(rule.trigger_relative_deformation)}; {details}."
        )
        identity = {
            "metric": metric.value,
            "service": service,
            "signal": signal,
            "onset_ts": onset_ts.isoformat(),
            "current": current,
            "baseline": baseline,
            "relative_deformation": deformation,
            "evidence_refs": evidence_refs,
            "details": details,
        }
        symptom = Symptom(
            symptom_id=_digest(identity),
            kind=SymptomKind.RATIO_DEFORM,
            service=service,
            signal=signal,
            onset_ts=onset_ts,
            score=score,
            note=note,
            evidence_refs=evidence_refs,
        )
        return RatioEvaluation(
            metric=metric,
            current=current,
            baseline=baseline,
            relative_deformation=deformation,
            symptom=symptom,
        )

    def _rule(self, metric: BehavioralRatio) -> BehavioralRatioRuleConfig:
        if metric is BehavioralRatio.SOURCE_ENTROPY:
            return self._configuration.source_entropy
        if metric is BehavioralRatio.AUTH_FAILURE:
            return self._configuration.auth_failure
        if metric is BehavioralRatio.SYN_ACK:
            return self._configuration.syn_ack
        return self._configuration.rpc_amplification


def shannon_entropy(source_counts: Mapping[str, int]) -> float | None:
    """Return Shannon entropy in bits, or None when the window has no samples."""
    counts = _validated_source_counts(source_counts)
    total = sum(counts.values())
    if total == 0:
        return None
    terms = (-(count / total) * math.log2(count / total) for count in counts.values() if count > 0)
    return max(math.fsum(terms), 0.0)


def _validated_source_counts(source_counts: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(source_counts, Mapping):
        raise TypeError("source_counts must be a mapping")
    counts: dict[str, int] = {}
    for source, count in source_counts.items():
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source identifiers must be non-empty strings")
        normalized = source.strip()
        if normalized in counts:
            raise ValueError("source identifiers must be unique after trimming")
        counts[normalized] = _validate_count(count, name=f"source_counts[{normalized!r}]")
    return counts


def _validate_common(
    *,
    service: str,
    onset_ts: datetime,
    baseline: float,
    evidence_refs: tuple[str, ...],
) -> tuple[str, datetime, float, tuple[str, ...]]:
    if not isinstance(service, str) or not service.strip():
        raise ValueError("service must be a non-empty string")
    if not isinstance(onset_ts, datetime) or onset_ts.utcoffset() != timedelta(0):
        raise ValueError("onset_ts must be timezone-aware UTC")
    if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
        raise TypeError("baseline must be a number")
    normalized_baseline = float(baseline)
    if not math.isfinite(normalized_baseline) or normalized_baseline < 0.0:
        raise ValueError("baseline must be finite and non-negative")
    refs = tuple(evidence_refs)
    if not refs:
        raise ValueError("at least one evidence reference is required")
    if any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError("evidence references must be non-empty strings")
    normalized_refs = tuple(sorted(ref.strip() for ref in refs))
    if len(normalized_refs) != len(set(normalized_refs)):
        raise ValueError("evidence references must be unique")
    return service.strip(), onset_ts.astimezone(UTC), normalized_baseline, normalized_refs


def _validate_count(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _capped_ratio(
    numerator: int,
    denominator: int,
    *,
    ceiling: float,
) -> tuple[float | None, bool]:
    if denominator == 0:
        return (None, False) if numerator == 0 else (ceiling, True)
    try:
        raw = numerator / denominator
    except OverflowError:
        return ceiling, True
    return min(raw, ceiling), raw > ceiling


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
