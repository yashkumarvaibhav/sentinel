"""The held-out scorer emits a deterministic machine proof beside its report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from lab.scenarios.models import ScoredSymptomKind, SymptomLabelInterval
from lab.scoring.evaluator import score_symptom_episodes
from lab.scoring.gates import evaluate_symptom_gates, load_gate_config
from lab.scoring.proof import build_symptom_score_proof, render_score_proof

from contracts import EpisodeStatus, SymptomEpisode, SymptomKind

START = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _episode(
    episode_id: str,
    kind: SymptomKind,
    *,
    opened_after: float,
    closed_after: float,
) -> SymptomEpisode:
    opened = START + timedelta(seconds=opened_after)
    confirmed = opened + timedelta(seconds=1)
    return SymptomEpisode(
        episode_id=episode_id,
        kind=kind,
        service="checkout",
        signal="fault.signal",
        opened_ts=opened,
        confirmed_ts=confirmed,
        last_breach_ts=confirmed,
        closed_ts=START + timedelta(seconds=closed_after),
        status=EpisodeStatus.CLOSED,
        peak_score=0.9,
        breach_tick_count=3,
        revision=1,
        opening_symptom_id=f"{episode_id}-open",
        peak_symptom_id=f"{episode_id}-peak",
        latest_symptom_id=f"{episode_id}-latest",
    )


def _label(label_id: str, kind: SymptomKind, start: float, end: float) -> SymptomLabelInterval:
    return SymptomLabelInterval(
        label_id=label_id,
        kind=cast(ScoredSymptomKind, kind.value),
        service="checkout",
        signal="fault.signal",
        start_offset_seconds=start,
        end_offset_seconds=end,
    )


def test_score_proof_uses_evidence_time_and_all_matched_latency_samples() -> None:
    first = score_symptom_episodes(
        capture_id="capture-a",
        scenario_id="combo_night",
        seed=9439,
        seed_purpose="held_out",
        anchor_ts=START,
        evaluation_end_ts=START + timedelta(seconds=60),
        episodes=(
            _episode(
                "residual-a",
                SymptomKind.RESIDUAL_EXCEED,
                opened_after=12,
                closed_after=30,
            ),
        ),
        labels=(_label("label-a", SymptomKind.RESIDUAL_EXCEED, 10, 25),),
    )
    second_start = START + timedelta(hours=1)
    second = score_symptom_episodes(
        capture_id="capture-b",
        scenario_id="cascade_night",
        seed=9403,
        seed_purpose="held_out",
        anchor_ts=second_start,
        evaluation_end_ts=second_start + timedelta(seconds=80),
        episodes=(
            SymptomEpisode(
                episode_id="edge-b",
                kind=SymptomKind.EDGE_DEGRADED,
                service="checkout",
                signal="fault.signal",
                opened_ts=second_start + timedelta(seconds=35),
                confirmed_ts=second_start + timedelta(seconds=36),
                last_breach_ts=second_start + timedelta(seconds=36),
                closed_ts=second_start + timedelta(seconds=51),
                status=EpisodeStatus.CLOSED,
                peak_score=0.9,
                breach_tick_count=3,
                revision=1,
                opening_symptom_id="edge-b-open",
                peak_symptom_id="edge-b-peak",
                latest_symptom_id="edge-b-latest",
            ),
        ),
        labels=(
            SymptomLabelInterval(
                label_id="label-b",
                kind=SymptomKind.EDGE_DEGRADED.value,
                service="checkout",
                signal="fault.signal",
                start_offset_seconds=10,
                end_offset_seconds=50,
            ),
        ),
    )
    config = load_gate_config(REPO_ROOT / "lab" / "scoring" / "config.yml")
    gate = evaluate_symptom_gates(
        (first, second),
        config,
        required_kinds=(SymptomKind.RESIDUAL_EXCEED, SymptomKind.EDGE_DEGRADED),
    )

    proof = build_symptom_score_proof(
        runs=(first, second),
        gate=gate,
        config_fingerprint="b" * 64,
    )

    assert proof.evidence_start == START
    assert proof.evidence_end == second_start + timedelta(seconds=80)
    assert proof.capture_ids == ("capture-a", "capture-b")
    latency = next(
        metric for metric in proof.headline_metrics if metric.key == "detection_latency_p95_seconds"
    )
    assert latency.value == 26.0
    assert latency.sample_count == 2
    assert render_score_proof(proof).endswith("\n")
    assert render_score_proof(proof) == render_score_proof(proof)
