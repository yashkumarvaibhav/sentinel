"""Strict loader for the seeded synthetic-history training configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from common.config import (
    Identifier,
    NonNegativeFloat,
    PositiveFloat,
    Probability,
    SignalName,
    UtcDatetime,
)

type Hour = Annotated[float, Field(ge=0.0, lt=24.0, allow_inf_nan=False)]
type Factor = Annotated[float, Field(gt=0.0, le=100.0, allow_inf_nan=False)]
type UnitFraction = Annotated[float, Field(ge=0.0, lt=1.0, allow_inf_nan=False)]
type EventMultiplier = Annotated[float, Field(ge=1.0, le=100.0, allow_inf_nan=False)]
type Quantile = Annotated[float, Field(gt=0.0, lt=1.0, allow_inf_nan=False)]
type LearningRate = Annotated[float, Field(gt=0.0, le=1.0, allow_inf_nan=False)]


class MlConfigLoadError(ValueError):
    """A training configuration file could not be parsed or validated safely."""


class MlConfigModel(BaseModel):
    """Strict, immutable base for the training configuration artifact."""

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    @field_validator("*", mode="before")
    @classmethod
    def freeze_yaml_sequences(cls, value: object) -> object:
        """Convert YAML lists to immutable tuples while retaining strict scalar types."""
        return tuple(value) if isinstance(value, list) else value


class HistorySettings(MlConfigModel):
    """The synthetic horizon: anchor, span, cadence and generator seeds."""

    start: UtcDatetime
    horizon_days: int = Field(ge=1, le=366)
    tick_seconds: int = Field(ge=1, le=86_400)
    seeds: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("synthetic seeds must be unique")
        if 86_400 % self.tick_seconds != 0:
            raise ValueError("tick_seconds must divide a 24h day so ticks align to the clock")
        return self


class SignalSpec(MlConfigModel):
    """The seasonal + noise shape of one synthetic signal."""

    signal: SignalName
    base_level: PositiveFloat
    diurnal_amplitude: UnitFraction
    diurnal_peak_hour: Hour
    weekend_factor: Factor
    noise_fraction: UnitFraction
    floor: NonNegativeFloat


class SyntheticEvent(MlConfigModel):
    """A recurring explained-event lift applied to one signal on named days."""

    event_id: Identifier
    event_type: Identifier
    signal: SignalName
    day_indices: tuple[int, ...] = Field(min_length=1)
    start_hour: Hour
    duration_hours: PositiveFloat
    multiplier: EventMultiplier
    trust_score: Probability

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if any(day < 0 for day in self.day_indices):
            raise ValueError("event day_indices must be non-negative")
        if len(self.day_indices) != len(set(self.day_indices)):
            raise ValueError("event day_indices must be unique")
        if self.start_hour + self.duration_hours > 24.0:
            raise ValueError("an event window must stay within a single calendar day")
        return self


class MlTrainingConfig(MlConfigModel):
    """One fully cross-validated synthetic-history configuration snapshot."""

    version: Literal[1]
    history: HistorySettings
    signals: tuple[SignalSpec, ...] = Field(min_length=1)
    events: tuple[SyntheticEvent, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        signals = [spec.signal for spec in self.signals]
        if len(signals) != len(set(signals)):
            raise ValueError("each signal may appear only once")
        known = set(signals)
        for event in self.events:
            if event.signal not in known:
                raise ValueError(f"event {event.event_id} references unknown signal {event.signal}")
            if any(day >= self.history.horizon_days for day in event.day_indices):
                raise ValueError(f"event {event.event_id} lands outside the configured horizon")
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("event ids must be unique")
        return self

    @property
    def fingerprint(self) -> str:
        """Content hash recorded with any dataset built from this configuration."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class EnvelopeParamsConfig(MlConfigModel):
    """LightGBM quantile-regression hyperparameters for the learned envelopes."""

    version: Literal[1]
    quantiles: tuple[Quantile, ...] = Field(min_length=1)
    blind_quantile: Quantile
    learning_rate: LearningRate
    num_leaves: int = Field(ge=2, le=1024)
    n_estimators: int = Field(ge=1, le=10_000)
    min_data_in_leaf: int = Field(ge=1, le=100_000)
    max_depth: int = Field(ge=-1, le=64)
    seed: int = Field(ge=0)
    min_rows_per_signal: int = Field(ge=1, le=10_000_000)

    @model_validator(mode="after")
    def validate_quantiles(self) -> Self:
        if len(self.quantiles) != len(set(self.quantiles)):
            raise ValueError("quantiles must be unique")
        if list(self.quantiles) != sorted(self.quantiles):
            raise ValueError("quantiles must be listed in ascending order")
        if self.blind_quantile not in self.quantiles:
            raise ValueError("blind_quantile must be one of the trained quantiles")
        return self

    @property
    def fingerprint(self) -> str:
        """Content hash recorded inside any model bundle trained with these params."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class ForecastParamsConfig(MlConfigModel):
    """Seasonal STL/MSTL forecast-baseline hyperparameters."""

    version: Literal[1]
    seasonal_periods_days: tuple[int, ...] = Field(min_length=1)
    min_cycles_per_period: int = Field(ge=2, le=100)
    robust: bool
    seasonal_smoother: int = Field(ge=7, le=1001)
    trend_level_days: int = Field(ge=1, le=366)
    min_rows_per_signal: int = Field(ge=2, le=10_000_000)
    floor: NonNegativeFloat
    holdout_fraction: Quantile

    @model_validator(mode="after")
    def validate_forecast(self) -> Self:
        days = self.seasonal_periods_days
        if len(days) != len(set(days)):
            raise ValueError("seasonal_periods_days must be unique")
        if list(days) != sorted(days):
            raise ValueError("seasonal_periods_days must be listed in ascending order")
        if self.seasonal_smoother % 2 == 0:
            raise ValueError("seasonal_smoother must be an odd window length")
        return self

    @property
    def fingerprint(self) -> str:
        """Content hash recorded inside any forecast bundle trained with these params."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise MlConfigLoadError(f"{path.name}: required configuration file is missing")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise MlConfigLoadError(f"{path.name}: {error}") from error
    if not isinstance(document, dict):
        raise MlConfigLoadError(f"{path.name}: YAML root must be a mapping")
    return document


def load_training_config(path: Path) -> MlTrainingConfig:
    """Load and strictly validate the synthetic-history training configuration."""
    try:
        return MlTrainingConfig.model_validate(_load_yaml_mapping(path))
    except ValidationError as error:
        raise MlConfigLoadError(f"{path.name}: {error}") from error


def load_envelope_params(path: Path) -> EnvelopeParamsConfig:
    """Load and strictly validate the envelope hyperparameter configuration."""
    try:
        return EnvelopeParamsConfig.model_validate(_load_yaml_mapping(path))
    except ValidationError as error:
        raise MlConfigLoadError(f"{path.name}: {error}") from error


def load_forecast_params(path: Path) -> ForecastParamsConfig:
    """Load and strictly validate the seasonal forecast hyperparameter configuration."""
    try:
        return ForecastParamsConfig.model_validate(_load_yaml_mapping(path))
    except ValidationError as error:
        raise MlConfigLoadError(f"{path.name}: {error}") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Validate ml configuration file(s) and print their reproducibility fingerprints."""
    parser = argparse.ArgumentParser(prog="python -m ml.config")
    parser.add_argument("--path", type=Path, help="synthetic-history training configuration")
    parser.add_argument("--envelopes", type=Path, help="envelope hyperparameter configuration")
    parser.add_argument("--forecast", type=Path, help="seasonal forecast hyperparameters")
    args = parser.parse_args(argv)
    if args.path is None and args.envelopes is None and args.forecast is None:
        parser.error("at least one of --path, --envelopes or --forecast is required")
    if args.path is not None:
        print(f"training configuration valid: {load_training_config(args.path).fingerprint}")
    if args.envelopes is not None:
        print(f"envelope configuration valid: {load_envelope_params(args.envelopes).fingerprint}")
    if args.forecast is not None:
        print(f"forecast configuration valid: {load_forecast_params(args.forecast).fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
