"""The interface every actuator implements, and the one place a plan is built.

Five methods, and each one answers a different question:

* ``plan``    - what concrete effect would carry this decision out?
* ``simulate``- what would change if we did it? Touches nothing, ever.
* ``apply``   - put the effect in place.
* ``verify``  - is the effect actually in place? A separate, stronger claim than
                "we sent the request and got a 200".
* ``revert``  - put the target back the way it was.

``build_plan`` is not a convenience. It is the seam between the decision plane
and production, and it is deliberately the *only* way a plan comes into
existence, because three safety-critical facts must be carried across it
unchanged rather than chosen by an adapter:

* **A plan may only be built from a decision that decided to act.** Suppressing,
  alerting or escalating produces no plan at all, and asking for one is refused
  rather than quietly indulged.
* **The target service is the decision's**, which the decision contract already
  guarantees was computed from evidence by the causal collapse. An adapter never
  picks who to aim at.
* **The approval requirement is the policy gate's**, widened by the destructive
  rungs and by irreversibility, never narrowed. An adapter cannot decide that
  its own action needs nobody.

The idempotency key is likewise derived in exactly one place. Two derivations of
the same key drift, and a key that drifts is not an idempotency key.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime
from typing import ClassVar, Literal

from contracts import (
    ACTING_ACTIONS,
    DESTRUCTIVE_ACTIONS,
    ActionKind,
    ActionOutcome,
    ActionParameterValue,
    ActionPlan,
    ActuatorKind,
    Decision,
    action_idempotency_key,
)


class ActuatorError(RuntimeError):
    """An actuator could not do what it was asked."""


class ActionRejectedError(ActuatorError):
    """The request is not something this plane will carry out at all."""


class ActuatorContractError(ActuatorError):
    """An adapter returned an outcome that breaks the interface's own guarantees."""


def build_plan(
    decision: Decision,
    *,
    actuator: ActuatorKind,
    action_kind: ActionKind,
    target_ref: str,
    parameters: Mapping[str, ActionParameterValue] | None = None,
    reason: str,
    expected_effect: str,
    reversible: bool,
    estimated_blast_fraction: float,
    honesty: Literal["REAL", "SIMULATED"],
    ts: datetime,
) -> ActionPlan:
    """Turn a decision that decided to act into one concrete, keyed effect."""
    if decision.action not in ACTING_ACTIONS:
        raise ActionRejectedError(
            f"{decision.decision_id} decided to {decision.action.value}, which touches nothing; "
            "an action plan may only be built from a decision that decided to act"
        )
    if decision.target_service is None:  # pragma: no cover - the contract forbids it
        raise ActionRejectedError(
            f"{decision.decision_id} names no target service, so there is nothing to aim at"
        )
    values = dict(parameters or {})
    key = action_idempotency_key(
        actuator=actuator,
        action_kind=action_kind,
        target_ref=target_ref,
        parameters=values,
    )
    # Approval is only ever widened here. The gate's requirement stands, and the
    # rungs that are destructive or that we cannot undo add their own.
    approval = (
        decision.requires_human_approval or action_kind in DESTRUCTIVE_ACTIONS or not reversible
    )
    return ActionPlan(
        plan_id=_plan_id(decision.decision_id, key),
        ts=ts,
        decision_id=decision.decision_id,
        incident_id=decision.incident_id,
        actuator=actuator,
        action_kind=action_kind,
        target_service=decision.target_service,
        target_ref=target_ref,
        parameters=values,
        reason=reason,
        expected_effect=expected_effect,
        reversible=reversible,
        requires_human_approval=approval,
        estimated_blast_fraction=estimated_blast_fraction,
        idempotency_key=key,
        honesty=honesty,
    )


def rebuild_plan(
    plan: ActionPlan,
    *,
    parameters: Mapping[str, ActionParameterValue],
    estimated_blast_fraction: float,
    ts: datetime,
) -> ActionPlan:
    """One plan re-dialled to different parameters, for a canary's own steps.

    Deliberately derived from a plan rather than from a decision. The three
    safety-critical facts ``build_plan`` exists to carry - the decision that
    authorised acting, the evidence-computed target, and the approval
    requirement - already crossed that seam once when this plan was built.
    Synthesising a decision here to walk them across a second time would put
    them back in the hands of the caller, which is exactly what the seam
    prevents. So they are copied, and only what genuinely differs between two
    shares of one effect is recomputed.
    """
    values = dict(parameters)
    key = action_idempotency_key(
        actuator=plan.actuator,
        action_kind=plan.action_kind,
        target_ref=plan.target_ref,
        parameters=values,
    )
    return plan.model_copy(
        update={
            "plan_id": _plan_id(plan.decision_id, key),
            "ts": ts,
            "parameters": values,
            "estimated_blast_fraction": estimated_blast_fraction,
            "idempotency_key": key,
        }
    )


def _plan_id(decision_id: str, idempotency_key: str) -> str:
    """A stable id for one decision's attempt at one effect.

    The effect key alone would collide across decisions that want the same
    state, and the decision alone would collide across the rungs of one ladder;
    together they name this attempt and replay to the same value every time.
    """
    digest = hashlib.sha256(f"{decision_id}|{idempotency_key}".encode()).hexdigest()
    return f"plan-{digest[:24]}"


class Actuator(ABC):
    """One system the platform can change, and put back.

    Adapters are stateless with respect to the platform: everything about what
    has been done lives in the journal and the audit ledger, so an actuator may
    be reconstructed at any time without losing track of an outstanding effect.
    """

    kind: ClassVar[ActuatorKind]
    honesty: ClassVar[Literal["REAL", "SIMULATED"]]

    @abstractmethod
    def plan(
        self,
        decision: Decision,
        *,
        action_kind: ActionKind,
        parameters: Mapping[str, ActionParameterValue] | None = None,
        ts: datetime,
    ) -> ActionPlan:
        """Resolve a decision and a chosen rung into one concrete effect."""

    def plan_share(
        self,
        plan: ActionPlan,
        *,
        parameter: str,
        share: int,
        ts: datetime,
    ) -> ActionPlan:
        """The same effect as ``plan``, dialled to a smaller share of itself.

        A canary needs the effect it is widening at each intermediate share, and
        it needs them **without re-resolving the target**: the plan already
        names the exact pod or workload the decision was aimed at, and looking
        it up again would let a canary walk from one target onto another
        between steps.

        This is not a base-class convenience, which is why it refuses by
        default. How much of the world a share disturbs is a measurement only
        the adapter can make, and an adapter that has not stated it must not
        have one guessed on its behalf.
        """
        del plan, parameter, share, ts
        raise ActionRejectedError(
            f"the {self.kind.value} adapter states no share of its own effect, so it cannot be "
            "canaried; a rung that widens needs an adapter that can measure each step"
        )

    @abstractmethod
    def simulate(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Report what applying this plan would change, without changing it."""

    @abstractmethod
    def apply(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Put the effect in place. Called only through the executor."""

    @abstractmethod
    def verify(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Check the target itself for the effect the plan said to expect."""

    @abstractmethod
    def revert(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        revert_token: str | None = None,
    ) -> ActionOutcome:
        """Put the target back the way it was before this plan was applied.

        ``revert_token`` is whatever this adapter returned from its own ``apply``
        and is **opaque to everything else** - the executor carries it from the
        journal back to the adapter that minted it, and nothing in between
        interprets it.

        It exists because the state to undo *to* cannot live in the plan.
        ``parameters`` is hashed into the idempotency key, so recording "was 3
        replicas" there would give two plans that both scale to 6 different
        keys depending on where they started - and "the same effect is one
        effect" is the property the whole idempotency design rests on. The
        starting state is a fact about one *application*, not about the effect,
        so it travels with the outcome instead.
        """
