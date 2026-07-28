"""Sensitive action-control REST routes over authoritative durable state."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from action.control import transition_action_control
from api.app import create_app
from api.gate import SECRET_HEADER
from api.stream import StreamBroker
from common.settings import Settings
from contracts import (
    ActionControlIntent,
    ActionControlRequest,
    ActionControlSnapshot,
    SnapshotInvalidation,
    SnapshotResource,
)
from tests.test_action_control import _snapshot

TS = datetime(2026, 7, 28, 11, 0, tzinfo=UTC)


class _Store:
    def __init__(self, control: ActionControlSnapshot | None) -> None:
        self.control = control
        self.transitions: list[tuple[ActionControlRequest, str]] = []

    async def get_action_control(
        self,
        incident_id: str,
        *,
        plan_revision: int | None = None,
    ) -> ActionControlSnapshot | None:
        if (
            self.control is None
            or self.control.incident_id != incident_id
            or (plan_revision is not None and self.control.plan_revision != plan_revision)
        ):
            return None
        return self.control

    async def transition_action_control(
        self,
        request: ActionControlRequest,
        *,
        actor: str,
        ts: datetime,
    ) -> tuple[bool, ActionControlSnapshot]:
        if self.control is None:
            raise AssertionError("missing controls are not transitioned")
        self.transitions.append((request, actor))
        transitioned = transition_action_control(
            self.control,
            request,
            actor=actor,
            ts=ts,
        )
        changed = transitioned != self.control
        self.control = transitioned
        return changed, transitioned


class _Broker(StreamBroker):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[SnapshotInvalidation] = []

    def publish(self, event: SnapshotInvalidation) -> int:
        self.events.append(event)
        return super().publish(event)


def test_action_control_get_distinguishes_ready_and_not_found() -> None:
    store = _Store(_snapshot())
    with TestClient(create_app(probes={}, action_control_store=store)) as client:
        ready = client.get("/api/incidents/incident-1/action")
        missing = client.get("/api/incidents/missing/action")

    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["control"]["plan"]["target_ref"] == "route/login"
    assert missing.status_code == 404
    assert missing.json() == {
        "status": "not_found",
        "control": None,
        "message": "no action plan exists for incident missing",
    }


def test_mutation_is_secret_gated_and_identity_is_server_bound() -> None:
    store = _Store(_snapshot())
    broker = _Broker()
    config = Settings(
        SENTINEL_SHARED_SECRET="one-secret",
        SENTINEL_INTERIM_OPERATOR_ID="on-call-primary",
    )
    body = {
        "incident_id": "incident-1",
        "plan_revision": 3,
        "intent": "REJECT",
    }
    with TestClient(
        create_app(
            config=config,
            probes={},
            stream_broker=broker,
            action_control_store=store,
        )
    ) as client:
        denied = client.post("/api/incidents/incident-1/action", json=body)
        accepted = client.post(
            "/api/incidents/incident-1/action",
            headers={SECRET_HEADER: "one-secret"},
            json=body,
        )

    assert denied.status_code == 401
    assert len(store.transitions) == 1
    assert store.transitions[0][1] == "on-call-primary"
    assert accepted.status_code == 200
    assert accepted.json()["control"]["state"] == "REJECTED"
    assert accepted.json()["control"]["rejected_by"] == "on-call-primary"
    assert broker.events[-1].resources == (
        SnapshotResource.INCIDENTS,
        SnapshotResource.ACTIONS,
    )


def test_route_rejects_client_plan_fields_path_mismatch_and_stale_revision() -> None:
    store = _Store(_snapshot())
    with TestClient(create_app(probes={}, action_control_store=store)) as client:
        unsafe = client.post(
            "/api/incidents/incident-1/action",
            json={
                "incident_id": "incident-1",
                "plan_revision": 3,
                "intent": "APPROVE",
                "target_ref": "deployment/attacker-choice",
            },
        )
        mismatch = client.post(
            "/api/incidents/incident-1/action",
            json={
                "incident_id": "different-incident",
                "plan_revision": 3,
                "intent": "APPROVE",
            },
        )
        stale = client.post(
            "/api/incidents/incident-1/action",
            json={
                "incident_id": "incident-1",
                "plan_revision": 2,
                "intent": "APPROVE",
            },
        )

    assert unsafe.status_code == 422
    assert mismatch.status_code == 400
    assert stale.status_code == 409
    assert store.transitions[-1][0].intent is ActionControlIntent.APPROVE
