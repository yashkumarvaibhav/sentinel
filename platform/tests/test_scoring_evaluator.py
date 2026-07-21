"""The evaluator scores detector outputs after, and separately from, runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from lab.scenarios import SeedPurpose, compile_profile, load_profile
from lab.scoring.evaluator import build_rate_observations, score_observations
from lab.scoring.gates import ScoreGateConfig, evaluate_gates

from common.config import DetectorConfig
from tests.factories import behavioral_ratio_config, log_template_config

SCENARIO_ROOT = Path(__file__).resolve().parents[2] / "lab" / "scenarios"
START = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def test_expected_event_volume_is_not_labeled_but_offset_is() -> None:
    profile = load_profile(SCENARIO_ROOT / "match_night.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )
    counts = (4,) * 64 + (10,) * 20 + (16,) * 16
    timestamps = tuple(
        START + timedelta(seconds=index, milliseconds=100 + sample * 10)
        for index, count in enumerate(counts)
        for sample in range(count)
    )
    observations = build_rate_observations(
        profile=profile,
        run_id="test-match",
        span_timestamps=timestamps,
    )

    score = score_observations(
        profile=profile,
        artifacts=artifacts,
        observations=observations,
        detector=_detector(),
        expected_span_count=sum(counts),
        actual_span_count=len(timestamps),
    )

    assert score.telemetry_completeness == 1.0
    assert score.metrics.false_positive == 0
    assert score.metrics.false_negative == 0
    assert score.metrics.true_positive == 8
    assert score.metrics.precision.value == 1.0
    assert score.metrics.recall.value == 1.0


def test_quiet_profile_has_no_false_positive_on_realistic_constant_arrivals() -> None:
    profile = load_profile(SCENARIO_ROOT / "quiet_day.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )
    timestamps = tuple(
        START + timedelta(seconds=index, milliseconds=100 + sample * 200)
        for index in range(profile.duration_seconds)
        for sample in range(4)
    )
    observations = build_rate_observations(
        profile=profile,
        run_id="test-quiet",
        span_timestamps=timestamps,
    )

    score = score_observations(
        profile=profile,
        artifacts=artifacts,
        observations=observations,
        detector=_detector(),
        expected_span_count=len(timestamps),
        actual_span_count=len(timestamps),
    )

    assert score.metrics.false_positive == 0
    assert score.metrics.false_positive_rate.value == 0.0
    assert score.metrics.recall.status == "insufficient"


def test_score_gates_fail_closed_on_insufficient_or_incomplete_evidence() -> None:
    profile = load_profile(SCENARIO_ROOT / "quiet_day.yml")
    artifacts = compile_profile(
        profile,
        seed=profile.seeds.development[0],
        purpose=SeedPurpose.DEVELOPMENT,
    )
    timestamps = tuple(START + timedelta(seconds=index) for index in range(10))
    observations = build_rate_observations(
        profile=profile,
        run_id="incomplete",
        span_timestamps=timestamps,
    )
    score = score_observations(
        profile=profile,
        artifacts=artifacts,
        observations=observations,
        detector=_detector(),
        expected_span_count=220,
        actual_span_count=len(timestamps),
    )

    result = evaluate_gates(
        (score,),
        ScoreGateConfig(
            version=1,
            residual_precision_min=0.9,
            residual_recall_min=0.9,
            quiet_day_false_positive_rate_max=0.0,
            telemetry_completeness_min=0.95,
        ),
    )

    assert not result.passed
    assert {failure.metric for failure in result.failures} >= {
        "telemetry_completeness",
        "residual_precision",
        "residual_recall",
    }


def _detector() -> DetectorConfig:
    return DetectorConfig(
        version=1,
        feature_window_seconds=60,
        watermark_lateness_seconds=15,
        ewma_alpha=0.15,
        baseline_warmup_points=30,
        baseline_update_gate_ratio=0.25,
        expected_band_relative_tolerance=0.1,
        absolute_noise_floors={"frontend.request_rate": 1.0},
        behavioral_ratios=behavioral_ratio_config(),
        log_templates=log_template_config(),
    )
