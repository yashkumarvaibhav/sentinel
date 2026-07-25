"""The harness that drives the remediation loop from a recorded capture.

The replay over a real capture takes minutes and lives behind
``make remediate-replay``; what is tested here is everything about the harness
that could quietly make that replay's report untrue - which adapters stand in
for which, that they touch nothing, and that a report says what it measured.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from lab.scoring.remediate import (
    FlagStandIn,
    KubernetesStandIn,
    MeshStandIn,
    RemediationReplay,
    UnreadableSlo,
    build_remediator,
    render_report,
)

from action import BreakerState, RemediationRun
from audit import AuditChain
from common.config import load_config
from contracts import ActuatorKind, AuditEventKind, Decision, DecisionAction, IncidentSeverity
from contracts.decision import VerdictClass

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _decision(confidence: float = 0.88) -> Decision:
    return Decision(
        decision_id="decision-1",
        ts=TICK,
        incident_id="incident-1",
        action=DecisionAction.ACT,
        rule_id="remediate-a-verified-fault",
        reason="a confirmed fault with an evidence-computed origin",
        evidence_ts=TICK,
        severity=IncidentSeverity.HIGH,
        confirmed=True,
        verification_id="verification-1",
        requires_human_approval=False,
        verdict_class=VerdictClass.ATTACK,
        verdict_id="verdict-1",
        confidence=confidence,
        target_service="frontend",
    )


def test_every_stand_in_answers_to_a_real_adapters_kind() -> None:
    """The committed ladders are run exactly as written, or they are not evidence."""
    assert MeshStandIn.kind is ActuatorKind.MESH
    assert KubernetesStandIn.kind is ActuatorKind.KUBERNETES
    assert FlagStandIn.kind is ActuatorKind.FEATURE_FLAG


def test_every_stand_in_labels_everything_it_produces_simulated() -> None:
    """The one property that keeps a stand-in from being a lie."""
    for adapter in (MeshStandIn(), KubernetesStandIn(), FlagStandIn()):
        assert adapter.honesty == "SIMULATED"


def test_the_replay_reads_no_slo_and_says_so_rather_than_guessing() -> None:
    """A capture records telemetry, not SLO positions. None is the honest answer."""
    assert UnreadableSlo()("frontend", ts=TICK) is None


def test_the_loop_the_replay_wires_runs_the_committed_ladders_end_to_end() -> None:
    """The wiring itself, without the minutes a real capture replay costs."""
    ledger = AuditChain()
    remediator = build_remediator(load_config(CONFIG_DIR), config_root=CONFIG_DIR, ledger=ledger)

    run = remediator.consider(_decision(), ts=TICK, settled_at=TICK + timedelta(seconds=60))

    assert run.selection is not None
    assert run.selection.primary.rung_id == "hold-the-cohort-to-its-ceiling"
    # It acted, and then undid it: no SLO can be read, and a protected service
    # nobody can see is never one known to be fine.
    assert run.applied != ()
    assert run.rollback is not None
    assert run.rollback.reverted
    assert run.rollback.availability_restored is None, "an unmeasured recovery is not claimed"
    assert remediator.registry.for_incident("incident-1") == ()
    kinds = {entry.kind for entry in ledger.entries()}
    assert AuditEventKind.DECISION in kinds
    assert AuditEventKind.ROLLBACK in kinds


def test_a_report_states_its_honesty_label_before_anything_else() -> None:
    """A simulated run presented as a measured one is the failure mode here."""
    replay = RemediationReplay(
        capture_id="capture-1",
        scenario_id="combo_night",
        seed=503,
        seed_purpose="development",
        anchor_ts=TICK,
        runs=(
            (
                12.0,
                RemediationRun(
                    decision_id="decision-1",
                    incident_id="incident-1",
                    breaker=BreakerState(open=False),
                    refusals=("hold-the-cohort-to-its-ceiling: needs 1 approver(s)",),
                ),
            ),
        ),
        expired=0,
        ledger_entries=3,
        ledger_head="a" * 64,
        ladder_fingerprint="b" * 64,
        detector_fingerprint="c" * 64,
        decision_fingerprint="d" * 48,
    )

    report = render_report([replay])

    assert "**SIMULATED.**" in report.split("\n")[2]
    assert "Acting decisions considered: **1**" in report
    assert "that took no action: **1**" in report
    assert "1x hold-the-cohort-to-its-ceiling: needs 1 approver(s)" in report
