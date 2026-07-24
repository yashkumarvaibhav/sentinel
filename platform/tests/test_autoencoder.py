"""LSTM auth-sequence autoencoder: determinism, separation, eval, persistence."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from ml.autoencoder import (
    AuthSequenceSample,
    AutoencoderBundleError,
    AutoencoderError,
    AutoencoderModel,
    autoencoder_holdout_metrics,
    evaluate_autoencoder,
    fit_autoencoder,
    generate_all_auth_sequences,
    load_autoencoder_model,
    save_autoencoder_model,
    sequences_to_array,
)
from ml.config import AutoencoderParamsConfig, load_autoencoder_params

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
_LABEL_NORMAL = 0
_LABEL_ATTACK = 1


@pytest.fixture(scope="module")
def params() -> AutoencoderParamsConfig:
    return load_autoencoder_params(CONFIG_ROOT / "ml-autoencoder.yml")


@pytest.fixture(scope="module")
def samples(params: AutoencoderParamsConfig) -> tuple[AuthSequenceSample, ...]:
    return generate_all_auth_sequences(params)


@pytest.fixture(scope="module")
def model(
    params: AutoencoderParamsConfig, samples: Sequence[AuthSequenceSample]
) -> AutoencoderModel:
    normal = [sample for sample in samples if sample.label == _LABEL_NORMAL]
    return fit_autoencoder(sequences_to_array(normal), params=params)


def test_samples_are_simulated_and_labeled(
    params: AutoencoderParamsConfig, samples: Sequence[AuthSequenceSample]
) -> None:
    expected = len(params.simulation.seeds) * (
        params.simulation.normal_sequences + params.simulation.attack_sequences
    )
    assert len(samples) == expected
    assert all(sample.honesty == "SIMULATED" for sample in samples)
    assert all(len(sample.steps) == params.seq_len for sample in samples)
    assert all(len(step) == len(params.features) for sample in samples for step in sample.steps)


def test_generation_is_deterministic(params: AutoencoderParamsConfig) -> None:
    first = generate_all_auth_sequences(params)
    second = generate_all_auth_sequences(params)
    assert [s.steps for s in first] == [s.steps for s in second]


def test_fit_is_deterministic_bit_exact(
    model: AutoencoderModel,
    params: AutoencoderParamsConfig,
    samples: Sequence[AuthSequenceSample],
) -> None:
    normal = [sample for sample in samples if sample.label == _LABEL_NORMAL]
    other = fit_autoencoder(sequences_to_array(normal), params=params)
    attack = [sample for sample in samples if sample.label == _LABEL_ATTACK]
    array = sequences_to_array(attack)
    assert (other.score_many(array) == model.score_many(array)).all()


def test_attack_reconstructs_worse_than_normal(
    model: AutoencoderModel, samples: Sequence[AuthSequenceSample]
) -> None:
    normal = [sample for sample in samples if sample.label == _LABEL_NORMAL]
    attack = [sample for sample in samples if sample.label == _LABEL_ATTACK]
    normal_mean = model.score_many(sequences_to_array(normal)).mean()
    attack_mean = model.score_many(sequences_to_array(attack)).mean()
    assert attack_mean > normal_mean


def test_holdout_separates_attacks(params: AutoencoderParamsConfig) -> None:
    metric = autoencoder_holdout_metrics(params, holdout_fraction=0.2)
    assert metric.roc_auc > 0.95
    assert metric.separation > 0.0
    assert metric.attack_mean_error > metric.normal_mean_error


def test_fit_rejects_wrong_shape(params: AutoencoderParamsConfig) -> None:
    bad = sequences_to_array(generate_all_auth_sequences(params))[:, :-1, :]  # short seq_len
    with pytest.raises(AutoencoderError, match="seq_len"):
        fit_autoencoder(bad, params=params)


def test_evaluate_needs_both_classes(
    model: AutoencoderModel, samples: Sequence[AuthSequenceSample]
) -> None:
    normal_only = [sample for sample in samples if sample.label == _LABEL_NORMAL]
    with pytest.raises(AutoencoderError, match="both normal and attack"):
        evaluate_autoencoder(model, normal_only)


def test_save_load_round_trip(
    model: AutoencoderModel, samples: Sequence[AuthSequenceSample], tmp_path: Path
) -> None:
    save_autoencoder_model(tmp_path, model)
    loaded = load_autoencoder_model(tmp_path)
    assert loaded.params_fingerprint == model.params_fingerprint
    assert loaded.feature_names == model.feature_names
    attack = next(sample for sample in samples if sample.label == _LABEL_ATTACK)
    assert loaded.score_one(attack) == pytest.approx(model.score_one(attack), abs=1e-6)


def test_load_missing_bundle_raises(tmp_path: Path) -> None:
    with pytest.raises(AutoencoderBundleError, match="manifest"):
        load_autoencoder_model(tmp_path)
