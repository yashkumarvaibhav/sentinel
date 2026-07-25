"""Incident clustering, dedup, lifecycle and severity."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from common.config import load_config
from contracts import (
    AgentAssessment,
    AgentStatus,
    AgentTrend,
    EpisodeStatus,
    EvidenceAxis,
    IncidentSeverity,
    IncidentState,
    SymptomEpisode,
    SymptomKind,
)
from decision import AgentEvidenceWindow, BusinessImpactEvidenceAgent, IncidentTracker
from decision.config import load_evidence_agents, load_incidents
from tests.factories import EPOCH, symptom_episode

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
ALL_KINDS = frozenset(SymptomKind)


def _tracker() -> IncidentTracker:
    return IncidentTracker(
        configuration=load_incidents(CONFIG_ROOT / "incidents.yml"),
        topology=load_config(CONFIG_ROOT).topology,
    )


def _business(*episodes: SymptomEpisode, offset_seconds: float = 600.0) -> AgentAssessment:
    agents = load_evidence_agents(CONFIG_ROOT / "decision-agents.yml")
    topology = load_config(CONFIG_ROOT).topology
    agent = BusinessImpactEvidenceAgent(
        configuration=agents.axis(EvidenceAxis.BUSINESS_IMPACT),
        criticality={service.service: service.criticality for service in topology.services},
    )
    return agent.assess(
        AgentEvidenceWindow(
            ts=EPOCH + timedelta(seconds=offset_seconds),
            episodes=episodes,
            covered_kinds=ALL_KINDS,
        )
    )


def _storm() -> tuple[SymptomEpisode, ...]:
    """The real shape of one payment fault: many detectors, one problem."""
    return (
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="checkout",
            signal="dependency.payment",
            opened_offset_seconds=100.0,
        ),
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="frontend",
            signal="dependency.checkout",
            opened_offset_seconds=104.0,
        ),
        symptom_episode(
            kind=SymptomKind.LOG_BURST,
            service="payment",
            signal="log_template_rate",
            opened_offset_seconds=110.0,
        ),
        symptom_episode(
            kind=SymptomKind.LOG_BURST,
            service="checkout",
            signal="log_template_rate",
            opened_offset_seconds=118.0,
        ),
        symptom_episode(
            kind=SymptomKind.SATURATION,
            service="email",
            signal="container_memory",
            opened_offset_seconds=140.0,
        ),
    )


def test_a_symptom_storm_becomes_one_incident_not_twelve() -> None:
    incidents = _tracker().observe(
        ts=EPOCH + timedelta(seconds=300.0),
        episodes=_storm(),
    )

    assert len(incidents) == 1
    incident = incidents[0]
    assert len(incident.episode_ids) == 5
    assert set(incident.services) == {"checkout", "frontend", "payment", "email"}
    assert set(incident.kinds) == {
        SymptomKind.EDGE_DEGRADED,
        SymptomKind.LOG_BURST,
        SymptomKind.SATURATION,
    }
    assert "collapsed into one incident" in incident.note


def test_the_incident_is_anchored_to_the_earliest_episode_in_the_storm() -> None:
    storm = _storm()
    incidents = _tracker().observe(ts=EPOCH + timedelta(seconds=300.0), episodes=storm)

    assert incidents[0].anchor_episode_id == storm[0].episode_id
    assert incidents[0].opened_ts == storm[0].opened_ts


def test_identity_survives_the_storm_growing_around_it() -> None:
    tracker = _tracker()
    storm = _storm()

    early = tracker.observe(ts=EPOCH + timedelta(seconds=120.0), episodes=storm[:2])
    later = tracker.observe(ts=EPOCH + timedelta(seconds=300.0), episodes=storm)

    assert early[0].incident_id == later[0].incident_id
    assert later[0].revision == early[0].revision + 1
    assert len(later[0].episode_ids) > len(early[0].episode_ids)


def test_far_apart_services_stay_separate_incidents() -> None:
    incidents = _tracker().observe(
        ts=EPOCH + timedelta(seconds=300.0),
        episodes=(
            symptom_episode(
                kind=SymptomKind.EDGE_DEGRADED,
                service="payment",
                signal="dependency.payment",
                opened_offset_seconds=100.0,
            ),
            symptom_episode(
                kind=SymptomKind.SATURATION,
                service="email",
                signal="container_memory",
                opened_offset_seconds=100.0,
            ),
        ),
    )

    assert len(incidents) == 2


def test_episodes_far_apart_in_time_stay_separate_incidents() -> None:
    incidents = _tracker().observe(
        ts=EPOCH + timedelta(seconds=2000.0),
        episodes=(
            symptom_episode(
                kind=SymptomKind.EDGE_DEGRADED,
                service="checkout",
                signal="dependency.payment",
                opened_offset_seconds=100.0,
                status=EpisodeStatus.CLOSED,
            ),
            symptom_episode(
                kind=SymptomKind.LOG_BURST,
                service="checkout",
                signal="log_template_rate",
                opened_offset_seconds=1800.0,
            ),
        ),
    )

    assert len(incidents) == 2


def test_a_bridging_episode_merges_two_incidents_under_the_older_anchor() -> None:
    tracker = _tracker()
    payment = symptom_episode(
        kind=SymptomKind.EDGE_DEGRADED,
        service="payment",
        signal="dependency.payment",
        opened_offset_seconds=100.0,
    )
    email = symptom_episode(
        kind=SymptomKind.SATURATION,
        service="email",
        signal="container_memory",
        opened_offset_seconds=110.0,
    )
    bridge = symptom_episode(
        kind=SymptomKind.LOG_BURST,
        service="checkout",
        signal="log_template_rate",
        opened_offset_seconds=130.0,
    )

    separate = tracker.observe(ts=EPOCH + timedelta(seconds=200.0), episodes=(payment, email))
    merged = tracker.observe(ts=EPOCH + timedelta(seconds=300.0), episodes=(payment, email, bridge))

    assert len(separate) == 2
    assert len(merged) == 1
    assert merged[0].anchor_episode_id == payment.episode_id
    absorbed = {incident.incident_id for incident in separate} - {merged[0].incident_id}
    assert set(merged[0].merged_incident_ids) == absorbed


def _combo_night() -> tuple[SymptomEpisode, ...]:
    """The measured onsets of phase2-combo-503-dev-v11: an attack, a fault, a late night.

    Every episode here ran long enough to overlap the next, which is exactly how
    the whole 1004 s scenario used to collapse into one incident.
    """
    return (
        symptom_episode(
            kind=SymptomKind.RESIDUAL_EXCEED,
            service="frontend",
            signal="request_rate",
            opened_offset_seconds=144.0,
        ),
        symptom_episode(
            kind=SymptomKind.RATIO_DEFORM,
            service="frontend",
            signal="path_entropy",
            opened_offset_seconds=180.0,
        ),
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="frontend",
            signal="dependency.checkout",
            opened_offset_seconds=344.0,
        ),
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="checkout",
            signal="dependency.payment",
            opened_offset_seconds=344.0,
        ),
        symptom_episode(
            kind=SymptomKind.LOG_BURST,
            service="payment",
            signal="log_template_rate",
            opened_offset_seconds=360.0,
        ),
        symptom_episode(
            kind=SymptomKind.SILENCE,
            service="frontend",
            signal="request_rate",
            opened_offset_seconds=902.0,
        ),
    )


def test_a_whole_night_of_symptoms_is_not_one_incident() -> None:
    incidents = _tracker().observe(ts=EPOCH + timedelta(seconds=1004.0), episodes=_combo_night())

    assert len(incidents) > 1
    services = [incident.services for incident in incidents]
    # The attack on the frontend and the payment fault are separate problems.
    assert ("frontend",) in services
    assert ("checkout", "frontend", "payment") in services


def test_each_separated_incident_keeps_its_own_origin() -> None:
    incidents = _tracker().observe(ts=EPOCH + timedelta(seconds=1004.0), episodes=_combo_night())

    origins = {incident.services: incident.origin_service for incident in incidents}
    assert origins[("frontend",)] == "frontend"
    assert origins[("checkout", "frontend", "payment")] == "payment"


def test_a_late_symptom_cannot_join_a_storm_that_started_long_before() -> None:
    """The join window is measured from the anchor, not from the nearest member."""
    early = symptom_episode(
        kind=SymptomKind.EDGE_DEGRADED,
        service="checkout",
        signal="dependency.payment",
        opened_offset_seconds=100.0,
    )
    chain = tuple(
        symptom_episode(
            kind=SymptomKind.LOG_BURST,
            service="checkout",
            signal="log_template_rate",
            opened_offset_seconds=offset,
            episode_id=f"chain-{offset:.0f}",
        )
        for offset in (200.0, 300.0, 400.0)
    )

    incidents = _tracker().observe(ts=EPOCH + timedelta(seconds=600.0), episodes=(early, *chain))

    # Each link overlaps the one before it; none of them is the same problem as
    # a fault that started five minutes earlier.
    assert len(incidents) > 1
    assert all(len(incident.episode_ids) <= 2 for incident in incidents)


def test_an_incident_is_watched_before_it_is_declared_over() -> None:
    tracker = _tracker()
    closed = symptom_episode(
        kind=SymptomKind.EDGE_DEGRADED,
        service="checkout",
        signal="dependency.payment",
        opened_offset_seconds=100.0,
        status=EpisodeStatus.CLOSED,
    )

    watching = tracker.observe(ts=EPOCH + timedelta(seconds=200.0), episodes=(closed,))
    still_watching = tracker.observe(ts=EPOCH + timedelta(seconds=400.0), episodes=(closed,))
    over = tracker.observe(ts=EPOCH + timedelta(seconds=520.0), episodes=(closed,))

    assert watching[0].state is IncidentState.MONITORING
    assert still_watching[0].state is IncidentState.MONITORING
    assert over[0].state is IncidentState.RESOLVED


def test_a_symptom_coming_straight_back_reopens_the_same_incident() -> None:
    tracker = _tracker()
    closed = symptom_episode(
        kind=SymptomKind.EDGE_DEGRADED,
        service="checkout",
        signal="dependency.payment",
        opened_offset_seconds=100.0,
        status=EpisodeStatus.CLOSED,
    )
    active = closed.model_copy(update={"status": EpisodeStatus.ACTIVE, "closed_ts": None})

    watching = tracker.observe(ts=EPOCH + timedelta(seconds=200.0), episodes=(closed,))
    back = tracker.observe(ts=EPOCH + timedelta(seconds=260.0), episodes=(active,))

    assert watching[0].state is IncidentState.MONITORING
    assert back[0].state is IncidentState.OPEN
    assert back[0].incident_id == watching[0].incident_id


def test_severity_comes_from_the_measured_business_impact() -> None:
    drop = symptom_episode(
        kind=SymptomKind.DROP,
        service="frontend",
        signal="request_rate",
        peak_score=1.0,
        opened_offset_seconds=100.0,
    )

    incidents = _tracker().observe(
        ts=EPOCH + timedelta(seconds=600.0),
        episodes=(drop,),
        business=_business(drop),
    )

    assert incidents[0].business_impact == pytest.approx(0.80)
    assert incidents[0].severity is IncidentSeverity.CRITICAL


def test_a_lesser_service_earns_a_lower_severity_from_the_same_symptom() -> None:
    drop = symptom_episode(
        kind=SymptomKind.DROP,
        service="cart",
        signal="request_rate",
        peak_score=1.0,
        opened_offset_seconds=100.0,
    )

    incidents = _tracker().observe(
        ts=EPOCH + timedelta(seconds=600.0),
        episodes=(drop,),
        business=_business(drop),
    )

    assert incidents[0].business_impact == pytest.approx(0.56)
    assert incidents[0].severity is IncidentSeverity.HIGH


def test_an_unmeasured_incident_falls_back_below_what_evidence_would_earn() -> None:
    incidents = _tracker().observe(
        ts=EPOCH + timedelta(seconds=300.0),
        episodes=(
            symptom_episode(
                kind=SymptomKind.EDGE_DEGRADED,
                service="frontend",
                signal="dependency.checkout",
                opened_offset_seconds=100.0,
            ),
        ),
    )

    assert incidents[0].business_impact is None
    assert incidents[0].severity is IncidentSeverity.HIGH


def test_impact_counts_only_the_evidence_belonging_to_this_incident() -> None:
    frontend_drop = symptom_episode(
        kind=SymptomKind.DROP,
        service="frontend",
        signal="request_rate",
        peak_score=1.0,
        opened_offset_seconds=100.0,
    )
    email_saturation = symptom_episode(
        kind=SymptomKind.SATURATION,
        service="email",
        signal="container_memory",
        peak_score=1.0,
        opened_offset_seconds=100.0,
    )

    incidents = _tracker().observe(
        ts=EPOCH + timedelta(seconds=600.0),
        episodes=(frontend_drop, email_saturation),
        business=_business(frontend_drop, email_saturation),
    )

    by_service = {incident.services: incident for incident in incidents}
    assert by_service[("frontend",)].business_impact == pytest.approx(0.80)
    # Saturation is not user-visible impact, so its incident has no measured share.
    assert by_service[("email",)].business_impact == 0.0


def test_an_out_of_order_tick_fails_closed() -> None:
    tracker = _tracker()
    tracker.observe(ts=EPOCH + timedelta(seconds=300.0), episodes=_storm())

    with pytest.raises(ValueError, match="must not move backwards"):
        tracker.observe(ts=EPOCH + timedelta(seconds=100.0), episodes=_storm())


def test_duplicate_episode_revisions_fail_closed() -> None:
    episode = symptom_episode(kind=SymptomKind.DROP, opened_offset_seconds=100.0)

    with pytest.raises(ValueError, match="collapsed before clustering"):
        _tracker().observe(ts=EPOCH + timedelta(seconds=300.0), episodes=(episode, episode))


def test_only_the_business_axis_may_estimate_impact() -> None:
    security = AgentAssessment(
        assessment_id="security",
        axis=EvidenceAxis.SECURITY,
        ts=EPOCH + timedelta(seconds=600.0),
        status=AgentStatus.SCORED,
        score=0.0,
        trend=AgentTrend.STEADY,
        covered_kinds=(SymptomKind.RESIDUAL_EXCEED,),
        note="calm",
    )

    with pytest.raises(ValueError, match="business-impact assessment"):
        _tracker().observe(
            ts=EPOCH + timedelta(seconds=600.0),
            episodes=(symptom_episode(kind=SymptomKind.DROP, opened_offset_seconds=100.0),),
            business=security,
        )


def test_an_empty_tick_produces_no_incidents() -> None:
    assert _tracker().observe(ts=EPOCH + timedelta(seconds=300.0), episodes=()) == ()
