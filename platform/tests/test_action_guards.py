"""Blast-radius guards and the canary that widens an action instead of firing it.

The committed `config/cohorts.yml` is used wherever the question is "what does
this platform actually protect", because that file is the operator's statement
and a test that invented its own cohorts would prove nothing about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from action import (
    BLAST_CAP_GATE,
    CANARY_GATE,
    PROTECTED_COHORT_GATE,
    ActionExecutor,
    BlastRadiusExceededError,
    BlastRadiusGuard,
    CanaryRollout,
    CollateralReport,
    ProtectedCohortError,
    RemediationLadder,
    guarded_apply,
    load_action_config,
    load_ladder_config,
)
from action.actuators import MeshActuator, SimulatedActuator
from common.config import load_config
from contracts import (
    ActionKind,
    ActionPlan,
    ActionStatus,
    Decision,
    DecisionAction,
    IncidentSeverity,
    VerdictClass,
)
from tests.test_action_mesh import FakeEdge

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _decision(
    target: str = "frontend",
    *,
    verdict: VerdictClass = VerdictClass.ATTACK,
    confidence: float = 0.88,
) -> Decision:
    return Decision(
        decision_id="decision-1",
        ts=TICK,
        incident_id="incident-1",
        action=DecisionAction.ACT,
        rule_id="a-rule",
        reason="evidence the gate acted on",
        evidence_ts=TICK,
        severity=IncidentSeverity.HIGH,
        confirmed=True,
        verification_id="verification-1",
        requires_human_approval=False,
        verdict_class=verdict,
        verdict_id="verdict-1",
        confidence=confidence,
        target_service=target,
    )


def _guard() -> BlastRadiusGuard:
    return BlastRadiusGuard(load_config(CONFIG_DIR).cohorts)


def _ladder() -> RemediationLadder:
    action = load_action_config(CONFIG_DIR / "action.yml")
    return RemediationLadder(load_ladder_config(CONFIG_DIR / "ladders.yml"), flags=action.flags)


def _mesh() -> MeshActuator:
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    assert configuration.mesh is not None
    return MeshActuator(configuration=configuration.mesh, command=FakeEdge())


def _plan(cohort: str, *, percent: int = 100) -> ActionPlan:
    return _mesh().plan(
        _decision(),
        action_kind=ActionKind.RATE_LIMIT,
        parameters={"cohort": cohort, "enforced_percent": percent},
        ts=TICK,
    )


@dataclass
class ScriptedProbe:
    """A collateral probe with a script, so a canary's stopping point is chosen."""

    clean_until: int = 100
    seen: list[int] = field(default_factory=list)

    def __call__(self, plan: ActionPlan, *, ts: datetime) -> CollateralReport:
        share = plan.parameters["enforced_percent"]
        assert isinstance(share, int)
        self.seen.append(share)
        if share <= self.clean_until:
            return CollateralReport(clean=True, detail=f"nothing else moved at {share}%")
        return CollateralReport(
            clean=False,
            detail=f"checkout conversion fell while the limit was at {share}%",
            harmed=("checkout-users",),
        )


# --- who an action may land on ----------------------------------------------


def test_the_protected_conversion_path_may_not_be_restrained() -> None:
    """`checkout-users` ships protected at 0.0, and this is where that bites.

    The mesh actuator will happily restrain it - correctly, because carrying an
    effect out and deciding who deserves protection are different jobs.
    """
    choice = _ladder().select(_decision()).primary
    with pytest.raises(ProtectedCohortError, match=r"a protected cohort allowed at most 0\.000"):
        _guard().check(_plan("checkout-users"), choice)


def test_a_capped_cohort_may_be_restrained_up_to_its_allowance() -> None:
    guard = _guard()
    choice = _ladder().select(_decision()).primary

    assert PROTECTED_COHORT_GATE in guard.check(_plan("general-traffic", percent=20), choice)
    with pytest.raises(ProtectedCohortError, match=r"a capped cohort allowed at most 0\.200"):
        guard.check(_plan("general-traffic", percent=21), choice)


def test_a_cohort_with_no_protection_statement_is_refused_not_waved_through() -> None:
    guard = _guard()
    choice = _ladder().select(_decision()).primary
    forged = _plan("general-traffic").model_copy(
        update={"parameters": {"cohort": "vips", "enforced_percent": 100}}
    )
    with pytest.raises(ProtectedCohortError, match="not one this platform has a protection"):
        guard.check(forged, choice)


def test_an_action_naming_no_cohort_passes_no_cohort_gate() -> None:
    """Silence about a gate is how an outcome shows it was never held to one."""
    ladder = _ladder()
    choice = ladder.select(
        _decision("cart", verdict=VerdictClass.OPERATIONAL_FAULT, confidence=0.78)
    ).primary
    plan = SimulatedActuator(blast_fraction=0.1).plan(
        _decision("cart"), action_kind=ActionKind.SCALE, parameters={"replicas": 2}, ts=TICK
    )

    gates = _guard().check(plan, choice)
    assert gates == (BLAST_CAP_GATE,)


# --- how far an action may reach --------------------------------------------


def test_a_plan_that_outgrew_its_rung_is_refused() -> None:
    ladder = _ladder()
    choice = ladder.select(
        _decision("cart", verdict=VerdictClass.OPERATIONAL_FAULT, confidence=0.78)
    ).primary
    assert choice.maximum_blast_fraction == 0.5

    plan = SimulatedActuator(blast_fraction=0.9).plan(
        _decision("cart"), action_kind=ActionKind.SCALE, parameters={"replicas": 2}, ts=TICK
    )
    with pytest.raises(BlastRadiusExceededError, match=r"authorises at most 0\.500"):
        _guard().check(plan, choice)


def test_the_gates_an_action_passed_are_recorded_on_its_outcome() -> None:
    """ "The guard would have caught it" is not a claim anybody can check later."""
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    adapter = SimulatedActuator(blast_fraction=0.1)
    executor = ActionExecutor(actuators=[adapter], configuration=configuration, dry_run=False)
    choice = (
        _ladder()
        .select(_decision("cart", verdict=VerdictClass.OPERATIONAL_FAULT, confidence=0.78))
        .primary
    )
    plan = adapter.plan(
        _decision("cart"), action_kind=ActionKind.SCALE, parameters={"replicas": 2}, ts=TICK
    )

    outcome = guarded_apply(
        executor=executor, guard=_guard(), plan=plan, choice=choice, ts=TICK, owner="test"
    )
    assert outcome.status is ActionStatus.APPLIED
    assert outcome.gates_passed == (BLAST_CAP_GATE,)


# --- widening rather than firing --------------------------------------------


def _canary(probe: ScriptedProbe, *, dry_run: bool = False) -> tuple[CanaryRollout, MeshActuator]:
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    adapter = _mesh()
    executor = ActionExecutor(actuators=[adapter], configuration=configuration, dry_run=dry_run)
    return CanaryRollout(executor=executor, guard=_guard(), probe=probe), adapter


def test_a_canary_widens_only_on_clean_collateral() -> None:
    probe = ScriptedProbe()
    canary, adapter = _canary(probe)
    choice = _ladder().select(_decision()).primary
    assert choice.canary_shares == (5, 10, 20)

    result = canary.apply(
        actuator=adapter, decision=_decision(), choice=choice, ts=TICK, owner="test"
    )
    assert result.completed
    assert result.widened == 20
    assert probe.seen == [5, 10, 20]
    assert [step.share for step in result.steps] == [5, 10, 20]
    assert all(CANARY_GATE in step.outcome.gates_passed for step in result.steps)


def test_a_canary_stops_at_the_share_that_harmed_something_else() -> None:
    probe = ScriptedProbe(clean_until=5)
    canary, adapter = _canary(probe)
    choice = _ladder().select(_decision()).primary

    result = canary.apply(
        actuator=adapter, decision=_decision(), choice=choice, ts=TICK, owner="test"
    )
    assert not result.completed
    assert result.widened == 10
    assert probe.seen == [5, 10], "the canary widened past the share that broke something"
    assert result.stopped_because is not None
    assert "checkout conversion fell" in result.stopped_because
    assert CANARY_GATE not in result.final.gates_passed


def test_every_step_of_a_canary_is_its_own_effect() -> None:
    """`enforced_percent: 10` and `: 100` are different states of the world.

    That is what makes each step independently deduplicated, verifiable and
    revertible, and it is why the dial had to be an absolute parameter.
    """
    probe = ScriptedProbe()
    canary, adapter = _canary(probe)
    choice = _ladder().select(_decision()).primary

    result = canary.apply(
        actuator=adapter, decision=_decision(), choice=choice, ts=TICK, owner="test"
    )
    keys = [step.plan.idempotency_key for step in result.steps]
    assert len(set(keys)) == len(keys)


def test_a_canary_holds_every_step_to_the_guard_not_only_the_first() -> None:
    """A widening that outgrows its cohort's allowance stops being permitted."""
    probe = ScriptedProbe()
    canary, adapter = _canary(probe)
    base = _ladder().select(_decision()).primary
    choice = type(base)(
        **{
            **{field: getattr(base, field) for field in base.__slots__},
            "parameters": {"cohort": "checkout-users", "enforced_percent": 100},
        }
    )

    with pytest.raises(ProtectedCohortError):
        canary.apply(actuator=adapter, decision=_decision(), choice=choice, ts=TICK, owner="test")
    assert probe.seen == [], "a refused step was still probed"


def test_a_rung_with_no_dial_cannot_be_canaried() -> None:
    probe = ScriptedProbe()
    canary, adapter = _canary(probe)
    choice = (
        _ladder()
        .select(_decision("cart", verdict=VerdictClass.OPERATIONAL_FAULT, confidence=0.78))
        .primary
    )
    assert choice.canary_parameter is None

    with pytest.raises(ValueError, match="has no dial to widen"):
        canary.apply(
            actuator=adapter, decision=_decision("cart"), choice=choice, ts=TICK, owner="test"
        )


# --- the committed configuration --------------------------------------------


def test_every_canaried_rung_ends_at_the_effect_the_operator_asked_for() -> None:
    configuration = load_ladder_config(CONFIG_DIR / "ladders.yml")
    canaried = 0
    for ladder in configuration.ladders:
        for rung in (*ladder.rungs, *ladder.companions):
            if rung.canary_parameter is None:
                continue
            canaried += 1
            assert rung.canary_shares[-1] == rung.parameters[rung.canary_parameter]
    assert canaried >= 2, "no rung is canaried, so the widening is never exercised"


def test_at_least_one_cohort_is_protected_outright() -> None:
    """A platform with no cohort it refuses to touch has no protection to speak of."""
    cohorts = load_config(CONFIG_DIR).cohorts.cohorts
    outright = [cohort for cohort in cohorts if cohort.protected]
    assert outright
    assert all(cohort.max_blast_radius_pct == 0.0 for cohort in outright)


def test_no_committed_rung_asks_for_more_than_its_cohort_permits() -> None:
    """The cross-artifact check that caught a real disagreement while 5.6 was built.

    `config/ladders.yml` states how hard to restrain a cohort and
    `config/cohorts.yml` states how much that cohort may absorb. They are written
    by different people for different reasons, and a ladder asking for more than
    the cohort permits would fail at the worst possible moment - the first time
    the platform tried to act. The rate-limit rung genuinely did ask for 100%
    against a cohort capped at 20%.
    """
    allowances = {
        cohort.cohort_id: cohort.max_blast_radius_pct / 100.0
        for cohort in load_config(CONFIG_DIR).cohorts.cohorts
    }
    configuration = load_ladder_config(CONFIG_DIR / "ladders.yml")

    checked = 0
    for ladder in configuration.ladders:
        for rung in (*ladder.rungs, *ladder.companions):
            cohort = rung.parameters.get("cohort")
            if cohort is None:
                continue
            assert cohort in allowances, f"{rung.rung_id} names an uncommitted cohort"
            allowance = allowances[str(cohort)]
            checked += 1
            assert rung.maximum_blast_fraction <= allowance, (
                f"{rung.rung_id} authorises more than {cohort} permits"
            )
            for share in rung.canary_shares:
                assert share / 100.0 <= allowance, (
                    f"{rung.rung_id} would widen to {share}%, past what {cohort} permits"
                )
    assert checked >= 2, "no cohort-scoped rung was checked"
