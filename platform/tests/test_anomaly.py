"""Multivariate isolation-forest anomaly: determinism, separation, eval, persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from ml.anomaly import (
    AnomalyBundleError,
    AnomalyFitError,
    AnomalyModel,
    anomaly_holdout_metrics,
    evaluate_anomaly,
    fit_anomaly_model,
    generate_all_behavioral_samples,
    load_anomaly_model,
    save_anomaly_model,
)
from ml.config import AnomalyParamsConfig, load_anomaly_params

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
_LABEL_NORMAL = 0
_LABEL_ATTACK = 1


@pytest.fixture(scope="module")
def params() -> AnomalyParamsConfig:
    return load_anomaly_params(CONFIG_ROOT / "ml-anomaly.yml")


@pytest.fixture(scope="module")
def model(params: AnomalyParamsConfig) -> AnomalyModel:
    normal = [
        sample.features
        for sample in generate_all_behavioral_samples(params)
        if sample.label == _LABEL_NORMAL
    ]
    return fit_anomaly_model(normal, params=params)


def _regime_vector(params: AnomalyParamsConfig, *, attack: bool) -> dict[str, float]:
    regimes = {regime.feature: regime for regime in params.simulation.regimes}
    column = 0  # the mean of each (mean, sd) regime
    return {
        name: (regimes[name].attack[column] if attack else regimes[name].normal[column])
        for name in params.features
    }


def test_samples_are_simulated_and_labeled(params: AnomalyParamsConfig) -> None:
    samples = generate_all_behavioral_samples(params)
    expected = len(params.simulation.seeds) * (
        params.simulation.normal_samples + params.simulation.attack_samples
    )
    assert len(samples) == expected
    assert all(sample.honesty == "SIMULATED" for sample in samples)
    assert {sample.label for sample in samples} == {_LABEL_NORMAL, _LABEL_ATTACK}
    assert all(set(sample.features) == set(params.features) for sample in samples)


def test_generation_is_deterministic(params: AnomalyParamsConfig) -> None:
    first = generate_all_behavioral_samples(params)
    second = generate_all_behavioral_samples(params)
    assert [dict(s.features) for s in first] == [dict(s.features) for s in second]


def test_fit_is_deterministic(model: AnomalyModel, params: AnomalyParamsConfig) -> None:
    normal = [
        sample.features
        for sample in generate_all_behavioral_samples(params)
        if sample.label == _LABEL_NORMAL
    ]
    other = fit_anomaly_model(normal, params=params)
    attack = _regime_vector(params, attack=True)
    assert other.score_one(attack) == model.score_one(attack)


def test_attack_scores_higher_than_normal(model: AnomalyModel, params: AnomalyParamsConfig) -> None:
    normal = _regime_vector(params, attack=False)
    attack = _regime_vector(params, attack=True)
    assert model.score_one(attack) > model.score_one(normal)


def test_holdout_separates_attacks(params: AnomalyParamsConfig) -> None:
    metric = anomaly_holdout_metrics(params, holdout_fraction=0.2)
    assert metric.roc_auc > 0.95
    assert metric.separation > 0.0
    assert metric.attack_mean_score > metric.normal_mean_score


def test_evaluate_needs_both_classes(model: AnomalyModel, params: AnomalyParamsConfig) -> None:
    normal_only = [
        sample
        for sample in generate_all_behavioral_samples(params)
        if sample.label == _LABEL_NORMAL
    ]
    with pytest.raises(AnomalyFitError, match="both normal and attack"):
        evaluate_anomaly(model, normal_only)


def test_fit_refuses_too_few_vectors(params: AnomalyParamsConfig) -> None:
    with pytest.raises(AnomalyFitError, match="max_samples"):
        fit_anomaly_model([_regime_vector(params, attack=False)], params=params)


def test_save_load_round_trip(
    model: AnomalyModel, params: AnomalyParamsConfig, tmp_path: Path
) -> None:
    save_anomaly_model(tmp_path, model)
    loaded = load_anomaly_model(tmp_path)
    assert loaded.params_fingerprint == model.params_fingerprint
    assert loaded.feature_names == model.feature_names
    attack = _regime_vector(params, attack=True)
    assert loaded.score_one(attack) == pytest.approx(model.score_one(attack))


def test_load_missing_bundle_raises(tmp_path: Path) -> None:
    with pytest.raises(AnomalyBundleError, match="manifest"):
        load_anomaly_model(tmp_path)
