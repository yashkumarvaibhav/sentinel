"""Durable action intent is executed once, from server-held fields, after commit."""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from action.actuators.kubernetes import KubernetesActuator
from action.actuators.simulated import SimulatedActuator
from action.config import ActionConfig, load_action_config
from action.control import (
    ActionExecutionClaim,
    ActionExecutionOperation,
    ActionExecutionPhase,
    complete_action_control,
    complete_rollback_verification,
    transition_action_control,
)
from action.executor import ActionExecutor
from action.guards import BLAST_CAP_GATE, BlastRadiusGuard
from action.orchestrator import ActionControlOrchestrator
from action.rollback import (
    SloReading,
    SloSettlementVerifier,
    VictoriaMetricsSloReader,
)
from common.config import (
    CohortConfig,
    CohortDefinition,
    ServiceSlo,
    SloConfig,
    load_config,
)
from contracts import (
    ActionControlIntent,
    ActionControlRequest,
    ActionControlSnapshot,
    ActionControlState,
    ActionGateResult,
    ActionGateStatus,
    ActionKind,
    ActionOutcome,
    ActionPlan,
    ActionRollbackVerification,
    ActionRungSnapshot,
    ActionSloSample,
    ActionSloSampleStatus,
    ActionStatus,
    ActuatorKind,
    action_idempotency_key,
)

TS = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


class _Store:
    def __init__(self, claim: ActionExecutionClaim) -> None:
        self.claim: ActionExecutionClaim | None = claim
        self.dispatched = 0
        self.completed: list[ActionOutcome] = []

    async def claim_action_control(
        self,
        *,
        worker_id: str,
        ts: datetime,
        lease_seconds: int,
        settlement_delay_seconds: int,
    ) -> ActionExecutionClaim | None:
        del worker_id, ts, lease_seconds, settlement_delay_seconds
        claim, self.claim = self.claim, None
        return claim

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

    async def complete_action_execution(
        self,
        claim: ActionExecutionClaim,
        outcome: ActionOutcome,
        *,
        ts: datetime,
        rollback_slo_before: tuple[ActionSloSample, ...] = (),
    ) -> ActionControlSnapshot:
        self.completed.append(outcome)
        return complete_action_control(
            claim.control,
            operation=claim.operation,
            outcome=outcome,
            ts=ts,
            rollback_slo_before=rollback_slo_before,
        )

    async def complete_rollback_verification(
        self,
        claim: ActionExecutionClaim,
        verification: ActionRollbackVerification,
        *,
        ts: datetime,
    ) -> ActionControlSnapshot:
        return complete_rollback_verification(
            claim.control,
            verification=verification,
            ts=ts,
        )


def test_apply_is_published_only_after_durable_completion() -> None:
    executor, adapter = _executor()
    store = _Store(_claim(_control()))
    published: list[ActionControlSnapshot] = []
    worker = ActionControlOrchestrator(
        store=store,
        executor=executor,
        guard=_guard(),
        worker_id="action-worker-1",
        after_commit=published.append,
    )

    completed = asyncio.run(worker.run_once(ts=TS + timedelta(seconds=1)))

    assert completed is not None and completed.state is ActionControlState.APPLIED
    assert completed.latest_outcome is not None
    assert completed.latest_outcome.revert_token is not None
    assert adapter.call_count("apply") == 1
    assert adapter.call_count("verify") == 0
    assert store.dispatched == 1
    assert published == [completed]


def test_an_expired_dispatched_claim_is_verified_and_never_applied_again() -> None:
    executor, adapter = _executor()
    claim = _claim(
        _control(),
        phase=ActionExecutionPhase.DISPATCHED,
        recovered=True,
    )
    store = _Store(claim)
    worker = ActionControlOrchestrator(
        store=store,
        executor=executor,
        guard=_guard(),
        worker_id="action-worker-2",
    )

    completed = asyncio.run(worker.run_once(ts=TS + timedelta(seconds=31)))

    assert completed is not None and completed.state is ActionControlState.FAILED
    assert completed.latest_outcome is not None
    assert "not repeated" in completed.latest_outcome.detail
    assert adapter.call_count("apply") == 0
    assert adapter.call_count("verify") == 1
    assert store.dispatched == 0


def test_rollback_uses_only_the_server_held_plan_and_revert_token() -> None:
    executor, adapter = _executor()
    initial = _control()
    applied = executor.apply(initial.plan, ts=TS, owner="earlier-worker")
    rollback = initial.model_copy(
        update={
            "state": ActionControlState.ROLLBACK_REQUESTED,
            "latest_outcome": applied,
            "updated_at": TS + timedelta(seconds=1),
        }
    )
    claim = _claim(
        ActionControlSnapshot.model_validate(rollback),
        operation=ActionExecutionOperation.ROLLBACK,
    )
    store = _Store(claim)
    worker = ActionControlOrchestrator(
        store=store,
        executor=executor,
        guard=_guard(),
        worker_id="action-worker-3",
    )

    completed = asyncio.run(worker.run_once(ts=TS + timedelta(seconds=2)))

    assert completed is not None and completed.state is ActionControlState.ROLLED_BACK
    assert adapter.reverted_with == [applied.revert_token]
    assert completed.latest_outcome is not None
    assert completed.latest_outcome.status is ActionStatus.REVERTED


def test_delayed_target_verification_reads_the_adapter_without_redispatching() -> None:
    executor, adapter = _executor()
    initial = _control()
    applied_outcome = executor.apply(initial.plan, ts=TS, owner="earlier-worker")
    applied = ActionControlSnapshot.model_validate(
        initial.model_copy(
            update={
                "state": ActionControlState.APPLIED,
                "latest_outcome": applied_outcome,
                "updated_at": TS + timedelta(seconds=1),
            }
        )
    )
    store = _Store(
        _claim(
            applied,
            operation=ActionExecutionOperation.VERIFY,
        )
    )
    worker = ActionControlOrchestrator(
        store=store,
        executor=executor,
        guard=_guard(),
        worker_id="action-worker-verifier",
    )

    completed = asyncio.run(worker.run_once(ts=TS + timedelta(seconds=31)))

    assert completed is not None and completed.state is ActionControlState.VERIFIED
    assert completed.latest_outcome is not None
    assert completed.latest_outcome.revert_token == applied_outcome.revert_token
    assert adapter.call_count("apply") == 1
    assert adapter.call_count("verify") == 1
    assert store.dispatched == 0


def test_delayed_rollback_settlement_records_slo_proof_without_second_revert() -> None:
    executor, adapter = _executor()
    initial = _control()
    applied_outcome = executor.apply(initial.plan, ts=TS, owner="earlier-worker")
    reverted_outcome = executor.revert(
        initial.plan,
        ts=TS + timedelta(seconds=1),
        owner="earlier-worker",
    )
    verifier = _settlement_verifier(
        before_at=TS + timedelta(seconds=1),
        after_at=TS + timedelta(seconds=31),
    )
    before = verifier.capture(ts=TS + timedelta(seconds=1))
    rolled_back = ActionControlSnapshot.model_validate(
        initial.model_copy(
            update={
                "state": ActionControlState.ROLLED_BACK,
                "latest_outcome": reverted_outcome,
                "rollback_slo_before": before,
                "updated_at": TS + timedelta(seconds=1),
            }
        )
    )
    store = _Store(
        _claim(
            rolled_back,
            operation=ActionExecutionOperation.VERIFY_ROLLBACK,
        )
    )
    worker = ActionControlOrchestrator(
        store=store,
        executor=executor,
        guard=_guard(),
        settlement=verifier,
        worker_id="action-worker-rollback-verifier",
    )

    completed = asyncio.run(worker.run_once(ts=TS + timedelta(seconds=31)))

    assert completed is not None and completed.state is ActionControlState.ROLLED_BACK
    assert completed.rollback_verification is not None
    assert completed.rollback_verification.users_restored == pytest.approx(0.09)
    assert adapter.call_count("revert") == 1
    assert store.dispatched == 0
    assert applied_outcome.revert_token is not None


@pytest.mark.skipif(
    os.getenv("SENTINEL_ACTION_ORCHESTRATOR_INTEGRATION") != "1",
    reason="set SENTINEL_ACTION_ORCHESTRATOR_INTEGRATION=1 for a contained live scale",
)
def test_real_testbed_scale_is_applied_and_rolled_back_through_the_worker() -> None:
    configuration = load_action_config(REPO_ROOT / "config/action.yml")
    runtime = load_config(REPO_ROOT / "config")
    kubernetes = configuration.kubernetes
    assert kubernetes is not None
    actuator = KubernetesActuator(configuration=kubernetes)
    executor = ActionExecutor(
        actuators=[actuator],
        configuration=configuration,
        dry_run=False,
    )
    slo_client = httpx.Client(base_url="http://127.0.0.1:8042", timeout=10.0)
    settlement = SloSettlementVerifier(
        slos=runtime.slos,
        reader=VictoriaMetricsSloReader(client=slo_client, window_seconds=60),
    )
    original = int(_kubectl("get", "deployment/payment", "-o", "jsonpath={.spec.replicas}"))
    target = original + 1
    if target > kubernetes.maximum_replicas:
        target = original - 1
    assert target >= 0 and target != original
    now = datetime.now(UTC)
    plan = ActionPlan(
        plan_id=f"contained-payment-scale-{int(now.timestamp())}",
        ts=now,
        decision_id=f"contained-decision-{int(now.timestamp())}",
        incident_id=f"contained-incident-{int(now.timestamp())}",
        actuator=ActuatorKind.KUBERNETES,
        action_kind=ActionKind.SCALE,
        target_service="payment",
        target_ref=f"{kubernetes.namespace}/deployment/payment",
        parameters={"replicas": target},
        reason="A contained integration proof selected an absolute payment replica target.",
        expected_effect="The workload reports the requested number of ready replicas.",
        reversible=True,
        requires_human_approval=False,
        estimated_blast_fraction=round(
            1.0 / max(len(kubernetes.workloads), 1),
            6,
        ),
        idempotency_key=action_idempotency_key(
            actuator=ActuatorKind.KUBERNETES,
            action_kind=ActionKind.SCALE,
            target_ref=f"{kubernetes.namespace}/deployment/payment",
            parameters={"replicas": target},
        ),
        honesty="REAL",
    )
    control = _control_for_plan(plan, now=now)
    applied: ActionControlSnapshot | None = None
    try:
        store = _Store(_claim_at(control, now=now))
        worker = ActionControlOrchestrator(
            store=store,
            executor=executor,
            guard=BlastRadiusGuard(runtime.cohorts),
            settlement=settlement,
            worker_id="contained-action-worker",
        )
        applied = asyncio.run(worker.run_once(ts=now + timedelta(seconds=1)))
        assert applied is not None and applied.state is ActionControlState.APPLIED
        assert (
            int(_kubectl("get", "deployment/payment", "-o", "jsonpath={.spec.replicas}")) == target
        )
        _kubectl("rollout", "status", "deployment/payment", "--timeout=90s")

        verify_store = _Store(
            _claim_at(
                applied,
                now=now + timedelta(seconds=2),
                operation=ActionExecutionOperation.VERIFY,
            )
        )
        verify_worker = ActionControlOrchestrator(
            store=verify_store,
            executor=executor,
            guard=BlastRadiusGuard(runtime.cohorts),
            settlement=settlement,
            worker_id="contained-action-worker",
        )
        verified = asyncio.run(verify_worker.run_once(ts=now + timedelta(seconds=2)))
        assert verified is not None and verified.state is ActionControlState.VERIFIED

        rollback = transition_action_control(
            verified,
            ActionControlRequest(
                incident_id=verified.incident_id,
                plan_revision=verified.plan_revision,
                intent=ActionControlIntent.ROLLBACK,
            ),
            actor="contained-operator",
            ts=now + timedelta(seconds=3),
        )
        rollback_store = _Store(
            _claim_at(
                rollback,
                now=now + timedelta(seconds=3),
                operation=ActionExecutionOperation.ROLLBACK,
            )
        )
        rollback_worker = ActionControlOrchestrator(
            store=rollback_store,
            executor=executor,
            guard=BlastRadiusGuard(runtime.cohorts),
            settlement=settlement,
            worker_id="contained-action-worker",
        )
        reverted = asyncio.run(rollback_worker.run_once(ts=now + timedelta(seconds=4)))
        assert reverted is not None and reverted.state is ActionControlState.ROLLED_BACK
        assert reverted.rollback_slo_before
        assert all(
            sample.status is ActionSloSampleStatus.MEASURED
            for sample in reverted.rollback_slo_before
        )
        assert (
            int(_kubectl("get", "deployment/payment", "-o", "jsonpath={.spec.replicas}"))
            == original
        )
        settlement_store = _Store(
            _claim_at(
                reverted,
                now=now + timedelta(seconds=19),
                operation=ActionExecutionOperation.VERIFY_ROLLBACK,
            )
        )
        settlement_worker = ActionControlOrchestrator(
            store=settlement_store,
            executor=executor,
            guard=BlastRadiusGuard(runtime.cohorts),
            settlement=settlement,
            worker_id="contained-action-worker",
        )
        settled = asyncio.run(settlement_worker.run_once(ts=now + timedelta(seconds=19)))
        assert settled is not None and settled.rollback_verification is not None
        assert settled.rollback_verification.users_restored is not None
    finally:
        slo_client.close()
        current = int(_kubectl("get", "deployment/payment", "-o", "jsonpath={.spec.replicas}"))
        if current != original:
            _kubectl("scale", "deployment/payment", f"--replicas={original}")
        _kubectl("rollout", "status", "deployment/payment", "--timeout=90s")


def _control() -> ActionControlSnapshot:
    parameters: dict[str, str | bool | int | float] = {"replicas": 2}
    plan = ActionPlan(
        plan_id="orchestrated-plan",
        ts=TS,
        decision_id="decision-1",
        incident_id="incident-1",
        actuator=ActuatorKind.SIMULATED,
        action_kind=ActionKind.SCALE,
        target_service="frontend",
        target_ref="deployment/frontend",
        parameters=parameters,
        reason="Verified capacity evidence selected an absolute scale target.",
        expected_effect="The target reports the configured number of ready replicas.",
        reversible=True,
        requires_human_approval=False,
        estimated_blast_fraction=0.1,
        idempotency_key=action_idempotency_key(
            actuator=ActuatorKind.SIMULATED,
            action_kind=ActionKind.SCALE,
            target_ref="deployment/frontend",
            parameters=parameters,
        ),
        honesty="SIMULATED",
    )
    return ActionControlSnapshot(
        incident_id=plan.incident_id,
        plan_revision=1,
        state=ActionControlState.APPLY_REQUESTED,
        rung=ActionRungSnapshot(
            rung_id="add-headroom",
            ladder_id="fault",
            actuator=plan.actuator,
            action_kind=plan.action_kind,
            parameters=plan.parameters,
            ttl_seconds=300,
            requires_human_approval=False,
            required_approval_count=0,
            maximum_blast_fraction=0.2,
            reason=plan.reason,
        ),
        plan=plan,
        guard_results=(
            ActionGateResult(
                gate_id=BLAST_CAP_GATE,
                status=ActionGateStatus.PASSED,
                detail="The measured blast radius stayed within the rung ceiling.",
            ),
        ),
        latest_outcome=None,
        created_at=TS,
        updated_at=TS,
    )


def _control_for_plan(plan: ActionPlan, *, now: datetime) -> ActionControlSnapshot:
    return ActionControlSnapshot(
        incident_id=plan.incident_id,
        plan_revision=1,
        state=ActionControlState.APPLY_REQUESTED,
        rung=ActionRungSnapshot(
            rung_id="contained-scale",
            ladder_id="remediate-our-own-fault",
            actuator=plan.actuator,
            action_kind=plan.action_kind,
            parameters=plan.parameters,
            ttl_seconds=300,
            requires_human_approval=False,
            required_approval_count=0,
            maximum_blast_fraction=0.5,
            reason=plan.reason,
        ),
        plan=plan,
        guard_results=(
            ActionGateResult(
                gate_id=BLAST_CAP_GATE,
                status=ActionGateStatus.PASSED,
                detail="The measured blast radius stayed within the rung ceiling.",
            ),
        ),
        latest_outcome=None,
        created_at=now,
        updated_at=now,
    )


def _claim(
    control: ActionControlSnapshot,
    *,
    operation: ActionExecutionOperation = ActionExecutionOperation.APPLY,
    phase: ActionExecutionPhase = ActionExecutionPhase.CLAIMED,
    recovered: bool = False,
) -> ActionExecutionClaim:
    return ActionExecutionClaim(
        claim_id="claim-1",
        worker_id="action-worker",
        operation=operation,
        phase=phase,
        claimed_at=TS,
        expires_at=TS + timedelta(seconds=30),
        control=control,
        recovered=recovered,
    )


def _claim_at(
    control: ActionControlSnapshot,
    *,
    now: datetime,
    operation: ActionExecutionOperation = ActionExecutionOperation.APPLY,
) -> ActionExecutionClaim:
    return ActionExecutionClaim(
        claim_id=f"claim-{int(now.timestamp())}",
        worker_id="contained-action-worker",
        operation=operation,
        phase=ActionExecutionPhase.CLAIMED,
        claimed_at=now,
        expires_at=now + timedelta(seconds=30),
        control=control,
    )


def _kubectl(*args: str) -> str:
    completed = subprocess.run(
        [
            "kubectl",
            "--context",
            "k3d-sentinel-lab",
            "-n",
            "otel-demo",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.stdout.strip()


def _executor() -> tuple[ActionExecutor, SimulatedActuator]:
    adapter = SimulatedActuator()
    configuration = ActionConfig.model_validate(
        {
            "version": 1,
            "execution": {
                "dry_run": False,
                "lease_ttl_seconds": 30,
                "journal_capacity": 32,
            },
            "actuators": [{"actuator": "SIMULATED", "enabled": True}],
        }
    )
    return (
        ActionExecutor(
            actuators=[adapter],
            configuration=configuration,
            dry_run=False,
        ),
        adapter,
    )


def _guard() -> BlastRadiusGuard:
    return BlastRadiusGuard(
        CohortConfig(
            version=1,
            cohorts=(
                CohortDefinition(
                    cohort_id="checkout-users",
                    description="Users submitting checkout requests.",
                    match={"http.route": "/api/checkout"},
                    protected=True,
                    max_blast_radius_pct=0.0,
                ),
            ),
        )
    )


def _settlement_verifier(
    *,
    before_at: datetime,
    after_at: datetime,
) -> SloSettlementVerifier:
    def read(service: str, *, ts: datetime) -> SloReading | None:
        assert service == "checkout"
        if ts == before_at:
            return SloReading(service=service, availability=0.90, latency_p95_ms=1200.0)
        assert ts == after_at
        return SloReading(service=service, availability=0.99, latency_p95_ms=200.0)

    return SloSettlementVerifier(
        slos=SloConfig(
            version=1,
            slos=(
                ServiceSlo(
                    service="checkout",
                    availability_target=0.98,
                    latency_p95_ms=1000.0,
                    evaluation_window_minutes=1440,
                ),
            ),
        ),
        reader=read,
    )
