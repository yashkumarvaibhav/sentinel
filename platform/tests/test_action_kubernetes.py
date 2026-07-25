"""The Kubernetes actuator, against a recorded cluster and against a real one.

The unit tests drive a fake ``ClusterCommand`` so they run in hosted CI with no
cluster at all. The integration test at the bottom talks to the real testbed and
is gated behind an environment variable, so a machine without the lab skips it
rather than failing.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from action import (
    ActionConfig,
    ActionExecutor,
    ActionRejectedError,
    KubernetesActuator,
    KubernetesConfig,
    load_action_config,
)
from action.actuators.kubernetes import KubectlCommand
from contracts import (
    ActionKind,
    ActionStatus,
    ActuatorKind,
    Decision,
    DecisionAction,
    IncidentSeverity,
    VerdictClass,
)

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "action.yml"
INTEGRATION_ENV = "SENTINEL_ACTION_K8S_INTEGRATION"


@dataclass
class FakeCluster:
    """A recorded cluster: canned answers in, the argument vectors it saw out."""

    answers: dict[str, str] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, timeout: int = 60) -> str:
        self.calls.append(list(argv))
        for marker, answer in self.answers.items():
            if marker in " ".join(argv):
                return answer
        return ""

    def mutating_calls(self) -> list[list[str]]:
        """Every call that was not a read, which is what safety tests care about."""
        return [call for call in self.calls if not _is_read(call)]


def _is_read(argv: Sequence[str]) -> bool:
    return "get" in argv or "--dry-run=server" in argv


def _kubernetes_config(**overrides: Any) -> KubernetesConfig:
    fields: dict[str, Any] = {
        "context": "k3d-sentinel-lab",
        "namespace": "otel-demo",
        "maximum_replicas": 6,
        "workloads": {"payment": "payment", "frontend": "frontend"},
    }
    fields.update(overrides)
    return KubernetesConfig.model_validate(fields)


def _decision(target: str = "payment") -> Decision:
    return Decision(
        decision_id="decision-1",
        ts=TICK,
        incident_id="incident-1",
        action=DecisionAction.ACT,
        rule_id="remediate-a-verified-fault",
        reason="a confirmed fault with a named origin",
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


def _actuator(cluster: FakeCluster | None = None, **overrides: Any) -> KubernetesActuator:
    return KubernetesActuator(
        configuration=_kubernetes_config(**overrides),
        command=cluster or FakeCluster(),
    )


# --- planning ---------------------------------------------------------------


def test_a_service_with_no_mapped_workload_is_refused_not_guessed() -> None:
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match="no workload is mapped for service 'cart'"):
        actuator.plan(
            _decision("cart"), action_kind=ActionKind.SCALE, parameters={"replicas": 2}, ts=TICK
        )


def test_a_rung_this_adapter_does_not_carry_out_is_refused() -> None:
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match="does not carry out RATE_LIMIT"):
        actuator.plan(_decision(), action_kind=ActionKind.RATE_LIMIT, ts=TICK)


def test_scale_demands_an_absolute_replica_count() -> None:
    """A relative change cannot be deduplicated, so it is refused at plan time."""
    actuator = _actuator()
    for parameters in ({"replicas_delta": 2}, {"replicas": "6"}, {"replicas": True}, {}):
        with pytest.raises(ActionRejectedError):
            actuator.plan(_decision(), action_kind=ActionKind.SCALE, parameters=parameters, ts=TICK)


def test_a_replica_count_beyond_the_ceiling_is_refused_at_plan_time() -> None:
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match=r"outside the configured 0\.\.6 range"):
        actuator.plan(
            _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 99}, ts=TICK
        )


def test_a_plan_names_a_real_workload_and_what_to_expect() -> None:
    actuator = _actuator()
    plan = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 3}, ts=TICK
    )
    assert plan.target_ref == "otel-demo/deployment/payment"
    assert plan.target_service == "payment"
    assert plan.honesty == "REAL"
    assert plan.reversible
    assert "ready replicas" in plan.expected_effect


def test_isolating_claims_the_whole_blast_radius() -> None:
    """Cordoning a node affects everything scheduled there, not one workload."""
    actuator = _actuator()
    isolate = actuator.plan(_decision(), action_kind=ActionKind.ISOLATE, ts=TICK)
    scale = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 3}, ts=TICK
    )
    assert isolate.estimated_blast_fraction == 1.0
    assert scale.estimated_blast_fraction < 1.0
    assert isolate.requires_human_approval, "ISOLATE is destructive whatever it targets"


# --- carrying out -----------------------------------------------------------


def test_a_simulation_touches_nothing() -> None:
    cluster = FakeCluster(answers={"--dry-run=server": "deployment.apps/payment scaled (dry run)"})
    actuator = _actuator(cluster)
    plan = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 3}, ts=TICK
    )
    outcome = actuator.simulate(plan, ts=TICK)
    assert outcome.status is ActionStatus.SIMULATED
    assert outcome.dry_run
    assert cluster.mutating_calls() == [], "a simulation issued a mutating command"
    assert all("--dry-run=server" in call for call in cluster.calls)


def test_scaling_reads_the_previous_count_before_it_writes() -> None:
    """The count we scale from is only knowable before the write, and revert needs it."""
    cluster = FakeCluster(answers={"jsonpath={.spec.replicas}": "2"})
    actuator = _actuator(cluster)
    plan = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 5}, ts=TICK
    )
    applied = actuator.apply(plan, ts=TICK)
    assert applied.status is ActionStatus.APPLIED
    assert applied.revert_token == "replicas=2"
    assert cluster.mutating_calls() == [
        ["-n", "otel-demo", "scale", "deployment/payment", "--replicas=5"]
    ]


def test_reverting_a_scale_restores_the_count_it_was_applied_over() -> None:
    cluster = FakeCluster()
    actuator = _actuator(cluster)
    plan = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 5}, ts=TICK
    )
    actuator.revert(plan, ts=TICK, revert_token="replicas=2")
    assert cluster.mutating_calls() == [
        ["-n", "otel-demo", "scale", "deployment/payment", "--replicas=2"]
    ]


def test_reverting_without_the_token_is_refused_rather_than_guessed() -> None:
    cluster = FakeCluster()
    actuator = _actuator(cluster)
    plan = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 5}, ts=TICK
    )
    with pytest.raises(ActionRejectedError, match="cannot be put back without the token"):
        actuator.revert(plan, ts=TICK)
    assert cluster.calls == []


def test_a_token_minted_for_another_rung_is_refused() -> None:
    actuator = _actuator()
    plan = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 5}, ts=TICK
    )
    with pytest.raises(ActionRejectedError, match="does not record a replicas"):
        actuator.revert(plan, ts=TICK, revert_token="revision=4")


def test_verify_asks_the_cluster_rather_than_trusting_the_apply() -> None:
    cluster = FakeCluster(answers={"jsonpath={.status.readyReplicas}": "5"})
    actuator = _actuator(cluster)
    plan = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 5}, ts=TICK
    )
    assert actuator.verify(plan, ts=TICK).status is ActionStatus.VERIFIED
    drifted = FakeCluster(answers={"jsonpath={.status.readyReplicas}": "1"})
    assert _actuator(drifted).verify(plan, ts=TICK).status is ActionStatus.FAILED


def test_a_rollback_records_the_revision_it_rolled_off() -> None:
    cluster = FakeCluster(answers={"revision": "7"})
    actuator = _actuator(cluster)
    plan = actuator.plan(_decision(), action_kind=ActionKind.ROLLBACK, ts=TICK)
    applied = actuator.apply(plan, ts=TICK)
    assert applied.revert_token == "revision=7"
    actuator.revert(plan, ts=TICK, revert_token="revision=7")
    assert cluster.mutating_calls()[-1] == [
        "-n",
        "otel-demo",
        "rollout",
        "undo",
        "deployment/payment",
        "--to-revision=7",
    ]


def test_isolating_cordons_exactly_the_nodes_the_workload_runs_on() -> None:
    cluster = FakeCluster(answers={"get pods": "k3d-node-1 k3d-node-0 k3d-node-1"})
    actuator = _actuator(cluster)
    plan = actuator.plan(_decision(), action_kind=ActionKind.ISOLATE, ts=TICK)
    applied = actuator.apply(plan, ts=TICK)
    assert applied.revert_token == "nodes=k3d-node-0,k3d-node-1"
    assert cluster.mutating_calls() == [["cordon", "k3d-node-0"], ["cordon", "k3d-node-1"]]
    actuator.revert(plan, ts=TICK, revert_token="nodes=k3d-node-0,k3d-node-1")
    assert cluster.mutating_calls()[-2:] == [
        ["uncordon", "k3d-node-0"],
        ["uncordon", "k3d-node-1"],
    ]


def test_isolating_a_workload_we_cannot_place_is_refused() -> None:
    cluster = FakeCluster()
    actuator = _actuator(cluster)
    plan = actuator.plan(_decision(), action_kind=ActionKind.ISOLATE, ts=TICK)
    with pytest.raises(ActionRejectedError, match="nothing to cordon"):
        actuator.apply(plan, ts=TICK)
    assert cluster.mutating_calls() == []


def test_a_plan_aimed_outside_the_mapped_workloads_is_refused() -> None:
    """A plan is data; the adapter re-checks its target before touching anything."""
    cluster = FakeCluster()
    actuator = _actuator(cluster)
    honest = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 2}, ts=TICK
    )
    forged = honest.model_copy(update={"target_ref": "otel-demo/deployment/kafka"})
    with pytest.raises(ActionRejectedError, match="not a mapped workload"):
        actuator.apply(forged, ts=TICK)
    foreign = honest.model_copy(update={"target_ref": "kube-system/deployment/payment"})
    with pytest.raises(ActionRejectedError, match="not a workload in otel-demo"):
        actuator.apply(foreign, ts=TICK)
    assert cluster.calls == []


# --- configuration ----------------------------------------------------------


def test_the_committed_config_maps_only_services_the_topology_knows() -> None:
    from common.config import load_config

    configuration = load_action_config(CONFIG_PATH)
    assert configuration.kubernetes is not None
    known = {service.service for service in load_config(CONFIG_PATH.parent).topology.services}
    assert set(configuration.kubernetes.workloads) <= known


def test_enabling_the_adapter_without_a_cluster_section_is_refused() -> None:
    with pytest.raises(ValueError, match="no `kubernetes:` section"):
        ActionConfig.model_validate(
            {
                "version": 1,
                "execution": {
                    "dry_run": True,
                    "lease_ttl_seconds": 120.0,
                    "journal_capacity": 16,
                },
                "actuators": [{"actuator": "KUBERNETES", "enabled": True}],
            }
        )


def test_a_workload_name_kubernetes_would_reject_is_refused() -> None:
    with pytest.raises(ValueError, match="not valid Kubernetes object names"):
        _kubernetes_config(workloads={"payment": "Payment_Service"})


# --- the real testbed -------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get(INTEGRATION_ENV) != "1",
    reason=f"set {INTEGRATION_ENV}=1 with the testbed up to run this",
)
def test_against_the_real_testbed_in_dry_run() -> None:
    """Plan, simulate and verify against the live cluster - and change nothing.

    Deliberately no apply: this asserts the read and admission paths work
    against a real API server, which is what could silently be wrong. Mutating
    the shared testbed from a unit-test run is not something a green suite
    should ever do.
    """
    configuration = load_action_config(CONFIG_PATH)
    assert configuration.kubernetes is not None
    cluster = KubectlCommand(context=configuration.kubernetes.context)
    actuator = KubernetesActuator(configuration=configuration.kubernetes, command=cluster)

    plan = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 1}, ts=TICK
    )
    simulated = actuator.simulate(plan, ts=TICK)
    assert simulated.status is ActionStatus.SIMULATED
    assert simulated.observed, "the API server said nothing about admitting the scale"

    # The testbed runs one replica of each service, so verifying replicas=1
    # against the untouched cluster must pass on evidence rather than by luck.
    verified = actuator.verify(plan, ts=TICK)
    assert verified.status is ActionStatus.VERIFIED, verified.detail
    assert "1 ready replicas" in verified.detail

    isolate = actuator.plan(_decision(), action_kind=ActionKind.ISOLATE, ts=TICK)
    nodes = actuator.simulate(isolate, ts=TICK)
    assert "would cordon" in nodes.observed[0]


@pytest.mark.skipif(
    os.environ.get(INTEGRATION_ENV) != "1",
    reason=f"set {INTEGRATION_ENV}=1 with the testbed up to run this",
)
def test_the_executor_stays_in_dry_run_against_the_real_testbed() -> None:
    """The committed posture is dry-run, and it holds all the way to a real cluster."""
    configuration = load_action_config(CONFIG_PATH)
    assert configuration.kubernetes is not None
    assert configuration.execution.dry_run, "config/action.yml must ship dry-run"
    actuator = KubernetesActuator(
        configuration=configuration.kubernetes,
        command=KubectlCommand(context=configuration.kubernetes.context),
    )
    executor = ActionExecutor(actuators=[actuator], configuration=configuration, dry_run=True)
    plan = actuator.plan(
        _decision(), action_kind=ActionKind.SCALE, parameters={"replicas": 4}, ts=TICK
    )
    outcome = executor.apply(plan, ts=TICK + timedelta(seconds=1), owner="integration-test")
    assert outcome.status is ActionStatus.SIMULATED
    assert executor.journal.entries() == ()
    assert actuator.kind is ActuatorKind.KUBERNETES
