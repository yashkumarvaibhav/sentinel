"""Reliability KPIs expose measurements and missing evidence without conflating them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app
from api.kpis import build_kpi_response
from common.settings import Settings
from contracts import (
    KpiKey,
    KpiMetric,
    KpiStatus,
    KpiWindow,
    ReliabilityMetricEvidence,
    ReliabilityProof,
    ScoreHeadline,
    ScoreProof,
)

START = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _proof() -> ScoreProof:
    return ScoreProof(
        version=1,
        proof_id="phase-2-held-out-symptoms",
        gate_status="pass",
        evidence_start=START,
        evidence_end=START + timedelta(minutes=20),
        telemetry_honesty="REAL",
        stimulus_honesty="SIMULATED",
        seed_purpose="held_out",
        capture_ids=(
            "phase2-cascade-9403-v1",
            "phase2-cascade-9421-v1",
            "phase2-combo-9439-v1",
            "phase2-combo-9457-v1",
        ),
        config_fingerprint="a" * 64,
        report_path="docs/reports/phase-2-held-out-score.md",
        headline_metrics=(
            ScoreHeadline(
                key="minimum_symptom_recall",
                label="Minimum symptom recall",
                status=KpiStatus.OK,
                value=1.0,
                unit="ratio",
                sample_count=7,
            ),
            ScoreHeadline(
                key="detection_latency_p95_seconds",
                label="Detection latency p95",
                status=KpiStatus.OK,
                value=181.6,
                unit="seconds",
                sample_count=20,
            ),
        ),
    )


def _reliability() -> ReliabilityProof:
    return ReliabilityProof(
        version=1,
        proof_id="phase-6-reliability-readers",
        gate_status="pass",
        metrics=(
            ReliabilityMetricEvidence(
                key=KpiKey.AUTONOMOUS_MTTR,
                value=18.4,
                unit="seconds",
                evidence_start=START + timedelta(hours=1),
                evidence_end=START + timedelta(hours=1, seconds=18, milliseconds=400),
                sample_count=1,
                evidence_kind="contained_real_testbed_action",
                source_ids=("contained-payment-scale-1",),
                telemetry_honesty="REAL",
                provenance=(
                    "phase-6-contained-action-recovery · docs/reports/phase-6-reliability-proof.md"
                ),
            ),
            ReliabilityMetricEvidence(
                key=KpiKey.QUIET_DAY_FALSE_ACTS,
                value=0.0,
                unit="actions",
                evidence_start=START + timedelta(hours=2),
                evidence_end=START + timedelta(hours=2, minutes=4),
                sample_count=2,
                evidence_kind="held_out_decision_replay",
                source_ids=("phase1-quiet-7901-v2", "phase1-quiet-7919-v2"),
                telemetry_honesty="REAL",
                provenance=(
                    "phase-6-held-out-quiet-actions · docs/reports/phase-6-reliability-proof.md"
                ),
            ),
        ),
    )


def test_an_ok_metric_requires_a_value_and_samples() -> None:
    with pytest.raises(ValidationError, match="ok KPI"):
        KpiMetric(
            key=KpiKey.DETECTION_LATENCY,
            label="Detection latency",
            definition="Time from labeled symptom onset to the matching episode.",
            status=KpiStatus.OK,
            value=None,
            unit="seconds",
            window=KpiWindow(
                start=START,
                end=START + timedelta(minutes=1),
                description="latest held-out capture matrix",
            ),
            sample_count=0,
            provenance="generated held-out score proof",
        )


def test_an_insufficient_metric_cannot_smuggle_in_a_zero() -> None:
    with pytest.raises(ValidationError, match="insufficient KPI"):
        KpiMetric(
            key=KpiKey.AUTONOMOUS_MTTR,
            label="Autonomous MTTR",
            definition="Time from action application to verified SLO recovery.",
            status=KpiStatus.INSUFFICIENT,
            value=0.0,
            unit="seconds",
            window=KpiWindow(
                start=None,
                end=None,
                description="no production SLO reader is attached",
            ),
            sample_count=0,
            provenance="no measured reader",
        )


def test_only_detection_latency_is_currently_measured() -> None:
    response = build_kpi_response(_proof())

    assert tuple(metric.key for metric in response.metrics) == tuple(KpiKey)
    measured = [metric for metric in response.metrics if metric.status is KpiStatus.OK]
    assert [(metric.key, metric.value, metric.sample_count) for metric in measured] == [
        (KpiKey.DETECTION_LATENCY, 181.6, 20)
    ]
    unavailable = [metric for metric in response.metrics if metric.status is KpiStatus.INSUFFICIENT]
    assert {metric.key for metric in unavailable} == {
        KpiKey.AUTONOMOUS_MTTR,
        KpiKey.QUIET_DAY_FALSE_ACTS,
        KpiKey.PROTECTED_COHORT_INTEGRITY,
    }
    assert all(metric.value is None for metric in unavailable)


def test_the_two_new_readers_measure_mttr_and_a_real_quiet_day_zero() -> None:
    response = build_kpi_response(_proof(), _reliability())

    measured = [metric for metric in response.metrics if metric.status is KpiStatus.OK]
    assert [(metric.key, metric.value, metric.sample_count) for metric in measured] == [
        (KpiKey.DETECTION_LATENCY, 181.6, 20),
        (KpiKey.AUTONOMOUS_MTTR, 18.4, 1),
        (KpiKey.QUIET_DAY_FALSE_ACTS, 0.0, 2),
    ]
    assert response.metrics[2].window.description == (
        "held-out quiet-day decision replay across 2 captures"
    )
    assert response.metrics[3].status is KpiStatus.INSUFFICIENT
    assert response.metrics[3].value is None


def test_the_endpoint_returns_the_validated_proof_and_four_kpis() -> None:
    with TestClient(
        create_app(
            config=Settings(),
            probes={},
            score_proof=_proof(),
            reliability_proof=_reliability(),
        )
    ) as client:
        response = client.get("/api/kpis")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert len(body["metrics"]) == 4
    assert body["latest_score_proof"]["capture_ids"] == [
        "phase2-cascade-9403-v1",
        "phase2-cascade-9421-v1",
        "phase2-combo-9439-v1",
        "phase2-combo-9457-v1",
    ]
    assert [metric["status"] for metric in body["metrics"]] == [
        "ok",
        "ok",
        "ok",
        "insufficient",
    ]


def test_a_missing_proof_fails_closed_to_typed_insufficient_states(tmp_path: Path) -> None:
    config = Settings(SENTINEL_SCORE_PROOF_PATH=tmp_path / "missing.json")

    with TestClient(create_app(config=config, probes={})) as client:
        response = client.get("/api/kpis")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["latest_score_proof"] is None
    assert len(body["metrics"]) == 4
    assert all(metric["status"] == "insufficient" for metric in body["metrics"])
    assert all(metric["value"] is None for metric in body["metrics"])
