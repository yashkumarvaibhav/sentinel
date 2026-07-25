"""Collapsing a symptom storm to the one service that started it."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from common.config import TopologyConfig, load_config
from contracts import SymptomEpisode, SymptomKind
from decision import CausalCollapse, IncidentTracker, collapse_to_origin
from decision.causal import dependents
from decision.config import CausalCollapseConfig, load_incidents
from tests.factories import EPOCH, symptom_episode

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


def _topology() -> TopologyConfig:
    return load_config(CONFIG_ROOT).topology


def _causal() -> CausalCollapseConfig:
    return load_incidents(CONFIG_ROOT / "incidents.yml").causal


def _collapse(*episodes: SymptomEpisode) -> CausalCollapse:
    return collapse_to_origin(episodes, configuration=_causal(), topology=_topology())


def _payment_cascade() -> tuple[SymptomEpisode, ...]:
    """The real cascade: payment breaks, checkout waits, frontend screams."""
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


def test_a_cascade_collapses_to_the_true_origin_not_the_loudest_service() -> None:
    collapse = _collapse(*_payment_cascade())

    assert collapse.origin is not None
    assert collapse.origin.service == "payment"


def test_the_service_that_started_first_is_not_automatically_the_origin() -> None:
    cascade = _payment_cascade()
    collapse = _collapse(*cascade)

    ranked = {candidate.service: candidate for candidate in collapse.candidates}
    # checkout starts the storm and still loses: its own trouble is explained by
    # the payment dependency it is waiting on. payment ties it on onset - the
    # accusing edge is the first observation of payment, which is why its own
    # later log burst does not hold it back - and wins on the other two priors.
    assert ranked["checkout"].onset_score == 1.0
    assert ranked["checkout"].accusation_score == 0.0
    assert ranked["payment"].onset_score == 1.0
    assert ranked["payment"].accusation_score > 0.0
    assert ranked["payment"].reachability_score > ranked["checkout"].reachability_score
    assert ranked["payment"].score > ranked["checkout"].score
    # frontend screams loudest, started late and is accused by nothing.
    assert ranked["frontend"].onset_score < 1.0


def test_a_degraded_edge_accuses_the_callee_and_exonerates_the_caller() -> None:
    collapse = _collapse(
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="checkout",
            signal="dependency.payment",
            opened_offset_seconds=100.0,
        ),
        symptom_episode(
            kind=SymptomKind.LOG_BURST,
            service="payment",
            signal="log_template_rate",
            opened_offset_seconds=120.0,
        ),
    )

    ranked = {candidate.service: candidate for candidate in collapse.candidates}
    assert ranked["payment"].accusation_score == 1.0
    assert ranked["checkout"].accusation_score == 0.0
    assert collapse.origin is not None
    assert collapse.origin.service == "payment"


def _silent_origin_cascade() -> tuple[SymptomEpisode, ...]:
    """The shape measured on the real development cascade: the origin says nothing.

    Both detectors that fired are edges. payment - the service that actually
    broke - emitted no episode of its own; the only evidence it exists at all
    is checkout's degraded call to it.
    """
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
            opened_offset_seconds=100.0,
        ),
    )


def test_a_silent_origin_is_still_named_by_the_edges_that_accuse_it() -> None:
    collapse = _collapse(*_silent_origin_cascade())

    assert collapse.origin is not None
    assert collapse.origin.service == "payment"
    assert collapse.implicated == ("payment",)


def test_a_symptomatic_service_is_never_reported_as_merely_implicated() -> None:
    collapse = _collapse(*_payment_cascade())

    assert collapse.origin is not None
    assert collapse.origin.service == "payment"
    assert collapse.implicated == ()


def test_an_edge_naming_an_unknown_service_cannot_put_it_in_the_ranking() -> None:
    """A degraded edge to nowhere is a configuration fault, not a new service."""
    collapse = _collapse(
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="checkout",
            signal="dependency.ghost-service",
            opened_offset_seconds=100.0,
        ),
    )

    assert collapse.implicated == ()
    assert [candidate.service for candidate in collapse.candidates] == ["checkout"]


def test_an_edge_that_is_not_a_committed_dependency_cannot_implicate_anyone() -> None:
    collapse = _collapse(
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="email",
            signal="dependency.payment",
            opened_offset_seconds=100.0,
        ),
    )

    assert collapse.implicated == ()
    assert [candidate.service for candidate in collapse.candidates] == ["email"]


def test_the_incident_separates_what_it_saw_from_what_it_was_told() -> None:
    tracker = IncidentTracker(
        configuration=load_incidents(CONFIG_ROOT / "incidents.yml"),
        topology=load_config(CONFIG_ROOT).topology,
    )

    incidents = tracker.observe(
        ts=EPOCH + timedelta(seconds=300.0), episodes=_silent_origin_cascade()
    )

    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.services == ("checkout", "frontend")
    assert incident.implicated_services == ("payment",)
    assert incident.origin_service == "payment"


def test_an_evenly_balanced_storm_names_no_origin_at_all() -> None:
    collapse = _collapse(
        symptom_episode(
            kind=SymptomKind.SATURATION,
            service="cart",
            signal="container_memory",
            opened_offset_seconds=100.0,
        ),
        symptom_episode(
            kind=SymptomKind.SATURATION,
            service="email",
            signal="container_memory",
            opened_offset_seconds=100.0,
        ),
    )

    assert collapse.origin is None
    assert "margin" in collapse.note
    assert len(collapse.candidates) == 2


def test_a_single_service_storm_collapses_to_that_service() -> None:
    collapse = _collapse(
        symptom_episode(
            kind=SymptomKind.SATURATION,
            service="email",
            signal="container_memory",
            opened_offset_seconds=100.0,
        )
    )

    assert collapse.origin is not None
    assert collapse.origin.service == "email"


def test_collapsing_nothing_names_nothing() -> None:
    collapse = _collapse()

    assert collapse.origin is None
    assert collapse.candidates == ()


def test_transitive_dependents_follow_the_committed_graph() -> None:
    reached = dependents(_topology())

    assert reached["payment"] == frozenset({"checkout", "frontend"})
    assert reached["checkout"] == frozenset({"frontend"})
    assert reached["frontend"] == frozenset()


def test_the_incident_carries_its_collapsed_origin_and_confidence() -> None:
    tracker = IncidentTracker(
        configuration=load_incidents(CONFIG_ROOT / "incidents.yml"),
        topology=load_config(CONFIG_ROOT).topology,
    )

    incidents = tracker.observe(ts=EPOCH + timedelta(seconds=300.0), episodes=_payment_cascade())

    assert len(incidents) == 1
    assert incidents[0].origin_service == "payment"
    assert incidents[0].origin_confidence is not None
    assert 0.0 < incidents[0].origin_confidence <= 1.0
    assert "origin: payment leads" in incidents[0].note


def test_the_incident_confidence_is_the_winning_candidate_own_score() -> None:
    tracker = IncidentTracker(
        configuration=load_incidents(CONFIG_ROOT / "incidents.yml"),
        topology=load_config(CONFIG_ROOT).topology,
    )
    cascade = _payment_cascade()

    incidents = tracker.observe(ts=EPOCH + timedelta(seconds=300.0), episodes=cascade)
    collapse = _collapse(*cascade)

    assert collapse.origin is not None
    assert incidents[0].origin_confidence == pytest.approx(collapse.origin.score)


def test_an_incident_whose_storm_points_nowhere_records_no_origin() -> None:
    """A tie inside one cluster must leave the origin unnamed, not guessed."""
    tie = _collapse(
        symptom_episode(
            kind=SymptomKind.SATURATION,
            service="cart",
            signal="container_memory",
            opened_offset_seconds=100.0,
        ),
        symptom_episode(
            kind=SymptomKind.SATURATION,
            service="email",
            signal="container_memory",
            opened_offset_seconds=100.0,
        ),
    )

    assert tie.origin is None
    assert {candidate.score for candidate in tie.candidates} == {tie.candidates[0].score}


def test_the_causal_priors_must_be_weighted_to_sum_to_one() -> None:
    with pytest.raises(ValueError, match="sum to one"):
        CausalCollapseConfig(
            onset_weight=0.5,
            reachability_weight=0.5,
            accusation_weight=0.5,
            minimum_margin=0.05,
            dependency_signal_prefix="dependency.",
        )
