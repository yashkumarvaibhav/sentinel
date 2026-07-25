"""The remediation loop: the one component whose job is to compose the others.

Phase 5 built eight pieces and every one of them is finished - the ladder that
chooses a rung, the adapters that carry an effect out, the guard that refuses one
that lands where it may not, the canary that widens instead of firing, the
breaker that stops the platform acting when it has stopped helping, the rollback
that undoes an action that cost somebody their SLO, the registry that remembers
what is standing, and the ledger that writes it all down. None of them run. This
module is the thing that runs them, and the properties it exists to guarantee are
the ones that only appear when they are put together:

* **An incident is answered once, not once per tick.** The decision plane emits a
  decision for an open incident on every pass; a loop that acted on each of them
  would apply the same restraint forty times. The registry is the memory: an
  incident with an effect already standing is not answered again.
* **A stronger answer supersedes a weaker one; a weaker one never displaces a
  stronger one.** As evidence firms up the ladder climbs, so the loop gives back
  what is standing and applies the rung the new evidence earned. It does *not*
  loosen a standing restraint when confidence dips for a tick - releasing a rate
  limit because a signal flickered is how a flapping metric hands an attack its
  throughput back.
* **Effects are given back in the reverse of the order they were applied.** Each
  step of a canary records the value it found, so unwinding 20 -> 10 -> 5 -> 0
  restores the world and unwinding 5 -> 10 -> 20 leaves it restrained at 20. The
  registry lists oldest-first; every release here walks it backwards.
* **Nothing raises out of the loop.** A refusal - the breaker being open, a guard
  saying no, a rung with no approver - is a recorded outcome, not an exception,
  because an always-on loop that dies on tick N never reaches tick N+1. Only a
  breach of the actuator contract is allowed to escape, because that is a bug in
  an adapter rather than an answer about an incident.

Time and measurement are passed in. Nothing here reads a clock or a metric store,
so a capture replay drives the loop exactly as a live run does.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from action.actuators.base import ActionRejectedError, Actuator
from action.breaker import BreakerState, RemediationBreaker
from action.executor import ActionExecutor
from action.guards import (
    BlastRadiusGuard,
    CanaryResult,
    CanaryRollout,
    CollateralReport,
    guarded_apply,
)
from action.ladder import (
    LadderError,
    LadderSelection,
    RemediationLadder,
    RestraintRegistry,
    RungChoice,
    StandingRestraint,
)
from action.rollback import RollbackResult, VerifiedRollback
from audit import AuditSink
from contracts import (
    WATCHING_ACTIONS,
    ActionOutcome,
    ActionPlan,
    ActionStatus,
    AuditEventKind,
    Decision,
)

# Who the ledger and the leases record as having acted when a person did not.
DEFAULT_OWNER = "sentinel-remediator"


@dataclass(frozen=True, slots=True)
class AppliedEffect:
    """One effect this loop put in place, and how it got there."""

    choice: RungChoice
    plan: ActionPlan
    outcome: ActionOutcome
    # Present when the rung carried a dial, in which case ``plan`` is the last
    # step that ran rather than the share the rung asked for.
    canary: CanaryResult | None = None

    @property
    def in_force(self) -> bool:
        """Whether this effect actually changed something that must be given back."""
        return self.outcome.status is ActionStatus.APPLIED


@dataclass(frozen=True, slots=True)
class ReleasedRestraint:
    """One standing effect the loop gave back, or could not.

    ``outcome`` is ``None`` when the revert itself was refused. That is recorded
    rather than raised: one effect that cannot be undone must not stop the others
    from being, and an operator needs to be told which one is still in force.
    """

    plan: ActionPlan
    outcome: ActionOutcome | None
    detail: str

    @property
    def reverted(self) -> bool:
        return self.outcome is not None and self.outcome.status is ActionStatus.REVERTED


@dataclass(frozen=True, slots=True)
class RemediationRun:
    """One pass of the loop over one incident: what it did, and what it declined.

    A run that did nothing is a first-class result with a stated reason, which is
    why ``refusals`` is a tuple rather than an exception. Both fields can be
    populated at once: a primary rung can land while its companion is refused,
    and reporting only one of those would be reporting half of what happened.
    """

    decision_id: str
    incident_id: str
    breaker: BreakerState
    selection: LadderSelection | None = None
    applied: tuple[AppliedEffect, ...] = ()
    released: tuple[ReleasedRestraint, ...] = ()
    collateral: CollateralReport | None = None
    rollback: RollbackResult | None = None
    # How much the target's own availability moved, measured with the rollback's
    # reader. None means nobody measured it - which is not the same as zero, and
    # is deliberately not counted against the breaker's futility streak.
    improvement: float | None = None
    refusals: tuple[str, ...] = ()

    @property
    def acted(self) -> bool:
        """Whether anything was actually put in place this pass."""
        return any(effect.in_force for effect in self.applied)

    @property
    def requires_page(self) -> bool:
        """Whether this pass ended in a state a person has to be told about."""
        return self.breaker.requires_page


class Remediator:
    """Runs a decision through the ladder, the guards, the executor and back out."""

    __slots__ = (
        "_actuators",
        "_breaker",
        "_breaker_open",
        "_executor",
        "_guard",
        "_ladder",
        "_ledger",
        "_owner",
        "_registry",
        "_rollback",
    )

    def __init__(
        self,
        *,
        ladder: RemediationLadder,
        actuators: Iterable[Actuator],
        executor: ActionExecutor,
        guard: BlastRadiusGuard,
        breaker: RemediationBreaker,
        rollback: VerifiedRollback,
        registry: RestraintRegistry | None = None,
        ledger: AuditSink | None = None,
        owner: str = DEFAULT_OWNER,
    ) -> None:
        self._ladder = ladder
        self._actuators = {actuator.kind: actuator for actuator in actuators}
        self._executor = executor
        self._guard = guard
        self._breaker = breaker
        # Required, not optional. A loop that can apply an effect but not undo
        # one that harmed somebody is not a loop this platform offers.
        self._rollback = rollback
        # Explicitly `is None`: an empty registry is falsy, and `registry or
        # RestraintRegistry()` would hand back one nothing is recorded in.
        self._registry = registry if registry is not None else RestraintRegistry()
        # Optional, and `is None` for the same reason the others are: an empty
        # chain is falsy, and `ledger or AuditChain()` would hand back a ledger
        # nothing is ever read from.
        self._ledger = ledger
        # Whether the breaker was open the last time this loop looked, so the
        # ledger records it *opening* rather than it being open.
        self._breaker_open = False
        self._owner = owner

    @property
    def registry(self) -> RestraintRegistry:
        """What this loop currently has standing."""
        return self._registry

    @property
    def ladder_fingerprint(self) -> str:
        """The committed configuration every rung this loop climbed was chosen under."""
        return self._ladder.fingerprint

    def consider(
        self,
        decision: Decision,
        *,
        ts: datetime,
        settled_at: datetime | None = None,
        recovered_at: datetime | None = None,
        approvals: Sequence[str] = (),
    ) -> RemediationRun:
        """Answer one incident: choose a rung, hold it to every gate, apply, watch.

        Three moments, because there are genuinely three and collapsing them
        would silently make every recovery measure zero:

        * ``ts`` - when the effect is put in place, and what the canary probes
          against as it widens.
        * ``settled_at`` - when the effect has had time to show itself, and so
          when the loop looks for collateral it could not have seen at ``ts``.
        * ``recovered_at`` - when an undo has had time to show *itself*, which is
          the second of the two readings ``users_restored`` is computed from.

        All three are passed in rather than taken from a clock, so a capture
        replay measures a recovery exactly as a live run does. Each defaults to
        the one before it, which is the honest degenerate case: readings taken at
        the same instant report no change, rather than a change nobody measured.
        """
        settled = settled_at if settled_at is not None else ts
        recovered = recovered_at if recovered_at is not None else settled
        state = self._breaker.state(ts)
        self._audit_breaker(state, ts=ts)
        if state.open:
            return self._audited(
                RemediationRun(
                    decision_id=decision.decision_id,
                    incident_id=decision.incident_id,
                    breaker=state,
                    refusals=(state.reason or "the remediation breaker is open",),
                ),
                ts=ts,
            )
        try:
            selection = self._ladder.select(decision)
        except LadderError as refusal:
            return self._audited(
                RemediationRun(
                    decision_id=decision.decision_id,
                    incident_id=decision.incident_id,
                    breaker=state,
                    refusals=(str(refusal),),
                ),
                ts=ts,
            )

        superseded, blocked = self._supersede(decision, selection, ts=ts)
        if blocked is not None:
            return self._audited(
                RemediationRun(
                    decision_id=decision.decision_id,
                    incident_id=decision.incident_id,
                    breaker=state,
                    selection=selection,
                    released=superseded,
                    refusals=(blocked,),
                ),
                ts=ts,
            )

        before = self._read(decision.target_service, ts=ts)
        applied, refusals = self._apply(selection, decision, ts=ts, approvals=approvals)
        run = RemediationRun(
            decision_id=decision.decision_id,
            incident_id=decision.incident_id,
            breaker=self._breaker.state(ts),
            selection=selection,
            applied=applied,
            released=superseded,
            refusals=refusals,
        )
        if not run.acted:
            return self._audited(run, ts=ts)
        watched = self._watch(
            run,
            before=before,
            settled=settled,
            recovered=recovered,
            approvals=approvals,
        )
        return self._audited(watched, ts=settled)

    def expire(
        self, now: datetime, *, approvals: Sequence[str] = ()
    ) -> tuple[ReleasedRestraint, ...]:
        """Give back every effect that has outlived the rung that authorised it.

        This is the half of the loop nobody sees working and everybody notices
        when it does not: a restraint whose TTL has passed and that nobody
        released is a configuration change with no author.
        """
        return self._release(self._registry.expired(now), ts=now, approvals=approvals)

    # --- choosing whether to act at all --------------------------------------

    def _supersede(
        self, decision: Decision, selection: LadderSelection, *, ts: datetime
    ) -> tuple[tuple[ReleasedRestraint, ...], str | None]:
        """Clear the way for a stronger answer, or report why there is no room.

        Returns the restraints given back and, when the loop should not act at
        all, the reason. A different ladder always supersedes: the diagnosis
        itself changed, so the response it earned is no longer the one standing.
        """
        standing = self._registry.for_incident(decision.incident_id)
        if not standing:
            return (), None
        chosen = self._ladder.rank(selection.primary)
        # Companions rank as None and are skipped: a companion is part of an
        # answer rather than an answer, so it is not what a new rung is measured
        # against. It is still released with the primary it rode in with.
        ranked = [
            (entry, rank)
            for entry in standing
            if (rank := self._ladder.rank(entry.choice)) is not None
        ]
        if not ranked or chosen is None:
            # Nothing comparable is standing, and this incident already has an
            # effect in force. Acting again would stack a second effect on top of
            # one we cannot say we have outgrown, so the safe direction is to
            # leave what is standing alone until its TTL gives it back.
            return (), (
                f"an effect is already standing on {decision.incident_id} and "
                f"{selection.primary.rung_id} cannot be compared with it"
            )
        held, held_rank = max(ranked, key=lambda pair: pair[1])
        if held.choice.ladder_id == selection.primary.ladder_id and chosen <= held_rank:
            return (), (
                f"{held.choice.rung_id} is already standing on {decision.incident_id} until "
                f"{held.expires_at.isoformat()}; {selection.primary.rung_id} is not a stronger "
                "answer, and a restraint is not loosened because confidence dipped for a tick"
            )
        return self._release(standing, ts=ts), None

    # --- putting the answer in place -----------------------------------------

    def _apply(
        self,
        selection: LadderSelection,
        decision: Decision,
        *,
        ts: datetime,
        approvals: Sequence[str],
    ) -> tuple[tuple[AppliedEffect, ...], tuple[str, ...]]:
        """Carry out every effect this selection asks for, in order.

        A companion that is refused does not retract the primary that landed:
        "restrain the cohort and add headroom" degrades honestly to "restrain the
        cohort", with the failure stated rather than swallowed.
        """
        applied: list[AppliedEffect] = []
        refusals: list[str] = []
        for choice in selection.choices:
            try:
                effect = self._apply_one(choice, decision, ts=ts, approvals=approvals)
            except ActionRejectedError as refusal:
                refusals.append(f"{choice.rung_id}: {refusal}")
                continue
            applied.append(effect)
            if not effect.in_force:
                # A dry run touched nothing, so there is nothing to give back and
                # nothing for the breaker to count. Recording a restraint here
                # would make the loop try to revert an effect that was never
                # applied - and the executor would rightly refuse it.
                continue
            self._record(effect, ts=ts)
        return tuple(applied), tuple(refusals)

    def _apply_one(
        self,
        choice: RungChoice,
        decision: Decision,
        *,
        ts: datetime,
        approvals: Sequence[str],
    ) -> AppliedEffect:
        actuator = self._actuators.get(choice.actuator)
        if actuator is None:
            raise ActionRejectedError(
                f"{choice.rung_id} needs the {choice.actuator.value} adapter, which this loop was "
                "not given; a rung whose adapter is absent is refused rather than substituted"
            )
        if choice.canary_parameter is not None and choice.canary_shares:
            canary = CanaryRollout(
                executor=self._executor, guard=self._guard, probe=self._rollback.probe()
            ).apply(
                actuator=actuator,
                decision=decision,
                choice=choice,
                ts=ts,
                owner=self._owner,
                approvals=approvals,
            )
            step = canary.steps[-1]
            return AppliedEffect(choice=choice, plan=step.plan, outcome=step.outcome, canary=canary)
        plan = actuator.plan(
            decision,
            action_kind=choice.action_kind,
            parameters=choice.parameters,
            ts=ts,
        )
        outcome = guarded_apply(
            executor=self._executor,
            guard=self._guard,
            plan=plan,
            choice=choice,
            ts=ts,
            owner=self._owner,
            approvals=approvals,
        )
        return AppliedEffect(choice=choice, plan=plan, outcome=outcome)

    def _record(self, effect: AppliedEffect, *, ts: datetime) -> None:
        """Remember what must be given back, and what the breaker should count.

        The two are deliberately different. **Every step of a canary is recorded
        as standing**, because each one wrote a value some later revert has to
        restore. **One action is recorded against the breaker**, because a canary
        that walked 5 -> 10 -> 20 is one answer to one incident, and counting it
        three times would trip a runaway detector on the platform being careful.
        """
        if effect.canary is not None:
            for step in effect.canary.steps:
                if step.outcome.status is ActionStatus.APPLIED:
                    self._registry.record(step.plan, effect.choice, applied_at=ts)
        else:
            self._registry.record(effect.plan, effect.choice, applied_at=ts)
        if effect.choice.action_kind not in WATCHING_ACTIONS:
            self._breaker.record(effect.plan, ts=ts)

    # --- watching what it did ------------------------------------------------

    def _watch(
        self,
        run: RemediationRun,
        *,
        before: float | None,
        settled: datetime,
        recovered: datetime,
        approvals: Sequence[str],
    ) -> RemediationRun:
        """Probe for collateral, undo what harmed anything, and judge the result.

        The probe here is a second look, not a repeat of the canary's. The canary
        asked "is anything breaking as I widen?" at the moment of each step; this
        asks "now that it has been in force for a while, is anything breaking?" -
        and harm that takes time to appear is exactly the harm a canary cannot
        see.
        """
        newest = run.applied[-1]
        rollback = self._rollback.rollback_if_harmed(
            newest.plan,
            ts=settled,
            owner=self._owner,
            approvals=approvals,
            settled_at=recovered,
        )
        released = list(run.released)
        if rollback.reverted:
            self._registry.release(newest.plan)
            rest = [
                entry
                for entry in self._registry.for_incident(run.incident_id)
                if entry.plan.plan_id != newest.plan.plan_id
            ]
            released.extend(self._release(rest, ts=settled, approvals=approvals))
        improvement = self._improvement(run, rollback, before=before, recovered=recovered)
        self._observe(run, improvement)
        return RemediationRun(
            decision_id=run.decision_id,
            incident_id=run.incident_id,
            breaker=self._breaker.state(settled),
            selection=run.selection,
            applied=run.applied,
            released=tuple(released),
            collateral=CollateralReport(
                clean=not rollback.reverted, detail=rollback.detail, harmed=rollback.harmed
            ),
            rollback=rollback if rollback.reverted else None,
            improvement=improvement,
            refusals=run.refusals,
        )

    def _improvement(
        self,
        run: RemediationRun,
        rollback: RollbackResult,
        *,
        before: float | None,
        recovered: datetime,
    ) -> float | None:
        """How much good the action did, as a signed number or not at all.

        An action that had to be undone did not help, and the recovery measured
        by the rollback is the size of the harm it caused - so it is reported
        **negated**. Feeding that number in unsigned would be the worst possible
        bug in this file: the more damage an action did, the more effective the
        breaker would believe it to be.

        An action that harmed nothing is judged on the target's own availability,
        read with the same instrument. Either reading missing means the effect
        was not measured, which is not the same as an effect of zero and is
        deliberately not counted against the futility streak.
        """
        if rollback.reverted:
            restored = rollback.availability_restored
            return None if restored is None else -restored
        after = self._read(run.applied[0].plan.target_service, ts=recovered)
        return None if before is None or after is None else after - before

    def _observe(self, run: RemediationRun, improvement: float | None) -> None:
        """Tell the breaker how the primary rung turned out, and only the primary.

        A companion is part of the same answer rather than a second attempt at
        it, so judging both would put two failures on the futility streak for one
        wrong diagnosis - and trip the breaker in half the actions it was
        configured for.
        """
        if improvement is None:
            return
        for effect in run.applied:
            if effect.in_force and effect.choice.action_kind not in WATCHING_ACTIONS:
                self._breaker.observe(effect.plan, improvement=improvement)
                return

    def _read(self, service: str | None, *, ts: datetime) -> float | None:
        """This service's availability now, or nothing if it could not be read."""
        if service is None:
            return None
        reading = self._rollback.reader(service, ts=ts)
        return None if reading is None else reading.availability

    # --- writing down what the loop itself decided ---------------------------

    def _audited(self, run: RemediationRun, *, ts: datetime) -> RemediationRun:
        """Record what the platform did about this incident, and hand the run back.

        This is the **incident-level** record, and it is deliberately a different
        statement from the plan-level entries the executor writes. "``RATE_LIMIT``
        on this target was refused for want of a signature" and "nothing was done
        about this incident, and here is every reason" are different facts, and
        only the second one answers the question somebody reconstructing an
        outage actually asks. Where both are true, both are written: they are two
        levels of the same event, not two copies of it.

        A run that acted earns a ``DECISION`` entry naming the rung it climbed;
        one that did not earns ``ACTION_REFUSED`` carrying every reason. The
        rollback, when there was one, is recorded separately because *why* an
        action was undone is not derivable from the fact that it was.
        """
        if self._ledger is None:
            return run
        if run.rollback is not None:
            self._append(
                ts=ts,
                kind=AuditEventKind.ROLLBACK,
                summary=f"undid the action taken on {run.incident_id}: {run.rollback.detail}",
                body={
                    "harmed": list(run.rollback.harmed),
                    "availability_restored": run.rollback.availability_restored,
                    "reverted": run.rollback.reverted,
                },
                run=run,
            )
        if run.acted:
            primary = run.selection.primary if run.selection is not None else None
            self._append(
                ts=ts,
                kind=AuditEventKind.DECISION,
                summary=(
                    f"acted on {run.incident_id} at rung "
                    f"{primary.rung_id if primary else 'unknown'}: "
                    f"{primary.reason if primary else 'no reason recorded'}"
                ),
                body={
                    "ladder_id": primary.ladder_id if primary else None,
                    "rung_id": primary.rung_id if primary else None,
                    "effects": [effect.plan.plan_id for effect in run.applied],
                    "gates_passed": sorted(
                        {gate for effect in run.applied for gate in effect.outcome.gates_passed}
                    ),
                    "improvement": run.improvement,
                    "released": [entry.plan.plan_id for entry in run.released],
                    "refusals": list(run.refusals),
                },
                run=run,
            )
            return run
        self._append(
            ts=ts,
            kind=AuditEventKind.ACTION_REFUSED,
            summary=(
                f"took no action on {run.incident_id}: "
                f"{run.refusals[0] if run.refusals else 'no reason recorded'}"
            ),
            body={
                "reasons": list(run.refusals),
                "released": [e.plan.plan_id for e in run.released],
            },
            run=run,
        )
        return run

    def _audit_breaker(self, state: BreakerState, *, ts: datetime) -> None:
        """Record the breaker *opening*, which is an event, not a state.

        Written on the transition only. A loop that appended an entry every time
        it found the breaker already open would bury the moment it tripped - the
        one thing a person reading the ledger needs to find - under a page of
        identical lines saying the same thing about the same condition.
        """
        was_open = self._breaker_open
        self._breaker_open = state.open
        if self._ledger is None or not state.open or was_open:
            return
        self._append(
            ts=ts,
            kind=AuditEventKind.BREAKER_OPENED,
            summary=state.reason or "the remediation breaker opened",
            body={
                "recent_actions": state.recent_actions,
                "requires_page": state.requires_page,
            },
            run=None,
        )

    def _append(
        self,
        *,
        ts: datetime,
        kind: AuditEventKind,
        summary: str,
        body: dict[str, object],
        run: RemediationRun | None,
    ) -> None:
        if self._ledger is None:  # pragma: no cover - callers check first
            return
        self._ledger.append(
            ts=ts,
            kind=kind,
            actor=self._owner,
            summary=summary,
            body=body,
            incident_id=None if run is None else run.incident_id,
            decision_id=None if run is None else run.decision_id,
            # No plan id: this is a statement about an incident, and attaching
            # one of several effects to it would quietly make it look like a
            # statement about that effect.
            plan_id=None,
            honesty="SIMULATED" if self._executor.dry_run else "REAL",
        )

    # --- giving effects back -------------------------------------------------

    def _release(
        self,
        restraints: Sequence[StandingRestraint],
        *,
        ts: datetime,
        approvals: Sequence[str] = (),
    ) -> tuple[ReleasedRestraint, ...]:
        """Revert standing effects newest-first, and keep going if one refuses.

        Newest-first is not a detail. Each canary step recorded the value it
        found, so unwinding 20 -> 10 -> 5 restores the world while unwinding
        5 -> 10 -> 20 would leave the cohort restrained at the widest share the
        platform ever reached.
        """
        released: list[ReleasedRestraint] = []
        for entry in sorted(restraints, key=lambda item: item.order, reverse=True):
            try:
                outcome = self._executor.revert(
                    entry.plan, ts=ts, owner=self._owner, approvals=approvals
                )
            except ActionRejectedError as refusal:
                released.append(
                    ReleasedRestraint(
                        plan=entry.plan,
                        outcome=None,
                        detail=(
                            f"{entry.choice.rung_id} on {entry.plan.target_ref} could not be given "
                            f"back and is still in force: {refusal}"
                        ),
                    )
                )
                continue
            self._registry.release(entry.plan)
            released.append(
                ReleasedRestraint(
                    plan=entry.plan,
                    outcome=outcome,
                    detail=(
                        f"{entry.choice.rung_id} on {entry.plan.target_ref} was given back after "
                        f"{(ts - entry.applied_at).total_seconds():.0f}s"
                    ),
                )
            )
        return tuple(released)


__all__ = [
    "DEFAULT_OWNER",
    "AppliedEffect",
    "ReleasedRestraint",
    "RemediationRun",
    "Remediator",
]
