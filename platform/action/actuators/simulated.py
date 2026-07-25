"""An actuator that changes nothing and says so.

This is not only a test double. The platform has to be demonstrable end to end
with no cluster attached, and a run that produces a full audit trail of what
*would* have happened is a real product capability - provided every artifact it
leaves behind is labelled ``SIMULATED``, which is exactly the honesty rule this
project applies to every synthetic element.

It records the calls it received, so a test can assert the thing that actually
matters about the executor: how many times the world was touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Literal

from action.actuators.base import ActionRejectedError, Actuator, build_plan
from contracts import (
    ActionKind,
    ActionOutcome,
    ActionParameterValue,
    ActionPlan,
    ActionStatus,
    ActuatorKind,
    Decision,
)

type ActuatorMethod = Literal["simulate", "apply", "verify", "revert"]

# What one rung is understood to change, and what should therefore be observable
# afterwards. Stated per rung so the simulated trail reads like the real one.
_EFFECTS: dict[ActionKind, str] = {
    ActionKind.OBSERVE: "nothing changes; the target is watched for another interval",
    ActionKind.RATE_LIMIT: "the target's ingress rate settles at the configured limit",
    ActionKind.THROTTLE: "the target's concurrency settles at the configured ceiling",
    ActionKind.SCALE: "the target reports the configured number of ready replicas",
    ActionKind.FLAG_FLIP: "the target serves the configured flag value",
    ActionKind.RESTART: "the target's processes report a start time after this action",
    ActionKind.ISOLATE: "the target receives no traffic from its dependents",
    ActionKind.ROLLBACK: "the target reports the previous revision",
}

# Rungs whose effect this adapter cannot put back. Kept explicit rather than
# assumed: the plan contract makes an irreversible rung require approval, so
# getting this wrong is expensive in the right direction.
_IRREVERSIBLE: frozenset[ActionKind] = frozenset({ActionKind.RESTART})


@dataclass(frozen=True, slots=True)
class SimulatedCall:
    """One call this adapter received, in the order it received it."""

    method: ActuatorMethod
    plan_id: str
    idempotency_key: str
    ts: datetime


@dataclass(slots=True)
class SimulatedActuator(Actuator):
    """An adapter that carries every action out against nothing at all."""

    kind: ClassVar[ActuatorKind] = ActuatorKind.SIMULATED
    honesty: ClassVar[Literal["REAL", "SIMULATED"]] = "SIMULATED"

    blast_fraction: float = 0.05
    fail_on_apply: bool = False
    effect_present_after_apply: bool = True
    calls: list[SimulatedCall] = field(default_factory=list)
    # The simulated world. `verify` reads this rather than answering yes by
    # default, so the adapter models "is the effect actually there?" instead of
    # assuming it - which is the only version of it worth testing an executor
    # against.
    in_place: set[str] = field(default_factory=set)

    def plan(
        self,
        decision: Decision,
        *,
        action_kind: ActionKind,
        parameters: Mapping[str, ActionParameterValue] | None = None,
        ts: datetime,
    ) -> ActionPlan:
        """Build a plan against a target named for the decision's own origin."""
        if decision.target_service is None:
            raise ActionRejectedError(
                f"{decision.decision_id} names no target service, so there is nothing to aim at"
            )
        reversible = action_kind not in _IRREVERSIBLE
        blast = 0.0 if action_kind is ActionKind.OBSERVE else self.blast_fraction
        return build_plan(
            decision,
            actuator=self.kind,
            action_kind=action_kind,
            target_ref=f"simulated/{decision.target_service}",
            parameters=parameters,
            reason=decision.reason,
            expected_effect=_EFFECTS[action_kind],
            reversible=reversible,
            estimated_blast_fraction=blast,
            honesty=self.honesty,
            ts=ts,
        )

    def simulate(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Describe the change without making it."""
        self._record("simulate", plan, ts)
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.SIMULATED,
            detail=f"would {plan.action_kind.value.lower()} {plan.target_ref}",
            observed=(plan.expected_effect,),
        )

    def apply(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Record an application, or fail on request so the executor can be tested."""
        self._record("apply", plan, ts)
        if self.fail_on_apply:
            return self._outcome(
                plan,
                ts=ts,
                status=ActionStatus.FAILED,
                detail=f"simulated failure applying {plan.action_kind.value} to {plan.target_ref}",
            )
        self.in_place.add(plan.idempotency_key)
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.APPLIED,
            detail=f"{plan.action_kind.value.lower()} in place on {plan.target_ref}",
            observed=(plan.expected_effect,),
            revert_token=f"revert-{plan.idempotency_key[:16]}" if plan.reversible else None,
        )

    def verify(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Report whether the expected effect is present on the target."""
        self._record("verify", plan, ts)
        present = self.effect_present_after_apply and plan.idempotency_key in self.in_place
        if not present:
            return self._outcome(
                plan,
                ts=ts,
                status=ActionStatus.FAILED,
                detail=f"expected effect is absent on {plan.target_ref}",
            )
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.VERIFIED,
            detail=f"observed on {plan.target_ref}: {plan.expected_effect}",
            observed=(plan.expected_effect,),
        )

    def revert(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Put the simulated target back the way it was."""
        self._record("revert", plan, ts)
        if not plan.reversible:
            raise ActionRejectedError(
                f"{plan.action_kind.value} on {plan.target_ref} cannot be undone"
            )
        self.in_place.discard(plan.idempotency_key)
        return self._outcome(
            plan,
            ts=ts,
            status=ActionStatus.REVERTED,
            detail=f"{plan.target_ref} restored to its state before {plan.plan_id}",
        )

    def call_count(self, method: ActuatorMethod) -> int:
        """How many times one method was actually reached."""
        return sum(1 for call in self.calls if call.method == method)

    def _record(self, method: ActuatorMethod, plan: ActionPlan, ts: datetime) -> None:
        self.calls.append(
            SimulatedCall(
                method=method,
                plan_id=plan.plan_id,
                idempotency_key=plan.idempotency_key,
                ts=ts,
            )
        )

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
            outcome_id=f"{plan.plan_id}-{status.value.lower()}-{len(self.calls)}",
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
