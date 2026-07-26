"""Reliability KPI snapshot built only from validated measured evidence."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from contracts import (
    DETECTION_LATENCY_HEADLINE_KEY,
    KpiKey,
    KpiMetric,
    KpiResponse,
    KpiStatus,
    KpiWindow,
    ScoreHeadline,
    ScoreProof,
)


class ScoreProofUnavailableError(ValueError):
    """The configured proof cannot support measured KPI claims."""


def load_score_proof(path: Path) -> ScoreProof:
    """Read and strictly validate the scorer's machine artifact."""
    try:
        return ScoreProof.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        raise ScoreProofUnavailableError(f"score proof unavailable: {exc}") from exc


def build_kpi_response(proof: ScoreProof) -> KpiResponse:
    """Map the latest score evidence to the four reliability questions."""
    latency = _headline(proof, DETECTION_LATENCY_HEADLINE_KEY)
    measured_latency = latency is not None and latency.status is KpiStatus.OK
    latency_metric = KpiMetric(
        key=KpiKey.DETECTION_LATENCY,
        label="Detection latency",
        definition=(
            "P95 time from a private labeled symptom onset to the first matching runtime "
            "episode in the latest held-out symptom capture matrix."
        ),
        status=KpiStatus.OK if measured_latency else KpiStatus.INSUFFICIENT,
        value=latency.value if measured_latency and latency is not None else None,
        unit="seconds",
        window=KpiWindow(
            start=proof.evidence_start,
            end=proof.evidence_end,
            description=f"latest held-out score across {len(proof.capture_ids)} captures",
        ),
        sample_count=latency.sample_count if latency is not None else 0,
        provenance=f"{proof.proof_id} · {proof.report_path}",
    )
    return KpiResponse(
        status="ready",
        metrics=(
            latency_metric,
            _insufficient_metric(
                key=KpiKey.AUTONOMOUS_MTTR,
                label="Autonomous MTTR",
                definition=(
                    "Time from an autonomous action being applied to verified recovery of the "
                    "affected service-level objective."
                ),
                unit="seconds",
                reason="No production SLO reader currently verifies action-to-recovery windows.",
            ),
            _insufficient_metric(
                key=KpiKey.QUIET_DAY_FALSE_ACTS,
                label="Quiet-day false acts",
                definition=(
                    "Autonomous actions taken when held-out quiet-day evidence contains no "
                    "fault requiring action."
                ),
                unit="actions",
                reason=(
                    "No held-out action scorer exists yet; zero residual false positives is "
                    "not evidence of zero autonomous actions."
                ),
            ),
            _insufficient_metric(
                key=KpiKey.PROTECTED_COHORT_INTEGRITY,
                label="Protected-cohort integrity",
                definition=(
                    "Share of protected-cohort requests that remain within their committed SLO "
                    "while an autonomous mitigation is active."
                ),
                unit="ratio",
                reason=(
                    "The testbed has no production protected-cohort telemetry reader attached."
                ),
            ),
        ),
        latest_score_proof=proof,
    )


def unavailable_kpi_response(detail: str) -> KpiResponse:
    """Keep all four states typed when the proof itself is unavailable."""
    return KpiResponse(
        status="degraded",
        metrics=(
            _insufficient_metric(
                key=KpiKey.DETECTION_LATENCY,
                label="Detection latency",
                definition=(
                    "P95 time from a private labeled symptom onset to the first matching runtime "
                    "episode in the latest held-out symptom capture matrix."
                ),
                unit="seconds",
                reason="The generated held-out score proof is unavailable.",
            ),
            _insufficient_metric(
                key=KpiKey.AUTONOMOUS_MTTR,
                label="Autonomous MTTR",
                definition=(
                    "Time from an autonomous action being applied to verified recovery of the "
                    "affected service-level objective."
                ),
                unit="seconds",
                reason="No production SLO reader currently verifies action-to-recovery windows.",
            ),
            _insufficient_metric(
                key=KpiKey.QUIET_DAY_FALSE_ACTS,
                label="Quiet-day false acts",
                definition=(
                    "Autonomous actions taken when held-out quiet-day evidence contains no "
                    "fault requiring action."
                ),
                unit="actions",
                reason="No held-out action scorer exists yet.",
            ),
            _insufficient_metric(
                key=KpiKey.PROTECTED_COHORT_INTEGRITY,
                label="Protected-cohort integrity",
                definition=(
                    "Share of protected-cohort requests that remain within their committed SLO "
                    "while an autonomous mitigation is active."
                ),
                unit="ratio",
                reason="No production protected-cohort telemetry reader is attached.",
            ),
        ),
        latest_score_proof=None,
        detail=detail,
    )


def _headline(proof: ScoreProof, key: str) -> ScoreHeadline | None:
    return next((metric for metric in proof.headline_metrics if metric.key == key), None)


def _insufficient_metric(
    *,
    key: KpiKey,
    label: str,
    definition: str,
    unit: str,
    reason: str,
) -> KpiMetric:
    return KpiMetric(
        key=key,
        label=label,
        definition=definition,
        status=KpiStatus.INSUFFICIENT,
        value=None,
        unit=unit,
        window=KpiWindow(start=None, end=None, description=reason),
        sample_count=0,
        provenance=reason,
    )
