"""The browser can express intent; only evidence-owned server state may define an action."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from action.control import (
    ActionControlTransitionError,
    materialize_action_control,
    transition_action_control,
)
from action.guards import BlastRadiusGuard
from action.ladder import RungChoice
from common.config import CohortConfig, CohortDefinition
from contracts import (
    ActionControlIntent,
    ActionControlRequest,
    ActionControlSnapshot,
    ActionControlState,
    ActionGateResult,
    ActionGateStatus,
    ActionKind,
    ActionPlan,
    ActionRungSnapshot,
    ActuatorKind,
    action_idempotency_key,
)

TS = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


def _plan(*, destructive: bool = False) -> ActionPlan:
    action_kind = ActionKind.ISOLATE if destructive else ActionKind.RATE_LIMIT
    actuator = ActuatorKind.KUBERNETES if destructive else ActuatorKind.MESH
    target_ref = "deployment/payment" if destructive else "route/login"
    parameters: dict[str, str | bool | int | float] = (
        {"nodes": "worker-0"} if destructive else {"cohort": "invalid-credentials"}
    )
    return ActionPlan(
        plan_id="plan-1",
        ts=TS,
        decision_id="decision-1",
        incident_id="incident-1",
        actuator=actuator,
        action_kind=action_kind,
        target_service="payment" if destructive else "frontend",
        target_ref=target_ref,
        parameters=parameters,
        reason="The verified residual earned this committed rung.",
        expected_effect="The selected traffic is contained.",
        reversible=True,
        requires_human_approval=True,
        estimated_blast_fraction=1.0 if destructive else 0.1,
        idempotency_key=action_idempotency_key(
            actuator=actuator,
            action_kind=action_kind,
            target_ref=target_ref,
            parameters=parameters,
        ),
        honesty="REAL",
    )


def _snapshot(*, destructive: bool = False) -> ActionControlSnapshot:
    plan = _plan(destructive=destructive)
    return ActionControlSnapshot(
        incident_id=plan.incident_id,
        plan_revision=3,
        state=ActionControlState.AWAITING_APPROVAL,
        rung=ActionRungSnapshot(
            rung_id="isolate" if destructive else "rate-limit-invalid-credentials",
            ladder_id="fault" if destructive else "attack",
            actuator=plan.actuator,
            action_kind=plan.action_kind,
            parameters=plan.parameters,
            ttl_seconds=300,
            requires_human_approval=True,
            required_approval_count=2 if destructive else 1,
            maximum_blast_fraction=1.0 if destructive else 0.2,
            reason=plan.reason,
        ),
        plan=plan,
        guard_results=(
            ActionGateResult(
                gate_id="protected-cohort-unharmed",
                status=ActionGateStatus.PASSED,
                detail="The committed protected-cohort allowance was respected.",
            ),
            ActionGateResult(
                gate_id="blast-radius-within-the-rung-ceiling",
                status=ActionGateStatus.PASSED,
                detail="The measured blast radius stayed within the rung ceiling.",
            ),
        ),
        latest_outcome=None,
        approvals=(),
        rejected_by=None,
        rejected_at=None,
        created_at=TS,
        updated_at=TS,
    )


def test_action_request_is_intent_only_and_forbids_safety_critical_fields() -> None:
    request = ActionControlRequest(
        incident_id="incident-1",
        plan_revision=3,
        intent=ActionControlIntent.APPROVE,
    )

    assert set(request.model_dump()) == {"incident_id", "plan_revision", "intent"}
    for unsafe_field, value in {
        "rung": "rollback",
        "target_ref": "deployment/payment",
        "blast_radius": 1.0,
        "ttl": 0,
        "gates_passed": ["made-up-gate"],
        "revert_token": "attacker-controlled",
        "outcome": {"status": "VERIFIED"},
    }.items():
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ActionControlRequest.model_validate(
                {
                    **request.model_dump(mode="json"),
                    unsafe_field: value,
                }
            )


def test_action_snapshot_keeps_rung_plan_and_guard_identity_consistent() -> None:
    snapshot = _snapshot()

    assert snapshot.plan.incident_id == snapshot.incident_id
    assert snapshot.rung.action_kind == snapshot.plan.action_kind
    assert snapshot.rung.actuator == snapshot.plan.actuator

    with pytest.raises(ValidationError, match="rung and plan"):
        ActionControlSnapshot.model_validate(
            {
                **snapshot.model_dump(),
                "rung": {
                    **snapshot.rung.model_dump(),
                    "actuator": ActuatorKind.KUBERNETES,
                },
            }
        )


def test_materializer_owns_the_rung_plan_guards_and_initial_request_state() -> None:
    plan = _plan()
    choice = _choice(plan, maximum_blast_fraction=0.2)

    control = materialize_action_control(
        plan_revision=1,
        choice=choice,
        plan=plan,
        guard=_guard(),
        ts=TS,
    )

    assert control.state is ActionControlState.AWAITING_APPROVAL
    assert control.plan is plan
    assert control.rung.rung_id == choice.rung_id
    assert tuple(result.gate_id for result in control.guard_results) == (
        "protected-cohort-unharmed",
        "blast-radius-within-the-rung-ceiling",
    )
    assert all(result.status is ActionGateStatus.PASSED for result in control.guard_results)


def test_materializer_persists_a_guard_refusal_instead_of_requesting_execution() -> None:
    plan = _plan()

    control = materialize_action_control(
        plan_revision=1,
        choice=_choice(plan, maximum_blast_fraction=0.05),
        plan=plan,
        guard=_guard(),
        ts=TS,
    )

    assert control.state is ActionControlState.REFUSED
    assert control.guard_results[0].gate_id == "blast-radius-within-the-rung-ceiling"
    assert control.guard_results[0].status is ActionGateStatus.REFUSED
    assert "authorises at most" in control.guard_results[0].detail


def test_one_key_approval_requests_apply_without_claiming_it_happened() -> None:
    approved = transition_action_control(
        _snapshot(),
        ActionControlRequest(
            incident_id="incident-1",
            plan_revision=3,
            intent=ActionControlIntent.APPROVE,
        ),
        actor="interim-operator",
        ts=TS.replace(minute=1),
    )

    assert approved.state is ActionControlState.APPLY_REQUESTED
    assert approved.latest_outcome is None
    assert tuple(approval.actor for approval in approved.approvals) == ("interim-operator",)


def test_rejection_is_terminal_and_a_replayed_rejection_is_idempotent() -> None:
    request = ActionControlRequest(
        incident_id="incident-1",
        plan_revision=3,
        intent=ActionControlIntent.REJECT,
    )
    rejected = transition_action_control(
        _snapshot(),
        request,
        actor="interim-operator",
        ts=TS.replace(minute=1),
    )

    replayed = transition_action_control(
        rejected,
        request,
        actor="interim-operator",
        ts=TS.replace(minute=2),
    )

    assert rejected.state is ActionControlState.REJECTED
    assert replayed == rejected
    with pytest.raises(ActionControlTransitionError, match="terminal"):
        transition_action_control(
            rejected,
            request.model_copy(update={"intent": ActionControlIntent.APPROVE}),
            actor="interim-operator",
            ts=TS.replace(minute=2),
        )


def test_stale_revision_and_client_identity_mismatch_fail_closed() -> None:
    snapshot = _snapshot()

    with pytest.raises(ActionControlTransitionError, match="revision"):
        transition_action_control(
            snapshot,
            ActionControlRequest(
                incident_id=snapshot.incident_id,
                plan_revision=2,
                intent=ActionControlIntent.REJECT,
            ),
            actor="interim-operator",
            ts=TS.replace(minute=1),
        )
    with pytest.raises(ActionControlTransitionError, match="incident"):
        transition_action_control(
            snapshot,
            ActionControlRequest(
                incident_id="different-incident",
                plan_revision=3,
                intent=ActionControlIntent.REJECT,
            ),
            actor="interim-operator",
            ts=TS.replace(minute=1),
        )


def test_single_interim_identity_cannot_approve_a_two_key_plan() -> None:
    with pytest.raises(ActionControlTransitionError, match="two distinct"):
        transition_action_control(
            _snapshot(destructive=True),
            ActionControlRequest(
                incident_id="incident-1",
                plan_revision=3,
                intent=ActionControlIntent.APPROVE,
            ),
            actor="interim-operator",
            ts=TS.replace(minute=1),
        )


def test_rollback_request_requires_a_server_held_effect_in_force() -> None:
    request = ActionControlRequest(
        incident_id="incident-1",
        plan_revision=3,
        intent=ActionControlIntent.ROLLBACK,
    )

    with pytest.raises(ActionControlTransitionError, match="in force"):
        transition_action_control(
            _snapshot(),
            request,
            actor="interim-operator",
            ts=TS.replace(minute=1),
        )


def _choice(plan: ActionPlan, *, maximum_blast_fraction: float) -> RungChoice:
    return RungChoice(
        rung_id="rate-limit-invalid-credentials",
        ladder_id="attack",
        actuator=plan.actuator,
        action_kind=plan.action_kind,
        parameters=plan.parameters,
        ttl=timedelta(minutes=5),
        requires_human_approval=True,
        maximum_blast_fraction=maximum_blast_fraction,
        reason=plan.reason,
    )


def _guard() -> BlastRadiusGuard:
    return BlastRadiusGuard(
        CohortConfig(
            version=1,
            cohorts=(
                CohortDefinition(
                    cohort_id="invalid-credentials",
                    description="Untrusted sessions failing authentication.",
                    match={"auth.valid": False},
                    protected=False,
                    max_blast_radius_pct=20.0,
                ),
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
