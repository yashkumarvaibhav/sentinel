"""Contracts for the action plane: what may be done, and what was actually done.

The decision plane already states *what should happen* about an incident. This
module is the boundary where that becomes something a real cluster feels, so it
is written to make the dangerous mistakes unrepresentable rather than merely
discouraged.

Three properties are enforced here rather than left to the executor:

* **A dry run can never claim to have changed anything.** ``dry_run`` and the
  ``SIMULATED`` status are one bit of truth, biconditionally: an outcome cannot
  be a dry run and report ``APPLIED``, and it cannot report ``SIMULATED`` while
  claiming the world was touched.
* **The idempotency key is derived from the effect, not from the request.** It
  is a content hash over the actuator, the rung, the concrete target and the
  parameters - and deliberately *not* over the decision, the incident or the
  timestamp. Two decisions that would rate-limit the same target the same way
  are the same effect, so the second one is a no-op rather than a second
  rate-limit. This is what makes retries, reconnects and duplicate decisions
  safe. The contract recomputes the key and rejects a plan whose key disagrees
  with its own fields, so there is exactly one derivation and no way around it.
* **A destructive or irreversible rung requires a human.** ``ISOLATE`` and
  ``ROLLBACK`` touch production in ways that are expensive to undo, so they
  require approval regardless of which service they aim at; and anything the
  platform cannot itself reverse is not something the platform does alone.

One consequence of keying on the effect is a rule every actuator must honour:
**plan parameters are absolute, never relative.** ``replicas: 6`` is an effect
and can be deduplicated; ``replicas_delta: +2`` is a request whose meaning
depends on when it runs, and applying it twice is exactly the accident the key
exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from contracts._base import (
    ContractModel,
    HumanText,
    Identifier,
    Probability,
    UtcDatetime,
    ensure_unique,
)

type ActionParameterValue = str | bool | int | float


class ActuatorKind(StrEnum):
    """Which system an action is carried out against.

    ``SIMULATED`` is a first-class member rather than a test fixture: the
    platform must be demonstrable end to end with no cluster attached, and an
    adapter that touches nothing has to say so in the audit trail like any
    other.
    """

    KUBERNETES = "KUBERNETES"
    MESH = "MESH"
    FEATURE_FLAG = "FEATURE_FLAG"
    SIMULATED = "SIMULATED"


class ActionKind(StrEnum):
    """The rungs of the remediation ladder, cheapest and safest first.

    The ordering of the rungs into ladders, and which rung a given diagnosis
    earns, is operator-owned configuration - this enum is only the vocabulary
    those ladders are written in.
    """

    OBSERVE = "OBSERVE"
    RATE_LIMIT = "RATE_LIMIT"
    THROTTLE = "THROTTLE"
    SCALE = "SCALE"
    FLAG_FLIP = "FLAG_FLIP"
    RESTART = "RESTART"
    ISOLATE = "ISOLATE"
    ROLLBACK = "ROLLBACK"


# The rungs whose blast radius is large and whose undo is expensive. These
# require human approval regardless of which service they target, which is why
# the policy gate's `approval.service_criticalities` can ship empty: protection
# belongs to the rung, not to the label on the target.
DESTRUCTIVE_ACTIONS: frozenset[ActionKind] = frozenset({ActionKind.ISOLATE, ActionKind.ROLLBACK})


class ActionStatus(StrEnum):
    """How far one action got, and whether the world was touched.

    ``SIMULATED`` means nothing was touched - either the caller asked for a
    simulation or the executor is in dry-run mode. ``APPLIED`` means the effect
    was put in place; ``VERIFIED`` means it was afterwards observed to actually
    be in place, which is a different and stronger claim. ``FAILED`` is the
    honest answer when an actuator could not finish, and it deliberately does
    not assert whether the world changed - only a ``verify`` can settle that.
    """

    SIMULATED = "SIMULATED"
    APPLIED = "APPLIED"
    VERIFIED = "VERIFIED"
    REVERTED = "REVERTED"
    FAILED = "FAILED"


# The statuses under which an effect is believed to be in place. The journal
# treats exactly these as "already done": an apply against a key in one of them
# is deduplicated, and a revert against a key in none of them has nothing to
# undo.
EFFECTIVE_STATUSES: frozenset[ActionStatus] = frozenset(
    {ActionStatus.APPLIED, ActionStatus.VERIFIED}
)

# The statuses a deduplicated outcome may carry: the ones where the world has
# settled into a known state. A simulation is cheap and is always run for real,
# and a failure is ambiguous about what actually happened - neither is ever a
# reason to skip a call. A deduplicated outcome reports the status the journal
# is holding, so an effect confirmed by a verification says VERIFIED rather than
# quietly weakening itself back to APPLIED.
DEDUPLICABLE_STATUSES: frozenset[ActionStatus] = frozenset(
    {ActionStatus.APPLIED, ActionStatus.VERIFIED, ActionStatus.REVERTED}
)


def _canonical_parameter(value: ActionParameterValue) -> ActionParameterValue:
    """Fold values that print differently but mean the same thing."""
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    # -0.0 == 0.0 but they serialise differently, and a key that depends on the
    # sign of a zero would let the same effect hash two ways.
    return 0.0 if value == 0.0 else value


def action_idempotency_key(
    *,
    actuator: ActuatorKind,
    action_kind: ActionKind,
    target_ref: str,
    parameters: dict[str, ActionParameterValue],
) -> str:
    """Derive the content hash that identifies one effect on the world.

    Deliberately independent of the decision, the incident and the clock: this
    names *what would be true afterwards*, so two callers asking for the same
    state are asking for one thing.
    """
    canonical = json.dumps(
        {
            "actuator": actuator.value,
            "action_kind": action_kind.value,
            "target_ref": target_ref,
            "parameters": {key: _canonical_parameter(value) for key, value in parameters.items()},
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ActionPlan(ContractModel):
    """One concrete, reversible effect an actuator proposes to put in place.

    A plan is built from a decision that already decided to act, and it carries
    the safety-critical facts forward unchanged: the target service is the
    origin the causal collapse computed from evidence, and the approval
    requirement is the policy gate's, never the adapter's opinion.

    ``expected_effect`` is required because an action nobody can check is not
    something this platform takes: it states, in advance, what ``verify`` should
    find once the effect is in place.
    """

    plan_id: Identifier
    ts: UtcDatetime
    decision_id: Identifier
    incident_id: Identifier
    actuator: ActuatorKind
    action_kind: ActionKind
    target_service: Identifier
    target_ref: Identifier
    parameters: dict[Identifier, ActionParameterValue] = Field(default_factory=dict)
    reason: HumanText
    expected_effect: HumanText
    reversible: bool
    requires_human_approval: bool
    estimated_blast_fraction: Probability
    idempotency_key: Identifier
    honesty: Literal["REAL", "SIMULATED"]

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        """Recompute the key and refuse the rungs that may not run unattended."""
        expected = action_idempotency_key(
            actuator=self.actuator,
            action_kind=self.action_kind,
            target_ref=self.target_ref,
            parameters=self.parameters,
        )
        if self.idempotency_key != expected:
            raise ValueError(
                "idempotency_key must be the content hash of the effect this plan describes; "
                "a key that can disagree with its own plan is not an idempotency key"
            )
        if self.action_kind in DESTRUCTIVE_ACTIONS and not self.requires_human_approval:
            raise ValueError(
                f"{self.action_kind.value} is destructive and requires human approval "
                "regardless of which service it targets"
            )
        if not self.reversible and not self.requires_human_approval:
            raise ValueError(
                "an effect the platform cannot itself undo is not one it takes alone; "
                "an irreversible plan requires human approval"
            )
        if self.action_kind is ActionKind.OBSERVE and self.estimated_blast_fraction != 0.0:
            raise ValueError("observing changes nothing, so it cannot have a blast radius")
        return self


class ActionOutcome(ContractModel):
    """What one actuator call actually did, recorded whether it worked or not.

    ``dry_run`` and ``SIMULATED`` are the same fact stated twice, and the
    contract keeps them in agreement: this is the single place that makes "we
    were only pretending" impossible to confuse with "we changed production".

    ``deduplicated`` marks an outcome that was returned *without* calling the
    actuator, because the effect this key names was already in place. It is not
    a failure and not a no-op error - it is the idempotency key doing its job.
    """

    outcome_id: Identifier
    ts: UtcDatetime
    plan_id: Identifier
    idempotency_key: Identifier
    status: ActionStatus
    dry_run: bool
    detail: HumanText
    deduplicated: bool = False
    observed: tuple[HumanText, ...] = ()
    approvals: tuple[Identifier, ...] = ()
    revert_token: Identifier | None = None
    honesty: Literal["REAL", "SIMULATED"]

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """Keep an untouched world and a changed one impossible to confuse."""
        ensure_unique(self.approvals, field_name="approvals")
        simulated = self.status is ActionStatus.SIMULATED
        if simulated != self.dry_run:
            raise ValueError(
                "dry_run and the SIMULATED status are the same fact: an outcome that touched "
                "nothing must report both, and one that touched production may report neither"
            )
        if self.dry_run and self.revert_token is not None:
            raise ValueError("a dry run changed nothing, so there is nothing to revert")
        if self.deduplicated and self.status not in DEDUPLICABLE_STATUSES:
            raise ValueError(
                f"{self.status.value} cannot be deduplicated; only an effect that has settled "
                "into a known state short-circuits a call"
            )
        return self

    @property
    def in_force(self) -> bool:
        """Whether this outcome leaves the effect in place on the target."""
        return self.status in EFFECTIVE_STATUSES
