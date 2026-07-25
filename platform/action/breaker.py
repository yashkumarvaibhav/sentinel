"""The remediation circuit breaker: stop, and fetch a person.

Every other safety mechanism in this plane asks "is this action allowed?". This
one asks a different and more uncomfortable question: **is the platform itself
still helping?** An autonomous remediator that is wrong is not dangerous because
any single action is dangerous - each one passed a policy gate, a ladder, a
blast-radius guard and an approval rule. It is dangerous because it will do the
same reasonable thing again, and again, at machine speed.

So there are two ways to trip, and the second one is the one that matters:

* **Rate.** More than ``maximum_actions`` in ``window_seconds`` is a runaway
  whatever each action claimed to be for.
* **Ineffectiveness.** ``ineffective_streak`` consecutive actions against the
  same service that did not move its signal by ``minimum_improvement``. A
  platform acting repeatedly without improving anything has a wrong model of the
  problem, and the honest response to a wrong model is to stop using it.

A tripped breaker is not a failure state to be retried past. It **requires a
person**, and says so in the state it returns, because the situation it detects
is exactly the one where more automation is the wrong answer.

Effectiveness is *reported in*, never measured here. What "improved" means is an
SLO question that ``action/rollback.py`` answers against real readings; a breaker
that inferred it from its own actions would be marking its own homework.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from action.actuators.base import ActionRejectedError
from action.config import BreakerConfig
from contracts import ActionPlan


class BreakerOpenError(ActionRejectedError):
    """The platform has stopped acting on its own and is waiting for a person."""


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """One autonomous action, and whether it turned out to help."""

    ts: datetime
    plan_id: str
    target_service: str
    # None means "not yet known", which is different from "did not help" and is
    # deliberately not counted against the streak: an action whose effect has
    # not been measured has not failed to have one.
    improvement: float | None = None


@dataclass(frozen=True, slots=True)
class BreakerState:
    """Whether the platform may keep acting, and what a person needs to be told."""

    open: bool
    reason: str | None = None
    requires_page: bool = False
    recent_actions: int = 0

    @property
    def closed(self) -> bool:
        return not self.open


class RemediationBreaker:
    """Counts what the platform has done lately, and whether any of it worked."""

    __slots__ = ("_configuration", "_records")

    def __init__(self, configuration: BreakerConfig) -> None:
        self._configuration = configuration
        self._records: list[ActionRecord] = []

    def record(
        self,
        plan: ActionPlan,
        *,
        ts: datetime,
        improvement: float | None = None,
    ) -> None:
        """Remember an action the platform took, and how much good it did."""
        self._records.append(
            ActionRecord(
                ts=ts,
                plan_id=plan.plan_id,
                target_service=plan.target_service,
                improvement=improvement,
            )
        )

    def observe(self, plan: ActionPlan, *, improvement: float) -> None:
        """Fill in how much good an already-recorded action did.

        Separate from ``record`` because the two facts are known at different
        times: an action is taken now and judged later, and pretending otherwise
        would make every action look unmeasured for as long as it mattered.
        """
        for index, entry in enumerate(self._records):
            if entry.plan_id == plan.plan_id:
                self._records[index] = ActionRecord(
                    ts=entry.ts,
                    plan_id=entry.plan_id,
                    target_service=entry.target_service,
                    improvement=improvement,
                )
                return
        raise KeyError(f"{plan.plan_id} was never recorded, so there is nothing to judge")

    def state(self, ts: datetime) -> BreakerState:
        """Whether the platform may act right now, and why not if it may not."""
        recent = self._within_window(ts)
        if len(recent) >= self._configuration.maximum_actions:
            return BreakerState(
                open=True,
                reason=(
                    f"{len(recent)} autonomous actions in the last "
                    f"{self._configuration.window_seconds:.0f}s reached the limit of "
                    f"{self._configuration.maximum_actions}; acting faster than anyone can "
                    "read is a runaway whatever each action was for"
                ),
                requires_page=True,
                recent_actions=len(recent),
            )
        ineffective = self._ineffective_streak(recent)
        if ineffective is not None:
            service, streak = ineffective
            return BreakerState(
                open=True,
                reason=(
                    f"{streak} consecutive actions on {service} moved its signal by less than "
                    f"{self._configuration.minimum_improvement:.3f}; a platform acting without "
                    "improving anything has the wrong model of the problem"
                ),
                requires_page=True,
                recent_actions=len(recent),
            )
        return BreakerState(open=False, recent_actions=len(recent))

    def check(self, ts: datetime) -> BreakerState:
        """Raise if the platform has stopped being allowed to act on its own."""
        state = self.state(ts)
        if state.open:
            raise BreakerOpenError(state.reason or "the remediation breaker is open")
        return state

    def _within_window(self, ts: datetime) -> list[ActionRecord]:
        """Half-open, so an action exactly at the horizon has already aged out."""
        horizon = ts - timedelta(seconds=self._configuration.window_seconds)
        return [entry for entry in self._records if entry.ts > horizon]

    def _ineffective_streak(self, recent: Sequence[ActionRecord]) -> tuple[str, int] | None:
        """The service whose last N measured actions all failed to help, if any."""
        needed = self._configuration.ineffective_streak
        by_service: dict[str, list[ActionRecord]] = {}
        for entry in recent:
            if entry.improvement is not None:
                by_service.setdefault(entry.target_service, []).append(entry)
        for service, entries in sorted(by_service.items()):
            tail = entries[-needed:]
            if len(tail) < needed:
                continue
            if all(
                entry.improvement is not None
                and entry.improvement < self._configuration.minimum_improvement
                for entry in tail
            ):
                return service, len(tail)
        return None
