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

# One segment of an Envoy runtime key. Deliberately narrower than the key itself:
# a segment carrying a dot would silently re-nest the key under a different
# parent, and one carrying `&` or `=` would smuggle a second assignment into the
# admin query string.
_RUNTIME_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_]*")

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


class MeshConfig(ActionConfigModel):
    """Which edge proxy the mesh actuator reaches, and which cohorts it may restrain.

    ``cohorts`` maps a cohort in ``config/cohorts.yml`` to the runtime-key
    segment the edge proxy publishes for it. The mapping is written down rather
    than derived from the cohort id because the two artifacts belong to
    different owners: a cohort is a statement about users, and a runtime key is
    a fact about the proxy's committed configuration. A cohort that is not
    mapped is refused, never guessed at.
    """

    context: Identifier
    namespace: Identifier
    workload: Identifier
    component_label: Identifier = "app.kubernetes.io/component"
    admin_port: Annotated[int, Field(ge=1, le=65_535)] = 10_000
    ratelimit_key_prefix: Identifier = "sentinel.ratelimit"
    throttle_key_prefix: Identifier = "sentinel.throttle"
    cohorts: dict[Identifier, Identifier] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mesh(self) -> Self:
        if not self.cohorts:
            raise ValueError(
                "a mesh actuator that may restrain no cohort can do nothing; map at least one"
            )
        if _KUBERNETES_NAME.fullmatch(self.namespace) is None:
            raise ValueError(f"{self.namespace} is not a valid Kubernetes namespace")
        if _KUBERNETES_NAME.fullmatch(self.workload) is None:
            raise ValueError(f"{self.workload} is not a valid Kubernetes object name")
        unsafe = sorted(
            name for name in self.cohorts.values() if _RUNTIME_SEGMENT.fullmatch(name) is None
        )
        if unsafe:
            raise ValueError(f"not usable as runtime-key segments: {', '.join(unsafe)}")
        if self.ratelimit_key_prefix == self.throttle_key_prefix:
            raise ValueError(
                "refusing and slowing a cohort are different effects and cannot share a "
                "runtime key; one would silently overwrite the other"
            )
        return self


class FlagRemediation(ActionConfigModel):
    """One flag the platform may set, and the single variant it may set it to.

    The variant is committed rather than passed in, and that is the whole safety
    design of this adapter. The testbed's flags are *fault injectors*:
    ``paymentFailure: on`` is a weapon and ``paymentFailure: off`` is a remedy.
    An actuator that could write any variant could cause the incident it was
    dispatched to fix, so the set of (flag, variant) pairs it may ever write is
    exactly this table.
    """

    service: Identifier
    variant: Identifier


class FlagsConfig(ActionConfigModel):
    """Where the flag document lives, and which flags may be put back to safe."""

    context: Identifier
    namespace: Identifier
    config_map: Identifier
    document_key: Identifier
    workload: Identifier
    component_label: Identifier = "app.kubernetes.io/component"
    ofrep_port: Annotated[int, Field(ge=1, le=65_535)] = 8_016
    rollout_timeout_seconds: Annotated[int, Field(ge=1, le=900)] = 120
    remediations: dict[Identifier, FlagRemediation] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_flags(self) -> Self:
        if not self.remediations:
            raise ValueError(
                "a flag actuator with no remediation may set nothing; list at least one flag"
            )
        unsafe = sorted(
            name
            for name in (self.namespace, self.workload, self.config_map)
            if _KUBERNETES_NAME.fullmatch(name) is None
        )
        if unsafe:
            raise ValueError(f"not valid Kubernetes object names: {', '.join(unsafe)}")
        return self

    def flags_for(self, service: str) -> tuple[str, ...]:
        """Every flag whose remediation affects this service, in a stable order."""
        return tuple(
            sorted(flag for flag, entry in self.remediations.items() if entry.service == service)
        )


class ActionConfig(ActionConfigModel):
    """One fully validated snapshot of how the action plane may execute."""

    version: Literal[1]
    execution: ExecutionConfig
    actuators: tuple[ActuatorConfig, ...] = ()
    kubernetes: KubernetesConfig | None = None
    mesh: MeshConfig | None = None
    flags: FlagsConfig | None = None

    @model_validator(mode="after")
    def validate_actuators(self) -> Self:
        named = [entry.actuator for entry in self.actuators]
        if len(named) != len(set(named)):
            raise ValueError("each actuator may be configured at most once")
        enabled = self.enabled_actuators()
        if ActuatorKind.KUBERNETES in enabled and self.kubernetes is None:
            raise ValueError(
                "the kubernetes actuator is enabled but no `kubernetes:` section says which "
                "cluster it may reach; an adapter with no stated target is not permitted"
            )
        if ActuatorKind.MESH in enabled and self.mesh is None:
            raise ValueError(
                "the mesh actuator is enabled but no `mesh:` section says which edge proxy it "
                "may reach; an adapter with no stated target is not permitted"
            )
        if ActuatorKind.FEATURE_FLAG in enabled and self.flags is None:
            raise ValueError(
                "the feature-flag actuator is enabled but no `flags:` section says which flags "
                "it may set; an adapter with no stated target is not permitted"
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
