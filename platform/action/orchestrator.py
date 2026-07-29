"""Crash-aware orchestration for durable, intent-only action controls.

The public API stops at intent. This worker is the only bridge from that
durable request to an actuator call. Claims are leased in Postgres, marked
DISPATCHED before crossing the side-effect boundary, and completed only with an
observed executor outcome. A replacement worker never repeats an ambiguous
dispatch: it verifies the target and fails closed for operator reconciliation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from action.actuators.base import ActionRejectedError
from action.control import (
    ActionExecutionClaim,
    ActionExecutionOperation,
    ActionExecutionPhase,
)
from action.executor import ActionExecutor
from action.guards import BlastRadiusGuard
from action.ladder import RungChoice
from contracts import (
    ActionControlSnapshot,
    ActionGateStatus,
    ActionOutcome,
    ActionStatus,
)

DEFAULT_CLAIM_TTL = timedelta(seconds=30)


class ActionOrchestrationStore(Protocol):
    """The atomic claim/checkpoint/completion operations the worker needs."""

    async def claim_action_control(
        self,
        *,
        worker_id: str,
        ts: datetime,
        lease_seconds: int,
    ) -> ActionExecutionClaim | None: ...

    async def mark_action_dispatched(
        self,
        claim: ActionExecutionClaim,
        *,
        ts: datetime,
    ) -> ActionExecutionClaim: ...

    async def complete_action_execution(
        self,
        claim: ActionExecutionClaim,
        outcome: ActionOutcome,
        *,
        ts: datetime,
    ) -> ActionControlSnapshot: ...


class ActionControlOrchestrator:
    """Consume at most one durable action request and reconcile it authoritatively."""

    def __init__(
        self,
        *,
        store: ActionOrchestrationStore,
        executor: ActionExecutor,
        guard: BlastRadiusGuard,
        worker_id: str,
        claim_ttl: timedelta = DEFAULT_CLAIM_TTL,
        after_commit: Callable[[ActionControlSnapshot], None] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("an action orchestrator needs a stable worker identity")
        if claim_ttl.total_seconds() < 1:
            raise ValueError("an action execution claim must live for at least one second")
        self._store = store
        self._executor = executor
        self._guard = guard
        self._worker_id = worker_id
        self._lease_seconds = int(claim_ttl.total_seconds())
        self._after_commit = after_commit

    async def run_once(self, *, ts: datetime) -> ActionControlSnapshot | None:
        """Claim and finish one request, or report that no work was ready."""
        claim = await self._store.claim_action_control(
            worker_id=self._worker_id,
            ts=ts,
            lease_seconds=self._lease_seconds,
        )
        if claim is None:
            return None
        if (
            claim.recovered
            and claim.phase is ActionExecutionPhase.DISPATCHED
            and not self._executor.dry_run
        ):
            outcome = self._recover_ambiguous_dispatch(claim, ts=ts)
        else:
            dispatched = await self._store.mark_action_dispatched(claim, ts=ts)
            outcome = self._execute(dispatched, ts=ts)
            claim = dispatched
        completed = await self._store.complete_action_execution(claim, outcome, ts=ts)
        if self._after_commit is not None:
            self._after_commit(completed)
        return completed

    def _execute(self, claim: ActionExecutionClaim, *, ts: datetime) -> ActionOutcome:
        control = claim.control
        plan = control.plan
        approvals = tuple(approval.actor for approval in control.approvals)
        try:
            if claim.operation is ActionExecutionOperation.ROLLBACK:
                prior = control.latest_outcome
                if prior is None or not prior.in_force or prior.revert_token is None:
                    raise ActionRejectedError(
                        "the server-held applied outcome carries no usable revert token"
                    )
                self._executor.journal.record(prior)
                return self._executor.revert(
                    plan,
                    ts=ts,
                    owner=claim.worker_id,
                    approvals=approvals,
                )

            self._recheck_guard(control)
            applied = self._executor.apply(
                plan,
                ts=ts,
                owner=claim.worker_id,
                approvals=approvals,
            )
            return applied
        except ActionRejectedError as refusal:
            return _failed_outcome(
                claim,
                ts=ts,
                detail=f"Execution refused without changing the requested plan: {refusal}",
            )

    def _recheck_guard(self, control: ActionControlSnapshot) -> None:
        if control.rung.canary_shares:
            raise ActionRejectedError(
                "a canary rung needs its durable multi-step rollout worker; "
                "a single-plan control cannot bypass widening checks"
            )
        passed = self._guard.check(control.plan, _choice(control))
        committed = tuple(
            result.gate_id
            for result in control.guard_results
            if result.status is ActionGateStatus.PASSED
        )
        if passed != committed:
            raise ActionRejectedError(
                "current guard results no longer match the evidence-time committed gates"
            )

    def _recover_ambiguous_dispatch(
        self,
        claim: ActionExecutionClaim,
        *,
        ts: datetime,
    ) -> ActionOutcome:
        verification = self._executor.verify(claim.control.plan, ts=ts)
        observed = (
            f"target verification after an expired dispatch returned {verification.status.value}",
            *verification.observed,
        )
        return _failed_outcome(
            claim,
            ts=ts,
            detail=(
                "A prior worker crashed after dispatch was recorded. The action was not repeated; "
                "manual reconciliation is required because its revert token may not be durable."
            ),
            observed=observed,
        )


def _choice(control: ActionControlSnapshot) -> RungChoice:
    rung = control.rung
    return RungChoice(
        rung_id=rung.rung_id,
        ladder_id=rung.ladder_id,
        actuator=rung.actuator,
        action_kind=rung.action_kind,
        parameters=rung.parameters,
        ttl=timedelta(seconds=rung.ttl_seconds),
        requires_human_approval=rung.requires_human_approval,
        maximum_blast_fraction=rung.maximum_blast_fraction,
        reason=rung.reason,
        canary_parameter=rung.canary_parameter,
        canary_shares=rung.canary_shares,
    )


def _failed_outcome(
    claim: ActionExecutionClaim,
    *,
    ts: datetime,
    detail: str,
    observed: tuple[str, ...] = (),
) -> ActionOutcome:
    plan = claim.control.plan
    return ActionOutcome(
        outcome_id=f"{plan.plan_id}-failed-{claim.claim_id}",
        ts=ts,
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        status=ActionStatus.FAILED,
        dry_run=False,
        detail=detail,
        observed=observed,
        approvals=tuple(approval.actor for approval in claim.control.approvals),
        gates_passed=tuple(
            result.gate_id
            for result in claim.control.guard_results
            if result.status is ActionGateStatus.PASSED
        ),
        honesty=plan.honesty,
    )
