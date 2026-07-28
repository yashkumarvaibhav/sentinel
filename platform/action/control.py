"""Pure transition rules for the durable incident action control.

Storage serializes competing requests before calling this function. Keeping the
state transition pure makes the rejection/approval race deterministic and lets
the same rules be proven without a database.
"""

from __future__ import annotations

from datetime import datetime

from contracts import (
    ActionApproval,
    ActionControlIntent,
    ActionControlRequest,
    ActionControlSnapshot,
    ActionControlState,
)


class ActionControlTransitionError(RuntimeError):
    """A requested intent is stale or unsafe for the server-held plan state."""


class ActionControlNotFoundError(LookupError):
    """No server-held plan exists at the requested incident/revision."""


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
