"""Freezing a live judgement into a plan, against the committed ladder.

What is under test is restraint. The planner's only judgement is *when* a
decision earns a plan; every safety-critical field in the plan it produces
comes from somewhere else, and the tests below are mostly about the cases
where it must produce nothing at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from action.actuators import SimulatedActuator
from action.actuators.base import ActionRejectedError, Actuator
from action.planner import LIVE_PLAN_REVISION, LiveActionPlanner
from common.config import load_config
from contracts import (
    ActionControlState,
    ActionKind,
    ActionParameterValue,
    ActionPlan,
    ActuatorKind,
    Decision,
    DecisionAction,
    IncidentSeverity,
    VerdictClass,
)

TS = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _decision(
    *,
    action: DecisionAction = DecisionAction.ACT,
    confidence: float = 0.5,
    target: str | None = "frontend",
    verdict_class: VerdictClass = VerdictClass.OPERATIONAL_FAULT,
    requires_human_approval: bool = False,
) -> Decision:
    return Decision(
        decision_id="decision-1",
        ts=TS,
        incident_id="incident-1",
        action=action,
        rule_id="rule-under-test",
        reason="Verified evidence selected an absolute target.",
        evidence_ts=TS,
        severity=IncidentSeverity.HIGH,
        confirmed=True,
        verification_id="verification-1",
        requires_human_approval=requires_human_approval,
        approval_reasons=("a person must sign this",) if requires_human_approval else (),
        escalation_reasons=(
            ("a person is in the loop",)
            if action
            in {DecisionAction.ESCALATE_TO_HUMAN, DecisionAction.AUTO_CONTAIN_THEN_ESCALATE}
            else ()
        ),
        verdict_class=verdict_class,
        verdict_id="verdict-1",
        confidence=confidence,
        target_service=target,
    )


def _planner(*actuators: Actuator) -> LiveActionPlanner:
    return LiveActionPlanner(
        config=load_config(CONFIG_DIR),
        config_root=CONFIG_DIR,
        actuators=list(actuators) or [SimulatedActuator()],
    )


def test_a_verified_acting_decision_freezes_a_guarded_plan() -> None:
    control = _planner().plan(_decision(), ts=TS)

    assert control is not None
    assert control.plan_revision == LIVE_PLAN_REVISION
    assert control.state is ActionControlState.APPLY_REQUESTED
    assert control.guard_results, "a control carries the gates it was actually held to"
    assert control.latest_outcome is None, "freezing a plan is not carrying it out"


def test_the_target_is_the_decisions_and_the_planner_never_picks_one() -> None:
    """The causal collapse computed it from evidence; nothing here may override it."""
    control = _planner().plan(_decision(target="checkout"), ts=TS)

    assert control is not None
    assert control.plan.target_service == "checkout"


def test_a_decision_that_did_not_decide_to_act_earns_no_plan() -> None:
    for action in (
        DecisionAction.SUPPRESS,
        DecisionAction.ALERT,
        DecisionAction.ESCALATE_TO_HUMAN,
    ):
        assert _planner().plan(_decision(action=action), ts=TS) is None


def test_a_decision_that_names_no_target_cannot_exist_to_be_planned() -> None:
    """The planner's target guard is unreachable, and that is the stronger claim.

    An acting decision with no target is refused by the decision contract
    itself, so a plan aimed at a guess cannot be built even by a caller trying
    to. The guard in the planner stays as a second floor under that.
    """
    with pytest.raises(ValueError, match="requires an evidence-computed target"):
        _decision(target=None)


def test_a_rung_whose_adapter_is_absent_is_refused_rather_than_substituted() -> None:
    """A rung the platform cannot carry out is not an action it may offer."""
    # 0.95 clears every rung, so the ladder reaches for one this planner has no
    # adapter for rather than quietly settling for the simulated one.
    assert _planner().plan(_decision(confidence=0.95), ts=TS) is None


def test_an_adapter_that_refuses_to_plan_produces_no_half_formed_control() -> None:
    class _Refusing(SimulatedActuator):
        def plan(
            self,
            decision: Decision,
            *,
            action_kind: ActionKind,
            parameters: Mapping[str, ActionParameterValue] | None = None,
            ts: datetime,
        ) -> ActionPlan:
            raise ActionRejectedError("this adapter cannot aim at that target")

    assert _planner(_Refusing()).plan(_decision(), ts=TS) is None


def test_the_approval_requirement_is_the_gates_and_is_only_ever_widened() -> None:
    control = _planner().plan(_decision(requires_human_approval=True), ts=TS)

    assert control is not None
    assert control.rung.requires_human_approval
    assert control.plan.requires_human_approval
    assert control.state is ActionControlState.AWAITING_APPROVAL, (
        "a plan needing a person cannot be requested for apply"
    )


def test_a_live_plan_is_always_the_first_revision() -> None:
    """A second plan for one incident would be a competing statement of evidence."""
    first = _planner().plan(_decision(), ts=TS)
    second = _planner().plan(_decision(), ts=TS)

    assert first is not None and second is not None
    assert first.plan_revision == second.plan_revision == LIVE_PLAN_REVISION
    assert first == second, "the same evidence at the same moment must freeze identically"


@pytest.mark.parametrize(
    "verdict_class",
    [VerdictClass.ATTACK, VerdictClass.OPERATIONAL_FAULT, VerdictClass.CODE_CONFIG_FAULT],
)
def test_every_actionable_class_reaches_a_committed_ladder(verdict_class: VerdictClass) -> None:
    control = _planner().plan(_decision(verdict_class=verdict_class, confidence=0.1), ts=TS)

    assert control is not None, f"{verdict_class.value} has no committed weakest rung"
    assert control.rung.actuator is ActuatorKind.SIMULATED
