"""The mesh actuator, against a recorded edge and against a real Envoy.

The unit tests drive a fake ``ClusterCommand`` so they run in hosted CI with no
proxy at all. The integration tests at the bottom talk to a real Envoy running
the committed edge template (``make lab-edge``) and are gated behind an
environment variable, so a machine without one skips rather than fails.

That integration is worth having rather than trusting the unit tests, because
the one thing a fake cannot reproduce is the reason ``verify`` exists: Envoy's
admin API answers ``OK`` to a runtime write whose value is not a percentage at
all, and then quietly behaves as if nothing was set.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from action import (
    ActionConfig,
    ActionExecutor,
    ActionRejectedError,
    MeshActuator,
    MeshConfig,
    load_action_config,
)
from action.actuators.mesh import EdgeUnreachableError
from contracts import (
    ActionKind,
    ActionParameterValue,
    ActionStatus,
    ActuatorKind,
    Decision,
    DecisionAction,
    IncidentSeverity,
    VerdictClass,
)

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "action.yml"
INTEGRATION_ENV = "SENTINEL_ACTION_MESH_INTEGRATION"
SANDBOX_ADMIN = "http://127.0.0.1:8049"

POD = "frontend-proxy-abc123"
RATELIMIT_ENABLED = "sentinel.ratelimit.checkout_users.enabled"
RATELIMIT_ENFORCED = "sentinel.ratelimit.checkout_users.enforced"
THROTTLE_PERCENT = "sentinel.throttle.checkout_users.percent"


@dataclass
class FakeEdge:
    """A recorded proxy: runtime values in, the argument vectors it saw out."""

    runtime: dict[str, str] = field(default_factory=dict)
    pods: str = POD
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, timeout: int = 60) -> str:
        self.calls.append(list(argv))
        if "pods" in argv and "get" in argv and "--raw" not in argv:
            return self.pods
        raw = argv[argv.index("--raw") + 1]
        path = raw.split("/proxy/", 1)[1]
        if path.startswith("runtime_modify?"):
            self._record(path.removeprefix("runtime_modify?"))
            return "OK"
        if path == "runtime":
            return json.dumps(
                {"entries": {key: {"final_value": value} for key, value in self.runtime.items()}}
            )
        return ""

    def _record(self, query: str) -> None:
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            self.runtime[key] = value

    def writes(self) -> list[list[str]]:
        """Every call that changed the proxy, which is what safety tests care about."""
        return [call for call in self.calls if "create" in call]


def _mesh_config(**overrides: Any) -> MeshConfig:
    fields: dict[str, Any] = {
        "context": "k3d-sentinel-lab",
        "namespace": "otel-demo",
        "workload": "frontend-proxy",
        "cohorts": {"checkout-users": "checkout_users", "general-traffic": "general_traffic"},
    }
    fields.update(overrides)
    return MeshConfig.model_validate(fields)


def _decision(target: str = "frontend") -> Decision:
    return Decision(
        decision_id="decision-1",
        ts=TICK,
        incident_id="incident-1",
        action=DecisionAction.ACT,
        rule_id="restrain-a-deforming-cohort",
        reason="a confirmed attack with a named origin",
        evidence_ts=TICK,
        severity=IncidentSeverity.HIGH,
        confirmed=True,
        verification_id="verification-1",
        requires_human_approval=False,
        verdict_class=VerdictClass.ATTACK,
        verdict_id="verdict-1",
        confidence=0.9,
        target_service=target,
    )


def _actuator(edge: FakeEdge | None = None, **overrides: Any) -> MeshActuator:
    return MeshActuator(configuration=_mesh_config(**overrides), command=edge or FakeEdge())


def _limit(actuator: MeshActuator, percent: int = 100, cohort: str = "checkout-users") -> Any:
    return actuator.plan(
        _decision(),
        action_kind=ActionKind.RATE_LIMIT,
        parameters={"cohort": cohort, "enforced_percent": percent},
        ts=TICK,
    )


# --- planning ---------------------------------------------------------------


def test_a_cohort_with_no_mapped_runtime_key_is_refused_not_guessed() -> None:
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match="no runtime key is mapped for cohort 'vips'"):
        _limit(actuator, cohort="vips")


def test_a_rung_this_adapter_does_not_carry_out_is_refused() -> None:
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match="does not carry out SCALE"):
        actuator.plan(_decision(), action_kind=ActionKind.SCALE, parameters={}, ts=TICK)


def test_a_restraint_demands_an_absolute_percentage() -> None:
    """A relative change cannot be deduplicated, so it is refused at plan time."""
    actuator = _actuator()
    rejected: tuple[dict[str, ActionParameterValue], ...] = (
        {"cohort": "checkout-users", "enforced_percent": "100"},
        {"cohort": "checkout-users", "enforced_percent": True},
        {"cohort": "checkout-users", "percent_delta": 10},
        {"cohort": "checkout-users"},
        {},
    )
    for parameters in rejected:
        with pytest.raises(ActionRejectedError):
            actuator.plan(
                _decision(), action_kind=ActionKind.RATE_LIMIT, parameters=parameters, ts=TICK
            )


def test_restraining_none_or_more_than_all_of_a_cohort_is_refused() -> None:
    actuator = _actuator()
    for percent in (0, -10, 101):
        with pytest.raises(ActionRejectedError, match=r"outside 1\.\.100"):
            _limit(actuator, percent)


def test_each_rung_takes_its_own_parameter_so_a_plan_cannot_be_mistaken() -> None:
    """RATE_LIMIT refuses traffic and THROTTLE slows it; the two are not interchangeable."""
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match="takes exactly 'cohort' and 'delayed_percent'"):
        actuator.plan(
            _decision(),
            action_kind=ActionKind.THROTTLE,
            parameters={"cohort": "checkout-users", "enforced_percent": 50},
            ts=TICK,
        )


def test_a_plan_names_the_live_edge_and_what_to_expect() -> None:
    actuator = _actuator()
    plan = _limit(actuator, 60)
    assert plan.target_ref == f"otel-demo/pod/{POD}/cohort/checkout-users"
    assert plan.target_service == "frontend", "the plan protects the service the decision named"
    assert plan.honesty == "REAL"
    assert plan.reversible
    assert not plan.requires_human_approval, "restraining a cohort is not a destructive rung"
    assert plan.estimated_blast_fraction == 0.6


def test_restraining_a_cohort_harder_is_a_different_effect() -> None:
    """The idempotency key is the effect, so 60% and 100% must not deduplicate."""
    actuator = _actuator()
    assert _limit(actuator, 60).idempotency_key != _limit(actuator, 100).idempotency_key
    assert _limit(actuator, 60).idempotency_key == _limit(actuator, 60).idempotency_key


def test_two_edges_mean_a_restraint_nobody_chose() -> None:
    """A runtime override reaches one process; covering N is a feature, not an inference."""
    edge = FakeEdge(pods="frontend-proxy-a frontend-proxy-b")
    with pytest.raises(ActionRejectedError, match="2 frontend-proxy pods are running"):
        _limit(_actuator(edge))
    assert edge.writes() == []


def test_no_edge_at_all_is_an_unreachable_edge_not_a_silent_pass() -> None:
    with pytest.raises(EdgeUnreachableError, match="there is no edge to restrain"):
        _limit(_actuator(FakeEdge(pods="")))


# --- carrying out -----------------------------------------------------------


def test_a_simulation_touches_nothing() -> None:
    edge = FakeEdge(runtime={RATELIMIT_ENABLED: "0", RATELIMIT_ENFORCED: "0"})
    actuator = _actuator(edge)
    outcome = actuator.simulate(_limit(actuator), ts=TICK)

    assert outcome.status is ActionStatus.SIMULATED
    assert outcome.dry_run
    assert edge.writes() == [], "a simulation wrote to the proxy"
    assert f"{RATELIMIT_ENFORCED}: 0 -> 100" in outcome.observed


def test_a_rate_limit_holds_the_whole_cohort_to_its_ceiling_and_enforces_a_share() -> None:
    """`enabled` measures the cohort; `enforced` is the dial a canary widens."""
    edge = FakeEdge(runtime={RATELIMIT_ENABLED: "0", RATELIMIT_ENFORCED: "0"})
    actuator = _actuator(edge)
    applied = actuator.apply(_limit(actuator, 40), ts=TICK)

    assert applied.status is ActionStatus.APPLIED
    assert edge.runtime[RATELIMIT_ENABLED] == "100"
    assert edge.runtime[RATELIMIT_ENFORCED] == "40"
    assert THROTTLE_PERCENT not in edge.runtime, "a rate limit must not also slow the cohort"


def test_a_throttle_slows_a_share_and_refuses_nobody() -> None:
    edge = FakeEdge(runtime={THROTTLE_PERCENT: "0"})
    actuator = _actuator(edge)
    plan = actuator.plan(
        _decision(),
        action_kind=ActionKind.THROTTLE,
        parameters={"cohort": "checkout-users", "delayed_percent": 25},
        ts=TICK,
    )
    actuator.apply(plan, ts=TICK)

    assert edge.runtime[THROTTLE_PERCENT] == "25"
    assert RATELIMIT_ENFORCED not in edge.runtime, "a throttle must not also refuse traffic"


def test_applying_reads_the_previous_restraint_before_it_writes() -> None:
    """The restraint we apply over is only knowable before the write, and revert needs it."""
    edge = FakeEdge(runtime={RATELIMIT_ENABLED: "100", RATELIMIT_ENFORCED: "30"})
    actuator = _actuator(edge)
    applied = actuator.apply(_limit(actuator, 90), ts=TICK)

    assert applied.revert_token == (f"runtime={RATELIMIT_ENABLED}:100|{RATELIMIT_ENFORCED}:30")


def test_a_key_the_edge_never_published_is_recorded_as_no_restraint() -> None:
    """Absent means "not restrained"; omitting it would leave a cohort limited forever."""
    actuator = _actuator(FakeEdge(runtime={}))
    applied = actuator.apply(_limit(actuator), ts=TICK)

    assert applied.revert_token == f"runtime={RATELIMIT_ENABLED}:0|{RATELIMIT_ENFORCED}:0"


def test_a_value_the_edge_cannot_mean_is_recorded_as_no_restraint() -> None:
    """Envoy stores a non-percentage verbatim and behaves as if it were zero."""
    actuator = _actuator(FakeEdge(runtime={RATELIMIT_ENABLED: "abc", RATELIMIT_ENFORCED: "0"}))
    applied = actuator.apply(_limit(actuator), ts=TICK)

    assert applied.revert_token == f"runtime={RATELIMIT_ENABLED}:0|{RATELIMIT_ENFORCED}:0"


def test_reverting_restores_exactly_what_was_there_before() -> None:
    edge = FakeEdge(runtime={RATELIMIT_ENABLED: "100", RATELIMIT_ENFORCED: "100"})
    actuator = _actuator(edge)
    actuator.revert(
        _limit(actuator),
        ts=TICK,
        revert_token=f"runtime={RATELIMIT_ENABLED}:0|{RATELIMIT_ENFORCED}:25",
    )

    assert edge.runtime == {RATELIMIT_ENABLED: "0", RATELIMIT_ENFORCED: "25"}


def test_reverting_without_the_token_is_refused_rather_than_guessed() -> None:
    edge = FakeEdge()
    actuator = _actuator(edge)
    with pytest.raises(ActionRejectedError, match="cannot be put back without the token"):
        actuator.revert(_limit(actuator), ts=TICK)
    assert edge.writes() == []


def test_a_token_minted_for_another_rung_is_refused() -> None:
    actuator = _actuator()
    plan = _limit(actuator)
    for token in ("replicas=2", f"runtime={THROTTLE_PERCENT}:0", "runtime=", "runtime=a:b"):
        with pytest.raises(ActionRejectedError):
            actuator.revert(plan, ts=TICK, revert_token=token)


def test_verify_reads_the_edge_rather_than_trusting_the_write() -> None:
    """The admin API answers OK to a write it then ignores, so the read is the check."""
    actuator = _actuator(FakeEdge(runtime={RATELIMIT_ENABLED: "100", RATELIMIT_ENFORCED: "100"}))
    assert actuator.verify(_limit(actuator), ts=TICK).status is ActionStatus.VERIFIED

    ignored = _actuator(FakeEdge(runtime={RATELIMIT_ENABLED: "100", RATELIMIT_ENFORCED: "abc"}))
    failed = ignored.verify(_limit(ignored), ts=TICK)
    assert failed.status is ActionStatus.FAILED
    assert "abc" in failed.detail


def test_a_plan_aimed_at_a_proxy_that_is_gone_is_refused() -> None:
    """A plan is data; a restraint applied to a replaced pod would be applied to nothing."""
    actuator = _actuator()
    honest = _limit(actuator)
    replaced = _actuator(FakeEdge(pods="frontend-proxy-def456"))
    with pytest.raises(ActionRejectedError, match="the edge is now frontend-proxy-def456"):
        replaced.apply(honest, ts=TICK)


def test_a_plan_whose_target_and_parameters_disagree_is_refused() -> None:
    edge = FakeEdge()
    actuator = _actuator(edge)
    forged = _limit(actuator).model_copy(
        update={"target_ref": f"otel-demo/pod/{POD}/cohort/general-traffic"}
    )
    with pytest.raises(ActionRejectedError, match="disagree about who is being restrained"):
        actuator.apply(forged, ts=TICK)
    assert edge.writes() == []


def test_a_plan_aimed_outside_the_namespace_is_refused() -> None:
    edge = FakeEdge()
    actuator = _actuator(edge)
    foreign = _limit(actuator).model_copy(
        update={"target_ref": f"kube-system/pod/{POD}/cohort/checkout-users"}
    )
    with pytest.raises(ActionRejectedError, match="not an edge proxy in otel-demo"):
        actuator.apply(foreign, ts=TICK)
    assert edge.writes() == []


# --- configuration ----------------------------------------------------------


def test_the_committed_config_maps_only_cohorts_the_platform_knows() -> None:
    from common.config import load_config

    configuration = load_action_config(CONFIG_PATH)
    assert configuration.mesh is not None
    known = {cohort.cohort_id for cohort in load_config(CONFIG_PATH.parent).cohorts.cohorts}
    assert set(configuration.mesh.cohorts) <= known


def test_the_committed_config_matches_the_keys_the_edge_publishes() -> None:
    """Three artifacts have to agree, and only a test can hold them together."""
    import yaml

    configuration = load_action_config(CONFIG_PATH)
    assert configuration.mesh is not None
    template = (CONFIG_PATH.parents[1] / "lab" / "testbed" / "envoy.tmpl.yaml").read_text(
        encoding="utf-8"
    )

    published = yaml.safe_load(template)["layered_runtime"]["layers"]
    keys = next(layer for layer in published if layer["name"] == "sentinel_static")
    ratelimit = keys["static_layer"]["sentinel"]["ratelimit"]
    throttle = keys["static_layer"]["sentinel"]["throttle"]

    for segment in configuration.mesh.cohorts.values():
        assert segment in ratelimit, f"the edge publishes no rate-limit keys for {segment}"
        assert segment in throttle, f"the edge publishes no throttle key for {segment}"


def test_enabling_the_adapter_without_an_edge_section_is_refused() -> None:
    with pytest.raises(ValueError, match="no `mesh:` section"):
        ActionConfig.model_validate(
            {
                "version": 1,
                "execution": {
                    "dry_run": True,
                    "lease_ttl_seconds": 120.0,
                    "journal_capacity": 16,
                },
                "actuators": [{"actuator": "MESH", "enabled": True}],
            }
        )


def test_the_two_mechanisms_may_not_share_a_runtime_key() -> None:
    with pytest.raises(ValueError, match="cannot share a runtime key"):
        _mesh_config(ratelimit_key_prefix="sentinel.edge", throttle_key_prefix="sentinel.edge")


def test_a_runtime_segment_that_could_smuggle_a_second_assignment_is_refused() -> None:
    for segment in ("checkout&other.enabled", "checkout users", "Checkout", "a.b"):
        with pytest.raises(ValueError, match="not usable as runtime-key segments"):
            _mesh_config(cohorts={"checkout-users": segment})


def test_an_edge_that_may_restrain_no_cohort_is_refused() -> None:
    with pytest.raises(ValueError, match="map at least one"):
        _mesh_config(cohorts={})


# --- a real Envoy -----------------------------------------------------------


def _sandbox_admin(path: str, *, write: bool = False) -> str:
    request = urllib.request.Request(
        f"{SANDBOX_ADMIN}/{path}", method="POST" if write else "GET", data=b"" if write else None
    )
    with urllib.request.urlopen(request, timeout=10) as answer:
        return str(answer.read().decode("utf-8"))


@dataclass
class SandboxEdge:
    """The real Envoy from `make lab-edge`, behind the cluster seam.

    The sandbox has no API server in front of it, so this translates the same
    argument vectors into direct admin calls. Everything the adapter decides -
    which keys, which values, what counts as verified - is exercised for real.
    """

    def __call__(self, argv: Sequence[str], *, timeout: int = 60) -> str:
        if "pods" in argv and "--raw" not in argv:
            return POD
        path = argv[argv.index("--raw") + 1].split("/proxy/", 1)[1]
        try:
            return _sandbox_admin(path, write="create" in argv)
        except urllib.error.URLError as error:  # pragma: no cover - sandbox down
            raise EdgeUnreachableError(f"the sandbox edge is unreachable: {error}") from error


@pytest.fixture
def sandbox() -> Any:
    """A real Envoy with every cohort key back at zero, before and after."""
    if os.environ.get(INTEGRATION_ENV) != "1":
        pytest.skip(f"set {INTEGRATION_ENV}=1 with `make lab-edge` running to run this")
    reset = "&".join(
        f"{key}=0" for key in (RATELIMIT_ENABLED, RATELIMIT_ENFORCED, THROTTLE_PERCENT)
    )
    _sandbox_admin(f"runtime_modify?{reset}", write=True)
    yield SandboxEdge()
    _sandbox_admin(f"runtime_modify?{reset}", write=True)


def test_a_real_envoy_applies_verifies_and_gives_the_cohort_back(sandbox: SandboxEdge) -> None:
    actuator = MeshActuator(configuration=_mesh_config(), command=sandbox)
    plan = _limit(actuator, 100)

    applied = actuator.apply(plan, ts=TICK)
    assert applied.status is ActionStatus.APPLIED
    assert applied.revert_token == f"runtime={RATELIMIT_ENABLED}:0|{RATELIMIT_ENFORCED}:0"
    assert actuator.verify(plan, ts=TICK).status is ActionStatus.VERIFIED

    actuator.revert(plan, ts=TICK, revert_token=applied.revert_token)
    assert actuator.verify(plan, ts=TICK).status is ActionStatus.FAILED, (
        "the cohort is still restrained after a revert"
    )


def test_a_real_envoy_restrains_the_named_cohort_and_nobody_else(sandbox: SandboxEdge) -> None:
    """The whole point of the rung: the surge keeps being served.

    The proxy's frontend cluster is its own admin listener in the sandbox, so a
    request that survives gets a real HTTP answer and a refused one is a 429.
    """
    actuator = MeshActuator(configuration=_mesh_config(), command=sandbox)
    actuator.apply(_limit(actuator, 100), ts=TICK)

    restrained = _sandbox_status("/api/checkout", times=20)
    untouched = _sandbox_status("/", times=20)

    assert 429 in restrained, "the cohort was not restrained at all"
    assert 429 not in untouched, "traffic outside the cohort was restrained"


def _sandbox_status(path: str, *, times: int) -> list[int]:
    """Status codes from the sandbox proxy, which publishes 8048 beside the admin port."""
    codes: list[int] = []
    for _ in range(times):
        completed = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", f"http://127.0.0.1:8048{path}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        codes.append(int(completed.stdout.strip() or 0))
    return codes


def test_the_executor_stays_in_dry_run_against_a_real_envoy(sandbox: SandboxEdge) -> None:
    """The committed posture is dry-run, and it holds all the way to a real proxy."""
    configuration = load_action_config(CONFIG_PATH)
    assert configuration.mesh is not None
    assert configuration.execution.dry_run, "config/action.yml must ship dry-run"
    actuator = MeshActuator(configuration=configuration.mesh, command=sandbox)
    executor = ActionExecutor(actuators=[actuator], configuration=configuration, dry_run=True)

    plan = _limit(actuator, 100)
    outcome = executor.apply(plan, ts=TICK, owner="integration-test")

    assert outcome.status is ActionStatus.SIMULATED
    assert executor.journal.entries() == ()
    assert actuator.kind is ActuatorKind.MESH
    assert _sandbox_admin("runtime")  # the proxy is still readable and unchanged
    assert actuator.verify(plan, ts=TICK).status is ActionStatus.FAILED
