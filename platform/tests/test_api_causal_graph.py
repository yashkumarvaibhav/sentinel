"""The strict current-incident causal graph snapshot."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from api.app import create_app
from common.storage import IncidentGraphRecord
from contracts import (
    CausalGraph,
    CausalGraphEdge,
    CausalGraphNode,
    IncidentState,
    SymptomKind,
)

TICK = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)


def _graph() -> CausalGraph:
    return CausalGraph(
        incident_id="incident-live-1",
        incident_state=IncidentState.OPEN,
        updated_at=TICK,
        honesty="REAL",
        origin_service="payment",
        origin_confidence=0.88,
        nodes=(
            CausalGraphNode(
                service="checkout",
                tier="application",
                criticality="critical",
                symptom_heat=0.81,
                active_episode_count=1,
                symptom_kinds=(SymptomKind.EDGE_DEGRADED,),
                is_origin=False,
                origin_confidence=None,
                implicated=False,
                note="One active edge degradation episode.",
            ),
            CausalGraphNode(
                service="payment",
                tier="application",
                criticality="critical",
                symptom_heat=0.0,
                active_episode_count=0,
                symptom_kinds=(),
                is_origin=True,
                origin_confidence=0.88,
                implicated=True,
                note="Collapsed origin implicated by dependency evidence.",
            ),
        ),
        edges=(
            CausalGraphEdge(
                source_service="payment",
                target_service="checkout",
                active=True,
                evidence_episode_ids=("edge-1",),
                note="Measured propagation from dependency to caller.",
            ),
        ),
    )


def _record(graph: CausalGraph) -> IncidentGraphRecord:
    return IncidentGraphRecord(
        incident_id=graph.incident_id,
        updated_at=graph.updated_at,
        payload=graph.model_dump(mode="json"),
    )


class _Reader:
    def __init__(self, record: IncidentGraphRecord | None) -> None:
        self.record = record

    async def latest_incident_graph(self) -> IncidentGraphRecord | None:
        return self.record


def test_causal_graph_endpoint_distinguishes_ready_and_no_current_incident() -> None:
    with TestClient(
        create_app(probes={}, causal_graph_reader=_Reader(_record(_graph())))
    ) as client:
        ready = client.get("/api/causal-graph")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["graph"]["origin_service"] == "payment"

    with TestClient(create_app(probes={}, causal_graph_reader=_Reader(None))) as client:
        empty = client.get("/api/causal-graph")
    assert empty.status_code == 200
    assert empty.json()["status"] == "empty"
    assert empty.json()["graph"] is None
    assert "current" in empty.json()["detail"].lower()


def test_causal_graph_endpoint_fails_closed_on_corrupt_or_missing_store() -> None:
    with TestClient(create_app(probes={})) as client:
        unavailable = client.get("/api/causal-graph")
    assert unavailable.status_code == 503
    assert unavailable.json()["status"] == "degraded"

    corrupt = _record(_graph()).model_copy(
        update={"payload": _graph().model_dump(mode="json") | {"origin_service": "unknown"}}
    )
    with TestClient(create_app(probes={}, causal_graph_reader=_Reader(corrupt))) as client:
        invalid = client.get("/api/causal-graph")
    assert invalid.status_code == 503
    assert invalid.json()["status"] == "degraded"
    assert invalid.json()["graph"] is None
