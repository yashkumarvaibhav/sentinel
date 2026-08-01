"""The durable canary: one share per claim, and an unwind that puts all of them back.

Widening an effect is the one thing this plane does that is not a single
dispatch. What makes it safe is not the widening but the bookkeeping: every
share that reaches production is a committed fact before the next one is even
claimed, because the one thing a revert cannot do is put back a value nobody
wrote down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from action.actuators.base import ActionRejectedError
from action.actuators.simulated import SimulatedActuator
from action.config import load_action_config
from action.control import (
    ActionControlTransitionError,
    ActionExecutionClaim,
    ActionExecutionOperation,
    ActionExecutionPhase,
    complete_action_control,
    complete_canary_step,
)
from action.executor import ActionExecutor
from action.guards import CANARY_GATE, BlastRadiusGuard, CollateralReport
from action.orchestrator import ActionControlOrchestrator
from common.config import load_config
from contracts import (
    ActionCanaryStep,
    ActionControlSnapshot,
    ActionControlState,
    ActionGateResult,
    ActionGateStatus,
    ActionKind,
    ActionOutcome,
    ActionPlan,
    ActionRungSnapshot,
    ActionStatus,
    ActuatorKind,
    action_idempotency_key,
)

TS = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
SHARES = (5, 10, 20)
COHORT = "general-traffic"


# --- doubles -----------------------------------------------------------------


@dataclass
class _ScriptedCollateral:
    """Collateral answers keyed by the share being probed."""

    dirty_at: int | None = None
    calls: list[int] = field(default_factory=list)

    def __call__(self, plan: ActionPlan, *, ts: datetime) -> CollateralReport:
        del ts
        share = int(str(plan.parameters["enforced_percent"]))
        self.calls.append(share)
        if self.dirty_at is not None and share == self.dirty_at:
            return CollateralReport(
                clean=False,
                detail=f"checkout fell below its committed SLO at {share}%",
                harmed=("checkout",),
            )
        return CollateralReport(clean=True, detail=f"nothing else moved at {share}%")


class _ShareableActuator(SimulatedActuator):
    """A simulated adapter that can re-dial its own effect, as the mesh does."""

    def plan_share(
        self,
        plan: ActionPlan,
        *,
        parameter: str,
        share: int,
        ts: datetime,
    ) -> ActionPlan:
        from action.actuators.base import rebuild_plan

        if parameter != "enforced_percent":
            raise ActionRejectedError(f"this effect does not widen on {parameter!r}")
        return rebuild_plan(
            plan,
            parameters={"cohort": COHORT, parameter: share},
            estimated_blast_fraction=share / 100,
            ts=ts,
        )


class _Store:
    """Applies the real transition rules, so the state machine is under test."""

    def __init__(self, control: ActionControlSnapshot) -> None:
        self.control = control
        self.steps: list[ActionCanaryStep] = []
        self.unwound: list[ActionCanaryStep] = []
        self.dispatched = 0
        self.claims = 0

    async def claim_action_control(
        self,
        *,
        worker_id: str,
        ts: datetime,
        lease_seconds: int,
        settlement_delay_seconds: int,
    ) -> ActionExecutionClaim | None:
        del settlement_delay_seconds
        operation = {
            ActionControlState.APPLY_REQUESTED: ActionExecutionOperation.APPLY,
            ActionControlState.ROLLBACK_REQUESTED: ActionExecutionOperation.ROLLBACK,
        }.get(self.control.state)
        if operation is None:
            return None
        self.claims += 1
        return ActionExecutionClaim(
            claim_id=f"claim-{self.claims}",
            worker_id=worker_id,
            operation=operation,
            phase=ActionExecutionPhase.CLAIMED,
            claimed_at=ts,
            expires_at=ts + timedelta(seconds=lease_seconds),
            control=self.control,
        )

    async def mark_action_dispatched(
        self,
        claim: ActionExecutionClaim,
        *,
        ts: datetime,
    ) -> ActionExecutionClaim:
        self.dispatched += 1
        return ActionExecutionClaim(
            claim_id=claim.claim_id,
            worker_id=claim.worker_id,
            operation=claim.operation,
            phase=ActionExecutionPhase.DISPATCHED,
            claimed_at=claim.claimed_at,
            expires_at=ts + timedelta(seconds=30),
            control=claim.control,
            recovered=claim.recovered,
        )

    async def complete_canary_step(
        self,
        claim: ActionExecutionClaim,
        step: ActionCanaryStep,
        *,
        ts: datetime,
    ) -> ActionControlSnapshot:
        self.steps.append(step)
        self.control = complete_canary_step(claim.control, step=step, ts=ts)
        return self.control

    async def complete_action_execution(
        self,
        claim: ActionExecutionClaim,
        outcome: ActionOutcome | None,
        *,
        ts: datetime,
        rollback_slo_before: tuple[object, ...] = (),
        unwound: tuple[ActionCanaryStep, ...] = (),
    ) -> ActionControlSnapshot:
        del rollback_slo_before
        self.unwound.extend(unwound)
        self.control = complete_action_control(
            claim.control,
            operation=claim.operation,
            outcome=outcome,
            ts=ts,
            unwound=unwound,
        )
        return self.control

    async def complete_rollback_verification(
        self,
        claim: ActionExecutionClaim,
        verification: object,
        *,
        ts: datetime,
    ) -> ActionControlSnapshot:  # pragma: no cover - not reached by these tests
        raise AssertionError("these tests never settle a rollback verification")


# --- fixtures ----------------------------------------------------------------


def _guard() -> BlastRadiusGuard:
    """The committed cohorts, because the ceiling under test is the operator's."""
    return BlastRadiusGuard(load_config(CONFIG_DIR).cohorts)


def _executor() -> tuple[ActionExecutor, _ShareableActuator]:
    adapter = _ShareableActuator()
    return (
        ActionExecutor(
            actuators=[adapter],
            configuration=load_action_config(CONFIG_DIR / "action.yml"),
            dry_run=False,
        ),
        adapter,
    )


def _plan(share: int) -> ActionPlan:
    parameters: dict[str, str | bool | int | float] = {"cohort": COHORT, "enforced_percent": share}
    key = action_idempotency_key(
        actuator=ActuatorKind.SIMULATED,
        action_kind=ActionKind.RATE_LIMIT,
        target_ref="otel-demo/pod/edge/cohort/general-traffic",
        parameters=parameters,
    )
    return ActionPlan(
        plan_id=f"canary-plan-{share}",
        ts=TS,
        decision_id="decision-1",
        incident_id="incident-1",
        actuator=ActuatorKind.SIMULATED,
        action_kind=ActionKind.RATE_LIMIT,
        target_service="frontend",
        target_ref="otel-demo/pod/edge/cohort/general-traffic",
        parameters=parameters,
        reason="Hostile behaviour is confirmed against telemetry.",
        expected_effect="The edge reports the cohort held to its configured ceiling.",
        reversible=True,
        requires_human_approval=False,
        estimated_blast_fraction=share / 100,
        idempotency_key=key,
        honesty="SIMULATED",
    )


def _control(shares: tuple[int, ...] = SHARES) -> ActionControlSnapshot:
    plan = _plan(shares[-1])
    return ActionControlSnapshot(
        incident_id=plan.incident_id,
        plan_revision=1,
        state=ActionControlState.APPLY_REQUESTED,
        rung=ActionRungSnapshot(
            rung_id="hold-the-cohort-to-its-ceiling",
            ladder_id="contain-hostile-traffic",
            actuator=plan.actuator,
            action_kind=plan.action_kind,
            parameters=plan.parameters,
            ttl_seconds=900,
            requires_human_approval=False,
            required_approval_count=0,
            maximum_blast_fraction=0.20,
            reason="The deforming cohort is restrained while the explained surge is served.",
            canary_parameter="enforced_percent",
            canary_shares=shares,
        ),
        plan=plan,
        guard_results=(
            ActionGateResult(
                gate_id="blast-radius-within-the-rung-ceiling",
                status=ActionGateStatus.PASSED,
                detail="The measured blast radius stayed within the rung ceiling.",
            ),
        ),
        latest_outcome=None,
        created_at=TS,
        updated_at=TS,
    )


def _worker(
    store: _Store,
    *,
    collateral: _ScriptedCollateral | None,
) -> tuple[ActionControlOrchestrator, _ShareableActuator]:
    executor, adapter = _executor()
    return (
        ActionControlOrchestrator(
            store=store,
            executor=executor,
            guard=_guard(),
            worker_id="canary-worker",
            collateral=collateral,
        ),
        adapter,
    )


def _drain(worker: ActionControlOrchestrator, *, passes: int = 8, start: int = 1) -> None:
    async def run() -> None:
        for index in range(passes):
            if await worker.run_once(ts=TS + timedelta(seconds=start + index)) is None:
                return

    asyncio.run(run())


# --- widening ----------------------------------------------------------------


def test_each_share_is_committed_before_the_next_one_is_claimed() -> None:
    store = _Store(_control())
    probe = _ScriptedCollateral()
    worker, adapter = _worker(store, collateral=probe)

    _drain(worker)

    assert [step.share for step in store.steps] == list(SHARES)
    assert probe.calls == list(SHARES)
    # One dispatch per share, and one apply per dispatch: a canary that widened
    # inside a single dispatch would show three applies against one dispatch.
    assert store.dispatched == len(SHARES)
    assert adapter.call_count("apply") == len(SHARES)
    assert store.control.state is ActionControlState.APPLIED


def test_an_intermediate_share_carries_no_outcome_of_the_controls_own_plan() -> None:
    """A share that is not the rung's value is not the effect the control holds."""
    store = _Store(_control())
    worker, _ = _worker(store, collateral=_ScriptedCollateral())

    asyncio.run(worker.run_once(ts=TS + timedelta(seconds=1)))

    assert store.control.state is ActionControlState.APPLY_REQUESTED
    assert store.control.latest_outcome is None
    assert len(store.control.canary_progress) == 1
    assert store.control.canary_progress[0].share == 5


def test_the_completed_canarys_last_step_is_the_controls_own_plan() -> None:
    store = _Store(_control())
    worker, _ = _worker(store, collateral=_ScriptedCollateral())

    _drain(worker)

    final = store.control.canary_progress[-1]
    assert final.plan.plan_id == store.control.plan.plan_id
    assert store.control.latest_outcome is not None
    assert store.control.latest_outcome.plan_id == final.plan.plan_id
    assert store.control.latest_outcome.status is ActionStatus.APPLIED


def test_a_clean_step_records_the_canary_gate_and_a_dirty_one_does_not() -> None:
    clean = _Store(_control())
    clean_worker, _ = _worker(clean, collateral=_ScriptedCollateral())
    _drain(clean_worker)

    dirty = _Store(_control())
    dirty_worker, _ = _worker(dirty, collateral=_ScriptedCollateral(dirty_at=5))
    asyncio.run(dirty_worker.run_once(ts=TS + timedelta(seconds=1)))

    assert CANARY_GATE in clean.control.canary_progress[0].outcome.gates_passed
    assert CANARY_GATE not in dirty.control.canary_progress[0].outcome.gates_passed


def test_a_step_that_harms_something_stops_the_widening_and_asks_for_the_undo() -> None:
    store = _Store(_control())
    probe = _ScriptedCollateral(dirty_at=10)
    worker, adapter = _worker(store, collateral=probe)

    asyncio.run(worker.run_once(ts=TS + timedelta(seconds=1)))
    asyncio.run(worker.run_once(ts=TS + timedelta(seconds=2)))

    assert probe.calls == [5, 10]
    assert store.control.state is ActionControlState.ROLLBACK_REQUESTED
    assert [step.share for step in store.control.canary_progress] == [5, 10]
    assert store.control.canary_progress[-1].harmed == ("checkout",)
    # It stopped: the widest share was never reached.
    assert adapter.call_count("apply") == 2


def test_a_worker_with_no_probe_does_not_widen_on_an_assumption() -> None:
    store = _Store(_control())
    worker, _ = _worker(store, collateral=None)

    asyncio.run(worker.run_once(ts=TS + timedelta(seconds=1)))

    step = store.control.canary_progress[0]
    assert not step.collateral_clean
    assert step.harmed == ("collateral-probe",)
    assert "cannot say whether" in step.collateral_detail


# --- unwinding ---------------------------------------------------------------


def test_a_stopped_canary_is_unwound_newest_first_and_nothing_is_left_standing() -> None:
    store = _Store(_control())
    worker, adapter = _worker(store, collateral=_ScriptedCollateral(dirty_at=10))

    _drain(worker)

    assert [step.share for step in store.unwound] == [10, 5]
    assert store.control.state is ActionControlState.ROLLED_BACK
    assert all(not step.outcome.in_force for step in store.control.canary_progress)
    # The rung's own value never landed, so no outcome of the control's plan is
    # invented to say the control was undone.
    assert store.control.latest_outcome is None
    assert adapter.call_count("revert") == 2


def test_a_completed_canary_unwinds_every_share_including_the_rungs_own() -> None:
    store = _Store(_control())
    worker, adapter = _worker(store, collateral=_ScriptedCollateral())
    _drain(worker)
    assert store.control.state is ActionControlState.APPLIED

    store.control = store.control.model_copy(
        update={"state": ActionControlState.ROLLBACK_REQUESTED}
    )
    _drain(worker, start=60)

    assert [step.share for step in store.unwound] == [20, 10, 5]
    assert store.control.state is ActionControlState.ROLLED_BACK
    assert store.control.latest_outcome is not None
    assert store.control.latest_outcome.status is ActionStatus.REVERTED
    assert store.control.latest_outcome.plan_id == store.control.plan.plan_id
    assert adapter.call_count("revert") == 3


# --- the transition rules on their own ---------------------------------------


def test_a_step_out_of_the_committed_order_is_refused() -> None:
    control = _control()
    step = ActionCanaryStep(
        share=20,
        plan=control.plan,
        outcome=_applied(control.plan),
        collateral_clean=True,
        collateral_detail="nothing else moved",
    )

    with pytest.raises(ActionControlTransitionError, match="next committed share is 5%"):
        complete_canary_step(control, step=step, ts=TS + timedelta(seconds=1))


def test_progress_that_is_not_a_prefix_of_the_committed_shares_is_refused() -> None:
    control = _control()
    stray = ActionCanaryStep(
        share=10,
        plan=_plan(10),
        outcome=_applied(_plan(10)),
        collateral_clean=True,
        collateral_detail="nothing else moved",
    )

    with pytest.raises(ValueError, match="committed shares walked in order"):
        control.model_copy(update={"canary_progress": (stray,)}).model_validate(
            control.model_copy(update={"canary_progress": (stray,)}).model_dump()
        )


def test_a_canary_is_not_rolled_back_while_a_widened_step_still_stands() -> None:
    control = _control()
    standing = ActionCanaryStep(
        share=5,
        plan=_plan(5),
        outcome=_applied(_plan(5)),
        collateral_clean=True,
        collateral_detail="nothing else moved",
    )
    requested = control.model_copy(
        update={
            "state": ActionControlState.ROLLBACK_REQUESTED,
            "canary_progress": (standing,),
        }
    )

    with pytest.raises(ActionControlTransitionError, match="still in force"):
        complete_action_control(
            requested,
            operation=ActionExecutionOperation.ROLLBACK,
            outcome=None,
            ts=TS + timedelta(seconds=2),
            unwound=(standing,),
        )


def test_a_revert_cannot_unwind_a_share_the_canary_never_applied() -> None:
    control = _control()
    applied = ActionCanaryStep(
        share=5,
        plan=_plan(5),
        outcome=_applied(_plan(5)),
        collateral_clean=True,
        collateral_detail="nothing else moved",
    )
    invented = ActionCanaryStep(
        share=10,
        plan=_plan(10),
        outcome=_reverted(_plan(10)),
        collateral_clean=True,
        collateral_detail="nothing else moved",
    )
    requested = control.model_copy(
        update={
            "state": ActionControlState.ROLLBACK_REQUESTED,
            "canary_progress": (applied,),
        }
    )

    with pytest.raises(ActionControlTransitionError, match="never applied"):
        complete_action_control(
            requested,
            operation=ActionExecutionOperation.ROLLBACK,
            outcome=None,
            ts=TS + timedelta(seconds=2),
            unwound=(invented,),
        )


def test_an_adapter_that_states_no_share_of_its_own_effect_refuses_to_be_canaried() -> None:
    adapter = SimulatedActuator()

    with pytest.raises(ActionRejectedError, match="states no share of its own effect"):
        adapter.plan_share(_plan(20), parameter="enforced_percent", share=5, ts=TS)


def test_a_canary_step_that_found_harm_must_name_what_was_harmed() -> None:
    with pytest.raises(ValueError, match="must name what was harmed"):
        ActionCanaryStep(
            share=5,
            plan=_plan(5),
            outcome=_applied(_plan(5)),
            collateral_clean=False,
            collateral_detail="something went wrong",
        )


def _applied(plan: ActionPlan) -> ActionOutcome:
    return ActionOutcome(
        outcome_id=f"{plan.plan_id}-applied",
        ts=TS,
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        status=ActionStatus.APPLIED,
        dry_run=False,
        detail="the edge accepted the restraint",
        revert_token="runtime=0",
        honesty="SIMULATED",
    )


def _reverted(plan: ActionPlan) -> ActionOutcome:
    return ActionOutcome(
        outcome_id=f"{plan.plan_id}-reverted",
        ts=TS,
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        status=ActionStatus.REVERTED,
        dry_run=False,
        detail="the edge was put back",
        honesty="SIMULATED",
    )
