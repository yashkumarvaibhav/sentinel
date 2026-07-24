"""The seeded synthetic history generator: determinism and structure."""

from __future__ import annotations

from pathlib import Path

import pytest

from ml.config import MlTrainingConfig, load_training_config
from ml.features import AWARE_FEATURES
from ml.frames import canonical_frames_bytes, frames_data_hash
from ml.synthetic import generate_all_synthetic_history, generate_synthetic_history

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "ml-training.yml"


@pytest.fixture(scope="module")
def config() -> MlTrainingConfig:
    return load_training_config(CONFIG_PATH)


def test_generation_is_deterministic_for_a_seed(config: MlTrainingConfig) -> None:
    first = generate_synthetic_history(config, seed=40100)
    second = generate_synthetic_history(config, seed=40100)
    assert frames_data_hash(first) == frames_data_hash(second)
    assert canonical_frames_bytes(first) == canonical_frames_bytes(second)


def test_different_seeds_diverge(config: MlTrainingConfig) -> None:
    first = generate_synthetic_history(config, seed=40100)
    other = generate_synthetic_history(config, seed=40101)
    assert frames_data_hash(first) != frames_data_hash(other)


def test_row_count_and_labels(config: MlTrainingConfig) -> None:
    frames = generate_synthetic_history(config, seed=40100)
    ticks_per_day = 86_400 // config.history.tick_seconds
    expected = ticks_per_day * config.history.horizon_days * len(config.signals)
    assert len(frames) == expected
    for frame in frames:
        assert frame.source == "synthetic"
        assert frame.honesty == "SIMULATED"
        assert frame.seed_purpose == "synthetic"
        assert frame.scenario_id == "synthetic_history"
        assert frame.value >= 0.0
        assert set(frame.features) == set(AWARE_FEATURES)


def test_events_lift_some_ticks_and_leave_others_flat(config: MlTrainingConfig) -> None:
    frames = generate_synthetic_history(config, seed=40100)
    lifts = [frame.features["event_lift"] for frame in frames]
    # weekend match alone: 1.5*0.9 = 1.35; midweek promo alone: 0.8*0.75 = 0.6.
    assert any(lift == 0.0 for lift in lifts)
    assert any(lift == pytest.approx(1.35) for lift in lifts)
    assert any(lift == pytest.approx(0.6) for lift in lifts)
    assert any(lift > 0.0 for lift in lifts)  # some ticks fall inside an event window


def test_all_seeds_are_concatenated(config: MlTrainingConfig) -> None:
    combined = generate_all_synthetic_history(config)
    per_seed = len(generate_synthetic_history(config, seed=40100))
    assert len(combined) == per_seed * len(config.history.seeds)
