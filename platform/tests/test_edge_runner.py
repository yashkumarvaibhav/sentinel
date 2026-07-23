"""Real trace observations drive edge detectors and episode ticks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lab.captures import load_runtime_capture
from lab.captures.edge_transcript import replay_edge_detection

from common.config import (
    EdgeDegradationConfig,
    EdgeDegradationRuleConfig,
    EpisodeConfig,
    EpisodePolicyConfig,
    load_config,
)
from contracts import Observation, SymptomKind
from detection.episodes import EpisodeAction, EpisodeKey
from detection.runner import EdgeDetectionRunner, EdgeWindowAdvance, EdgeWindowStatus

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_edge_runner_opens_and_closes_only_from_sufficient_measured_windows() -> None:
    runner = _runner()

    warmup = runner.advance(
        observations=tuple(_span(f"baseline-{index}", 0.1 + index * 0.1) for index in range(3)),
        tick_ts=START + timedelta(seconds=1),
    )
    assert warmup[0].status is EdgeWindowStatus.INSUFFICIENT
    assert warmup[0].baseline_sample_count == 3
    assert warmup[0].window_sample_count == 0
    assert warmup[0].transition is None

    actions: list[EpisodeAction] = []
    statuses: list[EdgeWindowStatus] = []
    for tick in range(2, 11):
        failed = tick <= 4
        result = runner.advance(
            observations=tuple(
                _span(f"tick-{tick}-{index}", tick - 0.9 + index * 0.1, failed=failed)
                for index in range(3)
            ),
            tick_ts=START + timedelta(seconds=tick),
        )[0]
        statuses.append(result.status)
        if result.transition is not None:
            actions.append(result.transition.action)

    assert statuses[:3] == [EdgeWindowStatus.BREACH] * 3
    assert EpisodeAction.OPENED in actions
    assert actions[-3:] == [
        EpisodeAction.CLEARING,
        EpisodeAction.CLEARING,
        EpisodeAction.CLOSED,
    ]
    assert runner.active_episodes() == ()


def test_rule_baseline_warmup_override_takes_precedence_over_global_default() -> None:
    runner = _runner(rule_baseline_warmup_samples=4)

    warming = runner.advance(
        observations=tuple(_span(f"baseline-{index}", 0.1 + index * 0.1) for index in range(3)),
        tick_ts=START + timedelta(seconds=1),
    )[0]
    ready = runner.advance(
        observations=(_span("baseline-3", 1.1),),
        tick_ts=START + timedelta(seconds=2),
    )[0]

    assert warming.status is EdgeWindowStatus.WARMING
    assert warming.baseline_sample_count == 3
    assert ready.status is EdgeWindowStatus.INSUFFICIENT
    assert ready.baseline_sample_count == 4


def test_ambiguous_status_is_insufficient_and_never_clears_an_episode() -> None:
    runner = _runner()
    runner.advance(
        observations=tuple(_span(f"baseline-{index}", 0.1 + index * 0.1) for index in range(3)),
        tick_ts=START + timedelta(seconds=1),
    )
    ambiguous = _span("ambiguous", 1.1, failed=False).model_copy(
        update={
            "attributes": {
                "span.kind": 3,
                "rpc.service": "oteldemo.PaymentService",
                "rpc.grpc.status_code": "0",
                "span.status_code": 2,
            }
        }
    )

    result = runner.advance(
        observations=(ambiguous, _span("valid-1", 1.2), _span("valid-2", 1.3)),
        tick_ts=START + timedelta(seconds=2),
    )[0]

    assert result.status is EdgeWindowStatus.INSUFFICIENT
    assert result.ambiguous_observation_ids == ("ambiguous",)
    assert result.transition is None


def test_non_client_rpc_span_is_not_dependency_edge_evidence() -> None:
    runner = _runner()
    server_span = _span("server", 0.1).model_copy(
        update={"attributes": {"span.kind": 2, "rpc.service": "oteldemo.PaymentService"}}
    )

    result = runner.advance(
        observations=(server_span,),
        tick_ts=START + timedelta(seconds=1),
    )[0]

    assert result.status is EdgeWindowStatus.WARMING
    assert result.baseline_sample_count == 0
    assert result.ambiguous_observation_ids == ()


def test_runner_monitored_keys_and_replay_are_stable() -> None:
    batches = [
        (
            tuple(_span(f"baseline-{index}", 0.1 + index * 0.1) for index in range(3)),
            START + timedelta(seconds=1),
        ),
        (
            tuple(_span(f"fault-{index}", 1.1 + index * 0.1, failed=True) for index in range(3)),
            START + timedelta(seconds=2),
        ),
    ]

    first = _runner()
    second = _runner()
    assert first.monitored_keys == (
        EpisodeKey(SymptomKind.EDGE_DEGRADED, "checkout", "dependency.payment"),
    )
    first_results = tuple(
        first.advance(observations=batch, tick_ts=tick) for batch, tick in batches
    )
    second_results = tuple(
        second.advance(observations=tuple(reversed(batch)), tick_ts=tick) for batch, tick in batches
    )

    assert first_results == second_results


def test_exact_redelivery_ignores_attribute_insertion_order() -> None:
    runner = _runner()
    original = _span("stable", 0.1)
    reordered = original.model_copy(
        update={"attributes": dict(reversed(tuple(original.attributes.items())))}
    )
    tick = START + timedelta(seconds=1)

    first = runner.advance(observations=(original,), tick_ts=tick)

    assert runner.advance(observations=(reordered,), tick_ts=tick) == first


def test_real_v5_capture_proves_breach_but_recovery_is_explicitly_insufficient() -> None:
    capture_root = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v5"
    if not capture_root.is_dir():
        pytest.skip("local real cascade development capture is not present")
    config = load_config(REPO_ROOT / "config")

    first = replay_edge_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )
    second = replay_edge_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    target = EpisodeKey(SymptomKind.EDGE_DEGRADED, "checkout", "dependency.payment")
    steps = tuple(result for step in first.steps for result in step.results if result.key == target)
    assert any(result.status is EdgeWindowStatus.BREACH for result in steps)
    assert any(
        result.status is EdgeWindowStatus.INSUFFICIENT
        and result.tick_ts > first.anchor_ts + timedelta(seconds=124)
        for result in steps
    )
    assert any(episode.kind is SymptomKind.EDGE_DEGRADED for episode in first.active_episodes)


def test_real_v6_capture_opens_and_closes_from_sufficient_measured_windows() -> None:
    capture_root = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v6"
    if not capture_root.is_dir():
        pytest.skip("local real cascade recovery capture is not present")
    config = load_config(REPO_ROOT / "config")

    first = replay_edge_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )
    second = replay_edge_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    target = EpisodeKey(SymptomKind.EDGE_DEGRADED, "checkout", "dependency.payment")
    steps = tuple(result for step in first.steps for result in step.results if result.key == target)
    assert [
        result.transition.action
        for result in steps
        if result.transition is not None
        and result.transition.action in {EpisodeAction.OPENED, EpisodeAction.CLOSED}
    ] == [EpisodeAction.OPENED, EpisodeAction.CLOSED]
    assert [
        result.status
        for result in steps
        if result.transition is not None
        and result.transition.action in {EpisodeAction.CLEARING, EpisodeAction.CLOSED}
    ] == [EdgeWindowStatus.CLEAR] * 3
    assert first.active_episodes == ()


# Baseline-stability and detectability oracles for the direct checkout->payment edge.
#
# These fix the calibration target the combo_night payment fault must satisfy:
# a representative context-blind baseline must absorb normal latency (no spurious
# episode), while a fault carried by an isolated 2 rps journey must open one. They
# mirror the committed checkout->payment rule (12 s window, 2 s advance, 20-sample
# window) and vary only ``baseline_warmup_samples`` -- the knob under study -- so
# the oracle cannot be satisfied by an unrepresentative short baseline. The
# global default remains short because raising it globally would starve the
# cascade_night early/sparse fault of a clean baseline. Representative warmup is
# therefore an explicit per-edge override, guarded by these oracles.


def test_representative_edge_baseline_absorbs_normal_latency() -> None:
    runner = EdgeDetectionRunner(
        configuration=_payment_edge_config(baseline_warmup_samples=40),
        episodes=_edge_episode_config(),
    )

    results = _drive_payment_edge(runner, _fast_then_steady_specs(ticks=20, per_tick=4))

    assert any(result.status is EdgeWindowStatus.CLEAR for result in results)
    assert all(result.status is not EdgeWindowStatus.BREACH for result in results)
    assert runner.active_episodes() == ()


def test_short_edge_baseline_opens_spurious_episode_on_normal_latency() -> None:
    runner = EdgeDetectionRunner(
        configuration=_payment_edge_config(baseline_warmup_samples=3),
        episodes=_edge_episode_config(),
    )

    results = _drive_payment_edge(runner, _fast_then_steady_specs(ticks=20, per_tick=4))

    assert any(result.status is EdgeWindowStatus.BREACH for result in results)
    assert any(
        result.transition is not None and result.transition.action is EpisodeAction.OPENED
        for result in results
    )
    assert runner.active_episodes() != ()


def test_two_rps_failing_payment_edge_opens_episode() -> None:
    runner = EdgeDetectionRunner(
        configuration=_payment_edge_config(baseline_warmup_samples=40),
        episodes=_edge_episode_config(),
    )
    warmup = [[(50.0, False)] * 4 for _ in range(18)]  # 2 rps -> 4 calls / 2 s tick
    fault = [[(50.0, True)] * 4 for _ in range(8)]

    results = _drive_payment_edge(runner, warmup + fault)

    assert any(result.status is EdgeWindowStatus.CLEAR for result in results[:18])
    assert any(
        result.transition is not None and result.transition.action is EpisodeAction.OPENED
        for result in results
    )
    assert [
        (episode.kind, episode.service, episode.signal) for episode in runner.active_episodes()
    ] == [(SymptomKind.EDGE_DEGRADED, "checkout", "dependency.payment")]


def test_one_rps_failing_payment_edge_is_never_evaluable() -> None:
    runner = EdgeDetectionRunner(
        configuration=_payment_edge_config(baseline_warmup_samples=40),
        episodes=_edge_episode_config(),
    )
    warmup = [[(50.0, False)] * 2 for _ in range(30)]  # 1 rps -> 2 calls / 2 s tick
    fault = [[(50.0, True)] * 2 for _ in range(20)]

    results = _drive_payment_edge(runner, warmup + fault)

    evaluated = [result for result in results if result.status is not EdgeWindowStatus.WARMING]
    assert evaluated  # the baseline did freeze
    assert all(result.status is EdgeWindowStatus.INSUFFICIENT for result in evaluated)
    assert runner.active_episodes() == ()


def _payment_edge_config(*, baseline_warmup_samples: int) -> EdgeDegradationConfig:
    return EdgeDegradationConfig(
        window_seconds=12,
        advance_seconds=2,
        baseline_warmup_samples=baseline_warmup_samples,
        dedup_capacity=10000,
        rules=(
            EdgeDegradationRuleConfig(
                caller="checkout",
                downstream="payment",
                rpc_service="oteldemo.PaymentService",
                minimum_samples=20,
                latency_baseline_floor_ms=5.0,
                error_rate_baseline_floor=0.01,
                trigger_relative_latency_rise=0.5,
                full_score_relative_latency_rise=2.0,
                trigger_relative_error_rise=1.0,
                full_score_relative_error_rise=5.0,
            ),
        ),
    )


def _edge_episode_config() -> EpisodeConfig:
    return EpisodeConfig(
        policies={
            SymptomKind.EDGE_DEGRADED.value: EpisodePolicyConfig(
                open_after_ticks=3,
                close_after_ticks=3,
                breach_score=0.5,
                clear_score=0.2,
            )
        }
    )


def _fast_then_steady_specs(*, ticks: int, per_tick: int) -> list[list[tuple[float, bool]]]:
    """Cold-start-fast then steady successful latency, as one edge sees under load."""
    specs: list[list[tuple[float, bool]]] = []
    index = 0
    for _ in range(ticks):
        tick: list[tuple[float, bool]] = []
        for _ in range(per_tick):
            tick.append((4.0 if index < 3 else 50.0, False))
            index += 1
        specs.append(tick)
    return specs


def _drive_payment_edge(
    runner: EdgeDetectionRunner,
    tick_specs: list[list[tuple[float, bool]]],
) -> list[EdgeWindowAdvance]:
    """Advance the single payment edge tick-by-tick; each spec is one tick's calls."""
    advance = runner.advance_seconds
    results: list[EdgeWindowAdvance] = []
    for tick_index, calls in enumerate(tick_specs, start=1):
        tick_ts = START + timedelta(seconds=tick_index * advance)
        count = len(calls)
        observations = tuple(
            _payment_call(
                f"call-{tick_index}-{call_index}",
                START
                + timedelta(seconds=(tick_index - 1) * advance)
                + timedelta(seconds=advance * (call_index + 1) / (count + 1)),
                latency_ms=latency_ms,
                failed=failed,
            )
            for call_index, (latency_ms, failed) in enumerate(calls)
        )
        results.append(runner.advance(observations=observations, tick_ts=tick_ts)[0])
    return results


def _payment_call(
    observation_id: str,
    ts: datetime,
    *,
    latency_ms: float,
    failed: bool,
) -> Observation:
    attributes: dict[str, str | bool | int | float] = {
        "span.kind": 3,
        "rpc.service": "oteldemo.PaymentService",
        "rpc.grpc.status_code": "2" if failed else "0",
    }
    if failed:
        attributes["span.status_code"] = 2
    return Observation(
        observation_id=observation_id,
        ts=ts,
        service="checkout",
        signal="span.duration_ms",
        value=latency_ms,
        unit="ms",
        attributes=attributes,
        trace_refs=(f"trace-{observation_id}",),
    )


def _runner(*, rule_baseline_warmup_samples: int | None = None) -> EdgeDetectionRunner:
    edge = EdgeDegradationConfig(
        window_seconds=4,
        advance_seconds=1,
        baseline_warmup_samples=3,
        dedup_capacity=100,
        rules=(
            EdgeDegradationRuleConfig(
                caller="checkout",
                downstream="payment",
                rpc_service="oteldemo.PaymentService",
                baseline_warmup_samples=rule_baseline_warmup_samples,
                minimum_samples=3,
                latency_baseline_floor_ms=1.0,
                error_rate_baseline_floor=0.01,
                trigger_relative_latency_rise=0.5,
                full_score_relative_latency_rise=2.0,
                trigger_relative_error_rise=1.0,
                full_score_relative_error_rise=5.0,
            ),
        ),
    )
    episodes = EpisodeConfig(
        policies={
            SymptomKind.EDGE_DEGRADED.value: EpisodePolicyConfig(
                open_after_ticks=3,
                close_after_ticks=3,
                breach_score=0.5,
                clear_score=0.2,
            )
        }
    )
    return EdgeDetectionRunner(configuration=edge, episodes=episodes)


def _span(observation_id: str, offset: float, *, failed: bool = False) -> Observation:
    attributes: dict[str, str | bool | int | float] = {
        "span.kind": 3,
        "rpc.service": "oteldemo.PaymentService",
        "rpc.grpc.status_code": "2" if failed else "0",
    }
    if failed:
        attributes["span.status_code"] = 2
    return Observation(
        observation_id=observation_id,
        ts=START + timedelta(seconds=offset),
        service="checkout",
        signal="span.duration_ms",
        value=5.0,
        unit="ms",
        attributes=attributes,
        trace_refs=(f"trace-{observation_id}",),
    )
