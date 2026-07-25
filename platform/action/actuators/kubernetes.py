"""The Kubernetes actuator: scale, roll back and cordon, against a real cluster.

Three rungs, each with a real undo and a real check:

* ``SCALE``    - set an **absolute** replica count; revert restores the count
                 that was observed at apply time.
* ``ROLLBACK`` - ``rollout undo`` a deployment; revert rolls forward again to
                 the revision that was live before we touched it.
* ``ISOLATE``  - cordon the nodes a workload runs on so nothing new schedules
                 there; revert uncordons exactly the nodes we cordoned.

**How it talks to the cluster, and why.** Through ``kubectl`` as a subprocess,
behind a ``ClusterCommand`` seam - not the Python Kubernetes client. The lab
already requires ``kubectl`` and drives the whole testbed through it, so this
adds no dependency to a plane that sits on the safety-critical path; the client
library would pull a transitive tree (requests/urllib3/google-auth/oauthlib)
into an image whose weight this project has twice decided to protect. Just as
importantly, ``--dry-run=server`` is a first-class ``kubectl`` feature and is
exactly the "dry-run diff" this rung needs: the API server validates and admits
the object, and changes nothing.

Two rules this adapter is careful about, both inherited from the interface:

* **Parameters are absolute.** ``replicas: 6`` is an effect that can be
  deduplicated. The count we are scaling *from* is a fact about one application,
  not about the effect, so it travels back as the ``revert_token`` instead of
  being hashed into the key.
* **``verify`` reads the cluster.** It never trusts the apply's exit code - it
  asks the API server what is true now, which is the only version of
  verification worth having.

Every service must be mapped to a workload in ``config/action.yml``. The
``deployments.yml`` mapping runs the other way and is many-to-one, so it cannot
be reversed: ``frontend`` and ``frontend-proxy`` both report as ``frontend``,
and only an operator can say which one an action should aim at.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Literal, Protocol

from action.actuators.base import ActionRejectedError, Actuator, ActuatorError, build_plan
from action.config import KubernetesConfig
from contracts import (
    ActionKind,
    ActionOutcome,
    ActionParameterValue,
    ActionPlan,
    ActionStatus,
    ActuatorKind,
    Decision,
)

# Kubernetes object names, and therefore everything interpolated into an
# argument vector. Nothing that fails this ever reaches a subprocess.
_SAFE_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")

SUPPORTED_RUNGS: frozenset[ActionKind] = frozenset(
    {ActionKind.SCALE, ActionKind.ROLLBACK, ActionKind.ISOLATE}
)

_EXPECTED_EFFECT: dict[ActionKind, str] = {
    ActionKind.SCALE: "the workload reports the requested number of ready replicas",
    ActionKind.ROLLBACK: "the workload settles on a rolled-back revision, fully ready",
    ActionKind.ISOLATE: "every node the workload runs on is unschedulable",
}


class ClusterCommand(Protocol):
    """How this adapter reaches a cluster. One seam, so tests need no cluster."""

    def __call__(self, argv: Sequence[str], *, timeout: int = 60) -> str:
        """Run one cluster command and return its stdout."""


class KubectlUnavailableError(ActuatorError):
    """The cluster could not be reached at all."""


@dataclass(frozen=True, slots=True)
class KubectlCommand:
    """The real cluster, reached through ``kubectl`` on a pinned context."""

    context: str

    def __call__(self, argv: Sequence[str], *, timeout: int = 60) -> str:
        command = ["kubectl", "--context", self.context, *argv]
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except FileNotFoundError as error:
            raise KubectlUnavailableError("kubectl is not on PATH") from error
        except subprocess.TimeoutExpired as error:
            raise KubectlUnavailableError(f"kubectl timed out after {timeout}s") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ActuatorError(f"kubectl failed: {detail}")
        return completed.stdout


@dataclass(slots=True)
class KubernetesActuator(Actuator):
    """Scale, roll back and cordon real workloads, reversibly and idempotently."""

    kind: ClassVar[ActuatorKind] = ActuatorKind.KUBERNETES
    honesty: ClassVar[Literal["REAL", "SIMULATED"]] = "REAL"

    configuration: KubernetesConfig
    command: ClusterCommand | None = None
    _run: ClusterCommand = field(init=False)

    def __post_init__(self) -> None:
        self._run = self.command or KubectlCommand(context=self.configuration.context)

    # --- planning -----------------------------------------------------------

    def plan(
        self,
        decision: Decision,
        *,
        action_kind: ActionKind,
        parameters: Mapping[str, ActionParameterValue] | None = None,
        ts: datetime,
    ) -> ActionPlan:
        """Resolve the decision's target service to a real workload and a real effect."""
        if action_kind not in SUPPORTED_RUNGS:
            supported = ", ".join(sorted(rung.value for rung in SUPPORTED_RUNGS))
            raise ActionRejectedError(
                f"the kubernetes actuator does not carry out {action_kind.value}; it does "
                f"{supported}"
            )
        if decision.target_service is None:
            raise ActionRejectedError(
                f"{decision.decision_id} names no target service, so there is nothing to aim at"
            )
        workload = self._workload_for(decision.target_service)
        values = self._checked_parameters(action_kind, parameters)
        return build_plan(
            decision,
            actuator=self.kind,
            action_kind=action_kind,
            target_ref=f"{self.configuration.namespace}/deployment/{workload}",
            parameters=values,
            reason=decision.reason,
            expected_effect=_EXPECTED_EFFECT[action_kind],
            reversible=True,
            estimated_blast_fraction=self._blast_fraction(action_kind),
            honesty=self.honesty,
            ts=ts,
        )

    def _workload_for(self, service: str) -> str:
        workload = self.configuration.workloads.get(service)
        if workload is None:
            known = ", ".join(sorted(self.configuration.workloads)) or "none"
            raise ActionRejectedError(
                f"no workload is mapped for service {service!r}; an action is refused rather "
                f"than aimed at a guess (mapped services: {known})"
            )
        return workload

    def _checked_parameters(
        self,
        action_kind: ActionKind,
        parameters: Mapping[str, ActionParameterValue] | None,
    ) -> dict[str, ActionParameterValue]:
        values: dict[str, ActionParameterValue] = dict(parameters or {})
        if action_kind is not ActionKind.SCALE:
            if values:
                raise ActionRejectedError(f"{action_kind.value} takes no parameters")
            return values
        if set(values) != {"replicas"}:
            got = ", ".join(sorted(values)) or "nothing"
            raise ActionRejectedError(f"SCALE takes exactly 'replicas'; got {got}")
        replicas = values["replicas"]
        if isinstance(replicas, bool) or not isinstance(replicas, int):
            raise ActionRejectedError(
                "SCALE needs an absolute integer 'replicas'; a relative change cannot be "
                "deduplicated and would be applied twice under a retry"
            )
        if not 0 <= replicas <= self.configuration.maximum_replicas:
            raise ActionRejectedError(
                f"replicas={replicas} is outside the configured "
                f"0..{self.configuration.maximum_replicas} range"
            )
        return values

    def _blast_fraction(self, action_kind: ActionKind) -> float:
        """How much of the mesh this rung can disturb, as the 5.6 guard will read it."""
        if action_kind is ActionKind.ISOLATE:
            # Cordoning a node affects everything scheduled there, not one workload.
            return 1.0
        return round(1.0 / max(len(self.configuration.workloads), 1), 6)

    # --- carrying out -------------------------------------------------------

    def simulate(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Ask the cluster whether this would be admitted, and change nothing."""
        workload = self._plan_workload(plan)
        match plan.action_kind:
            case ActionKind.SCALE:
                admitted = self._run(
                    [
                        *self._scoped(["scale", f"deployment/{workload}"]),
                        f"--replicas={self._replicas(plan)}",
                        "--dry-run=server",
                    ]
                ).strip()
                observed = admitted or f"{workload} would accept the scale"
            case ActionKind.ROLLBACK:
                observed = f"{workload} is at revision {self._revision(workload)}"
            case _:
                nodes = self._nodes_for(workload)
                observed = f"would cordon {len(nodes)} node(s): {', '.join(nodes) or 'none'}"
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.SIMULATED,
            detail=f"would {plan.action_kind.value.lower()} {plan.target_ref}",
            observed=(observed,),
        )

    def apply(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Put the effect in place, recording how to put it back."""
        workload = self._plan_workload(plan)
        match plan.action_kind:
            case ActionKind.SCALE:
                # Read before writing: this is the only moment the previous count
                # is knowable, and it is what revert restores.
                previous = self._desired_replicas(workload)
                self._run(
                    [
                        *self._scoped(["scale", f"deployment/{workload}"]),
                        f"--replicas={self._replicas(plan)}",
                    ]
                )
                token = f"replicas={previous}"
            case ActionKind.ROLLBACK:
                previous_revision = self._revision(workload)
                self._run(self._scoped(["rollout", "undo", f"deployment/{workload}"]))
                token = f"revision={previous_revision}"
            case _:
                nodes = self._nodes_for(workload)
                if not nodes:
                    raise ActionRejectedError(
                        f"{workload} runs on no node we can see; there is nothing to cordon"
                    )
                for node in nodes:
                    self._run(["cordon", node])
                token = f"nodes={','.join(nodes)}"
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.APPLIED,
            detail=f"{plan.action_kind.value.lower()} applied to {plan.target_ref}",
            revert_token=token,
        )

    def verify(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Ask the cluster what is true now; never trust the apply's exit code."""
        workload = self._plan_workload(plan)
        match plan.action_kind:
            case ActionKind.SCALE:
                wanted = self._replicas(plan)
                ready = self._ready_replicas(workload)
                present = ready == wanted
                found = f"{workload} reports {ready} ready replicas, wanted {wanted}"
            case ActionKind.ROLLBACK:
                present = self._rollout_is_complete(workload)
                found = (
                    f"{workload} is at revision {self._revision(workload)} and "
                    f"{'fully rolled out' if present else 'still rolling'}"
                )
            case _:
                nodes = self._nodes_for(workload)
                cordoned = [node for node in nodes if self._is_unschedulable(node)]
                present = bool(nodes) and len(cordoned) == len(nodes)
                found = f"{len(cordoned)} of {len(nodes)} node(s) unschedulable"
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.VERIFIED if present else ActionStatus.FAILED,
            detail=found,
            observed=(plan.expected_effect,) if present else (),
        )

    def revert(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        revert_token: str | None = None,
    ) -> ActionOutcome:
        """Put the workload back using the token this adapter minted on apply."""
        workload = self._plan_workload(plan)
        if revert_token is None:
            raise ActionRejectedError(
                f"{plan.target_ref} cannot be put back without the token recorded when it was "
                "applied; reverting to a guessed state is worse than not reverting"
            )
        match plan.action_kind:
            case ActionKind.SCALE:
                previous = _token_value(revert_token, "replicas")
                _require_digits(previous, "replicas")
                self._run(
                    [*self._scoped(["scale", f"deployment/{workload}"]), f"--replicas={previous}"]
                )
                detail = f"{workload} scaled back to {previous} replicas"
            case ActionKind.ROLLBACK:
                revision = _token_value(revert_token, "revision")
                _require_digits(revision, "revision")
                self._run(
                    self._scoped(
                        ["rollout", "undo", f"deployment/{workload}", f"--to-revision={revision}"]
                    )
                )
                detail = f"{workload} rolled forward to revision {revision}"
            case _:
                names = [name for name in _token_value(revert_token, "nodes").split(",") if name]
                for node in names:
                    _require_safe(node, "node")
                    self._run(["uncordon", node])
                detail = f"uncordoned {len(names)} node(s) for {workload}"
        return self._outcome(plan, ts=ts, status=ActionStatus.REVERTED, detail=detail)

    # --- cluster reads ------------------------------------------------------

    def _scoped(self, argv: Sequence[str]) -> list[str]:
        return ["-n", self.configuration.namespace, *argv]

    def _desired_replicas(self, workload: str) -> int:
        return _as_count(self._jsonpath(workload, "{.spec.replicas}"))

    def _ready_replicas(self, workload: str) -> int:
        return _as_count(self._jsonpath(workload, "{.status.readyReplicas}"))

    def _revision(self, workload: str) -> str:
        annotation = "{.metadata.annotations.deployment\\.kubernetes\\.io/revision}"
        return self._jsonpath(workload, annotation) or "unknown"

    def _rollout_is_complete(self, workload: str) -> bool:
        desired = self._desired_replicas(workload)
        updated = _as_count(self._jsonpath(workload, "{.status.updatedReplicas}"))
        return desired > 0 and updated == desired and self._ready_replicas(workload) == desired

    def _jsonpath(self, workload: str, expression: str) -> str:
        return self._run(
            self._scoped(["get", f"deployment/{workload}", "-o", f"jsonpath={expression}"])
        ).strip()

    def _nodes_for(self, workload: str) -> list[str]:
        selector = f"{self.configuration.component_label}={workload}"
        raw = self._run(
            self._scoped(
                ["get", "pods", "-l", selector, "-o", "jsonpath={.items[*].spec.nodeName}"]
            )
        )
        return sorted({name for name in raw.split() if name})

    def _is_unschedulable(self, node: str) -> bool:
        _require_safe(node, "node")
        answer = self._run(["get", f"node/{node}", "-o", "jsonpath={.spec.unschedulable}"]).strip()
        return answer.casefold() == "true"

    # --- helpers ------------------------------------------------------------

    def _plan_workload(self, plan: ActionPlan) -> str:
        """Recover the workload from the plan's own target, refusing a foreign one."""
        prefix = f"{self.configuration.namespace}/deployment/"
        if not plan.target_ref.startswith(prefix):
            raise ActionRejectedError(
                f"{plan.target_ref} is not a workload in {self.configuration.namespace}"
            )
        workload = plan.target_ref.removeprefix(prefix)
        if workload not in set(self.configuration.workloads.values()):
            raise ActionRejectedError(
                f"{workload!r} is not a mapped workload; an action is refused rather than aimed "
                "at whatever a plan happened to name"
            )
        _require_safe(workload, "workload")
        return workload

    def _replicas(self, plan: ActionPlan) -> int:
        replicas = plan.parameters.get("replicas")
        if isinstance(replicas, bool) or not isinstance(replicas, int):
            raise ActionRejectedError(f"{plan.plan_id} carries no absolute replica count")
        return replicas

    def _outcome(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        status: ActionStatus,
        detail: str,
        observed: tuple[str, ...] = (),
        revert_token: str | None = None,
    ) -> ActionOutcome:
        return ActionOutcome(
            outcome_id=f"{plan.plan_id}-{status.value.lower()}-{int(ts.timestamp())}",
            ts=ts,
            plan_id=plan.plan_id,
            idempotency_key=plan.idempotency_key,
            status=status,
            dry_run=status is ActionStatus.SIMULATED,
            detail=detail,
            observed=observed,
            revert_token=revert_token,
            honesty=self.honesty,
        )


def _as_count(value: str) -> int:
    """A missing count is zero; a non-numeric one is a cluster we do not understand."""
    if not value:
        return 0
    if not value.isdigit():
        raise ActuatorError(f"expected a replica count, the cluster said {value!r}")
    return int(value)


def _token_value(token: str, key: str) -> str:
    """Read one field out of a revert token this adapter itself minted."""
    prefix = f"{key}="
    if not token.startswith(prefix):
        raise ActionRejectedError(
            f"revert token {token!r} does not record a {key}; it was minted for another rung"
        )
    value = token.removeprefix(prefix)
    if not value:
        raise ActionRejectedError(f"revert token {token!r} records an empty {key}")
    return value


def _require_digits(value: str, field_name: str) -> None:
    if not value.isdigit():
        raise ActionRejectedError(f"{field_name} must be a number, got {value!r}")


def _require_safe(value: str, field_name: str) -> None:
    if _SAFE_NAME.fullmatch(value) is None:
        raise ActionRejectedError(f"unsafe {field_name}: {value!r}")
