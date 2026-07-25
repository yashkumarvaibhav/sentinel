"""The action contracts must make the dangerous states unrepresentable."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts import (
    ActionKind,
    ActionOutcome,
    ActionPlan,
    ActionStatus,
    ActuatorKind,
    action_idempotency_key,
)

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _plan_fields(**overrides: object) -> dict[str, object]:
    parameters: dict[str, object] = overrides.pop("parameters", {"replicas": 6})  # type: ignore[assignment]
    actuator = overrides.pop("actuator", ActuatorKind.KUBERNETES)
    action_kind = overrides.pop("action_kind", ActionKind.SCALE)
    target_ref = overrides.pop("target_ref", "deployment/payment")
    assert isinstance(actuator, ActuatorKind)
    assert isinstance(action_kind, ActionKind)
    assert isinstance(target_ref, str)
    fields: dict[str, object] = {
        "plan_id": "plan-1",
        "ts": TICK,
        "decision_id": "decision-1",
        "incident_id": "incident-1",
        "actuator": actuator,
        "action_kind": action_kind,
        "target_service": "payment",
        "target_ref": target_ref,
        "parameters": parameters,
        "reason": "a confirmed fault with a named origin",
        "expected_effect": "the target reports six ready replicas",
        "reversible": True,
        "requires_human_approval": False,
        "estimated_blast_fraction": 0.1,
        "idempotency_key": action_idempotency_key(
            actuator=actuator,
            action_kind=action_kind,
            target_ref=target_ref,
            parameters=parameters,  # type: ignore[arg-type]
        ),
        "honesty": "REAL",
    }
    fields.update(overrides)
    return fields


def test_the_key_names_the_effect_not_the_request() -> None:
    """Two decisions asking for the same state are asking for one thing."""
    first = action_idempotency_key(
        actuator=ActuatorKind.KUBERNETES,
        action_kind=ActionKind.SCALE,
        target_ref="deployment/payment",
        parameters={"replicas": 6},
    )
    same_effect_other_order = action_idempotency_key(
        actuator=ActuatorKind.KUBERNETES,
        action_kind=ActionKind.SCALE,
        target_ref="deployment/payment",
        parameters={"replicas": 6},
    )
    assert first == same_effect_other_order

    for changed in (
        {"target_ref": "deployment/checkout"},
        {"parameters": {"replicas": 7}},
        {"action_kind": ActionKind.RESTART},
        {"actuator": ActuatorKind.SIMULATED},
    ):
        arguments: dict[str, object] = {
            "actuator": ActuatorKind.KUBERNETES,
            "action_kind": ActionKind.SCALE,
            "target_ref": "deployment/payment",
            "parameters": {"replicas": 6},
        }
        arguments.update(changed)
        assert action_idempotency_key(**arguments) != first  # type: ignore[arg-type]


def test_the_key_is_independent_of_who_asked_and_when() -> None:
    """A plan built by a later decision for the same effect keeps the same key."""
    first = ActionPlan.model_validate(_plan_fields())
    later = ActionPlan.model_validate(
        _plan_fields(decision_id="decision-2", incident_id="incident-2", plan_id="plan-2")
    )
    assert later.idempotency_key == first.idempotency_key


def test_a_signed_zero_parameter_does_not_produce_a_second_key() -> None:
    positive = action_idempotency_key(
        actuator=ActuatorKind.MESH,
        action_kind=ActionKind.RATE_LIMIT,
        target_ref="route/frontend",
        parameters={"burst": 0.0},
    )
    negative = action_idempotency_key(
        actuator=ActuatorKind.MESH,
        action_kind=ActionKind.RATE_LIMIT,
        target_ref="route/frontend",
        parameters={"burst": -0.0},
    )
    assert positive == negative


def test_a_plan_whose_key_disagrees_with_itself_is_refused() -> None:
    with pytest.raises(ValidationError, match="content hash of the effect"):
        ActionPlan.model_validate(_plan_fields(idempotency_key="a" * 64))


def test_a_destructive_rung_always_requires_a_human() -> None:
    with pytest.raises(ValidationError, match="destructive and requires human approval"):
        ActionPlan.model_validate(
            _plan_fields(
                action_kind=ActionKind.ISOLATE,
                parameters={},
                expected_effect="the target receives no traffic",
                requires_human_approval=False,
            )
        )
    approved = ActionPlan.model_validate(
        _plan_fields(
            action_kind=ActionKind.ROLLBACK,
            parameters={},
            expected_effect="the target reports the previous revision",
            requires_human_approval=True,
        )
    )
    assert approved.requires_human_approval


def test_an_effect_we_cannot_undo_is_not_taken_alone() -> None:
    with pytest.raises(ValidationError, match="cannot itself undo"):
        ActionPlan.model_validate(_plan_fields(reversible=False, requires_human_approval=False))


def test_observing_cannot_claim_a_blast_radius() -> None:
    with pytest.raises(ValidationError, match="cannot have a blast radius"):
        ActionPlan.model_validate(
            _plan_fields(
                action_kind=ActionKind.OBSERVE,
                parameters={},
                expected_effect="nothing changes",
                estimated_blast_fraction=0.2,
            )
        )


def _outcome_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "outcome_id": "outcome-1",
        "ts": TICK,
        "plan_id": "plan-1",
        "idempotency_key": "k" * 64,
        "status": ActionStatus.APPLIED,
        "dry_run": False,
        "detail": "scale in place on deployment/payment",
        "honesty": "REAL",
    }
    fields.update(overrides)
    return fields


def test_a_dry_run_can_never_claim_to_have_changed_anything() -> None:
    with pytest.raises(ValidationError, match="same fact"):
        ActionOutcome.model_validate(_outcome_fields(dry_run=True))
    with pytest.raises(ValidationError, match="same fact"):
        ActionOutcome.model_validate(_outcome_fields(status=ActionStatus.SIMULATED, dry_run=False))
    honest = ActionOutcome.model_validate(
        _outcome_fields(status=ActionStatus.SIMULATED, dry_run=True)
    )
    assert honest.dry_run and not honest.in_force


def test_a_dry_run_has_nothing_to_revert() -> None:
    with pytest.raises(ValidationError, match="nothing to revert"):
        ActionOutcome.model_validate(
            _outcome_fields(
                status=ActionStatus.SIMULATED,
                dry_run=True,
                revert_token="revert-1",
            )
        )


def test_only_an_effect_already_settled_can_be_deduplicated() -> None:
    with pytest.raises(ValidationError, match="cannot be deduplicated"):
        ActionOutcome.model_validate(_outcome_fields(status=ActionStatus.FAILED, deduplicated=True))
    with pytest.raises(ValidationError, match="cannot be deduplicated"):
        ActionOutcome.model_validate(
            _outcome_fields(status=ActionStatus.SIMULATED, dry_run=True, deduplicated=True)
        )
    for settled in (ActionStatus.APPLIED, ActionStatus.VERIFIED, ActionStatus.REVERTED):
        assert ActionOutcome.model_validate(
            _outcome_fields(status=settled, deduplicated=True)
        ).deduplicated


def test_only_an_applied_or_verified_effect_counts_as_in_force() -> None:
    in_force = {
        status: ActionOutcome.model_validate(
            _outcome_fields(status=status, dry_run=status is ActionStatus.SIMULATED)
        ).in_force
        for status in ActionStatus
    }
    assert in_force == {
        ActionStatus.APPLIED: True,
        ActionStatus.VERIFIED: True,
        ActionStatus.SIMULATED: False,
        ActionStatus.REVERTED: False,
        ActionStatus.FAILED: False,
    }


def test_an_approver_is_recorded_once() -> None:
    with pytest.raises(ValidationError, match="approvals must not contain duplicates"):
        ActionOutcome.model_validate(_outcome_fields(approvals=("ada", "ada")))
