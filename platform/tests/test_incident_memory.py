"""Incident memory: signatures, similarity ranking and the bounded nudge."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from common.config import load_config
from common.storage import IncidentSignatureRecord
from contracts import (
    AgentAssessment,
    AgentStatus,
    AgentTrend,
    EvidenceAxis,
    EvidenceDirection,
    EvidenceItem,
    Incident,
    SymptomKind,
    Verdict,
    VerdictClass,
)
from decision import (
    IncidentSignature,
    IncidentTracker,
    build_signature,
    nearest,
    recognized,
)
from decision.config import IncidentMemoryConfig, load_incidents
from decision.memory import SIGNATURE_AXES, similarity, similarity_from_distance
from tests.factories import EPOCH, symptom_episode

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
TICK = EPOCH + timedelta(seconds=600.0)


def _memory_config() -> IncidentMemoryConfig:
    return load_incidents(CONFIG_ROOT / "incidents.yml").memory


def _incident() -> Incident:
    tracker = IncidentTracker(
        configuration=load_incidents(CONFIG_ROOT / "incidents.yml"),
        topology=load_config(CONFIG_ROOT).topology,
    )
    return tracker.observe(
        ts=TICK,
        episodes=(
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
                opened_offset_seconds=110.0,
            ),
        ),
    )[0]


def _assessment(axis: EvidenceAxis, score: float, *, scored: bool = True) -> AgentAssessment:
    if not scored:
        return AgentAssessment(
            assessment_id=f"{axis.value}-none",
            axis=axis,
            ts=TICK,
            status=AgentStatus.INSUFFICIENT,
            score=0.0,
            trend=AgentTrend.UNKNOWN,
            note="no coverage",
        )
    evidence: tuple[EvidenceItem, ...] = ()
    if score > 0.0:
        evidence = (
            EvidenceItem(
                feature=f"frontend.{axis.value.lower()}",
                value=score,
                baseline=0.0,
                direction=EvidenceDirection.ABOVE_BASELINE,
                contribution=score,
                note="stub evidence",
            ),
        )
    return AgentAssessment(
        assessment_id=f"{axis.value}-{score}",
        axis=axis,
        ts=TICK,
        status=AgentStatus.SCORED,
        score=score,
        trend=AgentTrend.STEADY,
        covered_kinds=(SymptomKind.RESIDUAL_EXCEED,),
        contributing_kinds=(SymptomKind.RESIDUAL_EXCEED,) if evidence else (),
        services=("frontend",) if evidence else (),
        evidence=evidence,
        note="stub",
    )


def _all_scored(*scores: float) -> tuple[AgentAssessment, ...]:
    return tuple(
        _assessment(axis, score) for axis, score in zip(SIGNATURE_AXES, scores, strict=True)
    )


def _signature(incident_id: str, *vector: float, origin: str | None = None) -> IncidentSignature:
    return IncidentSignature(
        incident_id=incident_id,
        recorded_at=TICK,
        vector=vector,
        severity="HIGH",
        origin_service=origin,
    )


def _verdict(confidence: float = 0.70) -> Verdict:
    return Verdict(
        verdict_id="verdict-1",
        ts=TICK,
        verdict_class=VerdictClass.OPERATIONAL_FAULT,
        rule_id="degradation-without-hostility-or-change",
        confidence=confidence,
        reason="services degraded with no hostile behavior",
        distribution={
            "EXPECTED_EVENT": 0.1,
            "ATTACK": 0.1,
            "OPERATIONAL_FAULT": 0.6,
            "CODE_CONFIG_FAULT": 0.1,
            "COMBINATION": 0.1,
        },
    )


def test_a_signature_is_the_four_axis_scores_in_a_fixed_order() -> None:
    signature = build_signature(_incident(), assessments=_all_scored(0.1, 0.9, 0.2, 0.4))

    assert signature is not None
    assert signature.vector == (0.1, 0.9, 0.2, 0.4)
    assert signature.severity in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
    assert signature.origin_service == "payment"


def test_an_incident_with_an_unmeasured_axis_is_not_remembered() -> None:
    partial = (
        _assessment(EvidenceAxis.SECURITY, 0.1),
        _assessment(EvidenceAxis.RELIABILITY, 0.9),
        _assessment(EvidenceAxis.CHANGE_CONFIG, 0.0, scored=False),
        _assessment(EvidenceAxis.BUSINESS_IMPACT, 0.4),
    )

    assert build_signature(_incident(), assessments=partial) is None


def test_a_missing_axis_is_not_quietly_padded() -> None:
    assert build_signature(_incident(), assessments=_all_scored(0.1, 0.9, 0.2, 0.4)[:3]) is None


def test_one_axis_may_contribute_only_once() -> None:
    duplicated = (*_all_scored(0.1, 0.9, 0.2, 0.4), _assessment(EvidenceAxis.SECURITY, 0.5))

    with pytest.raises(ValueError, match="only once"):
        build_signature(_incident(), assessments=duplicated)


def test_the_signature_records_the_verdict_class_when_there_is_one() -> None:
    signature = build_signature(
        _incident(), assessments=_all_scored(0.1, 0.9, 0.2, 0.4), verdict=_verdict()
    )

    assert signature is not None
    assert signature.verdict_class == VerdictClass.OPERATIONAL_FAULT.value


def test_identical_shapes_are_perfectly_similar_and_opposites_are_not() -> None:
    same = similarity(_signature("a", 0.2, 0.8, 0.1, 0.5), _signature("b", 0.2, 0.8, 0.1, 0.5))
    opposite = similarity(_signature("a", 0.0, 0.0, 0.0, 0.0), _signature("b", 1.0, 1.0, 1.0, 1.0))

    assert same == 1.0
    assert opposite == 0.0


def test_stored_distance_and_in_memory_similarity_agree() -> None:
    left = _signature("a", 0.2, 0.8, 0.1, 0.5)
    right = _signature("b", 0.3, 0.7, 0.1, 0.5)
    distance = sum((x - y) ** 2 for x, y in zip(left.vector, right.vector, strict=True)) ** 0.5

    assert similarity_from_distance(distance) == pytest.approx(similarity(left, right))


def test_the_nearest_precedent_ranks_first_and_weak_matches_are_dropped() -> None:
    probe = _signature("current", 0.1, 0.9, 0.1, 0.5)
    memory = (
        _signature("near", 0.15, 0.85, 0.1, 0.5, origin="payment"),
        _signature("middling", 0.4, 0.6, 0.3, 0.4),
        _signature("opposite", 1.0, 0.0, 1.0, 0.0),
    )

    matches = nearest(probe, memory, configuration=_memory_config())

    assert [match.signature.incident_id for match in matches] == ["near", "middling"]
    assert matches[0].similarity > matches[1].similarity


def test_an_incident_is_never_its_own_precedent() -> None:
    probe = _signature("current", 0.1, 0.9, 0.1, 0.5)

    assert nearest(probe, (probe,), configuration=_memory_config()) == ()


def test_recognition_nudges_confidence_without_ever_carrying_the_verdict() -> None:
    probe = _signature("current", 0.1, 0.9, 0.1, 0.5)
    matches = nearest(
        probe,
        (_signature("near", 0.1, 0.9, 0.1, 0.5, origin="payment"),),
        configuration=_memory_config(),
    )

    nudged = recognized(_verdict(0.70), matches, configuration=_memory_config())

    assert nudged.confidence == pytest.approx(0.75)
    assert nudged.confidence - 0.70 == pytest.approx(_memory_config().confidence_nudge)
    assert nudged.verdict_class is VerdictClass.OPERATIONAL_FAULT
    assert nudged.evidence[-1].feature == "incident.memory_similarity"
    assert "originated in payment" in nudged.evidence[-1].note


def test_a_barely_credible_match_nudges_barely() -> None:
    probe = _signature("current", 0.0, 0.0, 0.0, 0.0)
    barely = _signature("far", 0.35, 0.35, 0.35, 0.35)
    matches = nearest(probe, (barely,), configuration=_memory_config())

    nudged = recognized(_verdict(0.70), matches, configuration=_memory_config())

    assert matches
    assert 0.70 < nudged.confidence < 0.71


def test_a_match_exactly_at_the_similarity_floor_moves_nothing() -> None:
    probe = _signature("current", 0.0, 0.0, 0.0, 0.0)
    at_floor = _signature("edge", 0.4, 0.4, 0.4, 0.4)
    matches = nearest(probe, (at_floor,), configuration=_memory_config())

    nudged = recognized(_verdict(0.70), matches, configuration=_memory_config())

    assert matches[0].similarity == pytest.approx(_memory_config().minimum_similarity)
    assert nudged.confidence == 0.70
    assert nudged.evidence[-1].contribution == 0.0


def test_no_credible_precedent_leaves_the_verdict_untouched() -> None:
    verdict = _verdict(0.70)

    assert recognized(verdict, (), configuration=_memory_config()) == verdict


def test_the_nudge_can_never_push_confidence_past_certainty() -> None:
    probe = _signature("current", 0.1, 0.9, 0.1, 0.5)
    matches = nearest(
        probe, (_signature("near", 0.1, 0.9, 0.1, 0.5),), configuration=_memory_config()
    )

    nudged = recognized(_verdict(0.99), matches, configuration=_memory_config())

    assert nudged.confidence == 1.0


def test_a_signature_must_be_four_bounded_dimensions() -> None:
    with pytest.raises(ValueError, match="exactly 4 dimensions"):
        _signature("bad", 0.1, 0.2)

    with pytest.raises(ValueError, match="probability"):
        _signature("bad", 0.1, 0.2, 0.3, 1.5)


def test_a_similarity_floor_of_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="every incident a match"):
        IncidentMemoryConfig(
            neighbours=5,
            minimum_similarity=0.0,
            confidence_nudge=0.05,
            bootstrap_minimum_entries=20,
        )


def test_the_stored_record_rejects_an_out_of_range_dimension() -> None:
    with pytest.raises(ValueError, match="probability"):
        IncidentSignatureRecord(
            incident_id="incident-1",
            recorded_at=TICK,
            vector=(0.1, 0.2, 0.3, 2.0),
            severity="HIGH",
        )


def test_the_stored_record_requires_utc() -> None:
    with pytest.raises(ValueError, match="UTC"):
        IncidentSignatureRecord(
            incident_id="incident-1",
            recorded_at=TICK.replace(tzinfo=None),
            vector=(0.1, 0.2, 0.3, 0.4),
            severity="HIGH",
        )
