"""Grading what the live producer stored, rather than what a replay concludes."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from lab.scoring.decision_gate import evaluate_decision_gates
from lab.scoring.gates import load_gate_config
from lab.scoring.live_gate import evaluate_live_run, render_live_run_report
from lab.scoring.live_score import (
    LiveRunScoringError,
    RunBounds,
    score_live_run,
    stored_judgements,
)

from common.storage.models import IncidentRecord, LiveProducerCheckpoint
from contracts import (
    DecisionAction,
    IncidentActionState,
    IncidentConfidence,
    IncidentConfidenceStatus,
    IncidentFeedItem,
    IncidentSeverity,
    IncidentState,
    VerdictClass,
)
from tests.factories import EPOCH

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPO_ROOT / "lab" / "scenarios"
GATE_CONFIG = REPO_ROOT / "lab" / "scoring" / "config.yml"

PRODUCER_ID = "sentinel-live-producer"
RUN_SECONDS = 1004.0


def _bounds(*, seed_purpose: str = "development") -> RunBounds:
    return RunBounds(
        capture_id="live-run-under-test",
        scenario_id="combo_night",
        seed=503,
        seed_purpose=seed_purpose,  # type: ignore[arg-type]
        anchor_ts=EPOCH,
        end_ts=EPOCH + timedelta(seconds=RUN_SECONDS),
    )


def _labels() -> dict[str, Any]:
    """The measured answer key combo_night's decision windows point at."""
    return {
        "version": 1,
        "intervals": [
            {
                "label_id": "behavior-attack-volume",
                "start_offset_seconds": 124.0,
                "end_offset_seconds": 304.0,
            }
        ],
        "symptom_intervals": [
            {
                "label_id": "behavior-attack-path-entropy",
                "kind": "RATIO_DEFORM",
                "service": "frontend",
                "signal": "path_entropy",
                "start_offset_seconds": 124.0,
                "end_offset_seconds": 304.0,
            },
            {
                "label_id": "payment-failure-log-burst",
                "kind": "LOG_BURST",
                "service": "payment",
                "signal": "log_template_rate",
                "start_offset_seconds": 304.0,
                "end_offset_seconds": 484.0,
            },
            {
                "label_id": "checkout-payment-failure",
                "kind": "EDGE_DEGRADED",
                "service": "checkout",
                "signal": "dependency.payment",
                "start_offset_seconds": 304.0,
                "end_offset_seconds": 484.0,
            },
            {
                "label_id": "frontend-checkout-propagation",
                "kind": "EDGE_DEGRADED",
                "service": "frontend",
                "signal": "dependency.checkout",
                "start_offset_seconds": 304.0,
                "end_offset_seconds": 484.0,
            },
            {
                "label_id": "email-memory-saturation",
                "kind": "SATURATION",
                "service": "email",
                "signal": "container_memory",
                "start_offset_seconds": 484.0,
                "end_offset_seconds": 664.0,
            },
            {
                "label_id": "frontend-measured-rate-drop",
                "kind": "DROP",
                "service": "frontend",
                "signal": "request_rate",
                "start_offset_seconds": 664.0,
                "end_offset_seconds": 724.0,
            },
            {
                "label_id": "frontend-emitter-silence",
                "kind": "SILENCE",
                "service": "frontend",
                "signal": "request_rate",
                "start_offset_seconds": 724.0,
                "end_offset_seconds": 944.0,
            },
        ],
    }


def _card(
    *,
    incident_id: str,
    opened: float,
    updated: float,
    services: tuple[str, ...],
    verdict_class: VerdictClass | None,
    action: DecisionAction,
    origin: str | None = None,
    state: IncidentState = IncidentState.OPEN,
) -> IncidentFeedItem:
    return IncidentFeedItem(
        incident_id=incident_id,
        opened_at=EPOCH + timedelta(seconds=opened),
        updated_at=EPOCH + timedelta(seconds=updated),
        state=state,
        severity=IncidentSeverity.HIGH,
        services=services,
        origin_service=origin,
        verdict_class=verdict_class,
        reason="stub reason recorded by the live producer.",
        evidence=(),
        action=IncidentActionState(
            decision_action=action,
            effect_status=None,
            detail="stub action detail.",
        ),
        confidence=IncidentConfidence(
            status=IncidentConfidenceStatus.INSUFFICIENT,
            value=None,
            note="Runtime rule confidence is not calibrated.",
        ),
        muted=False,
        explanation=None,
        honesty="REAL",
    )


def _record(item: IncidentFeedItem) -> IncidentRecord:
    return IncidentRecord(
        incident_id=item.incident_id,
        state=item.state.value,
        created_at=item.opened_at,
        updated_at=item.updated_at,
        payload=item.model_dump(mode="json"),
    )


def _checkpoint(
    *,
    anchor_offset: float = -120.0,
    tick_offset: float = RUN_SECONDS + 60.0,
    published: int = 5,
) -> LiveProducerCheckpoint:
    return LiveProducerCheckpoint(
        producer_id=PRODUCER_ID,
        anchor_ts=EPOCH + timedelta(seconds=anchor_offset),
        tick_ts=EPOCH + timedelta(seconds=tick_offset),
        published_incidents=published,
    )


def _clean_run() -> tuple[IncidentRecord, ...]:
    """Stored rows that answer every combo_night window the way it must be answered."""
    return (
        _record(
            _card(
                incident_id="incident-attack",
                opened=130.0,
                updated=300.0,
                services=("frontend",),
                verdict_class=VerdictClass.ATTACK,
                action=DecisionAction.ESCALATE_TO_HUMAN,
                origin="frontend",
            )
        ),
        _record(
            _card(
                incident_id="incident-payment",
                opened=310.0,
                updated=480.0,
                services=("payment", "checkout", "frontend"),
                verdict_class=VerdictClass.OPERATIONAL_FAULT,
                action=DecisionAction.ESCALATE_TO_HUMAN,
                origin="payment",
            )
        ),
        _record(
            _card(
                incident_id="incident-email",
                opened=490.0,
                updated=660.0,
                services=("email",),
                verdict_class=VerdictClass.OPERATIONAL_FAULT,
                action=DecisionAction.ESCALATE_TO_HUMAN,
                origin="email",
            )
        ),
        _record(
            _card(
                incident_id="incident-drop",
                opened=670.0,
                updated=720.0,
                services=("frontend",),
                verdict_class=None,
                action=DecisionAction.ESCALATE_TO_HUMAN,
            )
        ),
        _record(
            _card(
                incident_id="incident-silence",
                opened=730.0,
                updated=940.0,
                services=("frontend",),
                verdict_class=VerdictClass.OPERATIONAL_FAULT,
                action=DecisionAction.ESCALATE_TO_HUMAN,
                origin="frontend",
            )
        ),
    )


def _score(records: tuple[IncidentRecord, ...], **kwargs: Any) -> Any:
    return score_live_run(
        records,
        kwargs.pop("checkpoint", _checkpoint()),
        bounds=kwargs.pop("bounds", _bounds()),
        scenario_root=SCENARIO_ROOT,
        labels=_labels(),
        **kwargs,
    )


def test_a_run_the_producer_stored_correctly_scores_clean() -> None:
    score = _score(_clean_run())

    gate = evaluate_live_run(score, load_gate_config(GATE_CONFIG))

    assert gate.passed
    assert gate.decision_accuracy.value == 1.0
    assert gate.decision_reason_accuracy.value == 1.0
    assert gate.attack_recall.value == 1.0
    assert gate.origin_accuracy.value == 1.0
    assert gate.false_act_count == 0


def test_acting_on_an_unexplained_drop_is_both_mishandling_and_a_false_act() -> None:
    """Nothing may be done to a system nobody understands, and the count says so.

    An UNEXPLAINED window names no class, so an action inside it is not excused
    by the window existing: it is a real autonomous act on a service that was
    working perfectly, which is the exact mistake the safety count exists for.
    """
    records = tuple(
        _record(
            _card(
                incident_id="incident-drop",
                opened=670.0,
                updated=720.0,
                services=("frontend",),
                verdict_class=None,
                action=DecisionAction.ACT,
            )
        )
        if record.incident_id == "incident-drop"
        else record
        for record in _clean_run()
    )

    score = _score(records)

    drop = next(
        outcome
        for outcome in score.score.windows
        if outcome.window.label_id == "measured-drop-decision"
    )
    assert drop.acted
    assert not drop.handled_correctly
    assert score.score.false_act_count == 1
    assert score.score.false_acts[0].incident_id == "incident-drop"
    assert not evaluate_live_run(score, load_gate_config(GATE_CONFIG)).passed


def test_naming_a_class_over_an_unexplained_window_fails_the_reason() -> None:
    records = tuple(
        _record(
            _card(
                incident_id="incident-drop",
                opened=670.0,
                updated=720.0,
                services=("frontend",),
                verdict_class=VerdictClass.OPERATIONAL_FAULT,
                action=DecisionAction.ESCALATE_TO_HUMAN,
                origin="frontend",
            )
        )
        if record.incident_id == "incident-drop"
        else record
        for record in _clean_run()
    )

    score = _score(records)

    drop = next(
        outcome
        for outcome in score.score.windows
        if outcome.window.label_id == "measured-drop-decision"
    )
    assert drop.handled_correctly
    assert not drop.reason_correct


def test_an_action_where_no_window_names_a_class_is_a_false_act() -> None:
    quiet = _record(
        _card(
            incident_id="incident-quiet",
            opened=20.0,
            updated=60.0,
            services=("cart",),
            verdict_class=VerdictClass.OPERATIONAL_FAULT,
            action=DecisionAction.ACT,
            origin="cart",
        )
    )

    score = _score((*_clean_run(), quiet))

    assert score.score.false_act_count == 1
    assert score.score.false_acts[0].incident_id == "incident-quiet"
    # The card is what a decision concluded, not what an actuator was pointed
    # at, so the store genuinely does not know a target here.
    assert score.score.false_acts[0].target_service is None
    assert not evaluate_live_run(score, load_gate_config(GATE_CONFIG)).passed


def test_rows_from_other_traffic_are_not_graded_and_not_counted_as_mistakes() -> None:
    """The store is a lifetime record; only rows overlapping the run are evidence."""
    before = _record(
        _card(
            incident_id="incident-yesterday",
            opened=-4000.0,
            updated=-3000.0,
            services=("cart",),
            verdict_class=VerdictClass.OPERATIONAL_FAULT,
            action=DecisionAction.ACT,
            origin="cart",
        )
    )
    after = _record(
        _card(
            incident_id="incident-tomorrow",
            opened=RUN_SECONDS + 500.0,
            updated=RUN_SECONDS + 900.0,
            services=("cart",),
            verdict_class=VerdictClass.OPERATIONAL_FAULT,
            action=DecisionAction.ACT,
            origin="cart",
        )
    )

    score = _score((before, *_clean_run(), after))

    assert score.stored_incidents == 7
    assert score.considered_incidents == 5
    assert score.score.false_act_count == 0
    assert evaluate_live_run(score, load_gate_config(GATE_CONFIG)).passed


def test_a_producer_that_started_after_the_run_began_is_refused() -> None:
    with pytest.raises(LiveRunScoringError, match="did not observe the whole run"):
        _score(_clean_run(), checkpoint=_checkpoint(anchor_offset=200.0))


def test_a_producer_that_stopped_before_the_run_finished_is_refused() -> None:
    with pytest.raises(LiveRunScoringError, match="did not observe the whole run"):
        _score(_clean_run(), checkpoint=_checkpoint(tick_offset=500.0))


def test_a_run_nothing_was_watching_is_refused_rather_than_scored_as_quiet() -> None:
    with pytest.raises(LiveRunScoringError, match="nothing was watching"):
        _score(_clean_run(), checkpoint=None)


def test_a_held_out_seed_is_not_spent_without_an_explicit_acknowledgement() -> None:
    with pytest.raises(LiveRunScoringError, match="spent by being scored"):
        _score(_clean_run(), bounds=_bounds(seed_purpose="held_out"))


def test_a_held_out_seed_scores_once_the_spend_is_acknowledged() -> None:
    score = _score(
        _clean_run(),
        bounds=_bounds(seed_purpose="held_out"),
        spend_held_out_seed=True,
    )

    assert score.bounds.seed_purpose == "held_out"
    assert evaluate_live_run(score, load_gate_config(GATE_CONFIG)).passed


def test_a_row_whose_payload_disagrees_with_its_own_columns_fails_closed() -> None:
    card = _card(
        incident_id="incident-attack",
        opened=130.0,
        updated=300.0,
        services=("frontend",),
        verdict_class=VerdictClass.ATTACK,
        action=DecisionAction.ESCALATE_TO_HUMAN,
        origin="frontend",
    )
    tampered = IncidentRecord(
        incident_id="incident-attack",
        state=IncidentState.RESOLVED.value,
        created_at=card.opened_at,
        updated_at=card.updated_at,
        payload=card.model_dump(mode="json"),
    )

    with pytest.raises(LiveRunScoringError, match="state disagrees"):
        stored_judgements((tampered,), bounds=_bounds())


def test_a_row_whose_payload_is_not_a_card_at_all_fails_closed() -> None:
    junk = IncidentRecord(
        incident_id="incident-junk",
        state="OPEN",
        created_at=EPOCH,
        updated_at=EPOCH,
        payload={"incident_id": "incident-junk"},
    )

    with pytest.raises(ValueError):
        stored_judgements((junk,), bounds=_bounds())


def test_stored_grading_agrees_with_the_replay_scorer_on_the_same_judgements() -> None:
    """The two scorers differ in where they read from, never in what they mean."""
    score = _score(_clean_run())

    live = evaluate_live_run(score, load_gate_config(GATE_CONFIG))
    shared = evaluate_decision_gates((score.score,), load_gate_config(GATE_CONFIG))

    assert live == shared


def test_the_report_states_the_coverage_that_dates_the_rows() -> None:
    score = _score(_clean_run())

    report = render_live_run_report(score, evaluate_live_run(score, load_gate_config(GATE_CONFIG)))

    assert "# Phase 6 live-run score" in report
    assert "Gate: PASS" in report
    assert PRODUCER_ID in report
    assert "5 of 5 stored incidents" in report
    assert "current state" in report
    assert report.endswith("\n")


def test_the_scorer_reads_no_private_label_before_the_rows_are_in_hand() -> None:
    """A structural check: the scoring module never reaches for runtime state."""
    source = (REPO_ROOT / "lab" / "scoring" / "live_score.py").read_text(encoding="utf-8")

    assert "dev_labels" not in source
    assert "load_private_labels" not in source


def test_a_card_payload_round_trips_through_json_exactly_as_stored() -> None:
    card = _clean_run()[0]
    reparsed = IncidentFeedItem.model_validate_json(json.dumps(card.payload))

    assert reparsed.incident_id == "incident-attack"
    assert reparsed.verdict_class is VerdictClass.ATTACK
