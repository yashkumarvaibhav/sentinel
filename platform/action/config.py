"""Strict loader for the operator-owned action-plane configuration.

The action plane keeps its own configuration artifact with its own fingerprint,
for the same reason the decision plane does (decision #45): Phase 1 and Phase 2
captures, goldens and score reports are pinned by the runtime ``SentinelConfig``
bundle, and folding execution settings into it would move the detector
fingerprint for reasons that have nothing to do with detection.

One rule here is not merely configuration. ``dry_run`` may be forced *on* by the
environment and can never be forced *off*, so the reviewed file is the only
thing that can put this platform into a state where it touches production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
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

from contracts import ActuatorKind

# The environment may only ever tighten the file's safety posture.
FORCE_DRY_RUN_ENV = "SENTINEL_ACTION_FORCE_DRY_RUN"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# RFC 1123 label, which is what a namespace, a workload and a node all are.
# Validated here so nothing that fails it can ever reach an argument vector.
_KUBERNETES_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")

type Identifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=253)
]


def _named_actuator_kind(value: object) -> object:
    """Accept the YAML spelling of an actuator kind under strict validation."""
    if isinstance(value, str):
        try:
            return ActuatorKind(value)
        except ValueError as error:
            known = ", ".join(member.value for member in ActuatorKind)
            raise ValueError(f"unknown actuator {value!r}; known actuators are {known}") from error
    return value


type PositiveSeconds = Annotated[float, Field(gt=0.0, le=86_400.0, allow_inf_nan=False)]
type NamedActuatorKind = Annotated[ActuatorKind, BeforeValidator(_named_actuator_kind)]


class ActionConfigLoadError(ValueError):
    """The action-plane configuration could not be parsed or validated safely."""


class ActionConfigModel(BaseModel):
    """Strict, immutable base for the action-plane configuration artifact."""

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


class ExecutionConfig(ActionConfigModel):
    """How the executor behaves before any adapter is consulted."""

    dry_run: bool
    lease_ttl_seconds: PositiveSeconds
    journal_capacity: Annotated[int, Field(ge=1, le=1_000_000)]


class ActuatorConfig(ActionConfigModel):
    """Whether one adapter is permitted to run at all."""

    actuator: NamedActuatorKind
    enabled: bool


class KubernetesConfig(ActionConfigModel):
    """Which cluster the Kubernetes actuator reaches, and what it may aim at.

    ``workloads`` maps a topology service to the workload an action targets. It
    is a separate mapping from ``deployments.yml``'s ``workload_mappings`` on
    purpose: that one runs the other way and is many-to-one (``frontend`` and
    ``frontend-proxy`` both report as the ``frontend`` service), so reversing it
    would mean guessing which of them to scale. An unmapped service is refused.
    """

    context: Identifier
    namespace: Identifier
    component_label: Identifier = "app.kubernetes.io/component"
    maximum_replicas: Annotated[int, Field(ge=1, le=100)] = 10
    workloads: dict[Identifier, Identifier] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_workloads(self) -> Self:
        unsafe = sorted(
            name
            for name in (*self.workloads.values(), self.namespace)
            if _KUBERNETES_NAME.fullmatch(name) is None
        )
        if unsafe:
            raise ValueError(f"not valid Kubernetes object names: {', '.join(unsafe)}")
        return self


class ActionConfig(ActionConfigModel):
    """One fully validated snapshot of how the action plane may execute."""

    version: Literal[1]
    execution: ExecutionConfig
    actuators: tuple[ActuatorConfig, ...] = ()
    kubernetes: KubernetesConfig | None = None

    @model_validator(mode="after")
    def validate_actuators(self) -> Self:
        named = [entry.actuator for entry in self.actuators]
        if len(named) != len(set(named)):
            raise ValueError("each actuator may be configured at most once")
        if ActuatorKind.KUBERNETES in self.enabled_actuators() and self.kubernetes is None:
            raise ValueError(
                "the kubernetes actuator is enabled but no `kubernetes:` section says which "
                "cluster it may reach; an adapter with no stated target is not permitted"
            )
        return self

    @property
    def fingerprint(self) -> str:
        """Content hash recorded with any action taken under this configuration."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def enabled_actuators(self) -> frozenset[ActuatorKind]:
        """The adapters permitted to run; anything unlisted is refused."""
        return frozenset(entry.actuator for entry in self.actuators if entry.enabled)


def resolve_dry_run(configuration: ActionConfig, *, environ: Mapping[str, str]) -> bool:
    """Combine the committed posture with the one-way environment latch.

    The latch can only tighten. A deployment can force a live system to pretend;
    nothing in the environment can make a pretending system act.
    """
    if configuration.execution.dry_run:
        return True
    return environ.get(FORCE_DRY_RUN_ENV, "").strip().casefold() in _TRUTHY


def load_action_config(path: Path) -> ActionConfig:
    """Load and strictly validate the action-plane configuration."""
    if not path.is_file():
        raise ActionConfigLoadError(f"{path.name}: required configuration file is missing")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ActionConfigLoadError(f"{path.name}: {error}") from error
    if not isinstance(document, dict):
        raise ActionConfigLoadError(f"{path.name}: YAML root must be a mapping")
    try:
        return ActionConfig.model_validate(document)
    except ValidationError as error:
        raise ActionConfigLoadError(f"{path.name}: {error}") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Validate action-plane configuration and print its reproducibility fingerprint."""
    parser = argparse.ArgumentParser(prog="python -m action")
    parser.add_argument("--config", type=Path, required=True, help="action-plane configuration")
    args = parser.parse_args(argv)
    configuration = load_action_config(args.config)
    print(f"action configuration valid: {configuration.fingerprint}")
    return 0
