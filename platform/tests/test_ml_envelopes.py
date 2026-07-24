"""The context-conditioned quantile envelopes: monotonicity, context use, IO."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from statistics import mean

import pytest

from common.config import load_config
from ml.config import EnvelopeParamsConfig, load_envelope_params, load_training_config
from ml.data import TrainingDataset, build_training_dataset
from ml.envelopes import (
    EnvelopeModel,
    EnvelopeTrainingError,
    load_envelope_model,
    load_envelope_model_or_none,
    save_envelope_model,
    train_envelopes,
)
from ml.frames import TrainingFrame

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
SIGNAL = "frontend.request_rate"


@pytest.fixture(scope="module")
def params() -> EnvelopeParamsConfig:
    return load_envelope_params(CONFIG_ROOT / "ml-envelopes.yml")


@pytest.fixture(scope="module")
def dataset() -> TrainingDataset:
    # Synthetic-only so the fixture is fully reproducible without local captures.
    training = load_training_config(CONFIG_ROOT / "ml-training.yml")
    config = load_config(CONFIG_ROOT)
    return build_training_dataset(repo_root=REPO_ROOT, training_config=training, config=config)


@pytest.fixture(scope="module")
def model(dataset: TrainingDataset, params: EnvelopeParamsConfig) -> EnvelopeModel:
    return train_envelopes(
        dataset.frames,
        params=params,
        training_data_hash=dataset.manifest.data_hash,
        training_config_fingerprint=dataset.manifest.training_config_fingerprint,
        detector_config_fingerprint=dataset.manifest.detector_config_fingerprint,
    )


def _frames(dataset: TrainingDataset) -> list[TrainingFrame]:
    return [frame for frame in dataset.frames if frame.signal_key == SIGNAL]


def _zero_events(features: Mapping[str, float]) -> dict[str, float]:
    return {**features, "event_lift": 0.0, "event_count": 0.0, "event_trust_max": 0.0}


def test_model_provenance_is_recorded(model: EnvelopeModel, params: EnvelopeParamsConfig) -> None:
    assert model.params_fingerprint == params.fingerprint
    assert model.training_data_hash
    assert set(model.signals) == {SIGNAL}
    assert model.signals[SIGNAL].train_rows == 12096


def test_quantiles_never_cross(model: EnvelopeModel, dataset: TrainingDataset) -> None:
    envelope = model.signals[SIGNAL]
    for frame in _frames(dataset)[::40]:  # a strided sample across the whole history
        band = envelope.predict(frame.features)
        ordered = [band.aware[q] for q in envelope.quantiles]
        assert ordered == sorted(ordered)
        assert band.lower <= band.upper


def test_aware_band_responds_to_event_features(
    model: EnvelopeModel, dataset: TrainingDataset
) -> None:
    envelope = model.signals[SIGNAL]
    event_frames = [f for f in _frames(dataset) if f.features["event_lift"] > 0.0]
    assert event_frames
    with_event = mean(envelope.predict(f.features).upper for f in event_frames)
    without_event = mean(envelope.predict(_zero_events(f.features)).upper for f in event_frames)
    # Turning the event feature off must lower the learned upper band substantially.
    assert with_event > without_event + 1.0


def test_blind_twin_gap_is_positive_on_partially_correlated_events(
    model: EnvelopeModel, dataset: TrainingDataset
) -> None:
    envelope = model.signals[SIGNAL]
    # The midweek promo (event_lift ~= 0.6) fires on only some Wednesdays, so the
    # context-blind twin cannot fully absorb it: the aware-minus-blind gap is > 0.
    promo = [f for f in _frames(dataset) if abs(f.features["event_lift"] - 0.6) < 1e-6]
    assert promo
    gap = mean(envelope.predict(f.features).event_explained_upper for f in promo)
    assert gap > 0.0


def test_training_is_deterministic(dataset: TrainingDataset, params: EnvelopeParamsConfig) -> None:
    first = train_envelopes(dataset.frames, params=params).signals[SIGNAL]
    second = train_envelopes(dataset.frames, params=params).signals[SIGNAL]
    for frame in _frames(dataset)[::400]:
        a = first.predict(frame.features)
        b = second.predict(frame.features)
        assert a.aware == b.aware
        assert a.blind_upper == b.blind_upper


def test_save_load_round_trip(
    model: EnvelopeModel, dataset: TrainingDataset, tmp_path: Path
) -> None:
    save_envelope_model(tmp_path, model)
    reloaded = load_envelope_model(tmp_path)
    assert reloaded.params_fingerprint == model.params_fingerprint
    assert reloaded.training_data_hash == model.training_data_hash
    original = model.signals[SIGNAL]
    restored = reloaded.signals[SIGNAL]
    for frame in _frames(dataset)[::400]:
        assert restored.predict(frame.features).aware == original.predict(frame.features).aware
        assert restored.predict(frame.features).blind_upper == pytest.approx(
            original.predict(frame.features).blind_upper
        )


def test_missing_bundle_returns_none(tmp_path: Path) -> None:
    assert load_envelope_model_or_none(tmp_path) is None


def test_predict_unknown_signal_is_none(model: EnvelopeModel, dataset: TrainingDataset) -> None:
    features = _frames(dataset)[0].features
    assert model.predict("cart.request_rate", features) is None


def test_training_refuses_too_little_data(
    dataset: TrainingDataset, params: EnvelopeParamsConfig
) -> None:
    with pytest.raises(EnvelopeTrainingError):
        train_envelopes(dataset.frames[:5], params=params)
