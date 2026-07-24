"""Measured proof: replay a real development capture through the learned band."""

from __future__ import annotations

from pathlib import Path

import pytest
from lab.captures import load_runtime_capture, replay_decomposition

from common.config import load_config
from ml.config import load_envelope_params, load_training_config
from ml.data import build_training_dataset
from ml.envelopes import EnvelopeModel, train_envelopes
from ml.features import aware_features

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
CAPTURE = REPO_ROOT / "var" / "captures" / "phase1-quiet-101-golden-v1"
SIGNAL_KEY = "frontend.request_rate"


@pytest.fixture(scope="module")
def model() -> EnvelopeModel:
    training = load_training_config(CONFIG_ROOT / "ml-training.yml")
    params = load_envelope_params(CONFIG_ROOT / "ml-envelopes.yml")
    config = load_config(CONFIG_ROOT)
    dataset = build_training_dataset(repo_root=REPO_ROOT, training_config=training, config=config)
    return train_envelopes(dataset.frames, params=params)


def test_capture_replays_through_the_learned_band(model: EnvelopeModel) -> None:
    if not CAPTURE.is_dir():
        pytest.skip("local development capture phase1-quiet-101-golden-v1 is not present")
    capture = load_runtime_capture(CAPTURE)
    config = load_config(CONFIG_ROOT)

    baseline = replay_decomposition(
        capture, detector=config.detectors, replay_config_fingerprint=config.fingerprint
    )
    learned = replay_decomposition(
        capture,
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
        envelope=model,
    )

    # The EWMA path warms up; the envelope path is pre-trained, so every tick is
    # decomposed against the model band from the first observation.
    assert baseline.stats.warming > 0
    assert learned.stats.warming == 0
    assert learned.stats.decomposed == len(learned.steps)

    frames = [step.frame for step in learned.steps if step.frame is not None]
    assert frames
    # this capture carries no context windows, so every band is the model's
    # no-event prediction for that observation's timestamp
    for step in learned.steps:
        if step.frame is None:
            continue
        band = model.predict(SIGNAL_KEY, aware_features(step.observation.ts, ()))
        assert band is not None
        assert step.frame.band_low == pytest.approx(band.lower)
        assert step.frame.band_high == pytest.approx(band.upper)

    # the learned decomposition genuinely differs from the EWMA one
    baseline_bands = {s.frame.band_high for s in baseline.steps if s.frame is not None}
    learned_bands = {s.frame.band_high for s in learned.steps if s.frame is not None}
    assert learned_bands != baseline_bands
