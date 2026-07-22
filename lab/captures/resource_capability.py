"""Conservative, label-free qualification of fixed-capacity resource evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from contracts import Observation
from lab.captures.detection_replay import detection_replay_timeline
from lab.captures.store import RuntimeCapture

_USED_SIGNAL = "container.memory.working_set"
_CAPACITY_SIGNAL = "k8s.container.memory_limit"
_UNIT = "By"


class ResourceCapabilityStatus(StrEnum):
    """Whether one logical service has detector-safe resource evidence."""

    QUALIFIED = "QUALIFIED"
    INSUFFICIENT = "INSUFFICIENT"


class ResourceCapabilityReason(StrEnum):
    """Stable reason explaining qualification or fail-closed insufficiency."""

    QUALIFIED = "QUALIFIED"
    MISSING_BOTH = "MISSING_USED_AND_CAPACITY"
    MISSING_USED = "MISSING_USED"
    MISSING_CAPACITY = "MISSING_FIXED_CAPACITY"
    MISSING_IDENTITY = "MISSING_CONTAINER_INSTANCE_IDENTITY"
    UNSUPPORTED_METRIC = "UNSUPPORTED_METRIC_SEMANTICS"
    INSUFFICIENT_POINTS = "INSUFFICIENT_SERIES_POINTS"
    NON_CONSTANT_CAPACITY = "NON_CONSTANT_CAPACITY"
    INVALID_CAPACITY = "INVALID_CAPACITY"
    NEGATIVE_USED = "NEGATIVE_USED"
    USED_EXCEEDS_CAPACITY = "USED_EXCEEDS_CAPACITY"
    CONFLICTING_TIMESTAMP = "CONFLICTING_USED_TIMESTAMP"


@dataclass(frozen=True, slots=True)
class ResourceCapabilityResult:
    """Auditable capability result for one configured topology service."""

    service: str
    status: ResourceCapabilityStatus
    reason: ResourceCapabilityReason
    instance_id: str | None
    sample_count: int
    capacity_observation_count: int
    capacity: float | None
    first_ts: datetime | None
    last_ts: datetime | None
    used_evidence_refs: tuple[str, ...]
    capacity_evidence_refs: tuple[str, ...]

    def canonical_value(self) -> dict[str, object]:
        return {
            "capacity": self.capacity,
            "capacity_evidence_refs": self.capacity_evidence_refs,
            "capacity_observation_count": self.capacity_observation_count,
            "first_ts": self.first_ts.isoformat() if self.first_ts is not None else None,
            "instance_id": self.instance_id,
            "last_ts": self.last_ts.isoformat() if self.last_ts is not None else None,
            "reason": self.reason.value,
            "sample_count": self.sample_count,
            "service": self.service,
            "status": self.status.value,
            "used_evidence_refs": self.used_evidence_refs,
        }


@dataclass(frozen=True, slots=True)
class ResourceObservationAudit:
    """Deterministic resource capability results over public observations."""

    namespace: str
    minimum_series_points: int
    used_signal: str
    capacity_signal: str
    results: tuple[ResourceCapabilityResult, ...]

    @property
    def qualified_count(self) -> int:
        return sum(item.status is ResourceCapabilityStatus.QUALIFIED for item in self.results)

    def canonical_value(self) -> dict[str, object]:
        return {
            "capacity_signal": self.capacity_signal,
            "minimum_series_points": self.minimum_series_points,
            "namespace": self.namespace,
            "qualified_count": self.qualified_count,
            "results": [item.canonical_value() for item in self.results],
            "used_signal": self.used_signal,
            "version": 1,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical(self.canonical_value())


@dataclass(frozen=True, slots=True)
class CaptureResourceCapabilityAudit:
    """Replay-stable resource audit whose runtime boundary cannot read labels."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    capture_config_fingerprint: str
    raw_replay_sha256: str
    raw_dead_letters: tuple[str, ...]
    anchor_ts: datetime
    end_ts: datetime
    audit: ResourceObservationAudit
    private_labels_read: Literal[False] = False

    @property
    def results(self) -> tuple[ResourceCapabilityResult, ...]:
        return self.audit.results

    @property
    def qualified_count(self) -> int:
        return self.audit.qualified_count

    def canonical_bytes(self) -> bytes:
        return _canonical(
            {
                "anchor_ts": self.anchor_ts.isoformat(),
                "audit": self.audit.canonical_value(),
                "capture_config_fingerprint": self.capture_config_fingerprint,
                "capture_id": self.capture_id,
                "end_ts": self.end_ts.isoformat(),
                "honesty": {"stimulus": "SIMULATED", "telemetry": "REAL"},
                "private_labels_read": self.private_labels_read,
                "raw_dead_letters": self.raw_dead_letters,
                "raw_replay_sha256": self.raw_replay_sha256,
                "scenario_id": self.scenario_id,
                "seed": self.seed,
                "seed_purpose": self.seed_purpose,
                "version": 1,
            }
        )


def audit_resource_observations(
    observations: Sequence[Observation],
    *,
    topology_services: Sequence[str],
    namespace: str,
    minimum_series_points: int,
) -> ResourceObservationAudit:
    """Qualify only hard container limits paired with one container working set."""
    if not isinstance(observations, Sequence) or any(
        not isinstance(item, Observation) for item in observations
    ):
        raise TypeError("observations must be a sequence of Observation values")
    services = _validated_services(topology_services)
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("namespace must be a non-empty string")
    namespace = namespace.strip()
    if isinstance(minimum_series_points, bool) or not isinstance(minimum_series_points, int):
        raise TypeError("minimum_series_points must be an integer")
    if minimum_series_points < 1:
        raise ValueError("minimum_series_points must be positive")

    by_service: dict[str, list[Observation]] = defaultdict(list)
    missing_identity: set[str] = set()
    known = set(services)
    for observation in observations:
        if observation.signal not in {_USED_SIGNAL, _CAPACITY_SIGNAL}:
            continue
        attributes = observation.attributes
        if attributes.get("k8s.namespace.name") != namespace:
            continue
        container = attributes.get("k8s.container.name")
        if not isinstance(container, str) or container not in known:
            continue
        instance = attributes.get("k8s.pod.uid")
        if not isinstance(instance, str) or not instance.strip():
            missing_identity.add(container)
            continue
        by_service[container].append(observation)

    results = tuple(
        _service_result(
            service,
            by_service.get(service, ()),
            has_missing_identity=service in missing_identity,
            minimum_series_points=minimum_series_points,
        )
        for service in services
    )
    return ResourceObservationAudit(
        namespace=namespace,
        minimum_series_points=minimum_series_points,
        used_signal=_USED_SIGNAL,
        capacity_signal=_CAPACITY_SIGNAL,
        results=results,
    )


def audit_capture_resources(
    capture: RuntimeCapture,
    *,
    topology_services: Sequence[str],
    minimum_series_points: int,
    namespace: str = "otel-demo",
) -> CaptureResourceCapabilityAudit:
    """Audit a verified public capture without opening its private-label artifact."""
    timeline = detection_replay_timeline(capture)
    audit = audit_resource_observations(
        timeline.observations,
        topology_services=topology_services,
        namespace=namespace,
        minimum_series_points=minimum_series_points,
    )
    manifest = capture.manifest
    return CaptureResourceCapabilityAudit(
        capture_id=manifest.capture_id,
        scenario_id=manifest.scenario_id,
        seed=manifest.seed,
        seed_purpose=manifest.seed_purpose,
        capture_config_fingerprint=manifest.config_fingerprint,
        raw_replay_sha256=hashlib.sha256(timeline.raw.canonical_bytes()).hexdigest(),
        raw_dead_letters=timeline.raw.dead_letters,
        anchor_ts=timeline.anchor_ts,
        end_ts=timeline.end_ts,
        audit=audit,
    )


def _service_result(
    service: str,
    observations: Sequence[Observation],
    *,
    has_missing_identity: bool,
    minimum_series_points: int,
) -> ResourceCapabilityResult:
    if has_missing_identity:
        return _insufficient(service, ResourceCapabilityReason.MISSING_IDENTITY)
    if not observations:
        return _insufficient(service, ResourceCapabilityReason.MISSING_BOTH)

    by_instance: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        instance = observation.attributes["k8s.pod.uid"]
        assert isinstance(instance, str)
        by_instance[instance].append(observation)
    instance_id, instance_observations = max(
        by_instance.items(),
        key=lambda item: (
            max(observation.ts for observation in item[1]),
            item[0],
        ),
    )
    used = tuple(item for item in instance_observations if item.signal == _USED_SIGNAL)
    capacities = tuple(item for item in instance_observations if item.signal == _CAPACITY_SIGNAL)
    if not used and not capacities:
        return _insufficient(
            service, ResourceCapabilityReason.MISSING_BOTH, instance_id=instance_id
        )
    if not used:
        return _insufficient(
            service,
            ResourceCapabilityReason.MISSING_USED,
            instance_id=instance_id,
            capacities=capacities,
        )
    if not capacities:
        return _insufficient(
            service,
            ResourceCapabilityReason.MISSING_CAPACITY,
            instance_id=instance_id,
            used=used,
        )
    if any(not _supported_metric(item) for item in (*used, *capacities)):
        return _insufficient(
            service,
            ResourceCapabilityReason.UNSUPPORTED_METRIC,
            instance_id=instance_id,
            used=used,
            capacities=capacities,
        )

    capacity_values = {item.value for item in capacities}
    if any(value <= 0.0 for value in capacity_values):
        return _insufficient(
            service,
            ResourceCapabilityReason.INVALID_CAPACITY,
            instance_id=instance_id,
            used=used,
            capacities=capacities,
        )
    if len(capacity_values) != 1:
        return _insufficient(
            service,
            ResourceCapabilityReason.NON_CONSTANT_CAPACITY,
            instance_id=instance_id,
            used=used,
            capacities=capacities,
        )
    capacity = next(iter(capacity_values))

    by_timestamp: dict[datetime, list[Observation]] = defaultdict(list)
    for observation in used:
        by_timestamp[observation.ts].append(observation)
    if any(len({item.value for item in values}) != 1 for values in by_timestamp.values()):
        return _insufficient(
            service,
            ResourceCapabilityReason.CONFLICTING_TIMESTAMP,
            instance_id=instance_id,
            used=used,
            capacities=capacities,
            capacity=capacity,
        )
    deduplicated_used = tuple(
        min(values, key=lambda item: item.observation_id)
        for _, values in sorted(by_timestamp.items())
    )
    if any(item.value < 0.0 for item in deduplicated_used):
        return _insufficient(
            service,
            ResourceCapabilityReason.NEGATIVE_USED,
            instance_id=instance_id,
            used=deduplicated_used,
            capacities=capacities,
            capacity=capacity,
        )
    if any(item.value > capacity for item in deduplicated_used):
        return _insufficient(
            service,
            ResourceCapabilityReason.USED_EXCEEDS_CAPACITY,
            instance_id=instance_id,
            used=deduplicated_used,
            capacities=capacities,
            capacity=capacity,
        )
    if len(deduplicated_used) < minimum_series_points:
        return _insufficient(
            service,
            ResourceCapabilityReason.INSUFFICIENT_POINTS,
            instance_id=instance_id,
            used=deduplicated_used,
            capacities=capacities,
            capacity=capacity,
        )

    ordered_used = tuple(sorted(deduplicated_used, key=lambda item: item.ts))
    ordered_capacities = tuple(sorted(capacities, key=lambda item: (item.ts, item.observation_id)))
    return ResourceCapabilityResult(
        service=service,
        status=ResourceCapabilityStatus.QUALIFIED,
        reason=ResourceCapabilityReason.QUALIFIED,
        instance_id=instance_id,
        sample_count=len(ordered_used),
        capacity_observation_count=len(ordered_capacities),
        capacity=capacity,
        first_ts=ordered_used[0].ts,
        last_ts=ordered_used[-1].ts,
        used_evidence_refs=tuple(item.observation_id for item in ordered_used),
        capacity_evidence_refs=tuple(item.observation_id for item in ordered_capacities),
    )


def _insufficient(
    service: str,
    reason: ResourceCapabilityReason,
    *,
    instance_id: str | None = None,
    used: Sequence[Observation] = (),
    capacities: Sequence[Observation] = (),
    capacity: float | None = None,
) -> ResourceCapabilityResult:
    ordered_used = tuple(sorted(used, key=lambda item: (item.ts, item.observation_id)))
    ordered_capacities = tuple(sorted(capacities, key=lambda item: (item.ts, item.observation_id)))
    timestamps = tuple(item.ts for item in ordered_used)
    return ResourceCapabilityResult(
        service=service,
        status=ResourceCapabilityStatus.INSUFFICIENT,
        reason=reason,
        instance_id=instance_id,
        sample_count=len({item.ts for item in ordered_used}),
        capacity_observation_count=len(ordered_capacities),
        capacity=capacity,
        first_ts=min(timestamps) if timestamps else None,
        last_ts=max(timestamps) if timestamps else None,
        used_evidence_refs=tuple(item.observation_id for item in ordered_used),
        capacity_evidence_refs=tuple(item.observation_id for item in ordered_capacities),
    )


def _supported_metric(observation: Observation) -> bool:
    return (
        observation.unit == _UNIT
        and observation.attributes.get("otel.metric.kind") == "gauge"
        and math.isfinite(observation.value)
    )


def _validated_services(services: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(services, Sequence):
        raise TypeError("topology_services must be a sequence")
    values: list[str] = []
    for service in services:
        if not isinstance(service, str) or not service.strip():
            raise ValueError("topology services must be non-empty strings")
        values.append(service.strip())
    if not values:
        raise ValueError("topology_services must not be empty")
    if len(values) != len(set(values)):
        raise ValueError("topology services must be unique")
    return tuple(sorted(values))


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
