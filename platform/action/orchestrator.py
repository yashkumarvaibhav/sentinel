"""Crash-aware orchestration for durable, intent-only action controls.

The public API stops at intent. This worker is the only bridge from that
durable request to an actuator call. Claims are leased in Postgres, marked
DISPATCHED before crossing the side-effect boundary, and completed only with an
observed executor outcome. A replacement worker never repeats an ambiguous
dispatch: it verifies the target and fails closed for operator reconciliation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from action.actuators.base import ActionRejectedError
from action.control import (
    ActionExecutionClaim,
    ActionExecutionOperation,
    ActionExecutionPhase,
)
from action.executor import ActionExecutor
from action.guards import (
    CANARY_GATE,
    BlastRadiusGuard,
    CollateralProbe,
    CollateralReport,
    with_gates,
)
from action.ladder import RungChoice
from action.rollback import SloSettlementVerifier
from contracts import (
    ActionCanaryStep,
    ActionControlSnapshot,
    ActionGateStatus,
    ActionOutcome,
    ActionPlan,
    ActionRollbackVerification,
    ActionSloSample,
    ActionStatus,
)

DEFAULT_CLAIM_TTL = timedelta(seconds=30)
DEFAULT_SETTLEMENT_DELAY = timedelta(seconds=15)


@dataclass(frozen=True, slots=True)
class _ExecutionResult:
    """What one dispatch observed, and any canary shares it put back.

    ``outcome`` is ``None`` only for a revert that undid a canary which never
    reached the rung's own value: there is no outcome of the control's own plan
    because that plan was never applied, and inventing one would be a false
    entry in the journal the next revert reads from.
    """

    outcome: ActionOutcome | None
    unwound: tuple[ActionCanaryStep, ...] = ()


def _is_canary_apply(claim: ActionExecutionClaim) -> bool:
    return (
        claim.operation is ActionExecutionOperation.APPLY
        and bool(claim.control.rung.canary_shares)
        and not claim.recovered
    )


def _next_share(control: ActionControlSnapshot) -> int:
    walked = len(control.canary_progress)
    shares = control.rung.canary_shares
    if walked >= len(shares):
        raise ActionRejectedError("this canary has already walked every committed share")
    return shares[walked]


class ActionOrchestrationStore(Protocol):
    """The atomic claim/checkpoint/completion operations the worker needs."""

    async def claim_action_control(
        self,
        *,
        worker_id: str,
        ts: datetime,
        lease_seconds: int,
        settlement_delay_seconds: int,
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
        outcome: ActionOutcome | None,
        *,
        ts: datetime,
        rollback_slo_before: tuple[ActionSloSample, ...] = (),
        unwound: tuple[ActionCanaryStep, ...] = (),
    ) -> ActionControlSnapshot: ...

    async def complete_canary_step(
        self,
        claim: ActionExecutionClaim,
        step: ActionCanaryStep,
        *,
        ts: datetime,
    ) -> ActionControlSnapshot: ...

    async def complete_rollback_verification(
        self,
        claim: ActionExecutionClaim,
        verification: ActionRollbackVerification,
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
        settlement_delay: timedelta = DEFAULT_SETTLEMENT_DELAY,
        settlement: SloSettlementVerifier | None = None,
        collateral: CollateralProbe | None = None,
        after_commit: Callable[[ActionControlSnapshot], None] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("an action orchestrator needs a stable worker identity")
        if claim_ttl.total_seconds() < 1:
            raise ValueError("an action execution claim must live for at least one second")
        if settlement_delay.total_seconds() < 1:
            raise ValueError("action verification must settle for at least one second")
        self._store = store
        self._executor = executor
        self._guard = guard
        self._worker_id = worker_id
        self._lease_seconds = int(claim_ttl.total_seconds())
        self._settlement_delay_seconds = int(settlement_delay.total_seconds())
        self._settlement = settlement
        self._collateral = collateral
        self._after_commit = after_commit

    def _probe(self, plan: ActionPlan, *, ts: datetime) -> CollateralReport:
        """Ask the protected signals whether this step cost anything.

        A canary with no probe does not widen optimistically. Widening is a
        claim that nothing else broke, and a worker that cannot look has not
        got one to make.
        """
        if self._collateral is None:
            return CollateralReport(
                clean=False,
                detail=(
                    "this worker was given no collateral probe, so it cannot say whether "
                    "widening harmed anything; it stops rather than assuming"
                ),
                harmed=("collateral-probe",),
            )
        return self._collateral(plan, ts=ts)

    async def run_once(self, *, ts: datetime) -> ActionControlSnapshot | None:
        """Claim and finish one request, or report that no work was ready."""
        claim = await self._store.claim_action_control(
            worker_id=self._worker_id,
            ts=ts,
            lease_seconds=self._lease_seconds,
            settlement_delay_seconds=self._settlement_delay_seconds,
        )
        if claim is None:
            return None
        if claim.operation is ActionExecutionOperation.VERIFY_ROLLBACK:
            completed = await self._settle_rollback(claim, ts=ts)
        elif claim.operation is ActionExecutionOperation.VERIFY:
            outcome = await asyncio.to_thread(self._verify_target, claim, ts=ts)
            completed = await self._store.complete_action_execution(
                claim,
                outcome,
                ts=ts,
            )
        elif (
            claim.recovered
            and claim.phase is ActionExecutionPhase.DISPATCHED
            and not self._executor.dry_run
        ):
            outcome = await asyncio.to_thread(
                self._recover_ambiguous_dispatch,
                claim,
                ts=ts,
            )
            completed = await self._store.complete_action_execution(claim, outcome, ts=ts)
        elif _is_canary_apply(claim):
            dispatched = await self._store.mark_action_dispatched(claim, ts=ts)
            step = await asyncio.to_thread(self._widen_one_share, dispatched, ts=ts)
            completed = await self._store.complete_canary_step(dispatched, step, ts=ts)
        else:
            rollback_slo_before = await self._capture_before_rollback(claim, ts=ts)
            dispatched = await self._store.mark_action_dispatched(claim, ts=ts)
            result = await asyncio.to_thread(self._execute, dispatched, ts=ts)
            claim = dispatched
            completed = await self._store.complete_action_execution(
                claim,
                result.outcome,
                ts=ts,
                rollback_slo_before=rollback_slo_before,
                unwound=result.unwound,
            )
        if self._after_commit is not None:
            self._after_commit(completed)
        return completed

    async def _capture_before_rollback(
        self,
        claim: ActionExecutionClaim,
        *,
        ts: datetime,
    ) -> tuple[ActionSloSample, ...]:
        if claim.operation is not ActionExecutionOperation.ROLLBACK or self._settlement is None:
            return ()
        return await asyncio.to_thread(self._settlement.capture, ts=ts)

    def _verify_target(
        self,
        claim: ActionExecutionClaim,
        *,
        ts: datetime,
    ) -> ActionOutcome:
        prior = claim.control.latest_outcome
        if prior is None or not prior.in_force:
            raise ActionRejectedError("target verification needs an applied server-held outcome")
        self._executor.journal.record(prior)
        return self._executor.verify(claim.control.plan, ts=ts)

    async def _settle_rollback(
        self,
        claim: ActionExecutionClaim,
        *,
        ts: datetime,
    ) -> ActionControlSnapshot:
        if self._settlement is None:
            raise RuntimeError("rollback settlement has no SLO verifier attached")
        verification = await asyncio.to_thread(
            self._settlement.verify_rollback,
            before=claim.control.rollback_slo_before,
            ts=ts,
        )
        return await self._store.complete_rollback_verification(
            claim,
            verification,
            ts=ts,
        )

    def _widen_one_share(
        self,
        claim: ActionExecutionClaim,
        *,
        ts: datetime,
    ) -> ActionCanaryStep:
        """Apply exactly the next committed share, then look at what it cost.

        One share per claim is the durability argument. A widening that walked
        every share inside a single dispatch would, on a crash between two of
        them, leave an effect standing that no durable record names - and the
        one thing a revert cannot do is put back a value nobody wrote down.
        """
        control = claim.control
        rung = control.rung
        parameter = rung.canary_parameter
        if parameter is None:
            raise ActionRejectedError("a canary rung must name the dial it widens")
        share = _next_share(control)
        approvals = tuple(approval.actor for approval in control.approvals)
        try:
            plan = (
                control.plan
                if share == rung.canary_shares[-1]
                else self._executor.plan_share(
                    control.plan,
                    parameter=parameter,
                    share=share,
                    ts=ts,
                )
            )
            gates = self._guard.check(plan, _choice(control))
            outcome = with_gates(
                self._executor.apply(plan, ts=ts, owner=claim.worker_id, approvals=approvals),
                gates,
            )
        except ActionRejectedError as refusal:
            # The step never reached the world, so nothing is standing at this
            # share and the canary stops without anything to give back.
            return ActionCanaryStep(
                share=share,
                plan=control.plan,
                outcome=_failed_outcome(
                    claim,
                    ts=ts,
                    detail=f"The {share}% step was refused without changing anything: {refusal}",
                ),
                collateral_clean=False,
                collateral_detail=f"The {share}% step was refused before it was applied.",
                harmed=("action-guard",),
            )
        report = self._probe(plan, ts=ts)
        return ActionCanaryStep(
            share=share,
            plan=plan,
            outcome=(
                with_gates(outcome, (*outcome.gates_passed, CANARY_GATE))
                if report.clean
                else outcome
            ),
            collateral_clean=report.clean,
            collateral_detail=report.detail,
            harmed=report.harmed,
        )

    def _execute(self, claim: ActionExecutionClaim, *, ts: datetime) -> _ExecutionResult:
        control = claim.control
        plan = control.plan
        approvals = tuple(approval.actor for approval in control.approvals)
        try:
            if claim.operation is ActionExecutionOperation.ROLLBACK:
                return self._revert_everything_standing(claim, ts=ts, approvals=approvals)

            self._recheck_guard(control)
            applied = self._executor.apply(
                plan,
                ts=ts,
                owner=claim.worker_id,
                approvals=approvals,
            )
            return _ExecutionResult(outcome=applied)
        except ActionRejectedError as refusal:
            return _ExecutionResult(
                outcome=_failed_outcome(
                    claim,
                    ts=ts,
                    detail=f"Execution refused without changing the requested plan: {refusal}",
                )
            )

    def _revert_everything_standing(
        self,
        claim: ActionExecutionClaim,
        *,
        ts: datetime,
        approvals: tuple[str, ...],
    ) -> _ExecutionResult:
        """Undo every effect this control put in place, widest share first.

        A canary that walked 5 -> 10 -> 20 wrote three values, and each step's
        revert restores what *that* step found. Unwinding newest-first therefore
        walks 20 -> 10 -> 5 -> whatever was there before the canary began;
        undoing only the widest would leave the dial at 10 and report the
        restraint as lifted.

        Done inside one claim rather than one per step, because a restore is the
        replay of a recorded value: repeating it changes nothing, so a crash
        mid-unwind costs a re-run rather than a lost effect.
        """
        control = claim.control
        standing = tuple(step for step in control.canary_progress if step.outcome.in_force)
        unwound: list[ActionCanaryStep] = []
        control_outcome: ActionOutcome | None = None
        for step in reversed(standing):
            if step.outcome.revert_token is None:
                raise ActionRejectedError(
                    f"the {step.share}% canary step in force carries no usable revert token"
                )
            self._executor.journal.record(step.outcome)
            reverted = self._executor.revert(
                step.plan,
                ts=ts,
                owner=claim.worker_id,
                approvals=approvals,
            )
            unwound.append(step.model_copy(update={"outcome": reverted}))
            if step.plan.plan_id == control.plan.plan_id:
                control_outcome = reverted
        if control_outcome is not None:
            return _ExecutionResult(outcome=control_outcome, unwound=tuple(unwound))
        if standing:
            # The canary stopped before it reached the rung's own value, so the
            # control's plan was never applied and there is nothing of *its* to
            # revert. Every effect that did land has now been put back, and no
            # outcome is invented to say so: attributing one step's revert to a
            # plan that never ran would put a false entry in the journal the
            # next revert reads from.
            return _ExecutionResult(outcome=None, unwound=tuple(unwound))
        prior = control.latest_outcome
        if prior is None or not prior.in_force or prior.revert_token is None:
            raise ActionRejectedError(
                "the server-held applied outcome carries no usable revert token"
            )
        self._executor.journal.record(prior)
        return _ExecutionResult(
            outcome=self._executor.revert(
                control.plan,
                ts=ts,
                owner=claim.worker_id,
                approvals=approvals,
            )
        )

    def _recheck_guard(self, control: ActionControlSnapshot) -> None:
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
