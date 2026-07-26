"""Gateway endpoints, exercised without a running stack."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from audit import AuditChain
from common.settings import Settings
from contracts import AuditEntry, AuditEventKind


async def ok() -> None:
    return None


async def refused() -> None:
    raise ConnectionRefusedError("connection refused")


def client_with(**probes: object) -> Iterator[TestClient]:
    app = create_app(config=Settings(), probes=probes)  # type: ignore[arg-type]
    with TestClient(app) as client:
        yield client


@pytest.fixture
def healthy_client() -> Iterator[TestClient]:
    yield from client_with(postgres=ok, loki=ok)


@pytest.fixture
def degraded_client() -> Iterator[TestClient]:
    yield from client_with(postgres=ok, loki=refused)


def test_version_reports_the_build_stamp(healthy_client: TestClient) -> None:
    body = healthy_client.get("/api/version").json()

    assert body["service"] == "sentinel-gateway"
    assert set(body) >= {"version", "git_sha", "short_sha", "built_at", "env"}


def test_health_is_200_when_every_dependency_answers(healthy_client: TestClient) -> None:
    response = healthy_client.get("/api/health")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["degraded"] == []
    assert {c["name"] for c in body["components"]} == {"postgres", "loki"}


def test_health_is_503_and_names_the_degraded_dependency(degraded_client: TestClient) -> None:
    response = degraded_client.get("/api/health")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["degraded"] == ["loki"]


# --- the audit read path ----------------------------------------------------


class _FakeLedger:
    """A ledger the gateway can be tested against without a database."""

    def __init__(self, entries: tuple[AuditEntry, ...]) -> None:
        self._entries = entries

    async def entries(
        self, *, limit: int = 100, after_sequence: int | None = None
    ) -> tuple[AuditEntry, ...]:
        rows = [
            entry
            for entry in self._entries
            if after_sequence is None or entry.sequence > after_sequence
        ]
        return tuple(rows[:limit])


def _ledger_entries(count: int = 3) -> tuple[AuditEntry, ...]:
    chain = AuditChain()
    for index in range(count):
        chain.append(
            ts=datetime(2026, 3, 1, 12, index, tzinfo=UTC),
            kind=AuditEventKind.ACTION_APPLIED,
            actor="sentinel",
            summary=f"acted, step {index}",
            body={"step": index},
            incident_id="incident-1",
        )
    return chain.entries()


def _audit_client(entries: tuple[AuditEntry, ...] | None = None) -> TestClient:
    ledger = _FakeLedger(entries if entries is not None else _ledger_entries())
    return TestClient(create_app(probes={}, ledger=ledger))


def test_the_audit_route_returns_the_chain_in_order() -> None:
    with _audit_client() as client:
        body = client.get("/api/audit").json()

    assert body["count"] == 3
    assert [entry["sequence"] for entry in body["entries"]] == [0, 1, 2]
    assert body["intact"] is True
    assert body["head_hash"] == body["entries"][-1]["entry_hash"]


def test_the_audit_route_re_verifies_what_it_is_about_to_return() -> None:
    """These rows came out of a database somebody could have edited directly."""
    entries = list(_ledger_entries())
    entries[1] = AuditEntry.model_construct(
        **(entries[1].model_dump() | {"summary": "nothing happened here"})
    )

    with _audit_client(tuple(entries)) as client:
        response = client.get("/api/audit")

    assert response.status_code == 409, "a broken chain is not a 200 with a footnote"
    assert response.json()["intact"] is False
    assert response.json()["broken_at"] == 1


def test_the_audit_route_is_bounded_and_pageable() -> None:
    with _audit_client(_ledger_entries(5)) as client:
        first = client.get("/api/audit", params={"limit": 2}).json()
        rest = client.get("/api/audit", params={"after": 1}).json()

    assert [entry["sequence"] for entry in first["entries"]] == [0, 1]
    assert [entry["sequence"] for entry in rest["entries"]] == [2, 3, 4]


def test_a_gateway_with_no_ledger_says_so_rather_than_reporting_an_empty_one() -> None:
    """ "No ledger attached" and "nothing has happened" must not look the same."""
    with TestClient(create_app(probes={})) as client:
        response = client.get("/api/audit")

    assert response.status_code == 503
    assert response.json()["intact"] is False


def test_sensitive_proof_routes_ride_the_gate_even_for_reads() -> None:
    """Action history and an incident proof are sensitive, unlike the public feed."""
    from api.app import _SENSITIVE_PREFIXES

    assert "/api/audit" in _SENSITIVE_PREFIXES
    assert "/api/incidents/" in _SENSITIVE_PREFIXES
