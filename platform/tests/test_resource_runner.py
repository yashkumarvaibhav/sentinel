"""Real container gauges drive saturation windows and episode ticks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lab.captures import load_runtime_capture
from lab.captures.resource_transcript import replay_resource_detection

from common.config import (
    ChangePointSaturationConfig,
    EpisodeConfig,
    EpisodePolicyConfig,
    ResourceWindowConfig,
    ResourceWindowRuleConfig,
    load_config,
)
from contracts import Observation, SymptomKind
from detection.episodes import EpisodeAction
from detection.resource_runner import (
    ResourceDetectionRunner,
    ResourceWindowStatus,
)

START = datetime(2026, 7, 22, 17, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
CAPACITY = 1_000.0


def test_resource_runner_opens_only_after_verified_leak_and_sufficient_series() -> None:
    runner = _runner()
    values = (500.0,) * 8 + (520.0, 560.0, 610.0, 670.0, 740.0, 810.0, 860.0, 900.0)

    results = []
    for tick, value in enumerate(values, start=1):
        observations = [_used(f"used-{tick}", tick=tick, value=value)]
        if tick == 1:
            observations.append(_capacity("capacity-1", tick=tick))
        result = runner.advance(observations=tuple(observations), tick_ts=_tick(tick))[0]
        results.append(result)

    assert [item.status for item in results[:11]] == [ResourceWindowStatus.WARMING] * 11
    assert all(item.transition is None for item in results[:11])
    breaches = [item for item in results if item.status is ResourceWindowStatus.BREACH]
    assert len(breaches) >= 2
    assert breaches[0].transition is not None
    assert breaches[0].transition.action is EpisodeAction.PENDING
    assert breaches[1].transition is not None
    assert breaches[1].transition.action is EpisodeAction.OPENED
    assert results[-1].status is ResourceWindowStatus.BREACH
    assert results[-1].sample_count == 16
    assert results[-1].capacity == CAPACITY
    assert results[-1].evaluation is not None
    assert results[-1].evaluation.symptom is not None
    assert results[-1].evaluation.symptom.kind is SymptomKind.SATURATION
    assert results[-1].evaluation.symptom.signal == "container_memory"
    assert "capacity-1" in results[-1].evaluation.symptom.evidence_refs
    assert runner.active_episodes()[0].kind is SymptomKind.SATURATION


def test_missing_measurement_never_clears_and_recovery_requires_fresh_series() -> None:
    runner = _runner()
    values = (500.0,) * 8 + (520.0, 560.0, 610.0, 670.0, 740.0, 810.0, 860.0, 900.0)
    for tick, value in enumerate(values, start=1):
        observations = [_used(f"leak-{tick}", tick=tick, value=value)]
        if tick == 1:
            observations.append(_capacity("capacity", tick=tick))
        runner.advance(observations=tuple(observations), tick_ts=_tick(tick))
    assert len(runner.active_episodes()) == 1

    missing = runner.advance(observations=(), tick_ts=_tick(17))[0]
    assert missing.status is ResourceWindowStatus.INSUFFICIENT
    assert missing.transition is None
    assert missing.sample_count == 0
    assert len(runner.active_episodes()) == 1

    clears = []
    for tick in range(18, 33):
        result = runner.advance(
            observations=(_used(f"healthy-{tick}", tick=tick, value=500.0),),
            tick_ts=_tick(tick),
        )[0]
        if result.transition is not None:
            clears.append(result.transition.action)

    assert clears[-4:] == [
        EpisodeAction.CLEARING,
        EpisodeAction.CLEARING,
        EpisodeAction.CLEARING,
        EpisodeAction.CLOSED,
    ]
    assert runner.active_episodes() == ()


def test_restart_and_capacity_change_reset_series_without_fabricating_clear() -> None:
    runner = _runner()
    first = runner.advance(
        observations=(
            _capacity("capacity-a", tick=1, pod="pod-a"),
            _used("used-a", tick=1, value=500.0, pod="pod-a"),
        ),
        tick_ts=_tick(1),
    )[0]
    assert first.sample_count == 1

    no_capacity = runner.advance(
        observations=(_used("used-b-missing-cap", tick=2, value=500.0, pod="pod-b"),),
        tick_ts=_tick(2),
    )[0]
    assert no_capacity.status is ResourceWindowStatus.INSUFFICIENT
    assert no_capacity.instance_id == "pod-b"
    assert no_capacity.sample_count == 0

    restarted = runner.advance(
        observations=(
            _capacity("capacity-b", tick=3, pod="pod-b"),
            _used("used-b", tick=3, value=500.0, pod="pod-b"),
        ),
        tick_ts=_tick(3),
    )[0]
    assert restarted.status is ResourceWindowStatus.WARMING
    assert restarted.instance_id == "pod-b"
    assert restarted.sample_count == 1

    changed = runner.advance(
        observations=(
            _capacity("capacity-changed", tick=4, pod="pod-b", value=2_000.0),
            _used("used-after-change", tick=4, value=500.0, pod="pod-b"),
        ),
        tick_ts=_tick(4),
    )[0]
    assert changed.status is ResourceWindowStatus.INSUFFICIENT
    assert changed.sample_count == 0
    assert changed.ambiguous_observation_ids == ("capacity-changed",)
    assert changed.transition is None


def test_conflicting_or_unsupported_resource_evidence_is_insufficient() -> None:
    conflicting = _runner().advance(
        observations=(
            _capacity("capacity", tick=1),
            _used("used-a", tick=1, value=500.0),
            _used("used-b", tick=1, value=600.0),
        ),
        tick_ts=_tick(1),
    )[0]
    unsupported = _runner().advance(
        observations=(
            _capacity("capacity", tick=1),
            _used("used", tick=1, value=500.0).model_copy(update={"unit": "MiBy"}),
        ),
        tick_ts=_tick(1),
    )[0]

    assert conflicting.status is ResourceWindowStatus.INSUFFICIENT
    assert conflicting.ambiguous_observation_ids == ("used-a", "used-b")
    assert conflicting.sample_count == 0
    assert unsupported.status is ResourceWindowStatus.INSUFFICIENT
    assert unsupported.ambiguous_observation_ids == ("used",)


def test_semantic_duplicates_are_collapsed_and_input_order_is_stable() -> None:
    observations = (
        _capacity("capacity-b", tick=1),
        _used("used-b", tick=1, value=500.0),
        _capacity("capacity-a", tick=1),
        _used("used-a", tick=1, value=500.0),
    )

    first = _runner().advance(observations=observations, tick_ts=_tick(1))
    second = _runner().advance(observations=tuple(reversed(observations)), tick_ts=_tick(1))

    assert first == second
    assert first[0].status is ResourceWindowStatus.WARMING
    assert first[0].sample_count == 1
    assert first[0].used_evidence_id == "used-a"
    assert first[0].capacity_evidence_id == "capacity-a"


def test_resource_runner_exact_retry_and_event_time_conflicts_fail_closed() -> None:
    runner = _runner()
    observations = (_capacity("capacity", tick=1), _used("used", tick=1, value=500.0))
    first = runner.advance(observations=observations, tick_ts=_tick(1))

    assert runner.advance(observations=tuple(reversed(observations)), tick_ts=_tick(1)) == first
    with pytest.raises(ValueError, match="conflicting resource advance"):
        runner.advance(observations=(), tick_ts=_tick(1))
    with pytest.raises(ValueError, match="complete configured ticks"):
        runner.advance(observations=(), tick_ts=_tick(3))

    changed = observations[1].model_copy(update={"value": 600.0})
    with pytest.raises(ValueError, match="reused with different resource evidence"):
        runner.advance(observations=(changed,), tick_ts=_tick(2))

    future = _runner()
    with pytest.raises(ValueError, match="timestamp exceeds its runner tick"):
        future.advance(
            observations=(
                _capacity("future-capacity", tick=2),
                _used("future-used", tick=2, value=500.0),
            ),
            tick_ts=_tick(1),
        )


@pytest.mark.lab
def test_real_v6_capture_is_explicitly_insufficient_for_every_resource_tick() -> None:
    capture_root = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v6"
    if not capture_root.is_dir():
        pytest.skip("local REAL v6 capture is not present")
    config = load_config(REPO_ROOT / "config")

    replay = replay_resource_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert replay.raw_dead_letters == ()
    assert replay.active_episodes == ()
    assert len(replay.steps) == 14
    assert all(
        result.status is ResourceWindowStatus.INSUFFICIENT
        for step in replay.steps
        for result in step.results
    )


@pytest.mark.lab
def test_real_v7_capture_materializes_all_topology_resources_stably() -> None:
    capture_root = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v7"
    if not capture_root.is_dir():
        pytest.skip("local REAL v7 capture is not present")
    config = load_config(REPO_ROOT / "config")

    first = replay_resource_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )
    second = replay_resource_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.raw_dead_letters == ()
    assert first.private_labels_read is False
    assert first.active_episodes == ()
    assert len(first.steps) == 14
    assert all(len(step.results) == 5 for step in first.steps)
    assert (
        sum(
            result.status is ResourceWindowStatus.WARMING
            for step in first.steps
            for result in step.results
        )
        == 55
    )
    assert (
        sum(
            result.status is ResourceWindowStatus.CLEAR
            for step in first.steps
            for result in step.results
        )
        == 15
    )
    assert {
        result.key.service
        for step in first.steps
        for result in step.results
        if result.sample_count == 14
    } == {"cart", "checkout", "email", "frontend", "payment"}


def _runner() -> ResourceDetectionRunner:
    return ResourceDetectionRunner(
        configuration=_configuration(),
        episodes=EpisodeConfig(
            policies={
                "SATURATION": EpisodePolicyConfig(
                    open_after_ticks=2,
                    close_after_ticks=4,
                    breach_score=0.5,
                    clear_score=0.2,
                )
            }
        ),
    )


def _configuration() -> ChangePointSaturationConfig:
    return ChangePointSaturationConfig(
        resource_windows=ResourceWindowConfig(
            advance_seconds=10,
            maximum_series_points=16,
            dedup_capacity=100,
            namespace="otel-demo",
            used_signal="container.memory.working_set",
            capacity_signal="k8s.container.memory_limit",
            unit="By",
            rules=(
                ResourceWindowRuleConfig(
                    service="frontend",
                    container="frontend",
                    detector_signal="container_memory",
                ),
            ),
        ),
        pelt_model="l2",
        pelt_penalty=0.01,
        minimum_series_points=12,
        minimum_segment_points=4,
        minimum_increasing_fraction=0.8,
        minimum_utilization_slope_per_second=0.0002,
        maximum_headroom_ratio=0.2,
        full_score_headroom_ratio=0.05,
    )


def _tick(index: int) -> datetime:
    return START + timedelta(seconds=index * 10)


def _used(
    observation_id: str,
    *,
    tick: int,
    value: float,
    pod: str = "pod-a",
) -> Observation:
    return _metric(
        observation_id,
        tick=tick,
        signal="container.memory.working_set",
        value=value,
        pod=pod,
    )


def _capacity(
    observation_id: str,
    *,
    tick: int,
    value: float = CAPACITY,
    pod: str = "pod-a",
) -> Observation:
    return _metric(
        observation_id,
        tick=tick,
        signal="k8s.container.memory_limit",
        value=value,
        pod=pod,
    )


def _metric(
    observation_id: str,
    *,
    tick: int,
    signal: str,
    value: float,
    pod: str,
) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=_tick(tick),
        service="astronomy",
        signal=signal,
        value=value,
        unit="By",
        attributes={
            "otel.metric.kind": "gauge",
            "k8s.namespace.name": "otel-demo",
            "k8s.container.name": "frontend",
            "k8s.pod.uid": pod,
        },
    )
