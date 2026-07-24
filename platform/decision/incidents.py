"""Collapse a storm of symptom episodes into the incidents a person deals with.

One payment fault lights the payment edge, the checkout edge above it, the log
templates on both, and the memory of whatever retries. Paging twelve times for
that is how on-call teams learn to ignore alerts. This module groups
co-occurring episodes on topologically adjacent services into one incident,
keeps that incident's identity stable as the storm grows around it, and lets it
resolve only after a quiet period rather than the moment the last symptom
blinks out.

Nothing here is a threshold on telemetry: every input is an already-durable
episode from the detection plane, and every grouping decision is stated in
committed configuration.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from common.config import TopologyConfig
from contracts import (
    AgentAssessment,
    AgentStatus,
    EpisodeStatus,
    EvidenceAxis,
    Incident,
    IncidentSeverity,
    IncidentState,
    SymptomEpisode,
)
from decision.config import IncidentsConfig

_CRITICALITY_ORDER = ("low", "medium", "high", "critical")


@dataclass(frozen=True, slots=True)
class _Cluster:
    """One connected group of episodes, before it becomes a contract."""

    anchor: SymptomEpisode
    episodes: tuple[SymptomEpisode, ...]


class IncidentTracker:
    """Assemble and age incidents from the durable episode stream.

    The tracker is stateful across ticks because incident identity is: an
    incident is the same incident tomorrow as today, anchored to the earliest
    episode that started it. When a new episode bridges two existing clusters,
    they merge under the older anchor and the absorbed identity is recorded, so
    a responder can follow what happened to the alert they were looking at.
    """

    def __init__(
        self,
        *,
        configuration: IncidentsConfig,
        topology: TopologyConfig,
    ) -> None:
        self._configuration = configuration
        self._criticality = {service.service: service.criticality for service in topology.services}
        self._hops = _hop_distances(topology)
        self._last_ts: datetime | None = None
        self._revisions: dict[str, int] = {}
        self._known: dict[str, str] = {}
        self._quiet_since: dict[str, datetime] = {}

    def observe(
        self,
        *,
        ts: datetime,
        episodes: Sequence[SymptomEpisode],
        business: AgentAssessment | None = None,
    ) -> tuple[Incident, ...]:
        """Return the incident set implied by every episode seen at this tick."""
        moment = _utc(ts)
        if self._last_ts is not None and moment < self._last_ts:
            raise ValueError("incident ticks must not move backwards in event time")
        members = _validated(episodes)
        if business is not None and business.axis is not EvidenceAxis.BUSINESS_IMPACT:
            raise ValueError("only the business-impact assessment estimates incident impact")

        clusters = self._cluster(members)
        incidents = tuple(
            self._incident(cluster, moment=moment, business=business) for cluster in clusters
        )
        self._last_ts = moment
        return incidents

    def _cluster(self, episodes: tuple[SymptomEpisode, ...]) -> tuple[_Cluster, ...]:
        slack = timedelta(seconds=self._configuration.clustering.join_window_seconds)
        parent = {episode.episode_id: episode.episode_id for episode in episodes}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            first, second = find(left), find(right)
            if first != second:
                parent[max(first, second)] = min(first, second)

        for index, left in enumerate(episodes):
            for right in episodes[index + 1 :]:
                if self._same_problem(left, right, slack=slack):
                    union(left.episode_id, right.episode_id)

        grouped: dict[str, list[SymptomEpisode]] = {}
        for episode in episodes:
            grouped.setdefault(find(episode.episode_id), []).append(episode)
        clusters = []
        for group in grouped.values():
            ordered = tuple(sorted(group, key=lambda item: (item.opened_ts, item.episode_id)))
            clusters.append(_Cluster(anchor=ordered[0], episodes=ordered))
        return tuple(
            sorted(clusters, key=lambda item: (item.anchor.opened_ts, item.anchor.episode_id))
        )

    def _same_problem(
        self,
        left: SymptomEpisode,
        right: SymptomEpisode,
        *,
        slack: timedelta,
    ) -> bool:
        if not _overlaps(left, right, slack=slack):
            return False
        if left.service == right.service:
            return True
        distance = self._hops.get((left.service, right.service))
        if distance is None:
            return False
        return distance <= self._configuration.clustering.max_topology_hops

    def _incident(
        self,
        cluster: _Cluster,
        *,
        moment: datetime,
        business: AgentAssessment | None,
    ) -> Incident:
        episode_ids = tuple(episode.episode_id for episode in cluster.episodes)
        incident_id = _incident_id(cluster.anchor.episode_id)
        merged = tuple(
            sorted(
                {
                    _incident_id(episode_id)
                    for episode_id in episode_ids
                    if episode_id in self._known and self._known[episode_id] != incident_id
                }
            )
        )
        for episode_id in episode_ids:
            self._known[episode_id] = incident_id

        active = [episode for episode in cluster.episodes if episode.status is EpisodeStatus.ACTIVE]
        last_activity = max(
            (episode.closed_ts or episode.last_breach_ts for episode in cluster.episodes),
            default=cluster.anchor.opened_ts,
        )
        state = self._state(
            incident_id, active=bool(active), last_activity=last_activity, moment=moment
        )
        impact = _impact_for(business, frozenset(episode_ids))
        severity = self._severity(cluster.episodes, impact=impact)
        revision = self._revisions.get(incident_id, 0) + 1
        self._revisions[incident_id] = revision

        services = tuple(sorted({episode.service for episode in cluster.episodes}))
        kinds = tuple(
            sorted({episode.kind for episode in cluster.episodes}, key=lambda kind: kind.value)
        )
        measured = "unmeasured" if impact is None else f"{impact:.2f}"
        note = (
            f"{len(episode_ids)} episodes across {len(services)} "
            f"{'service' if len(services) == 1 else 'services'} collapsed into one incident; "
            f"business impact {measured}"
        )
        return Incident(
            incident_id=incident_id,
            anchor_episode_id=cluster.anchor.episode_id,
            opened_ts=cluster.anchor.opened_ts,
            last_activity_ts=max(last_activity, cluster.anchor.opened_ts),
            state=state,
            severity=severity,
            services=services,
            kinds=kinds,
            episode_ids=episode_ids,
            business_impact=impact,
            merged_incident_ids=merged,
            revision=revision,
            note=note,
        )

    def _state(
        self,
        incident_id: str,
        *,
        active: bool,
        last_activity: datetime,
        moment: datetime,
    ) -> IncidentState:
        if active:
            self._quiet_since.pop(incident_id, None)
            return IncidentState.OPEN
        quiet_since = self._quiet_since.setdefault(incident_id, max(last_activity, moment))
        grace = timedelta(seconds=self._configuration.lifecycle.resolve_after_seconds)
        if moment - quiet_since >= grace:
            return IncidentState.RESOLVED
        return IncidentState.MONITORING

    def _severity(
        self,
        episodes: Sequence[SymptomEpisode],
        *,
        impact: float | None,
    ) -> IncidentSeverity:
        settings = self._configuration.severity
        if impact is not None:
            return settings.band_for(impact)
        levels = [
            self._criticality[episode.service]
            for episode in episodes
            if episode.service in self._criticality
        ]
        if not levels:
            return settings.fallback_for(_CRITICALITY_ORDER[0])
        worst = max(levels, key=_CRITICALITY_ORDER.index)
        return settings.fallback_for(worst)


def _validated(episodes: Sequence[SymptomEpisode]) -> tuple[SymptomEpisode, ...]:
    seen: set[str] = set()
    for episode in episodes:
        if not isinstance(episode, SymptomEpisode):
            raise TypeError("incidents are assembled from SymptomEpisode values")
        if episode.episode_id in seen:
            raise ValueError(
                "duplicate episode revisions must be collapsed before clustering: "
                f"{episode.episode_id}"
            )
        seen.add(episode.episode_id)
    return tuple(episodes)


def _overlaps(left: SymptomEpisode, right: SymptomEpisode, *, slack: timedelta) -> bool:
    left_end = (left.closed_ts or left.last_breach_ts) + slack
    right_end = (right.closed_ts or right.last_breach_ts) + slack
    return left.opened_ts - slack <= right_end and right.opened_ts - slack <= left_end


def _impact_for(business: AgentAssessment | None, episode_ids: frozenset[str]) -> float | None:
    """Reuse the business agent's own numbers, restricted to this incident.

    The impact estimate is never recomputed here: it is exactly the share of the
    business-impact axis that this incident's episodes produced, so the incident
    and the axis can never disagree about the same evidence.
    """
    if business is None or business.status is not AgentStatus.SCORED:
        return None
    remaining = 1.0
    for item in business.evidence:
        if episode_ids & set(item.evidence_refs):
            remaining *= 1.0 - item.contribution
    return min(max(1.0 - remaining, 0.0), 1.0)


def _hop_distances(topology: TopologyConfig) -> dict[tuple[str, str], int]:
    """All-pairs hop counts over the undirected dependency graph."""
    neighbours: dict[str, set[str]] = {service.service: set() for service in topology.services}
    for service in topology.services:
        for dependency in service.dependencies:
            neighbours.setdefault(dependency, set()).add(service.service)
            neighbours[service.service].add(dependency)
    distances: dict[tuple[str, str], int] = {}
    for origin in neighbours:
        frontier = [origin]
        seen = {origin: 0}
        while frontier:
            following: list[str] = []
            for node in frontier:
                for neighbour in sorted(neighbours[node]):
                    if neighbour not in seen:
                        seen[neighbour] = seen[node] + 1
                        following.append(neighbour)
            frontier = following
        for target, hops in seen.items():
            distances[(origin, target)] = hops
    return distances


def _incident_id(anchor_episode_id: str) -> str:
    identity = {"anchor_episode_id": anchor_episode_id}
    rendered = json.dumps(identity, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("ts must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError("ts must be timezone-aware UTC")
    return value.astimezone(UTC)


def incident_services(incidents: Iterable[Incident]) -> Mapping[str, tuple[str, ...]]:
    """Index incident ids to the services they cover, for downstream planes."""
    return {incident.incident_id: incident.services for incident in incidents}
