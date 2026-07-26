"""Deterministic machine-readable evidence emitted by held-out scoring."""

from __future__ import annotations

import json
from pathlib import Path

from contracts import (
    DETECTION_LATENCY_HEADLINE_KEY,
    KpiStatus,
    ScoreHeadline,
    ScoreProof,
)
from lab.scoring.evaluator import EpisodeRunScore
from lab.scoring.gates import SymptomGateResult
from lab.scoring.metrics import nearest_rank


def build_symptom_score_proof(
    *,
    runs: tuple[EpisodeRunScore, ...],
    gate: SymptomGateResult,
    config_fingerprint: str,
    report_path: str = "docs/reports/phase-2-held-out-score.md",
) -> ScoreProof:
    """Build the compact proof from the same objects that rendered the report."""
    if not runs:
        raise ValueError("a held-out score proof requires at least one run")
    if any(run.seed_purpose != "held_out" for run in runs):
        raise ValueError("a held-out score proof cannot contain development runs")

    ordered = tuple(
        sorted(
            runs,
            key=lambda run: (
                run.evaluation_start_ts,
                run.scenario_id,
                run.seed,
                run.capture_id,
            ),
        )
    )
    required = tuple(score for score in gate.by_kind if score.kind in set(gate.required_kinds))
    recall_values = tuple(
        score.metrics.recall.value for score in required if score.metrics.recall.value is not None
    )
    recall_complete = len(recall_values) == len(gate.required_kinds)
    latencies = tuple(
        match.detection_latency_seconds for score in required for match in score.matches
    )
    latency_p95 = nearest_rank(latencies, percentile=0.95)
    matched_count = len(latencies)

    return ScoreProof(
        version=1,
        proof_id="phase-2-held-out-symptoms",
        gate_status="pass" if gate.passed else "fail",
        evidence_start=min(run.evaluation_start_ts for run in ordered),
        evidence_end=max(run.evaluation_end_ts for run in ordered),
        telemetry_honesty="REAL",
        stimulus_honesty="SIMULATED",
        seed_purpose="held_out",
        capture_ids=tuple(run.capture_id for run in ordered),
        config_fingerprint=config_fingerprint,
        report_path=report_path,
        headline_metrics=(
            ScoreHeadline(
                key="minimum_symptom_recall",
                label="Minimum symptom recall",
                status=KpiStatus.OK if recall_complete else KpiStatus.INSUFFICIENT,
                value=min(recall_values) if recall_complete else None,
                unit="ratio",
                sample_count=len(required),
            ),
            ScoreHeadline(
                key=DETECTION_LATENCY_HEADLINE_KEY,
                label="Detection latency p95",
                status=KpiStatus(latency_p95.status),
                value=latency_p95.value,
                unit="seconds",
                sample_count=latency_p95.denominator,
            ),
            ScoreHeadline(
                key="matched_symptom_episodes",
                label="Matched symptom episodes",
                status=KpiStatus.OK if matched_count else KpiStatus.INSUFFICIENT,
                value=float(matched_count) if matched_count else None,
                unit="episodes",
                sample_count=matched_count,
            ),
        ),
    )


def render_score_proof(proof: ScoreProof) -> str:
    """Canonical JSON: repeated generation from one score is byte-identical."""
    return json.dumps(proof.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def write_score_proof(path: Path, proof: ScoreProof) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_score_proof(proof), encoding="utf-8")
