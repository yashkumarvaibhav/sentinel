"""Graded ladders: which rung a decision earns, and how long it may stand.

The policy gate already answered *whether*. Everything here answers *which*, and
it is a deliberately small amount of logic, because the interesting content is in
``config/ladders.yml`` where an operator can read it.

Three properties are the point of the module:

* **Confidence gates how far you may climb.** A ladder is ordered weakest first
  and the chosen rung is the strongest one whose ``minimum_confidence`` the
  decision actually clears. Less certainty therefore produces a gentler action -
  never a stronger one - which is the confidence-gated downgrade this plane was
  asked for, expressed as an ordering rather than as a special case.
* **A rung may only ever widen approval.** ``autonomous: false`` requires a
  person; nothing in a ladder can decide that an action the gate wanted signed
  no longer needs signing.
* **Every choice expires.** A rung carries a TTL, and the registry here is what
  turns "we restrained a cohort" into "we restrained a cohort until 14:32". An
  action with no expiry is a configuration change nobody remembers making.

What this module deliberately does NOT do is build or run anything. It returns a
*choice*: the adapter to use, the rung, and the parameters in that adapter's own
absolute vocabulary. The adapter still builds the plan, so every refusal an
adapter makes - an unmapped service, a cohort that is not committed, a variant
this platform may not write - still applies to a laddered action exactly as it
does to a hand-made one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from action.config import FlagsConfig, Ladder, LadderConfig, LadderRung
from contracts import (
    ACTING_ACTIONS,
    ActionKind,
    ActionParameterValue,
    ActionPlan,
    ActuatorKind,
    Decision,
)


class LadderError(RuntimeError):
    """A ladder could not answer the question it was asked."""


class NoRungAvailableError(LadderError):
    """Nothing on the ladder is reachable on this evidence.

    A first-class outcome, not a failure: a decision that clears no rung's
    confidence has earned no action, and inventing one for it would defeat the
    gating the thresholds exist to provide.
    """


@dataclass(frozen=True, slots=True)
class RungChoice:
    """One rung, resolved against one decision, ready for an adapter to plan."""

    rung_id: str
    ladder_id: str
    actuator: ActuatorKind
    action_kind: ActionKind
    parameters: Mapping[str, ActionParameterValue]
    ttl: timedelta
    requires_human_approval: bool
    maximum_blast_fraction: float
    reason: str
    canary_parameter: str | None = None
    canary_shares: tuple[int, ...] = ()

    def expires_at(self, applied_at: datetime) -> datetime:
        """When the platform gives this effect back if nobody else does."""
        return applied_at + self.ttl

    def permits(self, plan: ActionPlan) -> bool:
        """Whether the plan an adapter built stays inside this rung's blast cap.

        Checked after planning rather than before, because only the adapter knows
        what its effect actually disturbs - the ladder states a ceiling and the
        adapter states a measurement, and the two meet here. This is the seam the
        5.6 blast-radius guard plugs into: the ladder owns the number, the guard
        owns what to do about it.
        """
        return plan.estimated_blast_fraction <= self.maximum_blast_fraction


@dataclass(frozen=True, slots=True)
class LadderSelection:
    """What one decision earned: a rung, and any companion that rides with it.

    The companion is not a second choice. It is part of the same answer - "hold
    the cohort to its ceiling *and* add headroom" - and it is separated here only
    because the two effects are carried out by different adapters.
    """

    primary: RungChoice
    companion: RungChoice | None = None

    @property
    def choices(self) -> tuple[RungChoice, ...]:
        """Every effect this selection asks for, in the order to apply them."""
        return (self.primary,) if self.companion is None else (self.primary, self.companion)


class RemediationLadder:
    """Chooses the rung a decision has earned, from committed configuration."""

    __slots__ = ("_configuration", "_flags")

    def __init__(self, configuration: LadderConfig, *, flags: FlagsConfig | None = None) -> None:
        self._configuration = configuration
        # Only ever read, and only to answer "which flag does this service
        # have" - the resolver a FLAG_FLIP rung needs and the one thing a
        # ladder cannot state in advance.
        self._flags = flags

    @property
    def fingerprint(self) -> str:
        """The configuration these choices were made under."""
        return self._configuration.fingerprint

    def select(self, decision: Decision) -> LadderSelection:
        """Choose the strongest rung this decision's confidence actually reaches."""
        ladder = self._ladder_for(decision)
        available = [rung for rung in ladder.rungs if self._is_available(rung, decision)]
        reachable = [rung for rung in available if _clears(rung, decision)]
        if not reachable:
            offered = (
                ", ".join(
                    f"{rung.rung_id} needs {rung.minimum_confidence:.2f}" for rung in available
                )
                or "no rung of it can name a target on this incident"
            )
            raise NoRungAvailableError(
                f"{decision.decision_id} reaches no rung of {ladder.ladder_id} at confidence "
                f"{_confidence(decision):.3f}: {offered}"
            )
        # Weakest first, so the last one that cleared is the strongest reached.
        chosen = reachable[-1]
        return LadderSelection(
            primary=self._resolve(chosen, ladder, decision),
            companion=self._companion(chosen, ladder, decision),
        )

    def rank(self, choice: RungChoice) -> int | None:
        """How high up its own ladder a chosen rung sits; the weakest is 0.

        The ordering already exists - a ladder is committed weakest-first and the
        loader rejects one whose minimum confidences decrease - so this reads it
        rather than deriving a second one. It is what lets the remediation loop
        answer "is this a stronger answer than the one already standing?" without
        comparing confidences, which would be comparing the *evidence* rather
        than the *response* the operator committed to it.

        **A companion ranks as ``None``, and that is a real answer rather than a
        failure.** A companion rides with the rung that named it and is never an
        answer on its own, so "how far up the escalation is it?" is a question
        with no meaning - and a caller that compared it with a primary would be
        ordering two things that are not on the same scale. Not knowing where a
        rung sits is only an error when the rung is not in the committed
        configuration at all.
        """
        ladder = self._configuration.for_ladder(choice.ladder_id)
        if ladder is None:
            raise LadderError(f"no committed ladder is named {choice.ladder_id}")
        for index, rung in enumerate(ladder.rungs):
            if rung.rung_id == choice.rung_id:
                return index
        if any(rung.rung_id == choice.rung_id for rung in ladder.companions):
            return None
        raise LadderError(
            f"{choice.rung_id} is neither a rung nor a companion of {ladder.ladder_id}"
        )

    def _ladder_for(self, decision: Decision) -> Ladder:
        if decision.action not in ACTING_ACTIONS:
            raise LadderError(
                f"{decision.decision_id} decided to {decision.action.value}, which touches "
                "nothing; a ladder is climbed only by a decision that decided to act"
            )
        if decision.verdict_class is None:
            raise LadderError(f"{decision.decision_id} carries no verdict, so no ladder answers it")
        ladder = self._configuration.for_verdict(decision.verdict_class.value)
        if ladder is None:
            raise LadderError(
                f"no ladder answers {decision.verdict_class.value}; a diagnosis with no committed "
                "response is left to a person rather than improvised on"
            )
        return ladder

    def _is_available(self, rung: LadderRung, decision: Decision) -> bool:
        """Whether this rung can name its own target on this decision."""
        if rung.parameters_from is None:
            return True
        return self._committed_flag(decision) is not None

    def _committed_flag(self, decision: Decision) -> str | None:
        """The one flag committed for this service, or nothing.

        Two flags is not a choice this may make: which of a service's changes
        actually caused the incident is a question the change feed answers, and
        wiring that in is the RCA-informed refinement. Until then a service with
        several committed flags simply does not offer the rung, and the ladder
        falls through to one it can aim.
        """
        if self._flags is None or decision.target_service is None:
            return None
        candidates = self._flags.flags_for(decision.target_service)
        return candidates[0] if len(candidates) == 1 else None

    def _resolve(self, rung: LadderRung, ladder: Ladder, decision: Decision) -> RungChoice:
        parameters: dict[str, ActionParameterValue] = dict(rung.parameters)
        if rung.parameters_from == "committed_flag":
            flag = self._committed_flag(decision)
            if flag is None:  # pragma: no cover - _is_available filtered these out
                raise LadderError(f"{rung.rung_id} cannot name a flag for this incident")
            parameters["flag"] = flag
        return RungChoice(
            rung_id=rung.rung_id,
            ladder_id=ladder.ladder_id,
            actuator=rung.actuator,
            action_kind=rung.action_kind,
            parameters=parameters,
            ttl=timedelta(seconds=rung.ttl_seconds),
            # Only ever widened. A rung that says a person must sign adds that
            # requirement; one that says nobody need not cannot remove the
            # gate's.
            requires_human_approval=decision.requires_human_approval or not rung.autonomous,
            maximum_blast_fraction=rung.maximum_blast_fraction,
            reason=ladder.reason,
            canary_parameter=rung.canary_parameter,
            canary_shares=rung.canary_shares,
        )

    def _companion(self, rung: LadderRung, ladder: Ladder, decision: Decision) -> RungChoice | None:
        if rung.paired_with is None:
            return None
        companion = next(entry for entry in ladder.companions if entry.rung_id == rung.paired_with)
        if not self._is_available(companion, decision) or not _clears(companion, decision):
            return None
        return self._resolve(companion, ladder, decision)


def _confidence(decision: Decision) -> float:
    """An acting decision always carries one; treat a missing one as none at all."""
    return decision.confidence if decision.confidence is not None else 0.0


def _clears(rung: LadderRung, decision: Decision) -> bool:
    return _confidence(decision) >= rung.minimum_confidence


@dataclass(frozen=True, slots=True)
class StandingRestraint:
    """One applied effect, and the moment the platform takes it back."""

    plan: ActionPlan
    choice: RungChoice
    applied_at: datetime
    # Where this effect falls in the order they were applied in. A timestamp is
    # not enough: several effects are routinely applied within one tick - every
    # step of a canary, a rung and its companion - and they carry the SAME
    # `applied_at`. Undoing them requires knowing which came last, and a stable
    # sort over equal timestamps silently yields the order they were applied in
    # rather than its reverse.
    sequence: int = 0

    @property
    def order(self) -> tuple[datetime, int]:
        """The total order effects were applied in, with ties broken honestly."""
        return (self.applied_at, self.sequence)

    @property
    def expires_at(self) -> datetime:
        return self.choice.expires_at(self.applied_at)


class RestraintRegistry:
    """What is currently in force, and what has outlived its rung's TTL.

    In-process, like the journal and the lease it sits beside (5.1): the durable
    single-writer form lands with the audit ledger. The shape is the seam - an
    effect is recorded when it is applied, dropped when it is given back, and
    listed the moment it is older than the rung allowed.
    """

    __slots__ = ("_applied", "_standing")

    def __init__(self) -> None:
        self._standing: dict[str, StandingRestraint] = {}
        # Monotonic across the registry's life, never reused. It is the only
        # thing that can order two effects applied in the same tick.
        self._applied = 0

    def __len__(self) -> int:
        return len(self._standing)

    def __bool__(self) -> bool:
        """An empty registry is still a registry.

        Stated explicitly for the same reason the journal states it: defining
        ``__len__`` makes an empty instance falsy, and a caller writing
        ``registry or RestraintRegistry()`` would then silently discard the one
        it was handed. That exact trap cost a real bug in 5.1.
        """
        return True

    def record(self, plan: ActionPlan, choice: RungChoice, *, applied_at: datetime) -> None:
        """Remember an effect that is now standing, keyed by the effect itself."""
        self._applied += 1
        self._standing[plan.idempotency_key] = StandingRestraint(
            plan=plan, choice=choice, applied_at=applied_at, sequence=self._applied
        )

    def release(self, plan: ActionPlan) -> None:
        """Forget an effect that has been given back."""
        self._standing.pop(plan.idempotency_key, None)

    def standing(self) -> tuple[StandingRestraint, ...]:
        """Everything in force, in the order it was applied.

        Ordered by ``order`` rather than by ``applied_at`` so that reversing this
        sequence really is the order to undo it in. Sorting on the timestamp
        alone leaves effects from one tick tied, and a stable sort then returns
        ties in the order they were applied - so a caller reversing the result
        would unwind a canary forwards and leave the widest share it ever reached
        still in place.
        """
        return tuple(sorted(self._standing.values(), key=lambda entry: entry.order))

    def for_incident(self, incident_id: str) -> tuple[StandingRestraint, ...]:
        """Everything currently in force because of one incident, oldest first.

        The question the remediation loop asks before it acts: an incident this
        platform has already answered, and whose answer has not yet been given
        back, is not one to answer a second time.
        """
        return tuple(entry for entry in self.standing() if entry.plan.incident_id == incident_id)

    def expired(self, now: datetime) -> tuple[StandingRestraint, ...]:
        """Everything that has outlived its rung's TTL, oldest first.

        Half-open on the far side: an effect exactly at its expiry has not yet
        outlived it, so a TTL of 900 s means 900 s of restraint rather than 899.
        """
        return tuple(entry for entry in self.standing() if entry.expires_at < now)
