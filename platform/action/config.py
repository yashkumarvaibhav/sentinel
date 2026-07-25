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

from contracts import ActionKind, ActionParameterValue, ActuatorKind, VerdictClass

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


def _named_action_kind(value: object) -> object:
    """Accept the YAML spelling of a rung under strict validation."""
    if isinstance(value, str):
        try:
            return ActionKind(value)
        except ValueError as error:
            known = ", ".join(member.value for member in ActionKind)
            raise ValueError(f"unknown rung {value!r}; known rungs are {known}") from error
    return value


type PositiveSeconds = Annotated[float, Field(gt=0.0, le=86_400.0, allow_inf_nan=False)]
type NamedActuatorKind = Annotated[ActuatorKind, BeforeValidator(_named_actuator_kind)]
type NamedActionKind = Annotated[ActionKind, BeforeValidator(_named_action_kind)]

# The ladders name diagnoses, and a diagnosis this build does not have is a
# typo rather than a future feature.
_VERDICT_CLASSES: frozenset[str] = frozenset(member.value for member in VerdictClass)


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


class LadderRung(ActionConfigModel):
    """One rung: an adapter, an effect, and the certainty it costs to reach it."""

    rung_id: Identifier
    action_kind: NamedActionKind
    actuator: NamedActuatorKind
    minimum_confidence: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    autonomous: bool
    maximum_blast_fraction: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    ttl_seconds: Annotated[int, Field(ge=1, le=86_400)]
    parameters: dict[Identifier, ActionParameterValue] = Field(default_factory=dict)
    parameters_from: Literal["committed_flag"] | None = None
    paired_with: Identifier | None = None

    @model_validator(mode="after")
    def validate_rung(self) -> Self:
        if self.action_kind is ActionKind.OBSERVE and self.maximum_blast_fraction != 0.0:
            raise ValueError(f"{self.rung_id}: observing changes nothing, so it disturbs nothing")
        if self.parameters_from is not None and self.parameters:
            raise ValueError(
                f"{self.rung_id}: a resolved rung may not also carry literal parameters; one "
                "source of parameters or the other, never both"
            )
        if (
            self.parameters_from == "committed_flag"
            and self.action_kind is not ActionKind.FLAG_FLIP
        ):
            raise ValueError(f"{self.rung_id}: only a FLAG_FLIP rung resolves a committed flag")
        return self


class Ladder(ActionConfigModel):
    """One ordered ladder, and the diagnoses it answers."""

    ladder_id: Identifier
    verdict_classes: tuple[Identifier, ...] = Field(min_length=1)
    reason: str
    rungs: tuple[LadderRung, ...] = Field(min_length=1)
    companions: tuple[LadderRung, ...] = ()

    @model_validator(mode="after")
    def validate_ladder(self) -> Self:
        named = [rung.rung_id for rung in (*self.rungs, *self.companions)]
        if len(named) != len(set(named)):
            raise ValueError(f"{self.ladder_id}: each rung is named once")
        thresholds = [rung.minimum_confidence for rung in self.rungs]
        if thresholds != sorted(thresholds):
            raise ValueError(
                f"{self.ladder_id}: rungs are ordered weakest first, so their minimum confidences "
                "may not decrease; a ladder read in a different order is a different ladder"
            )
        companions = {rung.rung_id for rung in self.companions}
        for rung in self.companions:
            if rung.paired_with is not None:
                raise ValueError(f"{rung.rung_id}: a companion may not itself pair with one")
        for rung in self.rungs:
            if rung.paired_with is not None and rung.paired_with not in companions:
                raise ValueError(
                    f"{rung.rung_id}: pairs with {rung.paired_with}, which is not a companion of "
                    f"{self.ladder_id}; a companion is never chosen alone and must be declared"
                )
        return self


class LadderConfig(ActionConfigModel):
    """Every ladder the platform may climb, and nothing it may improvise."""

    version: Literal[1]
    ladders: tuple[Ladder, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ladders(self) -> Self:
        named = [ladder.ladder_id for ladder in self.ladders]
        if len(named) != len(set(named)):
            raise ValueError("each ladder is named once")
        claimed: dict[str, str] = {}
        for ladder in self.ladders:
            for verdict in ladder.verdict_classes:
                if verdict not in _VERDICT_CLASSES:
                    known = ", ".join(sorted(_VERDICT_CLASSES))
                    raise ValueError(f"unknown verdict class {verdict!r}; known classes: {known}")
                owner = claimed.setdefault(verdict, ladder.ladder_id)
                if owner != ladder.ladder_id:
                    raise ValueError(
                        f"{verdict} is answered by both {owner} and {ladder.ladder_id}; two "
                        "ladders for one diagnosis is an ambiguity, not a choice"
                    )
        return self

    @property
    def fingerprint(self) -> str:
        """Content hash recorded with any action chosen under these ladders."""
        rendered = json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def for_verdict(self, verdict_class: str) -> Ladder | None:
        """The one ladder that answers this diagnosis, or none."""
        for ladder in self.ladders:
            if verdict_class in ladder.verdict_classes:
                return ladder
        return None


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
    return _load_document(path, ActionConfig)


def load_ladder_config(path: Path) -> LadderConfig:
    """Load and strictly validate the graded remediation ladders."""
    return _load_document(path, LadderConfig)


def _load_document[T: ActionConfigModel](path: Path, model: type[T]) -> T:
    if not path.is_file():
        raise ActionConfigLoadError(f"{path.name}: required configuration file is missing")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ActionConfigLoadError(f"{path.name}: {error}") from error
    if not isinstance(document, dict):
        raise ActionConfigLoadError(f"{path.name}: YAML root must be a mapping")
    try:
        return model.model_validate(document)
    except ValidationError as error:
        raise ActionConfigLoadError(f"{path.name}: {error}") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Validate action-plane configuration and print its reproducibility fingerprint."""
    parser = argparse.ArgumentParser(prog="python -m action")
    parser.add_argument("--config", type=Path, required=True, help="action-plane configuration")
    parser.add_argument("--ladders", type=Path, help="graded remediation ladders")
    args = parser.parse_args(argv)
    configuration = load_action_config(args.config)
    print(f"action configuration valid: {configuration.fingerprint}")
    if args.ladders is not None:
        ladders = load_ladder_config(args.ladders)
        print(f"ladder configuration valid: {ladders.fingerprint}")
    return 0
