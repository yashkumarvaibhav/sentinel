"""The current security endpoint preserves empty, ready, and degraded truth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from api.app import create_app
from api.gate import SECRET_HEADER
from common.settings import Settings
from common.storage import IncidentSecurityRecord
from contracts import (
    ActionControlSnapshot,
    IncidentDecomposition,
    IncidentState,
    SecurityFeature,
    SecurityMeasurement,
    SecurityMeasurementStatus,
    SecurityMitigation,
    SecuritySnapshot,
)
from tests.test_action_control import _snapshot as _action_snapshot

TICK = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)


def _measurement(feature: SecurityFeature) -> SecurityMeasurement:
    return SecurityMeasurement(
        feature=feature,
        status=SecurityMeasurementStatus.INSUFFICIENT,
        scope=None,
        value=None,
        baseline=None,
        unit=None,
        window_start=None,
        window_end=None,
        window_count=0,
        evidence_refs=(),
        detail=f"No {feature.value.lower()} evidence exists.",
    )


def _snapshot() -> SecuritySnapshot:
    integrity = _measurement(SecurityFeature.PROTECTED_COHORT_INTEGRITY)
    return SecuritySnapshot(
        incident_id="incident-1",
        opened_at=TICK - timedelta(minutes=2),
        updated_at=TICK,
        state=IncidentState.OPEN,
        honesty="REAL",
        timeline=(),
        measurements=tuple(_measurement(feature) for feature in SecurityFeature),
        suspect_cohorts=(),
        decomposition=IncidentDecomposition(
            status="insufficient",
            service=None,
            signal=None,
            start=TICK - timedelta(minutes=2),
            end=TICK,
            frames=(),
            truncated=False,
            detail="No decision-window decomposition frames were supplied.",
        ),
        mitigation=SecurityMitigation(
            state=None,
            plan_revision=None,
            rung_id=None,
            action_kind=None,
            target_ref=None,
            estimated_blast_fraction=None,
            updated_at=None,
            protected_cohort_integrity=integrity,
            detail="No server-held mitigation plan exists.",
        ),
    )


def _record(snapshot: SecuritySnapshot) -> IncidentSecurityRecord:
    return IncidentSecurityRecord(
        incident_id=snapshot.incident_id,
        updated_at=snapshot.updated_at,
        payload=snapshot.model_dump(mode="json"),
    )


class _Reader:
    def __init__(
        self,
        record: IncidentSecurityRecord | None,
        control: ActionControlSnapshot | None = None,
    ) -> None:
        self.record = record
        self.control = control

    async def latest_security_snapshot(self) -> IncidentSecurityRecord | None:
        return self.record

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


def test_security_endpoint_distinguishes_ready_and_no_current_incident() -> None:
    with TestClient(create_app(probes={}, security_reader=_Reader(_record(_snapshot())))) as client:
        ready = client.get("/api/security")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["snapshot"]["incident_id"] == "incident-1"
    assert ready.json()["snapshot"]["measurements"][2]["status"] == "INSUFFICIENT"
    assert ready.json()["snapshot"]["measurements"][2]["value"] is None

    with TestClient(create_app(probes={}, security_reader=_Reader(None))) as client:
        empty = client.get("/api/security")
    assert empty.status_code == 200
    assert empty.json()["status"] == "empty"
    assert empty.json()["snapshot"] is None


def test_security_endpoint_fails_closed_on_corrupt_or_missing_store() -> None:
    with TestClient(create_app(probes={})) as client:
        unavailable = client.get("/api/security")
    assert unavailable.status_code == 503
    assert unavailable.json()["status"] == "degraded"

    corrupt = _record(_snapshot()).model_copy(
        update={
            "payload": _snapshot().model_dump(mode="json") | {"incident_id": "different-incident"}
        }
    )
    with TestClient(create_app(probes={}, security_reader=_Reader(corrupt))) as client:
        invalid = client.get("/api/security")
    assert invalid.status_code == 503
    assert invalid.json()["status"] == "degraded"
    assert invalid.json()["snapshot"] is None


def test_security_endpoint_overlays_only_the_latest_server_held_mitigation() -> None:
    reader = _Reader(_record(_snapshot()), control=_action_snapshot())
    with TestClient(create_app(probes={}, security_reader=reader)) as client:
        response = client.get("/api/security")

    assert response.status_code == 200
    mitigation = response.json()["snapshot"]["mitigation"]
    assert mitigation["plan_revision"] == 3
    assert mitigation["rung_id"] == "rate-limit-invalid-credentials"
    assert mitigation["target_ref"] == "route/login"
    assert mitigation["state"] == "AWAITING_APPROVAL"
    assert mitigation["protected_cohort_integrity"]["status"] == "INSUFFICIENT"


def test_security_evidence_is_behind_the_interim_sensitive_read_gate() -> None:
    config = Settings(SENTINEL_SHARED_SECRET="security-secret")
    reader = _Reader(_record(_snapshot()))
    with TestClient(create_app(config=config, probes={}, security_reader=reader)) as client:
        denied = client.get("/api/security")
        allowed = client.get(
            "/api/security",
            headers={SECRET_HEADER: "security-secret"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ready"
