"""A public capture can populate one honest runtime incident bundle, once."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import pytest
from lab.scoring.publish import CapturePublicationError, CaptureReplayPublisher, main

from api.incidents import IncidentFeedPublisher
from common.config import load_config
from common.storage import (
    IncidentDetailRecord,
    IncidentGraphRecord,
    IncidentRecord,
    IncidentSecurityRecord,
)
from contracts import (
    ActionControlSnapshot,
    ActionControlState,
    CheckOutcome,
    Decision,
    DecisionAction,
    EpisodeStatus,
    EvidenceDirection,
    EvidenceItem,
    Incident,
    IncidentSeverity,
    IncidentState,
    SnapshotInvalidation,
    SnapshotResource,
    SymptomEpisode,
    SymptomKind,
    Verdict,
    VerdictClass,
    Verification,
    VerificationCheck,
)
from decision import IncidentOutcome

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
TICK = datetime(2026, 7, 23, 11, 37, 25, tzinfo=UTC)


@dataclass(frozen=True)
class _Tick:
    ts: datetime
    outcomes: tuple[IncidentOutcome, ...]


@dataclass(frozen=True)
class _Replay:
    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: str
    ticks: tuple[_Tick, ...]
    episode_revisions: tuple[SymptomEpisode, ...]


class _Store:
    def __init__(self) -> None:
        self.bundle: (
            tuple[
                IncidentRecord,
                IncidentGraphRecord,
                IncidentDetailRecord,
                IncidentSecurityRecord,
                ActionControlSnapshot | None,
            ]
            | None
        ) = None
        self.writes = 0

    async def put_incident_bundle(
        self,
        record: IncidentRecord,
        graph: IncidentGraphRecord,
        detail: IncidentDetailRecord,
        security: IncidentSecurityRecord,
        action_control: ActionControlSnapshot | None = None,
    ) -> bool:
        candidate = (record, graph, detail, security, action_control)
        if self.bundle is not None:
            assert self.bundle == candidate
            return False
        self.bundle = candidate
        self.writes += 1
        return True


class _BrokenStore:
    async def put_incident_bundle(
        self,
        record: IncidentRecord,
        graph: IncidentGraphRecord,
        detail: IncidentDetailRecord,
        security: IncidentSecurityRecord,
        action_control: ActionControlSnapshot | None = None,
    ) -> NoReturn:
        del record, graph, detail, security, action_control
        raise RuntimeError("atomic store unavailable")


class _Broker:
    def __init__(self) -> None:
        self.events: list[SnapshotInvalidation] = []

    def publish(self, event: SnapshotInvalidation) -> int:
        self.events.append(event)
        return 1


def test_public_replay_publishes_one_terminal_simulation_and_retry_is_a_noop() -> None:
    async def exercise() -> None:
        store = _Store()
        broker = _Broker()
        producer = CaptureReplayPublisher(
            config=load_config(CONFIG_ROOT),
            config_root=CONFIG_ROOT,
        )
        publisher = IncidentFeedPublisher(store=store, broker=broker)

        first = await producer.publish(_replay(), publisher=publisher)
        retried = await producer.publish(_replay(), publisher=publisher)

        assert first.persisted is True
        assert retried.persisted is False
        assert first.incident_id == retried.incident_id == "incident-combo-1"
        assert store.writes == 1
        assert store.bundle is not None
        _, _, detail, security, control = store.bundle
        provenance = detail.payload["provenance"]
        assert isinstance(provenance, dict)
        assert provenance["capture_id"] == "phase2-combo-503-dev-v11"
        assert provenance["mode"] == "REPLAY"
        assert provenance["telemetry"] == "REAL"
        assert provenance["stimulus"] == "SIMULATED"
        assert control is not None
        assert control.state is ActionControlState.SIMULATED
        assert control.plan.target_service == "frontend"
        assert control.plan.honesty == "SIMULATED"
        assert control.latest_outcome is not None
        assert control.latest_outcome.dry_run is True
        mitigation = security.payload["mitigation"]
        assert isinstance(mitigation, dict)
        assert mitigation["state"] == "SIMULATED"
        assert len(broker.events) == 1
        assert broker.events[0].resources == (
            SnapshotResource.INCIDENTS,
            SnapshotResource.ACTIONS,
            SnapshotResource.SECURITY,
        )

    asyncio.run(exercise())


def test_failed_atomic_capture_write_emits_no_invalidation() -> None:
    async def exercise() -> None:
        broker = _Broker()
        producer = CaptureReplayPublisher(
            config=load_config(CONFIG_ROOT),
            config_root=CONFIG_ROOT,
        )
        publisher = IncidentFeedPublisher(store=_BrokenStore(), broker=broker)

        try:
            await producer.publish(_replay(), publisher=publisher)
        except RuntimeError as error:
            assert str(error) == "atomic store unavailable"
        else:  # pragma: no cover - the broken store must fail
            raise AssertionError("capture publication unexpectedly succeeded")
        assert broker.events == []

    asyncio.run(exercise())


def test_runtime_publication_refuses_a_held_out_capture_before_storage() -> None:
    async def exercise() -> None:
        store = _Store()
        broker = _Broker()
        producer = CaptureReplayPublisher(
            config=load_config(CONFIG_ROOT),
            config_root=CONFIG_ROOT,
        )
        publisher = IncidentFeedPublisher(store=store, broker=broker)

        with pytest.raises(CapturePublicationError, match="development"):
            await producer.publish(
                replace(_replay(), seed_purpose="held_out"),
                publisher=publisher,
            )
        assert store.bundle is None
        assert broker.events == []

    asyncio.run(exercise())


def test_capture_publisher_has_no_private_label_import_or_path_literal() -> None:
    source = (REPO_ROOT / "lab" / "scoring" / "publish.py").read_text(encoding="utf-8")

    assert "load_capture_labels" not in source
    assert "load_private_labels" not in source
    assert "labels.json" not in source


def test_cli_requires_explicit_runtime_write_acknowledgement() -> None:
    with pytest.raises(SystemExit):
        main(
            (
                "--repo-root",
                str(REPO_ROOT),
                "--capture",
                str(REPO_ROOT / "var" / "captures" / "phase2-combo-503-dev-v11"),
            )
        )


def _replay() -> _Replay:
    episode = SymptomEpisode(
        episode_id="episode-combo-1",
        kind=SymptomKind.RATIO_DEFORM,
        service="frontend",
        signal="auth.failure_ratio",
        status=EpisodeStatus.ACTIVE,
        opened_ts=TICK - timedelta(seconds=20),
        confirmed_ts=TICK - timedelta(seconds=18),
        last_breach_ts=TICK,
        peak_score=0.92,
        breach_tick_count=4,
        revision=2,
        opening_symptom_id="symptom-open",
        peak_symptom_id="symptom-peak",
        latest_symptom_id="symptom-latest",
        evidence_refs=("ratio-window-1",),
    )
    incident = Incident(
        incident_id="incident-combo-1",
        anchor_episode_id=episode.episode_id,
        opened_ts=episode.opened_ts,
        last_activity_ts=TICK,
        state=IncidentState.OPEN,
        severity=IncidentSeverity.LOW,
        services=("frontend",),
        kinds=(episode.kind,),
        episode_ids=(episode.episode_id,),
        business_impact=0.2,
        origin_service="frontend",
        origin_confidence=0.9,
        revision=2,
        note="one verified credential-stuffing incident",
    )
    evidence = EvidenceItem(
        feature="auth.failure_ratio",
        value=0.72,
        baseline=0.04,
        direction=EvidenceDirection.ABOVE_BASELINE,
        contribution=0.9,
        note="authentication failures deformed against the measured baseline",
        evidence_refs=(episode.episode_id,),
    )
    verdict = Verdict(
        verdict_id="verdict-combo-1",
        ts=TICK,
        verdict_class=VerdictClass.ATTACK,
        rule_id="hostile-behaviour",
        confidence=0.9,
        reason="credential failures concentrated into machine-regular traffic",
        distribution={
            "EXPECTED_EVENT": 0.02,
            "ATTACK": 0.9,
            "OPERATIONAL_FAULT": 0.02,
            "CODE_CONFIG_FAULT": 0.01,
            "COMBINATION": 0.05,
        },
        corroborating_kinds=(episode.kind,),
        services=("frontend",),
        evidence=(evidence,),
        assessment_ids=("assessment-security-1",),
        rejected_alternatives=(),
    )
    checks = tuple(
        VerificationCheck(name=name, outcome=CheckOutcome.PASSED, detail=f"{name} passed")
        for name in (
            "temporal_causality",
            "trace_coverage",
            "dependency_validity",
            "memory_similarity",
        )
    )
    verification = Verification(
        verification_id="verification-combo-1",
        ts=TICK,
        incident_id=incident.incident_id,
        confirmed=True,
        checks=checks,
    )
    decision = Decision(
        decision_id="decision-combo-1",
        ts=TICK,
        incident_id=incident.incident_id,
        action=DecisionAction.AUTO_CONTAIN_THEN_ESCALATE,
        rule_id="contain-verified-attack",
        reason="the verified residual requires a bounded response",
        evidence_ts=TICK,
        severity=incident.severity,
        confirmed=True,
        verification_id=verification.verification_id,
        requires_human_approval=False,
        escalation_reasons=("notify the operator after containment",),
        verdict_class=verdict.verdict_class,
        verdict_id=verdict.verdict_id,
        confidence=verdict.confidence,
        target_service="frontend",
    )
    outcome = IncidentOutcome(
        incident=incident,
        verdict=verdict,
        verification=verification,
        decision=decision,
        matches=(),
        signature=None,
    )
    return _Replay(
        capture_id="phase2-combo-503-dev-v11",
        scenario_id="combo_night",
        seed=503,
        seed_purpose="development",
        ticks=(_Tick(ts=TICK, outcomes=(outcome,)),),
        episode_revisions=(episode,),
    )
