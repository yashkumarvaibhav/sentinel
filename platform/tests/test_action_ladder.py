"""The graded ladders: which rung a decision earns, and how long it stands.

These tests drive the committed `config/ladders.yml` wherever the question is
"what does this configuration actually do", and a small hand-built ladder
wherever the question is "what does the code refuse". Both matter: the file is
the artifact an operator reads, and the refusals are what stop a bad one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from action import (
    LadderConfig,
    LadderError,
    NoRungAvailableError,
    RemediationLadder,
    RestraintRegistry,
    load_action_config,
    load_ladder_config,
)
from action.actuators import (
    FlagActuator,
    KubernetesActuator,
    MeshActuator,
    SimulatedActuator,
)
from contracts import (
    ActionKind,
    ActionPlan,
    ActuatorKind,
    Decision,
    DecisionAction,
    IncidentSeverity,
    VerdictClass,
)

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
LADDERS_PATH = CONFIG_DIR / "ladders.yml"
ACTION_PATH = CONFIG_DIR / "action.yml"


def _decision(
    *,
    verdict: VerdictClass = VerdictClass.ATTACK,
    confidence: float = 0.90,
    target: str = "frontend",
    action: DecisionAction = DecisionAction.ACT,
    approval: bool = False,
) -> Decision:
    return Decision(
        decision_id="decision-1",
        ts=TICK,
        incident_id="incident-1",
        action=action,
        rule_id="a-rule",
        reason="evidence the gate acted on",
        evidence_ts=TICK,
        severity=IncidentSeverity.HIGH,
        confirmed=True,
        verification_id="verification-1",
        requires_human_approval=approval,
        approval_reasons=("an operator asked for it",) if approval else (),
        verdict_class=verdict,
        verdict_id="verdict-1",
        confidence=confidence,
        target_service=target,
    )


def _committed() -> RemediationLadder:
    """The real ladders, with the real flag table behind the FLAG_FLIP resolver."""
    action = load_action_config(ACTION_PATH)
    return RemediationLadder(load_ladder_config(LADDERS_PATH), flags=action.flags)


def _ladder(**overrides: Any) -> LadderConfig:
    rungs: list[dict[str, Any]] = [
        {
            "rung_id": "watch",
            "action_kind": "OBSERVE",
            "actuator": "SIMULATED",
            "minimum_confidence": 0.0,
            "autonomous": True,
            "maximum_blast_fraction": 0.0,
            "ttl_seconds": 60,
        },
        {
            "rung_id": "restrain",
            "action_kind": "RATE_LIMIT",
            "actuator": "MESH",
            "minimum_confidence": 0.80,
            "autonomous": True,
            "maximum_blast_fraction": 0.5,
            "ttl_seconds": 120,
            "parameters": {"cohort": "general-traffic", "enforced_percent": 100},
        },
    ]
    document: dict[str, Any] = {
        "version": 1,
        "ladders": [
            {
                "ladder_id": "test-ladder",
                "verdict_classes": ["ATTACK"],
                "reason": "a stated reason",
                "rungs": rungs,
            }
        ],
    }
    document["ladders"][0].update(overrides)
    return LadderConfig.model_validate(document)


def _plan(blast: float) -> ActionPlan:
    """A real plan from a real adapter, so the cap is measured against a contract."""
    return SimulatedActuator(blast_fraction=blast).plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 2}, ts=TICK
    )


# --- what the committed configuration does ----------------------------------


def test_less_confidence_earns_a_gentler_rung_never_a_stronger_one() -> None:
    """The confidence-gated downgrade, read off the file an operator maintains."""
    ladder = _committed()
    climbed = [
        (confidence, ladder.select(_decision(confidence=confidence)).primary.action_kind)
        for confidence in (0.50, 0.72, 0.88, 0.97)
    ]

    assert climbed == [
        (0.50, ActionKind.OBSERVE),
        (0.72, ActionKind.THROTTLE),
        (0.88, ActionKind.RATE_LIMIT),
        (0.97, ActionKind.ISOLATE),
    ]


def test_an_attack_is_answered_at_the_edge_before_the_cluster() -> None:
    """Restraining the cohort comes before touching the service everyone else uses."""
    ladder = _committed()
    for confidence in (0.72, 0.88):
        choice = ladder.select(_decision(confidence=confidence)).primary
        assert choice.actuator is ActuatorKind.MESH
        assert "cohort" in choice.parameters


def test_holding_a_cohort_to_its_ceiling_adds_headroom_at_the_same_time() -> None:
    """Scaling into an attack buys the attacker throughput; pairing does not."""
    selection = _committed().select(_decision(confidence=0.88))

    assert selection.primary.action_kind is ActionKind.RATE_LIMIT
    assert selection.companion is not None
    assert selection.companion.action_kind is ActionKind.SCALE
    assert selection.companion.actuator is ActuatorKind.KUBERNETES
    assert selection.choices == (selection.primary, selection.companion)


def test_a_companion_is_never_reachable_on_its_own() -> None:
    """ "Just add headroom" must not be selectable on evidence of an attack."""
    ladder = _committed()
    for confidence in (0.0, 0.5, 0.7, 0.85, 0.86, 0.9, 1.0):
        selection = ladder.select(_decision(confidence=confidence))
        assert selection.primary.rung_id != "add-headroom-for-the-surge"


def test_the_destructive_rungs_are_never_autonomous() -> None:
    ladder = _committed()
    isolate = ladder.select(_decision(confidence=1.0)).primary
    rollback = ladder.select(
        _decision(verdict=VerdictClass.OPERATIONAL_FAULT, confidence=1.0, target="cart")
    ).primary

    assert isolate.action_kind is ActionKind.ISOLATE
    assert isolate.requires_human_approval
    assert rollback.action_kind is ActionKind.ROLLBACK
    assert rollback.requires_human_approval


def test_a_fault_undoes_the_change_before_it_rolls_the_deployment_back() -> None:
    ladder = _committed()
    climbed = [
        (
            confidence,
            ladder.select(
                _decision(
                    verdict=VerdictClass.OPERATIONAL_FAULT, confidence=confidence, target="cart"
                )
            ).primary.action_kind,
        )
        for confidence in (0.10, 0.78, 0.86, 0.95)
    ]

    assert climbed == [
        (0.10, ActionKind.OBSERVE),
        (0.78, ActionKind.SCALE),
        (0.86, ActionKind.FLAG_FLIP),
        (0.95, ActionKind.ROLLBACK),
    ]


def test_the_flag_rung_names_the_flag_committed_for_that_service() -> None:
    choice = (
        _committed()
        .select(_decision(verdict=VerdictClass.OPERATIONAL_FAULT, confidence=0.86, target="email"))
        .primary
    )

    assert choice.action_kind is ActionKind.FLAG_FLIP
    assert choice.parameters == {"flag": "emailMemoryLeak"}


def test_a_service_with_two_committed_flags_is_not_offered_the_flag_rung() -> None:
    """Which change caused it is the change feed's question, not a coin toss.

    `payment` has two committed flags, so the rung cannot name its own target and
    the ladder falls through to one it can aim rather than guessing.
    """
    selection = _committed().select(
        _decision(verdict=VerdictClass.OPERATIONAL_FAULT, confidence=0.86, target="payment")
    )

    assert selection.primary.action_kind is ActionKind.SCALE


def test_every_rung_the_committed_ladders_offer_expires() -> None:
    configuration = load_ladder_config(LADDERS_PATH)
    for ladder in configuration.ladders:
        for rung in (*ladder.rungs, *ladder.companions):
            assert rung.ttl_seconds > 0, f"{rung.rung_id} would stand forever"


def test_the_committed_ladders_only_name_adapters_that_are_enabled() -> None:
    """A rung nothing can carry out is a promise the platform cannot keep."""
    enabled = load_action_config(ACTION_PATH).enabled_actuators()
    configuration = load_ladder_config(LADDERS_PATH)

    for ladder in configuration.ladders:
        for rung in (*ladder.rungs, *ladder.companions):
            assert rung.actuator in enabled, f"{rung.rung_id} names a disabled adapter"


def test_the_committed_ladders_have_their_own_fingerprint() -> None:
    ladder = _committed()
    assert len(ladder.fingerprint) == 64
    assert ladder.fingerprint == load_ladder_config(LADDERS_PATH).fingerprint


# --- what the code refuses --------------------------------------------------


def test_a_ladder_is_climbed_only_by_a_decision_that_decided_to_act() -> None:
    ladder = RemediationLadder(_ladder())
    with pytest.raises(LadderError, match="which touches nothing"):
        ladder.select(_decision(action=DecisionAction.ALERT))


def test_a_diagnosis_with_no_committed_response_is_left_to_a_person() -> None:
    ladder = RemediationLadder(_ladder())
    with pytest.raises(LadderError, match="no ladder answers EXPECTED_EVENT"):
        ladder.select(_decision(verdict=VerdictClass.EXPECTED_EVENT))


def test_reaching_no_rung_is_an_answer_rather_than_an_invented_action() -> None:
    document = _ladder()
    stripped = LadderConfig.model_validate(
        {
            "version": 1,
            "ladders": [
                {
                    **document.ladders[0].model_dump(mode="json"),
                    "rungs": [document.ladders[0].rungs[1].model_dump(mode="json")],
                }
            ],
        }
    )
    with pytest.raises(NoRungAvailableError, match=r"restrain needs 0\.80"):
        RemediationLadder(stripped).select(_decision(confidence=0.5))


def test_a_rung_may_widen_approval_and_may_never_narrow_it() -> None:
    ladder = _committed()
    signed = ladder.select(_decision(confidence=0.72, approval=True)).primary
    assert signed.requires_human_approval, "the gate's requirement was dropped by a ladder"

    unsigned = ladder.select(_decision(confidence=0.72)).primary
    assert not unsigned.requires_human_approval


def test_a_ladder_whose_rungs_are_not_ordered_weakest_first_is_refused() -> None:
    with pytest.raises(ValueError, match="minimum confidences may not decrease"):
        _ladder(
            rungs=[
                {
                    "rung_id": "strong",
                    "action_kind": "ISOLATE",
                    "actuator": "KUBERNETES",
                    "minimum_confidence": 0.9,
                    "autonomous": False,
                    "maximum_blast_fraction": 1.0,
                    "ttl_seconds": 60,
                },
                {
                    "rung_id": "weak",
                    "action_kind": "OBSERVE",
                    "actuator": "SIMULATED",
                    "minimum_confidence": 0.1,
                    "autonomous": True,
                    "maximum_blast_fraction": 0.0,
                    "ttl_seconds": 60,
                },
            ]
        )


def test_pairing_with_something_that_is_not_a_companion_is_refused() -> None:
    with pytest.raises(ValueError, match="which is not a companion"):
        _ladder(
            rungs=[
                {
                    "rung_id": "watch",
                    "action_kind": "OBSERVE",
                    "actuator": "SIMULATED",
                    "minimum_confidence": 0.0,
                    "autonomous": True,
                    "maximum_blast_fraction": 0.0,
                    "ttl_seconds": 60,
                    "paired_with": "nothing-like-that",
                }
            ]
        )


def test_two_ladders_for_one_diagnosis_is_an_ambiguity_not_a_choice() -> None:
    document = _ladder().ladders[0].model_dump(mode="json")
    with pytest.raises(ValueError, match="is answered by both"):
        LadderConfig.model_validate(
            {
                "version": 1,
                "ladders": [document, {**document, "ladder_id": "another-ladder"}],
            }
        )


def test_an_unknown_diagnosis_in_a_ladder_is_a_typo_not_a_future_feature() -> None:
    with pytest.raises(ValueError, match="unknown verdict class"):
        _ladder(verdict_classes=["MISCHIEF"])


def test_an_observing_rung_that_claims_a_blast_radius_is_refused() -> None:
    with pytest.raises(ValueError, match="observing changes nothing"):
        _ladder(
            rungs=[
                {
                    "rung_id": "watch",
                    "action_kind": "OBSERVE",
                    "actuator": "SIMULATED",
                    "minimum_confidence": 0.0,
                    "autonomous": True,
                    "maximum_blast_fraction": 0.5,
                    "ttl_seconds": 60,
                }
            ]
        )


def test_a_resolved_rung_may_not_also_carry_literal_parameters() -> None:
    with pytest.raises(ValueError, match="one source of parameters or the other"):
        _ladder(
            rungs=[
                {
                    "rung_id": "flip",
                    "action_kind": "FLAG_FLIP",
                    "actuator": "FEATURE_FLAG",
                    "minimum_confidence": 0.5,
                    "autonomous": True,
                    "maximum_blast_fraction": 1.0,
                    "ttl_seconds": 60,
                    "parameters_from": "committed_flag",
                    "parameters": {"flag": "cartFailure"},
                }
            ]
        )


# --- the blast cap and the expiry -------------------------------------------


def test_a_rung_states_a_ceiling_the_adapter_is_measured_against() -> None:
    """The ladder owns the number; the 5.6 guard will own what to do about it."""
    choice = _committed().select(_decision(confidence=0.72)).primary
    assert choice.maximum_blast_fraction == 0.20
    assert choice.permits(_plan(0.2))
    assert not choice.permits(_plan(0.8))


def test_an_effect_is_given_back_once_it_outlives_its_rung() -> None:
    registry = RestraintRegistry()
    choice = _committed().select(_decision(confidence=0.72)).primary
    plan = _plan(0.2)
    registry.record(plan, choice, applied_at=TICK)

    assert registry.expired(TICK + choice.ttl) == (), "an effect exactly at its expiry has not"
    expired = registry.expired(TICK + choice.ttl + timedelta(seconds=1))
    assert [entry.plan.plan_id for entry in expired] == [plan.plan_id]
    assert expired[0].expires_at == TICK + choice.ttl

    registry.release(plan)
    assert registry.expired(TICK + timedelta(days=1)) == ()


def test_an_empty_registry_is_still_a_registry() -> None:
    """Defining __len__ makes an empty instance falsy; that trap cost a bug in 5.1."""
    assert bool(RestraintRegistry()) is True
    assert len(RestraintRegistry()) == 0


# --- the ladder and the adapters speak the same vocabulary ------------------


def test_every_committed_rung_produces_a_plan_its_adapter_accepts() -> None:
    """The one mistake a ladder can make that no unit test of either half catches.

    A rung states parameters in an adapter's own vocabulary, and the adapter is
    strict about that vocabulary on purpose. If the two ever drift, the failure
    lands at the worst possible moment - the first time the platform tries to act
    on a real incident. So every rung of every committed ladder is planned here,
    against the real adapters, with only the cluster stood in for.
    """
    from tests.test_action_flags import FakeProvider
    from tests.test_action_kubernetes import FakeCluster
    from tests.test_action_mesh import POD, FakeEdge

    configuration = load_action_config(ACTION_PATH)
    assert configuration.kubernetes is not None
    assert configuration.mesh is not None
    assert configuration.flags is not None
    adapters = {
        ActuatorKind.SIMULATED: SimulatedActuator(),
        ActuatorKind.KUBERNETES: KubernetesActuator(
            configuration=configuration.kubernetes,
            command=FakeCluster(answers={"get pods": "k3d-node-0"}),
        ),
        ActuatorKind.MESH: MeshActuator(configuration=configuration.mesh, command=FakeEdge()),
        ActuatorKind.FEATURE_FLAG: FlagActuator(
            configuration=configuration.flags, command=FakeProvider()
        ),
    }
    ladder = _committed()

    planned = 0
    for verdict, target in (
        (VerdictClass.ATTACK, "frontend"),
        (VerdictClass.OPERATIONAL_FAULT, "email"),
    ):
        for confidence in (0.0, 0.70, 0.75, 0.80, 0.85, 0.86, 0.90, 0.95, 1.0):
            decision = _decision(verdict=verdict, confidence=confidence, target=target)
            for choice in ladder.select(decision).choices:
                plan = adapters[choice.actuator].plan(
                    decision,
                    action_kind=choice.action_kind,
                    parameters=choice.parameters,
                    ts=TICK,
                )
                assert plan.action_kind is choice.action_kind
                assert plan.target_service == target
                if choice.actuator is ActuatorKind.MESH:
                    assert POD in plan.target_ref
                planned += 1

    assert planned >= 18, "the sweep did not actually reach every rung"
