"""Decomposition frames drive residual symptoms into persisted episodes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from common.config import DetectorConfig, EpisodeConfig, EpisodePolicyConfig
from contracts import (
    DecompFrame,
    EpisodeStatus,
    Observation,
    Symptom,
    SymptomEpisode,
    SymptomKind,
)
from detection.decompose import DecompositionEngine
from detection.edges import DependencyCall, EdgeDegradationDetector
from detection.episodes import EpisodeAction, EpisodeKey
from detection.pipeline import (
    EpisodeWorker,
    SymptomEpisodePipeline,
    residual_symptom,
)
from tests.factories import (
    behavioral_ratio_config,
    change_point_saturation_config,
    edge_degradation_config,
    episode_config,
    liveness_config,
    log_template_config,
)

START = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class _RecordingSink:
    """In-memory episode persister that records every write."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, int]] = []
        self.latest: dict[str, SymptomEpisode] = {}

    async def put_episode(self, episode: SymptomEpisode) -> bool:
        self.writes.append((episode.episode_id, episode.status.value, episode.revision))
        self.latest[episode.episode_id] = episode
        return True


def _frame(
    *,
    frame_id: str,
    observed: float,
    explained_base: float,
    residual: float,
    band_low: float,
    band_high: float,
    residual_score: float,
) -> DecompFrame:
    return DecompFrame(
        frame_id=frame_id,
        observation_id=f"obs-{frame_id}",
        ts=START,
        service="frontend",
        signal="request_rate",
        observed=observed,
        explained_base=explained_base,
        explained_event=0.0,
        residual=residual,
        band_low=band_low,
        band_high=band_high,
        residual_score=residual_score,
    )


def test_residual_symptom_emits_only_for_an_upside_band_breach() -> None:
    breach = _frame(
        frame_id="breach",
        observed=16.0,
        explained_base=4.0,
        residual=12.0,
        band_low=2.5,
        band_high=5.5,
        residual_score=1.0,
    )
    within_band = _frame(
        frame_id="calm",
        observed=4.0,
        explained_base=4.0,
        residual=0.0,
        band_low=2.5,
        band_high=5.5,
        residual_score=0.0,
    )
    drop = _frame(
        frame_id="drop",
        observed=1.0,
        explained_base=4.0,
        residual=-3.0,
        band_low=2.5,
        band_high=5.5,
        residual_score=0.5,
    )

    symptom = residual_symptom(breach)
    assert symptom is not None
    assert symptom.kind is SymptomKind.RESIDUAL_EXCEED
    assert symptom.service == "frontend"
    assert symptom.signal == "request_rate"
    assert symptom.score == 1.0
    assert symptom.evidence_refs == ("breach",)
    assert "residual_score=1" in symptom.note

    assert residual_symptom(within_band) is None  # inside the band
    assert residual_symptom(drop) is None  # a downside breach is not RESIDUAL_EXCEED


def test_symptom_id_is_stable_per_frame() -> None:
    breach = _frame(
        frame_id="breach",
        observed=16.0,
        explained_base=4.0,
        residual=12.0,
        band_low=2.5,
        band_high=5.5,
        residual_score=1.0,
    )
    first = residual_symptom(breach)
    second = residual_symptom(breach)
    assert first is not None and second is not None
    assert first.symptom_id == second.symptom_id


def _detector() -> DetectorConfig:
    return DetectorConfig(
        version=1,
        feature_window_seconds=60,
        watermark_lateness_seconds=15,
        ewma_alpha=0.15,
        baseline_warmup_points=3,
        baseline_update_gate_ratio=0.25,
        expected_band_relative_tolerance=0.1,
        absolute_noise_floors={"frontend.request_rate": 1.0},
        behavioral_ratios=behavioral_ratio_config(),
        log_templates=log_template_config(),
        change_point_saturation=change_point_saturation_config(),
        liveness=liveness_config(),
        edge_degradation=edge_degradation_config(),
        episodes=episode_config(open_after_ticks=3, close_after_ticks=3),
    )


def _run(
    values: list[float],
) -> tuple[SymptomEpisodePipeline, _RecordingSink, list[EpisodeAction]]:
    detector = _detector()
    engine = DecompositionEngine(configuration=detector, dedup_capacity=len(values) + 1)
    pipeline = SymptomEpisodePipeline(configuration=detector.episodes)
    sink = _RecordingSink()
    worker = EpisodeWorker(pipeline=pipeline, sink=sink)
    actions: list[EpisodeAction] = []

    async def drive() -> None:
        for index, value in enumerate(values):
            observation = Observation(
                observation_id=f"o{index}",
                ts=START + timedelta(seconds=2 * index),
                service="frontend",
                signal="request_rate",
                value=value,
                unit="requests/s",
            )
            result = engine.decompose(observation)
            if result.frame is None:
                continue
            actions.append((await worker.handle_frame(result.frame)).action)

    asyncio.run(drive())
    return pipeline, sink, actions


def test_pipeline_opens_and_closes_a_residual_episode_end_to_end() -> None:
    # 3 warmup ticks, 5 sustained breaches, 3 clears.
    _, sink, actions = _run([4.0, 4.0, 4.0] + [16.0] * 5 + [4.0] * 3)

    assert actions == [
        EpisodeAction.PENDING,
        EpisodeAction.PENDING,
        EpisodeAction.OPENED,
        EpisodeAction.SUSTAINED,
        EpisodeAction.SUSTAINED,
        EpisodeAction.CLEARING,
        EpisodeAction.CLEARING,
        EpisodeAction.CLOSED,
    ]

    # Persisted only when the durable record changed: open, two sustains, close.
    statuses = [(status, revision) for _, status, revision in sink.writes]
    assert statuses == [
        (EpisodeStatus.ACTIVE.value, 1),
        (EpisodeStatus.ACTIVE.value, 2),
        (EpisodeStatus.ACTIVE.value, 3),
        (EpisodeStatus.CLOSED.value, 4),
    ]
    closed = sink.latest[sink.writes[-1][0]]
    assert closed.status is EpisodeStatus.CLOSED
    assert closed.kind is SymptomKind.RESIDUAL_EXCEED
    assert closed.service == "frontend"
    assert closed.breach_tick_count == 5
    assert closed.peak_score == 1.0
    assert closed.closed_ts is not None


def test_sub_persistence_residual_blip_never_persists_an_episode() -> None:
    # 3 warmup, then a single breach, then calm: never reaches open persistence.
    pipeline, sink, actions = _run([4.0, 4.0, 4.0, 16.0, 4.0, 4.0])

    assert EpisodeAction.OPENED not in actions
    assert sink.writes == []
    assert pipeline.active_episodes() == ()


def test_pipeline_replay_is_deterministic() -> None:
    values = [4.0, 4.0, 4.0] + [16.0] * 4 + [4.0] * 3
    _, first_sink, first_actions = _run(values)
    _, second_sink, second_actions = _run(values)

    assert first_actions == second_actions
    assert first_sink.writes == second_sink.writes


_CONFIGURED_KINDS = (
    SymptomKind.RESIDUAL_EXCEED,
    SymptomKind.RATIO_DEFORM,
    SymptomKind.LOG_BURST,
    SymptomKind.EDGE_DEGRADED,
    SymptomKind.SATURATION,
    SymptomKind.DROP,
    SymptomKind.SILENCE,
)


def _all_kinds_config() -> EpisodeConfig:
    policy = EpisodePolicyConfig(
        open_after_ticks=3, close_after_ticks=3, breach_score=0.5, clear_score=0.2
    )
    return EpisodeConfig(policies={kind.value: policy for kind in _CONFIGURED_KINDS})


def _symptom_of(kind: SymptomKind, index: int, *, score: float = 0.9) -> Symptom:
    return Symptom(
        symptom_id=f"{kind.value}-{index}",
        kind=kind,
        service="frontend",
        signal="request_rate",
        onset_ts=START + timedelta(seconds=index),
        score=score,
        note="synthetic detector symptom",
        evidence_refs=(f"ev-{index}",),
    )


@pytest.mark.parametrize("kind", _CONFIGURED_KINDS)
def test_every_configured_symptom_kind_drives_an_episode(kind: SymptomKind) -> None:
    pipeline = SymptomEpisodePipeline(configuration=_all_kinds_config())
    key = EpisodeKey(kind, "frontend", "request_rate")

    opened = None
    for index in range(3):  # three sustained breaches open the episode
        opened = pipeline.observe_symptom(
            _symptom_of(kind, index), tick_ts=START + timedelta(seconds=index)
        )
    assert opened is not None
    assert opened.action is EpisodeAction.OPENED
    assert opened.episode is not None
    assert opened.episode.kind is kind

    closed = None
    for index in range(3, 6):  # three clears close it (a monitored key gone quiet)
        closed = pipeline.observe(key=key, tick_ts=START + timedelta(seconds=index), symptom=None)
    assert closed is not None
    assert closed.action is EpisodeAction.CLOSED
    assert pipeline.active_episodes() == ()


def test_unconfigured_symptom_kind_is_ignored_by_the_router() -> None:
    pipeline = SymptomEpisodePipeline(configuration=_all_kinds_config())
    marker = _symptom_of(SymptomKind.DEPLOY_MARKER, 0)  # no episode policy for markers

    transition = pipeline.observe_symptom(marker, tick_ts=START)

    assert transition.action is EpisodeAction.IGNORED
    assert transition.episode is None
    assert pipeline.active_episodes() == ()


def _degraded_edge_symptom(detector: EdgeDegradationDetector, tick: int) -> Symptom:
    calls = tuple(
        DependencyCall(
            evidence_id=f"call-{tick}-{sample}",
            ts=START + timedelta(seconds=tick, milliseconds=sample),
            caller="frontend",
            downstream="checkout",
            latency_ms=latency,
            failed=False,
        )
        for sample, latency in enumerate((100.0, 110.0, 120.0, 300.0, 320.0))
    )
    evaluation = detector.evaluate(calls, baseline_latency_p95_ms=100.0, baseline_error_rate=0.01)
    assert evaluation.symptom is not None
    return evaluation.symptom


def test_real_edge_detector_symptoms_open_an_episode_through_the_pipeline() -> None:
    # Prove the full chain with a real producer: a genuine EdgeDegradationDetector
    # symptom, routed by kind/service/signal, opens the right episode.
    detector = EdgeDegradationDetector(configuration=edge_degradation_config())
    pipeline = SymptomEpisodePipeline(configuration=_all_kinds_config())

    opened = None
    for tick in range(3):
        symptom = _degraded_edge_symptom(detector, tick)
        assert symptom.kind is SymptomKind.EDGE_DEGRADED
        opened = pipeline.observe_symptom(symptom, tick_ts=START + timedelta(seconds=tick))

    assert opened is not None
    assert opened.action is EpisodeAction.OPENED
    assert opened.episode is not None
    assert opened.episode.kind is SymptomKind.EDGE_DEGRADED
    assert opened.episode.service == "frontend"
    assert opened.episode.signal == "dependency.checkout"
