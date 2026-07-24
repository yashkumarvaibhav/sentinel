"""The four deterministic checks that gate every hypothesis."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from common.config import load_config
from contracts import (
    REQUIRED_CHECKS,
    CheckOutcome,
    Incident,
    SymptomEpisode,
    SymptomKind,
    Verification,
    VerificationCheck,
)
from decision import IncidentTracker, verify_incident
from decision.config import load_incidents
from decision.memory import IncidentSignature, SimilarIncident
from tests.factories import EPOCH, symptom_episode

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
TICK = EPOCH + timedelta(seconds=300.0)
COVERED = frozenset({"frontend", "checkout", "payment", "cart", "email"})


def _payment_cascade() -> tuple[SymptomEpisode, ...]:
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
    )


def _incident(episodes: tuple[SymptomEpisode, ...]) -> Incident:
    tracker = IncidentTracker(
        configuration=load_incidents(CONFIG_ROOT / "incidents.yml"),
        topology=load_config(CONFIG_ROOT).topology,
    )
    return tracker.observe(ts=TICK, episodes=episodes)[0]


def _match(similarity: float) -> SimilarIncident:
    return SimilarIncident(
        signature=IncidentSignature(
            incident_id="precedent-1",
            recorded_at=TICK,
            vector=(0.1, 0.9, 0.1, 0.5),
            severity="HIGH",
            origin_service="payment",
        ),
        similarity=similarity,
    )


def _verify(
    episodes: tuple[SymptomEpisode, ...] | None = None,
    *,
    incident: Incident | None = None,
    covered: frozenset[str] = COVERED,
    matches: tuple[SimilarIncident, ...] = (),
    memory_size: int = 0,
) -> Verification:
    members = _payment_cascade() if episodes is None else episodes
    settings = load_incidents(CONFIG_ROOT / "incidents.yml")
    return verify_incident(
        _incident(members) if incident is None else incident,
        episodes=members,
        covered_services=covered,
        matches=matches,
        memory_size=memory_size,
        topology=load_config(CONFIG_ROOT).topology,
        configuration=settings.verification,
        memory=settings.memory,
        causal=settings.causal,
    )


def _outcomes(verification: Verification) -> dict[str, CheckOutcome]:
    return {check.name: check.outcome for check in verification.checks}


def test_a_real_cascade_is_confirmed_on_an_empty_memory_via_the_bootstrap() -> None:
    verification = _verify()

    outcomes = _outcomes(verification)
    assert set(outcomes) == set(REQUIRED_CHECKS)
    assert outcomes["temporal_causality"] is CheckOutcome.PASSED
    assert outcomes["trace_coverage"] is CheckOutcome.PASSED
    assert outcomes["dependency_validity"] is CheckOutcome.PASSED
    assert outcomes["memory_similarity"] is CheckOutcome.BOOTSTRAP
    assert verification.confirmed is True


def test_the_bootstrap_is_recorded_as_a_bootstrap_never_as_a_confirmation() -> None:
    verification = _verify(memory_size=3)

    memory_check = next(check for check in verification.checks if check.name == "memory_similarity")
    assert memory_check.outcome is CheckOutcome.BOOTSTRAP
    assert "vacuously as a bootstrap" in memory_check.detail
    assert "3 of the 20 entries" in memory_check.detail


def test_a_caller_noticing_its_callee_counts_as_observing_the_origin() -> None:
    """The origin's own log burst lands last; the edge that accused it did not."""
    verification = _verify()

    causality = next(check for check in verification.checks if check.name == "temporal_causality")
    assert causality.outcome is CheckOutcome.PASSED
    assert "payment was observed misbehaving first" in causality.detail


def test_a_hypothesis_that_names_a_late_service_as_the_cause_is_blocked() -> None:
    """Something broke long before the accused origin, so the story cannot hold."""
    episodes = (
        *_payment_cascade(),
        symptom_episode(
            kind=SymptomKind.SATURATION,
            service="cart",
            signal="container_memory",
            opened_offset_seconds=0.0,
        ),
    )
    accused = _incident(episodes).model_copy(
        update={"origin_service": "payment", "origin_confidence": 0.6}
    )

    verification = _verify(episodes, incident=accused)

    causality = next(check for check in verification.checks if check.name == "temporal_causality")
    assert causality.outcome is CheckOutcome.FAILED
    assert "cart broke" in causality.detail
    assert verification.confirmed is False


def test_a_storm_that_points_nowhere_is_never_confirmed() -> None:
    episodes = _payment_cascade()
    incident = _incident(episodes).model_copy(
        update={"origin_service": None, "origin_confidence": None}
    )

    verification = _verify(episodes, incident=incident)

    causality = next(check for check in verification.checks if check.name == "temporal_causality")
    assert causality.outcome is CheckOutcome.FAILED
    assert "did not collapse to an origin" in causality.detail
    assert verification.confirmed is False


def test_an_unwatched_service_blocks_confirmation() -> None:
    verification = _verify(covered=frozenset({"frontend", "checkout"}))

    coverage = next(check for check in verification.checks if check.name == "trace_coverage")
    assert coverage.outcome is CheckOutcome.FAILED
    assert "payment" in coverage.detail
    assert verification.confirmed is False


def test_an_invented_dependency_edge_blocks_confirmation() -> None:
    episodes = (
        *_payment_cascade(),
        symptom_episode(
            kind=SymptomKind.EDGE_DEGRADED,
            service="payment",
            signal="dependency.cart",
            opened_offset_seconds=150.0,
        ),
    )

    verification = _verify(episodes)

    validity = next(check for check in verification.checks if check.name == "dependency_validity")
    assert validity.outcome is CheckOutcome.FAILED
    assert "payment->cart" in validity.detail
    assert verification.confirmed is False


def test_a_populated_memory_with_no_resemblance_blocks_confirmation() -> None:
    verification = _verify(memory_size=50)

    memory_check = next(check for check in verification.checks if check.name == "memory_similarity")
    assert memory_check.outcome is CheckOutcome.FAILED
    assert "nothing in a memory of 50 incidents" in memory_check.detail
    assert verification.confirmed is False


def test_a_populated_memory_with_a_strong_precedent_confirms() -> None:
    verification = _verify(memory_size=50, matches=(_match(0.92),))

    assert _outcomes(verification)["memory_similarity"] is CheckOutcome.PASSED
    assert verification.confirmed is True


def test_a_weak_precedent_does_not_count_as_recognition() -> None:
    verification = _verify(memory_size=50, matches=(_match(0.40),))

    memory_check = next(check for check in verification.checks if check.name == "memory_similarity")
    assert memory_check.outcome is CheckOutcome.FAILED
    assert "under the 0.60 floor" in memory_check.detail


def test_verification_is_stable_for_the_same_evidence() -> None:
    assert _verify().verification_id == _verify().verification_id


def test_verifying_without_every_member_episode_fails_closed() -> None:
    settings = load_incidents(CONFIG_ROOT / "incidents.yml")
    episodes = _payment_cascade()
    incident = _incident(episodes)

    with pytest.raises(ValueError, match="every member episode"):
        verify_incident(
            incident,
            episodes=episodes[:1],
            covered_services=COVERED,
            matches=(),
            memory_size=0,
            topology=load_config(CONFIG_ROOT).topology,
            configuration=settings.verification,
            memory=settings.memory,
            causal=settings.causal,
        )


def test_a_verification_missing_a_check_is_rejected_by_the_contract() -> None:
    with pytest.raises(ValueError, match="exactly the required checks"):
        Verification(
            verification_id="v1",
            ts=TICK,
            incident_id="incident-1",
            confirmed=True,
            checks=(
                VerificationCheck(
                    name="temporal_causality",
                    outcome=CheckOutcome.PASSED,
                    detail="fine",
                ),
            ),
        )


def test_confirmed_cannot_disagree_with_the_checks() -> None:
    checks = tuple(
        VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED if name == "trace_coverage" else CheckOutcome.PASSED,
            detail="detail",
        )
        for name in REQUIRED_CHECKS
    )

    with pytest.raises(ValueError, match="confirmed must be true exactly when"):
        Verification(
            verification_id="v1",
            ts=TICK,
            incident_id="incident-1",
            confirmed=True,
            checks=checks,
        )
