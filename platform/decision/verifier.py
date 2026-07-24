"""The deterministic gate every hypothesis must survive before anything acts on it.

Reasoning proposes; physics disposes. Whatever named this incident's origin -
today the causal collapse, later a language model - only ever produced a
hypothesis. This module is the part that checks it, and it is arithmetic over
telemetry and committed topology with no model anywhere in it.

Four checks, all required:

* ``temporal_causality`` - the origin must have been observed misbehaving no
  later than everything it is claimed to have caused. Crucially, the origin's
  first evidence includes the *dependency edges that accuse it*: when checkout's
  call to payment degrades, that IS the first observation of payment's fault,
  not a separate later effect. Checking only episodes whose service is the
  origin would fail every real cascade, because a service's own log burst
  routinely lands after the caller already noticed.
* ``trace_coverage`` - every service in the incident must actually have been
  covered by telemetry. A conclusion about a service we could not see is not a
  conclusion.
* ``dependency_validity`` - every service named must exist in the committed
  topology, and every dependency edge cited must be a real edge.
* ``memory_similarity`` - the shape must resemble something seen before, above
  the configured floor. Below ``bootstrap_minimum_entries`` stored incidents
  this passes **vacuously and is recorded as a bootstrap**, never as a
  confirmation - otherwise nothing could ever be confirmed on an empty store.

Anything unconfirmed is left for a human. That is the whole point.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from common.config import TopologyConfig
from contracts import (
    CheckOutcome,
    Incident,
    SymptomEpisode,
    SymptomKind,
    Verification,
    VerificationCheck,
)
from decision.config import CausalCollapseConfig, IncidentMemoryConfig, VerificationConfig
from decision.memory import SimilarIncident


def verify_incident(
    incident: Incident,
    *,
    episodes: Sequence[SymptomEpisode],
    covered_services: frozenset[str],
    matches: Sequence[SimilarIncident],
    memory_size: int,
    topology: TopologyConfig,
    configuration: VerificationConfig,
    memory: IncidentMemoryConfig,
    causal: CausalCollapseConfig,
) -> Verification:
    """Run all four checks over one incident and record what each one found."""
    if memory_size < 0:
        raise ValueError("memory_size cannot be negative")
    members = {episode.episode_id for episode in episodes}
    missing = sorted(set(incident.episode_ids) - members)
    if missing:
        raise ValueError(f"verification needs every member episode: missing {', '.join(missing)}")
    considered = tuple(
        episode for episode in episodes if episode.episode_id in set(incident.episode_ids)
    )

    checks = (
        _temporal_causality(incident, considered, configuration=configuration, causal=causal),
        _trace_coverage(incident, covered_services),
        _dependency_validity(incident, considered, topology=topology, causal=causal),
        _memory_similarity(matches, memory_size=memory_size, memory=memory),
    )
    confirmed = all(check.outcome is not CheckOutcome.FAILED for check in checks)
    ts = incident.last_activity_ts.astimezone(UTC)
    return Verification(
        verification_id=_verification_id(incident.incident_id, ts, checks),
        ts=ts,
        incident_id=incident.incident_id,
        confirmed=confirmed,
        checks=checks,
    )


def _temporal_causality(
    incident: Incident,
    episodes: Sequence[SymptomEpisode],
    *,
    configuration: VerificationConfig,
    causal: CausalCollapseConfig,
) -> VerificationCheck:
    name = "temporal_causality"
    if incident.origin_service is None:
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED,
            detail="the storm did not collapse to an origin, so nothing can be timed against it",
        )
    origin = incident.origin_service
    observations = [
        episode.opened_ts
        for episode in episodes
        if episode.service == origin or _accuses(episode, origin, causal=causal)
    ]
    if not observations:
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED,
            detail=f"no episode observed {origin} misbehaving at all",
        )
    first_seen = min(observations)
    tolerance = timedelta(seconds=configuration.onset_tolerance_seconds)
    earlier = [
        episode
        for episode in episodes
        if episode.opened_ts + tolerance < first_seen
        and episode.service != origin
        and not _accuses(episode, origin, causal=causal)
    ]
    if earlier:
        leader = min(earlier, key=lambda episode: episode.opened_ts)
        lead = (first_seen - leader.opened_ts).total_seconds()
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED,
            detail=(
                f"{leader.service} broke {lead:.0f}s before {origin} was first observed, "
                f"beyond the {configuration.onset_tolerance_seconds:.0f}s tolerance"
            ),
        )
    return VerificationCheck(
        name=name,
        outcome=CheckOutcome.PASSED,
        detail=(
            f"{origin} was observed misbehaving first, no later than any of the "
            f"{len(episodes)} episodes it is credited with"
        ),
    )


def _trace_coverage(incident: Incident, covered_services: frozenset[str]) -> VerificationCheck:
    name = "trace_coverage"
    uncovered = sorted(set(incident.services) - covered_services)
    if uncovered:
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED,
            detail=f"no telemetry coverage for {', '.join(uncovered)}",
        )
    return VerificationCheck(
        name=name,
        outcome=CheckOutcome.PASSED,
        detail=f"all {len(incident.services)} affected services were covered by telemetry",
    )


def _dependency_validity(
    incident: Incident,
    episodes: Sequence[SymptomEpisode],
    *,
    topology: TopologyConfig,
    causal: CausalCollapseConfig,
) -> VerificationCheck:
    name = "dependency_validity"
    known = {service.service for service in topology.services}
    edges = {
        (service.service, dependency)
        for service in topology.services
        for dependency in service.dependencies
    }
    unknown = sorted(set(incident.services) - known)
    if unknown:
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED,
            detail=f"unknown to the committed topology: {', '.join(unknown)}",
        )
    if incident.origin_service is not None and incident.origin_service not in known:
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED,
            detail=f"the named origin {incident.origin_service} is not a topology service",
        )
    invented = sorted(
        f"{episode.service}->{_callee(episode, causal=causal)}"
        for episode in episodes
        if _callee(episode, causal=causal) is not None
        and (episode.service, _callee(episode, causal=causal)) not in edges
    )
    if invented:
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED,
            detail=f"dependency edges that do not exist: {', '.join(invented)}",
        )
    return VerificationCheck(
        name=name,
        outcome=CheckOutcome.PASSED,
        detail="every named service and dependency edge exists in the committed topology",
    )


def _memory_similarity(
    matches: Sequence[SimilarIncident],
    *,
    memory_size: int,
    memory: IncidentMemoryConfig,
) -> VerificationCheck:
    name = "memory_similarity"
    if memory_size < memory.bootstrap_minimum_entries:
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.BOOTSTRAP,
            detail=(
                f"incident memory holds {memory_size} of the "
                f"{memory.bootstrap_minimum_entries} entries this check needs; "
                "passed vacuously as a bootstrap, not as a confirmation"
            ),
        )
    if not matches:
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED,
            detail=(
                f"nothing in a memory of {memory_size} incidents resembles this shape at "
                f"{memory.minimum_similarity:.2f} or better"
            ),
        )
    best = max(matches, key=lambda match: match.similarity)
    if best.similarity < memory.minimum_similarity:
        return VerificationCheck(
            name=name,
            outcome=CheckOutcome.FAILED,
            detail=(
                f"the closest precedent resembles this only at {best.similarity:.3f}, "
                f"under the {memory.minimum_similarity:.2f} floor"
            ),
        )
    return VerificationCheck(
        name=name,
        outcome=CheckOutcome.PASSED,
        detail=(
            f"resembles incident {best.signature.incident_id} at "
            f"{best.similarity:.3f} across a memory of {memory_size}"
        ),
    )


def _callee(episode: SymptomEpisode, *, causal: CausalCollapseConfig) -> str | None:
    if episode.kind is not SymptomKind.EDGE_DEGRADED:
        return None
    prefix = causal.dependency_signal_prefix
    if not episode.signal.startswith(prefix):
        return None
    return episode.signal[len(prefix) :] or None


def _accuses(episode: SymptomEpisode, service: str, *, causal: CausalCollapseConfig) -> bool:
    return _callee(episode, causal=causal) == service


def _verification_id(
    incident_id: str,
    ts: datetime,
    checks: Sequence[VerificationCheck],
) -> str:
    identity = {
        "incident_id": incident_id,
        "ts": ts.isoformat(),
        "checks": {check.name: check.outcome.value for check in checks},
    }
    rendered = json.dumps(identity, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
