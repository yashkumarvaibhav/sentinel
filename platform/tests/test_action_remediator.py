"""The remediation loop, driven end to end against the committed configuration.

Every ladder, cohort, SLO and adapter here is the one the repository actually
ships. A test that invented its own ladder would prove the loop can run *a*
ladder; these prove it runs the one an operator committed, which is the only
claim worth making about a component whose whole job is composition.

The proxy's own runtime state (`FakeEdge.runtime`) is the strongest assertion
available without a cluster: rather than checking the *order* reverts were
issued in, the unwinding tests read what the edge is left holding. An effect
that was given back in the wrong order leaves a number behind, and a number is
harder to argue with than a call sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from action import (
    DEFAULT_OWNER,
    ActionExecutor,
    BlastRadiusGuard,
    RemediationBreaker,
    RemediationLadder,
    Remediator,
    RestraintRegistry,
    SloReading,
    VerifiedRollback,
    load_action_config,
    load_ladder_config,
)
from action.actuators import KubernetesActuator, MeshActuator, SimulatedActuator
from audit import AuditChain, verify_chain
from common.config import load_config
from contracts import (
    ActionKind,
    ActionStatus,
    AuditEntry,
    AuditEventKind,
    Decision,
    DecisionAction,
    IncidentSeverity,
)
from contracts.decision import VerdictClass
from tests.test_action_kubernetes import FakeCluster
from tests.test_action_mesh import FakeEdge

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# Confidences that reach each rung of the committed attack ladder. Stated as
# names so a test reads as the rung it is about rather than as a magic number.
WATCH = 0.50
THROTTLE = 0.72
RATE_LIMIT = 0.88
ISOLATE = 0.97


def _decision(
    *,
    target: str = "frontend",
    verdict: VerdictClass = VerdictClass.ATTACK,
    confidence: float = RATE_LIMIT,
    incident: str = "incident-1",
    decision_id: str = "decision-1",
    ts: datetime = TICK,
) -> Decision:
    return Decision(
        decision_id=decision_id,
        ts=ts,
        incident_id=incident,
        action=DecisionAction.ACT,
        rule_id="contain-confirmed-hostile-traffic",
        reason="hostile behaviour confirmed against telemetry",
        evidence_ts=ts,
        severity=IncidentSeverity.HIGH,
        confirmed=True,
        verification_id="verification-1",
        requires_human_approval=False,
        verdict_class=verdict,
        verdict_id="verdict-1",
        confidence=confidence,
        target_service=target,
    )


@dataclass
class ScriptedSlo:
    """An SLO reader driven by *when* it is asked, never by how often.

    A reader that answered from a queue would make every test a hostage to the
    loop's internal read count - and the loop legitimately reads a service
    several times per pass, once per canary step and again after the effect has
    settled. These answers are a step function over time, which is also what a
    real reader is: ``schedule`` maps a service to the moments its availability
    changed, and the latest moment that has arrived wins.
    """

    schedule: dict[str, tuple[tuple[datetime, float], ...]] = field(default_factory=dict)
    unreadable: frozenset[str] = frozenset()
    healthy: float = 1.0

    def __call__(self, service: str, *, ts: datetime) -> SloReading | None:
        if service in self.unreadable:
            return None
        reached = [value for start, value in self.schedule.get(service, ()) if ts >= start]
        availability = reached[-1] if reached else self.healthy
        return SloReading(service=service, availability=availability, latency_p95_ms=100.0)


@dataclass
class Harness:
    """One fully wired loop, plus the doubles whose state the tests assert on."""

    remediator: Remediator
    edge: FakeEdge
    cluster: FakeCluster
    simulated: SimulatedActuator
    registry: RestraintRegistry
    breaker: RemediationBreaker
    slo: ScriptedSlo


def _harness(
    *, dry_run: bool = False, slo: ScriptedSlo | None = None, ledger: AuditChain | None = None
) -> Harness:
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    bundle = load_config(CONFIG_DIR)
    edge = FakeEdge()
    cluster = FakeCluster(answers={"get pods": "node-a", "get deployment": "1"})
    simulated = SimulatedActuator(blast_fraction=0.05)
    assert configuration.mesh is not None
    assert configuration.kubernetes is not None
    assert configuration.breaker is not None
    actuators = [
        MeshActuator(configuration=configuration.mesh, command=edge),
        KubernetesActuator(configuration=configuration.kubernetes, command=cluster),
        simulated,
    ]
    # One chain for both, which is the real wiring: the executor writes the
    # plan-level entries and the loop writes the incident-level ones, into the
    # same tamper-evident record.
    executor = ActionExecutor(
        actuators=actuators, configuration=configuration, dry_run=dry_run, ledger=ledger
    )
    reader = slo if slo is not None else ScriptedSlo()
    registry = RestraintRegistry()
    breaker = RemediationBreaker(configuration.breaker)
    return Harness(
        remediator=Remediator(
            ladder=RemediationLadder(
                load_ladder_config(CONFIG_DIR / "ladders.yml"), flags=configuration.flags
            ),
            actuators=actuators,
            executor=executor,
            guard=BlastRadiusGuard(bundle.cohorts),
            breaker=breaker,
            rollback=VerifiedRollback(executor=executor, slos=bundle.slos, reader=reader),
            registry=registry,
            ledger=ledger,
        ),
        edge=edge,
        cluster=cluster,
        simulated=simulated,
        registry=registry,
        breaker=breaker,
        slo=reader,
    )


# --- answering an incident once ---------------------------------------------


def test_a_decision_reaches_production_through_every_gate_at_once() -> None:
    """The claim this whole module exists to support: the pieces compose."""
    harness = _harness()

    run = harness.remediator.consider(_decision(), ts=TICK)

    assert run.acted
    assert run.selection is not None
    assert run.selection.primary.rung_id == "hold-the-cohort-to-its-ceiling"
    assert run.refusals == ()
    # The cohort gate, the blast cap and the canary all held, and the outcome
    # says which - "the guard would have caught it" is not a checkable claim.
    gates = run.applied[0].outcome.gates_passed
    assert "protected-cohort-unharmed" in gates
    assert "blast-radius-within-the-rung-ceiling" in gates
    assert "canary-widened-on-clean-collateral" in gates
    # And the proxy is actually holding the restraint the ladder asked for.
    assert harness.edge.runtime["sentinel.ratelimit.general_traffic.enforced"] == "20"


def test_an_incident_is_answered_once_not_once_per_tick() -> None:
    """The decision plane re-emits an open incident every pass; the loop must not."""
    harness = _harness()
    first = harness.remediator.consider(_decision(decision_id="decision-1"), ts=TICK)
    assert first.acted

    later = TICK + timedelta(seconds=30)
    second = harness.remediator.consider(_decision(decision_id="decision-2", ts=later), ts=later)

    assert not second.acted
    assert second.applied == ()
    assert "is already standing on incident-1" in second.refusals[0]
    assert harness.simulated.call_count("apply") == 0


def test_a_stronger_rung_supersedes_the_one_already_standing() -> None:
    """Evidence firms up, the ladder climbs, and the weaker answer is given back."""
    harness = _harness()
    harness.remediator.consider(_decision(confidence=THROTTLE), ts=TICK)
    assert harness.edge.runtime["sentinel.throttle.general_traffic.percent"] == "20"

    later = TICK + timedelta(seconds=60)
    run = harness.remediator.consider(
        _decision(decision_id="decision-2", confidence=RATE_LIMIT, ts=later), ts=later
    )

    assert run.acted
    assert run.selection is not None
    assert run.selection.primary.rung_id == "hold-the-cohort-to-its-ceiling"
    assert all(released.reverted for released in run.released)
    # The throttle was genuinely let go, not merely superseded in bookkeeping.
    assert harness.edge.runtime["sentinel.throttle.general_traffic.percent"] == "0"
    assert harness.edge.runtime["sentinel.ratelimit.general_traffic.enforced"] == "20"


def test_a_weaker_rung_never_displaces_a_standing_stronger_one() -> None:
    """A restraint is not loosened because confidence dipped for one tick.

    Releasing a rate limit on a flickering signal is how a flapping metric hands
    an attack its throughput back.
    """
    harness = _harness()
    harness.remediator.consider(_decision(confidence=RATE_LIMIT), ts=TICK)

    later = TICK + timedelta(seconds=60)
    run = harness.remediator.consider(
        _decision(decision_id="decision-2", confidence=THROTTLE, ts=later), ts=later
    )

    assert not run.acted
    assert run.released == ()
    assert harness.edge.runtime["sentinel.ratelimit.general_traffic.enforced"] == "20"


def test_a_changed_diagnosis_always_supersedes_whatever_is_standing() -> None:
    """A different ladder is a different answer, not a weaker one."""
    harness = _harness()
    harness.remediator.consider(_decision(confidence=RATE_LIMIT), ts=TICK)

    later = TICK + timedelta(seconds=60)
    run = harness.remediator.consider(
        _decision(
            decision_id="decision-2",
            verdict=VerdictClass.OPERATIONAL_FAULT,
            confidence=0.78,
            ts=later,
        ),
        ts=later,
    )

    assert run.acted
    assert run.selection is not None
    assert run.selection.primary.ladder_id == "remediate-our-own-fault"
    assert harness.edge.runtime["sentinel.ratelimit.general_traffic.enforced"] == "0"


# --- what is recorded, and against what --------------------------------------


def test_a_canary_is_many_restraints_but_one_action() -> None:
    """Each step wrote a value some revert must restore; together they are one answer.

    Counting a careful three-step widening as three actions would trip the
    runaway detector on the platform being careful, which is precisely backwards.
    """
    harness = _harness()

    run = harness.remediator.consider(_decision(), ts=TICK)

    assert run.applied[0].canary is not None
    assert [step.share for step in run.applied[0].canary.steps] == [5, 10, 20]
    # Three steps and a companion standing; two actions counted (the canary is
    # one, the SCALE companion is the other).
    assert len(harness.registry.for_incident("incident-1")) == 4
    assert harness.breaker.state(TICK).recent_actions == 2


def test_an_observe_rung_is_not_counted_against_the_breaker() -> None:
    """A breaker that tripped on watching would page for the platform's attentiveness."""
    harness = _harness()

    run = harness.remediator.consider(_decision(confidence=WATCH), ts=TICK)

    assert run.acted
    assert run.selection is not None
    assert run.selection.primary.action_kind is ActionKind.OBSERVE
    assert harness.breaker.state(TICK).recent_actions == 0
    # It is still a recorded, expiring answer - just not an action.
    assert len(harness.registry.for_incident("incident-1")) == 1


def test_a_dry_run_records_nothing_as_standing() -> None:
    """Nothing was applied, so nothing may be remembered as needing to be undone.

    Recording a restraint here would have the loop try to revert an effect that
    was never applied, and the executor would rightly refuse it.
    """
    harness = _harness(dry_run=True)

    run = harness.remediator.consider(_decision(), ts=TICK)

    assert not run.acted
    assert run.applied != ()
    assert all(effect.outcome.status is ActionStatus.SIMULATED for effect in run.applied)
    assert harness.registry.standing() == ()
    assert harness.edge.writes() == []


# --- giving effects back ------------------------------------------------------


def test_an_expired_restraint_is_given_back() -> None:
    """An action with no expiry is a configuration change nobody remembers making."""
    harness = _harness()
    harness.remediator.consider(_decision(), ts=TICK)
    ttl = timedelta(seconds=900)

    assert harness.remediator.expire(TICK + ttl) == ()
    released = harness.remediator.expire(TICK + ttl + timedelta(seconds=1))

    assert released != ()
    assert all(entry.reverted for entry in released)
    assert harness.registry.standing() == ()


def test_effects_are_given_back_newest_first_so_the_edge_is_left_unrestrained() -> None:
    """The assertion is the proxy's own state, not the order of the calls.

    Each canary step's revert token restores the value that step found, so
    unwinding 20 -> 10 -> 5 returns the key to 0 while unwinding 5 -> 10 -> 20
    would leave the cohort restrained at the widest share ever reached.
    """
    harness = _harness()
    harness.remediator.consider(_decision(), ts=TICK)
    assert harness.edge.runtime["sentinel.ratelimit.general_traffic.enforced"] == "20"

    harness.remediator.expire(TICK + timedelta(seconds=901))

    assert harness.edge.runtime["sentinel.ratelimit.general_traffic.enforced"] == "0"
    assert harness.edge.runtime["sentinel.ratelimit.general_traffic.enabled"] == "0"


def test_a_revert_that_is_refused_is_recorded_and_the_rest_still_go_back() -> None:
    """One effect that cannot be undone must not strand the others."""
    harness = _harness()
    harness.remediator.consider(_decision(), ts=TICK)
    standing = harness.registry.for_incident("incident-1")
    # A restraint the journal has never heard of. The executor refuses to revert
    # an effect it holds no record of applying - which is correct, and is exactly
    # the failure this test needs to inject.
    phantom = harness.simulated.plan(_decision(), action_kind=ActionKind.OBSERVE, ts=TICK)
    harness.registry.record(phantom, standing[0].choice, applied_at=TICK - timedelta(seconds=1))

    released = harness.remediator.expire(TICK + timedelta(seconds=901))

    refused = [entry for entry in released if not entry.reverted]
    assert len(refused) == 1
    assert refused[0].plan.plan_id == phantom.plan_id
    assert "still in force" in refused[0].detail
    # Everything else went back regardless, which is the point.
    assert len(released) == len(standing) + 1
    assert harness.edge.runtime["sentinel.ratelimit.general_traffic.enforced"] == "0"


# --- harm, and telling the truth about it ------------------------------------


SETTLED = TICK + timedelta(seconds=120)
RECOVERED = TICK + timedelta(seconds=240)


def _harm_that_appears_late() -> ScriptedSlo:
    """`checkout` holds its SLO while the canary widens, and falls afterwards.

    Harm that takes time to appear is exactly the harm a canary cannot see,
    which is why the loop looks a second time.
    """
    return ScriptedSlo(schedule={"checkout": ((SETTLED, 0.90), (RECOVERED, 0.98))})


def test_an_action_that_harmed_a_protected_service_is_undone() -> None:
    harness = _harness(slo=_harm_that_appears_late())

    run = harness.remediator.consider(
        _decision(), ts=TICK, settled_at=SETTLED, recovered_at=RECOVERED
    )

    # It got all the way through the canary, so the harm was genuinely invisible
    # at apply time rather than something the canary should have caught.
    assert run.applied[0].canary is not None
    assert run.applied[0].canary.completed
    assert run.rollback is not None
    assert run.rollback.reverted
    assert "checkout" in run.rollback.harmed
    assert harness.registry.for_incident("incident-1") == ()
    assert harness.edge.runtime["sentinel.ratelimit.general_traffic.enforced"] == "0"


def test_the_harm_an_undone_action_did_is_reported_negated() -> None:
    """The worst bug this file could have, stated as a test.

    An action that had to be undone did not help. The recovery the rollback
    measures is the size of the harm that action caused, so feeding it to the
    breaker unsigned would mean: the more damage an action did, the more
    effective the breaker would believe it to be.
    """
    harness = _harness(slo=_harm_that_appears_late())

    run = harness.remediator.consider(
        _decision(), ts=TICK, settled_at=SETTLED, recovered_at=RECOVERED
    )

    assert run.rollback is not None
    assert run.rollback.availability_restored == pytest.approx(0.08)
    assert run.improvement == pytest.approx(-0.08)


def test_an_action_that_harmed_nothing_is_judged_on_the_target_it_aimed_at() -> None:
    """The clean path: the improvement is the target's own recovery, signed the right way."""
    harness = _harness(slo=ScriptedSlo(schedule={"cart": ((TICK, 0.90), (RECOVERED, 0.97))}))
    # `cart` is not in slo.yml, so nothing protected is disturbed by acting on it.
    fault = _decision(target="cart", verdict=VerdictClass.OPERATIONAL_FAULT, confidence=0.78)

    run = harness.remediator.consider(fault, ts=TICK, settled_at=SETTLED, recovered_at=RECOVERED)

    assert run.acted
    assert run.rollback is None
    assert run.collateral is not None and run.collateral.clean
    assert run.improvement == pytest.approx(0.07)


def test_an_unmeasured_action_is_not_counted_against_the_futility_streak() -> None:
    """An action nobody measured has not failed to help."""
    harness = _harness(slo=ScriptedSlo(unreadable=frozenset({"frontend-proxy"})))

    run = harness.remediator.consider(_decision(), ts=TICK, settled_at=SETTLED)

    # A service that cannot be read is unknown, never healthy - so the action is
    # undone, and the recovery is unmeasurable, so none is claimed.
    assert run.rollback is not None
    assert run.rollback.availability_restored is None
    assert run.improvement is None


# --- refusing, without falling over ------------------------------------------


def test_an_open_breaker_stops_the_loop_and_asks_for_a_person() -> None:
    harness = _harness()
    for index in range(5):
        harness.remediator.consider(
            _decision(incident=f"incident-{index}", decision_id=f"decision-{index}"),
            ts=TICK + timedelta(seconds=index),
        )

    run = harness.remediator.consider(
        _decision(incident="incident-late", decision_id="decision-late"),
        ts=TICK + timedelta(seconds=10),
    )

    assert not run.acted
    assert run.requires_page
    assert run.breaker.open
    assert run.selection is None, "a ladder is not even consulted once the breaker is open"


def test_a_rung_that_needs_a_signature_is_refused_rather_than_applied() -> None:
    """`take-the-node-out-of-rotation` is never autonomous, however sure we are."""
    harness = _harness()

    run = harness.remediator.consider(_decision(confidence=ISOLATE), ts=TICK)

    assert not run.acted
    assert run.refusals != ()
    assert "approver" in run.refusals[0]
    assert harness.cluster.mutating_calls() == []


def test_a_signed_destructive_rung_needs_two_distinct_people() -> None:
    harness = _harness()

    one = harness.remediator.consider(_decision(confidence=ISOLATE), ts=TICK, approvals=("sre-a",))
    assert not one.acted

    two = harness.remediator.consider(
        _decision(confidence=ISOLATE, decision_id="decision-2"),
        ts=TICK + timedelta(seconds=1),
        approvals=("sre-a", "sre-b"),
    )
    assert two.acted


def test_a_decision_that_decided_not_to_act_earns_no_action() -> None:
    harness = _harness()
    watching = _decision().model_copy(
        update={"action": DecisionAction.SUPPRESS, "target_service": None}
    )

    run = harness.remediator.consider(watching, ts=TICK)

    assert not run.acted
    assert "touches nothing" in run.refusals[0]


def test_a_companion_that_is_refused_does_not_retract_the_primary() -> None:
    """ "Restrain and add headroom" degrades honestly to "restrain"."""
    harness = _harness()
    # A restraint at the edge is aimed at a cohort, so the mesh rung lands for
    # any target; the committed Kubernetes workload map does not name `shipping`,
    # so the SCALE companion riding with it has nothing to aim at.
    run = harness.remediator.consider(_decision(target="shipping"), ts=TICK)

    assert run.acted
    assert len(run.applied) == 1
    assert run.applied[0].choice.rung_id == "hold-the-cohort-to-its-ceiling"
    assert run.refusals != ()
    assert "add-headroom-for-the-surge" in run.refusals[0]
    assert harness.cluster.mutating_calls() == []


# --- what the ledger is told --------------------------------------------------


def _loop_entries(ledger: AuditChain) -> list[AuditEntry]:
    """Only the loop's own entries.

    Discriminated by the absence of a ``plan_id``, not by the actor: the
    executor records the loop as the actor, because the loop is who acted. What
    separates the two is the *level* - the executor always writes about one
    plan, and the loop never does.
    """
    return [
        entry
        for entry in ledger.entries()
        if entry.plan_id is None and entry.actor == DEFAULT_OWNER
    ]


def test_the_loop_records_what_it_decided_about_the_incident() -> None:
    """An incident-level record, which is a different claim from the plan-level one.

    The executor writes "this action was applied"; only the loop can write "this
    is what the platform decided to do about this incident, and at which rung".
    """
    ledger = AuditChain()
    harness = _harness(ledger=ledger)

    harness.remediator.consider(_decision(), ts=TICK)

    decisions = [entry for entry in _loop_entries(ledger) if entry.kind is AuditEventKind.DECISION]
    assert len(decisions) == 1
    recorded = decisions[0]
    assert recorded.incident_id == "incident-1"
    assert recorded.decision_id == "decision-1"
    assert recorded.body["rung_id"] == "hold-the-cohort-to-its-ceiling"
    assert "canary-widened-on-clean-collateral" in recorded.body["gates_passed"]
    # A statement about an incident carries no plan id: attaching one of several
    # effects would quietly make it read as a statement about that effect.
    assert recorded.plan_id is None
    assert verify_chain(ledger.entries()).intact


def test_the_loop_records_taking_no_action_and_why() -> None:
    """Both levels are written, because they are two different statements.

    The executor's entry says a particular plan was refused for want of a
    signature. Only the loop's says the incident went unanswered - and that is
    the one somebody reconstructing an outage is actually looking for.
    """
    ledger = AuditChain()
    harness = _harness(ledger=ledger)

    harness.remediator.consider(_decision(confidence=ISOLATE), ts=TICK)

    refusals = [entry for entry in ledger.entries() if entry.kind is AuditEventKind.ACTION_REFUSED]
    plan_level = [entry for entry in refusals if entry.plan_id is not None]
    loop_level = [entry for entry in refusals if entry.plan_id is None]
    assert len(plan_level) == 1, "the executor still records the plan it refused"
    assert len(loop_level) == 1
    assert "took no action on incident-1" in loop_level[0].summary
    assert "approver" in loop_level[0].body["reasons"][0]
    assert verify_chain(ledger.entries()).intact


def test_the_breaker_is_recorded_opening_rather_than_being_open() -> None:
    """A loop that logged the state every pass would bury the moment it tripped."""
    ledger = AuditChain()
    harness = _harness(ledger=ledger)
    for index in range(5):
        harness.remediator.consider(
            _decision(incident=f"incident-{index}", decision_id=f"decision-{index}"),
            ts=TICK + timedelta(seconds=index),
        )

    for index in range(3):
        harness.remediator.consider(
            _decision(incident="incident-late", decision_id=f"decision-late-{index}"),
            ts=TICK + timedelta(seconds=20 + index),
        )

    opened = [
        entry for entry in _loop_entries(ledger) if entry.kind is AuditEventKind.BREAKER_OPENED
    ]
    assert len(opened) == 1
    assert opened[0].body["requires_page"] is True


def test_why_an_action_was_undone_is_recorded_separately_from_the_undoing() -> None:
    """That an effect was reverted does not say why; the ledger needs both."""
    ledger = AuditChain()
    harness = _harness(slo=_harm_that_appears_late(), ledger=ledger)

    harness.remediator.consider(_decision(), ts=TICK, settled_at=SETTLED, recovered_at=RECOVERED)

    rollbacks = [entry for entry in _loop_entries(ledger) if entry.kind is AuditEventKind.ROLLBACK]
    assert len(rollbacks) == 1
    assert rollbacks[0].body["harmed"] == ["checkout"]
    assert rollbacks[0].body["availability_restored"] == pytest.approx(0.08)
    # The executor's own ACTION_REVERTED entries are still there beside it.
    assert AuditEventKind.ACTION_REVERTED in [entry.kind for entry in ledger.entries()]
    assert verify_chain(ledger.entries()).intact


def test_a_dry_run_leaves_a_trail_labelled_simulated() -> None:
    """A run that produces a full audit trail of what would have happened is a
    real capability - provided every artifact it leaves is labelled as such."""
    ledger = AuditChain()
    harness = _harness(dry_run=True, ledger=ledger)

    harness.remediator.consider(_decision(), ts=TICK)

    written = _loop_entries(ledger)
    assert written != []
    assert all(entry.honesty == "SIMULATED" for entry in written)


def test_no_refusal_escapes_the_loop_as_an_exception() -> None:
    """An always-on loop that dies on tick N never reaches tick N+1."""
    harness = _harness()
    # A fault on a service no committed workload map names: the only rung its
    # confidence reaches cannot be aimed at anything.
    hopeless = _decision(target="shipping", verdict=VerdictClass.OPERATIONAL_FAULT, confidence=0.78)

    run = harness.remediator.consider(hopeless, ts=TICK)

    assert not run.acted
    assert run.refusals != ()
    assert "shipping" in run.refusals[0]
