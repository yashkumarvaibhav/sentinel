"""Seasonal STL forecast baselines: event regressor, seasonality, degradation, eval."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ml.config import ForecastParamsConfig, load_forecast_params, load_training_config
from ml.evaluate import evaluate_forecast, forecast_holdout_metrics
from ml.features import ActiveEvent, aware_features
from ml.forecast import (
    ForecastFitError,
    ForecastModel,
    SignalForecaster,
    fit_forecasters,
    fit_signal_forecaster,
    load_forecast_model,
    save_forecast_model,
    seasonal_history_frames,
)
from ml.frames import TrainingFrame
from ml.synthetic import generate_all_synthetic_history

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
SIGNAL = "frontend.request_rate"
_ANCHOR = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday, matching ml-training.yml


def _tick(index: int) -> timedelta:
    return timedelta(seconds=600 * index)


@pytest.fixture(scope="module")
def params() -> ForecastParamsConfig:
    return load_forecast_params(CONFIG_ROOT / "ml-forecast.yml")


@pytest.fixture(scope="module")
def synthetic_frames() -> tuple[TrainingFrame, ...]:
    training = load_training_config(CONFIG_ROOT / "ml-training.yml")
    return generate_all_synthetic_history(training)


@pytest.fixture(scope="module")
def model(synthetic_frames: Sequence[TrainingFrame], params: ForecastParamsConfig) -> ForecastModel:
    return fit_forecasters(synthetic_frames, params=params, training_data_hash="test-hash")


def _frame(ts: datetime, value: float, active: Sequence[ActiveEvent] = ()) -> TrainingFrame:
    return TrainingFrame(
        signal_key=SIGNAL,
        ts=ts,
        value=value,
        features=aware_features(ts, active),
        source="synthetic",
        honesty="SIMULATED",
        origin_seed=40100,
        scenario_id="synthetic_history",
        seed_purpose="synthetic",
    )


def test_event_regressor_rescales_prediction_multiplicatively() -> None:
    # The context regressor is exact: an event lift L scales the baseline by (1 + L),
    # matching the decomposition's multiplicative event model.
    forecaster = SignalForecaster(
        signal_key=SIGNAL,
        tick_seconds=600,
        floor=0.0,
        method="mstl",
        trend_level=8.0,
        daily_profile={42: 1.0},
        weekly_profile={0: 0.5},
        train_rows=4032,
    )
    ts = datetime(2026, 1, 5, 7, 0, tzinfo=UTC)  # bucket 42 (7*3600/600), a Monday
    base = forecaster.predict(ts, event_lift=0.0)
    lifted = forecaster.predict(ts, event_lift=1.35)
    assert base == pytest.approx(8.0 + 1.0 + 0.5)
    assert lifted == pytest.approx(base * (1.0 + 1.35))


def test_predict_clamps_at_floor() -> None:
    forecaster = SignalForecaster(
        signal_key=SIGNAL,
        tick_seconds=600,
        floor=0.0,
        method="stl",
        trend_level=1.0,
        daily_profile={42: -5.0},  # a trough that drives the base negative
        weekly_profile={},
        train_rows=1000,
    )
    ts = datetime(2026, 1, 5, 7, 0, tzinfo=UTC)
    assert forecaster.predict(ts, event_lift=0.0) == 0.0


def test_fit_is_deterministic(
    model: ForecastModel,
    synthetic_frames: Sequence[TrainingFrame],
    params: ForecastParamsConfig,
) -> None:
    fresh = fit_forecasters(synthetic_frames, params=params).signals[SIGNAL]
    base = model.signals[SIGNAL]
    assert fresh.trend_level == base.trend_level
    assert fresh.daily_profile == base.daily_profile
    assert fresh.weekly_profile == base.weekly_profile
    assert fresh.method == base.method


def test_fit_learns_diurnal_and_weekly_seasonality(model: ForecastModel) -> None:
    forecaster = model.signals[SIGNAL]
    assert forecaster.method == "mstl"
    assert len(forecaster.daily_profile) == 144  # one bucket per 10-min tick of the day
    assert set(forecaster.weekly_profile) == set(range(7))
    # The weekend (Python weekday 5, 6) sits well below the weekday level.
    weekday_mean = sum(forecaster.weekly_profile[d] for d in range(5)) / 5
    weekend_mean = (forecaster.weekly_profile[5] + forecaster.weekly_profile[6]) / 2
    assert weekend_mean < weekday_mean - 1.0
    # The diurnal peak lands near the configured 14:00 UTC peak (bucket 84).
    peak_bucket = max(forecaster.daily_profile, key=lambda b: forecaster.daily_profile[b])
    assert abs(peak_bucket - 84) <= 8


def test_robust_path_fits(params: ForecastParamsConfig) -> None:
    # Exercise the ~6x-costlier robust LOESS path on a short (cheap) diurnal history.
    frames = [_frame(_ANCHOR + _tick(i), 8.0 + (i % 5)) for i in range(144 * 10)]
    robust = params.model_copy(update={"robust": True, "min_rows_per_signal": 288})
    forecaster = fit_signal_forecaster(SIGNAL, frames, params=robust)
    assert forecaster.method == "stl"
    assert forecaster.daily_profile


def test_short_history_falls_back_to_flat_mean(params: ForecastParamsConfig) -> None:
    # One day of ticks cannot support even a diurnal cycle -> flat mean baseline.
    frames = [_frame(_ANCHOR + _tick(i), 8.0 + (i % 3)) for i in range(140)]
    low_min = params.model_copy(update={"min_rows_per_signal": 50})
    forecaster = fit_signal_forecaster(SIGNAL, frames, params=low_min)
    assert forecaster.method == "mean"
    assert forecaster.daily_profile == {}
    assert forecaster.weekly_profile == {}


def test_medium_history_is_diurnal_only(params: ForecastParamsConfig) -> None:
    # Ten days supports the diurnal cycle but not two full weekly cycles.
    frames = [_frame(_ANCHOR + _tick(i), 8.0) for i in range(144 * 10)]
    low_min = params.model_copy(update={"min_rows_per_signal": 288})
    forecaster = fit_signal_forecaster(SIGNAL, frames, params=low_min)
    assert forecaster.method == "stl"
    assert forecaster.daily_profile
    assert forecaster.weekly_profile == {}


def test_irregular_series_fails_closed(params: ForecastParamsConfig) -> None:
    frames = [
        _frame(_ANCHOR, 8.0),
        _frame(_ANCHOR + _tick(1), 8.0),
        _frame(_ANCHOR + _tick(3), 8.0),  # a gap breaks the regular grid
    ]
    with pytest.raises(ForecastFitError, match="regular grid"):
        fit_signal_forecaster(SIGNAL, frames, params=params)


def test_negative_event_lift_is_rejected(params: ForecastParamsConfig) -> None:
    negative = _frame(_ANCHOR, 8.0)
    features = dict(negative.features)
    features["event_lift"] = -0.5
    poisoned = TrainingFrame(
        signal_key=SIGNAL,
        ts=_ANCHOR,
        value=8.0,
        features=features,
        source="synthetic",
        honesty="SIMULATED",
        origin_seed=40100,
        scenario_id="synthetic_history",
        seed_purpose="synthetic",
    )
    with pytest.raises(ForecastFitError, match="non-negative"):
        fit_signal_forecaster(SIGNAL, [poisoned, _frame(_ANCHOR + _tick(1), 8.0)], params=params)


def test_empty_fit_raises(
    synthetic_frames: Sequence[TrainingFrame], params: ForecastParamsConfig
) -> None:
    impossible = params.model_copy(update={"min_rows_per_signal": 10_000_000})
    with pytest.raises(ForecastFitError, match="fitting minimum"):
        fit_forecasters(synthetic_frames, params=impossible)


def test_save_load_round_trip(model: ForecastModel, tmp_path: Path) -> None:
    save_forecast_model(tmp_path, model)
    loaded = load_forecast_model(tmp_path)
    assert loaded.params_fingerprint == model.params_fingerprint
    assert loaded.training_data_hash == "test-hash"
    assert set(loaded.signals) == set(model.signals)
    ts = datetime(2026, 1, 10, 20, 0, tzinfo=UTC)  # a Saturday inside an event window
    lift = aware_features(ts, [ActiveEvent(2.5, 0.9)])["event_lift"]
    assert loaded.predict(SIGNAL, ts, lift) == pytest.approx(model.predict(SIGNAL, ts, lift))


def test_seasonal_history_frames_excludes_captures() -> None:
    synthetic = _frame(_ANCHOR, 8.0)
    capture = TrainingFrame(
        signal_key=SIGNAL,
        ts=_ANCHOR,
        value=8.0,
        features=aware_features(_ANCHOR, ()),
        source="capture",
        honesty="REAL",
        origin_seed=101,
        scenario_id="quiet_day",
        seed_purpose="development",
    )
    kept = seasonal_history_frames([synthetic, capture])
    assert kept == (synthetic,)


def test_evaluate_forecast_scores_error_exactly(model: ForecastModel) -> None:
    forecaster = model.signals[SIGNAL]
    times = [datetime(2026, 2, 2, tzinfo=UTC) + _tick(i) for i in range(48)]
    # Frames whose value is exactly the forecast -> zero error.
    exact = [_frame(ts, forecaster.predict(ts, 0.0)) for ts in times]
    (perfect,) = evaluate_forecast(model, exact)
    assert perfect.mae == pytest.approx(0.0, abs=1e-9)
    assert perfect.rmse == pytest.approx(0.0, abs=1e-9)
    assert perfect.bias == pytest.approx(0.0, abs=1e-9)
    # Frames two units above the forecast -> bias is predicted-minus-observed = -2.
    biased = [_frame(ts, forecaster.predict(ts, 0.0) + 2.0) for ts in times]
    (metric,) = evaluate_forecast(model, biased)
    assert metric.bias == pytest.approx(-2.0)
    assert metric.mae == pytest.approx(2.0)


def test_forecast_holdout_metrics_generalise(
    synthetic_frames: Sequence[TrainingFrame], params: ForecastParamsConfig
) -> None:
    (metric,) = forecast_holdout_metrics(synthetic_frames, params=params, holdout_fraction=0.2)
    assert metric.signal_key == SIGNAL
    assert metric.eval_rows > 0
    # The seasonal baseline recovers the signal to roughly the irreducible noise floor.
    assert metric.nrmse < 0.15
    assert abs(metric.bias) < 0.1
