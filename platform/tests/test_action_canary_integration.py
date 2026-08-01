"""The durable canary against a real Envoy, one claim per share.

Gated behind an environment variable and `make lab-edge`, like every other
adapter proof in this plane, so a machine without a proxy skips rather than
fails.

Worth having rather than trusting the unit tests for one reason: the unit
tests drive a fake cluster command, and a fake cannot reproduce the thing
`verify` exists for. Envoy's admin API answers `OK` to a runtime write whose
value is not a percentage at all and then behaves as if nothing was set - so
"the canary widened" is only a real claim if each share is read back off the
proxy that is supposed to be enforcing it.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from action.actuators import MeshActuator
from action.actuators.mesh import EdgeUnreachableError
from action.config import MeshConfig, load_action_config
from action.control import (
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
    Decision,
    DecisionAction,
    IncidentSeverity,
    VerdictClass,
)

TICK = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
INTEGRATION_ENV = "SENTINEL_ACTION_MESH_INTEGRATION"
SANDBOX_ADMIN = "http://127.0.0.1:8049"
POD = "frontend-proxy-abc123"

# The rung the north-star scenario actually reaches, with the shares
# `config/ladders.yml` commits it to.
COHORT = "general-traffic"
RUNTIME_KEY = "general_traffic"
SHARES = (5, 10, 20)
ENABLED = f"sentinel.ratelimit.{RUNTIME_KEY}.enabled"
ENFORCED = f"sentinel.ratelimit.{RUNTIME_KEY}.enforced"


def _admin(path: str, *, write: bool = False) -> str:
    request = urllib.request.Request(
        f"{SANDBOX_ADMIN}/{path}", method="POST" if write else "GET", data=b"" if write else None
    )
    with urllib.request.urlopen(request, timeout=10) as answer:
        return str(answer.read().decode("utf-8"))


class _SandboxEdge:
    """The real Envoy from `make lab-edge`, behind the cluster-command seam."""

    def __call__(self, argv: Sequence[str], *, input_text: Any = None, timeout: int = 60) -> str:
        del input_text, timeout
        if "pods" in argv and "--raw" not in argv:
            return POD
        path = argv[list(argv).index("--raw") + 1].split("/proxy/", 1)[1]
        try:
            return _admin(path, write="create" in argv)
        except urllib.error.URLError as error:  # pragma: no cover - sandbox down
            raise EdgeUnreachableError(f"the sandbox edge is unreachable: {error}") from error


def _runtime_value(key: str) -> str:
    """What the proxy itself says the key is right now."""
    document = json.loads(_admin("runtime"))
    entry = document["entries"].get(key, {})
    return str(entry.get("final_value", ""))


@pytest.fixture
def edge() -> Any:
    if os.environ.get(INTEGRATION_ENV) != "1":
        pytest.skip(f"set {INTEGRATION_ENV}=1 with `make lab-edge` running to run this")
    reset = f"{ENABLED}=0&{ENFORCED}=0"
    _admin(f"runtime_modify?{reset}", write=True)
    yield _SandboxEdge()
    _admin(f"runtime_modify?{reset}", write=True)


class _Store:
    """The real transition rules over an in-memory row, so the state is genuine."""

    def __init__(self, control: ActionControlSnapshot) -> None:
        self.control = control
        self.steps: list[ActionCanaryStep] = []
        self.unwound: list[ActionCanaryStep] = []

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
        return ActionExecutionClaim(
            claim_id=f"claim-{len(self.steps) + len(self.unwound) + 1}",
            worker_id=worker_id,
            operation=operation,
            phase=ActionExecutionPhase.CLAIMED,
            claimed_at=ts,
            expires_at=ts + timedelta(seconds=lease_seconds),
            control=self.control,
        )

    async def mark_action_dispatched(
        self, claim: ActionExecutionClaim, *, ts: datetime
    ) -> ActionExecutionClaim:
        return ActionExecutionClaim(
            claim_id=claim.claim_id,
            worker_id=claim.worker_id,
            operation=claim.operation,
            phase=ActionExecutionPhase.DISPATCHED,
            claimed_at=claim.claimed_at,
            expires_at=ts + timedelta(seconds=30),
            control=claim.control,
        )

    async def complete_canary_step(
        self, claim: ActionExecutionClaim, step: ActionCanaryStep, *, ts: datetime
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
        rollback_slo_before: tuple[Any, ...] = (),
        unwound: tuple[ActionCanaryStep, ...] = (),
    ) -> ActionControlSnapshot:
        del rollback_slo_before
        self.unwound.extend(unwound)
        self.control = complete_action_control(
            claim.control, operation=claim.operation, outcome=outcome, ts=ts, unwound=unwound
        )
        return self.control

    async def complete_rollback_verification(
        self, claim: ActionExecutionClaim, verification: Any, *, ts: datetime
    ) -> ActionControlSnapshot:  # pragma: no cover - never reached here
        raise AssertionError("this proof never settles a rollback verification")


def _mesh_config() -> MeshConfig:
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    assert configuration.mesh is not None
    return configuration.mesh


def _decision() -> Decision:
    return Decision(
        decision_id="decision-canary",
        ts=TICK,
        incident_id="incident-canary",
        action=DecisionAction.ACT,
        rule_id="restrain-a-deforming-cohort",
        reason="Hostile behaviour is confirmed against telemetry.",
        evidence_ts=TICK,
        severity=IncidentSeverity.HIGH,
        confirmed=True,
        verification_id="verification-canary",
        requires_human_approval=False,
        verdict_class=VerdictClass.ATTACK,
        verdict_id="verdict-canary",
        confidence=0.9,
        target_service="frontend",
    )


def _control(plan: ActionPlan) -> ActionControlSnapshot:
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
            canary_shares=SHARES,
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
        created_at=TICK,
        updated_at=TICK,
    )


def _clean(plan: ActionPlan, *, ts: datetime) -> CollateralReport:
    del ts
    return CollateralReport(clean=True, detail=f"nothing else moved while {plan.plan_id} stood")


def _worker(store: _Store, actuator: MeshActuator) -> ActionControlOrchestrator:
    executor = ActionExecutor(
        actuators=[actuator],
        configuration=load_action_config(CONFIG_DIR / "action.yml"),
        # Deliberately not the committed posture. `config/action.yml` ships
        # dry-run and this proof is the one place that says otherwise, in a
        # gated test against a contained sandbox rather than in the file every
        # deployment reads.
        dry_run=False,
    )
    return ActionControlOrchestrator(
        store=store,
        executor=executor,
        guard=BlastRadiusGuard(load_config(CONFIG_DIR).cohorts),
        worker_id="canary-integration",
        collateral=_clean,
    )


def test_a_real_envoy_is_widened_one_durable_share_at_a_time(edge: _SandboxEdge) -> None:
    """Each share is read back off the proxy that is supposed to be enforcing it."""
    actuator = MeshActuator(configuration=_mesh_config(), command=edge)
    plan = actuator.plan(
        _decision(),
        action_kind=ActionKind.RATE_LIMIT,
        parameters={"cohort": COHORT, "enforced_percent": SHARES[-1]},
        ts=TICK,
    )
    store = _Store(_control(plan))
    worker = _worker(store, actuator)
    observed: list[tuple[int, str]] = []

    async def widen() -> None:
        for index, share in enumerate(SHARES):
            completed = await worker.run_once(ts=TICK + timedelta(seconds=index + 1))
            assert completed is not None
            observed.append((share, _runtime_value(ENFORCED)))

    asyncio.run(widen())

    assert [step.share for step in store.steps] == list(SHARES)
    # The proxy's own answer after each step, not the adapter's report of it.
    assert observed == [(5, "5"), (10, "10"), (20, "20")]
    # The whole cohort is measured against its ceiling (enabled=100); the dial
    # the canary widens is how much of the excess is actually refused.
    assert _runtime_value(ENABLED) == "100", "the cohort is actually being measured"
    assert store.control.state is ActionControlState.APPLIED
    assert all(CANARY_GATE in step.outcome.gates_passed for step in store.steps)
    assert store.control.latest_outcome is not None
    assert store.control.latest_outcome.status is ActionStatus.APPLIED


def test_a_real_envoy_is_put_all_the_way_back_widest_share_first(edge: _SandboxEdge) -> None:
    """Undoing only the widest would leave the dial at 10 and call it lifted."""
    actuator = MeshActuator(configuration=_mesh_config(), command=edge)
    plan = actuator.plan(
        _decision(),
        action_kind=ActionKind.RATE_LIMIT,
        parameters={"cohort": COHORT, "enforced_percent": SHARES[-1]},
        ts=TICK,
    )
    store = _Store(_control(plan))
    worker = _worker(store, actuator)

    async def widen_then_undo() -> None:
        for index in range(len(SHARES)):
            await worker.run_once(ts=TICK + timedelta(seconds=index + 1))
        assert _runtime_value(ENFORCED) == "20"
        store.control = store.control.model_copy(
            update={"state": ActionControlState.ROLLBACK_REQUESTED}
        )
        await worker.run_once(ts=TICK + timedelta(seconds=60))

    asyncio.run(widen_then_undo())

    assert [step.share for step in store.unwound] == [20, 10, 5]
    assert store.control.state is ActionControlState.ROLLED_BACK
    # Back where the canary found it, on the proxy itself.
    assert _runtime_value(ENFORCED) == "0"
    assert _runtime_value(ENABLED) == "0"
    assert actuator.verify(plan, ts=TICK).status is ActionStatus.FAILED, (
        "the cohort is still restrained after the unwind"
    )


def test_the_committed_posture_is_still_dry_run() -> None:
    """This proof arms an executor by hand; the file every deployment reads does not."""
    configuration = load_action_config(CONFIG_DIR / "action.yml")

    assert configuration.execution.dry_run, "config/action.yml must ship dry-run"
