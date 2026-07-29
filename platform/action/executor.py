"""The only thing allowed to call an actuator, and the reason that matters.

Every safety property of this plane is enforced here rather than asked of the
adapters, because an adapter that forgets one is an outage and there will
eventually be adapters this repository did not write:

* **Dry-run cannot be bypassed.** In dry-run mode an ``apply`` or ``revert`` is
  routed to the adapter's own ``simulate``, and if the adapter returns anything
  other than a ``SIMULATED`` outcome the call is rejected as a contract breach.
  The adapter is never even told which mode it is in, so it cannot special-case
  it.
* **An effect already in place is not applied again.** The journal is consulted
  before the adapter, and a deduplicated call returns without touching anything.
* **One actor at a time per target.** The lease is taken around the adapter call
  and fails fast, so a rate-limit cannot land halfway through a rollback.
* **Approval is required before it is useful.** A plan the policy gate marked as
  needing a human is refused without approvers, and a destructive rung needs two
  distinct ones - the two-key rule, applied regardless of which service is
  targeted.
* **An adapter cannot report against a plan it was not given.** Outcomes are
  checked to reference this plan and this key, so a buggy adapter cannot record
  an effect against somebody else's key and make the journal lie.
* **Everything that happens here is written down, including the refusals.** The
  executor is the one place every action passes through, which makes it the one
  place an audit trail can be complete rather than well-intentioned. A refused
  action is appended before the exception is re-raised: "we declined to do this,
  and why" is exactly the record somebody will want later, and it is the one a
  caller that swallows the exception would otherwise erase.

Time is passed in. Nothing here reads a wall clock, so a capture replay executes
and expires leases exactly as a live run does.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Literal

from action.actuators.base import ActionRejectedError, Actuator, ActuatorContractError
from action.config import ActionConfig
from action.journal import ActionJournal
from action.leases import LeaseRegistry
from audit import AuditSink
from contracts import (
    DESTRUCTIVE_ACTIONS,
    ActionOutcome,
    ActionPlan,
    ActionStatus,
    ActuatorKind,
    AuditEventKind,
)

# Which ledger entry each terminal status earns. A status with no entry here is
# one the ledger would silently omit, so the mapping is exhaustive over the
# statuses an executor can return rather than a lookup with a default.
_AUDITED_STATUS: dict[ActionStatus, AuditEventKind] = {
    ActionStatus.SIMULATED: AuditEventKind.ACTION_PLANNED,
    ActionStatus.APPLIED: AuditEventKind.ACTION_APPLIED,
    ActionStatus.VERIFIED: AuditEventKind.ACTION_VERIFIED,
    ActionStatus.REVERTED: AuditEventKind.ACTION_REVERTED,
    ActionStatus.FAILED: AuditEventKind.ACTION_REFUSED,
}

# A destructive rung needs two distinct people. One person with two accounts is
# not two keys, which is why approvals are required to be distinct identities.
TWO_KEY_APPROVERS = 2


class ActionExecutor:
    """Runs plans through their adapters under dry-run, idempotency and leases."""

    __slots__ = ("_actuators", "_dry_run", "_journal", "_leases", "_ledger")

    def __init__(
        self,
        *,
        actuators: Iterable[Actuator],
        configuration: ActionConfig,
        dry_run: bool,
        journal: ActionJournal | None = None,
        leases: LeaseRegistry | None = None,
        ledger: AuditSink | None = None,
    ) -> None:
        enabled = configuration.enabled_actuators()
        registered: dict[ActuatorKind, Actuator] = {}
        for actuator in actuators:
            if actuator.kind in registered:
                raise ValueError(f"{actuator.kind.value} is registered twice")
            if actuator.kind not in enabled:
                raise ValueError(
                    f"{actuator.kind.value} is not enabled in the action configuration; "
                    "landing an adapter and permitting it are two separate decisions"
                )
            registered[actuator.kind] = actuator
        self._actuators = registered
        self._dry_run = dry_run
        # Explicitly `is None`: an empty journal is falsy, and silently building
        # a second one would give the caller a journal nothing writes to.
        self._journal = (
            journal
            if journal is not None
            else ActionJournal(capacity=configuration.execution.journal_capacity)
        )
        self._leases = (
            leases
            if leases is not None
            else LeaseRegistry(ttl=timedelta(seconds=configuration.execution.lease_ttl_seconds))
        )
        # Optional, and `is None` for the same reason the journal is: an empty
        # chain is falsy, and `ledger or AuditChain()` would hand the caller a
        # ledger nothing writes to.
        self._ledger = ledger

    @property
    def dry_run(self) -> bool:
        """Whether this executor is forbidden from changing anything."""
        return self._dry_run

    @property
    def journal(self) -> ActionJournal:
        """The record of what is currently in place, keyed by effect."""
        return self._journal

    def simulate(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Ask the adapter what would change. Never records anything as in force."""
        actuator = self._actuator_for(plan)
        return self._checked(plan, actuator.simulate(plan, ts=ts), expect_simulated=True)

    def apply(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        owner: str,
        approvals: Sequence[str] = (),
    ) -> ActionOutcome:
        """Put the effect in place, exactly once, if everything permits it."""
        actuator = self._actuator_for(plan)
        with self._audited(plan, ts=ts, owner=owner):
            self._require_approval(plan, approvals)
            if self._dry_run:
                simulated = self._checked(
                    plan, actuator.simulate(plan, ts=ts), expect_simulated=True
                )
                return self._record(simulated, plan, ts=ts, owner=owner)
            already = self._journal.latest(plan.idempotency_key)
            if already is not None and already.in_force:
                duplicate = self._deduplicated(plan, already, ts=ts, approvals=approvals)
                return self._record(duplicate, plan, ts=ts, owner=owner)
            self._journal.ensure_room(plan.idempotency_key)
            with self._leases.hold(plan.target_ref, owner=owner, now=ts):
                outcome = self._checked(plan, actuator.apply(plan, ts=ts), expect_simulated=False)
            recorded = _with_approvals(outcome, approvals)
            self._journal.record(recorded)
            return self._record(recorded, plan, ts=ts, owner=owner)

    def verify(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        """Check the target for the effect the plan said to expect.

        A verification is a read, so it runs even in dry-run mode - finding out
        whether an effect is present changes nothing. Outside dry-run its result
        is recorded, because ``VERIFIED`` is the stronger claim about what is in
        place and a failed verification retracts a belief the journal held. In
        dry-run the journal stays empty, full stop: nothing was applied, so
        nothing may be remembered as being in force.
        """
        actuator = self._actuator_for(plan)
        outcome = self._checked(plan, actuator.verify(plan, ts=ts), expect_simulated=False)
        prior = self._journal.latest(plan.idempotency_key)
        if outcome.status is ActionStatus.VERIFIED and prior is not None and prior.in_force:
            outcome = ActionOutcome.model_validate(
                {
                    **outcome.model_dump(),
                    "approvals": prior.approvals,
                    "gates_passed": prior.gates_passed,
                    "revert_token": prior.revert_token,
                }
            )
        if not self._dry_run:
            self._journal.record(outcome)
        return self._record(outcome, plan, ts=ts, owner="verifier")

    def revert(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        owner: str,
        approvals: Sequence[str] = (),
    ) -> ActionOutcome:
        """Put the target back. Reverting what is not in place is refused.

        Deliberately not gated on approval. Approval protects production from
        being *changed*; undoing our own change is the safe direction, and a
        rollback that has to wait for a signature is how collateral becomes an
        outage. Approvers are still recorded when a person did ask for it.
        """
        actuator = self._actuator_for(plan)
        with self._audited(plan, ts=ts, owner=owner):
            if self._dry_run:
                simulated = self._checked(
                    plan, actuator.simulate(plan, ts=ts), expect_simulated=True
                )
                return self._record(simulated, plan, ts=ts, owner=owner)
            already = self._journal.latest(plan.idempotency_key)
            if already is None:
                raise ActionRejectedError(
                    f"nothing is recorded under {plan.idempotency_key[:16]}, so there is no "
                    "effect to put back; a revert is taken against something that happened"
                )
            if already.status is ActionStatus.REVERTED:
                duplicate = self._deduplicated(plan, already, ts=ts, approvals=approvals)
                return self._record(duplicate, plan, ts=ts, owner=owner)
            # The token the adapter minted on its own apply, carried back to it
            # untouched. Nothing between the two ends interprets it.
            token = already.revert_token
            with self._leases.hold(plan.target_ref, owner=owner, now=ts):
                outcome = self._checked(
                    plan,
                    actuator.revert(plan, ts=ts, revert_token=token),
                    expect_simulated=False,
                )
            recorded = _with_approvals(outcome, approvals)
            self._journal.record(recorded)
            return self._record(recorded, plan, ts=ts, owner=owner)

    @contextmanager
    def _audited(self, plan: ActionPlan, *, ts: datetime, owner: str) -> Iterator[None]:
        """Write a refusal down before it is raised.

        A caller that catches the exception and moves on would otherwise leave no
        trace that the platform declined to do something - which is exactly the
        record somebody reconstructing an incident will look for.
        """
        try:
            yield
        except ActionRejectedError as refusal:
            self._append(
                ts=ts,
                kind=AuditEventKind.ACTION_REFUSED,
                actor=owner,
                summary=f"refused {plan.action_kind.value} on {plan.target_ref}: {refusal}",
                plan=plan,
                body={"reason": str(refusal), "actuator": plan.actuator.value},
            )
            raise

    def _record(
        self, outcome: ActionOutcome, plan: ActionPlan, *, ts: datetime, owner: str
    ) -> ActionOutcome:
        """Append what actually happened, and hand the outcome straight back."""
        self._append(
            ts=ts,
            kind=_AUDITED_STATUS[outcome.status],
            actor=owner,
            summary=f"{outcome.status.value.lower()} {plan.action_kind.value} on "
            f"{plan.target_ref}: {outcome.detail}",
            plan=plan,
            body={
                "actuator": plan.actuator.value,
                "deduplicated": outcome.deduplicated,
                "gates_passed": list(outcome.gates_passed),
                "idempotency_key": plan.idempotency_key,
                "outcome_id": outcome.outcome_id,
                "parameters": dict(plan.parameters),
                "status": outcome.status.value,
            },
            honesty=outcome.honesty,
        )
        return outcome

    def _append(
        self,
        *,
        ts: datetime,
        kind: AuditEventKind,
        actor: str,
        summary: str,
        plan: ActionPlan,
        body: dict[str, object],
        honesty: Literal["REAL", "SIMULATED"] = "REAL",
    ) -> None:
        if self._ledger is None:
            return
        self._ledger.append(
            ts=ts,
            kind=kind,
            actor=actor,
            summary=summary,
            body=body,
            incident_id=plan.incident_id,
            decision_id=plan.decision_id,
            plan_id=plan.plan_id,
            honesty=honesty,
        )

    def _actuator_for(self, plan: ActionPlan) -> Actuator:
        actuator = self._actuators.get(plan.actuator)
        if actuator is None:
            raise ActionRejectedError(
                f"no enabled actuator for {plan.actuator.value}; an unlisted adapter is refused"
            )
        return actuator

    def _require_approval(self, plan: ActionPlan, approvals: Sequence[str]) -> None:
        if not plan.requires_human_approval:
            return
        distinct = set(approvals)
        if len(distinct) != len(approvals):
            raise ActionRejectedError("an approver may sign an action only once")
        needed = TWO_KEY_APPROVERS if plan.action_kind in DESTRUCTIVE_ACTIONS else 1
        if len(distinct) < needed:
            raise ActionRejectedError(
                f"{plan.action_kind.value} on {plan.target_ref} needs {needed} approver(s); "
                f"{len(distinct)} recorded"
            )

    def _checked(
        self,
        plan: ActionPlan,
        outcome: ActionOutcome,
        *,
        expect_simulated: bool,
    ) -> ActionOutcome:
        if outcome.plan_id != plan.plan_id or outcome.idempotency_key != plan.idempotency_key:
            raise ActuatorContractError(
                f"{plan.actuator.value} reported against {outcome.plan_id}, not {plan.plan_id}; "
                "an adapter may only account for the plan it was given"
            )
        if expect_simulated and outcome.status is not ActionStatus.SIMULATED:
            raise ActuatorContractError(
                f"{plan.actuator.value} reported {outcome.status.value} where nothing may be "
                "touched; a simulation that changes something is not a simulation"
            )
        if not expect_simulated and outcome.status is ActionStatus.SIMULATED:
            raise ActuatorContractError(
                f"{plan.actuator.value} reported a simulation where a real call was made; "
                "an outcome must say honestly whether the world was touched"
            )
        return outcome

    def _deduplicated(
        self,
        plan: ActionPlan,
        already: ActionOutcome,
        *,
        ts: datetime,
        approvals: Sequence[str],
    ) -> ActionOutcome:
        """Answer from what already happened, without reaching the adapter."""
        return ActionOutcome(
            outcome_id=f"{plan.plan_id}-dedup-{already.outcome_id}",
            ts=ts,
            plan_id=plan.plan_id,
            idempotency_key=plan.idempotency_key,
            status=already.status,
            dry_run=False,
            detail=(
                f"{already.status.value.lower()} already under this key by {already.outcome_id}; "
                "the effect this plan describes is the one already in place"
            ),
            deduplicated=True,
            observed=already.observed,
            approvals=tuple(dict.fromkeys(approvals)),
            revert_token=already.revert_token,
            honesty=already.honesty,
        )


def _with_approvals(outcome: ActionOutcome, approvals: Sequence[str]) -> ActionOutcome:
    """Record who signed the action on the outcome the ledger will keep.

    Re-validated rather than copied in place: a contract that is bypassed on the
    way into the audit trail is not a contract.
    """
    if not approvals:
        return outcome
    fields = outcome.model_dump()
    fields["approvals"] = tuple(dict.fromkeys(approvals))
    return ActionOutcome.model_validate(fields)
