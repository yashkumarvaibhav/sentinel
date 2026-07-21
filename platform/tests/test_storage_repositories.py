"""Fast storage-boundary tests that do not require live databases."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from common.storage import AuditRecord, ClickHouseRepository, IncidentRecord
from contracts import Observation


def test_storage_records_reject_invalid_time_and_hash_boundaries() -> None:
    ts = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="updated_at"):
        IncidentRecord(
            incident_id="incident-1",
            state="open",
            created_at=ts,
            updated_at=ts - timedelta(seconds=1),
            payload={},
        )

    with pytest.raises(ValidationError, match="timestamp"):
        AuditRecord(
            entry_id="audit-1",
            ts=ts.replace(tzinfo=None),
            event_type="incident.opened",
            payload={},
            prev_hash="0" * 64,
            entry_hash="1" * 64,
        )

    with pytest.raises(ValidationError, match="entry_hash"):
        AuditRecord(
            entry_id="audit-1",
            ts=ts,
            event_type="incident.opened",
            payload={},
            prev_hash="0" * 64,
            entry_hash="not-a-sha256",
        )


def test_clickhouse_reads_use_parameters_and_decode_quoted_int64_timestamps() -> None:
    asyncio.run(_exercise_parameterized_clickhouse_read())


async def _exercise_parameterized_clickhouse_read() -> None:
    record_id = "obs-' OR 1=1"
    ts = datetime(2026, 7, 21, 12, 0, 0, 123456, tzinfo=UTC)
    expected = Observation(
        observation_id=record_id,
        ts=ts,
        service="frontend",
        signal="request_rate",
        value=3.5,
        attributes={"status": 200},
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        query = request.content.decode()
        assert "{record_id:String}" in query
        assert record_id not in query
        assert request.url.params["param_record_id"] == record_id
        row = {
            "observation_id": record_id,
            "ts_us": "1784635200123456",
            "service": "frontend",
            "signal": "request_rate",
            "value": 3.5,
            "unit": "1",
            "attributes_json": json.dumps({"status": 200}),
            "flow_refs": [],
            "log_refs": [],
            "trace_refs": [],
        }
        return httpx.Response(200, text=json.dumps(row) + "\n")

    async with httpx.AsyncClient(
        base_url="http://clickhouse:8123", transport=httpx.MockTransport(handler)
    ) as client:
        repository = ClickHouseRepository(client=client, database="sentinel_test")
        assert await repository.get_observation(record_id) == expected


def test_clickhouse_rejects_unsafe_database_and_invalid_ranges() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(
            base_url="http://clickhouse:8123",
            transport=httpx.MockTransport(_unexpected_request),
        ) as client:
            with pytest.raises(ValueError, match="database"):
                ClickHouseRepository(client=client, database="sentinel; DROP DATABASE sentinel")

            repository = ClickHouseRepository(client=client, database="sentinel")
            await repository.write_observations(())
            with pytest.raises(ValueError, match="end"):
                await repository.list_observations(
                    service="frontend",
                    signal="request_rate",
                    start=datetime(2026, 7, 21, 12, 1, tzinfo=UTC),
                    end=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
                )

    asyncio.run(exercise())


def _unexpected_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")
