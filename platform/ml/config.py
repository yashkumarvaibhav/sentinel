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


class BehavioralRegime(MlConfigModel):
    """One behavioral ratio's normal vs attack (mean, sd) distributions."""

    feature: Identifier
    normal: tuple[float, float]
    attack: tuple[float, float]

    @model_validator(mode="after")
    def validate_regime(self) -> Self:
        if self.normal[1] < 0.0 or self.attack[1] < 0.0:
            raise ValueError("regime standard deviations must be non-negative")
        return self


class AnomalySimulation(MlConfigModel):
    """The SIMULATED behavioral-sample generator settings."""

    seeds: tuple[int, ...] = Field(min_length=1)
    normal_samples: int = Field(ge=1, le=1_000_000)
    attack_samples: int = Field(ge=1, le=1_000_000)
    regimes: tuple[BehavioralRegime, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_simulation(self) -> Self:
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("simulation seeds must be unique")
        features = [regime.feature for regime in self.regimes]
        if len(features) != len(set(features)):
            raise ValueError("each regime feature may appear only once")
        return self


class AnomalyParamsConfig(MlConfigModel):
    """Isolation-forest hyperparameters plus the SIMULATED behavioral generator."""

    version: Literal[1]
    n_estimators: int = Field(ge=1, le=10_000)
    max_samples: int = Field(ge=2, le=1_000_000)
    contamination: Quantile
    random_state: int = Field(ge=0)
    features: tuple[Identifier, ...] = Field(min_length=1)
    simulation: AnomalySimulation

    @model_validator(mode="after")
    def validate_anomaly(self) -> Self:
        if len(self.features) != len(set(self.features)):
            raise ValueError("features must be unique")
        regime_features = {regime.feature for regime in self.simulation.regimes}
        if regime_features != set(self.features):
            raise ValueError("simulation regimes must cover exactly the configured features")
        return self

    @property
    def fingerprint(self) -> str:
        """Content hash recorded inside any anomaly bundle trained with these params."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


_AUTH_SEQUENCE_FEATURES = ("inter_arrival", "is_failure", "is_new_account")


class AuthRegime(MlConfigModel):
    """One auth-sequence regime: inter-arrival distribution + per-step probabilities."""

    inter_arrival: tuple[float, float]
    failure_prob: Probability
    new_account_prob: Probability

    @model_validator(mode="after")
    def validate_regime(self) -> Self:
        if self.inter_arrival[1] < 0.0:
            raise ValueError("inter_arrival standard deviation must be non-negative")
        return self


class AuthSimulation(MlConfigModel):
    """The SIMULATED auth-sequence generator settings."""

    seeds: tuple[int, ...] = Field(min_length=1)
    normal_sequences: int = Field(ge=1, le=1_000_000)
    attack_sequences: int = Field(ge=1, le=1_000_000)
    normal: AuthRegime
    attack: AuthRegime

    @model_validator(mode="after")
    def validate_simulation(self) -> Self:
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("simulation seeds must be unique")
        return self


class AutoencoderParamsConfig(MlConfigModel):
    """LSTM auth-sequence autoencoder architecture, optimisation and generator."""

    version: Literal[1]
    features: tuple[Identifier, ...] = Field(min_length=1)
    seq_len: int = Field(ge=2, le=4096)
    hidden_size: int = Field(ge=1, le=4096)
    latent_size: int = Field(ge=1, le=4096)
    num_layers: int = Field(ge=1, le=8)
    epochs: int = Field(ge=1, le=100_000)
    learning_rate: LearningRate
    seed: int = Field(ge=0)
    simulation: AuthSimulation

    @model_validator(mode="after")
    def validate_autoencoder(self) -> Self:
        if tuple(self.features) != _AUTH_SEQUENCE_FEATURES:
            raise ValueError(f"features must be exactly {list(_AUTH_SEQUENCE_FEATURES)}")
        return self

    @property
    def fingerprint(self) -> str:
        """Content hash recorded inside any autoencoder bundle trained with these params."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class DriftDemo(MlConfigModel):
    """A stable-then-shifted stream for the injected-drift demonstration."""

    seed: int = Field(ge=0)
    stable_samples: int = Field(ge=1, le=1_000_000)
    drift_samples: int = Field(ge=1, le=1_000_000)
    stable: tuple[float, float]  # (mean, sd)
    drift: tuple[float, float]  # (mean, sd)

    @model_validator(mode="after")
    def validate_demo(self) -> Self:
        if self.stable[1] < 0.0 or self.drift[1] < 0.0:
            raise ValueError("demo standard deviations must be non-negative")
        return self


class DriftParamsConfig(MlConfigModel):
    """Concept-drift detector settings + the streams the board monitors."""

    version: Literal[1]
    detector: Literal["adwin", "kswin"]
    adwin_delta: Quantile
    kswin_alpha: Quantile
    kswin_window_size: int = Field(ge=2, le=1_000_000)
    kswin_stat_size: int = Field(ge=2, le=1_000_000)
    kswin_seed: int = Field(ge=0)
    streams: tuple[Identifier, ...] = Field(min_length=1)
    demo: DriftDemo

    @model_validator(mode="after")
    def validate_drift(self) -> Self:
        if self.kswin_stat_size >= self.kswin_window_size:
            raise ValueError("kswin_stat_size must be smaller than kswin_window_size")
        if len(self.streams) != len(set(self.streams)):
            raise ValueError("streams must be unique")
        return self

    @property
    def fingerprint(self) -> str:
        """Content hash recorded with any drift report built under these settings."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class CalibrationParamsConfig(MlConfigModel):
    """Conformal-calibration split fraction + calibration-metric settings."""

    version: Literal[1]
    calibration_fraction: Quantile
    alphas: tuple[Quantile, ...] = Field(min_length=1)
    ece_bins: int = Field(ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_calibration(self) -> Self:
        if len(self.alphas) != len(set(self.alphas)):
            raise ValueError("alphas must be unique")
        if list(self.alphas) != sorted(self.alphas):
            raise ValueError("alphas must be listed in ascending order")
        return self

    @property
    def fingerprint(self) -> str:
        """Content hash recorded with any calibrator built under these settings."""
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


def load_anomaly_params(path: Path) -> AnomalyParamsConfig:
    """Load and strictly validate the multivariate-anomaly hyperparameter configuration."""
    try:
        return AnomalyParamsConfig.model_validate(_load_yaml_mapping(path))
    except ValidationError as error:
        raise MlConfigLoadError(f"{path.name}: {error}") from error


def load_autoencoder_params(path: Path) -> AutoencoderParamsConfig:
    """Load and strictly validate the auth-sequence autoencoder configuration."""
    try:
        return AutoencoderParamsConfig.model_validate(_load_yaml_mapping(path))
    except ValidationError as error:
        raise MlConfigLoadError(f"{path.name}: {error}") from error


def load_calibration_params(path: Path) -> CalibrationParamsConfig:
    """Load and strictly validate the conformal-calibration configuration."""
    try:
        return CalibrationParamsConfig.model_validate(_load_yaml_mapping(path))
    except ValidationError as error:
        raise MlConfigLoadError(f"{path.name}: {error}") from error


def load_drift_params(path: Path) -> DriftParamsConfig:
    """Load and strictly validate the concept-drift monitoring configuration."""
    try:
        return DriftParamsConfig.model_validate(_load_yaml_mapping(path))
    except ValidationError as error:
        raise MlConfigLoadError(f"{path.name}: {error}") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Validate ml configuration file(s) and print their reproducibility fingerprints."""
    parser = argparse.ArgumentParser(prog="python -m ml.config")
    parser.add_argument("--path", type=Path, help="synthetic-history training configuration")
    parser.add_argument("--envelopes", type=Path, help="envelope hyperparameter configuration")
    parser.add_argument("--forecast", type=Path, help="seasonal forecast hyperparameters")
    parser.add_argument("--anomaly", type=Path, help="multivariate anomaly hyperparameters")
    parser.add_argument(
        "--autoencoder", type=Path, help="auth-sequence autoencoder hyperparameters"
    )
    parser.add_argument("--calibration", type=Path, help="conformal-calibration settings")
    parser.add_argument("--drift", type=Path, help="concept-drift monitoring settings")
    args = parser.parse_args(argv)
    provided = (
        args.path,
        args.envelopes,
        args.forecast,
        args.anomaly,
        args.autoencoder,
        args.calibration,
        args.drift,
    )
    if all(arg is None for arg in provided):
        parser.error("at least one configuration path is required")
    if args.path is not None:
        print(f"training configuration valid: {load_training_config(args.path).fingerprint}")
    if args.envelopes is not None:
        print(f"envelope configuration valid: {load_envelope_params(args.envelopes).fingerprint}")
    if args.forecast is not None:
        print(f"forecast configuration valid: {load_forecast_params(args.forecast).fingerprint}")
    if args.anomaly is not None:
        print(f"anomaly configuration valid: {load_anomaly_params(args.anomaly).fingerprint}")
    if args.autoencoder is not None:
        fingerprint = load_autoencoder_params(args.autoencoder).fingerprint
        print(f"autoencoder configuration valid: {fingerprint}")
    if args.calibration is not None:
        fingerprint = load_calibration_params(args.calibration).fingerprint
        print(f"calibration configuration valid: {fingerprint}")
    if args.drift is not None:
        fingerprint = load_drift_params(args.drift).fingerprint
        print(f"drift configuration valid: {fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
