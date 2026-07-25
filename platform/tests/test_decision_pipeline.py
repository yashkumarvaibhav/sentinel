"""The decision loop: agents, fusion, incidents, verification wired end to end."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from common.config import load_config
from contracts import (
    ChangeEvent,
    ChangeKind,
    CheckOutcome,
    EpisodeStatus,
    EvidenceAxis,
    IncidentState,
    SymptomEpisode,
    SymptomKind,
    VerdictClass,
)
from decision import (
    ChangeFeed,
    DecisionPipeline,
    DecisionTick,
    EpisodeSnapshot,
    episode_timeline,
)
from decision.agents import CHANGE_COVERAGE_KIND
from decision.config import load_evidence_agents, load_incidents, load_verdict_rules
from decision.memory import SIGNATURE_AXES, IncidentSignature
from decision.verdict import FusionStatus
from tests.factories import EPOCH, symptom_episode

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"

# The six deterministic detector paths, as a capture replay covers them. The
# change feed is deliberately absent: its coverage is the pipeline's to claim.
DETECTOR_KINDS = frozenset(
    {
        SymptomKind.RESIDUAL_EXCEED,
        SymptomKind.RATIO_DEFORM,
        SymptomKind.LOG_BURST,
        SymptomKind.EDGE_DEGRADED,
        SymptomKind.SATURATION,
        SymptomKind.DROP,
        SymptomKind.SILENCE,
    }
)
COVERED_SERVICES = frozenset({"frontend", "checkout", "payment", "cart", "email"})


def _pipeline(
    *, changes: ChangeFeed | None = None, memory: tuple[IncidentSignature, ...] = ()
) -> DecisionPipeline:
    return DecisionPipeline(
        agents=load_evidence_agents(CONFIG_ROOT / "decision-agents.yml"),
        verdict_rules=load_verdict_rules(CONFIG_ROOT / "verdict-rules.yml"),
        incidents=load_incidents(CONFIG_ROOT / "incidents.yml"),
        topology=load_config(CONFIG_ROOT).topology,
        changes=changes,
        memory=memory,
    )


def _cascade() -> tuple[SymptomEpisode, ...]:
    """One payment fault seen by three detectors, in the order they confirm it."""
    return (
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="checkout",
            signal="dependency.payment",
            opened_offset_seconds=100.0,
            peak_score=0.8,
        ),
        symptom_episode(
            kind=SymptomKind.LOG_BURST,
            service="payment",
            signal="log.template_rate",
            opened_offset_seconds=110.0,
            peak_score=0.7,
        ),
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="frontend",
            signal="dependency.checkout",
            opened_offset_seconds=140.0,
            peak_score=0.6,
        ),
    )


def _run(
    pipeline: DecisionPipeline,
    snapshots: tuple[EpisodeSnapshot, ...],
) -> tuple[DecisionTick, ...]:
    return tuple(
        pipeline.observe(
            ts=snapshot.ts,
            episodes=snapshot.episodes,
            covered_kinds=DETECTOR_KINDS,
            covered_services=COVERED_SERVICES,
        )
        for snapshot in snapshots
    )


def test_the_episode_stream_becomes_one_tick_per_event_time() -> None:
    snapshots = episode_timeline(_cascade())

    assert [snapshot.ts for snapshot in snapshots] == [
        EPOCH + timedelta(seconds=offset) for offset in (100.0, 110.0, 140.0)
    ]
    assert [len(snapshot.episodes) for snapshot in snapshots] == [1, 2, 3]


def test_a_snapshot_carries_the_latest_revision_of_every_episode_so_far() -> None:
    opened = symptom_episode(
        kind=SymptomKind.SATURATION,
        service="email",
        signal="container_memory",
        opened_offset_seconds=200.0,
        peak_score=0.4,
    )
    grown = opened.model_copy(
        update={
            "revision": 2,
            "peak_score": 0.9,
            "last_breach_ts": opened.opened_ts + timedelta(seconds=60),
        }
    )

    snapshots = episode_timeline((opened, grown))

    assert len(snapshots) == 2
    assert [episode.peak_score for episode in snapshots[-1].episodes] == [0.9]


def test_a_stale_revision_never_overwrites_a_newer_one() -> None:
    opened = symptom_episode(
        kind=SymptomKind.DROP,
        service="frontend",
        signal="request_rate",
        opened_offset_seconds=300.0,
        peak_score=0.5,
    )
    grown = opened.model_copy(update={"revision": 2, "peak_score": 0.95})

    snapshots = episode_timeline((grown, opened))

    assert [episode.peak_score for episode in snapshots[-1].episodes] == [0.95]


def test_a_closed_episode_stays_in_the_view_so_the_incident_can_age_out() -> None:
    opened = symptom_episode(
        kind=SymptomKind.LOG_BURST,
        service="payment",
        signal="log.template_rate",
        opened_offset_seconds=100.0,
    )
    closed = opened.model_copy(
        update={
            "revision": 2,
            "status": EpisodeStatus.CLOSED,
            "closed_ts": opened.opened_ts + timedelta(seconds=120),
        }
    )

    snapshots = episode_timeline((opened, closed))

    assert snapshots[-1].ts == opened.opened_ts + timedelta(seconds=120)
    assert [episode.status for episode in snapshots[-1].episodes] == [EpisodeStatus.CLOSED]


def test_the_storm_is_judged_as_one_incident_with_a_verdict_and_a_verification() -> None:
    ticks = _run(_pipeline(), episode_timeline(_cascade()))

    final = ticks[-1]
    assert len(final.outcomes) == 1
    outcome = final.outcomes[0]
    assert outcome.incident.services == ("checkout", "frontend", "payment")
    assert final.fusion.status is FusionStatus.DECIDED
    assert final.verdict is not None
    assert final.verdict.verdict_class is VerdictClass.OPERATIONAL_FAULT
    assert outcome.verification.confirmed is True


def test_the_collapse_names_the_service_that_never_called_anyone() -> None:
    ticks = _run(_pipeline(), episode_timeline(_cascade()))

    assert ticks[-1].outcomes[0].incident.origin_service == "payment"


def test_every_axis_is_assessed_every_tick() -> None:
    ticks = _run(_pipeline(), episode_timeline(_cascade()))

    for tick in ticks:
        assert {assessment.axis for assessment in tick.assessments} == set(EvidenceAxis)


def test_without_a_change_feed_the_change_axis_is_insufficient_not_calm() -> None:
    ticks = _run(_pipeline(), episode_timeline(_cascade()))

    change = ticks[-1].assessment(EvidenceAxis.CHANGE_CONFIG)
    assert change.status.value == "INSUFFICIENT"
    assert not ticks[-1].changes


def test_a_wired_change_feed_makes_the_change_axis_answerable() -> None:
    deploy = ChangeEvent(
        change_id="chg-payment-1",
        kind=ChangeKind.DEPLOY,
        service="payment",
        ts=EPOCH + timedelta(seconds=40),
        summary="payment 2.4.0 rollout",
        source="deployments.yml",
        honesty="SIMULATED",
    )
    pipeline = _pipeline(changes=ChangeFeed((deploy,)))

    ticks = _run(pipeline, episode_timeline(_cascade()))

    change = ticks[-1].assessment(EvidenceAxis.CHANGE_CONFIG)
    assert change.status.value == "SCORED"
    assert change.score > 0.0
    assert ticks[-1].changes == (deploy,)
    assert ticks[-1].verdict is not None
    assert ticks[-1].verdict.verdict_class is VerdictClass.CODE_CONFIG_FAULT


def test_the_caller_may_not_claim_change_coverage_the_pipeline_owns() -> None:
    pipeline = _pipeline()
    snapshot = episode_timeline(_cascade())[0]

    with pytest.raises(ValueError, match="owned by the pipeline"):
        pipeline.observe(
            ts=snapshot.ts,
            episodes=snapshot.episodes,
            covered_kinds=DETECTOR_KINDS | {CHANGE_COVERAGE_KIND},
            covered_services=COVERED_SERVICES,
        )


def test_a_service_without_telemetry_coverage_blocks_confirmation() -> None:
    pipeline = _pipeline()
    snapshots = episode_timeline(_cascade())

    ticks = tuple(
        pipeline.observe(
            ts=snapshot.ts,
            episodes=snapshot.episodes,
            covered_kinds=DETECTOR_KINDS,
            covered_services=COVERED_SERVICES - {"payment"},
        )
        for snapshot in snapshots
    )

    outcome = ticks[-1].outcomes[0]
    assert outcome.confirmed is False
    coverage = next(
        check for check in outcome.verification.checks if check.name == "trace_coverage"
    )
    assert coverage.outcome is CheckOutcome.FAILED


def test_an_empty_memory_verifies_as_a_bootstrap_never_as_a_confirmation() -> None:
    ticks = _run(_pipeline(), episode_timeline(_cascade()))

    memory_check = next(
        check
        for check in ticks[-1].outcomes[0].verification.checks
        if check.name == "memory_similarity"
    )
    assert memory_check.outcome is CheckOutcome.BOOTSTRAP


def test_the_same_stream_produces_identical_judgements_twice() -> None:
    snapshots = episode_timeline(_cascade())

    first = _run(_pipeline(), snapshots)
    second = _run(_pipeline(), snapshots)

    assert [tick.verdict for tick in first] == [tick.verdict for tick in second]
    assert [
        (outcome.incident, outcome.verification) for tick in first for outcome in tick.outcomes
    ] == [(outcome.incident, outcome.verification) for tick in second for outcome in tick.outcomes]


def test_a_tick_out_of_event_time_order_fails_closed() -> None:
    pipeline = _pipeline()
    snapshots = episode_timeline(_cascade())
    _run(pipeline, snapshots[1:])

    with pytest.raises(ValueError, match="must not move backwards"):
        pipeline.observe(
            ts=snapshots[0].ts,
            episodes=snapshots[0].episodes,
            covered_kinds=DETECTOR_KINDS,
            covered_services=COVERED_SERVICES,
        )


def _closed_cascade() -> tuple[SymptomEpisode, ...]:
    """The same storm, every episode closed, so the incident can resolve."""
    closed = []
    for episode in _cascade():
        closed.append(episode)
        closed.append(
            episode.model_copy(
                update={
                    "revision": 2,
                    "status": EpisodeStatus.CLOSED,
                    "closed_ts": episode.opened_ts + timedelta(seconds=200),
                }
            )
        )
    return tuple(closed)


def test_an_incident_is_remembered_only_once_it_is_over() -> None:
    deploy = ChangeEvent(
        change_id="chg-payment-1",
        kind=ChangeKind.DEPLOY,
        service="payment",
        ts=EPOCH + timedelta(seconds=40),
        summary="payment 2.4.0 rollout",
        source="deployments.yml",
        honesty="SIMULATED",
    )
    pipeline = _pipeline(changes=ChangeFeed((deploy,)))
    snapshots = episode_timeline(_closed_cascade())

    ticks = _run(pipeline, snapshots)
    assert not pipeline.memory  # still running: not yet a precedent

    settled = snapshots[-1]
    resolved = pipeline.observe(
        ts=settled.ts + timedelta(seconds=600),
        episodes=settled.episodes,
        covered_kinds=DETECTOR_KINDS,
        covered_services=COVERED_SERVICES,
    )

    assert resolved.outcomes[0].incident.state is IncidentState.RESOLVED
    assert len(pipeline.memory) == 1
    # The shape kept is the incident's peak, not the calm it decayed into.
    peak = max(max(tick.assessment(axis).score for tick in ticks) for axis in SIGNATURE_AXES)
    assert max(pipeline.memory[0].vector) == pytest.approx(peak)


def test_an_unmeasurable_axis_refuses_to_produce_a_signature() -> None:
    """A half-measured incident is not a memory: ignorance must not look like a shape."""
    pipeline = _pipeline()

    ticks = _run(pipeline, episode_timeline(_closed_cascade()))

    assert all(outcome.signature is None for tick in ticks for outcome in tick.outcomes)
    assert pipeline.memory == ()
