"""Evidence-only materialization and strict reads for the causal graph."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Literal, Protocol

from pydantic import ValidationError

from common.config import TopologyConfig
from common.storage import IncidentGraphRecord
from contracts import (
    CausalGraph,
    CausalGraphEdge,
    CausalGraphNode,
    CausalGraphResponse,
    EpisodeStatus,
    SymptomEpisode,
    SymptomKind,
)
from decision import IncidentOutcome


class CausalGraphReader(Protocol):
    """The one latest-current-graph read used by the gateway."""

    async def latest_incident_graph(self) -> IncidentGraphRecord | None: ...


class CausalGraphDataError(ValueError):
    """Durable graph state could not support the public contract."""


def build_causal_graph(
    outcome: IncidentOutcome,
    *,
    episodes: tuple[SymptomEpisode, ...],
    topology: TopologyConfig,
    dependency_signal_prefix: str,
    honesty: Literal["REAL", "SIMULATED"],
) -> CausalGraph:
    """Materialize one graph from the decision's exact accepted evidence."""
    if not dependency_signal_prefix or dependency_signal_prefix.isspace():
        raise ValueError("dependency signal prefix cannot be blank")

    by_id: dict[str, SymptomEpisode] = {}
    for episode in episodes:
        if episode.episode_id in by_id:
            raise ValueError(f"duplicate episode revision supplied: {episode.episode_id}")
        by_id[episode.episode_id] = episode
    missing = tuple(
        episode_id for episode_id in outcome.incident.episode_ids if episode_id not in by_id
    )
    if missing:
        raise ValueError(f"missing incident episode evidence: {', '.join(missing)}")
    members = tuple(by_id[episode_id] for episode_id in outcome.incident.episode_ids)

    topology_by_service = {service.service: service for service in topology.services}
    incident_services = {
        *outcome.incident.services,
        *outcome.incident.implicated_services,
    }
    if outcome.incident.origin_service is not None:
        incident_services.add(outcome.incident.origin_service)
    unknown_services = sorted(incident_services - topology_by_service.keys())
    if unknown_services:
        raise ValueError(
            "incident references services absent from committed topology: "
            + ", ".join(unknown_services)
        )
    member_unknown = sorted({episode.service for episode in members} - topology_by_service.keys())
    if member_unknown:
        raise ValueError(
            "incident episodes reference services absent from committed topology: "
            + ", ".join(member_unknown)
        )

    episodes_by_service: defaultdict[str, list[SymptomEpisode]] = defaultdict(list)
    for episode in members:
        episodes_by_service[episode.service].append(episode)

    implicated = set(outcome.incident.implicated_services)
    nodes = tuple(
        _node(
            service=service.service,
            tier=service.tier,
            criticality=service.criticality,
            episodes=tuple(episodes_by_service[service.service]),
            implicated=service.service in implicated,
            origin_service=outcome.incident.origin_service,
            origin_confidence=outcome.incident.origin_confidence,
        )
        for service in sorted(topology.services, key=lambda item: item.service)
    )

    edge_evidence: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    committed_edges = {
        (service.service, dependency)
        for service in topology.services
        for dependency in service.dependencies
    }
    for episode in members:
        if episode.kind is not SymptomKind.EDGE_DEGRADED:
            continue
        if not episode.signal.startswith(dependency_signal_prefix):
            raise ValueError(
                f"edge episode {episode.episode_id} does not name a committed topology dependency"
            )
        dependency = episode.signal.removeprefix(dependency_signal_prefix)
        caller_edge = (episode.service, dependency)
        if not dependency or caller_edge not in committed_edges:
            raise ValueError(f"edge episode {episode.episode_id} does not match committed topology")
        edge_evidence[caller_edge].append(episode.episode_id)

    edges = tuple(
        _edge(
            caller=caller,
            dependency=dependency,
            evidence_episode_ids=tuple(sorted(edge_evidence[(caller, dependency)])),
        )
        for caller, dependency in sorted(committed_edges, key=lambda edge: (edge[1], edge[0]))
    )
    return CausalGraph(
        incident_id=outcome.incident.incident_id,
        incident_state=outcome.incident.state,
        updated_at=outcome.decision.ts,
        honesty=honesty,
        origin_service=outcome.incident.origin_service,
        origin_confidence=outcome.incident.origin_confidence,
        nodes=nodes,
        edges=edges,
    )


def causal_graph_record(graph: CausalGraph) -> IncidentGraphRecord:
    """Encode the graph under its runtime storage identity."""
    return IncidentGraphRecord(
        incident_id=graph.incident_id,
        updated_at=graph.updated_at,
        payload=graph.model_dump(mode="json"),
    )


def causal_graph_snapshot(record: IncidentGraphRecord | None) -> CausalGraphResponse:
    """Strictly revalidate a current graph row before exposing it."""
    if record is None:
        return CausalGraphResponse(
            status="empty",
            graph=None,
            detail="No current incident has an evidence-backed causal graph.",
        )
    try:
        graph = CausalGraph.model_validate_json(
            json.dumps(record.payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
        )
        if graph.incident_id != record.incident_id:
            raise ValueError("stored graph incident id disagrees with its payload")
        if graph.updated_at != record.updated_at:
            raise ValueError("stored graph update time disagrees with its payload")
        if graph.incident_state.value == "RESOLVED":
            raise ValueError("current graph reader returned a resolved incident")
    except (ValidationError, ValueError, TypeError) as exc:
        raise CausalGraphDataError(f"stored causal graph is invalid: {exc}") from exc
    return CausalGraphResponse(status="ready", graph=graph, detail=None)


def unavailable_causal_graph(*, detail: str) -> CausalGraphResponse:
    """Return no partial graph when durable state cannot be trusted."""
    return CausalGraphResponse(
        status="degraded",
        graph=None,
        detail=_line(detail),
    )


def _node(
    *,
    service: str,
    tier: Literal["edge", "application", "data", "infrastructure"],
    criticality: Literal["low", "medium", "high", "critical"],
    episodes: tuple[SymptomEpisode, ...],
    implicated: bool,
    origin_service: str | None,
    origin_confidence: float | None,
) -> CausalGraphNode:
    kinds = tuple(sorted({episode.kind for episode in episodes}, key=lambda kind: kind.value))
    active = sum(episode.status is EpisodeStatus.ACTIVE for episode in episodes)
    heat = max((episode.peak_score for episode in episodes), default=0.0)
    is_origin = service == origin_service
    if implicated:
        note = "Collapsed candidate implicated by dependency evidence; no own symptom was observed."
    elif episodes:
        note = (
            f"{len(episodes)} member symptom episode"
            f"{'' if len(episodes) == 1 else 's'}; {active} currently active."
        )
    else:
        note = "No member symptom episode was observed for this topology service."
    return CausalGraphNode(
        service=service,
        tier=tier,
        criticality=criticality,
        symptom_heat=heat,
        active_episode_count=active,
        symptom_kinds=kinds,
        is_origin=is_origin,
        origin_confidence=origin_confidence if is_origin else None,
        implicated=implicated,
        note=note,
    )


def _edge(
    *,
    caller: str,
    dependency: str,
    evidence_episode_ids: tuple[str, ...],
) -> CausalGraphEdge:
    active = bool(evidence_episode_ids)
    return CausalGraphEdge(
        source_service=dependency,
        target_service=caller,
        active=active,
        evidence_episode_ids=evidence_episode_ids,
        note=(
            "Measured dependency degradation supports propagation from dependency to caller."
            if active
            else "Committed dependency path; no member propagation episode was observed."
        ),
    )


def _line(value: str) -> str:
    rendered = " ".join(value.split())
    if not rendered:
        raise ValueError("causal graph detail cannot be blank")
    return rendered
