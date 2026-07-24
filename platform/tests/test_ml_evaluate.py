"""Envelope evaluation metrics: calibration, sharpness, temporal split."""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config import load_config
from ml.config import EnvelopeParamsConfig, load_envelope_params, load_training_config
from ml.data import TrainingDataset, build_training_dataset
from ml.envelopes import EnvelopeModel, train_envelopes
from ml.evaluate import (
    evaluate_signal_envelope,
    holdout_metrics,
    pinball_loss,
    time_split,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
SIGNAL = "frontend.request_rate"


@pytest.fixture(scope="module")
def params() -> EnvelopeParamsConfig:
    return load_envelope_params(CONFIG_ROOT / "ml-envelopes.yml")


@pytest.fixture(scope="module")
def dataset() -> TrainingDataset:
    training = load_training_config(CONFIG_ROOT / "ml-training.yml")
    config = load_config(CONFIG_ROOT)
    return build_training_dataset(repo_root=REPO_ROOT, training_config=training, config=config)


@pytest.fixture(scope="module")
def model(dataset: TrainingDataset, params: EnvelopeParamsConfig) -> EnvelopeModel:
    return train_envelopes(dataset.frames, params=params)


def test_pinball_loss_is_asymmetric() -> None:
    # under-prediction of a high quantile is penalised heavily; over-prediction lightly
    assert pinball_loss(10.0, 8.0, alpha=0.9) == pytest.approx(1.8)
    assert pinball_loss(6.0, 8.0, alpha=0.9) == pytest.approx(0.2)
    assert pinball_loss(8.0, 8.0, alpha=0.5) == 0.0


def test_in_sample_calibration(model: EnvelopeModel, dataset: TrainingDataset) -> None:
    metrics = evaluate_signal_envelope(model.signals[SIGNAL], dataset.frames)
    assert metrics.eval_rows == 12096
    for quantile in metrics.per_quantile:
        assert quantile.coverage == pytest.approx(quantile.quantile, abs=0.05)
        assert quantile.pinball_loss >= 0.0
    assert 0.75 <= metrics.interval_coverage <= 0.85


def test_holdout_generalisation(dataset: TrainingDataset, params: EnvelopeParamsConfig) -> None:
    (metrics,) = holdout_metrics(dataset.frames, params=params, holdout_fraction=0.2)
    assert metrics.signal_key == SIGNAL
    assert metrics.eval_rows > 0
    for quantile in metrics.per_quantile:
        assert quantile.coverage == pytest.approx(quantile.quantile, abs=0.07)
        assert quantile.pinball_loss > 0.0
    assert 0.68 <= metrics.interval_coverage <= 0.86


def test_time_split_is_deterministic_and_temporal(dataset: TrainingDataset) -> None:
    train_a, holdout_a = time_split(dataset.frames, holdout_fraction=0.2)
    train_b, holdout_b = time_split(dataset.frames, holdout_fraction=0.2)
    assert [f.ts for f in train_a] == [f.ts for f in train_b]
    assert len(train_a) + len(holdout_a) == len(dataset.frames)
    assert len(holdout_b) == pytest.approx(len(dataset.frames) * 0.2, rel=0.01)
    # the holdout is temporally at or after the train set
    assert train_a[-1].ts <= holdout_a[0].ts


def test_time_split_rejects_degenerate_fractions(dataset: TrainingDataset) -> None:
    for fraction in (0.0, 1.0, 1.5):
        with pytest.raises(ValueError, match="holdout_fraction"):
            time_split(dataset.frames, holdout_fraction=fraction)


def test_evaluate_requires_matching_signal(model: EnvelopeModel, dataset: TrainingDataset) -> None:
    other = [f for f in dataset.frames if f.signal_key != SIGNAL]
    with pytest.raises(ValueError, match="no evaluation frames"):
        evaluate_signal_envelope(model.signals[SIGNAL], other)
