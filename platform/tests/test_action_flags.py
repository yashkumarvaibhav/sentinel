"""The feature-flag actuator, against a recorded cluster and against a real flagd.

The unit tests drive a fake ``ClusterCommand`` so they run in hosted CI with no
provider at all. The integration tests at the bottom talk to a real flagd running
the pinned chart's own flag document (``make lab-flags``) and are gated behind an
environment variable, so a machine without one skips rather than fails.

The sandbox mounts the document writable, so flagd's file watcher picks a change
up directly. That means an apply and its verification are both exercised against
a live provider - the one thing a fake cannot show is that OFREP really answers
where this adapter looks for it, and really names the variant being served.
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
    FlagActuator,
    FlagProviderError,
    FlagsConfig,
    load_action_config,
)
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
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "action.yml"
INTEGRATION_ENV = "SENTINEL_ACTION_FLAGS_INTEGRATION"
SANDBOX_OFREP = "http://127.0.0.1:8049"

POD = "flagd-abc123"
DOCUMENT = {
    "flags": {
        "paymentFailure": {
            "defaultVariant": "100%",
            "state": "ENABLED",
            "variants": {"100%": 1.0, "off": 0.0},
        },
        "cartFailure": {
            "defaultVariant": "off",
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
        },
    }
}


@dataclass
class FakeProvider:
    """A recorded cluster: a flag document in, the argument vectors it saw out."""

    document: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DOCUMENT)))
    served: dict[str, str] | None = None
    pods: str = POD
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, timeout: int = 60) -> str:
        self.calls.append(list(argv))
        if "--raw" in argv:
            return self._evaluate(argv[argv.index("--raw") + 1])
        if "pods" in argv:
            return self.pods
        if "configmap" in argv and "get" in argv:
            return json.dumps(self.document)
        if "patch" in argv:
            patch = json.loads(argv[argv.index("-p") + 1])
            self.document = json.loads(next(iter(patch["data"].values())))
            return "configmap/flagd-config patched"
        return ""

    def _evaluate(self, path: str) -> str:
        """What the provider serves: the document, unless a test says otherwise."""
        flag = path.rsplit("/", 1)[1]
        if self.served is not None:
            return json.dumps({"key": flag, "variant": self.served[flag]})
        definition = self.document["flags"].get(flag)
        if definition is None:
            return json.dumps({"key": flag, "errorCode": "FLAG_NOT_FOUND"})
        return json.dumps({"key": flag, "variant": definition["defaultVariant"]})

    def writes(self) -> list[list[str]]:
        """Every call that changed the world, which is what safety tests care about."""
        return [call for call in self.calls if "patch" in call or "restart" in call]

    def variant(self, flag: str) -> str:
        return str(self.document["flags"][flag]["defaultVariant"])


def _flags_config(**overrides: Any) -> FlagsConfig:
    fields: dict[str, Any] = {
        "context": "k3d-sentinel-lab",
        "namespace": "otel-demo",
        "config_map": "flagd-config",
        "document_key": "demo.flagd.json",
        "workload": "flagd",
        "remediations": {
            "paymentFailure": {"service": "payment", "variant": "off"},
            "cartFailure": {"service": "cart", "variant": "off"},
        },
    }
    fields.update(overrides)
    return FlagsConfig.model_validate(fields)


def _decision(target: str = "payment") -> Decision:
    return Decision(
        decision_id="decision-1",
        ts=TICK,
        incident_id="incident-1",
        action=DecisionAction.ACT,
        rule_id="undo-the-change-that-caused-it",
        reason="a confirmed self-inflicted fault correlated with a flag flip",
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


def _actuator(provider: FakeProvider | None = None, **overrides: Any) -> FlagActuator:
    return FlagActuator(
        configuration=_flags_config(**overrides), command=provider or FakeProvider()
    )


def _flip(actuator: FlagActuator, flag: str = "paymentFailure", service: str = "payment") -> Any:
    return actuator.plan(
        _decision(service), action_kind=ActionKind.FLAG_FLIP, parameters={"flag": flag}, ts=TICK
    )


# --- planning ---------------------------------------------------------------


def test_a_flag_with_no_committed_remediation_is_refused_not_guessed() -> None:
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match="no remediation is committed for flag"):
        _flip(actuator, flag="emailMemoryLeak")


def test_a_rung_this_adapter_does_not_carry_out_is_refused() -> None:
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match="does not carry out SCALE"):
        actuator.plan(_decision(), action_kind=ActionKind.SCALE, parameters={}, ts=TICK)


def test_a_flag_that_changes_another_service_is_refused() -> None:
    """A decision may only undo a change to the service its evidence named."""
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match="changes cart, but the decision named payment"):
        _flip(actuator, flag="cartFailure", service="payment")


def test_this_actuator_may_only_ever_turn_a_fault_off() -> None:
    """The variant is committed, not passed: these flags are the fault injectors.

    An actuator that could set any variant could cause the incident it was
    dispatched to fix, which no policy above it could make safe.
    """
    actuator = _actuator()
    rejected: tuple[dict[str, ActionParameterValue], ...] = (
        {"flag": "paymentFailure", "variant": "100%"},
        {"flag": "paymentFailure", "variant": "on"},
        {"flag": "paymentFailure", "variant": ""},
    )
    for parameters in rejected:
        with pytest.raises(ActionRejectedError, match="is not the committed remediation"):
            actuator.plan(
                _decision(), action_kind=ActionKind.FLAG_FLIP, parameters=parameters, ts=TICK
            )


def test_naming_the_committed_variant_is_allowed_and_redundant() -> None:
    actuator = _actuator()
    stated = actuator.plan(
        _decision(),
        action_kind=ActionKind.FLAG_FLIP,
        parameters={"flag": "paymentFailure", "variant": "off"},
        ts=TICK,
    )
    assert stated.idempotency_key == _flip(actuator).idempotency_key


def test_a_plan_names_the_flag_document_and_what_to_expect() -> None:
    plan = _flip(_actuator())
    assert plan.target_ref == "otel-demo/configmap/flagd-config/flag/paymentFailure"
    assert plan.target_service == "payment"
    assert plan.parameters == {"flag": "paymentFailure", "variant": "off"}
    assert plan.honesty == "REAL"
    assert plan.reversible
    assert "serves" in plan.expected_effect


def test_a_flip_claims_the_whole_blast_radius() -> None:
    """The document is only re-read at start, so applying one restarts the provider."""
    plan = _flip(_actuator())
    assert plan.estimated_blast_fraction == 1.0
    assert not plan.requires_human_approval, "a reversible flag flip is not a destructive rung"


def test_planning_asks_the_cluster_for_nothing() -> None:
    """The committed table is enough to name the effect; reads belong to carrying it out."""
    provider = FakeProvider()
    _flip(_actuator(provider))
    assert provider.calls == []


# --- carrying out -----------------------------------------------------------


def test_a_simulation_touches_nothing() -> None:
    provider = FakeProvider()
    actuator = _actuator(provider)
    outcome = actuator.simulate(_flip(actuator), ts=TICK)

    assert outcome.status is ActionStatus.SIMULATED
    assert outcome.dry_run
    assert provider.writes() == [], "a simulation changed the flag document"
    assert "the document says 100%" in outcome.observed[0]
    assert "restarts flagd" in outcome.observed[1]


def test_applying_patches_the_document_and_rolls_the_provider_onto_it() -> None:
    provider = FakeProvider()
    actuator = _actuator(provider)
    applied = actuator.apply(_flip(actuator), ts=TICK)

    assert applied.status is ActionStatus.APPLIED
    assert applied.revert_token == "variant=100%"
    assert provider.variant("paymentFailure") == "off"
    assert provider.variant("cartFailure") == "off", "an unrelated flag was changed"
    restarted = [call for call in provider.calls if "restart" in call]
    assert restarted == [["-n", "otel-demo", "rollout", "restart", "deployment/flagd"]]
    assert any("status" in call for call in provider.calls), "the rollout was never waited on"


def test_a_flag_already_safe_is_not_a_reason_to_restart_the_provider() -> None:
    """A restart nobody needs is a mesh-wide disturbance for no effect."""
    provider = FakeProvider()
    actuator = _actuator(provider)
    applied = actuator.apply(_flip(actuator, flag="cartFailure", service="cart"), ts=TICK)

    assert applied.status is ActionStatus.APPLIED
    assert applied.revert_token == "variant=off"
    assert provider.writes() == []
    assert "already off" in applied.detail


def test_reverting_restores_the_variant_it_was_applied_over() -> None:
    provider = FakeProvider()
    actuator = _actuator(provider)
    plan = _flip(actuator)
    applied = actuator.apply(plan, ts=TICK)
    assert provider.variant("paymentFailure") == "off"

    actuator.revert(plan, ts=TICK, revert_token=applied.revert_token)
    assert provider.variant("paymentFailure") == "100%"


def test_reverting_without_the_token_is_refused_rather_than_guessed() -> None:
    provider = FakeProvider()
    actuator = _actuator(provider)
    with pytest.raises(ActionRejectedError, match="cannot be put back without the token"):
        actuator.revert(_flip(actuator), ts=TICK)
    assert provider.writes() == []


def test_a_token_minted_for_another_rung_is_refused() -> None:
    actuator = _actuator()
    with pytest.raises(ActionRejectedError, match="does not record a variant"):
        actuator.revert(_flip(actuator), ts=TICK, revert_token="replicas=2")


def test_reverting_to_a_variant_that_no_longer_exists_is_refused() -> None:
    """The document changed underneath us; restoring a gone variant is not a revert."""
    provider = FakeProvider()
    actuator = _actuator(provider)
    plan = _flip(actuator)
    with pytest.raises(ActionRejectedError, match="has no variant '50%' any more"):
        actuator.revert(plan, ts=TICK, revert_token="variant=50%")
    assert provider.writes() == []


def test_verify_asks_the_provider_rather_than_the_document() -> None:
    """A ConfigMap that says `off` proves only that we wrote a file."""
    provider = FakeProvider()
    actuator = _actuator(provider)
    plan = _flip(actuator)
    actuator.apply(plan, ts=TICK)
    assert actuator.verify(plan, ts=TICK).status is ActionStatus.VERIFIED

    # The document was written but the provider never rolled onto it - exactly
    # the failure a document-only check would report as success.
    stale = FakeProvider(served={"paymentFailure": "100%"})
    actuator = _actuator(stale)
    plan = _flip(actuator)
    stale.document["flags"]["paymentFailure"]["defaultVariant"] = "off"
    failed = actuator.verify(plan, ts=TICK)
    assert failed.status is ActionStatus.FAILED
    assert "serves paymentFailure=100%" in failed.detail


def test_a_provider_that_serves_no_variant_is_an_error_not_a_pass() -> None:
    """A verification that cannot read the world does not get to report success."""
    plan = _flip(_actuator())
    blind = FakeProvider()
    blind.document["flags"].pop("paymentFailure")
    with pytest.raises(FlagProviderError, match="served no variant"):
        _actuator(blind).verify(plan, ts=TICK)


def test_a_plan_aimed_at_another_document_is_refused() -> None:
    """A plan is data; the adapter re-checks its target before touching anything."""
    provider = FakeProvider()
    actuator = _actuator(provider)
    forged = _flip(actuator).model_copy(
        update={"target_ref": "kube-system/configmap/flagd-config/flag/paymentFailure"}
    )
    with pytest.raises(ActionRejectedError, match="is not the committed flag document"):
        actuator.apply(forged, ts=TICK)
    assert provider.calls == []


def test_a_plan_carrying_an_unsafe_variant_is_refused_at_the_adapter_too() -> None:
    provider = FakeProvider()
    actuator = _actuator(provider)
    forged = _flip(actuator).model_copy(
        update={"parameters": {"flag": "paymentFailure", "variant": "100%"}}
    )
    with pytest.raises(ActionRejectedError, match="other than its committed remediation"):
        actuator.apply(forged, ts=TICK)
    assert provider.calls == []


def test_a_flag_the_document_does_not_have_is_refused_rather_than_invented() -> None:
    provider = FakeProvider()
    provider.document["flags"].pop("paymentFailure")
    actuator = _actuator(provider)
    with pytest.raises(ActionRejectedError, match="has no flag 'paymentFailure'"):
        actuator.apply(_flip(actuator), ts=TICK)
    assert provider.writes() == []


# --- configuration ----------------------------------------------------------


def test_the_committed_config_maps_only_services_the_topology_knows() -> None:
    from common.config import load_config

    configuration = load_action_config(CONFIG_PATH)
    assert configuration.flags is not None
    known = {service.service for service in load_config(CONFIG_PATH.parent).topology.services}
    assert {entry.service for entry in configuration.flags.remediations.values()} <= known


def test_the_committed_config_agrees_with_the_change_feed_about_who_owns_a_flag() -> None:
    """Two artifacts name the same flag→service fact; disagreeing would be a bug."""
    import yaml

    configuration = load_action_config(CONFIG_PATH)
    assert configuration.flags is not None
    ledger = yaml.safe_load((CONFIG_PATH.parent / "deployments.yml").read_text(encoding="utf-8"))

    for flag, entry in configuration.flags.remediations.items():
        assert ledger["flag_mappings"].get(flag) == entry.service, (
            f"{flag} is attributed to different services by the ledger and the action plane"
        )


def test_every_committed_remediation_is_safe_by_name() -> None:
    """Nothing here may be a variant that switches a fault on."""
    configuration = load_action_config(CONFIG_PATH)
    assert configuration.flags is not None
    assert {entry.variant for entry in configuration.flags.remediations.values()} == {"off"}


def test_enabling_the_adapter_without_a_flags_section_is_refused() -> None:
    with pytest.raises(ValueError, match="no `flags:` section"):
        ActionConfig.model_validate(
            {
                "version": 1,
                "execution": {
                    "dry_run": True,
                    "lease_ttl_seconds": 120.0,
                    "journal_capacity": 16,
                },
                "actuators": [{"actuator": "FEATURE_FLAG", "enabled": True}],
            }
        )


def test_an_adapter_that_may_set_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="list at least one flag"):
        _flags_config(remediations={})


# --- a real flagd -----------------------------------------------------------


def _sandbox_served(flag: str) -> str:
    request = urllib.request.Request(
        f"{SANDBOX_OFREP}/ofrep/v1/evaluate/flags/{flag}", method="POST", data=b""
    )
    with urllib.request.urlopen(request, timeout=10) as answer:
        return str(json.loads(answer.read().decode("utf-8"))["variant"])


@dataclass
class SandboxProvider:
    """The real flagd from `make lab-flags`, behind the cluster seam.

    The sandbox has no API server, so this translates the same argument vectors
    into a document read/write and a direct OFREP call. Everything the adapter
    decides - which flag, which variant, what counts as verified - is exercised
    against a live provider; only the transport is stood in for.
    """

    document: Path
    rollouts: int = 0

    def __call__(self, argv: Sequence[str], *, timeout: int = 60) -> str:
        if "--raw" in argv:
            flag = argv[argv.index("--raw") + 1].rsplit("/", 1)[1]
            try:
                return json.dumps({"key": flag, "variant": _sandbox_served(flag)})
            except urllib.error.URLError as error:  # pragma: no cover - sandbox down
                raise FlagProviderError(f"the sandbox provider is unreachable: {error}") from error
        if "pods" in argv:
            return POD
        if "configmap" in argv and "get" in argv:
            return self.document.read_text(encoding="utf-8")
        if "patch" in argv:
            patch = json.loads(argv[argv.index("-p") + 1])
            self.document.write_text(next(iter(patch["data"].values())), encoding="utf-8")
            return "configmap/flagd-config patched"
        if "restart" in argv:
            self.rollouts += 1
        return ""


@pytest.fixture
def provider() -> Any:
    """A real flagd whose document is restored before and after."""
    if os.environ.get(INTEGRATION_ENV) != "1":
        pytest.skip(f"set {INTEGRATION_ENV}=1 with `make lab-flags` running to run this")
    document = Path(
        subprocess.run(
            [str(REPO_ROOT / "lab" / "testbed" / "flagd-sandbox.sh"), "document"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    original = document.read_text(encoding="utf-8")
    yield SandboxProvider(document=document)
    document.write_text(original, encoding="utf-8")


def _sandbox_actuator(provider: SandboxProvider) -> FlagActuator:
    return FlagActuator(
        configuration=_flags_config(
            remediations={"paymentFailure": {"service": "payment", "variant": "off"}}
        ),
        command=provider,
    )


def test_a_real_flagd_confirms_the_flip_it_is_actually_serving(provider: SandboxProvider) -> None:
    """Apply, then ask the provider - which is the only claim worth making."""
    actuator = _sandbox_actuator(provider)
    plan = _flip(actuator)

    # Start from the fault switched ON, which is what an incident looks like.
    provider(
        ["-n", "otel-demo", "patch", "configmap", "flagd-config", "-p", _document_with("100%")]
    )
    _wait_until_served("paymentFailure", "100%")
    assert actuator.verify(plan, ts=TICK).status is ActionStatus.FAILED

    applied = actuator.apply(plan, ts=TICK)
    assert applied.status is ActionStatus.APPLIED
    assert applied.revert_token == "variant=100%"
    _wait_until_served("paymentFailure", "off")
    assert actuator.verify(plan, ts=TICK).status is ActionStatus.VERIFIED

    actuator.revert(plan, ts=TICK, revert_token=applied.revert_token)
    _wait_until_served("paymentFailure", "100%")
    assert actuator.verify(plan, ts=TICK).status is ActionStatus.FAILED


def test_a_real_flagd_leaves_every_other_flag_where_it_was(provider: SandboxProvider) -> None:
    actuator = _sandbox_actuator(provider)
    before = {flag: _sandbox_served(flag) for flag in ("cartFailure", "kafkaQueueProblems")}

    actuator.apply(_flip(actuator), ts=TICK)
    _wait_until_served("paymentFailure", "off")

    assert {flag: _sandbox_served(flag) for flag in before} == before


def test_the_executor_stays_in_dry_run_against_a_real_flagd(provider: SandboxProvider) -> None:
    """The committed posture is dry-run, and it holds all the way to a real provider."""
    configuration = load_action_config(CONFIG_PATH)
    assert configuration.flags is not None
    assert configuration.execution.dry_run, "config/action.yml must ship dry-run"
    actuator = FlagActuator(configuration=configuration.flags, command=provider)
    executor = ActionExecutor(actuators=[actuator], configuration=configuration, dry_run=True)

    served = _sandbox_served("paymentFailure")
    outcome = executor.apply(_flip(actuator), ts=TICK, owner="integration-test")

    assert outcome.status is ActionStatus.SIMULATED
    assert executor.journal.entries() == ()
    assert actuator.kind is ActuatorKind.FEATURE_FLAG
    assert provider.rollouts == 0, "a dry run restarted the flag provider"
    assert _sandbox_served("paymentFailure") == served


def _document_with(variant: str) -> str:
    return json.dumps({"data": {"demo.flagd.json": _rendered(variant)}})


def _rendered(variant: str) -> str:
    document = json.loads(
        subprocess.run(
            ["cat", str(_sandbox_document())], capture_output=True, text=True, check=True
        ).stdout
    )
    document["flags"]["paymentFailure"]["defaultVariant"] = variant
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _sandbox_document() -> Path:
    return Path(
        subprocess.run(
            [str(REPO_ROOT / "lab" / "testbed" / "flagd-sandbox.sh"), "document"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )


def _wait_until_served(flag: str, variant: str, *, attempts: int = 25) -> None:
    """flagd re-reads the file on its own schedule; the test waits for the fact."""
    import time

    for _ in range(attempts):
        if _sandbox_served(flag) == variant:
            return
        time.sleep(0.4)
    raise AssertionError(f"{flag} never became {variant}; it serves {_sandbox_served(flag)}")
