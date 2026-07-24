"""The MVP change-evidence source and the change/config evidence agent."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from common.config import load_config
from contracts import (
    AgentStatus,
    AgentTrend,
    ChangeEvent,
    ChangeKind,
    EvidenceAxis,
    EvidenceDirection,
    SymptomEpisode,
    SymptomKind,
)
from decision import AgentEvidenceWindow, ChangeConfigEvidenceAgent, ChangeFeed
from decision.changes import (
    FLAG_SOURCE,
    LEDGER_SOURCE,
    ROLLOUT_SOURCE,
    FlagChange,
    RolloutEvent,
    UnmappedChangeError,
    flag_changes,
    ledger_changes,
    rollout_changes,
)
from decision.config import (
    DeploymentLedgerConfig,
    load_deployment_ledger,
    load_evidence_agents,
)
from tests.factories import EPOCH, symptom_episode

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
AGENTS_PATH = CONFIG_ROOT / "decision-agents.yml"
LEDGER_PATH = CONFIG_ROOT / "deployments.yml"
COVERED = frozenset({SymptomKind.DEPLOY_MARKER, SymptomKind.EDGE_DEGRADED})


def _agent() -> ChangeConfigEvidenceAgent:
    config = load_evidence_agents(AGENTS_PATH)
    return ChangeConfigEvidenceAgent(configuration=config.axis(EvidenceAxis.CHANGE_CONFIG))


def _ledger() -> DeploymentLedgerConfig:
    return load_deployment_ledger(LEDGER_PATH)


def _window(
    *,
    changes: tuple[ChangeEvent, ...] = (),
    episodes: tuple[SymptomEpisode, ...] = (),
    offset_seconds: float = 600.0,
    covered: frozenset[SymptomKind] = COVERED,
) -> AgentEvidenceWindow:
    return AgentEvidenceWindow(
        ts=EPOCH + timedelta(seconds=offset_seconds),
        episodes=episodes,
        covered_kinds=covered,
        changes=changes,
    )


def _flag_change(*, flag: str = "paymentFailure", offset_seconds: float) -> ChangeEvent:
    return flag_changes(
        (
            FlagChange(
                flag=flag,
                previous_variant="off",
                current_variant="on",
                ts=EPOCH + timedelta(seconds=offset_seconds),
            ),
        ),
        ledger=_ledger(),
        honesty="SIMULATED",
    )[0]


def test_the_committed_ledger_only_names_known_topology_services() -> None:
    topology = load_config(CONFIG_ROOT).topology
    known = frozenset(service.service for service in topology.services)

    load_deployment_ledger(LEDGER_PATH).validate_services(known)


def test_a_rollout_event_normalizes_to_a_change_with_its_own_honesty() -> None:
    events = rollout_changes(
        (
            RolloutEvent(
                workload="checkout",
                revision="v2.2.0",
                ts=EPOCH,
                reason="ScalingReplicaSet",
            ),
        ),
        ledger=_ledger(),
        honesty="REAL",
    )

    assert len(events) == 1
    change = events[0]
    assert change.kind is ChangeKind.ROLLOUT
    assert change.service == "checkout"
    assert change.revision == "v2.2.0"
    assert change.source == ROLLOUT_SOURCE
    assert change.honesty == "REAL"
    assert "v2.2.0" in change.summary


def test_a_flag_change_normalizes_to_the_service_the_flag_owns() -> None:
    change = _flag_change(offset_seconds=0.0)

    assert change.kind is ChangeKind.FLAG
    assert change.service == "payment"
    assert change.source == FLAG_SOURCE
    assert change.honesty == "SIMULATED"


def test_an_unmapped_workload_is_refused_rather_than_guessed() -> None:
    with pytest.raises(UnmappedChangeError, match="no configured topology service"):
        rollout_changes(
            (RolloutEvent(workload="quote", revision="v1", ts=EPOCH, reason="rollout"),),
            ledger=_ledger(),
            honesty="REAL",
        )


def test_an_unmapped_flag_is_refused_rather_than_guessed() -> None:
    with pytest.raises(UnmappedChangeError, match="no configured topology service"):
        flag_changes(
            (
                FlagChange(
                    flag="adManualGc",
                    previous_variant="off",
                    current_variant="on",
                    ts=EPOCH,
                ),
            ),
            ledger=_ledger(),
            honesty="SIMULATED",
        )


def test_the_feed_returns_only_changes_inside_the_lookback_window() -> None:
    feed = ChangeFeed(
        (
            _flag_change(flag="paymentFailure", offset_seconds=0.0),
            _flag_change(flag="cartFailure", offset_seconds=540.0),
        )
    )

    recent = feed.within(EPOCH + timedelta(seconds=600.0), lookback_seconds=300.0)

    assert [change.service for change in recent] == ["cart"]


def test_a_change_exactly_at_the_horizon_has_already_decayed_out() -> None:
    feed = ChangeFeed((_flag_change(offset_seconds=0.0),))

    assert feed.within(EPOCH + timedelta(seconds=300.0), lookback_seconds=300.0) == ()
    assert len(feed.within(EPOCH + timedelta(seconds=299.0), lookback_seconds=300.0)) == 1


def test_the_feed_refuses_two_different_records_for_one_change_id() -> None:
    original = _flag_change(offset_seconds=0.0)
    conflicting = original.model_copy(update={"service": "cart"})

    with pytest.raises(ValueError, match="conflicting records"):
        ChangeFeed((original, conflicting))


def test_an_identical_redelivered_record_is_deduplicated() -> None:
    change = _flag_change(offset_seconds=0.0)

    assert len(ChangeFeed((change, change)).changes) == 1


def test_the_ledger_contributes_its_own_asserted_records() -> None:
    ledger = _ledger()

    assert all(change.source == LEDGER_SOURCE for change in ledger_changes(ledger))


def test_a_recent_change_on_a_symptomatic_service_scores_highest() -> None:
    agent = _agent()
    episode = symptom_episode(
        kind=SymptomKind.EDGE_DEGRADED,
        service="payment",
        signal="dependency.payment",
    )

    assessment = agent.assess(
        _window(
            changes=(_flag_change(offset_seconds=570.0),),
            episodes=(episode,),
        )
    )

    assert assessment.status is AgentStatus.SCORED
    # ceiling 0.90 * FLAG 0.80 * recency (1 - 30/900) * related 1.0
    assert assessment.score == pytest.approx(0.90 * 0.80 * (1.0 - 30.0 / 900.0))
    item = assessment.evidence[0]
    assert item.feature == "payment.change.flag"
    assert item.value == pytest.approx(30.0)
    assert item.baseline == pytest.approx(900.0)
    assert item.direction is EvidenceDirection.BELOW_BASELINE
    assert "on a service showing symptoms" in item.note
    assert "SIMULATED" in item.note


def test_the_same_change_on_a_quiet_service_is_discounted_not_discarded() -> None:
    related = _agent().assess(
        _window(
            changes=(_flag_change(offset_seconds=570.0),),
            episodes=(symptom_episode(kind=SymptomKind.EDGE_DEGRADED, service="payment"),),
        )
    )
    unrelated = _agent().assess(_window(changes=(_flag_change(offset_seconds=570.0),)))

    assert 0.0 < unrelated.score < related.score
    assert unrelated.score == pytest.approx(related.score * 0.25)
    assert "no symptom of its own" in unrelated.evidence[0].note


def test_change_evidence_decays_until_it_falls_under_the_floor() -> None:
    fresh = _agent().assess(
        _window(changes=(_flag_change(offset_seconds=999.0),), offset_seconds=1000.0)
    )
    stale = _agent().assess(
        _window(changes=(_flag_change(offset_seconds=300.0),), offset_seconds=1000.0)
    )

    assert fresh.score > stale.score
    assert stale.score == 0.0
    assert stale.evidence == ()


def test_a_change_older_than_the_horizon_contributes_nothing() -> None:
    agent = _agent()

    assessment = agent.assess(
        _window(
            changes=(_flag_change(offset_seconds=0.0),),
            offset_seconds=1200.0,
        )
    )

    assert assessment.score == 0.0
    assert assessment.evidence == ()


def test_change_pressure_never_reaches_certainty_from_correlation_alone() -> None:
    agent = _agent()
    changes = tuple(
        _flag_change(flag=flag, offset_seconds=599.0)
        for flag in ("paymentFailure", "cartFailure", "emailMemoryLeak", "kafkaQueueProblems")
    )

    assessment = agent.assess(
        _window(
            changes=changes,
            episodes=(symptom_episode(kind=SymptomKind.EDGE_DEGRADED, service="payment"),),
        )
    )

    assert len(assessment.evidence) == 4
    assert assessment.score < 1.0


def test_a_change_without_stated_feed_coverage_fails_closed() -> None:
    with pytest.raises(ValueError, match="contradicts the stated coverage"):
        _window(
            changes=(_flag_change(offset_seconds=0.0),),
            covered=frozenset({SymptomKind.EDGE_DEGRADED}),
        )


def test_a_change_after_the_observing_tick_fails_closed() -> None:
    with pytest.raises(ValueError, match="cannot happen after"):
        _window(changes=(_flag_change(offset_seconds=900.0),), offset_seconds=600.0)


def test_a_duplicate_change_in_one_window_fails_closed() -> None:
    change = _flag_change(offset_seconds=0.0)

    with pytest.raises(ValueError, match="duplicate change event"):
        _window(changes=(change, change))


def test_the_axis_is_insufficient_when_the_change_feed_was_not_consulted() -> None:
    agent = _agent()

    assessment = agent.assess(_window(covered=frozenset({SymptomKind.EDGE_DEGRADED})))

    assert assessment.status is AgentStatus.INSUFFICIENT
    assert assessment.trend is AgentTrend.UNKNOWN


def test_a_consulted_but_quiet_feed_scores_a_calm_zero() -> None:
    agent = _agent()

    assessment = agent.assess(_window(covered=frozenset({SymptomKind.DEPLOY_MARKER})))

    assert assessment.status is AgentStatus.SCORED
    assert assessment.score == 0.0
    assert assessment.covered_kinds == (SymptomKind.DEPLOY_MARKER,)
