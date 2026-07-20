"""Gateway endpoints, exercised without a running stack."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from common.settings import Settings


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
