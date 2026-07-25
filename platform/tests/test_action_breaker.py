"""The circuit breaker and the SLO-anchored rollback.

Both halves answer a question the rest of the plane does not: not "is this
action allowed" but "is the platform still helping". The committed
`config/action.yml` breaker settings and `config/slo.yml` targets are used
wherever the question is what this deployment actually does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from action import (
    ActionExecutor,
    BreakerOpenError,
    RemediationBreaker,
    SloCollateralProbe,
    SloReading,
    VerifiedRollback,
    load_action_config,
)
from action.actuators import SimulatedActuator
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

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
LATER = TICK + timedelta(minutes=5)


def _decision(target: str = "checkout") -> Decision:
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
        verdict_class=VerdictClass.OPERATIONAL_FAULT,
        verdict_id="verdict-1",
        confidence=0.9,
        target_service=target,
    )


def _breaker() -> RemediationBreaker:
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    assert configuration.breaker is not None
    return RemediationBreaker(configuration.breaker)


def _plan(adapter: SimulatedActuator, replicas: int, *, target: str = "checkout") -> ActionPlan:
    return adapter.plan(
        _decision(target),
        action_kind=ActionKind.SCALE,
        parameters={"replicas": replicas},
        ts=TICK,
    )


# --- the breaker ------------------------------------------------------------


def test_a_quiet_platform_may_keep_acting() -> None:
    assert _breaker().state(TICK).closed


def test_acting_faster_than_anyone_can_read_is_a_runaway() -> None:
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    assert configuration.breaker is not None
    breaker = _breaker()
    adapter = SimulatedActuator()

    for index in range(configuration.breaker.maximum_actions):
        breaker.record(_plan(adapter, index + 1), ts=TICK + timedelta(seconds=index))

    state = breaker.state(TICK + timedelta(seconds=10))
    assert state.open
    assert state.requires_page, "a runaway that pages nobody is a runaway"
    assert state.reason is not None
    assert "runaway" in state.reason
    with pytest.raises(BreakerOpenError):
        breaker.check(TICK + timedelta(seconds=10))


def test_actions_old_enough_to_have_aged_out_do_not_count() -> None:
    """Half-open, so an action exactly at the horizon has already aged out."""
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    assert configuration.breaker is not None
    window = timedelta(seconds=configuration.breaker.window_seconds)
    breaker = _breaker()
    adapter = SimulatedActuator()

    for index in range(configuration.breaker.maximum_actions):
        breaker.record(_plan(adapter, index + 1), ts=TICK + timedelta(seconds=index))

    # One microsecond before the oldest action reaches the horizon, all of them
    # are still inside the window and the breaker is open.
    assert breaker.state(TICK + window - timedelta(microseconds=1)).open

    # Exactly at the horizon the oldest has aged out - half-open on the far side
    # - so the count drops below the limit and the platform may act again.
    assert breaker.state(TICK + window).closed
    later = TICK + window + timedelta(seconds=configuration.breaker.maximum_actions)
    assert breaker.state(later).closed


def test_acting_repeatedly_without_helping_trips_the_breaker() -> None:
    """The condition that matters: a wrong model, not a fast one."""
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    assert configuration.breaker is not None
    breaker = _breaker()
    adapter = SimulatedActuator()

    for index in range(configuration.breaker.ineffective_streak):
        plan = _plan(adapter, index + 1)
        breaker.record(plan, ts=TICK + timedelta(seconds=index))
        breaker.observe(plan, improvement=0.0)

    state = breaker.state(TICK + timedelta(seconds=10))
    assert state.open
    assert state.requires_page
    assert state.reason is not None
    assert "wrong model of the problem" in state.reason


def test_an_action_that_helped_breaks_the_streak() -> None:
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    assert configuration.breaker is not None
    breaker = _breaker()
    adapter = SimulatedActuator()
    improvements = [0.0] * (configuration.breaker.ineffective_streak - 1) + [0.5]

    for index, improvement in enumerate(improvements):
        plan = _plan(adapter, index + 1)
        breaker.record(plan, ts=TICK + timedelta(seconds=index))
        breaker.observe(plan, improvement=improvement)

    assert breaker.state(TICK + timedelta(seconds=10)).closed


def test_an_unmeasured_action_has_not_failed_to_help() -> None:
    """ "Not yet known" and "did not help" must not be the same value."""
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    assert configuration.breaker is not None
    breaker = _breaker()
    adapter = SimulatedActuator()

    for index in range(configuration.breaker.ineffective_streak):
        breaker.record(_plan(adapter, index + 1), ts=TICK + timedelta(seconds=index))

    assert breaker.state(TICK + timedelta(seconds=10)).closed


def test_futility_is_counted_per_service_not_across_the_mesh() -> None:
    """Three failures on three different services is bad luck, not a wrong model."""
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    assert configuration.breaker is not None
    breaker = _breaker()
    adapter = SimulatedActuator()

    for index, service in enumerate(("checkout", "frontend", "cart")):
        plan = _plan(adapter, index + 1, target=service)
        breaker.record(plan, ts=TICK + timedelta(seconds=index))
        breaker.observe(plan, improvement=0.0)

    assert breaker.state(TICK + timedelta(seconds=10)).closed


def test_judging_an_action_nobody_recorded_is_refused() -> None:
    breaker = _breaker()
    with pytest.raises(KeyError, match="was never recorded"):
        breaker.observe(_plan(SimulatedActuator(), 2), improvement=1.0)


# --- the SLO-anchored rollback ----------------------------------------------


@dataclass
class ScriptedSlos:
    """SLO readings on a schedule, so a recovery is measured rather than assumed."""

    before: Mapping[str, SloReading] = field(default_factory=dict)
    after: Mapping[str, SloReading] = field(default_factory=dict)
    switch_at: datetime = LATER

    def __call__(self, service: str, *, ts: datetime) -> SloReading | None:
        table = self.after if ts >= self.switch_at else self.before
        return table.get(service)


def _healthy(service: str) -> SloReading:
    return SloReading(service=service, availability=0.9999, latency_p95_ms=100.0)


def _suffering(service: str) -> SloReading:
    return SloReading(service=service, availability=0.90, latency_p95_ms=100.0)


def _rollback(reader: ScriptedSlos) -> tuple[VerifiedRollback, SimulatedActuator, ActionExecutor]:
    configuration = load_action_config(CONFIG_DIR / "action.yml")
    adapter = SimulatedActuator()
    executor = ActionExecutor(actuators=[adapter], configuration=configuration, dry_run=False)
    slos = load_config(CONFIG_DIR).slos
    return VerifiedRollback(executor=executor, slos=slos, reader=reader), adapter, executor


def test_an_action_that_harms_nobody_is_left_alone() -> None:
    services = {slo.service for slo in load_config(CONFIG_DIR).slos.slos}
    reader = ScriptedSlos(before={service: _healthy(service) for service in services})
    rollback, adapter, executor = _rollback(reader)
    plan = _plan(adapter, 3)
    executor.apply(plan, ts=TICK, owner="test")

    result = rollback.rollback_if_harmed(plan, ts=TICK, owner="test")
    assert not result.reverted
    assert result.harmed == ()
    assert result.users_restored is None


def test_collateral_triggers_a_rollback_and_the_recovery_is_measured() -> None:
    services = {slo.service for slo in load_config(CONFIG_DIR).slos.slos}
    reader = ScriptedSlos(
        before={service: _healthy(service) for service in services}
        | {"checkout": _suffering("checkout")},
        after={service: _healthy(service) for service in services},
    )
    rollback, adapter, executor = _rollback(reader)
    plan = _plan(adapter, 3)
    executor.apply(plan, ts=TICK, owner="test")

    result = rollback.rollback_if_harmed(plan, ts=TICK, owner="test", settled_at=LATER)
    assert result.reverted
    assert result.harmed == ("checkout",)
    assert result.outcome is not None
    assert result.outcome.status is ActionStatus.REVERTED
    assert result.users_restored is not None
    assert result.users_restored == pytest.approx(0.9999 - 0.90)
    assert "recovered by +0.0999" in result.detail


def test_a_recovery_that_could_not_be_measured_is_not_claimed() -> None:
    """An unmeasured recovery is not a smaller recovery; it is one nobody may quote."""
    services = {slo.service for slo in load_config(CONFIG_DIR).slos.slos}
    reader = ScriptedSlos(
        before={service: _healthy(service) for service in services}
        | {"checkout": _suffering("checkout")},
        after={},
    )
    rollback, adapter, executor = _rollback(reader)
    plan = _plan(adapter, 3)
    executor.apply(plan, ts=TICK, owner="test")

    result = rollback.rollback_if_harmed(plan, ts=TICK, owner="test", settled_at=LATER)
    assert result.reverted, "the undo is the safe direction even unmeasured"
    assert result.users_restored is None
    assert "could not be measured, so none is claimed" in result.detail


def test_a_service_we_cannot_see_is_not_a_service_we_know_is_fine() -> None:
    """The probe must not report clean because the telemetry was down."""
    slos = load_config(CONFIG_DIR).slos
    reader = ScriptedSlos(before={"frontend": _healthy("frontend")})
    probe = SloCollateralProbe(slos=slos, reader=reader)
    plan = _plan(SimulatedActuator(), 3)

    report = probe(plan, ts=TICK)
    assert not report.clean
    assert "could not read the SLO position of checkout" in report.detail
    assert report.harmed == ("checkout",)


def test_latency_past_its_target_is_collateral_just_like_availability() -> None:
    slos = load_config(CONFIG_DIR).slos
    services = {slo.service for slo in slos.slos}
    slow = SloReading(service="frontend", availability=0.9999, latency_p95_ms=5_000.0)
    reader = ScriptedSlos(
        before={service: _healthy(service) for service in services} | {"frontend": slow}
    )

    report = SloCollateralProbe(slos=slos, reader=reader)(_plan(SimulatedActuator(), 3), ts=TICK)
    assert not report.clean
    assert report.harmed == ("frontend",)


def test_the_probe_watches_the_services_slo_yml_names() -> None:
    """Collateral is by definition what happens where the action was not aimed."""
    slos = load_config(CONFIG_DIR).slos
    assert {slo.service for slo in slos.slos}, "slo.yml names no service to protect"
    reader = ScriptedSlos(before={slo.service: _healthy(slo.service) for slo in slos.slos})

    report = SloCollateralProbe(slos=slos, reader=reader)(
        _plan(SimulatedActuator(), 3, target="email"), ts=TICK
    )
    assert report.clean
