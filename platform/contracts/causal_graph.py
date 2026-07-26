"""Public current-incident causal graph contracts.

The graph is materialized from the full decision outcome while its member
episodes are still available. It is not reconstructed from the compact
incident card: that projection intentionally omits the evidence required to
distinguish passive topology from measured propagation.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from contracts._base import (
    ContractModel,
    HumanText,
    Identifier,
    Probability,
    UtcDatetime,
    ensure_unique,
)
from contracts.decision import IncidentState
from contracts.detection import SymptomKind


class CausalGraphNode(ContractModel):
    """One committed topology service with evidence-derived incident heat."""

    service: Identifier
    tier: Literal["edge", "application", "data", "infrastructure"]
    criticality: Literal["low", "medium", "high", "critical"]
    symptom_heat: Probability
    active_episode_count: int = Field(ge=0)
    symptom_kinds: tuple[SymptomKind, ...]
    is_origin: bool
    origin_confidence: Probability | None
    implicated: bool
    note: HumanText

    @model_validator(mode="after")
    def coherent_node(self) -> Self:
        ensure_unique(self.symptom_kinds, field_name="causal graph node symptom kinds")
        if self.is_origin != (self.origin_confidence is not None):
            raise ValueError("only the collapsed origin may carry origin confidence")
        if not self.symptom_kinds and (self.symptom_heat != 0.0 or self.active_episode_count != 0):
            raise ValueError("a node without member symptoms cannot carry heat or active episodes")
        if self.active_episode_count > 0 and not self.symptom_kinds:
            raise ValueError("active episodes require at least one member symptom kind")
        if self.implicated and (
            self.symptom_kinds or self.symptom_heat != 0.0 or self.active_episode_count != 0
        ):
            raise ValueError("an implicated node cannot also claim its own symptom evidence")
        return self


class CausalGraphEdge(ContractModel):
    """One topology edge rendered in possible propagation direction.

    Configuration says ``caller depends on dependency``. This public edge is
    deliberately reversed to ``dependency -> caller`` so its arrow answers the
    incident-response question: where could an observed effect propagate?
    """

    source_service: Identifier
    target_service: Identifier
    active: bool
    evidence_episode_ids: tuple[Identifier, ...]
    note: HumanText

    @model_validator(mode="after")
    def coherent_edge(self) -> Self:
        if self.source_service == self.target_service:
            raise ValueError("a causal graph edge cannot point to itself")
        ensure_unique(
            self.evidence_episode_ids,
            field_name="causal graph edge evidence episode ids",
        )
        if self.active != bool(self.evidence_episode_ids):
            raise ValueError("only an evidence-backed propagation edge may be active")
        return self


class CausalGraph(ContractModel):
    """One durable graph revision for an incident.

    Resolved revisions remain valid durable state so closing an incident can
    advance both records atomically. The repository's current-graph query
    filters them out.
    """

    incident_id: Identifier
    incident_state: IncidentState
    updated_at: UtcDatetime
    honesty: Literal["REAL", "SIMULATED"]
    origin_service: Identifier | None
    origin_confidence: Probability | None
    nodes: tuple[CausalGraphNode, ...] = Field(min_length=1)
    edges: tuple[CausalGraphEdge, ...]

    @model_validator(mode="after")
    def coherent_graph(self) -> Self:
        ensure_unique(
            tuple(node.service for node in self.nodes),
            field_name="causal graph services",
        )
        edge_ids = tuple((edge.source_service, edge.target_service) for edge in self.edges)
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("causal graph edges must be unique")
        services = {node.service for node in self.nodes}
        for edge in self.edges:
            if edge.source_service not in services or edge.target_service not in services:
                raise ValueError("every causal graph edge endpoint must be a graph node")

        origin_nodes = tuple(node for node in self.nodes if node.is_origin)
        if (self.origin_service is None) != (self.origin_confidence is None):
            raise ValueError("causal graph origin service and confidence must appear together")
        if self.origin_service is None:
            if origin_nodes:
                raise ValueError("a graph without a collapsed origin cannot mark an origin node")
        elif (
            len(origin_nodes) != 1
            or origin_nodes[0].service != self.origin_service
            or origin_nodes[0].origin_confidence != self.origin_confidence
        ):
            raise ValueError("the collapsed origin must match exactly one graph node")
        return self


class CausalGraphResponse(ContractModel):
    """The authoritative current-incident graph or an explicit absence."""

    status: Literal["ready", "empty", "degraded"]
    graph: CausalGraph | None
    detail: HumanText | None

    @model_validator(mode="after")
    def coherent_response(self) -> Self:
        if self.status == "ready" and (self.graph is None or self.detail is not None):
            raise ValueError("a ready causal graph response requires only a graph")
        if self.status in {"empty", "degraded"} and (self.graph is not None or self.detail is None):
            raise ValueError("an empty or degraded causal graph response requires only detail")
        return self
