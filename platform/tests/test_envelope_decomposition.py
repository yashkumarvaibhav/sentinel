"""Envelope-based decomposition: learned band when a model is registered, else EWMA."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.config import DetectorConfig, load_config
from contracts import ContextWindow, Observation
from detection.decompose import DecompositionEngine
from ml.config import EnvelopeParamsConfig, load_envelope_params, load_training_config
from ml.data import build_training_dataset
from ml.envelopes import EnvelopeModel, train_envelopes
from ml.features import aware_features

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
SIGNAL_KEY = "frontend.request_rate"
# 2026-01-07 is a Wednesday with no synthetic event scheduled (a clean baseline tick).
_TS = datetime(2026, 1, 7, 14, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def params() -> EnvelopeParamsConfig:
    return load_envelope_params(CONFIG_ROOT / "ml-envelopes.yml")


@pytest.fixture(scope="module")
def detector() -> DetectorConfig:
    return load_config(CONFIG_ROOT).detectors


@pytest.fixture(scope="module")
def model(params: EnvelopeParamsConfig) -> EnvelopeModel:
    training = load_training_config(CONFIG_ROOT / "ml-training.yml")
    config = load_config(CONFIG_ROOT)
    dataset = build_training_dataset(repo_root=REPO_ROOT, training_config=training, config=config)
    return train_envelopes(dataset.frames, params=params)


def _obs(observation_id: str, value: float, *, ts: datetime = _TS) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=ts,
        service="frontend",
        signal="request_rate",
        value=value,
    )


def test_envelope_decomposes_immediately_against_the_learned_band(
    detector: DetectorConfig, model: EnvelopeModel
) -> None:
    engine = DecompositionEngine(configuration=detector, dedup_capacity=100, envelope=model)
    band = model.predict(SIGNAL_KEY, aware_features(_TS, ()))
    assert band is not None

    # No EWMA warmup: the very first tick is decomposed against the model band.
    result = engine.decompose(_obs("first", band.expected))
    assert result.status == "decomposed"
    assert result.baseline_updated is False
    frame = result.frame
    assert frame is not None
    assert frame.band_low == pytest.approx(band.lower)
    assert frame.band_high == pytest.approx(band.upper)
    # observed == expected ⇒ zero residual and the value sits inside the band
    assert frame.residual == pytest.approx(0.0, abs=1e-9)
    assert frame.residual_score == 0.0
    # the decomposition identity holds (also enforced by the DecompFrame contract)
    assert frame.explained_base + frame.explained_event + frame.residual == pytest.approx(
        frame.observed
    )


def test_value_above_the_band_scores_residual(
    detector: DetectorConfig, model: EnvelopeModel
) -> None:
    engine = DecompositionEngine(configuration=detector, dedup_capacity=100, envelope=model)
    band = model.predict(SIGNAL_KEY, aware_features(_TS, ()))
    assert band is not None
    frame = engine.decompose(_obs("spike", band.upper + 100.0)).frame
    assert frame is not None
    assert frame.residual > 0.0
    assert frame.residual_score > 0.0
    inside = engine.decompose(_obs("inside", band.expected)).frame
    assert inside is not None
    assert inside.residual_score == 0.0


def test_active_event_raises_the_explained_component(
    detector: DetectorConfig, model: EnvelopeModel
) -> None:
    engine = DecompositionEngine(configuration=detector, dedup_capacity=100, envelope=model)
    context = ContextWindow(
        context_id="match",
        name="match",
        event_type="sports_fixture",
        source="test",
        honesty="SIMULATED",
        valid_from=_TS - timedelta(minutes=1),
        valid_to=_TS + timedelta(minutes=1),
        expected_delta={SIGNAL_KEY: 2.5},
        trust_score=1.0,
    )
    frame = engine.decompose(_obs("event", 20.0), contexts=(context,)).frame
    assert frame is not None
    assert frame.context_ids == ("match",)
    # the aware model attributes part of the volume to the event
    assert frame.explained_event > 0.0
    assert frame.explained_base + frame.explained_event + frame.residual == pytest.approx(
        frame.observed
    )


def test_uncovered_signal_falls_back_to_ewma(
    detector: DetectorConfig, model: EnvelopeModel
) -> None:
    # The envelope covers only frontend.request_rate; checkout.error_ratio must
    # still take the deterministic EWMA path (warming on its first ticks).
    engine = DecompositionEngine(configuration=detector, dedup_capacity=100, envelope=model)
    result = engine.decompose(
        Observation(
            observation_id="err-0",
            ts=_TS,
            service="checkout",
            signal="error_ratio",
            value=0.01,
        )
    )
    assert result.status == "warming"
    assert result.frame is None


def test_no_envelope_is_the_deterministic_path(detector: DetectorConfig) -> None:
    # Without a model, the first request_rate ticks warm the EWMA baseline exactly
    # as before — the fallback is unchanged.
    engine = DecompositionEngine(configuration=detector, dedup_capacity=100)
    result = engine.decompose(_obs("warm-0", 8.0))
    assert result.status == "warming"
    assert result.frame is None
