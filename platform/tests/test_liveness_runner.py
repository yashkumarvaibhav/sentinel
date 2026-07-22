"""Decomposed rate frames drive deterministic drop and silence episodes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import lab.captures.liveness_transcript as liveness_transcript
import pytest
from lab.captures import load_runtime_capture
from lab.captures.liveness_transcript import replay_liveness_detection
from lab.captures.store import RuntimeCapture

from common.config import (
    EpisodeConfig,
    EpisodePolicyConfig,
    LivenessConfig,
    LivenessStreamConfig,
    load_config,
)
from contracts import DecompFrame, SymptomKind
from detection.episodes import EpisodeAction
from detection.liveness import DropEvaluation
from detection.liveness_runner import (
    LivenessDetectionRunner,
    LivenessWindowAdvance,
    LivenessWindowStatus,
)
from tests.factories import liveness_config

START = datetime(2026, 7, 22, 13, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_drop_episode_uses_only_measured_sufficient_frames() -> None:
    runner = _runner()

    initial = runner.advance(frames=(_frame("initial", tick=0),), tick_ts=START)
    assert _result(initial, SymptomKind.DROP).status is LivenessWindowStatus.CLEAR
    assert _result(initial, SymptomKind.SILENCE).status is LivenessWindowStatus.CLEAR

    actions: list[EpisodeAction] = []
    for tick in range(1, 4):
        result = _result(
            runner.advance(
                frames=(_frame(f"drop-{tick}", tick=tick, observed=2.0),),
                tick_ts=_tick(tick),
            ),
            SymptomKind.DROP,
        )
        assert result.status is LivenessWindowStatus.BREACH
        assert result.transition is not None
        actions.append(result.transition.action)

    assert actions == [EpisodeAction.PENDING, EpisodeAction.PENDING, EpisodeAction.OPENED]
    assert runner.active_episodes()[0].kind is SymptomKind.DROP

    ambiguous = runner.advance(
        frames=(
            _frame("ambiguous-a", tick=4),
            _frame("ambiguous-b", tick=4),
        ),
        tick_ts=_tick(4),
    )
    assert all(item.status is LivenessWindowStatus.INSUFFICIENT for item in ambiguous)
    assert all(item.transition is None for item in ambiguous)
    assert len(runner.active_episodes()) == 1

    missing = runner.advance(frames=(), tick_ts=_tick(5))
    assert _result(missing, SymptomKind.DROP).status is LivenessWindowStatus.INSUFFICIENT
    assert _result(missing, SymptomKind.DROP).transition is None
    assert len(runner.active_episodes()) == 1

    clears: list[EpisodeAction] = []
    for tick in range(6, 9):
        result = _result(
            runner.advance(
                frames=(_frame(f"clear-{tick}", tick=tick),),
                tick_ts=_tick(tick),
            ),
            SymptomKind.DROP,
        )
        assert result.status is LivenessWindowStatus.CLEAR
        assert result.transition is not None
        clears.append(result.transition.action)

    assert clears == [EpisodeAction.CLEARING, EpisodeAction.CLEARING, EpisodeAction.CLOSED]
    assert runner.active_episodes() == ()


def test_silence_episode_opens_from_event_time_and_closes_on_fresh_frames() -> None:
    runner = _runner(maximum_age_seconds=4.0, full_score_age_seconds=8.0)
    runner.advance(frames=(_frame("seen", tick=0),), tick_ts=START)

    actions: list[EpisodeAction] = []
    for tick in range(1, 5):
        result = _result(
            runner.advance(frames=(), tick_ts=_tick(tick)),
            SymptomKind.SILENCE,
        )
        if tick == 1:
            assert result.status is LivenessWindowStatus.CLEAR
        else:
            assert result.status is LivenessWindowStatus.BREACH
            assert result.evaluation is not None
            assert result.transition is not None
            actions.append(result.transition.action)

    assert actions == [EpisodeAction.PENDING, EpisodeAction.PENDING, EpisodeAction.OPENED]
    episode = runner.active_episodes()[0]
    assert episode.kind is SymptomKind.SILENCE
    assert episode.opened_ts == _tick(2)

    closes: list[EpisodeAction] = []
    for tick in range(5, 8):
        result = _result(
            runner.advance(
                frames=(_frame(f"recovered-{tick}", tick=tick),),
                tick_ts=_tick(tick),
            ),
            SymptomKind.SILENCE,
        )
        assert result.status is LivenessWindowStatus.CLEAR
        assert result.transition is not None
        closes.append(result.transition.action)

    assert closes == [EpisodeAction.CLEARING, EpisodeAction.CLEARING, EpisodeAction.CLOSED]
    assert runner.active_episodes() == ()


def test_never_seen_stream_uses_configured_event_time_registration() -> None:
    runner = _runner(maximum_age_seconds=4.0, full_score_age_seconds=8.0)

    runner.advance(frames=(), tick_ts=START)
    runner.advance(frames=(), tick_ts=_tick(1))
    silence = _result(
        runner.advance(frames=(), tick_ts=_tick(2)),
        SymptomKind.SILENCE,
    )

    assert silence.frame_id is None
    assert silence.last_seen_ts is None
    assert silence.status is LivenessWindowStatus.BREACH
    assert silence.evaluation is not None
    assert silence.evaluation.symptom is not None
    assert silence.evaluation.symptom.onset_ts == _tick(2)
    assert silence.evaluation.symptom.evidence_refs[0].startswith("liveness-expectation-")


def test_low_expectation_is_insufficient_and_does_not_tick_drop_episode() -> None:
    runner = _runner()

    advances = runner.advance(
        frames=(_frame("low-expectation", tick=0, observed=0.0, base=1.0),),
        tick_ts=START,
    )

    drop = _result(advances, SymptomKind.DROP)
    assert drop.status is LivenessWindowStatus.INSUFFICIENT
    assert isinstance(drop.evaluation, DropEvaluation)
    assert drop.evaluation.relative_drop is None
    assert drop.transition is None


def test_liveness_runner_is_service_scoped_and_input_order_stable() -> None:
    base = liveness_config()
    configuration = LivenessConfig(
        window_seconds=2,
        dedup_capacity=100,
        streams=(
            LivenessStreamConfig(service="frontend", signal="request_rate"),
            LivenessStreamConfig(service="checkout", signal="request_rate"),
        ),
        drop_rules={
            "frontend.request_rate": base.drop_rules["frontend.request_rate"],
            "checkout.request_rate": base.drop_rules["frontend.request_rate"],
        },
        silence_rules={
            "frontend.request_rate": base.silence_rules["frontend.request_rate"],
            "checkout.request_rate": base.silence_rules["frontend.request_rate"],
        },
    )
    first = _configured_runner(configuration)
    second = _configured_runner(configuration)
    frames = (
        _frame("frontend", tick=0),
        _frame("checkout", tick=0, service="checkout"),
    )

    left = first.advance(frames=frames, tick_ts=START)
    right = second.advance(frames=tuple(reversed(frames)), tick_ts=START)

    assert left == right
    assert [(item.key.service, item.key.kind) for item in left] == [
        ("checkout", SymptomKind.DROP),
        ("checkout", SymptomKind.SILENCE),
        ("frontend", SymptomKind.DROP),
        ("frontend", SymptomKind.SILENCE),
    ]


def test_liveness_runner_exact_retry_and_conflicts_fail_closed() -> None:
    runner = _runner()
    frame = _frame("first", tick=0)
    first = runner.advance(frames=(frame,), tick_ts=START)

    assert runner.advance(frames=(frame,), tick_ts=START) == first
    with pytest.raises(ValueError, match="conflicting liveness advance"):
        runner.advance(frames=(), tick_ts=START)

    changed = frame.model_copy(update={"observed": 9.0, "residual": -1.0})
    with pytest.raises(ValueError, match="reused with different liveness evidence"):
        runner.advance(frames=(changed,), tick_ts=_tick(1))

    with pytest.raises(ValueError, match="complete configured ticks"):
        runner.advance(frames=(), tick_ts=_tick(2))

    future_runner = _runner()
    with pytest.raises(ValueError, match="must equal its runner tick"):
        future_runner.advance(
            frames=(_frame("future", tick=1),),
            tick_ts=START,
        )


def test_real_v6_capture_materializes_liveness_stably() -> None:
    capture_root = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v6"
    if not capture_root.is_dir():
        pytest.skip("local real cascade recovery capture is not present")
    config = load_config(REPO_ROOT / "config")

    first = replay_liveness_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )
    second = replay_liveness_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.raw_dead_letters == ()
    assert len(first.steps) == 72
    assert first.active_episodes == ()
    assert all(len(step.results) == 2 for step in first.steps)
    assert (
        sum(
            item.status is LivenessWindowStatus.BREACH
            for step in first.steps
            for item in step.results
        )
        == 0
    )
    assert (
        sum(
            item.status is LivenessWindowStatus.INSUFFICIENT
            for step in first.steps
            for item in step.results
            if item.key.kind is SymptomKind.DROP
        )
        == 30
    )
    assert (
        sum(
            item.status is LivenessWindowStatus.CLEAR
            for step in first.steps
            for item in step.results
            if item.key.kind is SymptomKind.DROP
        )
        == 42
    )
    assert all(
        item.status is LivenessWindowStatus.CLEAR
        for step in first.steps
        for item in step.results
        if item.key.kind is SymptomKind.SILENCE
    )


def test_capture_liveness_advances_missing_span_ticks_from_schedule_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = load_config(REPO_ROOT / "config").detectors.model_copy(
        update={
            "liveness": liveness_config(
                maximum_age_seconds=4.0,
                full_score_age_seconds=8.0,
            ),
            "episodes": EpisodeConfig(
                policies={
                    kind: EpisodePolicyConfig(
                        open_after_ticks=2,
                        close_after_ticks=2,
                        breach_score=0.5,
                        clear_score=0.2,
                    )
                    for kind in ("DROP", "SILENCE")
                }
            ),
        }
    )
    decomposition = SimpleNamespace(
        anchor_ts=START,
        steps=(
            SimpleNamespace(
                frame=_frame("seen", tick=0),
                observation=SimpleNamespace(ts=START),
            ),
        ),
        raw_replay_sha256="a" * 64,
        raw_dead_letters=(),
    )
    monkeypatch.setattr(
        liveness_transcript, "replay_decomposition", lambda *args, **kwargs: decomposition
    )
    capture = SimpleNamespace(
        manifest=SimpleNamespace(
            capture_id="sparse-capture",
            scenario_id="combo_night",
            seed=503,
            seed_purpose="development",
            config_fingerprint="b" * 64,
            telemetry=SimpleNamespace(tick_seconds=2),
        ),
        schedule=json.dumps(
            {
                "version": 1,
                "scenario_id": "combo_night",
                "honesty": "SIMULATED",
                "seed": 503,
                "seed_purpose": "development",
                "request_mix_seed": 1,
                "target": "astronomy-shop/frontend-proxy",
                "phases": [
                    {
                        "name": "warmup",
                        "start_offset_seconds": 0,
                        "duration_seconds": 4,
                        "rate_rps": 4,
                    },
                    {
                        "name": "silence",
                        "start_offset_seconds": 4,
                        "duration_seconds": 6,
                        "rate_rps": 4,
                    },
                ],
            }
        ).encode(),
    )

    replay = replay_liveness_detection(
        cast(RuntimeCapture, capture),
        detector=detector,
        replay_config_fingerprint="c" * 64,
    )

    assert [step.tick_ts for step in replay.steps] == [
        START + timedelta(seconds=offset) for offset in range(0, 10, 2)
    ]
    assert replay.steps[0].results[0].frame_id == "seen"
    assert all(
        next(item for item in step.results if item.key.kind is SymptomKind.DROP).status
        is LivenessWindowStatus.INSUFFICIENT
        for step in replay.steps[1:]
    )
    assert replay.active_episodes[0].kind is SymptomKind.SILENCE


def _runner(
    *,
    maximum_age_seconds: float = 120.0,
    full_score_age_seconds: float = 300.0,
) -> LivenessDetectionRunner:
    return _configured_runner(
        liveness_config(
            maximum_age_seconds=maximum_age_seconds,
            full_score_age_seconds=full_score_age_seconds,
        )
    )


def _configured_runner(configuration: LivenessConfig) -> LivenessDetectionRunner:
    policy = EpisodePolicyConfig(
        open_after_ticks=3,
        close_after_ticks=3,
        breach_score=0.5,
        clear_score=0.2,
    )
    return LivenessDetectionRunner(
        configuration=configuration,
        episodes=EpisodeConfig(policies={"DROP": policy, "SILENCE": policy}),
        expected_since_ts=START,
    )


def _result(
    advances: tuple[LivenessWindowAdvance, ...],
    kind: SymptomKind,
) -> LivenessWindowAdvance:
    return next(item for item in advances if item.key.kind is kind)


def _tick(tick: int) -> datetime:
    return START + timedelta(seconds=tick * 2)


def _frame(
    frame_id: str,
    *,
    tick: int,
    observed: float = 10.0,
    base: float = 10.0,
    service: str = "frontend",
) -> DecompFrame:
    residual = observed - base
    return DecompFrame(
        frame_id=frame_id,
        observation_id=f"observation-{frame_id}",
        ts=_tick(tick),
        service=service,
        signal="request_rate",
        observed=observed,
        explained_base=base,
        explained_event=0.0,
        residual=residual,
        band_low=base - 1.0,
        band_high=base + 1.0,
        residual_score=min(abs(residual) / max(base, 1.0), 1.0),
    )


def test_liveness_config_requires_unique_known_streams() -> None:
    stream = LivenessStreamConfig(service="frontend", signal="request_rate")
    base = liveness_config()

    with pytest.raises(ValueError, match="unique"):
        LivenessConfig(
            window_seconds=2,
            dedup_capacity=100,
            streams=(stream, stream),
            drop_rules=base.drop_rules,
            silence_rules=base.silence_rules,
        )
