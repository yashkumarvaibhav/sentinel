"""Grading the decision plane against a scenario's answer key."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from lab.scenarios.models import DecisionLabelInterval
from lab.scoring.decision_gate import evaluate_decision_gates
from lab.scoring.decision_score import DecisionScore, resolve_windows, score_decisions
from lab.scoring.decisions import DecisionReplay
from lab.scoring.gates import load_gate_config

from contracts import (
    REQUIRED_CHECKS,
    CheckOutcome,
    Decision,
    DecisionAction,
    FusionStatus,
    Incident,
    IncidentSeverity,
    IncidentState,
    SymptomKind,
    Verdict,
    VerdictClass,
    Verification,
    VerificationCheck,
)
from decision import DecisionTick, IncidentOutcome
from tests.factories import EPOCH

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPO_ROOT / "lab" / "scenarios"
GATE_CONFIG = REPO_ROOT / "lab" / "scoring" / "config.yml"


def _labels(
    *,
    residual: dict[str, tuple[float, float]] | None = None,
    symptom: dict[str, tuple[float, float, str]] | None = None,
) -> dict[str, Any]:
    """A capture's measured evidence, split the way a real labels.json is."""
    return {
        "version": 1,
        "intervals": [
            {"label_id": name, "start_offset_seconds": start, "end_offset_seconds": end}
            for name, (start, end) in (residual or {}).items()
        ],
        "symptom_intervals": [
            {
                "label_id": name,
                "kind": "EDGE_DEGRADED",
                "service": service,
                "signal": "dependency.payment",
                "start_offset_seconds": start,
                "end_offset_seconds": end,
            }
            for name, (start, end, service) in (symptom or {}).items()
        ],
    }


def _verification(incident_id: str) -> Verification:
    return Verification(
        verification_id=f"verification-{incident_id}",
        ts=EPOCH,
        incident_id=incident_id,
        confirmed=True,
        checks=tuple(
            VerificationCheck(name=name, outcome=CheckOutcome.PASSED, detail="stub")
            for name in REQUIRED_CHECKS
        ),
    )


def _verdict(verdict_class: VerdictClass) -> Verdict:
    mass = {member.value: 0.1 for member in VerdictClass}
    mass[verdict_class.value] = 1.0 - 0.1 * (len(VerdictClass) - 1)
    return Verdict(
        verdict_id=f"verdict-{verdict_class.value}",
        ts=EPOCH,
        verdict_class=verdict_class,
        rule_id="rule-under-test",
        confidence=0.9,
        reason="stub",
        distribution=mass,
    )


def _outcome(
    *,
    action: DecisionAction,
    verdict_class: VerdictClass | None,
    origin: str | None,
    opened: float,
    last_activity: float,
    incident_id: str = "incident-1",
    service: str = "payment",
) -> IncidentOutcome:
    incident = Incident(
        incident_id=incident_id,
        anchor_episode_id="episode-1",
        opened_ts=EPOCH + timedelta(seconds=opened),
        last_activity_ts=EPOCH + timedelta(seconds=last_activity),
        state=IncidentState.OPEN,
        severity=IncidentSeverity.LOW,
        services=(service,),
        kinds=(SymptomKind.EDGE_DEGRADED,),
        episode_ids=("episode-1",),
        origin_service=origin,
        origin_confidence=None if origin is None else 0.8,
        revision=1,
        note="incident under test",
    )
    verdict = None if verdict_class is None else _verdict(verdict_class)
    acting = action in {DecisionAction.ACT, DecisionAction.AUTO_CONTAIN_THEN_ESCALATE}
    escalating = action in {
        DecisionAction.ESCALATE_TO_HUMAN,
        DecisionAction.AUTO_CONTAIN_THEN_ESCALATE,
    }
    decision = Decision(
        decision_id=f"decision-{incident_id}-{action.value}",
        ts=EPOCH + timedelta(seconds=last_activity),
        incident_id=incident_id,
        action=action,
        rule_id="rule-under-test",
        reason="stub",
        evidence_ts=EPOCH + timedelta(seconds=last_activity),
        severity=IncidentSeverity.LOW,
        confirmed=True,
        verification_id=f"verification-{incident_id}",
        requires_human_approval=escalating,
        approval_reasons=("a person is in the loop",) if escalating else (),
        verdict_class=None if verdict is None else verdict.verdict_class,
        verdict_id=None if verdict is None else verdict.verdict_id,
        confidence=None if verdict is None else verdict.confidence,
        target_service=origin if acting else None,
    )
    return IncidentOutcome(
        incident=incident,
        verdict=verdict,
        verification=_verification(incident_id),
        decision=decision,
        matches=(),
        signature=None,
    )


def _replay(*outcomes: IncidentOutcome, scenario_id: str = "cascade_night") -> DecisionReplay:
    ticks = tuple(
        DecisionTick(
            ts=outcome.decision.ts,
            assessments=(),
            fusion=type(
                "Fusion", (), {"status": FusionStatus.DECIDED, "verdict": outcome.verdict}
            )(),
            outcomes=(outcome,),
        )
        for outcome in outcomes
    )
    return DecisionReplay(
        capture_id="capture-under-test",
        scenario_id=scenario_id,
        seed=401,
        seed_purpose="development",
        anchor_ts=EPOCH,
        evaluation_end_ts=EPOCH + timedelta(seconds=200),
        covered_services=("payment", "frontend"),
        episode_count=1,
        ticks=ticks,
        detector_fingerprint="detector",
        decision_fingerprint="decision",
    )


def _score(*outcomes: IncidentOutcome, scenario_id: str = "cascade_night") -> DecisionScore:
    return score_decisions(
        _replay(*outcomes, scenario_id=scenario_id),
        scenario_root=SCENARIO_ROOT,
        labels=_labels(symptom={"checkout-payment-failure": (80.0, 120.0, "payment")}),
    )


def test_a_window_is_the_union_of_the_evidence_it_is_a_consequence_of() -> None:
    labels = _labels(residual={"first": (10.0, 20.0), "second": (50.0, 90.0)})
    label = DecisionLabelInterval(
        label_id="fault-decision",
        expectation="OPERATIONAL_FAULT",
        origin_service="payment",
        label_refs=("first", "second"),
    )

    windows = resolve_windows(labels, decision_labels=(label,), residual_service="frontend")

    assert windows[0].start_offset_seconds == 10.0
    assert windows[0].end_offset_seconds == 90.0
    assert windows[0].missing_refs == ()


def test_a_capture_that_predates_one_label_narrows_the_window_and_says_so() -> None:
    labels = _labels(residual={"first": (10.0, 20.0)})
    label = DecisionLabelInterval(
        label_id="fault-decision",
        expectation="OPERATIONAL_FAULT",
        origin_service="payment",
        label_refs=("first", "recorded-later"),
    )

    windows = resolve_windows(labels, decision_labels=(label,), residual_service="frontend")

    assert windows[0].end_offset_seconds == 20.0
    assert windows[0].missing_refs == ("recorded-later",)


def test_a_window_with_no_evidence_at_all_is_unanswerable() -> None:
    label = DecisionLabelInterval(
        label_id="fault-decision",
        expectation="OPERATIONAL_FAULT",
        origin_service="payment",
        label_refs=("nothing-here",),
    )

    with pytest.raises(ValueError, match="references no label this capture carries"):
        resolve_windows(
            _labels(residual={"other": (1.0, 2.0)}),
            decision_labels=(label,),
            residual_service="frontend",
        )


def test_a_fault_that_is_recognised_and_targeted_scores_clean() -> None:
    score = _score(
        _outcome(
            action=DecisionAction.ACT,
            verdict_class=VerdictClass.OPERATIONAL_FAULT,
            origin="payment",
            opened=85.0,
            last_activity=115.0,
        )
    )

    assert score.decision_accuracy.value == 1.0
    assert score.decision_reason_accuracy.value == 1.0
    assert score.origin_accuracy.value == 1.0
    # Acting on a labelled fault is the product working, not a false act.
    assert score.false_act_count == 0


def test_staying_silent_about_a_labelled_fault_is_a_miss() -> None:
    score = _score(
        _outcome(
            action=DecisionAction.SUPPRESS,
            verdict_class=VerdictClass.OPERATIONAL_FAULT,
            origin="payment",
            opened=85.0,
            last_activity=115.0,
        )
    )

    assert score.decision_accuracy.value == 0.0
    assert score.decision_reason_accuracy.value == 0.0


def test_naming_the_wrong_class_fails_the_reason_but_not_the_handling() -> None:
    score = _score(
        _outcome(
            action=DecisionAction.ESCALATE_TO_HUMAN,
            verdict_class=VerdictClass.ATTACK,
            origin="payment",
            opened=85.0,
            last_activity=115.0,
        )
    )

    assert score.decision_accuracy.value == 1.0
    assert score.decision_reason_accuracy.value == 0.0


def test_an_origin_the_answer_key_does_not_know_is_not_graded() -> None:
    score = score_decisions(
        _replay(
            _outcome(
                action=DecisionAction.ALERT,
                verdict_class=None,
                origin="frontend",
                service="frontend",
                opened=85.0,
                last_activity=99.0,
            ),
            scenario_id="match_night",
        ),
        scenario_root=SCENARIO_ROOT,
        labels=_labels(residual={"offset_core": (84.0, 100.0)}),
    )

    assert [outcome.origin_correct for outcome in score.windows] == [None]
    assert score.origin_accuracy.status == "insufficient"


def test_acting_on_something_nobody_understands_is_a_false_act() -> None:
    score = score_decisions(
        _replay(
            _outcome(
                action=DecisionAction.ACT,
                verdict_class=VerdictClass.OPERATIONAL_FAULT,
                origin="frontend",
                service="frontend",
                opened=85.0,
                last_activity=99.0,
            ),
            scenario_id="match_night",
        ),
        scenario_root=SCENARIO_ROOT,
        labels=_labels(residual={"offset_core": (84.0, 100.0)}),
    )

    assert score.false_act_count == 1
    assert score.false_acts[0].action is DecisionAction.ACT
    # Handling fails too: nothing may be done to a system nobody understands.
    assert score.decision_accuracy.value == 0.0


def test_claiming_a_class_over_an_unexplained_window_fails_the_reason() -> None:
    score = score_decisions(
        _replay(
            _outcome(
                action=DecisionAction.ESCALATE_TO_HUMAN,
                verdict_class=VerdictClass.ATTACK,
                origin="frontend",
                service="frontend",
                opened=85.0,
                last_activity=99.0,
            ),
            scenario_id="match_night",
        ),
        scenario_root=SCENARIO_ROOT,
        labels=_labels(residual={"offset_core": (84.0, 100.0)}),
    )

    assert score.decision_accuracy.value == 1.0
    assert score.decision_reason_accuracy.value == 0.0


def test_combination_is_accepted_only_where_the_scenario_contains_both() -> None:
    """A single-fault capture cannot score correct by calling its fault a combination."""
    single = _score(
        _outcome(
            action=DecisionAction.ESCALATE_TO_HUMAN,
            verdict_class=VerdictClass.COMBINATION,
            origin="payment",
            opened=85.0,
            last_activity=115.0,
        )
    )
    assert single.decision_reason_accuracy.value == 0.0

    both = score_decisions(
        _replay(
            _outcome(
                action=DecisionAction.ESCALATE_TO_HUMAN,
                verdict_class=VerdictClass.COMBINATION,
                origin="frontend",
                service="frontend",
                opened=130.0,
                last_activity=500.0,
            ),
            scenario_id="combo_night",
        ),
        scenario_root=SCENARIO_ROOT,
        labels=_labels(
            residual={"behavior-attack-volume": (124.0, 304.0)},
            symptom={
                "behavior-attack-path-entropy": (124.0, 304.0, "frontend"),
                "payment-failure-log-burst": (304.0, 484.0, "payment"),
                "checkout-payment-failure": (304.0, 484.0, "checkout"),
                "email-memory-saturation": (484.0, 664.0, "email"),
                "frontend-measured-rate-drop": (664.0, 724.0, "frontend"),
                "frontend-emitter-silence": (724.0, 944.0, "frontend"),
            },
        ),
    )
    attack = next(item for item in both.windows if item.window.expectation == "ATTACK")
    assert attack.reason_correct
    assert both.attack_recall.value == 1.0


def test_the_gate_fails_closed_on_a_matrix_that_proves_nothing() -> None:
    config = load_gate_config(GATE_CONFIG)
    empty = DecisionScore(
        capture_id="capture-under-test",
        scenario_id="quiet_day",
        seed=101,
        seed_purpose="development",
        windows=(),
        false_acts=(),
        decision_count=0,
    )

    result = evaluate_decision_gates((empty,), config)

    assert not result.passed
    assert {failure.metric for failure in result.failures} == {
        "decision_accuracy",
        "decision_reason_accuracy",
        "attack_recall",
        "origin_accuracy",
    }


def test_one_false_act_fails_the_gate_however_good_everything_else_is() -> None:
    config = load_gate_config(GATE_CONFIG)
    score = score_decisions(
        _replay(
            _outcome(
                action=DecisionAction.ACT,
                verdict_class=VerdictClass.OPERATIONAL_FAULT,
                origin="frontend",
                service="frontend",
                opened=85.0,
                last_activity=99.0,
            ),
            scenario_id="match_night",
        ),
        scenario_root=SCENARIO_ROOT,
        labels=_labels(residual={"offset_core": (84.0, 100.0)}),
    )

    result = evaluate_decision_gates((score,), config)

    assert not result.passed
    assert "decision_false_acts" in {failure.metric for failure in result.failures}
