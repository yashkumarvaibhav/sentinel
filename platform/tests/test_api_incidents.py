"""The REST snapshot behind the incident invalidation channel."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app
from api.incidents import observation_freshness
from common.storage import IncidentRecord, LiveProducerCheckpoint
from contracts import (
    DecisionAction,
    IncidentActionState,
    IncidentConfidence,
    IncidentConfidenceStatus,
    IncidentFeedItem,
    IncidentSeverity,
    IncidentState,
    ObservationFreshness,
    ObservationStatus,
    VerdictClass,
)

TICK = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)


def _item(incident_id: str, updated_at: datetime) -> IncidentFeedItem:
    return IncidentFeedItem(
        incident_id=incident_id,
        opened_at=updated_at - timedelta(minutes=2),
        updated_at=updated_at,
        state=IncidentState.OPEN,
        severity=IncidentSeverity.HIGH,
        services=("frontend",),
        origin_service="frontend",
        verdict_class=VerdictClass.ATTACK,
        reason="credential failures concentrated into machine-regular sources",
        evidence=(),
        action=IncidentActionState(
            decision_action=DecisionAction.ALERT,
            effect_status=None,
            detail="Alert raised; no production effect was requested.",
        ),
        confidence=IncidentConfidence(
            status=IncidentConfidenceStatus.INSUFFICIENT,
            value=None,
            note="No calibrated runtime confidence is attached.",
        ),
        muted=False,
        explanation=None,
        honesty="REAL",
    )


def _record(item: IncidentFeedItem) -> IncidentRecord:
    return IncidentRecord(
        incident_id=item.incident_id,
        state=item.state.value,
        created_at=item.opened_at,
        updated_at=item.updated_at,
        payload=item.model_dump(mode="json"),
    )


class _Reader:
    def __init__(
        self,
        records: Sequence[IncidentRecord],
        checkpoint: LiveProducerCheckpoint | None = None,
    ) -> None:
        self.records = tuple(records)
        self.limits: list[int] = []
        self.checkpoint = checkpoint

    async def list_incidents(self, *, limit: int) -> tuple[IncidentRecord, ...]:
        self.limits.append(limit)
        return self.records[:limit]

    async def get_incident(self, incident_id: str) -> IncidentRecord | None:
        return next((record for record in self.records if record.incident_id == incident_id), None)

    async def get_live_producer_checkpoint(self, producer_id: str) -> LiveProducerCheckpoint | None:
        return self.checkpoint


def test_a_feed_with_no_producer_says_it_has_never_observed_anything() -> None:
    freshness = observation_freshness(None, now=TICK, expected_within_seconds=120.0)

    assert freshness.status is ObservationStatus.NEVER
    assert freshness.last_judged_at is None
    assert "not evidence that nothing is wrong" in freshness.note


def test_a_producer_that_stopped_is_reported_as_not_watching() -> None:
    checkpoint = LiveProducerCheckpoint(
        producer_id="live-producer",
        anchor_ts=TICK - timedelta(hours=2),
        tick_ts=TICK - timedelta(minutes=30),
        published_incidents=3,
    )

    freshness = observation_freshness(checkpoint, now=TICK, expected_within_seconds=120.0)

    assert freshness.status is ObservationStatus.STALE
    assert freshness.age_seconds == 1800.0
    assert "the last state measured, not the current one" in freshness.note


def test_a_producer_judging_now_is_reported_as_watching() -> None:
    checkpoint = LiveProducerCheckpoint(
        producer_id="live-producer",
        anchor_ts=TICK - timedelta(hours=2),
        tick_ts=TICK - timedelta(seconds=4),
        published_incidents=3,
    )

    freshness = observation_freshness(checkpoint, now=TICK, expected_within_seconds=120.0)

    assert freshness.status is ObservationStatus.WATCHING
    assert freshness.age_seconds == 4.0


def test_a_stale_producer_cannot_be_dressed_up_as_watching() -> None:
    """The status is derived from the measured age, and the contract enforces it."""
    with pytest.raises(ValidationError, match="follow its own measured age"):
        ObservationFreshness(
            status=ObservationStatus.WATCHING,
            last_judged_at=TICK - timedelta(hours=1),
            age_seconds=3600.0,
            expected_within_seconds=120.0,
            note="all is well",
        )


def test_incident_snapshot_is_bounded_latest_first_and_strictly_revalidated() -> None:
    latest = _item("latest", TICK)
    older = _item("older", TICK - timedelta(minutes=1))
    reader = _Reader((_record(latest), _record(older)))

    with TestClient(create_app(probes={}, incident_reader=reader)) as client:
        body = client.get("/api/incidents", params={"limit": 10_000}).json()

    assert reader.limits == [50]
    assert body["status"] == "ready"
    assert body["count"] == 2
    assert body["limit"] == 50
    assert [item["incident_id"] for item in body["incidents"]] == ["latest", "older"]


def test_no_store_and_corrupt_runtime_payload_fail_closed() -> None:
    with TestClient(create_app(probes={})) as client:
        unavailable = client.get("/api/incidents")

    assert unavailable.status_code == 503
    assert unavailable.json()["status"] == "degraded"
    assert unavailable.json()["incidents"] == []

    item = _item("corrupt", TICK)
    corrupt = _record(item).model_copy(update={"payload": {"incident_id": "corrupt"}})
    with TestClient(create_app(probes={}, incident_reader=_Reader((corrupt,)))) as client:
        invalid = client.get("/api/incidents")

    assert invalid.status_code == 503
    assert invalid.json()["status"] == "degraded"
    assert invalid.json()["incidents"] == []
