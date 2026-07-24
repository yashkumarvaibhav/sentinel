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
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from contracts import EvidenceAxis, SymptomKind


def _named_symptom_kind(value: object) -> object:
    """Accept the YAML spelling of a symptom kind under strict validation."""
    if isinstance(value, str):
        try:
            return SymptomKind(value)
        except ValueError as error:
            known = ", ".join(kind.value for kind in SymptomKind)
            raise ValueError(f"unknown symptom kind {value!r}; known kinds are {known}") from error
    return value


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
type EpisodeSignal = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
type Weight = Annotated[float, Field(gt=0.0, le=1.0, allow_inf_nan=False)]
type Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


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


class EvidenceAxisConfig(DecisionConfigModel):
    """Everything one evidence agent is allowed to look at, and its scoring shape."""

    axis: NamedAxis
    minimum_contribution: Probability
    trend_deadband: Probability
    claims: tuple[EvidenceClaimConfig, ...] = Field(min_length=1)

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


def load_evidence_agents(path: Path) -> EvidenceAgentsConfig:
    """Load and strictly validate the evidence-agent configuration."""
    if not path.is_file():
        raise DecisionConfigLoadError(f"{path.name}: required configuration file is missing")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DecisionConfigLoadError(f"{path.name}: {error}") from error
    if not isinstance(document, dict):
        raise DecisionConfigLoadError(f"{path.name}: YAML root must be a mapping")
    try:
        return EvidenceAgentsConfig.model_validate(document)
    except ValidationError as error:
        raise DecisionConfigLoadError(f"{path.name}: {error}") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Validate decision configuration and print its reproducibility fingerprint."""
    parser = argparse.ArgumentParser(prog="python -m decision")
    parser.add_argument("--agents", type=Path, required=True, help="evidence-agent configuration")
    args = parser.parse_args(argv)
    print(f"evidence-agent configuration valid: {load_evidence_agents(args.agents).fingerprint}")
    return 0
