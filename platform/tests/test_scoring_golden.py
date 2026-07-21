"""Semantic goldens ignore capture-clock identity but freeze decomposition behavior."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lab.captures.transcript import DecompositionReplay, ReplayStep
from lab.scoring.golden import check_goldens, semantic_transcript, write_goldens

from contracts import DecompFrame, Observation
from detection.decompose import DecompositionStats

_START = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def test_semantic_transcript_ignores_timestamps_and_runtime_ids() -> None:
    first = _replay()
    original_frame = first.steps[0].frame
    assert original_frame is not None
    shifted_step = replace(
        first.steps[0],
        observation=first.steps[0].observation.model_copy(
            update={
                "observation_id": "observation-shifted",
                "ts": _START + timedelta(hours=1),
            }
        ),
        frame=original_frame.model_copy(
            update={
                "frame_id": "frame-shifted",
                "observation_id": "observation-shifted",
                "ts": _START + timedelta(hours=1),
            }
        ),
    )
    shifted = replace(
        first,
        anchor_ts=_START + timedelta(hours=1),
        steps=(shifted_step,),
    )

    assert semantic_transcript(first) == semantic_transcript(shifted)


def test_semantic_transcript_changes_when_residual_behavior_changes() -> None:
    first = _replay()
    original_frame = first.steps[0].frame
    assert original_frame is not None
    changed_frame = original_frame.model_copy(
        update={"explained_event": 0.0, "residual": 6.0, "residual_score": 1.0}
    )
    changed = replace(first, steps=(replace(first.steps[0], frame=changed_frame),))

    assert semantic_transcript(first) != semantic_transcript(changed)


def test_golden_checker_fails_a_reviewed_semantic_change(tmp_path: Path) -> None:
    candidates = {
        "match_night": b'{"behavior":"match"}\n',
        "quiet_day": b'{"behavior":"quiet"}\n',
    }
    write_goldens(candidates, tmp_path)
    check_goldens(candidates, tmp_path)

    changed = {**candidates, "match_night": b'{"behavior":"changed"}\n'}
    with pytest.raises(ValueError, match="semantic golden changed for match_night"):
        check_goldens(changed, tmp_path)


def _replay() -> DecompositionReplay:
    observation = Observation(
        observation_id="observation-original",
        ts=_START,
        service="frontend",
        signal="request_rate",
        value=10.0,
        unit="requests/s",
        attributes={"evidence.source": "captured_real_otel_ingress_spans"},
    )
    frame = DecompFrame(
        frame_id="frame-original",
        observation_id=observation.observation_id,
        ts=observation.ts,
        service=observation.service,
        signal=observation.signal,
        observed=10.0,
        explained_base=4.0,
        explained_event=6.0,
        residual=0.0,
        band_low=8.5,
        band_high=11.5,
        residual_score=0.0,
        context_ids=("simulated-match",),
    )
    return DecompositionReplay(
        capture_id="phase1-match-211-golden-v1",
        scenario_id="match_night",
        seed=211,
        seed_purpose="development",
        capture_config_fingerprint="a" * 64,
        replay_config_fingerprint="a" * 64,
        raw_replay_sha256="b" * 64,
        raw_dead_letters=(),
        anchor_ts=_START,
        expected_span_count=10,
        actual_span_count=10,
        telemetry_completeness=1.0,
        contexts=(),
        steps=(
            ReplayStep(
                observation=observation,
                status="decomposed",
                baseline_updated=False,
                frame=frame,
            ),
        ),
        stats=DecompositionStats(
            warming=0,
            context_blocked_warmup=0,
            decomposed=1,
            baseline_updates=0,
            replays=0,
        ),
    )
