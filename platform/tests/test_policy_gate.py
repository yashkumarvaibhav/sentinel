"""The policy gate: rules propose, guards restrict, floors raise, windows override."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from common.config import load_config
from contracts import (
    REQUIRED_CHECKS,
    CheckOutcome,
    Decision,
    DecisionAction,
    FusionStatus,
    Incident,
    IncidentSeverity,
    IncidentState,
    SuppressionKind,
    SymptomKind,
    Verdict,
    VerdictClass,
    Verification,
    VerificationCheck,
)
from decision import PolicyGate
from decision.config import ActionPolicyConfig, DecisionConfigLoadError, load_action_policy
from tests.factories import EPOCH

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
POLICY_PATH = CONFIG_ROOT / "policies" / "action-policy.yml"
TICK = EPOCH + timedelta(seconds=600.0)


def _policy() -> ActionPolicyConfig:
    return load_action_policy(POLICY_PATH)


def _gate(policy: ActionPolicyConfig | None = None) -> PolicyGate:
    return PolicyGate(
        configuration=policy if policy is not None else _policy(),
        topology=load_config(CONFIG_ROOT).topology,
    )


def _incident(
    *,
    incident_id: str = "incident-1",
    state: IncidentState = IncidentState.OPEN,
    severity: IncidentSeverity = IncidentSeverity.LOW,
    services: tuple[str, ...] = ("payment",),
    origin: str | None = "payment",
    business_impact: float | None = None,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        anchor_episode_id="episode-1",
        opened_ts=EPOCH,
        last_activity_ts=TICK,
        state=state,
        severity=severity,
        services=services,
        kinds=(SymptomKind.EDGE_DEGRADED,),
        episode_ids=("episode-1",),
        business_impact=business_impact,
        origin_service=origin,
        origin_confidence=None if origin is None else 0.8,
        revision=1,
        note="one incident under test",
    )


def _verdict(
    verdict_class: VerdictClass,
    confidence: float,
    *,
    verdict_id: str = "verdict-1",
) -> Verdict:
    mass = {member.value: 0.1 for member in VerdictClass}
    mass[verdict_class.value] = 1.0 - 0.1 * (len(VerdictClass) - 1)
    return Verdict(
        verdict_id=verdict_id,
        ts=TICK,
        verdict_class=verdict_class,
        rule_id="rule-under-test",
        confidence=confidence,
        reason="stub verdict for the gate",
        distribution=mass,
    )


def _verification(*, confirmed: bool = True, incident_id: str = "incident-1") -> Verification:
    checks = tuple(
        VerificationCheck(
            name=name,
            outcome=(
                CheckOutcome.PASSED
                if confirmed or name != "temporal_causality"
                else CheckOutcome.FAILED
            ),
            detail="stub check",
        )
        for name in REQUIRED_CHECKS
    )
    return Verification(
        verification_id=f"verification-{incident_id}-{confirmed}",
        ts=TICK,
        incident_id=incident_id,
        confirmed=confirmed,
        checks=checks,
    )


def _decide(
    gate: PolicyGate,
    incident: Incident,
    *,
    verdict: Verdict | None,
    confirmed: bool = True,
    status: FusionStatus = FusionStatus.DECIDED,
    ts: Any = TICK,
) -> Decision:
    return gate.decide(
        incident,
        ts=ts,
        verdict=verdict,
        verification=_verification(confirmed=confirmed, incident_id=incident.incident_id),
        fusion_status=status,
    )


def _policy_document() -> dict[str, Any]:
    """A minimal policy the validator accepts, for mutation in failure tests."""
    return {
        "version": 1,
        "rules": [
            {
                "rule_id": "act-on-a-fault",
                "action": "ACT",
                "verdict_classes": ["OPERATIONAL_FAULT"],
                "reason": "a confirmed fault is remediable",
            },
            {
                "rule_id": "catch-all",
                "action": "ESCALATE_TO_HUMAN",
                "reason": "anything else is a person's call",
            },
        ],
        "floors": [],
        "guards": {
            "require_verification": True,
            "require_named_origin": True,
            "require_active_incident": True,
            "minimum_act_confidence": 0.75,
            "maximum_affected_services": 3,
            "fallback_action": "ESCALATE_TO_HUMAN",
        },
        "approval": {"severities": ["CRITICAL"]},
        "suppressions": [],
    }


def test_the_shipped_policy_validates_and_fingerprints_stably() -> None:
    policy = _policy()
    assert policy.version == 1
    assert policy.fingerprint == _policy().fingerprint
    assert policy.rules[-1].requires_nothing


def test_a_verified_attack_is_contained_and_a_person_is_brought_in() -> None:
    gate = _gate()
    decision = _decide(
        gate,
        _incident(severity=IncidentSeverity.HIGH),
        verdict=_verdict(VerdictClass.ATTACK, 0.90),
    )
    assert decision.action is DecisionAction.AUTO_CONTAIN_THEN_ESCALATE
    assert decision.rule_id == "contain-a-verified-attack"
    # The target is the collapsed origin and nothing else.
    assert decision.target_service == "payment"
    assert decision.requires_human_approval
    assert decision.approval_reasons


def test_an_unconfirmed_attack_is_never_contained_autonomously() -> None:
    gate = _gate()
    decision = _decide(
        gate,
        _incident(),
        verdict=_verdict(VerdictClass.ATTACK, 0.90),
        confirmed=False,
    )
    assert decision.action is DecisionAction.ESCALATE_TO_HUMAN
    assert decision.target_service is None
    assert decision.confirmed is False


def test_a_confirmed_confident_fault_is_remediated_autonomously() -> None:
    gate = _gate()
    decision = _decide(
        gate,
        _incident(),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.85),
    )
    assert decision.action is DecisionAction.ACT
    assert decision.target_service == "payment"
    assert decision.guards_applied == ()
    # Nothing about this incident is a person's call, so the platform proceeds.
    assert decision.requires_human_approval is False


def test_a_critical_severity_action_is_held_for_a_person() -> None:
    gate = _gate()
    decision = _decide(
        gate,
        _incident(severity=IncidentSeverity.CRITICAL, business_impact=0.90),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.85),
    )
    # Critical severity is measured user impact, so the action still stands -
    # it is simply not the platform's to take unsupervised.
    assert decision.action is DecisionAction.ACT
    assert decision.requires_human_approval
    assert any("CRITICAL" in reason for reason in decision.approval_reasons)


def test_telling_a_person_is_not_an_approvable_event() -> None:
    decision = _decide(
        _gate(),
        _incident(severity=IncidentSeverity.MEDIUM, business_impact=0.40),
        verdict=None,
        status=FusionStatus.INSUFFICIENT,
    )
    assert decision.action is DecisionAction.ALERT
    assert decision.requires_human_approval is False
    assert decision.approval_reasons == ()


def test_a_fault_below_the_confidence_floor_is_handed_over() -> None:
    gate = _gate()
    decision = _decide(
        gate,
        _incident(),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.60),
    )
    assert decision.action is DecisionAction.ESCALATE_TO_HUMAN
    assert decision.rule_id == "hand-over-a-fault-we-cannot-confirm"


def test_an_action_without_a_computed_target_is_refused() -> None:
    gate = _gate()
    decision = _decide(
        gate,
        _incident(origin=None),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.95),
    )
    assert decision.action is DecisionAction.ESCALATE_TO_HUMAN
    assert "require_named_origin" in decision.guards_applied
    assert decision.target_service is None


def test_an_incident_whose_symptoms_have_closed_is_not_acted_on() -> None:
    gate = _gate()
    decision = _decide(
        gate,
        _incident(state=IncidentState.MONITORING),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.95),
    )
    assert decision.action is DecisionAction.ESCALATE_TO_HUMAN
    assert "require_active_incident" in decision.guards_applied


def test_a_storm_wider_than_the_blast_radius_cap_is_not_acted_on() -> None:
    gate = _gate()
    decision = _decide(
        gate,
        _incident(services=("payment", "checkout", "frontend", "cart")),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.95),
    )
    assert decision.action is DecisionAction.ESCALATE_TO_HUMAN
    assert "maximum_affected_services" in decision.guards_applied


def test_a_policy_cannot_authorise_an_action_with_no_diagnosis() -> None:
    """An acting rule that asks for no verdict class still cannot act without one."""
    document = _policy_document()
    document["rules"][0] = {
        "rule_id": "act-on-anything-open",
        "action": "ACT",
        "incident_states": ["OPEN"],
        "reason": "a deliberately over-broad rule",
    }
    gate = _gate(ActionPolicyConfig.model_validate(document))

    decision = _decide(gate, _incident(), verdict=None, status=FusionStatus.INSUFFICIENT)

    assert decision.action is DecisionAction.ESCALATE_TO_HUMAN
    assert "require_verdict" in decision.guards_applied


def test_the_two_refusals_take_opposite_decisions() -> None:
    quiet = _decide(_gate(), _incident(), verdict=None, status=FusionStatus.NO_EVIDENCE)
    assert quiet.action is DecisionAction.SUPPRESS

    unnamed = _decide(_gate(), _incident(), verdict=None, status=FusionStatus.INSUFFICIENT)
    assert unnamed.action is DecisionAction.ALERT
    assert unnamed.rule_id == "tell-someone-what-we-cannot-name"


def test_a_resolved_incident_needs_nothing() -> None:
    decision = _decide(
        _gate(),
        _incident(state=IncidentState.RESOLVED),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.95),
    )
    assert decision.action is DecisionAction.SUPPRESS
    assert decision.rule_id == "resolved-incident-needs-nothing"
    assert decision.requires_human_approval is False


def test_a_calm_diagnosis_with_measured_user_impact_is_never_suppressed() -> None:
    """The known EXPECTED_EVENT false calm: every axis quiet, users still hurting."""
    gate = _gate()
    incident = _incident(
        severity=IncidentSeverity.MEDIUM,
        business_impact=0.45,
    )
    decision = _decide(gate, incident, verdict=_verdict(VerdictClass.EXPECTED_EVENT, 0.55))
    assert decision.action is DecisionAction.ALERT
    assert "measured-user-impact-is-never-silent" in decision.floors_applied
    # The floor raised attention; it did not invent a diagnosis or an action.
    assert decision.verdict_class is VerdictClass.EXPECTED_EVENT
    assert decision.target_service is None


def test_an_unmeasured_impact_does_not_satisfy_an_impact_floor() -> None:
    gate = _gate()
    decision = _decide(
        gate,
        _incident(business_impact=None),
        verdict=_verdict(VerdictClass.EXPECTED_EVENT, 0.55),
    )
    assert decision.action is DecisionAction.SUPPRESS
    assert "measured-user-impact-is-never-silent" not in decision.floors_applied


def test_the_decision_is_taken_on_the_incidents_peak_not_the_calm_tick() -> None:
    gate = _gate()
    incident = _incident()
    peak = _decide(gate, incident, verdict=_verdict(VerdictClass.ATTACK, 0.90))
    assert peak.action is DecisionAction.AUTO_CONTAIN_THEN_ESCALATE

    later = TICK + timedelta(seconds=60.0)
    calm = gate.decide(
        incident,
        ts=later,
        verdict=None,
        verification=_verification(),
        fusion_status=FusionStatus.NO_EVIDENCE,
    )
    assert calm.action is DecisionAction.AUTO_CONTAIN_THEN_ESCALATE
    assert calm.verdict_class is VerdictClass.ATTACK
    # The evidence is honestly dated to the tick it was measured at.
    assert calm.evidence_ts == TICK
    assert calm.ts == later


def test_a_peak_is_forgotten_once_the_incident_resolves() -> None:
    gate = _gate()
    incident = _incident()
    _decide(gate, incident, verdict=_verdict(VerdictClass.ATTACK, 0.90))
    assert gate.peak(incident.incident_id) is not None

    resolved = _decide(
        gate,
        _incident(state=IncidentState.RESOLVED),
        verdict=None,
        status=FusionStatus.NO_EVIDENCE,
    )
    assert resolved.action is DecisionAction.SUPPRESS
    assert gate.peak(incident.incident_id) is None


def test_the_peak_keeps_the_worst_severity_and_the_strongest_verdict() -> None:
    gate = _gate()
    _decide(
        gate,
        _incident(severity=IncidentSeverity.CRITICAL),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.60),
    )
    decision = gate.decide(
        _incident(severity=IncidentSeverity.LOW),
        ts=TICK + timedelta(seconds=30.0),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.50, verdict_id="verdict-2"),
        verification=_verification(),
        fusion_status=FusionStatus.DECIDED,
    )
    assert decision.severity is IncidentSeverity.CRITICAL
    assert decision.confidence == pytest.approx(0.60)
    assert decision.verdict_id == "verdict-1"


def test_a_change_freeze_stops_an_action_and_records_who_owns_it() -> None:
    document = _policy_document()
    document["suppressions"] = [
        {
            "window_id": "q3-change-freeze",
            "kind": "CHANGE_FREEZE",
            "owner": "platform-oncall",
            "reason": "quarterly freeze",
            "starts_ts": TICK - timedelta(hours=1),
            "expires_ts": TICK + timedelta(hours=1),
        }
    ]
    gate = _gate(ActionPolicyConfig.model_validate(document))
    decision = _decide(gate, _incident(), verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.95))
    assert decision.action is DecisionAction.ESCALATE_TO_HUMAN
    assert decision.suppression is not None
    assert decision.suppression.owner == "platform-oncall"
    assert decision.suppression.kind is SuppressionKind.CHANGE_FREEZE
    assert any("q3-change-freeze" in reason for reason in decision.approval_reasons)


def test_an_expired_change_freeze_stops_applying_by_itself() -> None:
    document = _policy_document()
    document["suppressions"] = [
        {
            "window_id": "expired-freeze",
            "kind": "CHANGE_FREEZE",
            "owner": "platform-oncall",
            "reason": "last quarter's freeze",
            "starts_ts": TICK - timedelta(hours=4),
            "expires_ts": TICK - timedelta(hours=1),
        }
    ]
    gate = _gate(ActionPolicyConfig.model_validate(document))
    decision = _decide(gate, _incident(), verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.95))
    assert decision.action is DecisionAction.ACT
    assert decision.suppression is None


def test_a_maintenance_window_caps_the_disruption_it_covers() -> None:
    document = _policy_document()
    document["suppressions"] = [
        {
            "window_id": "payment-migration",
            "kind": "MAINTENANCE",
            "owner": "payments-team",
            "reason": "gateway migration, degradation expected",
            "starts_ts": TICK - timedelta(hours=1),
            "expires_ts": TICK + timedelta(hours=1),
            "services": ["payment"],
            "applies_to_classes": ["OPERATIONAL_FAULT"],
            "maximum_action": "ALERT",
        }
    ]
    policy = ActionPolicyConfig.model_validate(document)
    covered = _decide(
        _gate(policy), _incident(), verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.95)
    )
    assert covered.action is DecisionAction.ALERT
    assert covered.suppression is not None

    # A service the window does not name is not covered by it.
    elsewhere = _decide(
        _gate(policy),
        _incident(services=("cart",), origin="cart"),
        verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.95),
    )
    assert elsewhere.action is DecisionAction.ACT
    assert elsewhere.suppression is None


def test_a_maintenance_window_may_never_cover_a_hostile_diagnosis() -> None:
    document = _policy_document()
    document["suppressions"] = [
        {
            "window_id": "open-invitation",
            "kind": "MAINTENANCE",
            "owner": "someone",
            "reason": "quiet please",
            "starts_ts": TICK,
            "expires_ts": TICK + timedelta(hours=1),
            "services": ["payment"],
            "applies_to_classes": ["OPERATIONAL_FAULT", "ATTACK"],
            "maximum_action": "ALERT",
        }
    ]
    with pytest.raises(ValidationError, match="may never cover ATTACK"):
        ActionPolicyConfig.model_validate(document)


def test_a_floor_may_never_create_an_action() -> None:
    document = _policy_document()
    document["floors"] = [
        {
            "floor_id": "sneaky-authority",
            "minimum_action": "ACT",
            "minimum_severity": "LOW",
            "reason": "raise everything to an action",
        }
    ]
    with pytest.raises(ValidationError, match="never create an action"):
        ActionPolicyConfig.model_validate(document)


def test_a_guard_fallback_may_never_touch_production() -> None:
    document = _policy_document()
    document["guards"]["fallback_action"] = "ACT"
    with pytest.raises(ValidationError, match="must not itself touch production"):
        ActionPolicyConfig.model_validate(document)


def test_the_ladder_must_end_in_a_catch_all() -> None:
    document = _policy_document()
    document["rules"] = document["rules"][:1]
    with pytest.raises(ValidationError, match="last rule must match every situation"):
        ActionPolicyConfig.model_validate(document)


def test_a_catch_all_may_not_shadow_the_rest_of_the_ladder() -> None:
    document = _policy_document()
    shadow = {**document["rules"][1], "rule_id": "shadowing-catch-all"}
    document["rules"] = [shadow, *document["rules"]]
    with pytest.raises(ValidationError, match="only the last rule may match everything"):
        ActionPolicyConfig.model_validate(document)


def test_a_window_must_expire_after_it_starts() -> None:
    document = _policy_document()
    document["suppressions"] = [
        {
            "window_id": "backwards",
            "kind": "CHANGE_FREEZE",
            "owner": "platform-oncall",
            "reason": "freeze",
            "starts_ts": TICK,
            "expires_ts": TICK - timedelta(hours=1),
        }
    ]
    with pytest.raises(ValidationError, match="must expire after it starts"):
        ActionPolicyConfig.model_validate(document)


def test_a_blanket_maintenance_window_is_refused() -> None:
    document = _policy_document()
    document["suppressions"] = [
        {
            "window_id": "everything-everywhere",
            "kind": "MAINTENANCE",
            "owner": "someone",
            "reason": "quiet please",
            "starts_ts": TICK,
            "expires_ts": TICK + timedelta(hours=1),
            "applies_to_classes": ["OPERATIONAL_FAULT"],
            "maximum_action": "ALERT",
        }
    ]
    with pytest.raises(ValidationError, match="must name the services it covers"):
        ActionPolicyConfig.model_validate(document)


def test_the_contract_refuses_an_action_it_cannot_back() -> None:
    common: dict[str, Any] = {
        "decision_id": "decision-1",
        "ts": TICK,
        "incident_id": "incident-1",
        "rule_id": "rule-1",
        "reason": "stub",
        "evidence_ts": TICK,
        "severity": IncidentSeverity.LOW,
        "verification_id": "verification-1",
        "requires_human_approval": True,
        "approval_reasons": ("a person is in the loop",),
        "verdict_class": VerdictClass.OPERATIONAL_FAULT,
        "verdict_id": "verdict-1",
        "confidence": 0.9,
    }
    with pytest.raises(ValidationError, match="requires a confirmed verification"):
        Decision(**common, action=DecisionAction.ACT, confirmed=False, target_service="payment")
    with pytest.raises(ValidationError, match="requires an evidence-computed target service"):
        Decision(**common, action=DecisionAction.ACT, confirmed=True, target_service=None)


def test_approval_is_derived_from_its_own_reasons() -> None:
    with pytest.raises(ValidationError, match="requires_human_approval must be true exactly"):
        Decision(
            decision_id="decision-1",
            ts=TICK,
            incident_id="incident-1",
            action=DecisionAction.ALERT,
            rule_id="rule-1",
            reason="stub",
            evidence_ts=TICK,
            severity=IncidentSeverity.LOW,
            confirmed=True,
            verification_id="verification-1",
            requires_human_approval=True,
            approval_reasons=(),
        )


def test_a_decision_cannot_be_taken_on_evidence_from_the_future() -> None:
    with pytest.raises(ValidationError, match="evidence from the future"):
        Decision(
            decision_id="decision-1",
            ts=TICK,
            incident_id="incident-1",
            action=DecisionAction.ALERT,
            rule_id="rule-1",
            reason="stub",
            evidence_ts=TICK + timedelta(seconds=1),
            severity=IncidentSeverity.LOW,
            confirmed=True,
            verification_id="verification-1",
            requires_human_approval=False,
        )


def test_the_gate_is_deterministic_and_clock_free() -> None:
    first = _decide(_gate(), _incident(), verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.85))
    second = _decide(_gate(), _incident(), verdict=_verdict(VerdictClass.OPERATIONAL_FAULT, 0.85))
    assert first.decision_id == second.decision_id
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_the_gate_refuses_a_verification_of_a_different_incident() -> None:
    gate = _gate()
    with pytest.raises(ValueError, match="must belong to the incident"):
        gate.decide(
            _incident(incident_id="incident-1"),
            ts=TICK,
            verdict=None,
            verification=_verification(incident_id="incident-2"),
            fusion_status=FusionStatus.NO_EVIDENCE,
        )


def test_a_missing_policy_file_fails_closed() -> None:
    with pytest.raises(DecisionConfigLoadError, match="required configuration file is missing"):
        load_action_policy(CONFIG_ROOT / "policies" / "not-a-policy.yml")
