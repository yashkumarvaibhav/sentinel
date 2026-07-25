"""The mesh actuator: restrain one cohort at the edge, and leave everyone else alone.

This is the rung it is safe to fire *during* a legitimate surge. Scaling a
workload answers a fault; blocking a service answers an attack by taking the
service down with it. Restraining a cohort answers the thing the guiding
principle actually identifies - the traffic whose *behaviour* deformed - while
the surge the world explains carries on being served.

Two rungs, and they are deliberately two mechanisms rather than two settings of
one (``lab/testbed/envoy.tmpl.yaml`` builds both):

* ``RATE_LIMIT`` - the cohort's configured ceiling is enforced, and traffic over
  it is **refused** with a 429. The ceiling is the operator's number, committed
  in the proxy configuration; this rung only decides how much of the excess is
  actually turned away.
* ``THROTTLE``   - a share of the cohort is **slowed** and nobody is turned
  away. Slowing traffic you are not yet certain about is a different decision
  from refusing it, and a ladder that could not express the difference would be
  offering one rung under two names.

**How it reaches the proxy.** Through the same ``ClusterCommand`` seam the
Kubernetes actuator uses, via the API server's pod-proxy subresource. That
choice matters for two reasons: nothing has to exist inside the Envoy image (no
shell, no curl), and no port is published to reach an admin interface that can
change how production behaves. Envoy's admin API refuses ``GET`` on
``/runtime_modify``, so writes go through ``kubectl create --raw`` with an empty
body; the query string carries the assignment.

**Why ``verify`` is not optional here.** Envoy's admin API accepts a runtime
value that is not a percentage at all: writing ``abc`` returns ``OK``, reports
``abc`` back verbatim from ``/runtime``, and silently leaves the filter on its
default of zero. A rate limit that reports success and restrains nothing is the
worst outcome this plane has, so every key written is read back and required to
be exactly the digits that were sent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Literal
from urllib.parse import quote

from action.actuators.base import ActionRejectedError, Actuator, ActuatorError, build_plan
from action.actuators.kubernetes import ClusterCommand, KubectlCommand
from action.config import MeshConfig
from contracts import (
    ActionKind,
    ActionOutcome,
    ActionParameterValue,
    ActionPlan,
    ActionStatus,
    ActuatorKind,
    Decision,
)

SUPPORTED_RUNGS: frozenset[ActionKind] = frozenset({ActionKind.RATE_LIMIT, ActionKind.THROTTLE})

# The parameter each rung takes. Both are absolute percentages, because a
# relative one ("ten percent harder") cannot be deduplicated and a retry would
# apply it twice - the rule the idempotency key rests on.
_PERCENT_PARAMETER: dict[ActionKind, str] = {
    ActionKind.RATE_LIMIT: "enforced_percent",
    ActionKind.THROTTLE: "delayed_percent",
}

_EXPECTED_EFFECT: dict[ActionKind, str] = {
    ActionKind.RATE_LIMIT: "the edge reports the cohort held to its configured ceiling",
    ActionKind.THROTTLE: "the edge reports the cohort's requests delayed at the stated share",
}

_TOKEN_PREFIX = "runtime="


class EdgeUnreachableError(ActuatorError):
    """The edge proxy could not be found or could not be read."""


@dataclass(slots=True)
class MeshActuator(Actuator):
    """Rate-limit or throttle one cohort at the edge proxy, reversibly."""

    kind: ClassVar[ActuatorKind] = ActuatorKind.MESH
    honesty: ClassVar[Literal["REAL", "SIMULATED"]] = "REAL"

    configuration: MeshConfig
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
        """Resolve a decision and a chosen rung into one cohort's restraint."""
        if action_kind not in SUPPORTED_RUNGS:
            supported = ", ".join(sorted(rung.value for rung in SUPPORTED_RUNGS))
            raise ActionRejectedError(
                f"the mesh actuator does not carry out {action_kind.value}; it does {supported}"
            )
        cohort, percent = self._checked_parameters(action_kind, parameters)
        pod = self._edge_pod()
        return build_plan(
            decision,
            actuator=self.kind,
            action_kind=action_kind,
            target_ref=f"{self.configuration.namespace}/pod/{pod}/cohort/{cohort}",
            parameters={"cohort": cohort, _PERCENT_PARAMETER[action_kind]: percent},
            reason=decision.reason,
            expected_effect=_EXPECTED_EFFECT[action_kind],
            reversible=True,
            # Of the NAMED COHORT, not of the mesh. What share of the system one
            # cohort accounts for is not something this adapter can measure, and
            # an invented number is worse than an honest scope: `cohorts.yml` is
            # where an operator states how much blast radius a cohort may have,
            # and the 5.6 guard reads it with the cohort definition in hand.
            estimated_blast_fraction=percent / 100,
            honesty=self.honesty,
            ts=ts,
        )

    def _checked_parameters(
        self,
        action_kind: ActionKind,
        parameters: Mapping[str, ActionParameterValue] | None,
    ) -> tuple[str, int]:
        values = dict(parameters or {})
        percent_field = _PERCENT_PARAMETER[action_kind]
        if set(values) != {"cohort", percent_field}:
            got = ", ".join(sorted(values)) or "nothing"
            raise ActionRejectedError(
                f"{action_kind.value} takes exactly 'cohort' and '{percent_field}'; got {got}"
            )
        cohort = values["cohort"]
        if not isinstance(cohort, str) or cohort not in self.configuration.cohorts:
            known = ", ".join(sorted(self.configuration.cohorts)) or "none"
            raise ActionRejectedError(
                f"no runtime key is mapped for cohort {cohort!r}; a restraint is refused rather "
                f"than aimed at a guess (mapped cohorts: {known})"
            )
        percent = values[percent_field]
        if isinstance(percent, bool) or not isinstance(percent, int):
            raise ActionRejectedError(
                f"{action_kind.value} needs an absolute integer '{percent_field}'; a relative "
                "change cannot be deduplicated and would be applied twice under a retry"
            )
        if not 1 <= percent <= 100:
            raise ActionRejectedError(
                f"{percent_field}={percent} is outside 1..100; restraining nothing is what "
                "reverting is for, and there is no restraint beyond all of it"
            )
        return cohort, percent

    # --- carrying out -------------------------------------------------------

    def simulate(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Report what the edge would be told, having read it and changed nothing."""
        pod = self._plan_pod(plan)
        intended = self._intended(plan)
        current = self._runtime_values(pod, tuple(intended))
        changes = tuple(
            f"{key}: {current.get(key, 'unset')} -> {value}" for key, value in intended.items()
        )
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.SIMULATED,
            detail=f"would {plan.action_kind.value.lower()} {plan.target_ref}",
            observed=changes,
        )

    def apply(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Raise the cohort's keys, recording what they said before we did."""
        pod = self._plan_pod(plan)
        intended = self._intended(plan)
        # Read before writing: this is the only moment the previous restraint is
        # knowable, and it is what revert restores.
        previous = self._runtime_values(pod, tuple(intended))
        self._modify(pod, intended)
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.APPLIED,
            detail=f"{plan.action_kind.value.lower()} applied to {plan.target_ref}",
            revert_token=_mint_token(tuple(intended), previous),
        )

    def verify(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Read every key back off the proxy; the admin API's `OK` proves nothing."""
        pod = self._plan_pod(plan)
        intended = self._intended(plan)
        observed = self._runtime_values(pod, tuple(intended))
        wrong = sorted(key for key, value in intended.items() if observed.get(key) != value)
        found = ", ".join(f"{key}={observed.get(key, 'unset')}" for key in sorted(intended))
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.FAILED if wrong else ActionStatus.VERIFIED,
            detail=f"the edge reports {found}",
            observed=(plan.expected_effect,) if not wrong else (),
        )

    def revert(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        revert_token: str | None = None,
    ) -> ActionOutcome:
        """Put the cohort back to the restraint it was under before this plan."""
        pod = self._plan_pod(plan)
        if revert_token is None:
            raise ActionRejectedError(
                f"{plan.target_ref} cannot be put back without the token recorded when it was "
                "applied; reverting to a guessed restraint is worse than not reverting"
            )
        previous = _read_token(revert_token)
        if set(previous) != set(self._intended(plan)):
            raise ActionRejectedError(
                f"revert token {revert_token!r} records keys this plan never wrote; it was "
                "minted for another rung"
            )
        self._modify(pod, previous)
        restored = ", ".join(f"{key}={value}" for key, value in sorted(previous.items()))
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.REVERTED,
            detail=f"{plan.target_ref} restored to {restored}",
        )

    # --- the edge -----------------------------------------------------------

    def _intended(self, plan: ActionPlan) -> dict[str, str]:
        """The exact runtime keys and values this plan means, in a stable order."""
        cohort = self._plan_cohort(plan)
        segment = self.configuration.cohorts[cohort]
        percent = plan.parameters.get(_PERCENT_PARAMETER[plan.action_kind])
        if isinstance(percent, bool) or not isinstance(percent, int):
            raise ActionRejectedError(f"{plan.plan_id} carries no absolute percentage")
        if plan.action_kind is ActionKind.RATE_LIMIT:
            prefix = f"{self.configuration.ratelimit_key_prefix}.{segment}"
            # The whole cohort is measured against its ceiling; the dial is how
            # much of the excess is actually refused. Enforcing part of it is
            # what lets 5.6 widen a limit canary-first instead of all at once.
            return {f"{prefix}.enabled": "100", f"{prefix}.enforced": str(percent)}
        return {f"{self.configuration.throttle_key_prefix}.{segment}.percent": str(percent)}

    def _edge_pod(self) -> str:
        """The one proxy process whose runtime layer an action changes.

        A runtime override is per process, so restraining one of several proxies
        would restrain a fraction of traffic nobody chose. Rather than silently
        cover a fraction, this refuses: covering every replica is a real feature
        and deserves to be built deliberately, not inferred from a plural.
        """
        selector = f"{self.configuration.component_label}={self.configuration.workload}"
        raw = self._run(
            [
                "-n",
                self.configuration.namespace,
                "get",
                "pods",
                "-l",
                selector,
                "--field-selector=status.phase=Running",
                "-o",
                "jsonpath={.items[*].metadata.name}",
            ]
        )
        pods = sorted({name for name in raw.split() if name})
        if not pods:
            raise EdgeUnreachableError(
                f"no running {self.configuration.workload} pod in {self.configuration.namespace}; "
                "there is no edge to restrain"
            )
        if len(pods) > 1:
            raise ActionRejectedError(
                f"{len(pods)} {self.configuration.workload} pods are running ({', '.join(pods)}); "
                "a runtime override reaches one process, so restraining one of them would "
                "restrain a share of traffic nobody chose"
            )
        return pods[0]

    def _runtime_values(self, pod: str, keys: Sequence[str]) -> dict[str, str]:
        """What the proxy says these keys are right now."""
        try:
            document = json.loads(self._admin(pod, "runtime"))
        except ValueError as error:
            raise EdgeUnreachableError(
                f"the edge did not return readable runtime state: {error}"
            ) from error
        entries = document.get("entries", {})
        if not isinstance(entries, dict):
            raise EdgeUnreachableError("the edge's runtime state has no entries")
        found: dict[str, str] = {}
        for key in keys:
            entry = entries.get(key)
            if isinstance(entry, dict) and isinstance(entry.get("final_value"), str):
                found[key] = entry["final_value"]
        return found

    def _modify(self, pod: str, values: Mapping[str, str]) -> None:
        """Write runtime keys, one admin call, in a stable order."""
        query = "&".join(
            f"{quote(key, safe='.')}={quote(value)}" for key, value in sorted(values.items())
        )
        answer = self._admin(pod, f"runtime_modify?{query}", write=True).strip()
        if answer and answer.upper() != "OK":
            raise ActuatorError(f"the edge refused the change: {answer}")

    def _admin(self, pod: str, path: str, *, write: bool = False) -> str:
        """One call to this pod's Envoy admin interface, through the API server.

        The pod-proxy subresource is what makes this need nothing inside the
        container and no published port. Envoy answers `GET /runtime_modify`
        with 405, so a write is a POST with an empty body - `/dev/null` rather
        than stdin, because the seam passes an argument vector and not input.
        """
        namespace = self.configuration.namespace
        target = f"{pod}:{self.configuration.admin_port}"
        raw = f"/api/v1/namespaces/{namespace}/pods/{target}/proxy/{path}"
        if write:
            return self._run(["create", "--raw", raw, "-f", "/dev/null"])
        return self._run(["get", "--raw", raw])

    # --- helpers ------------------------------------------------------------

    def _plan_pod(self, plan: ActionPlan) -> str:
        """Recover the proxy from the plan's own target, refusing a foreign one."""
        namespace, _, remainder = plan.target_ref.partition("/pod/")
        pod, _, _ = remainder.partition("/cohort/")
        if namespace != self.configuration.namespace or not pod:
            raise ActionRejectedError(
                f"{plan.target_ref} is not an edge proxy in {self.configuration.namespace}"
            )
        # A plan is data, and data can be wrong: the pod it names must still be
        # the edge, not whatever process happened to be recorded.
        live = self._edge_pod()
        if pod != live:
            raise ActionRejectedError(
                f"{plan.target_ref} names {pod}, but the edge is now {live}; a restraint applied "
                "to a replaced proxy would be applied to nothing"
            )
        return live

    def _plan_cohort(self, plan: ActionPlan) -> str:
        cohort = plan.parameters.get("cohort")
        if not isinstance(cohort, str) or cohort not in self.configuration.cohorts:
            raise ActionRejectedError(
                f"{plan.plan_id} names no mapped cohort; a restraint is refused rather than "
                "aimed at whatever a plan happened to name"
            )
        if not plan.target_ref.endswith(f"/cohort/{cohort}"):
            raise ActionRejectedError(
                f"{plan.target_ref} and parameter cohort {cohort!r} disagree about who is "
                "being restrained"
            )
        return cohort

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


def _mint_token(keys: Sequence[str], previous: Mapping[str, str]) -> str:
    """Record what every key this plan writes said before we wrote it.

    A key the proxy did not publish, or published as something that is not a
    percentage at all, is recorded as ``0`` rather than skipped: the edge ships
    every cohort key at zero, so "not stated" means "no restraint", and a token
    that quietly omitted a key would leave a cohort restrained after the
    platform believed it had let go.
    """

    def _restraint(key: str) -> str:
        value = previous.get(key, "")
        return value if value.isdigit() else "0"

    return _TOKEN_PREFIX + "|".join(f"{key}:{_restraint(key)}" for key in sorted(keys))


def _read_token(token: str) -> dict[str, str]:
    """Read back a token this adapter itself minted."""
    if not token.startswith(_TOKEN_PREFIX):
        raise ActionRejectedError(
            f"revert token {token!r} does not record runtime keys; it was minted for another rung"
        )
    body = token.removeprefix(_TOKEN_PREFIX)
    values: dict[str, str] = {}
    for pair in body.split("|"):
        key, separator, value = pair.partition(":")
        if not separator or not key or not value.isdigit():
            raise ActionRejectedError(f"revert token {token!r} is not a readable runtime record")
        values[key] = value
    if not values:
        raise ActionRejectedError(f"revert token {token!r} records nothing to restore")
    return values
