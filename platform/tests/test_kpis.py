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


def test_the_endpoint_returns_the_validated_proof_and_four_kpis() -> None:
    with TestClient(create_app(config=Settings(), probes={}, score_proof=_proof())) as client:
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
