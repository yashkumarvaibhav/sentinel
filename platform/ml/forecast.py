"""Seasonal STL/MSTL forecast baselines with an explicit event regressor.

The learned envelopes (3.2/3.3) answer "is this tick inside the learned band".
This forecaster answers a complementary question — "what value does the calendar
alone predict here" — from classical seasonal decomposition rather than trees.

It is a per-signal expected-value baseline built in three deterministic steps:

1. **Event regressor (context).** Each training value is divided by
   ``1 + event_lift`` (the trusted a-priori lift carried in the shared feature
   schema, matching the decomposition's multiplicative event semantics exactly),
   yielding the event-free series an STL fit can decompose cleanly. The same
   ``1 + event_lift`` is re-applied at predict time.
2. **Seasonal-trend decomposition.** statsmodels MSTL separates the diurnal and
   weekly cycles (falling back to single STL, then a flat mean, on shorter
   history). The seasonal components become clock-indexed profiles — one offset
   per time-of-day bucket and per weekday — so any future timestamp is
   forecastable, and the forward level is a robust median of the STL trend.
3. **Forecast.** ``trend_level + daily[tod] + weekly[weekday]`` re-scaled by the
   event regressor.

STL/MSTL are LOESS-based and carry no random seed, so a bundle is reproducible
from a regular series + these versioned parameters. Prophet was deliberately not
used (it pulls a compiled Stan backend onto this shared box); Chronos-Bolt as a
foundation-model second opinion is deferred.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import numpy.typing as npt
from statsmodels.tsa.seasonal import MSTL, STL

from ml.config import ForecastParamsConfig
from ml.frames import TrainingFrame

_BUNDLE_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_SECONDS_PER_DAY = 86_400
_DAILY_CYCLE_DAYS = 1
_WEEKLY_CYCLE_DAYS = 7
_SUPPORTED_CYCLE_DAYS = frozenset({_DAILY_CYCLE_DAYS, _WEEKLY_CYCLE_DAYS})

_METHOD_MSTL = "mstl"
_METHOD_STL = "stl"
_METHOD_MEAN = "mean"

FloatArray = npt.NDArray[np.float64]


class ForecastFitError(ValueError):
    """No signal had a regular series long enough to fit a forecaster."""


class ForecastBundleError(ValueError):
    """A saved forecast bundle is missing or inconsistent."""


def _tod_bucket(ts: datetime, tick_seconds: int) -> int:
    """The clock-aligned time-of-day bucket a timestamp falls in."""
    seconds = ts.hour * 3600 + ts.minute * 60 + ts.second
    return seconds // tick_seconds


@dataclass(frozen=True)
class SignalForecaster:
    """One signal's fitted seasonal profiles, trend level and event floor."""

    signal_key: str
    tick_seconds: int
    floor: float
    method: str
    trend_level: float
    daily_profile: Mapping[int, float]
    weekly_profile: Mapping[int, float]
    train_rows: int

    def predict(self, ts: datetime, event_lift: float) -> float:
        """The calendar-expected value at a timestamp, re-scaled by the event lift."""
        base = self.trend_level
        if self.daily_profile:
            base += self.daily_profile.get(_tod_bucket(ts, self.tick_seconds), 0.0)
        if self.weekly_profile:
            base += self.weekly_profile.get(ts.weekday(), 0.0)
        return max(self.floor, base * (1.0 + max(event_lift, 0.0)))


@dataclass(frozen=True)
class ForecastModel:
    """Every signal's forecaster plus the provenance identifying how it was fit."""

    version: int
    params_fingerprint: str
    training_data_hash: str
    training_config_fingerprint: str
    detector_config_fingerprint: str
    signals: Mapping[str, SignalForecaster]

    def predict(self, signal_key: str, ts: datetime, event_lift: float) -> float | None:
        """The calendar-expected value for a signal, or None when none was fit for it."""
        forecaster = self.signals.get(signal_key)
        return None if forecaster is None else forecaster.predict(ts, event_lift)


def seasonal_history_frames(frames: Sequence[TrainingFrame]) -> tuple[TrainingFrame, ...]:
    """Only the regular synthetic history spans a full seasonal cycle.

    Development-capture baselines are minutes long — too short to teach a diurnal,
    let alone weekly, cycle — so seasonal forecasting fits the synthetic history
    alone. Synthetic seeds are disjoint from every held-out seed by construction
    (enforced in the dataset builder), so this selection carries no leakage risk.
    """
    return tuple(frame for frame in frames if frame.source == "synthetic")


def _event_free_value(frame: TrainingFrame) -> float:
    """Divide out the trusted event lift, matching the decomposition's event model."""
    lift = frame.features["event_lift"]
    if lift < 0.0:
        raise ForecastFitError(f"event_lift must be non-negative, got {lift}")
    return frame.value / (1.0 + lift)


def _regular_series(frames: Sequence[TrainingFrame]) -> tuple[datetime, int, FloatArray]:
    """Aggregate frames into one event-free value per timestamp on a regular grid.

    Frames replicated across seeds share timestamps; their event-free values are
    averaged, so the series estimates the expected event-free level. A non-uniform
    cadence fails closed rather than silently misaligning the seasonal phase.
    """
    by_ts: dict[datetime, list[float]] = defaultdict(list)
    for frame in frames:
        by_ts[frame.ts].append(_event_free_value(frame))
    times = sorted(by_ts)
    if len(times) < 2:
        raise ForecastFitError("a forecast series needs at least two timestamps")
    deltas = {int((later - earlier).total_seconds()) for earlier, later in pairwise(times)}
    if deltas != {int((times[1] - times[0]).total_seconds())}:
        raise ForecastFitError(f"series is not on a regular grid: deltas {sorted(deltas)}")
    tick_seconds = next(iter(deltas))
    if tick_seconds <= 0:
        raise ForecastFitError("series timestamps must strictly increase")
    values = np.asarray([math.fsum(by_ts[ts]) / len(by_ts[ts]) for ts in times], dtype=np.float64)
    return times[0], tick_seconds, values


def _usable_periods(
    n: int, tick_seconds: int, params: ForecastParamsConfig
) -> list[tuple[int, int]]:
    """The (cycle_days, period_ticks) seasonalities the history can support, ascending."""
    usable: list[tuple[int, int]] = []
    for cycle_days in params.seasonal_periods_days:
        if cycle_days not in _SUPPORTED_CYCLE_DAYS:
            raise ForecastFitError(
                f"unsupported seasonal cycle of {cycle_days} days (only diurnal=1 and weekly=7)"
            )
        period_ticks = cycle_days * _SECONDS_PER_DAY // tick_seconds
        if period_ticks < 2:
            continue
        if n > params.min_cycles_per_period * period_ticks:
            usable.append((cycle_days, period_ticks))
    return sorted(usable, key=lambda pair: pair[1])


def _clock_profile(
    seasonal: FloatArray, start_ts: datetime, tick_seconds: int, *, by_weekday: bool
) -> dict[int, float]:
    """Average a seasonal component into a clock-indexed profile (weekday or time-of-day)."""
    groups: dict[int, list[float]] = defaultdict(list)
    tick = timedelta(seconds=tick_seconds)
    for index in range(len(seasonal)):
        ts = start_ts + tick * index
        key = ts.weekday() if by_weekday else _tod_bucket(ts, tick_seconds)
        groups[key].append(float(seasonal[index]))
    return {key: math.fsum(values) / len(values) for key, values in sorted(groups.items())}


def _trend_level(trend: FloatArray, tick_seconds: int, params: ForecastParamsConfig) -> float:
    """A robust forward level: the median of the STL trend over the last N days."""
    ticks_per_day = _SECONDS_PER_DAY // tick_seconds
    window = max(1, params.trend_level_days * ticks_per_day)
    tail = [float(value) for value in trend[-window:]]
    return statistics.median(tail)


def fit_signal_forecaster(
    signal_key: str,
    frames: Sequence[TrainingFrame],
    *,
    params: ForecastParamsConfig,
) -> SignalForecaster:
    """Fit one signal's seasonal forecaster from its regular event-free series."""
    start_ts, tick_seconds, values = _regular_series(frames)
    n = len(values)
    usable = _usable_periods(n, tick_seconds, params)
    if not usable:
        return SignalForecaster(
            signal_key=signal_key,
            tick_seconds=tick_seconds,
            floor=params.floor,
            method=_METHOD_MEAN,
            trend_level=math.fsum(values.tolist()) / n,
            daily_profile={},
            weekly_profile={},
            train_rows=n,
        )
    daily: dict[int, float] = {}
    weekly: dict[int, float] = {}
    if len(usable) == 1:
        (cycle_days, period) = usable[0]
        result = STL(
            values, period=period, seasonal=params.seasonal_smoother, robust=params.robust
        ).fit()
        seasonal = np.asarray(result.seasonal, dtype=np.float64)
        trend = np.asarray(result.trend, dtype=np.float64)
        if cycle_days == _WEEKLY_CYCLE_DAYS:
            weekly = _clock_profile(seasonal, start_ts, tick_seconds, by_weekday=True)
        else:
            daily = _clock_profile(seasonal, start_ts, tick_seconds, by_weekday=False)
        method = _METHOD_STL
    else:
        result = MSTL(
            values,
            periods=tuple(period for _, period in usable),
            windows=tuple(params.seasonal_smoother for _ in usable),
            stl_kwargs={"robust": params.robust},
        ).fit()  # type: ignore[no-untyped-call]
        seasonal = np.asarray(result.seasonal, dtype=np.float64)
        trend = np.asarray(result.trend, dtype=np.float64)
        if seasonal.ndim == 1:
            # MSTL dropped a period as too long for the history; the remaining
            # component is the fastest (diurnal) cycle.
            daily = _clock_profile(seasonal, start_ts, tick_seconds, by_weekday=False)
            method = _METHOD_STL
        else:
            for column, (cycle_days, _) in enumerate(usable):
                component = seasonal[:, column]
                if cycle_days == _WEEKLY_CYCLE_DAYS:
                    weekly = _clock_profile(component, start_ts, tick_seconds, by_weekday=True)
                else:
                    daily = _clock_profile(component, start_ts, tick_seconds, by_weekday=False)
            method = _METHOD_MSTL
    return SignalForecaster(
        signal_key=signal_key,
        tick_seconds=tick_seconds,
        floor=params.floor,
        method=method,
        trend_level=_trend_level(trend, tick_seconds, params),
        daily_profile=daily,
        weekly_profile=weekly,
        train_rows=n,
    )


def fit_forecasters(
    frames: Sequence[TrainingFrame],
    *,
    params: ForecastParamsConfig,
    training_data_hash: str = "",
    training_config_fingerprint: str = "",
    detector_config_fingerprint: str = "",
) -> ForecastModel:
    """Fit one seasonal forecaster per signal with a regular series long enough to model."""
    grouped: dict[str, list[TrainingFrame]] = defaultdict(list)
    for frame in frames:
        grouped[frame.signal_key].append(frame)
    signals = {
        signal_key: fit_signal_forecaster(signal_key, grouped[signal_key], params=params)
        for signal_key in sorted(grouped)
        if len({frame.ts for frame in grouped[signal_key]}) >= params.min_rows_per_signal
    }
    if not signals:
        raise ForecastFitError(
            f"no signal reached the {params.min_rows_per_signal}-timestamp fitting minimum"
        )
    return ForecastModel(
        version=_BUNDLE_VERSION,
        params_fingerprint=params.fingerprint,
        training_data_hash=training_data_hash,
        training_config_fingerprint=training_config_fingerprint,
        detector_config_fingerprint=detector_config_fingerprint,
        signals=signals,
    )


def save_forecast_model(directory: Path, model: ForecastModel) -> None:
    """Write the forecast bundle as a single JSON manifest (no pickle, no binaries)."""
    directory.mkdir(parents=True, exist_ok=True)
    signal_entries = [
        {
            "signal_key": forecaster.signal_key,
            "tick_seconds": forecaster.tick_seconds,
            "floor": forecaster.floor,
            "method": forecaster.method,
            "trend_level": forecaster.trend_level,
            "train_rows": forecaster.train_rows,
            "daily_profile": {
                str(key): value for key, value in sorted(forecaster.daily_profile.items())
            },
            "weekly_profile": {
                str(key): value for key, value in sorted(forecaster.weekly_profile.items())
            },
        }
        for _, forecaster in sorted(model.signals.items())
    ]
    manifest = {
        "version": model.version,
        "params_fingerprint": model.params_fingerprint,
        "training_data_hash": model.training_data_hash,
        "training_config_fingerprint": model.training_config_fingerprint,
        "detector_config_fingerprint": model.detector_config_fingerprint,
        "signals": signal_entries,
    }
    (directory / _MANIFEST_NAME).write_text(
        json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_profile(raw: object) -> dict[int, float]:
    if not isinstance(raw, dict):
        raise ForecastBundleError("a forecast profile must be a JSON object")
    return {int(key): float(value) for key, value in raw.items()}


def load_forecast_model(directory: Path) -> ForecastModel:
    """Load a saved forecast bundle, reconstructing every signal's profiles."""
    manifest_path = directory / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise ForecastBundleError(f"no forecast bundle manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    signals: dict[str, SignalForecaster] = {}
    for entry in manifest["signals"]:
        signals[entry["signal_key"]] = SignalForecaster(
            signal_key=str(entry["signal_key"]),
            tick_seconds=int(entry["tick_seconds"]),
            floor=float(entry["floor"]),
            method=str(entry["method"]),
            trend_level=float(entry["trend_level"]),
            daily_profile=_load_profile(entry["daily_profile"]),
            weekly_profile=_load_profile(entry["weekly_profile"]),
            train_rows=int(entry["train_rows"]),
        )
    return ForecastModel(
        version=int(manifest["version"]),
        params_fingerprint=str(manifest["params_fingerprint"]),
        training_data_hash=str(manifest["training_data_hash"]),
        training_config_fingerprint=str(manifest["training_config_fingerprint"]),
        detector_config_fingerprint=str(manifest["detector_config_fingerprint"]),
        signals=signals,
    )


def load_forecast_model_or_none(directory: Path) -> ForecastModel | None:
    """Load a bundle if present, else None so callers fall back to deterministic bands."""
    if not (directory / _MANIFEST_NAME).is_file():
        return None
    return load_forecast_model(directory)
