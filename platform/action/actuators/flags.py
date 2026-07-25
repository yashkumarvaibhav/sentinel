"""The feature-flag actuator: put a flag back to safe, and ask the provider it took.

This is the rung that answers a **self-inflicted** incident. When the change
agent's evidence is a flag flip and the verdict is an operational fault the
platform caused, the remedy is not to scale the victim - it is to undo the
change. That makes this adapter the one whose effect can be proven end to end
against a real induced fault, because in this testbed the same flags are the
fault injectors: ``paymentFailure: on`` is how the cascade scenario breaks
payment in the first place.

**Which is exactly why this adapter may only ever turn a fault OFF.** The single
variant it is permitted to write is committed per flag in ``config/action.yml``,
never passed in. An actuator that could set any variant could cause the incident
it was dispatched to fix, and no amount of policy above it would make that safe.
The plan still carries the variant so the audit trail is self-describing, but it
is checked against the committed table rather than trusted.

**How a flag actually changes.** The demo's flagd reads its document from an
``emptyDir`` that an init container copies the ``flagd-config`` ConfigMap into at
pod start - so patching the ConfigMap alone changes nothing until flagd is
rolled. That is the mechanism the scenario runner already uses, and it is the
reason ``FLAG_FLIP`` claims a blast fraction of 1.0: putting one service's flag
back restarts the flag provider *every* service evaluates against.

**Verification asks flagd, not the ConfigMap.** A ConfigMap that says ``off``
proves only that we wrote a file; flagd's OFREP endpoint reports the variant it
is actually serving, which is the claim worth making. It is reached through the
same pod-proxy transport the mesh actuator uses, so nothing is published and
nothing has to exist inside the flagd image.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

from action.actuators.base import ActionRejectedError, Actuator, ActuatorError, build_plan
from action.actuators.kubernetes import ClusterCommand, KubectlCommand
from action.config import FlagsConfig
from contracts import (
    ActionKind,
    ActionOutcome,
    ActionParameterValue,
    ActionPlan,
    ActionStatus,
    ActuatorKind,
    Decision,
)

SUPPORTED_RUNGS: frozenset[ActionKind] = frozenset({ActionKind.FLAG_FLIP})

_EXPECTED_EFFECT = "the flag provider serves the flag's safe variant"
_TOKEN_PREFIX = "variant="


class FlagProviderError(ActuatorError):
    """The flag document or the flag provider could not be read."""


@dataclass(slots=True)
class FlagActuator(Actuator):
    """Set one committed flag to its committed safe variant, reversibly."""

    kind: ClassVar[ActuatorKind] = ActuatorKind.FEATURE_FLAG
    honesty: ClassVar[Literal["REAL", "SIMULATED"]] = "REAL"

    configuration: FlagsConfig
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
        """Resolve a decision into one committed flag returning to its safe variant."""
        if action_kind not in SUPPORTED_RUNGS:
            raise ActionRejectedError(
                f"the feature-flag actuator does not carry out {action_kind.value}; it does "
                f"{ActionKind.FLAG_FLIP.value}"
            )
        if decision.target_service is None:
            raise ActionRejectedError(
                f"{decision.decision_id} names no target service, so there is nothing to aim at"
            )
        flag, variant = self._checked_parameters(decision.target_service, parameters)
        return build_plan(
            decision,
            actuator=self.kind,
            action_kind=action_kind,
            target_ref=self._target_ref(flag),
            parameters={"flag": flag, "variant": variant},
            reason=decision.reason,
            expected_effect=_EXPECTED_EFFECT,
            reversible=True,
            # The flag document is only re-read when flagd starts, so putting one
            # service's flag back restarts the provider EVERY service evaluates
            # against. The rung's blast radius is the mechanism's, not the flag's,
            # and understating it would hide that from the 5.6 guard.
            estimated_blast_fraction=1.0,
            honesty=self.honesty,
            ts=ts,
        )

    def _checked_parameters(
        self,
        target_service: str,
        parameters: Mapping[str, ActionParameterValue] | None,
    ) -> tuple[str, str]:
        values = dict(parameters or {})
        if set(values) not in ({"flag"}, {"flag", "variant"}):
            got = ", ".join(sorted(values)) or "nothing"
            raise ActionRejectedError(f"FLAG_FLIP takes 'flag' and optionally 'variant'; got {got}")
        flag = values["flag"]
        remediation = self.configuration.remediations.get(flag) if isinstance(flag, str) else None
        if not isinstance(flag, str) or remediation is None:
            known = ", ".join(sorted(self.configuration.remediations)) or "none"
            raise ActionRejectedError(
                f"no remediation is committed for flag {flag!r}; a flip is refused rather than "
                f"aimed at a guess (committed flags: {known})"
            )
        if remediation.service != target_service:
            offered = ", ".join(self.configuration.flags_for(target_service)) or "none"
            raise ActionRejectedError(
                f"flag {flag!r} changes {remediation.service}, but the decision named "
                f"{target_service}; flags committed for it: {offered}"
            )
        # The variant is the committed one whether or not the caller stated it. A
        # caller naming a different one is asking for something this adapter is
        # not permitted to do, and is told so rather than quietly corrected.
        stated = values.get("variant", remediation.variant)
        if stated != remediation.variant:
            raise ActionRejectedError(
                f"{flag}={stated!r} is not the committed remediation; this actuator may only set "
                f"{flag}={remediation.variant!r}, because a flag it could set freely could cause "
                "the incident it was dispatched to fix"
            )
        return flag, remediation.variant

    def _target_ref(self, flag: str) -> str:
        """The ConfigMap, not the pod: a flag flip outlives the process serving it."""
        configuration = self.configuration
        return f"{configuration.namespace}/configmap/{configuration.config_map}/flag/{flag}"

    # --- carrying out -------------------------------------------------------

    def simulate(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Report what the flip would change, having read it and changed nothing."""
        flag, wanted = self._plan_flag(plan)
        document = self._document()
        current = self._current_variant(document, flag)
        served = self._served_variant(flag)
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.SIMULATED,
            detail=f"would set {flag} to {wanted} in {plan.target_ref}",
            observed=(
                f"{flag}: the document says {current}, the provider serves {served}",
                f"applying it restarts {self.configuration.workload}",
            ),
        )

    def apply(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Patch the flag document and roll the provider onto it."""
        flag, wanted = self._plan_flag(plan)
        document = self._document()
        previous = self._current_variant(document, flag)
        if previous == wanted:
            # Nothing to write, and a restart nobody needs is a mesh-wide
            # disturbance for no effect. The effect this plan describes is
            # already in place, which is the honest thing to report.
            return self._outcome(
                plan,
                ts=ts,
                status=ActionStatus.APPLIED,
                detail=f"{flag} is already {wanted}; the provider was not restarted",
                revert_token=f"{_TOKEN_PREFIX}{previous}",
            )
        self._write(document, flag, wanted)
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.APPLIED,
            detail=f"{flag} set to {wanted} and {self.configuration.workload} rolled onto it",
            revert_token=f"{_TOKEN_PREFIX}{previous}",
        )

    def verify(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Ask the provider what it is serving; a written file is not a served flag."""
        flag, wanted = self._plan_flag(plan)
        served = self._served_variant(flag)
        present = served == wanted
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.VERIFIED if present else ActionStatus.FAILED,
            detail=f"the provider serves {flag}={served}, wanted {wanted}",
            observed=(plan.expected_effect,) if present else (),
        )

    def revert(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        revert_token: str | None = None,
    ) -> ActionOutcome:
        """Put the flag back to the variant it carried before this plan."""
        flag, wanted = self._plan_flag(plan)
        if revert_token is None:
            raise ActionRejectedError(
                f"{plan.target_ref} cannot be put back without the token recorded when it was "
                "applied; reverting to a guessed variant is worse than not reverting"
            )
        if not revert_token.startswith(_TOKEN_PREFIX):
            raise ActionRejectedError(
                f"revert token {revert_token!r} does not record a variant; it was minted for "
                "another rung"
            )
        previous = revert_token.removeprefix(_TOKEN_PREFIX)
        document = self._document()
        if previous not in self._variants(document, flag):
            raise ActionRejectedError(
                f"{flag} has no variant {previous!r} any more; the document changed underneath "
                "this action and restoring a variant that no longer exists is not a revert"
            )
        if self._current_variant(document, flag) == previous:
            return self._outcome(
                plan,
                ts=ts,
                status=ActionStatus.REVERTED,
                detail=f"{flag} is already back at {previous}",
            )
        self._write(document, flag, previous)
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.REVERTED,
            detail=f"{flag} restored from {wanted} to {previous}",
        )

    # --- the flag document and the provider ---------------------------------

    def _document(self) -> dict[str, Any]:
        """The committed flag document, as the ConfigMap currently holds it."""
        configuration = self.configuration
        raw = self._run(
            [
                "-n",
                configuration.namespace,
                "get",
                "configmap",
                configuration.config_map,
                "-o",
                f"jsonpath={{.data.{configuration.document_key.replace('.', chr(92) + '.')}}}",
            ]
        )
        if not raw.strip():
            raise FlagProviderError(
                f"{configuration.config_map} has no {configuration.document_key}; there is no "
                "flag document to change"
            )
        try:
            document = json.loads(raw)
        except ValueError as error:
            raise FlagProviderError(f"the flag document is not readable JSON: {error}") from error
        if not isinstance(document, dict) or not isinstance(document.get("flags"), dict):
            raise FlagProviderError("the flag document has no flags")
        return document

    def _variants(self, document: Mapping[str, Any], flag: str) -> Mapping[str, Any]:
        definition = document["flags"].get(flag)
        if not isinstance(definition, dict):
            raise ActionRejectedError(
                f"the flag document has no flag {flag!r}; a flip is refused rather than invented"
            )
        variants = definition.get("variants")
        if not isinstance(variants, dict) or not variants:
            raise FlagProviderError(f"flag {flag!r} declares no variants")
        return variants

    def _current_variant(self, document: Mapping[str, Any], flag: str) -> str:
        self._variants(document, flag)
        current = document["flags"][flag].get("defaultVariant")
        if not isinstance(current, str):
            raise FlagProviderError(f"flag {flag!r} has no default variant to change")
        return current

    def _write(self, document: Mapping[str, Any], flag: str, variant: str) -> None:
        """Patch the document and roll the provider, which only reads it at start."""
        if variant not in self._variants(document, flag):
            raise ActionRejectedError(f"flag {flag!r} has no variant {variant!r}")
        updated = json.loads(json.dumps(document))
        updated["flags"][flag]["defaultVariant"] = variant
        rendered = json.dumps(updated, allow_nan=False, indent=2, sort_keys=True) + "\n"
        configuration = self.configuration
        patch = json.dumps(
            {"data": {configuration.document_key: rendered}},
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._run(
            [
                "-n",
                configuration.namespace,
                "patch",
                "configmap",
                configuration.config_map,
                "--type",
                "merge",
                "-p",
                patch,
            ]
        )
        workload = f"deployment/{configuration.workload}"
        self._run(["-n", configuration.namespace, "rollout", "restart", workload])
        self._run(
            [
                "-n",
                configuration.namespace,
                "rollout",
                "status",
                workload,
                f"--timeout={configuration.rollout_timeout_seconds}s",
            ],
            timeout=configuration.rollout_timeout_seconds + 30,
        )

    def _served_variant(self, flag: str) -> str:
        """What the provider itself reports serving, through the pod-proxy.

        OFREP answers a POST with an empty body, so this needs nothing inside the
        flagd image and publishes no port - the same transport the mesh actuator
        reaches Envoy's admin interface through.
        """
        configuration = self.configuration
        pod = self._provider_pod()
        target = f"{pod}:{configuration.ofrep_port}"
        raw = f"/api/v1/namespaces/{configuration.namespace}/pods/{target}/proxy"
        answer = self._run(
            ["create", "--raw", f"{raw}/ofrep/v1/evaluate/flags/{flag}", "-f", "/dev/null"]
        )
        try:
            evaluation = json.loads(answer)
        except ValueError as error:
            raise FlagProviderError(f"the provider did not answer with JSON: {error}") from error
        variant = evaluation.get("variant") if isinstance(evaluation, dict) else None
        if not isinstance(variant, str):
            detail = evaluation.get("errorDetails") if isinstance(evaluation, dict) else answer
            raise FlagProviderError(f"the provider served no variant for {flag}: {detail}")
        return variant

    def _provider_pod(self) -> str:
        """The one flagd process an evaluation is read from."""
        configuration = self.configuration
        selector = f"{configuration.component_label}={configuration.workload}"
        raw = self._run(
            [
                "-n",
                configuration.namespace,
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
            raise FlagProviderError(
                f"no running {configuration.workload} pod in {configuration.namespace}; there is "
                "no provider to ask what it is serving"
            )
        # Unlike a runtime override, the flag document is shared: every replica
        # reads the same ConfigMap, so asking any one of them is asking all.
        return pods[0]

    # --- helpers ------------------------------------------------------------

    def _plan_flag(self, plan: ActionPlan) -> tuple[str, str]:
        """Recover the flag from the plan, refusing one this adapter may not set."""
        flag = plan.parameters.get("flag")
        remediation = self.configuration.remediations.get(flag) if isinstance(flag, str) else None
        if not isinstance(flag, str) or remediation is None:
            raise ActionRejectedError(
                f"{plan.plan_id} names no committed flag; a flip is refused rather than aimed at "
                "whatever a plan happened to name"
            )
        if plan.target_ref != self._target_ref(flag):
            raise ActionRejectedError(
                f"{plan.target_ref} is not the committed flag document for {flag}"
            )
        if plan.parameters.get("variant") != remediation.variant:
            raise ActionRejectedError(
                f"{plan.plan_id} would set {flag} to something other than its committed "
                f"remediation {remediation.variant!r}"
            )
        return flag, remediation.variant

    def _outcome(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        status: ActionStatus,
        detail: str,
        observed: Sequence[str] = (),
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
            observed=tuple(observed),
            revert_token=revert_token,
            honesty=self.honesty,
        )
