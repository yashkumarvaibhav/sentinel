"""Pure transition rules for the durable incident action control.

Storage serializes competing requests before calling this function. Keeping the
state transition pure makes the rejection/approval race deterministic and lets
the same rules be proven without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from action.guards import (
    BLAST_CAP_GATE,
    PROTECTED_COHORT_GATE,
    BlastRadiusExceededError,
    BlastRadiusGuard,
    GuardRefusedError,
    ProtectedCohortError,
)
from action.ladder import RungChoice
from contracts import (
    DESTRUCTIVE_ACTIONS,
    ActionApproval,
    ActionControlIntent,
    ActionControlRequest,
    ActionControlSnapshot,
    ActionControlState,
    ActionGateResult,
    ActionGateStatus,
    ActionOutcome,
    ActionPlan,
    ActionRungSnapshot,
    ActionStatus,
)


class ActionControlTransitionError(RuntimeError):
    """A requested intent is stale or unsafe for the server-held plan state."""


class ActionControlNotFoundError(LookupError):
    """No server-held plan exists at the requested incident/revision."""


class ActionExecutionOperation(StrEnum):
    """Which requested transition one internal worker claimed."""

    APPLY = "APPLY"
    ROLLBACK = "ROLLBACK"


class ActionExecutionPhase(StrEnum):
    """Whether the claim has crossed the external side-effect boundary."""

    CLAIMED = "CLAIMED"
    DISPATCHED = "DISPATCHED"


@dataclass(frozen=True, slots=True)
class ActionExecutionClaim:
    """One leased, server-internal claim over an immutable plan revision."""

    claim_id: str
    worker_id: str
    operation: ActionExecutionOperation
    phase: ActionExecutionPhase
    claimed_at: datetime
    expires_at: datetime
    control: ActionControlSnapshot
    recovered: bool = False

    def __post_init__(self) -> None:
        if not self.claim_id or not self.worker_id:
            raise ValueError("an execution claim needs stable claim and worker identities")
        if self.expires_at <= self.claimed_at:
            raise ValueError("an execution claim must expire after it was acquired")
        expected = (
            ActionControlState.APPLY_REQUESTED
            if self.operation is ActionExecutionOperation.APPLY
            else ActionControlState.ROLLBACK_REQUESTED
        )
        if self.control.state is not expected:
            raise ValueError(
                f"{self.operation.value} cannot claim {self.control.state.value}; "
                f"expected {expected.value}"
            )


_OUTCOME_STATE: dict[ActionStatus, ActionControlState] = {
    ActionStatus.SIMULATED: ActionControlState.SIMULATED,
    ActionStatus.APPLIED: ActionControlState.APPLIED,
    ActionStatus.VERIFIED: ActionControlState.VERIFIED,
    ActionStatus.FAILED: ActionControlState.FAILED,
    ActionStatus.REVERTED: ActionControlState.ROLLED_BACK,
}

_PASSED_GATE_DETAIL = {
    PROTECTED_COHORT_GATE: "The committed protected-cohort allowance was respected.",
    BLAST_CAP_GATE: "The measured blast radius stayed within the rung ceiling.",
}


def materialize_action_control(
    *,
    plan_revision: int,
    choice: RungChoice,
    plan: ActionPlan,
    guard: BlastRadiusGuard,
    ts: datetime,
    latest_outcome: ActionOutcome | None = None,
) -> ActionControlSnapshot:
    """Freeze one evidence-owned rung and plan while its guard inputs still exist.

    This is deliberately not an API helper. The caller must already hold the
    selected ``RungChoice`` and the exact adapter-built ``ActionPlan``; the
    browser never supplies either. Guard refusals become durable facts rather
    than exceptions that can disappear between the decision loop and the UI.
    """
    guard_results: tuple[ActionGateResult, ...]
    try:
        passed = guard.check(plan, choice)
    except GuardRefusedError as refusal:
        gate_id = _refused_gate(refusal)
        state = ActionControlState.REFUSED
        guard_results = (
            ActionGateResult(
                gate_id=gate_id,
                status=ActionGateStatus.REFUSED,
                detail=str(refusal),
            ),
        )
    else:
        guard_results = tuple(
            ActionGateResult(
                gate_id=gate_id,
                status=ActionGateStatus.PASSED,
                detail=_PASSED_GATE_DETAIL[gate_id],
            )
            for gate_id in passed
        )
        state = (
            _OUTCOME_STATE[latest_outcome.status]
            if latest_outcome is not None
            else (
                ActionControlState.AWAITING_APPROVAL
                if choice.requires_human_approval
                else ActionControlState.APPLY_REQUESTED
            )
        )

    required_approvals = (
        2 if choice.action_kind in DESTRUCTIVE_ACTIONS else int(choice.requires_human_approval)
    )
    return ActionControlSnapshot(
        incident_id=plan.incident_id,
        plan_revision=plan_revision,
        state=state,
        rung=ActionRungSnapshot(
            rung_id=choice.rung_id,
            ladder_id=choice.ladder_id,
            actuator=choice.actuator,
            action_kind=choice.action_kind,
            parameters=dict(choice.parameters),
            ttl_seconds=int(choice.ttl.total_seconds()),
            requires_human_approval=choice.requires_human_approval,
            required_approval_count=required_approvals,
            maximum_blast_fraction=choice.maximum_blast_fraction,
            reason=choice.reason,
            canary_parameter=choice.canary_parameter,
            canary_shares=choice.canary_shares,
        ),
        plan=plan,
        guard_results=guard_results,
        latest_outcome=latest_outcome,
        created_at=ts,
        updated_at=ts,
    )


def _refused_gate(refusal: GuardRefusedError) -> str:
    if isinstance(refusal, ProtectedCohortError):
        return PROTECTED_COHORT_GATE
    if isinstance(refusal, BlastRadiusExceededError):
        return BLAST_CAP_GATE
    return "action-guard-refused"


def complete_action_control(
    current: ActionControlSnapshot,
    *,
    operation: ActionExecutionOperation,
    outcome: ActionOutcome,
    ts: datetime,
) -> ActionControlSnapshot:
    """Commit an observed executor outcome against the exact claimed plan."""
    expected = (
        ActionControlState.APPLY_REQUESTED
        if operation is ActionExecutionOperation.APPLY
        else ActionControlState.ROLLBACK_REQUESTED
    )
    if current.state is not expected:
        raise ActionControlTransitionError(
            f"{operation.value} cannot complete {current.state.value}; expected {expected.value}"
        )
    if outcome.plan_id != current.plan.plan_id:
        raise ActionControlTransitionError(
            "an outcome cannot complete a different server-held plan"
        )
    if outcome.idempotency_key != current.plan.idempotency_key:
        raise ActionControlTransitionError(
            "an outcome cannot complete a different server-held effect key"
        )
    if ts < current.updated_at or outcome.ts > ts:
        raise ActionControlTransitionError("execution completion cannot move event time backwards")
    if operation is ActionExecutionOperation.ROLLBACK and outcome.status not in {
        ActionStatus.REVERTED,
        ActionStatus.FAILED,
        ActionStatus.SIMULATED,
    }:
        raise ActionControlTransitionError(
            f"a rollback cannot complete with {outcome.status.value}"
        )
    state = (
        ActionControlState.ROLLED_BACK
        if operation is ActionExecutionOperation.ROLLBACK
        and outcome.status is ActionStatus.REVERTED
        else _OUTCOME_STATE[outcome.status]
    )
    return _revalidate(
        current,
        state=state,
        latest_outcome=outcome,
        updated_at=ts,
    )


def transition_action_control(
    current: ActionControlSnapshot,
    request: ActionControlRequest,
    *,
    actor: str,
    ts: datetime,
) -> ActionControlSnapshot:
    """Apply one intent without accepting any client-authored action fact."""
    if request.incident_id != current.incident_id:
        raise ActionControlTransitionError(
            f"request incident {request.incident_id} does not match {current.incident_id}"
        )
    if request.plan_revision != current.plan_revision:
        raise ActionControlTransitionError(
            f"request plan revision {request.plan_revision} is stale; "
            f"current revision is {current.plan_revision}"
        )
    if ts < current.updated_at:
        raise ActionControlTransitionError("a control transition cannot move event time backwards")

    if request.intent is ActionControlIntent.REJECT:
        return _reject(current, actor=actor, ts=ts)
    if request.intent is ActionControlIntent.APPROVE:
        return _approve(current, actor=actor, ts=ts)
    return _request_rollback(current, actor=actor, ts=ts)


def _reject(current: ActionControlSnapshot, *, actor: str, ts: datetime) -> ActionControlSnapshot:
    if current.state is ActionControlState.REJECTED:
        return current
    if current.state is not ActionControlState.AWAITING_APPROVAL:
        raise ActionControlTransitionError(
            f"{current.state.value} cannot be rejected; a rejection is terminal for its revision"
        )
    return _revalidate(
        current,
        state=ActionControlState.REJECTED,
        rejected_by=actor,
        rejected_at=ts,
        updated_at=ts,
    )


def _approve(current: ActionControlSnapshot, *, actor: str, ts: datetime) -> ActionControlSnapshot:
    if current.state is ActionControlState.APPLY_REQUESTED:
        if any(approval.actor == actor for approval in current.approvals):
            return current
        raise ActionControlTransitionError(
            "this plan revision was already approved by another actor"
        )
    if current.state is ActionControlState.REJECTED:
        raise ActionControlTransitionError("this plan revision was rejected and is terminal")
    if current.state is not ActionControlState.AWAITING_APPROVAL:
        raise ActionControlTransitionError(f"{current.state.value} cannot be approved")
    if current.rung.required_approval_count > 1:
        raise ActionControlTransitionError(
            "this destructive plan needs two distinct approvers; the interim credential "
            "represents one identity and cannot satisfy a two-key action"
        )
    approval = ActionApproval(actor=actor, approved_at=ts)
    return _revalidate(
        current,
        state=ActionControlState.APPLY_REQUESTED,
        approvals=(*current.approvals, approval),
        updated_at=ts,
    )


def _request_rollback(
    current: ActionControlSnapshot, *, actor: str, ts: datetime
) -> ActionControlSnapshot:
    del actor  # Identity is written to the audit ledger by the endpoint orchestration.
    if current.state is ActionControlState.ROLLBACK_REQUESTED:
        return current
    if current.state not in {ActionControlState.APPLIED, ActionControlState.VERIFIED}:
        raise ActionControlTransitionError(
            "rollback requires a server-held applied or verified effect in force"
        )
    return _revalidate(
        current,
        state=ActionControlState.ROLLBACK_REQUESTED,
        updated_at=ts,
    )


def _revalidate(current: ActionControlSnapshot, **updates: object) -> ActionControlSnapshot:
    payload = current.model_dump()
    payload.update(updates)
    return ActionControlSnapshot.model_validate(payload)
