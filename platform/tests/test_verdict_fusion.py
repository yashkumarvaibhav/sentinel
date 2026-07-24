"""Evidence fusion: ordered rules, class distribution, differential diagnosis."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from common.config import load_config
from contracts import (
    AgentAssessment,
    AgentStatus,
    AgentTrend,
    EvidenceAxis,
    EvidenceDirection,
    EvidenceItem,
    ReasonSubtype,
    SymptomEpisode,
    SymptomKind,
    VerdictClass,
)
from decision import (
    AgentEvidenceWindow,
    BusinessImpactEvidenceAgent,
    ChangeConfigEvidenceAgent,
    EvidenceFusion,
    FusionStatus,
    ReliabilityEvidenceAgent,
    SecurityEvidenceAgent,
)
from decision.changes import FlagChange, flag_changes
from decision.config import (
    load_deployment_ledger,
    load_evidence_agents,
    load_verdict_rules,
)
from tests.factories import EPOCH, symptom_episode

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
ALL_KINDS = frozenset(SymptomKind)
TICK = EPOCH + timedelta(seconds=600.0)


def _fusion() -> EvidenceFusion:
    return EvidenceFusion(configuration=load_verdict_rules(CONFIG_ROOT / "verdict-rules.yml"))


def _assess(
    *episodes: SymptomEpisode,
    changes: tuple[object, ...] = (),
    covered: frozenset[SymptomKind] = ALL_KINDS,
) -> tuple[AgentAssessment, ...]:
    """Run every real agent over one window, exactly as the runtime will."""
    agents_config = load_evidence_agents(CONFIG_ROOT / "decision-agents.yml")
    topology = load_config(CONFIG_ROOT).topology
    criticality = {service.service: service.criticality for service in topology.services}
    window = AgentEvidenceWindow(
        ts=TICK,
        episodes=episodes,
        covered_kinds=covered,
        changes=changes,  # type: ignore[arg-type]
    )
    return (
        SecurityEvidenceAgent(configuration=agents_config.axis(EvidenceAxis.SECURITY)).assess(
            window
        ),
        ReliabilityEvidenceAgent(configuration=agents_config.axis(EvidenceAxis.RELIABILITY)).assess(
            window
        ),
        ChangeConfigEvidenceAgent(
            configuration=agents_config.axis(EvidenceAxis.CHANGE_CONFIG)
        ).assess(window),
        BusinessImpactEvidenceAgent(
            configuration=agents_config.axis(EvidenceAxis.BUSINESS_IMPACT),
            criticality=criticality,
        ).assess(window),
    )


def _flag(*, flag: str = "paymentFailure", offset_seconds: float) -> object:
    ledger = load_deployment_ledger(CONFIG_ROOT / "deployments.yml")
    return flag_changes(
        (
            FlagChange(
                flag=flag,
                previous_variant="off",
                current_variant="on",
                ts=EPOCH + timedelta(seconds=offset_seconds),
            ),
        ),
        ledger=ledger,
        honesty="SIMULATED",
    )[0]


def _stub(
    axis: EvidenceAxis,
    *,
    score: float,
    status: AgentStatus = AgentStatus.SCORED,
    kinds: tuple[SymptomKind, ...] = (SymptomKind.RESIDUAL_EXCEED,),
) -> AgentAssessment:
    """A hand-built assessment, for exercising axis combinations directly."""
    if status is AgentStatus.INSUFFICIENT:
        return AgentAssessment(
            assessment_id=f"{axis.value}-insufficient",
            axis=axis,
            ts=TICK,
            status=status,
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
        status=status,
        score=score,
        trend=AgentTrend.STEADY,
        covered_kinds=kinds,
        contributing_kinds=kinds if evidence else (),
        services=("frontend",) if evidence else (),
        evidence=evidence,
        note="stub",
    )


def test_a_hostile_surge_on_a_healthy_mesh_is_an_attack() -> None:
    result = _fusion().fuse(
        _assess(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                service="frontend",
                signal="path_entropy",
                peak_score=0.9,
            ),
            symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, service="frontend", peak_score=0.9),
        )
    )

    assert result.status is FusionStatus.DECIDED
    verdict = result.verdict
    assert verdict is not None
    assert verdict.verdict_class is VerdictClass.ATTACK
    assert verdict.rule_id == "hostile-behavior-without-degradation"
    assert verdict.distribution[VerdictClass.ATTACK.value] > 0.5
    assert set(verdict.corroborating_kinds) == {
        SymptomKind.RATIO_DEFORM,
        SymptomKind.RESIDUAL_EXCEED,
    }


def test_degradation_with_no_hostility_or_change_is_an_operational_fault() -> None:
    result = _fusion().fuse(
        _assess(
            symptom_episode(
                kind=SymptomKind.EDGE_DEGRADED,
                service="checkout",
                signal="dependency.payment",
                peak_score=1.0,
            )
        )
    )

    verdict = result.verdict
    assert verdict is not None
    assert verdict.verdict_class is VerdictClass.OPERATIONAL_FAULT
    assert verdict.reason_subtype is None


def test_saturation_driven_degradation_is_named_a_capacity_shortage() -> None:
    result = _fusion().fuse(
        _assess(
            symptom_episode(
                kind=SymptomKind.SATURATION,
                service="email",
                signal="container_memory",
                peak_score=0.9,
            )
        )
    )

    verdict = result.verdict
    assert verdict is not None
    assert verdict.verdict_class is VerdictClass.OPERATIONAL_FAULT
    assert verdict.reason_subtype is ReasonSubtype.CAPACITY_SHORTAGE


def test_degradation_right_after_our_own_flag_flip_is_a_code_config_fault() -> None:
    result = _fusion().fuse(
        _assess(
            symptom_episode(
                kind=SymptomKind.EDGE_DEGRADED,
                service="payment",
                signal="dependency.payment",
                peak_score=1.0,
            ),
            changes=(_flag(offset_seconds=599.0),),
        )
    )

    verdict = result.verdict
    assert verdict is not None
    assert verdict.verdict_class is VerdictClass.CODE_CONFIG_FAULT
    assert verdict.rule_id == "degradation-follows-our-change"
    assert any(item.feature == "payment.change.flag" for item in verdict.evidence)


def test_hostility_inside_a_real_fault_is_a_combination() -> None:
    result = _fusion().fuse(
        _assess(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                service="frontend",
                signal="path_entropy",
                peak_score=0.9,
            ),
            symptom_episode(
                kind=SymptomKind.EDGE_DEGRADED,
                service="checkout",
                signal="dependency.payment",
                peak_score=0.9,
            ),
        )
    )

    verdict = result.verdict
    assert verdict is not None
    assert verdict.verdict_class is VerdictClass.COMBINATION
    assert verdict.distribution[VerdictClass.ATTACK.value] < verdict.distribution["COMBINATION"]


def test_a_quiet_tick_with_full_coverage_is_explained_by_context() -> None:
    result = _fusion().fuse(_assess())

    verdict = result.verdict
    assert verdict is not None
    assert verdict.verdict_class is VerdictClass.EXPECTED_EVENT
    assert verdict.evidence == ()


def test_every_losing_class_is_recorded_with_the_requirement_it_failed() -> None:
    result = _fusion().fuse(
        _assess(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                service="frontend",
                signal="path_entropy",
                peak_score=0.9,
            )
        )
    )

    verdict = result.verdict
    assert verdict is not None
    rejected = {item.verdict_class: item.reason for item in verdict.rejected_alternatives}
    assert set(rejected) == set(VerdictClass) - {verdict.verdict_class}
    assert "RELIABILITY is not lit" in rejected[VerdictClass.COMBINATION]
    assert "SECURITY is not known to be quiet" in rejected[VerdictClass.EXPECTED_EVENT]


def test_the_distribution_covers_every_class_and_sums_to_one() -> None:
    result = _fusion().fuse(
        _assess(symptom_episode(kind=SymptomKind.RESIDUAL_EXCEED, peak_score=0.7))
    )

    verdict = result.verdict
    assert verdict is not None
    assert set(verdict.distribution) == {member.value for member in VerdictClass}
    assert sum(verdict.distribution.values()) == pytest.approx(1.0)


def test_corroboration_from_independent_kinds_raises_confidence() -> None:
    single = _fusion().fuse(
        _assess(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                service="frontend",
                signal="path_entropy",
                peak_score=1.0,
            )
        )
    )
    corroborated = _fusion().fuse(
        _assess(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                service="frontend",
                signal="path_entropy",
                peak_score=1.0,
            ),
            symptom_episode(
                kind=SymptomKind.RESIDUAL_EXCEED,
                service="frontend",
                peak_score=1.0,
            ),
        )
    )

    assert single.verdict is not None
    assert corroborated.verdict is not None
    assert corroborated.verdict.verdict_class is single.verdict.verdict_class
    assert corroborated.verdict.confidence > single.verdict.confidence


def test_confidence_never_reaches_certainty_from_one_axis_alone() -> None:
    result = _fusion().fuse(
        _assess(
            symptom_episode(
                kind=SymptomKind.RATIO_DEFORM,
                service="frontend",
                signal="path_entropy",
                peak_score=1.0,
            )
        )
    )

    assert result.verdict is not None
    assert result.verdict.confidence < 1.0


def test_an_unmeasured_reliability_axis_blocks_calling_it_a_pure_attack() -> None:
    result = _fusion().fuse(
        (
            _stub(EvidenceAxis.SECURITY, score=0.9),
            _stub(EvidenceAxis.RELIABILITY, score=0.0, status=AgentStatus.INSUFFICIENT),
            _stub(EvidenceAxis.CHANGE_CONFIG, score=0.0, status=AgentStatus.INSUFFICIENT),
            _stub(EvidenceAxis.BUSINESS_IMPACT, score=0.0, status=AgentStatus.INSUFFICIENT),
        )
    )

    assert result.status is FusionStatus.INSUFFICIENT
    assert result.verdict is None
    assert "never measured" in result.note or "unmeasured" in result.note


def test_a_missing_change_feed_still_allows_diagnosing_a_fault() -> None:
    result = _fusion().fuse(
        (
            _stub(EvidenceAxis.SECURITY, score=0.0, kinds=(SymptomKind.RESIDUAL_EXCEED,)),
            _stub(
                EvidenceAxis.RELIABILITY,
                score=0.9,
                kinds=(SymptomKind.EDGE_DEGRADED,),
            ),
            _stub(EvidenceAxis.CHANGE_CONFIG, score=0.0, status=AgentStatus.INSUFFICIENT),
            _stub(EvidenceAxis.BUSINESS_IMPACT, score=0.0, status=AgentStatus.INSUFFICIENT),
        )
    )

    assert result.verdict is not None
    assert result.verdict.verdict_class is VerdictClass.OPERATIONAL_FAULT


def test_an_unmeasured_axis_is_uncertainty_not_good_news() -> None:
    blind = _fusion().fuse(
        tuple(_stub(axis, score=0.0, status=AgentStatus.INSUFFICIENT) for axis in EvidenceAxis)
    )

    assert blind.status is FusionStatus.INSUFFICIENT
    assert blind.verdict is None


def test_fusing_the_same_evidence_twice_yields_the_same_verdict_id() -> None:
    episodes = (
        symptom_episode(
            kind=SymptomKind.RATIO_DEFORM,
            service="frontend",
            signal="path_entropy",
            peak_score=0.8,
        ),
    )

    first = _fusion().fuse(_assess(*episodes))
    second = _fusion().fuse(_assess(*episodes))

    assert first.verdict is not None
    assert second.verdict is not None
    assert first.verdict.verdict_id == second.verdict.verdict_id


def test_two_assessments_of_one_axis_fail_closed() -> None:
    with pytest.raises(ValueError, match="assessed twice"):
        _fusion().fuse(
            (_stub(EvidenceAxis.SECURITY, score=0.9), _stub(EvidenceAxis.SECURITY, score=0.1))
        )


def test_assessments_from_different_ticks_fail_closed() -> None:
    later = _stub(EvidenceAxis.RELIABILITY, score=0.9).model_copy(
        update={"ts": TICK + timedelta(seconds=60)}
    )

    with pytest.raises(ValueError, match="one event time"):
        _fusion().fuse((_stub(EvidenceAxis.SECURITY, score=0.1), later))


def test_fusing_nothing_fails_closed() -> None:
    with pytest.raises(ValueError, match="at least one axis"):
        _fusion().fuse(())
