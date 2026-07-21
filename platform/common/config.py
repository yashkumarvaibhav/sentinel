"""Strict loader for Sentinel's versioned operator-owned YAML configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

type Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
type SignalName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=512,
        pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+$",
    ),
]
type Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
type PositiveFloat = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
type NonNegativeFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
type Percentage = Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
type BoundedRatio = Annotated[float, Field(ge=0.0, le=10.0, allow_inf_nan=False)]
type CompetitionCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z0-9]{2,4}$"),
]

_FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "attack_flag",
        "expected_action",
        "expected_verdict",
        "ground_truth",
        "injected_fault_id",
        "scenario_label",
    }
)
_FILES = {
    "topology": "topology.yml",
    "events": "event-calendar.yml",
    "detectors": "detector-params.yml",
    "slos": "slo.yml",
    "cohorts": "cohorts.yml",
}


class ConfigLoadError(ValueError):
    """A configuration file could not be parsed or validated safely."""


def main(argv: Sequence[str] | None = None) -> int:
    """Validate a configuration directory and print its reproducibility fingerprint."""
    parser = argparse.ArgumentParser(prog="python -m common.config")
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args(argv)
    config = load_config(args.path)
    print(f"configuration valid: {config.fingerprint}")
    return 0


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


type UtcDatetime = Annotated[datetime, AfterValidator(_utc)]


class ConfigModel(BaseModel):
    """Strict immutable base for every committed configuration artifact."""

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


class TopologyService(ConfigModel):
    """One service and its direct downstream dependencies."""

    service: Identifier
    tier: Literal["edge", "application", "data", "infrastructure"]
    criticality: Literal["low", "medium", "high", "critical"]
    dependencies: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def reject_self_dependency(self) -> Self:
        if self.service in self.dependencies:
            raise ValueError("a service cannot depend on itself")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("dependencies must be unique")
        return self


class TopologyConfig(ConfigModel):
    version: Literal[1]
    services: tuple[TopologyService, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph_references(self) -> Self:
        names = [service.service for service in self.services]
        if len(names) != len(set(names)):
            raise ValueError("topology service ids must be unique")
        known = set(names)
        for service in self.services:
            missing = sorted(set(service.dependencies) - known)
            if missing:
                raise ValueError(
                    f"{service.service} references unknown dependencies: {', '.join(missing)}"
                )
        return self


class CalendarEvent(ConfigModel):
    event_id: Identifier
    name: Identifier
    event_type: Identifier
    source: Identifier
    honesty: Literal["REAL", "SIMULATED"]
    enabled: bool
    valid_from: UtcDatetime
    valid_to: UtcDatetime
    expected_delta: dict[SignalName, float] = Field(min_length=1)
    trust_score: Probability

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class SportsConnectorConfig(ConfigModel):
    """Operator-owned mapping from a real fixture feed to expected signal deltas."""

    enabled: bool
    provider: Literal["football-data.org"]
    competition_codes: tuple[CompetitionCode, ...] = Field(min_length=1, max_length=5)
    lead_minutes: int = Field(ge=0, le=1_440)
    duration_minutes: int = Field(ge=1, le=1_440)
    expected_delta: dict[SignalName, float] = Field(min_length=1)
    trust_score: Probability

    @model_validator(mode="after")
    def unique_competitions(self) -> Self:
        if len(self.competition_codes) != len(set(self.competition_codes)):
            raise ValueError("competition_codes must be unique")
        return self


class EventCalendarConfig(ConfigModel):
    version: Literal[1]
    events: tuple[CalendarEvent, ...]
    sports_connector: SportsConnectorConfig | None = None

    @model_validator(mode="after")
    def unique_events(self) -> Self:
        ids = [event.event_id for event in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("calendar event ids must be unique")
        return self


class BehavioralRatioRuleConfig(ConfigModel):
    """Operator-owned deformation and saturation parameters for one ratio."""

    baseline_floor: PositiveFloat
    trigger_relative_deformation: PositiveFloat
    full_score_relative_deformation: PositiveFloat
    ratio_ceiling: PositiveFloat | None = None

    @model_validator(mode="after")
    def validate_score_range(self) -> Self:
        if self.full_score_relative_deformation < self.trigger_relative_deformation:
            raise ValueError(
                "full_score_relative_deformation must be greater than or equal to "
                "trigger_relative_deformation"
            )
        return self


class BehavioralRatioConfig(ConfigModel):
    """Scale-free behavioral monitors; thresholds remain configuration, not code."""

    source_entropy: BehavioralRatioRuleConfig
    auth_failure: BehavioralRatioRuleConfig
    syn_ack: BehavioralRatioRuleConfig
    rpc_amplification: BehavioralRatioRuleConfig

    @model_validator(mode="after")
    def validate_ceiling_semantics(self) -> Self:
        for name in ("source_entropy", "auth_failure"):
            if getattr(self, name).ratio_ceiling is not None:
                raise ValueError(f"{name} must not define ratio_ceiling")
        for name in ("syn_ack", "rpc_amplification"):
            if getattr(self, name).ratio_ceiling is None:
                raise ValueError(f"{name} must define ratio_ceiling")
        return self


class DetectorConfig(ConfigModel):
    version: Literal[1]
    feature_window_seconds: int = Field(ge=1, le=3600)
    watermark_lateness_seconds: int = Field(ge=0, le=3600)
    ewma_alpha: Probability
    baseline_warmup_points: int = Field(ge=2, le=100_000)
    baseline_update_gate_ratio: BoundedRatio
    expected_band_relative_tolerance: BoundedRatio
    absolute_noise_floors: dict[SignalName, NonNegativeFloat] = Field(min_length=1)
    behavioral_ratios: BehavioralRatioConfig

    @model_validator(mode="after")
    def validate_watermark(self) -> Self:
        if self.watermark_lateness_seconds > self.feature_window_seconds:
            raise ValueError("watermark lateness cannot exceed the feature window")
        if self.ewma_alpha == 0.0:
            raise ValueError("ewma_alpha must be greater than zero")
        if self.baseline_update_gate_ratio == 0.0:
            raise ValueError("baseline_update_gate_ratio must be greater than zero")
        return self


class ServiceSlo(ConfigModel):
    service: Identifier
    availability_target: Probability
    latency_p95_ms: PositiveFloat
    evaluation_window_minutes: int = Field(ge=1, le=43_200)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.availability_target == 0.0:
            raise ValueError("availability_target must be greater than zero")
        return self


class SloConfig(ConfigModel):
    version: Literal[1]
    slos: tuple[ServiceSlo, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_services(self) -> Self:
        services = [slo.service for slo in self.slos]
        if len(services) != len(set(services)):
            raise ValueError("each service may define only one SLO")
        return self


type MatchValue = str | bool | int


class CohortDefinition(ConfigModel):
    cohort_id: Identifier
    description: Identifier
    match: dict[Identifier, MatchValue] = Field(min_length=1)
    protected: bool
    max_blast_radius_pct: Percentage


class CohortConfig(ConfigModel):
    version: Literal[1]
    cohorts: tuple[CohortDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cohorts(self) -> Self:
        ids = [cohort.cohort_id for cohort in self.cohorts]
        if len(ids) != len(set(ids)):
            raise ValueError("cohort ids must be unique")
        if not any(cohort.protected for cohort in self.cohorts):
            raise ValueError("at least one protected cohort is required")
        return self


class SentinelConfig(ConfigModel):
    """One fully cross-validated configuration snapshot."""

    topology: TopologyConfig
    events: EventCalendarConfig
    detectors: DetectorConfig
    slos: SloConfig
    cohorts: CohortConfig

    @property
    def fingerprint(self) -> str:
        """Content hash recorded with captures and decision transcripts."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def load_config(directory: Path) -> SentinelConfig:
    """Load all required artifacts and reject unsafe cross-file references."""
    root = directory.resolve()
    topology = _load(root / _FILES["topology"], TopologyConfig)
    events = _load(root / _FILES["events"], EventCalendarConfig)
    detectors = _load(root / _FILES["detectors"], DetectorConfig)
    slos = _load(root / _FILES["slos"], SloConfig)
    cohorts = _load(root / _FILES["cohorts"], CohortConfig)
    bundle = SentinelConfig(
        topology=topology,
        events=events,
        detectors=detectors,
        slos=slos,
        cohorts=cohorts,
    )
    _validate_references(bundle)
    return bundle


def _load[T: ConfigModel](path: Path, model: type[T]) -> T:
    if not path.is_file():
        raise ConfigLoadError(f"{path.name}: required configuration file is missing")
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except (OSError, yaml.YAMLError) as error:
        raise ConfigLoadError(f"{path.name}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigLoadError(f"{path.name}: YAML root must be a mapping")
    _reject_ground_truth(document, filename=path.name)
    try:
        return model.model_validate(document)
    except ValidationError as error:
        raise ConfigLoadError(f"{path.name}: {error}") from error


def _validate_references(config: SentinelConfig) -> None:
    services = {service.service for service in config.topology.services}
    for slo in config.slos.slos:
        if slo.service not in services:
            raise ConfigLoadError(f"slo.yml: {slo.service} references an unknown topology service")
    event_signals = [signal for event in config.events.events for signal in event.expected_delta]
    if config.events.sports_connector is not None:
        event_signals.extend(config.events.sports_connector.expected_delta)
    for filename, signals in (
        ("event-calendar.yml", iter(event_signals)),
        ("detector-params.yml", iter(config.detectors.absolute_noise_floors)),
    ):
        for signal in signals:
            service, _ = signal.split(".", maxsplit=1)
            if service not in services:
                raise ConfigLoadError(
                    f"{filename}: {signal} references an unknown topology service"
                )


def _reject_ground_truth(value: object, *, filename: str) -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            if isinstance(raw_key, str) and _normalized_key(raw_key) in _FORBIDDEN_CONFIG_KEYS:
                raise ConfigLoadError(
                    f"{filename}: ground-truth configuration key is forbidden: {raw_key}"
                )
            _reject_ground_truth(child, filename=filename)
    elif isinstance(value, list):
        for child in value:
            _reject_ground_truth(child, filename=filename)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that treats duplicate mapping keys as configuration errors."""

    def construct_mapping(
        self,
        node: MappingNode,
        deep: bool = False,
    ) -> dict[object, object]:
        return _construct_unique_mapping(self, node, deep)


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key ({key!r})",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


if __name__ == "__main__":
    raise SystemExit(main())
