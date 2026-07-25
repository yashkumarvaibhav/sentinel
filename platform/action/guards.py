"""Blast-radius guards, and the canary that widens an action instead of firing it.

The ladder said which rung. The guards here are the last thing between that
choice and production, and they answer two different questions:

* **Who does this land on?** A protected cohort is one an operator has said the
  platform may not harm, and `config/cohorts.yml` states how much blast radius
  each is allowed to absorb. `checkout-users` ships at **0.0** - the conversion
  path, where being wrong costs revenue rather than latency - so an action
  restraining it is refused here rather than in the adapter. That placement is
  deliberate: an adapter's job is to carry out an effect correctly, and a
  vocabulary of "correct" that also encoded who deserves protection would have
  to be re-implemented, identically, in every adapter that ever gets written.
* **How much of the world does it disturb?** The rung states a ceiling; the
  adapter states a measurement of its own plan. A plan over its rung's ceiling is
  a plan the operator did not authorise, whatever the ladder chose.

**What passes is recorded, not assumed.** Every gate an action was actually held
to is written onto its outcome (`ActionOutcome.gates_passed`), because "the guard
would have caught it" is not a claim anybody can check afterwards.

**Canary-first is a widening, not a rollout.** The mesh actuator's
`enforced_percent` exists precisely so a restraint can be applied to a small
share, the protected signals looked at, and the share raised only if nothing
else broke. The collateral check is a seam (`CollateralProbe`) rather than a
metric query, because what counts as collateral is an SLO question that 5.7 owns
- and a canary that widened on an assumption would be worse than no canary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from action.actuators.base import ActionRejectedError, Actuator
from action.executor import ActionExecutor
from action.ladder import RungChoice
from common.config import CohortConfig, CohortDefinition
from contracts import ActionOutcome, ActionPlan, ActionStatus, Decision

# The parameter naming the cohort an effect lands on. One name, because a plan
# that called it something else would slip past the protection silently.
COHORT_PARAMETER = "cohort"

BLAST_CAP_GATE = "blast-radius-within-the-rung-ceiling"
PROTECTED_COHORT_GATE = "protected-cohort-unharmed"
CANARY_GATE = "canary-widened-on-clean-collateral"


class GuardRefusedError(ActionRejectedError):
    """The action is not one this platform will carry out against this target."""


class ProtectedCohortError(GuardRefusedError):
    """The effect lands on a cohort an operator has protected."""


class BlastRadiusExceededError(GuardRefusedError):
    """The plan disturbs more than the rung that chose it authorised."""


@dataclass(frozen=True, slots=True)
class CollateralReport:
    """Whether anything that should not have been affected was.

    ``clean`` is the only field a caller acts on. The rest exists so a refusal
    can say *what* was harmed - a canary that stops without naming the signal it
    stopped on is an outage nobody can diagnose.
    """

    clean: bool
    detail: str
    harmed: tuple[str, ...] = ()


class CollateralProbe(Protocol):
    """Asks whether an action in force has harmed anything it should not have.

    Deliberately a seam. What counts as collateral is an SLO question, and the
    SLO-anchored version lands with 5.7's verified rollback; wiring a guess in
    here would make every canary widen on an assumption.
    """

    def __call__(self, plan: ActionPlan, *, ts: datetime) -> CollateralReport:
        """Report on the protected signals while this plan is in force."""


class BlastRadiusGuard:
    """Refuses an action that lands where it may not, or reaches further than allowed."""

    __slots__ = ("_cohorts",)

    def __init__(self, cohorts: CohortConfig) -> None:
        self._cohorts = {cohort.cohort_id: cohort for cohort in cohorts.cohorts}

    def check(self, plan: ActionPlan, choice: RungChoice) -> tuple[str, ...]:
        """Hold one plan to every gate, returning the gates it actually passed.

        Raises rather than returning a verdict: a guard whose result a caller
        could forget to read is decoration. The gates it returns are the ones to
        record on the outcome.

        The cohort is checked FIRST on purpose. "This lands on people we said we
        would not touch" and "the ladder chose a rung this plan outgrew" are
        different failures with different fixes, and an operator reading the
        second when the first is also true would fix the wrong one.
        """
        passed = [self._check_cohort(plan), self._check_blast_cap(plan, choice)]
        return tuple(gate for gate in passed if gate is not None)

    def _check_blast_cap(self, plan: ActionPlan, choice: RungChoice) -> str:
        if not choice.permits(plan):
            raise BlastRadiusExceededError(
                f"{plan.plan_id} disturbs {plan.estimated_blast_fraction:.3f} of its scope but "
                f"{choice.rung_id} authorises at most {choice.maximum_blast_fraction:.3f}; the "
                "ladder chose a rung the plan outgrew"
            )
        return BLAST_CAP_GATE

    def _check_cohort(self, plan: ActionPlan) -> str | None:
        """A plan naming no cohort passes no cohort gate, and says so by omission."""
        named = plan.parameters.get(COHORT_PARAMETER)
        if named is None:
            return None
        if not isinstance(named, str) or named not in self._cohorts:
            known = ", ".join(sorted(self._cohorts)) or "none"
            raise ProtectedCohortError(
                f"{plan.plan_id} names cohort {named!r}, which is not one this platform has a "
                f"protection statement for; known cohorts: {known}"
            )
        cohort = self._cohorts[named]
        allowance = cohort.max_blast_radius_pct / 100.0
        if plan.estimated_blast_fraction > allowance:
            raise ProtectedCohortError(_refusal(plan, cohort, allowance))
        return PROTECTED_COHORT_GATE


def _refusal(plan: ActionPlan, cohort: CohortDefinition, allowance: float) -> str:
    protection = "protected" if cohort.protected else "capped"
    return (
        f"{plan.plan_id} would disturb {plan.estimated_blast_fraction:.3f} of {cohort.cohort_id}, "
        f"a {protection} cohort allowed at most {allowance:.3f} "
        f"({cohort.description})"
    )


@dataclass(frozen=True, slots=True)
class CanaryStep:
    """One widening of an effect, and what it was allowed to reach."""

    share: int
    plan: ActionPlan
    outcome: ActionOutcome
    collateral: CollateralReport


@dataclass(frozen=True, slots=True)
class CanaryResult:
    """How far a canary got, and why it stopped there.

    ``widened`` is the share actually reached, which is not always the one the
    rung asked for - that is the point of running one.
    """

    steps: tuple[CanaryStep, ...]
    widened: int
    completed: bool
    stopped_because: str | None = None

    @property
    def final(self) -> ActionOutcome:
        """The outcome of the last step that ran."""
        return self.steps[-1].outcome


class CanaryRollout:
    """Applies an effect at a small share first, and widens only on clean collateral.

    Each step is a *separate effect* with its own idempotency key, because
    `enforced_percent: 10` and `enforced_percent: 100` genuinely are different
    states of the world. That means every step is independently deduplicated,
    independently verifiable and independently revertible - and it is why the
    dial had to be an absolute parameter rather than a relative one.
    """

    __slots__ = ("_executor", "_guard", "_probe")

    def __init__(
        self,
        *,
        executor: ActionExecutor,
        guard: BlastRadiusGuard,
        probe: CollateralProbe,
    ) -> None:
        self._executor = executor
        self._guard = guard
        self._probe = probe

    def apply(
        self,
        *,
        actuator: Actuator,
        decision: Decision,
        choice: RungChoice,
        ts: datetime,
        owner: str,
        approvals: Sequence[str] = (),
    ) -> CanaryResult:
        """Walk the rung's shares upward, stopping the moment anything else is harmed."""
        if choice.canary_parameter is None or not choice.canary_shares:
            raise ValueError(
                f"{choice.rung_id} has no dial to widen; a canary needs a parameter whose value "
                "is a share of the effect"
            )
        steps: list[CanaryStep] = []
        for share in choice.canary_shares:
            plan = actuator.plan(
                decision,
                action_kind=choice.action_kind,
                parameters={**choice.parameters, choice.canary_parameter: share},
                ts=ts,
            )
            gates = self._guard.check(plan, choice)
            outcome = self._executor.apply(plan, ts=ts, owner=owner, approvals=approvals)
            report = self._probe(plan, ts=ts)
            passed = (*gates, CANARY_GATE) if report.clean else gates
            steps.append(
                CanaryStep(
                    share=share,
                    plan=plan,
                    outcome=with_gates(outcome, passed),
                    collateral=report,
                )
            )
            if not report.clean:
                return CanaryResult(
                    steps=tuple(steps),
                    widened=share,
                    completed=False,
                    stopped_because=report.detail,
                )
            if outcome.status is ActionStatus.FAILED:
                return CanaryResult(
                    steps=tuple(steps),
                    widened=share,
                    completed=False,
                    stopped_because=f"the step at {share}% reported {outcome.status.value}",
                )
        return CanaryResult(steps=tuple(steps), widened=choice.canary_shares[-1], completed=True)


def with_gates(outcome: ActionOutcome, gates: Sequence[str]) -> ActionOutcome:
    """Record on the outcome the gates this action was actually held to.

    Re-validated rather than mutated, for the same reason approvals are: a
    contract bypassed on the way into the audit trail is not a contract.
    """
    if not gates:
        return outcome
    return ActionOutcome.model_validate(
        {**outcome.model_dump(), "gates_passed": tuple(dict.fromkeys(gates))}
    )


def guarded_apply(
    *,
    executor: ActionExecutor,
    guard: BlastRadiusGuard,
    plan: ActionPlan,
    choice: RungChoice,
    ts: datetime,
    owner: str,
    approvals: Sequence[str] = (),
) -> ActionOutcome:
    """Apply one plan only if it clears every gate, recording the ones it cleared."""
    gates = guard.check(plan, choice)
    return with_gates(executor.apply(plan, ts=ts, owner=owner, approvals=approvals), gates)
