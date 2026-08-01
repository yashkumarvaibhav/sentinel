"""Freeze one live judgement into the plan an operator can act on.

Until now a live judgement produced an incident and nothing else: the always-on
producer published no action control at all, so the durable worker had nothing
it could ever claim. That was deliberate - a plan published before the widening
and rollback machinery existed would have let the worker act before anything
could prove it safe.

This is the seam that lifts it, and it is written to be boring on purpose:

* **Nothing here chooses anything safety-critical.** The rung comes from the
  committed ladder, the target from ``decision.target_service`` - which the
  causal collapse computed from evidence - and the approval requirement from
  the policy gate, widened by the destructive rungs and never narrowed. This
  module's only judgement is *when* to freeze, and the answer is: once, on the
  first revision where the decision both decided to act and was verified.
* **A plan is frozen once per incident and never re-materialized.** The store
  refuses a revision that names different state, so re-publishing a control the
  worker has already advanced would be an error rather than an update. That is
  the right shape: a plan is immutable evidence about what was decided at a
  moment, not a mutable intention.
* **An adapter that refuses to plan produces no control**, rather than a
  half-formed one. A rung whose adapter cannot aim at this target is not an
  action the platform is entitled to offer.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from action.actuators.base import ActionRejectedError, Actuator
from action.config import load_action_config, load_ladder_config
from action.control import materialize_action_control
from action.guards import BlastRadiusGuard
from action.ladder import RemediationLadder
from common.config import SentinelConfig
from contracts import ACTING_ACTIONS, ActionControlSnapshot, ActuatorKind, Decision

LOGGER = logging.getLogger(__name__)

# The first and only revision a live judgement ever freezes. A second plan for
# one incident would be a competing statement about the same evidence, and the
# store is right to refuse it.
LIVE_PLAN_REVISION = 1


class LiveActionPlanner:
    """Turn a verified acting decision into one immutable, guarded control."""

    def __init__(
        self,
        *,
        config: SentinelConfig,
        config_root: Path,
        actuators: Iterable[Actuator],
    ) -> None:
        action_config = load_action_config(config_root / "action.yml")
        enabled = action_config.enabled_actuators()
        registered: dict[ActuatorKind, Actuator] = {}
        for actuator in actuators:
            if actuator.kind not in enabled:
                raise ValueError(
                    f"{actuator.kind.value} is not enabled in the action configuration; "
                    "landing an adapter and permitting it are two separate decisions"
                )
            registered[actuator.kind] = actuator
        self._actuators = registered
        self._ladder = RemediationLadder(
            load_ladder_config(config_root / "ladders.yml"),
            flags=action_config.flags,
        )
        self._guard = BlastRadiusGuard(config.cohorts)

    def plan(self, decision: Decision, *, ts: datetime) -> ActionControlSnapshot | None:
        """The control this decision earns, or ``None`` if it earns none.

        Returning ``None`` rather than raising is the point: most judgements do
        not act, and a planner that treated that as an error would make the
        producer's happy path an exception handler.
        """
        if decision.action not in ACTING_ACTIONS:
            return None
        if decision.target_service is None:
            return None
        try:
            choice = self._ladder.select(decision).primary
        except (ValueError, LookupError):
            LOGGER.exception(
                "no committed ladder rung answers %s; the incident stands with no plan",
                decision.decision_id,
            )
            return None
        actuator = self._actuators.get(choice.actuator)
        if actuator is None:
            LOGGER.warning(
                "rung %s needs the %s adapter, which this producer was not given; "
                "a rung whose adapter is absent is refused rather than substituted",
                choice.rung_id,
                choice.actuator.value,
            )
            return None
        try:
            plan = actuator.plan(
                decision,
                action_kind=choice.action_kind,
                parameters=choice.parameters,
                ts=ts,
            )
        except ActionRejectedError:
            LOGGER.exception(
                "the %s adapter refused to plan %s; no control is published",
                choice.actuator.value,
                choice.rung_id,
            )
            return None
        return materialize_action_control(
            plan_revision=LIVE_PLAN_REVISION,
            choice=choice,
            plan=plan,
            guard=self._guard,
            ts=ts,
        )
