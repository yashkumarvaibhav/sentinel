"""Independent security and reliability evidence agents."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from contracts import (
    AgentStatus,
    AgentTrend,
    EpisodeStatus,
    EvidenceAxis,
    EvidenceDirection,
    EvidenceItem,
    SymptomEpisode,
    SymptomKind,
)
from decision import (
    AgentEvidenceWindow,
    ReliabilityEvidenceAgent,
    SecurityEvidenceAgent,
)
from decision.config import EvidenceClaimConfig, load_evidence_agents
from tests.factories import EPOCH, evidence_axis_config, symptom_episode

AGENTS_PATH = Path(__file__).resolve().parents[2] / "config" / "decision-agents.yml"
ALL_KINDS = frozenset(SymptomKind)


def _agents() -> tuple[SecurityEvidenceAgent, ReliabilityEvidenceAgent]:
    config = load_evidence_agents(AGENTS_PATH)
    return (
        SecurityEvidenceAgent(configuration=config.axis(EvidenceAxis.SECURITY)),
        ReliabilityEvidenceAgent(configuration=config.axis(EvidenceAxis.RELIABILITY)),
    )


def _window(
    *episodes: SymptomEpisode,
    offset_seconds: float = 60.0,
    covered: frozenset[SymptomKind] = ALL_KINDS,
) -> AgentEvidenceWindow:
    return AgentEvidenceWindow(
        ts=EPOCH + timedelta(seconds=offset_seconds),
        episodes=episodes,
        covered_kinds=covered,
    )


def test_an_attack_calm_on_reliability_still_lights_security() -> None:
    security, reliability = _agents()
    window = _window(
        symptom_episode(
            kind=SymptomKind.RATIO_DEFORM,
            service="frontend",
            signal="path_entropy",
            peak_score=0.9,
        ),
        symptom_episode(
            kind=SymptomKind.RESIDUAL_EXCEED,
            service="frontend",
            signal="request_rate",
            peak_score=0.8,
        ),
    )

    security_assessment = security.assess(window)
    reliability_assessment = reliability.assess(window)

    assert security_assessment.status is AgentStatus.SCORED
    assert security_assessment.score > 0.8
    assert reliability_assessment.status is AgentStatus.SCORED
    assert reliability_assessment.score == 0.0
    assert reliability_assessment.evidence == ()


def test_a_fault_quiet_on_security_still_lights_reliability() -> None:
    security, reliability = _agents()
    window = _window(
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="checkout",
            signal="dependency.payment",
            peak_score=1.0,
        ),
        symptom_episode(
            kind=SymptomKind.SATURATION,
            service="email",
            signal="container_memory",
            peak_score=0.7,
        ),
    )

    assert reliability.assess(window).score > 0.85
    assert security.assess(window).score == 0.0


def test_neither_agent_sees_the_other_and_order_cannot_change_a_score() -> None:
    first_security, first_reliability = _agents()
    second_security, second_reliability = _agents()
    window = _window(
        symptom_episode(
            kind=SymptomKind.RATIO_DEFORM,
            service="frontend",
            signal="source_entropy",
            peak_score=0.6,
        ),
        symptom_episode(
            kind=SymptomKind.SILENCE,
            service="frontend",
            signal="request_rate",
            peak_score=0.9,
        ),
    )

    security_first = (first_security.assess(window), first_reliability.assess(window))
    reliability_first = (second_reliability.assess(window), second_security.assess(window))

    assert security_first[0] == reliability_first[1]
    assert security_first[1] == reliability_first[0]


def test_every_point_of_score_names_the_value_that_justified_it() -> None:
    security, _ = _agents()
    episode = symptom_episode(
        kind=SymptomKind.RATIO_DEFORM,
        service="frontend",
        signal="path_entropy",
        peak_score=0.5,
        breach_tick_count=4,
    )

    assessment = security.assess(_window(episode))

    assert assessment.score == pytest.approx(0.85 * 0.5)
    assert len(assessment.evidence) == 1
    item = assessment.evidence[0]
    assert isinstance(item, EvidenceItem)
    assert item.feature == "frontend.path_entropy"
    assert item.value == 0.5
    assert item.baseline == 0.0
    assert item.direction is EvidenceDirection.ABOVE_BASELINE
    assert item.contribution == pytest.approx(0.425)
    assert item.evidence_refs == (episode.episode_id,)
    assert "RATIO_DEFORM" in item.note
    assert "4 breaching ticks" in item.note
    assert assessment.services == ("frontend",)


def test_corroborating_kinds_score_above_the_strongest_single_kind() -> None:
    security, _ = _agents()
    alone = security.assess(
        _window(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                signal="path_entropy",
                peak_score=0.6,
            )
        )
    )

    corroborating_agent, _ = _agents()
    corroborated = corroborating_agent.assess(
        _window(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                signal="path_entropy",
                peak_score=0.6,
            ),
            symptom_episode(
                kind=SymptomKind.RESIDUAL_EXCEED,
                signal="request_rate",
                peak_score=0.6,
            ),
        )
    )

    assert corroborated.score > alone.score
    assert corroborated.score < 1.0
    assert len(corroborated.evidence) == 2


def test_an_axis_without_detector_coverage_is_insufficient_not_calm() -> None:
    security, reliability = _agents()
    window = _window(covered=frozenset({SymptomKind.EDGE_DEGRADED}))

    security_assessment = security.assess(window)
    reliability_assessment = reliability.assess(window)

    assert security_assessment.status is AgentStatus.INSUFFICIENT
    assert security_assessment.score == 0.0
    assert security_assessment.trend is AgentTrend.UNKNOWN
    assert security_assessment.covered_kinds == ()
    assert reliability_assessment.status is AgentStatus.SCORED
    assert reliability_assessment.covered_kinds == (SymptomKind.EDGE_DEGRADED,)


def test_a_calm_axis_records_exactly_the_kinds_it_could_see() -> None:
    security, _ = _agents()
    covered = frozenset({SymptomKind.RESIDUAL_EXCEED, SymptomKind.SATURATION})

    assessment = security.assess(_window(covered=covered))

    assert assessment.status is AgentStatus.SCORED
    assert assessment.score == 0.0
    assert assessment.covered_kinds == (SymptomKind.RESIDUAL_EXCEED,)
    assert "1 of 2 claimed symptom kinds" in assessment.note


def test_a_closed_episode_no_longer_contributes() -> None:
    _, reliability = _agents()

    assessment = reliability.assess(
        _window(
            symptom_episode(
                kind=SymptomKind.SATURATION,
                service="email",
                signal="container_memory",
                peak_score=1.0,
                status=EpisodeStatus.CLOSED,
            )
        )
    )

    assert assessment.score == 0.0
    assert assessment.evidence == ()


def test_an_unclaimed_signal_of_a_claimed_kind_is_ignored() -> None:
    security, _ = _agents()

    assessment = security.assess(
        _window(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                service="frontend",
                signal="conversion_ratio",
                peak_score=1.0,
            )
        )
    )

    assert assessment.score == 0.0
    assert assessment.evidence == ()


def test_a_contribution_below_the_floor_is_not_evidence() -> None:
    agent = SecurityEvidenceAgent(
        configuration=evidence_axis_config(
            axis=EvidenceAxis.SECURITY,
            minimum_contribution=0.2,
            claims=(EvidenceClaimConfig(kind=SymptomKind.RESIDUAL_EXCEED, weight=0.5),),
        )
    )

    below = agent.assess(_window(symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, peak_score=0.3)))
    above = agent.assess(
        _window(
            symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, peak_score=0.5),
            offset_seconds=120.0,
        )
    )

    assert below.score == 0.0
    assert below.evidence == ()
    assert above.score == pytest.approx(0.25)


def test_trend_rises_falls_and_holds_inside_the_deadband() -> None:
    agent = SecurityEvidenceAgent(
        configuration=evidence_axis_config(
            axis=EvidenceAxis.SECURITY,
            trend_deadband=0.1,
            claims=(EvidenceClaimConfig(kind=SymptomKind.RESIDUAL_EXCEED, weight=1.0),),
        )
    )

    def tick(peak: float, offset: float) -> AgentTrend:
        return agent.assess(
            _window(
                symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, peak_score=peak),
                offset_seconds=offset,
            )
        ).trend

    assert tick(0.4, 60.0) is AgentTrend.UNKNOWN
    assert tick(0.9, 120.0) is AgentTrend.RISING
    assert tick(0.85, 180.0) is AgentTrend.STEADY
    assert tick(0.5, 240.0) is AgentTrend.FALLING


def test_a_redelivered_tick_repeats_the_previous_answer() -> None:
    security, _ = _agents()
    window = _window(
        symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, peak_score=0.9),
        offset_seconds=60.0,
    )
    later = _window(
        symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, peak_score=0.9),
        offset_seconds=120.0,
    )

    first = security.assess(window)
    repeat = security.assess(window)
    following = security.assess(later)

    assert first == repeat
    assert following.trend is AgentTrend.STEADY


def test_a_tick_moving_backwards_in_event_time_fails_closed() -> None:
    security, _ = _agents()
    security.assess(_window(offset_seconds=120.0))

    with pytest.raises(ValueError, match="must not move backwards"):
        security.assess(_window(offset_seconds=60.0))


def test_duplicate_episode_revisions_must_be_collapsed_first() -> None:
    with pytest.raises(ValueError, match="collapsed"):
        _window(
            symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, episode_id="same"),
            symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, episode_id="same", peak_score=0.4),
        )


def test_an_episode_contradicting_the_stated_coverage_fails_closed() -> None:
    with pytest.raises(ValueError, match="contradicts the stated coverage"):
        _window(
            symptom_episode(kind=SymptomKind.SATURATION),
            covered=frozenset({SymptomKind.RESIDUAL_EXCEED}),
        )


def test_an_episode_opening_after_the_observing_tick_fails_closed() -> None:
    with pytest.raises(ValueError, match="cannot open after"):
        _window(
            symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, opened_offset_seconds=600.0),
            offset_seconds=60.0,
        )


def test_a_naive_tick_timestamp_fails_closed() -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        AgentEvidenceWindow(
            ts=EPOCH.replace(tzinfo=None),
            episodes=(),
            covered_kinds=ALL_KINDS,
        )


def test_an_agent_refuses_another_axis_configuration() -> None:
    config = load_evidence_agents(AGENTS_PATH)

    with pytest.raises(ValueError, match="requires SECURITY configuration"):
        SecurityEvidenceAgent(configuration=config.axis(EvidenceAxis.RELIABILITY))


def test_the_assessment_id_is_stable_for_the_same_evidence() -> None:
    first_security, _ = _agents()
    second_security, _ = _agents()
    episodes = (
        symptom_episode(kind=SymptomKind.RATIO_DEFORM, signal="path_entropy", peak_score=0.7),
        symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, peak_score=0.7),
    )

    forward = first_security.assess(_window(*episodes))
    reversed_order = second_security.assess(_window(*reversed(episodes)))

    assert forward.assessment_id == reversed_order.assessment_id
    assert forward.evidence == reversed_order.evidence


def test_a_scored_assessment_carries_the_axis_it_was_asked_about() -> None:
    security, reliability = _agents()
    window = _window()

    assert security.assess(window).axis is EvidenceAxis.SECURITY
    assert reliability.assess(window).axis is EvidenceAxis.RELIABILITY
