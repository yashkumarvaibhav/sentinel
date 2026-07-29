"""The executor is where every safety property of the action plane is enforced."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from action import (
    ActionConfig,
    ActionExecutor,
    ActionJournal,
    ActionJournalFullError,
    ActionRejectedError,
    Actuator,
    ActuatorContractError,
    LeaseRegistry,
    SimulatedActuator,
    TargetBusyError,
    build_plan,
    resolve_dry_run,
)
from action.config import FORCE_DRY_RUN_ENV
from audit import AuditChain, verify_chain
from contracts import (
    ESCALATING_ACTIONS,
    ActionKind,
    ActionOutcome,
    ActionPlan,
    ActionStatus,
    ActuatorKind,
    AuditEventKind,
    Decision,
    DecisionAction,
    IncidentSeverity,
    VerdictClass,
)

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _decision(
    action: DecisionAction = DecisionAction.ACT,
    *,
    approval: bool = False,
) -> Decision:
    # Approval is now the caller's alone. An escalating rung states why a person
    # is being brought in, which is a separate field and NOT a consent gate -
    # conflating the two is what stopped `AUTO_CONTAIN_THEN_ESCALATE` ever
    # containing anything.
    needs_approval = approval
    fields: dict[str, Any] = {
        "decision_id": "decision-1",
        "ts": TICK,
        "incident_id": "incident-1",
        "action": action,
        "rule_id": "remediate-a-verified-fault",
        "reason": "a confirmed fault with a named origin",
        "evidence_ts": TICK,
        "severity": IncidentSeverity.HIGH,
        "confirmed": True,
        "verification_id": "verification-1",
        "requires_human_approval": needs_approval,
        "approval_reasons": ("measured critical severity",) if needs_approval else (),
        # Stated exactly when the action brings a person in. Being told and
        # being asked to consent are different facts and different fields.
        "escalation_reasons": (
            ("the ladder routes this to a person",) if action in ESCALATING_ACTIONS else ()
        ),
        "verdict_class": VerdictClass.OPERATIONAL_FAULT,
        "verdict_id": "verdict-1",
        "confidence": 0.9,
        "target_service": "payment",
    }
    return Decision(**fields)


def _configuration(*, dry_run: bool, capacity: int = 4096) -> ActionConfig:
    return ActionConfig.model_validate(
        {
            "version": 1,
            "execution": {
                "dry_run": dry_run,
                "lease_ttl_seconds": 120.0,
                "journal_capacity": capacity,
            },
            "actuators": [{"actuator": "SIMULATED", "enabled": True}],
        }
    )


def _executor(
    *,
    dry_run: bool = False,
    actuator: SimulatedActuator | None = None,
    capacity: int = 4096,
    ledger: AuditChain | None = None,
) -> tuple[ActionExecutor, SimulatedActuator]:
    adapter = actuator or SimulatedActuator()
    configuration = _configuration(dry_run=dry_run, capacity=capacity)
    return (
        ActionExecutor(
            actuators=[adapter],
            configuration=configuration,
            dry_run=resolve_dry_run(configuration, environ={}),
            ledger=ledger,
        ),
        adapter,
    )


def _plan(
    adapter: SimulatedActuator,
    *,
    action_kind: ActionKind = ActionKind.SCALE,
    decision: Decision | None = None,
    parameters: dict[str, Any] | None = None,
) -> ActionPlan:
    return adapter.plan(
        decision or _decision(),
        action_kind=action_kind,
        parameters=parameters if parameters is not None else {"replicas": 6},
        ts=TICK,
    )


# --- the seam between deciding and doing ------------------------------------


@pytest.mark.parametrize(
    "action",
    [DecisionAction.SUPPRESS, DecisionAction.ALERT, DecisionAction.ESCALATE_TO_HUMAN],
)
def test_a_decision_that_did_not_decide_to_act_produces_no_plan(action: DecisionAction) -> None:
    adapter = SimulatedActuator()
    with pytest.raises(ActionRejectedError, match="may only be built from a decision"):
        _plan(adapter, decision=_decision(action))


def test_a_plan_carries_the_decisions_target_and_approval_unchanged() -> None:
    adapter = SimulatedActuator()
    plan = _plan(adapter, decision=_decision(approval=True))
    assert plan.target_service == "payment"
    assert plan.requires_human_approval
    assert plan.decision_id == "decision-1"
    assert plan.incident_id == "incident-1"


def test_approval_is_widened_by_the_rung_never_narrowed_by_the_adapter() -> None:
    adapter = SimulatedActuator()
    unapproved = _decision()
    assert not unapproved.requires_human_approval
    isolate = _plan(adapter, action_kind=ActionKind.ISOLATE, parameters={})
    restart = _plan(adapter, action_kind=ActionKind.RESTART, parameters={})
    scale = _plan(adapter)
    assert isolate.requires_human_approval, "a destructive rung always needs a human"
    assert restart.requires_human_approval, "an irreversible rung always needs a human"
    assert not scale.requires_human_approval, "the gate said nobody, and scale is safe"


def test_every_rung_can_be_planned_and_states_what_to_expect() -> None:
    """A rung nobody wrote an expected effect for could never be verified."""
    adapter = SimulatedActuator()
    for rung in ActionKind:
        plan = _plan(adapter, action_kind=rung, parameters={})
        assert plan.expected_effect, rung.value
        assert plan.action_kind is rung


def test_the_same_effect_from_two_decisions_shares_one_key() -> None:
    adapter = SimulatedActuator()
    first = _plan(adapter)
    second_decision = _decision().model_copy(update={"decision_id": "decision-2"})
    second = adapter.plan(
        second_decision,
        action_kind=ActionKind.SCALE,
        parameters={"replicas": 6},
        ts=TICK + timedelta(seconds=30),
    )
    assert second.idempotency_key == first.idempotency_key
    assert second.plan_id != first.plan_id


# --- dry run ----------------------------------------------------------------


def test_dry_run_routes_an_apply_to_the_simulation_and_never_to_apply() -> None:
    executor, adapter = _executor(dry_run=True)
    plan = _plan(adapter)
    outcome = executor.apply(plan, ts=TICK, owner="executor-a")
    assert outcome.status is ActionStatus.SIMULATED
    assert outcome.dry_run
    assert adapter.call_count("apply") == 0
    assert adapter.call_count("simulate") == 1
    assert adapter.in_place == set(), "a dry run must not change the simulated world either"


def test_a_dry_run_journal_stays_empty() -> None:
    executor, adapter = _executor(dry_run=True)
    plan = _plan(adapter)
    executor.apply(plan, ts=TICK, owner="executor-a")
    executor.verify(plan, ts=TICK + timedelta(seconds=1))
    executor.revert(plan, ts=TICK + timedelta(seconds=2), owner="executor-a")
    assert executor.journal.entries() == ()
    assert not executor.journal.in_force(plan.idempotency_key)


class _LyingActuator(Actuator):
    """An adapter that reports a real change from its own simulation."""

    kind: ClassVar[ActuatorKind] = ActuatorKind.SIMULATED
    honesty: ClassVar[Literal["REAL", "SIMULATED"]] = "SIMULATED"

    def plan(
        self, decision: Decision, *, action_kind: ActionKind, parameters: Any = None, ts: datetime
    ) -> ActionPlan:
        return build_plan(
            decision,
            actuator=self.kind,
            action_kind=action_kind,
            target_ref="simulated/payment",
            parameters=parameters,
            reason=decision.reason,
            expected_effect="six ready replicas",
            reversible=True,
            estimated_blast_fraction=0.05,
            honesty=self.honesty,
            ts=ts,
        )

    def simulate(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        return self._claim(plan, ts, ActionStatus.APPLIED)

    def apply(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        return self._claim(plan, ts, ActionStatus.APPLIED)

    def verify(self, plan: ActionPlan, *, ts: datetime) -> ActionOutcome:
        return self._claim(plan, ts, ActionStatus.VERIFIED)

    def revert(
        self, plan: ActionPlan, *, ts: datetime, revert_token: str | None = None
    ) -> ActionOutcome:
        return self._claim(plan, ts, ActionStatus.REVERTED)

    def _claim(self, plan: ActionPlan, ts: datetime, status: ActionStatus) -> ActionOutcome:
        return ActionOutcome(
            outcome_id=f"lie-{status.value}",
            ts=ts,
            plan_id=plan.plan_id,
            idempotency_key=plan.idempotency_key,
            status=status,
            dry_run=False,
            detail="claims to have changed production",
            honesty=self.honesty,
        )


def test_an_adapter_cannot_bypass_dry_run() -> None:
    """The executor rejects a simulation that claims to have touched anything."""
    liar = _LyingActuator()
    configuration = _configuration(dry_run=True)
    executor = ActionExecutor(actuators=[liar], configuration=configuration, dry_run=True)
    plan = liar.plan(_decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 6}, ts=TICK)
    with pytest.raises(ActuatorContractError, match="where nothing may be touched"):
        executor.apply(plan, ts=TICK, owner="executor-a")
    assert executor.journal.entries() == ()


def test_the_force_dry_run_latch_only_tightens() -> None:
    live = _configuration(dry_run=False)
    pretending = _configuration(dry_run=True)
    assert not resolve_dry_run(live, environ={})
    assert resolve_dry_run(live, environ={FORCE_DRY_RUN_ENV: "1"})
    assert resolve_dry_run(live, environ={FORCE_DRY_RUN_ENV: "TRUE"})
    # Nothing in the environment can make a pretending system act.
    assert resolve_dry_run(pretending, environ={FORCE_DRY_RUN_ENV: "0"})
    assert resolve_dry_run(pretending, environ={FORCE_DRY_RUN_ENV: "false"})


# --- idempotency ------------------------------------------------------------


def test_an_effect_already_in_place_is_not_applied_again() -> None:
    executor, adapter = _executor()
    plan = _plan(adapter)
    first = executor.apply(plan, ts=TICK, owner="executor-a")
    second = executor.apply(plan, ts=TICK + timedelta(seconds=5), owner="executor-a")
    assert first.status is ActionStatus.APPLIED and not first.deduplicated
    assert second.status is ActionStatus.APPLIED and second.deduplicated
    assert adapter.call_count("apply") == 1


def test_a_duplicate_decision_cannot_double_apply() -> None:
    """The second decision builds its own plan; the effect it names is the same one."""
    executor, adapter = _executor()
    first = _plan(adapter)
    duplicate_decision = _decision().model_copy(update={"decision_id": "decision-2"})
    second = adapter.plan(
        duplicate_decision,
        action_kind=ActionKind.SCALE,
        parameters={"replicas": 6},
        ts=TICK + timedelta(seconds=10),
    )
    executor.apply(first, ts=TICK, owner="executor-a")
    outcome = executor.apply(second, ts=TICK + timedelta(seconds=10), owner="executor-a")
    assert outcome.deduplicated
    assert outcome.plan_id == second.plan_id, "the answer is about the plan that asked"
    assert adapter.call_count("apply") == 1


def test_a_failed_apply_is_retryable_and_never_counts_as_in_force() -> None:
    adapter = SimulatedActuator(fail_on_apply=True)
    executor, _ = _executor(actuator=adapter)
    plan = _plan(adapter)
    failed = executor.apply(plan, ts=TICK, owner="executor-a")
    assert failed.status is ActionStatus.FAILED
    assert not executor.journal.in_force(plan.idempotency_key)
    adapter.fail_on_apply = False
    retried = executor.apply(plan, ts=TICK + timedelta(seconds=5), owner="executor-a")
    assert retried.status is ActionStatus.APPLIED and not retried.deduplicated
    assert adapter.call_count("apply") == 2


def test_a_reverted_effect_may_be_applied_again() -> None:
    executor, adapter = _executor()
    plan = _plan(adapter)
    executor.apply(plan, ts=TICK, owner="executor-a")
    executor.revert(plan, ts=TICK + timedelta(seconds=5), owner="executor-a")
    again = executor.apply(plan, ts=TICK + timedelta(seconds=10), owner="executor-a")
    assert again.status is ActionStatus.APPLIED and not again.deduplicated
    assert adapter.call_count("apply") == 2


def test_reverting_what_was_never_applied_is_refused() -> None:
    executor, adapter = _executor()
    plan = _plan(adapter)
    with pytest.raises(ActionRejectedError, match="no effect"):
        executor.revert(plan, ts=TICK, owner="executor-a")
    assert adapter.call_count("revert") == 0


def test_the_adapter_gets_back_the_revert_token_it_minted() -> None:
    """The state to undo to cannot live in the plan, so it travels with the outcome."""
    executor, adapter = _executor()
    plan = _plan(adapter)
    applied = executor.apply(plan, ts=TICK, owner="executor-a")
    assert applied.revert_token is not None
    verified = executor.verify(plan, ts=TICK + timedelta(seconds=3))
    assert verified.revert_token == applied.revert_token
    executor.revert(plan, ts=TICK + timedelta(seconds=5), owner="executor-a")
    assert adapter.reverted_with == [applied.revert_token]


def test_a_second_revert_is_deduplicated() -> None:
    executor, adapter = _executor()
    plan = _plan(adapter)
    executor.apply(plan, ts=TICK, owner="executor-a")
    executor.revert(plan, ts=TICK + timedelta(seconds=1), owner="executor-a")
    repeat = executor.revert(plan, ts=TICK + timedelta(seconds=2), owner="executor-a")
    assert repeat.deduplicated and repeat.status is ActionStatus.REVERTED
    assert adapter.call_count("revert") == 1


def test_a_failed_verification_retracts_the_belief_that_it_is_in_place() -> None:
    executor, adapter = _executor()
    plan = _plan(adapter)
    executor.apply(plan, ts=TICK, owner="executor-a")
    assert executor.journal.in_force(plan.idempotency_key)
    adapter.in_place.discard(plan.idempotency_key)  # the world drifted back
    outcome = executor.verify(plan, ts=TICK + timedelta(seconds=5))
    assert outcome.status is ActionStatus.FAILED
    assert not executor.journal.in_force(plan.idempotency_key)


@settings(max_examples=75, deadline=None)
@given(st.lists(st.sampled_from(("apply", "verify", "revert")), min_size=1, max_size=14))
def test_no_interleaving_of_retries_ever_applies_an_effect_twice(operations: list[str]) -> None:
    """However apply/verify/revert are interleaved, the world changes exactly as modelled."""
    executor, adapter = _executor()
    plan = _plan(adapter)
    # The model of what the journal should believe, kept independently of the
    # journal itself: `None` means nothing has ever been recorded for this key.
    last: ActionStatus | None = None
    expected_applies = 0
    expected_reverts = 0
    for index, operation in enumerate(operations):
        ts = TICK + timedelta(seconds=index)
        in_force = last in (ActionStatus.APPLIED, ActionStatus.VERIFIED)
        if operation == "apply":
            if not in_force:
                expected_applies += 1
                last = ActionStatus.APPLIED
            executor.apply(plan, ts=ts, owner="executor-a")
        elif operation == "verify":
            # A read: it confirms an effect that is there and retracts one that
            # is not, but it never changes anything.
            executor.verify(plan, ts=ts)
            last = ActionStatus.VERIFIED if in_force else ActionStatus.FAILED
        elif last is None:
            with pytest.raises(ActionRejectedError):
                executor.revert(plan, ts=ts, owner="executor-a")
        else:
            # Only an already-reverted key short-circuits. A FAILED key is
            # ambiguous about the world, so putting it back reaches the adapter.
            if last is not ActionStatus.REVERTED:
                expected_reverts += 1
            last = ActionStatus.REVERTED
            executor.revert(plan, ts=ts, owner="executor-a")
    settled = last in (ActionStatus.APPLIED, ActionStatus.VERIFIED)
    assert adapter.call_count("apply") == expected_applies
    assert adapter.call_count("revert") == expected_reverts
    assert executor.journal.in_force(plan.idempotency_key) == settled
    assert (plan.idempotency_key in adapter.in_place) == settled


# --- leases -----------------------------------------------------------------


def test_a_second_actor_on_the_same_target_is_refused_at_once() -> None:
    registry = LeaseRegistry(ttl=timedelta(seconds=120))
    registry.acquire("deployment/payment", owner="executor-a", now=TICK)
    with pytest.raises(TargetBusyError, match="leased by executor-a"):
        registry.acquire("deployment/payment", owner="executor-b", now=TICK)


def test_a_lease_on_another_target_is_unaffected() -> None:
    registry = LeaseRegistry(ttl=timedelta(seconds=120))
    registry.acquire("deployment/payment", owner="executor-a", now=TICK)
    other = registry.acquire("deployment/checkout", owner="executor-b", now=TICK)
    assert other.owner == "executor-b"


def test_a_stale_lease_does_not_block_a_target_forever() -> None:
    registry = LeaseRegistry(ttl=timedelta(seconds=30))
    registry.acquire("deployment/payment", owner="crashed", now=TICK)
    later = TICK + timedelta(seconds=31)
    assert registry.holder("deployment/payment", now=later) is None
    taken = registry.acquire("deployment/payment", owner="executor-b", now=later)
    assert taken.owner == "executor-b"


def test_the_same_owner_gets_its_own_lease_at_the_deadline_it_already_has() -> None:
    registry = LeaseRegistry(ttl=timedelta(seconds=60))
    first = registry.acquire("deployment/payment", owner="executor-a", now=TICK)
    later = TICK + timedelta(seconds=30)
    again = registry.acquire("deployment/payment", owner="executor-a", now=later)
    assert again.expires_ts == first.expires_ts, "asking again must not extend a hold"


def test_a_nested_hold_does_not_release_the_outer_one() -> None:
    registry = LeaseRegistry(ttl=timedelta(seconds=60))
    with registry.hold("deployment/payment", owner="executor-a", now=TICK):
        with registry.hold("deployment/payment", owner="executor-a", now=TICK):
            pass
        assert registry.holder("deployment/payment", now=TICK) is not None
    assert registry.holder("deployment/payment", now=TICK) is None


def test_an_actor_may_only_release_its_own_lease() -> None:
    registry = LeaseRegistry(ttl=timedelta(seconds=60))
    lease = registry.acquire("deployment/payment", owner="executor-a", now=TICK)
    stolen = lease.__class__(
        target=lease.target,
        owner="executor-b",
        acquired_ts=lease.acquired_ts,
        expires_ts=lease.expires_ts,
    )
    with pytest.raises(TargetBusyError, match="may only release its own lease"):
        registry.release(stolen)


def test_the_executor_refuses_to_act_on_a_target_another_actor_holds() -> None:
    leases = LeaseRegistry(ttl=timedelta(seconds=120))
    adapter = SimulatedActuator()
    configuration = _configuration(dry_run=False)
    executor = ActionExecutor(
        actuators=[adapter], configuration=configuration, dry_run=False, leases=leases
    )
    plan = _plan(adapter)
    leases.acquire(plan.target_ref, owner="another-executor", now=TICK)
    with pytest.raises(TargetBusyError):
        executor.apply(plan, ts=TICK, owner="executor-a")
    assert adapter.call_count("apply") == 0


def test_the_lease_is_given_back_even_when_the_adapter_fails() -> None:
    adapter = SimulatedActuator(fail_on_apply=True)
    leases = LeaseRegistry(ttl=timedelta(seconds=120))
    executor = ActionExecutor(
        actuators=[adapter],
        configuration=_configuration(dry_run=False),
        dry_run=False,
        leases=leases,
    )
    plan = _plan(adapter)
    executor.apply(plan, ts=TICK, owner="executor-a")
    assert leases.holder(plan.target_ref, now=TICK) is None


# --- approval ---------------------------------------------------------------


def test_a_plan_that_needs_a_human_is_refused_without_one() -> None:
    executor, adapter = _executor()
    plan = _plan(adapter, decision=_decision(approval=True))
    with pytest.raises(ActionRejectedError, match="needs 1 approver"):
        executor.apply(plan, ts=TICK, owner="executor-a")
    assert adapter.call_count("apply") == 0
    signed = executor.apply(plan, ts=TICK, owner="executor-a", approvals=("ada",))
    assert signed.status is ActionStatus.APPLIED
    assert signed.approvals == ("ada",)


def test_putting_our_own_change_back_never_waits_for_a_signature() -> None:
    """Approval protects production from being changed; undoing is the safe direction."""
    executor, adapter = _executor()
    plan = _plan(adapter, action_kind=ActionKind.ISOLATE, parameters={})
    executor.apply(plan, ts=TICK, owner="executor-a", approvals=("ada", "grace"))
    rolled_back = executor.revert(plan, ts=TICK + timedelta(seconds=5), owner="executor-a")
    assert rolled_back.status is ActionStatus.REVERTED
    assert not executor.journal.in_force(plan.idempotency_key)


def test_a_destructive_rung_needs_two_distinct_keys() -> None:
    executor, adapter = _executor()
    plan = _plan(adapter, action_kind=ActionKind.ISOLATE, parameters={})
    with pytest.raises(ActionRejectedError, match="needs 2 approver"):
        executor.apply(plan, ts=TICK, owner="executor-a", approvals=("ada",))
    with pytest.raises(ActionRejectedError, match="only once"):
        executor.apply(plan, ts=TICK, owner="executor-a", approvals=("ada", "ada"))
    signed = executor.apply(plan, ts=TICK, owner="executor-a", approvals=("ada", "grace"))
    assert signed.approvals == ("ada", "grace")


# --- registration and the journal -------------------------------------------


def test_an_adapter_that_is_not_permitted_cannot_be_registered() -> None:
    configuration = ActionConfig.model_validate(
        {
            "version": 1,
            "execution": {
                "dry_run": True,
                "lease_ttl_seconds": 120.0,
                "journal_capacity": 16,
            },
            "actuators": [{"actuator": "SIMULATED", "enabled": False}],
        }
    )
    with pytest.raises(ValueError, match="not enabled in the action configuration"):
        ActionExecutor(actuators=[SimulatedActuator()], configuration=configuration, dry_run=True)


def test_an_adapter_may_not_account_for_a_plan_it_was_not_given() -> None:
    executor, adapter = _executor()
    mine = _plan(adapter)
    other = _plan(adapter, parameters={"replicas": 9})
    forged = ActionOutcome(
        outcome_id="forged",
        ts=TICK,
        plan_id=other.plan_id,
        idempotency_key=other.idempotency_key,
        status=ActionStatus.APPLIED,
        dry_run=False,
        detail="reported against somebody else's key",
        honesty="SIMULATED",
    )
    with pytest.raises(ActuatorContractError, match="may only account for the plan"):
        executor._checked(mine, forged, expect_simulated=False)


def test_the_journal_never_forgets_an_effect_that_is_still_in_place() -> None:
    journal = ActionJournal(capacity=2)
    adapter = SimulatedActuator()
    executor = ActionExecutor(
        actuators=[adapter],
        configuration=_configuration(dry_run=False, capacity=2),
        dry_run=False,
        journal=journal,
    )
    plans = [_plan(adapter, parameters={"replicas": count}) for count in (2, 3, 4)]
    for index, plan in enumerate(plans[:2]):
        executor.apply(plan, ts=TICK + timedelta(seconds=index), owner="executor-a")
    with pytest.raises(ActionJournalFullError, match="still in force"):
        executor.apply(plans[2], ts=TICK + timedelta(seconds=2), owner="executor-a")
    assert adapter.call_count("apply") == 2, "it refused before touching anything"
    # Releasing one makes room, and the two live effects are still remembered.
    executor.revert(plans[0], ts=TICK + timedelta(seconds=3), owner="executor-a")
    executor.apply(plans[2], ts=TICK + timedelta(seconds=4), owner="executor-a")
    assert journal.in_force(plans[1].idempotency_key)
    assert journal.in_force(plans[2].idempotency_key)
    assert not journal.in_force(plans[0].idempotency_key)


# --- the audit trail --------------------------------------------------------


def test_every_action_the_executor_takes_is_written_down() -> None:
    """The executor is the one place every action passes through, so it is the
    one place the trail can be complete rather than well-intentioned."""
    ledger = AuditChain()
    executor, adapter = _executor(dry_run=False, ledger=ledger)
    plan = _plan(adapter)

    executor.apply(plan, ts=TICK, owner="operator")
    executor.verify(plan, ts=TICK)
    executor.revert(plan, ts=TICK, owner="operator")

    kinds = [entry.kind for entry in ledger.entries()]
    assert kinds == [
        AuditEventKind.ACTION_APPLIED,
        AuditEventKind.ACTION_VERIFIED,
        AuditEventKind.ACTION_REVERTED,
    ]
    assert verify_chain(ledger.entries()).intact
    assert all(entry.plan_id == plan.plan_id for entry in ledger.entries())
    assert all(entry.incident_id == plan.incident_id for entry in ledger.entries())


def test_a_refusal_is_written_down_before_it_is_raised() -> None:
    """A caller that swallows the exception must not be able to erase the record."""
    ledger = AuditChain()
    executor, adapter = _executor(dry_run=False, ledger=ledger)
    plan = _plan(adapter, action_kind=ActionKind.ROLLBACK, parameters={})

    with pytest.raises(ActionRejectedError):
        executor.apply(plan, ts=TICK, owner="operator")

    entries = ledger.entries()
    assert [entry.kind for entry in entries] == [AuditEventKind.ACTION_REFUSED]
    assert "refused ROLLBACK" in entries[0].summary
    assert entries[0].body["reason"]


def test_a_dry_run_is_recorded_as_a_plan_and_labelled_simulated() -> None:
    """Pretending is worth recording, and must never look like acting."""
    ledger = AuditChain()
    executor, adapter = _executor(dry_run=True, ledger=ledger)

    executor.apply(_plan(adapter), ts=TICK, owner="operator")

    entry = ledger.entries()[0]
    assert entry.kind is AuditEventKind.ACTION_PLANNED
    assert entry.honesty == "SIMULATED"


def test_an_executor_with_no_ledger_still_works() -> None:
    """The audit trail is a capability, not a dependency the plane cannot run without."""
    executor, adapter = _executor(dry_run=False)

    assert executor.apply(_plan(adapter), ts=TICK, owner="operator").status is ActionStatus.APPLIED
