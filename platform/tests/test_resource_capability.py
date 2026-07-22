"""Label-free resource evidence must prove a fixed-capacity topology stream."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lab.captures import load_runtime_capture
from lab.captures.resource_capability import (
    ResourceCapabilityReason,
    ResourceCapabilityResult,
    ResourceCapabilityStatus,
    audit_capture_resources,
    audit_resource_observations,
)

from common.config import load_config
from contracts import Observation

START = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
TOPOLOGY_SERVICES = ("cart", "checkout", "frontend", "payment")
CAPACITY = 20 * 1024 * 1024


def test_fixed_container_limit_qualifies_topology_working_set_order_independently() -> None:
    observations = _resource_series("checkout", points=12)

    first = audit_resource_observations(
        observations,
        topology_services=TOPOLOGY_SERVICES,
        namespace="otel-demo",
        minimum_series_points=12,
    )
    second = audit_resource_observations(
        tuple(reversed(observations)),
        topology_services=tuple(reversed(TOPOLOGY_SERVICES)),
        namespace="otel-demo",
        minimum_series_points=12,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    checkout = _result(first.results, "checkout")
    assert checkout.status is ResourceCapabilityStatus.QUALIFIED
    assert checkout.reason is ResourceCapabilityReason.QUALIFIED
    assert checkout.instance_id == "checkout-pod"
    assert checkout.sample_count == 12
    assert checkout.capacity_observation_count == 3
    assert checkout.capacity == CAPACITY
    assert checkout.first_ts == START
    assert checkout.last_ts == START + timedelta(seconds=110)
    assert checkout.used_evidence_refs == tuple(f"checkout-used-{index:02d}" for index in range(12))
    assert checkout.capacity_evidence_refs == tuple(
        f"checkout-capacity-{index:02d}" for index in range(3)
    )

    assert _result(first.results, "frontend").reason is ResourceCapabilityReason.MISSING_BOTH
    assert {item.service for item in first.results} == set(TOPOLOGY_SERVICES)


@pytest.mark.parametrize(
    ("observations_factory", "reason"),
    [
        (
            lambda: tuple(
                item
                for item in _resource_series("checkout", points=12)
                if item.signal != "k8s.container.memory_limit"
            ),
            ResourceCapabilityReason.MISSING_CAPACITY,
        ),
        (
            lambda: tuple(
                item
                for item in _resource_series("checkout", points=12)
                if item.signal != "container.memory.working_set"
            ),
            ResourceCapabilityReason.MISSING_USED,
        ),
        (
            lambda: _resource_series("checkout", points=11),
            ResourceCapabilityReason.INSUFFICIENT_POINTS,
        ),
        (
            lambda: _resource_series("checkout", points=12, changed_capacity=True),
            ResourceCapabilityReason.NON_CONSTANT_CAPACITY,
        ),
        (
            lambda: _resource_series("checkout", points=12, over_capacity=True),
            ResourceCapabilityReason.USED_EXCEEDS_CAPACITY,
        ),
    ],
)
def test_unusable_container_evidence_is_explicitly_insufficient(
    observations_factory: Callable[[], tuple[Observation, ...]],
    reason: ResourceCapabilityReason,
) -> None:
    audit = audit_resource_observations(
        observations_factory(),
        topology_services=("checkout",),
        namespace="otel-demo",
        minimum_series_points=12,
    )

    result = audit.results[0]
    assert result.status is ResourceCapabilityStatus.INSUFFICIENT
    assert result.reason is reason


def test_dynamic_runtime_heap_limits_and_non_topology_containers_never_qualify() -> None:
    dynamic = tuple(
        _metric(
            observation_id=f"payment-v8-{index:02d}",
            ts=START + timedelta(seconds=index * 10),
            service="payment",
            signal=("v8js.memory.heap.used" if index % 2 == 0 else "v8js.memory.heap.limit"),
            value=float(10_000_000 + index * 100_000),
            container="payment",
            pod="payment-pod",
        )
        for index in range(24)
    )
    infrastructure = _resource_series("otel-collector", points=12)

    audit = audit_resource_observations(
        dynamic + infrastructure,
        topology_services=("payment",),
        namespace="otel-demo",
        minimum_series_points=12,
    )

    assert audit.results[0].reason is ResourceCapabilityReason.MISSING_BOTH
    assert audit.qualified_count == 0


def test_ambiguous_identity_or_conflicting_timestamp_fails_closed() -> None:
    missing_identity = tuple(
        item.model_copy(
            update={
                "attributes": {
                    key: value for key, value in item.attributes.items() if key != "k8s.pod.uid"
                }
            }
        )
        for item in _resource_series("checkout", points=12)
    )
    conflict = (
        *_resource_series("checkout", points=12),
        _metric(
            observation_id="checkout-used-conflict",
            ts=START,
            service="astronomy",
            signal="container.memory.working_set",
            value=CAPACITY / 3,
            container="checkout",
            pod="checkout-pod",
        ),
    )

    missing = audit_resource_observations(
        missing_identity,
        topology_services=("checkout",),
        namespace="otel-demo",
        minimum_series_points=12,
    )
    conflicting = audit_resource_observations(
        conflict,
        topology_services=("checkout",),
        namespace="otel-demo",
        minimum_series_points=12,
    )

    assert missing.results[0].reason is ResourceCapabilityReason.MISSING_IDENTITY
    assert conflicting.results[0].reason is ResourceCapabilityReason.CONFLICTING_TIMESTAMP


def test_invalid_metric_semantics_and_capacity_fail_closed() -> None:
    unsupported = tuple(
        item.model_copy(update={"unit": "MiBy"})
        if item.signal == "container.memory.working_set"
        else item
        for item in _resource_series("checkout", points=12)
    )
    invalid_capacity = tuple(
        item.model_copy(update={"value": 0.0})
        if item.signal == "k8s.container.memory_limit"
        else item
        for item in _resource_series("checkout", points=12)
    )

    unsupported_result = audit_resource_observations(
        unsupported,
        topology_services=("checkout",),
        namespace="otel-demo",
        minimum_series_points=12,
    ).results[0]
    invalid_result = audit_resource_observations(
        invalid_capacity,
        topology_services=("checkout",),
        namespace="otel-demo",
        minimum_series_points=12,
    ).results[0]

    assert unsupported_result.reason is ResourceCapabilityReason.UNSUPPORTED_METRIC
    assert invalid_result.reason is ResourceCapabilityReason.INVALID_CAPACITY


@pytest.mark.lab
def test_v6_capture_proves_saturation_input_is_currently_insufficient() -> None:
    capture_root = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v6"
    if not capture_root.exists():
        pytest.skip("local REAL v6 capture is not present")
    config = load_config(REPO_ROOT / "config")

    first = audit_capture_resources(
        load_runtime_capture(capture_root),
        topology_services=tuple(item.service for item in config.topology.services),
        minimum_series_points=config.detectors.change_point_saturation.minimum_series_points,
    )
    second = audit_capture_resources(
        load_runtime_capture(capture_root),
        topology_services=tuple(item.service for item in config.topology.services),
        minimum_series_points=config.detectors.change_point_saturation.minimum_series_points,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.qualified_count == 0
    assert {item.reason for item in first.results} == {ResourceCapabilityReason.MISSING_BOTH}
    assert first.private_labels_read is False


@pytest.mark.lab
def test_v7_capture_qualifies_every_topology_resource_stream() -> None:
    capture_root = REPO_ROOT / "var" / "captures" / "phase2-cascade-401-dev-v7"
    if not capture_root.exists():
        pytest.skip("local REAL v7 capture is not present")
    config = load_config(REPO_ROOT / "config")

    first = audit_capture_resources(
        load_runtime_capture(capture_root),
        topology_services=tuple(item.service for item in config.topology.services),
        minimum_series_points=config.detectors.change_point_saturation.minimum_series_points,
    )
    second = audit_capture_resources(
        load_runtime_capture(capture_root),
        topology_services=tuple(item.service for item in config.topology.services),
        minimum_series_points=config.detectors.change_point_saturation.minimum_series_points,
    )

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.raw_dead_letters == ()
    assert first.qualified_count == 4
    assert all(item.status is ResourceCapabilityStatus.QUALIFIED for item in first.results)
    assert all(item.sample_count == 14 for item in first.results)
    assert all(item.capacity_observation_count == 15 for item in first.results)
    assert {item.service: item.capacity for item in first.results} == {
        "cart": 167_772_160.0,
        "checkout": 20_971_520.0,
        "frontend": 262_144_000.0,
        "payment": 146_800_640.0,
    }
    assert first.private_labels_read is False


def _resource_series(
    container: str,
    *,
    points: int,
    changed_capacity: bool = False,
    over_capacity: bool = False,
) -> tuple[Observation, ...]:
    used = tuple(
        _metric(
            observation_id=f"{container}-used-{index:02d}",
            ts=START + timedelta(seconds=index * 10),
            service="astronomy",
            signal="container.memory.working_set",
            value=(CAPACITY + 1 if over_capacity and index == points - 1 else CAPACITY / 4 + index),
            container=container,
            pod=f"{container}-pod",
        )
        for index in range(points)
    )
    capacity = tuple(
        _metric(
            observation_id=f"{container}-capacity-{index:02d}",
            ts=START + timedelta(seconds=index * 40),
            service="astronomy",
            signal="k8s.container.memory_limit",
            value=(CAPACITY * 2 if changed_capacity and index == 2 else CAPACITY),
            container=container,
            pod=f"{container}-pod",
        )
        for index in range(3)
    )
    return used + capacity


def _metric(
    *,
    observation_id: str,
    ts: datetime,
    service: str,
    signal: str,
    value: float,
    container: str,
    pod: str,
) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=ts,
        service=service,
        signal=signal,
        value=value,
        unit="By",
        attributes={
            "otel.metric.kind": "gauge",
            "k8s.namespace.name": "otel-demo",
            "k8s.container.name": container,
            "k8s.pod.uid": pod,
        },
    )


def _result(
    results: tuple[ResourceCapabilityResult, ...], service: str
) -> ResourceCapabilityResult:
    return next(item for item in results if item.service == service)
