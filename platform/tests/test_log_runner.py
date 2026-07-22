"""Normalized log observations drive deterministic windows and episodes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lab.captures import load_runtime_capture
from lab.captures.log_transcript import replay_log_detection

from common.config import EpisodeConfig, EpisodePolicyConfig, LogTemplateConfig
from contracts import Observation, SymptomKind
from detection.episodes import EpisodeAction, EpisodeKey
from detection.log_runner import LogDetectionRunner, LogWindowStatus

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_log_runner_opens_and_closes_only_from_sufficient_measured_windows() -> None:
    runner = _runner()

    warmup = runner.advance(
        observations=_logs("baseline", start_offset=0, count=6),
        tick_ts=START + timedelta(seconds=60),
    )[0]
    assert warmup.status is LogWindowStatus.WARMING
    assert warmup.baseline_window_count == 1
    assert warmup.window_record_count == 6
    assert warmup.transition is None

    actions: list[EpisodeAction] = []
    for window in range(1, 4):
        result = runner.advance(
            observations=_logs(
                f"storm-{window}",
                start_offset=window * 60,
                count=60,
            ),
            tick_ts=START + timedelta(seconds=(window + 1) * 60),
        )[0]
        assert result.status is LogWindowStatus.BREACH
        assert len(result.symptoms) == 1
        assert result.selected_symptom == result.symptoms[0]
        assert result.transition is not None
        actions.append(result.transition.action)

    assert actions == [
        EpisodeAction.PENDING,
        EpisodeAction.PENDING,
        EpisodeAction.OPENED,
    ]

    ambiguous = (
        *_logs("ambiguous-valid", start_offset=240, count=5),
        _log("ambiguous", offset=245, body=7),
    )
    insufficient = runner.advance(
        observations=ambiguous,
        tick_ts=START + timedelta(seconds=300),
    )[0]
    assert insufficient.status is LogWindowStatus.INSUFFICIENT
    assert insufficient.ambiguous_observation_ids == ("ambiguous",)
    assert insufficient.transition is None
    assert len(runner.active_episodes()) == 1

    clears: list[EpisodeAction] = []
    for window in range(5, 8):
        result = runner.advance(
            observations=_logs(
                f"quiet-{window}",
                start_offset=window * 60,
                count=6,
            ),
            tick_ts=START + timedelta(seconds=(window + 1) * 60),
        )[0]
        assert result.status is LogWindowStatus.CLEAR
        assert result.transition is not None
        clears.append(result.transition.action)

    assert clears == [
        EpisodeAction.CLEARING,
        EpisodeAction.CLEARING,
        EpisodeAction.CLOSED,
    ]
    assert runner.active_episodes() == ()


def test_sparse_log_window_is_insufficient_and_cannot_tick_an_episode() -> None:
    runner = _runner()
    runner.advance(
        observations=_logs("baseline", start_offset=0, count=6),
        tick_ts=START + timedelta(seconds=60),
    )

    result = runner.advance(
        observations=_logs("sparse", start_offset=60, count=4),
        tick_ts=START + timedelta(seconds=120),
    )[0]

    assert result.status is LogWindowStatus.INSUFFICIENT
    assert result.window_record_count == 4
    assert result.transition is None


def test_log_runner_is_service_scoped_and_replay_order_stable() -> None:
    first = _two_service_runner()
    second = _two_service_runner()
    baseline = _logs("checkout-base", start_offset=0, count=6) + _logs(
        "payment-base",
        start_offset=0,
        count=6,
        source_service="payment",
    )
    current = _logs("checkout-storm", start_offset=60, count=60)

    assert first.monitored_keys == (
        EpisodeKey(SymptomKind.LOG_BURST, "checkout", "log_template_rate"),
        EpisodeKey(SymptomKind.LOG_BURST, "payment", "log_template_rate"),
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
    assert left[0].status is LogWindowStatus.BREACH
    assert left[1].status is LogWindowStatus.INSUFFICIENT


def test_highest_scoring_template_is_selected_deterministically() -> None:
    runner = _runner()
    runner.advance(
        observations=_logs("baseline", start_offset=0, count=6),
        tick_ts=START + timedelta(seconds=60),
    )
    current = (
        *(
            _log(
                f"database-{index}",
                offset=60.1 + index * 0.5,
                body=f"database timeout on shard {index} after {index + 10} ms",
            )
            for index in range(5)
        ),
        *(
            _log(
                f"kernel-{index}",
                offset=70.1 + index * 0.5,
                body=f"kernel panic in worker {index} with code {index + 100}",
            )
            for index in range(10)
        ),
    )

    result = runner.advance(
        observations=tuple(reversed(current)),
        tick_ts=START + timedelta(seconds=120),
    )[0]

    assert len(result.symptoms) == 2
    assert result.selected_symptom is not None
    assert result.selected_symptom.score == max(item.score for item in result.symptoms)
    assert result.selected_symptom.evidence_refs == tuple(
        f"log-kernel-{index}" for index in range(10)
    )


def test_exact_redelivery_ignores_log_attribute_insertion_order() -> None:
    runner = _runner()
    original = _logs("baseline", start_offset=0, count=6)
    reordered = tuple(
        item.model_copy(update={"attributes": dict(reversed(tuple(item.attributes.items())))})
        for item in original
    )
    tick = START + timedelta(seconds=60)

    first = runner.advance(observations=original, tick_ts=tick)

    assert runner.advance(observations=reordered, tick_ts=tick) == first


def test_log_runner_rejects_conflicts_and_incomplete_window_ticks() -> None:
    runner = _runner()
    baseline = _logs("baseline", start_offset=0, count=6)
    tick = START + timedelta(seconds=60)
    runner.advance(observations=baseline, tick_ts=tick)

    with pytest.raises(ValueError, match="conflicting log runner advance"):
        runner.advance(observations=(), tick_ts=tick)

    changed = baseline[0].model_copy(update={"value": 2.0})
    with pytest.raises(ValueError, match="reused with different log evidence"):
        runner.advance(
            observations=(changed,),
            tick_ts=START + timedelta(seconds=120),
        )

    with pytest.raises(ValueError, match="complete configured windows"):
        runner.advance(
            observations=(),
            tick_ts=START + timedelta(seconds=180),
        )


def test_real_v6_capture_materializes_service_local_log_windows_stably() -> None:
    capture_root = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v6"
    if not capture_root.is_dir():
        pytest.skip("local real cascade recovery capture is not present")
    from common.config import load_config

    config = load_config(REPO_ROOT / "config")
    first = replay_log_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )
    second = replay_log_detection(
        load_runtime_capture(capture_root),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert len(first.steps) == 2
    assert [result.window_record_count for result in first.steps[0].results] == [
        25,
        18,
        308,
        6,
    ]
    assert [result.window_record_count for result in first.steps[1].results] == [
        148,
        41,
        767,
        82,
    ]
    assert [result.status for result in first.steps[0].results] == [LogWindowStatus.WARMING] * 4
    assert [result.status for result in first.steps[1].results] == [
        LogWindowStatus.BREACH,
        LogWindowStatus.BREACH,
        LogWindowStatus.CLEAR,
        LogWindowStatus.BREACH,
    ]
    payment = first.steps[1].results[-1]
    assert payment.selected_symptom is not None
    assert "Payment request failed" in payment.selected_symptom.note
    assert first.active_episodes == ()


def _runner() -> LogDetectionRunner:
    return LogDetectionRunner(
        configuration=_config({"checkout": "checkout"}),
        episodes=_episodes(),
    )


def _two_service_runner() -> LogDetectionRunner:
    return LogDetectionRunner(
        configuration=_config({"checkout": "checkout", "payment": "payment"}),
        episodes=_episodes(),
    )


def _episodes() -> EpisodeConfig:
    return EpisodeConfig(
        policies={
            SymptomKind.LOG_BURST.value: EpisodePolicyConfig(
                open_after_ticks=3,
                close_after_ticks=3,
                breach_score=0.5,
                clear_score=0.2,
            )
        }
    )


def _config(service_mappings: dict[str, str]) -> LogTemplateConfig:
    return LogTemplateConfig(
        window_seconds=60,
        baseline_warmup_windows=1,
        minimum_window_records=5,
        dedup_capacity=1_000,
        service_mappings=service_mappings,
        similarity_threshold=0.4,
        max_depth=4,
        max_children=100,
        max_clusters=10_000,
        parameterize_numeric_tokens=True,
        minimum_template_count=5,
        baseline_rate_floor=0.01,
        trigger_relative_deformation=2.0,
        full_score_relative_deformation=10.0,
    )


def _logs(
    prefix: str,
    *,
    start_offset: int,
    count: int,
    source_service: str = "checkout",
) -> tuple[Observation, ...]:
    return tuple(
        _log(
            f"{prefix}-{index:03d}",
            offset=start_offset + 0.1 + index * 0.5,
            body=(
                f"failed login for user user-{index} from 10.0.0.{index + 1} port {51000 + index}"
            ),
            source_service=source_service,
        )
        for index in range(count)
    )


def _log(
    observation_id: str,
    *,
    offset: float,
    body: str | int,
    source_service: str = "checkout",
) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=START + timedelta(seconds=offset),
        service=source_service,
        signal="log.record",
        value=1.0,
        unit="record",
        attributes={"log.body": body, "log.severity": "ERROR"},
        log_refs=(f"log-{observation_id}",),
    )
