"""Normalized ingress spans drive deterministic ratio windows and episodes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lab.captures import load_runtime_capture
from lab.captures.ratio_transcript import replay_ingress_ratios

from common.config import EpisodeConfig, EpisodePolicyConfig, load_config
from contracts import Observation, SymptomKind
from detection.episodes import EpisodeAction, EpisodeKey
from detection.ratio_runner import (
    IngressRatioDetectionRunner,
    RatioWindowAdvance,
    RatioWindowStatus,
)
from detection.ratios import BehavioralRatio
from tests.factories import behavioral_ratio_config

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
DIVERSE_PATHS = ("/a", "/b", "/c", "/d", "/a", "/b", "/c", "/d")
CONCENTRATED_PATHS = ("/products",) * 8
IRREGULAR_GAPS = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)
REGULAR_GAPS = (2.0,) * 7


def test_path_entropy_episode_uses_only_sufficient_unambiguous_windows() -> None:
    runner = _runner()

    warmup = runner.advance(
        observations=_window("warmup", window=0),
        tick_ts=START + timedelta(seconds=60),
    )
    assert [_result(warmup, metric).status for metric in _metrics()] == [
        RatioWindowStatus.WARMING,
        RatioWindowStatus.WARMING,
    ]

    actions: list[EpisodeAction] = []
    for window in range(1, 4):
        result = _result(
            runner.advance(
                observations=_window(
                    f"path-breach-{window}",
                    window=window,
                    paths=CONCENTRATED_PATHS,
                ),
                tick_ts=START + timedelta(seconds=(window + 1) * 60),
            ),
            BehavioralRatio.PATH_ENTROPY,
        )
        assert result.status is RatioWindowStatus.BREACH
        assert result.evaluation is not None
        assert result.evaluation.symptom is not None
        assert result.transition is not None
        actions.append(result.transition.action)

    assert actions == [
        EpisodeAction.PENDING,
        EpisodeAction.PENDING,
        EpisodeAction.OPENED,
    ]
    assert runner.active_episodes()[0].signal == "path_entropy"

    invalid_windows = (
        _window("sparse", window=4, paths=DIVERSE_PATHS[:5], gaps=IRREGULAR_GAPS[:4]),
        (
            *_window("bad-url-valid", window=5),
            _span("bad-url", offset=301.0, url=7),
        ),
        (
            *_window("missing-ref-valid", window=6),
            _span("missing-ref", offset=361.0, trace_refs=()),
        ),
    )
    for window, observations in enumerate(invalid_windows, start=4):
        advances = runner.advance(
            observations=observations,
            tick_ts=START + timedelta(seconds=(window + 1) * 60),
        )
        assert all(item.status is RatioWindowStatus.INSUFFICIENT for item in advances)
        assert all(item.transition is None for item in advances)
        assert len(runner.active_episodes()) == 1

    clears: list[EpisodeAction] = []
    for window in range(7, 10):
        result = _result(
            runner.advance(
                observations=_window(f"path-clear-{window}", window=window),
                tick_ts=START + timedelta(seconds=(window + 1) * 60),
            ),
            BehavioralRatio.PATH_ENTROPY,
        )
        assert result.status is RatioWindowStatus.CLEAR
        assert result.transition is not None
        clears.append(result.transition.action)

    assert clears == [
        EpisodeAction.CLEARING,
        EpisodeAction.CLEARING,
        EpisodeAction.CLOSED,
    ]
    assert runner.active_episodes() == ()


def test_interarrival_episode_is_independent_of_stable_path_diversity() -> None:
    runner = _runner()
    runner.advance(
        observations=_window("warmup", window=0),
        tick_ts=START + timedelta(seconds=60),
    )

    actions: list[EpisodeAction] = []
    for window in range(1, 4):
        advances = runner.advance(
            observations=_window(
                f"timing-breach-{window}",
                window=window,
                gaps=REGULAR_GAPS,
            ),
            tick_ts=START + timedelta(seconds=(window + 1) * 60),
        )
        path = _result(advances, BehavioralRatio.PATH_ENTROPY)
        timing = _result(advances, BehavioralRatio.INTERARRIVAL_VARIATION)
        assert path.status is RatioWindowStatus.CLEAR
        assert timing.status is RatioWindowStatus.BREACH
        assert timing.transition is not None
        actions.append(timing.transition.action)

    assert actions == [
        EpisodeAction.PENDING,
        EpisodeAction.PENDING,
        EpisodeAction.OPENED,
    ]
    assert runner.active_episodes()[0].signal == "interarrival_cv"

    clears: list[EpisodeAction] = []
    for window in range(4, 7):
        result = _result(
            runner.advance(
                observations=_window(f"timing-clear-{window}", window=window),
                tick_ts=START + timedelta(seconds=(window + 1) * 60),
            ),
            BehavioralRatio.INTERARRIVAL_VARIATION,
        )
        assert result.status is RatioWindowStatus.CLEAR
        assert result.transition is not None
        clears.append(result.transition.action)

    assert clears == [
        EpisodeAction.CLEARING,
        EpisodeAction.CLEARING,
        EpisodeAction.CLOSED,
    ]
    assert runner.active_episodes() == ()


def test_ratio_runner_is_service_scoped_and_replay_order_stable() -> None:
    mappings = {"frontend-proxy": "frontend", "checkout-proxy": "checkout"}
    first = _runner(mappings)
    second = _runner(mappings)
    baseline = _window("frontend-base", window=0) + _window(
        "checkout-base",
        window=0,
        source_service="checkout-proxy",
    )
    current = _window("frontend-current", window=1)

    assert first.monitored_keys == (
        EpisodeKey(SymptomKind.RATIO_DEFORM, "checkout", "path_entropy"),
        EpisodeKey(SymptomKind.RATIO_DEFORM, "checkout", "interarrival_cv"),
        EpisodeKey(SymptomKind.RATIO_DEFORM, "frontend", "path_entropy"),
        EpisodeKey(SymptomKind.RATIO_DEFORM, "frontend", "interarrival_cv"),
    )
    first.advance(observations=baseline, tick_ts=START + timedelta(seconds=60))
    second.advance(
        observations=tuple(reversed(baseline)),
        tick_ts=START + timedelta(seconds=60),
    )
    left = first.advance(observations=current, tick_ts=START + timedelta(seconds=120))
    right = second.advance(
        observations=tuple(reversed(current)),
        tick_ts=START + timedelta(seconds=120),
    )

    assert left == right
    assert [item.status for item in left[:2]] == [RatioWindowStatus.INSUFFICIENT] * 2
    assert [item.status for item in left[2:]] == [RatioWindowStatus.CLEAR] * 2


def test_distinct_ingress_observations_can_share_one_trace_without_undercounting() -> None:
    runner = _runner()
    observations = tuple(
        item.model_copy(update={"trace_refs": ("shared-page-trace",)})
        for item in _window("shared-trace", window=0)
    )

    advances = runner.advance(
        observations=observations,
        tick_ts=START + timedelta(seconds=60),
    )

    assert all(item.status is RatioWindowStatus.WARMING for item in advances)
    assert all(item.window_request_count == 8 for item in advances)
    assert _result(advances, BehavioralRatio.PATH_ENTROPY).current == pytest.approx(2.0)


def test_ratio_runner_exact_retry_and_conflicts_fail_closed() -> None:
    runner = _runner()
    original = _window("baseline", window=0)
    reordered = tuple(
        item.model_copy(update={"attributes": dict(reversed(tuple(item.attributes.items())))})
        for item in original
    )
    tick = START + timedelta(seconds=60)
    first = runner.advance(observations=original, tick_ts=tick)

    assert runner.advance(observations=reordered, tick_ts=tick) == first
    with pytest.raises(ValueError, match="conflicting ingress ratio advance"):
        runner.advance(observations=(), tick_ts=tick)

    changed = original[0].model_copy(update={"value": 999.0})
    with pytest.raises(ValueError, match="reused with different ingress evidence"):
        runner.advance(
            observations=(changed,),
            tick_ts=START + timedelta(seconds=120),
        )

    with pytest.raises(ValueError, match="complete configured windows"):
        runner.advance(
            observations=(),
            tick_ts=START + timedelta(seconds=180),
        )

    future_runner = _runner()
    with pytest.raises(ValueError, match="timestamp exceeds its runner tick"):
        future_runner.advance(
            observations=(_span("future", offset=60.1),),
            tick_ts=START + timedelta(seconds=60),
        )


def test_real_v6_capture_materializes_ingress_ratios_stably() -> None:
    capture_root = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v6"
    if not capture_root.is_dir():
        pytest.skip("local real cascade recovery capture is not present")
    config = load_config(REPO_ROOT / "config")

    first = replay_ingress_ratios(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )
    second = replay_ingress_ratios(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert len(first.steps) == 2
    assert [item.window_request_count for item in first.steps[0].results] == [308, 308]
    assert [item.window_request_count for item in first.steps[1].results] == [767, 767]
    assert [item.status for item in first.steps[0].results] == [RatioWindowStatus.WARMING] * 2
    assert [item.status for item in first.steps[1].results] == [RatioWindowStatus.CLEAR] * 2
    assert _result(first.steps[0].results, BehavioralRatio.PATH_ENTROPY).current == pytest.approx(
        2.4148864305900326
    )
    assert _result(
        first.steps[0].results, BehavioralRatio.INTERARRIVAL_VARIATION
    ).current == pytest.approx(0.4451111820945194)
    assert _result(first.steps[1].results, BehavioralRatio.PATH_ENTROPY).current == pytest.approx(
        2.2597074535257287
    )
    assert _result(
        first.steps[1].results, BehavioralRatio.INTERARRIVAL_VARIATION
    ).current == pytest.approx(0.5762182519963004)
    assert first.active_episodes == ()


def _runner(
    service_mappings: dict[str, str] | None = None,
) -> IngressRatioDetectionRunner:
    return IngressRatioDetectionRunner(
        configuration=behavioral_ratio_config(
            ingress_service_mappings=service_mappings or {"frontend-proxy": "frontend"}
        ),
        episodes=EpisodeConfig(
            policies={
                SymptomKind.RATIO_DEFORM.value: EpisodePolicyConfig(
                    open_after_ticks=3,
                    close_after_ticks=3,
                    breach_score=0.5,
                    clear_score=0.2,
                )
            }
        ),
    )


def _metrics() -> tuple[BehavioralRatio, BehavioralRatio]:
    return BehavioralRatio.PATH_ENTROPY, BehavioralRatio.INTERARRIVAL_VARIATION


def _result(
    advances: tuple[RatioWindowAdvance, ...],
    metric: BehavioralRatio,
    *,
    service: str = "frontend",
) -> RatioWindowAdvance:
    return next(item for item in advances if item.metric is metric and item.key.service == service)


def _window(
    prefix: str,
    *,
    window: int,
    paths: tuple[str, ...] = DIVERSE_PATHS,
    gaps: tuple[float, ...] = IRREGULAR_GAPS,
    source_service: str = "frontend-proxy",
) -> tuple[Observation, ...]:
    assert len(gaps) == len(paths) - 1
    offsets = [window * 60 + 0.1]
    for gap in gaps:
        offsets.append(offsets[-1] + gap)
    return tuple(
        _span(
            f"{prefix}-{index}",
            offset=offset,
            url=f"http://frontend-proxy:8080{path}?request={index}",
            source_service=source_service,
        )
        for index, (path, offset) in enumerate(zip(paths, offsets, strict=True))
    )


def _span(
    observation_id: str,
    *,
    offset: float,
    url: str | int = "http://frontend-proxy:8080/",
    trace_refs: tuple[str, ...] | None = None,
    source_service: str = "frontend-proxy",
) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=START + timedelta(seconds=offset),
        service=source_service,
        signal="span.duration_ms",
        value=12.0,
        unit="ms",
        attributes={"span.kind": 2, "http.url": url, "http.status_code": "200"},
        trace_refs=(f"trace-{observation_id}",) if trace_refs is None else trace_refs,
    )
