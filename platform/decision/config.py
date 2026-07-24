"""Strict loader for the operator-owned evidence-agent configuration.

The decision plane keeps its own configuration artifact rather than joining the
runtime ``SentinelConfig`` bundle. Phase 1 and Phase 2 captures, goldens and
score reports are pinned by that bundle's fingerprint, so folding decision
weights into it would move the detector fingerprint for reasons that have
nothing to do with detection. This file carries its own fingerprint; how a
decision transcript is pinned is settled when the decision goldens are frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from contracts import ChangeKind, EvidenceAxis, SymptomKind

CRITICALITY_LEVELS = ("critical", "high", "medium", "low")


def _named_symptom_kind(value: object) -> object:
    """Accept the YAML spelling of a symptom kind under strict validation."""
    if isinstance(value, str):
        try:
            return SymptomKind(value)
        except ValueError as error:
            known = ", ".join(kind.value for kind in SymptomKind)
            raise ValueError(f"unknown symptom kind {value!r}; known kinds are {known}") from error
    return value


def _named_change_kind(value: object) -> object:
    """Accept the YAML spelling of a change kind under strict validation."""
    if isinstance(value, str):
        try:
            return ChangeKind(value)
        except ValueError as error:
            known = ", ".join(kind.value for kind in ChangeKind)
            raise ValueError(f"unknown change kind {value!r}; known kinds are {known}") from error
    return value


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


def _named_evidence_axis(value: object) -> object:
    """Accept the YAML spelling of an evidence axis under strict validation."""
    if isinstance(value, str):
        try:
            return EvidenceAxis(value)
        except ValueError as error:
            known = ", ".join(axis.value for axis in EvidenceAxis)
            raise ValueError(f"unknown evidence axis {value!r}; known axes are {known}") from error
    return value


type ClaimedKind = Annotated[SymptomKind, BeforeValidator(_named_symptom_kind)]
type NamedAxis = Annotated[EvidenceAxis, BeforeValidator(_named_evidence_axis)]
type NamedChangeKind = Annotated[ChangeKind, BeforeValidator(_named_change_kind)]
type UtcDatetime = Annotated[datetime, AfterValidator(_utc)]
type Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
type Summary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]
type EpisodeSignal = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
type Weight = Annotated[float, Field(gt=0.0, le=1.0, allow_inf_nan=False)]
type Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
type PositiveSeconds = Annotated[float, Field(gt=0.0, le=86_400.0, allow_inf_nan=False)]


class DecisionConfigLoadError(ValueError):
    """A decision-plane configuration file could not be parsed or validated safely."""


class DecisionConfigModel(BaseModel):
    """Strict, immutable base for the decision-plane configuration artifact."""

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


class EvidenceClaimConfig(DecisionConfigModel):
    """One kind of durable symptom an axis treats as evidence, and how strongly.

    ``signals`` restricts the claim to named episode signals; omitting it claims
    every signal of that kind. The weight is the operator's statement of how
    much a fully-breaching episode of this kind argues for the axis.
    """

    kind: ClaimedKind
    weight: Weight
    signals: tuple[EpisodeSignal, ...] | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        if self.signals is None:
            return self
        if not self.signals:
            raise ValueError("omit signals to claim a whole kind; an empty list claims nothing")
        if len(self.signals) != len(set(self.signals)):
            raise ValueError("claimed signals must be unique")
        return self


class ChangePressureConfig(DecisionConfigModel):
    """How recent operator change is turned into deploy-correlated pressure.

    A change is evidence while it is recent: relevance decays linearly to
    nothing at ``correlation_window_seconds``. A change on a service that is not
    itself symptomatic is not discarded - it keeps
    ``unrelated_service_factor`` of its weight, because a change can break a
    neighbour through a dependency it does not own.
    """

    correlation_window_seconds: PositiveSeconds
    unrelated_service_factor: Probability
    kind_weights: dict[str, float] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_change_pressure(self) -> Self:
        known = {kind.value for kind in ChangeKind}
        unknown = sorted(set(self.kind_weights) - known)
        if unknown:
            raise ValueError(f"unknown change kinds: {', '.join(unknown)}")
        for kind, weight in self.kind_weights.items():
            if not 0.0 < weight <= 1.0:
                raise ValueError(f"change weight for {kind} must lie in (0, 1]")
        return self

    def weight_for(self, kind: ChangeKind) -> float | None:
        """Return the configured weight for one change kind, or None if unclaimed."""
        return self.kind_weights.get(kind.value)


class CriticalityWeightsConfig(DecisionConfigModel):
    """How much a symptom on a service of each topology criticality matters to users."""

    critical: Weight
    high: Weight
    medium: Weight
    low: Weight

    def weight_for(self, criticality: str) -> float:
        """Return the configured scaling for one topology criticality level."""
        if criticality not in CRITICALITY_LEVELS:
            raise DecisionConfigLoadError(f"unknown service criticality: {criticality}")
        weight: float = getattr(self, criticality)
        return weight


class EvidenceAxisConfig(DecisionConfigModel):
    """Everything one evidence agent is allowed to look at, and its scoring shape."""

    axis: NamedAxis
    minimum_contribution: Probability
    trend_deadband: Probability
    claims: tuple[EvidenceClaimConfig, ...] = Field(min_length=1)
    change_pressure: ChangePressureConfig | None = None
    criticality_weights: CriticalityWeightsConfig | None = None

    @model_validator(mode="after")
    def validate_axis_extras(self) -> Self:
        needs_change = self.axis is EvidenceAxis.CHANGE_CONFIG
        needs_criticality = self.axis is EvidenceAxis.BUSINESS_IMPACT
        if needs_change and self.change_pressure is None:
            raise ValueError("CHANGE_CONFIG requires change_pressure settings")
        if not needs_change and self.change_pressure is not None:
            raise ValueError(f"{self.axis.value} must not configure change_pressure")
        if needs_criticality and self.criticality_weights is None:
            raise ValueError("BUSINESS_IMPACT requires criticality_weights")
        if not needs_criticality and self.criticality_weights is not None:
            raise ValueError(f"{self.axis.value} must not configure criticality_weights")
        return self

    @model_validator(mode="after")
    def validate_axis(self) -> Self:
        whole_kinds: set[SymptomKind] = set()
        scoped: set[tuple[SymptomKind, str]] = set()
        for claim in self.claims:
            if claim.signals is None:
                if claim.kind in whole_kinds:
                    raise ValueError(f"{claim.kind.value} is claimed twice by {self.axis.value}")
                whole_kinds.add(claim.kind)
                continue
            for signal in claim.signals:
                key = (claim.kind, signal)
                if key in scoped:
                    raise ValueError(
                        f"{claim.kind.value}/{signal} is claimed twice by {self.axis.value}"
                    )
                scoped.add(key)
        overlapping = whole_kinds.intersection(kind for kind, _ in scoped)
        if overlapping:
            names = ", ".join(sorted(kind.value for kind in overlapping))
            raise ValueError(
                f"{self.axis.value} claims {names} both whole and per-signal; "
                "double counting is not allowed"
            )
        return self

    @property
    def claimed_kinds(self) -> frozenset[SymptomKind]:
        """Every symptom kind this axis is willing to consider."""
        return frozenset(claim.kind for claim in self.claims)

    def weight_for(self, kind: SymptomKind, signal: str) -> float | None:
        """Return the configured weight for one episode key, or None if unclaimed."""
        for claim in self.claims:
            if claim.kind is not kind:
                continue
            if claim.signals is None or signal in claim.signals:
                return claim.weight
        return None


class EvidenceAgentsConfig(DecisionConfigModel):
    """One fully validated snapshot of every configured evidence axis."""

    version: Literal[1]
    axes: tuple[EvidenceAxisConfig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_axes(self) -> Self:
        names = [axis.axis for axis in self.axes]
        if len(names) != len(set(names)):
            raise ValueError("each evidence axis may be configured only once")
        return self

    def axis(self, axis: EvidenceAxis) -> EvidenceAxisConfig:
        """Return one axis's configuration, refusing to invent an unconfigured axis."""
        for candidate in self.axes:
            if candidate.axis is axis:
                return candidate
        raise DecisionConfigLoadError(f"evidence axis is not configured: {axis.value}")

    @property
    def fingerprint(self) -> str:
        """Content hash recorded with any assessment produced under this configuration."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class DeploymentRecord(DecisionConfigModel):
    """One operator-maintained record of a change the team made to a service."""

    change_id: Identifier
    kind: NamedChangeKind
    service: Identifier
    ts: UtcDatetime
    summary: Summary
    honesty: Literal["REAL", "SIMULATED"]
    revision: Identifier | None = None


class DeploymentLedgerConfig(DecisionConfigModel):
    """The MVP change-evidence source: a committed ledger plus its cluster mappings.

    The full change ledger with real VCS provenance lands with RCA depth. Until
    then this file is what the operator states changed, together with the
    mappings that let observed cluster rollouts and flag flips be attributed to
    a logical service.
    """

    version: Literal[1]
    changes: tuple[DeploymentRecord, ...] = ()
    workload_mappings: dict[Identifier, Identifier] = Field(default_factory=dict)
    flag_mappings: dict[Identifier, Identifier] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ledger(self) -> Self:
        ids = [record.change_id for record in self.changes]
        if len(ids) != len(set(ids)):
            raise ValueError("change ids must be unique")
        return self

    def validate_services(self, known: frozenset[str]) -> None:
        """Reject any change or mapping that names a service outside the topology."""
        named = {
            *(record.service for record in self.changes),
            *self.workload_mappings.values(),
            *self.flag_mappings.values(),
        }
        unknown = sorted(named - known)
        if unknown:
            raise DecisionConfigLoadError(
                f"deployments.yml references unknown topology services: {', '.join(unknown)}"
            )

    @property
    def fingerprint(self) -> str:
        """Content hash recorded with any change evidence drawn from this ledger."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _document(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise DecisionConfigLoadError(f"{path.name}: required configuration file is missing")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DecisionConfigLoadError(f"{path.name}: {error}") from error
    if not isinstance(document, dict):
        raise DecisionConfigLoadError(f"{path.name}: YAML root must be a mapping")
    return document


def load_evidence_agents(path: Path) -> EvidenceAgentsConfig:
    """Load and strictly validate the evidence-agent configuration."""
    try:
        return EvidenceAgentsConfig.model_validate(_document(path))
    except ValidationError as error:
        raise DecisionConfigLoadError(f"{path.name}: {error}") from error


def load_deployment_ledger(path: Path) -> DeploymentLedgerConfig:
    """Load and strictly validate the committed change-evidence ledger."""
    try:
        return DeploymentLedgerConfig.model_validate(_document(path))
    except ValidationError as error:
        raise DecisionConfigLoadError(f"{path.name}: {error}") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Validate decision configuration and print its reproducibility fingerprint."""
    parser = argparse.ArgumentParser(prog="python -m decision")
    parser.add_argument("--agents", type=Path, required=True, help="evidence-agent configuration")
    parser.add_argument("--deployments", type=Path, help="committed change-evidence ledger")
    args = parser.parse_args(argv)
    print(f"evidence-agent configuration valid: {load_evidence_agents(args.agents).fingerprint}")
    if args.deployments is not None:
        fingerprint = load_deployment_ledger(args.deployments).fingerprint
        print(f"deployment ledger valid: {fingerprint}")
    return 0
