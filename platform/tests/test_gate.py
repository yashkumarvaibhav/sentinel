"""The interim shared-secret gate: reads open, mutations and sensitive routes gated."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.gate import SECRET_HEADER, SharedSecretGate

SECRET = "correct-horse-battery-staple"


def app_with(secret: str) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SharedSecretGate, secret=secret, sensitive_prefixes=("/api/lab",))

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/api/lab/scenario")
    async def fire() -> dict[str, str]:
        return {"fired": "yes"}

    @app.get("/api/lab/secrets")
    async def sensitive_read() -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.fixture
def gated() -> Iterator[TestClient]:
    with TestClient(app_with(SECRET)) as client:
        yield client


@pytest.fixture
def inert() -> Iterator[TestClient]:
    with TestClient(app_with("")) as client:
        yield client


def test_reads_are_open(gated: TestClient) -> None:
    assert gated.get("/api/health").status_code == 200


def test_mutation_without_secret_is_rejected(gated: TestClient) -> None:
    assert gated.post("/api/lab/scenario").status_code == 401


def test_mutation_with_secret_passes(gated: TestClient) -> None:
    response = gated.post("/api/lab/scenario", headers={SECRET_HEADER: SECRET})
    assert response.status_code == 200


def test_wrong_secret_is_rejected(gated: TestClient) -> None:
    response = gated.post("/api/lab/scenario", headers={SECRET_HEADER: "nope"})
    assert response.status_code == 401


def test_sensitive_read_is_gated(gated: TestClient) -> None:
    assert gated.get("/api/lab/secrets").status_code == 401
    ok = gated.get("/api/lab/secrets", headers={SECRET_HEADER: SECRET})
    assert ok.status_code == 200


def test_gate_is_inert_without_a_configured_secret(inert: TestClient) -> None:
    # Local and zero-config runs are unaffected: everything passes.
    assert inert.post("/api/lab/scenario").status_code == 200
    assert inert.get("/api/lab/secrets").status_code == 200
