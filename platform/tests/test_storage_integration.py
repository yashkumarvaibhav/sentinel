"""Real ClickHouse and Postgres repository round trips against the compose stack."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from psycopg import sql

from common.config import DetectorConfig
from common.settings import Settings
from common.storage import (
    AuditRecord,
    ClickHouseRepository,
    IncidentRecord,
    PostgresPool,
    PostgresRepository,
    create_postgres_pool,
)
from common.storage._clickhouse import execute as clickhouse_execute
from common.storage.dev_labels import DevLabelRecord, DevLabelRepository
from common.storage.migrations import migrate_storage
from contracts import ContextWindow, DecompFrame, Observation, Symptom, SymptomKind
from detection.decompose import DecompositionEngine, DecompositionWorker
from tests.factories import (
    behavioral_ratio_config,
    change_point_saturation_config,
    log_template_config,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("SENTINEL_STORAGE_INTEGRATION") != "1",
        reason="set SENTINEL_STORAGE_INTEGRATION=1 inside the compose network",
    ),
]


def test_real_storage_round_trips_and_idempotent_writes() -> None:
    asyncio.run(_exercise_real_storage())


async def _exercise_real_storage() -> None:
    suffix = uuid4().hex[:12]
    config = Settings().model_copy(
        update={
            "clickhouse_database": f"sentinel_test_{suffix}",
            "postgres_schema": f"sentinel_test_{suffix}",
            "postgres_dev_schema": f"sentinel_test_{suffix}_dev",
            "storage_pool_min_size": 1,
            "storage_pool_max_size": 2,
        }
    )
    pool = create_postgres_pool(config)
    auth = (config.clickhouse_user, config.clickhouse_password.get_secret_value())
    async with httpx.AsyncClient(base_url=config.clickhouse_url, auth=auth) as client:
        await pool.open(wait=True)
        try:
            await migrate_storage(config, clickhouse_client=client, postgres_pool=pool)
            await migrate_storage(config, clickhouse_client=client, postgres_pool=pool)
            await _round_trip_clickhouse(config, client, suffix)
            await _round_trip_postgres(config, pool, suffix)
        finally:
            await _drop_test_storage(config, client, pool)
            await pool.close()


async def _round_trip_clickhouse(config: Settings, client: httpx.AsyncClient, suffix: str) -> None:
    repository = ClickHouseRepository(client=client, database=config.clickhouse_database)
    ts = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    observation = Observation(
        observation_id=f"obs-{suffix}",
        ts=ts,
        service="frontend",
        signal="request_rate",
        value=412.5,
        unit="requests/s",
        attributes={"http.request.method": "GET", "http.response.status_code": 200},
        flow_refs=("flow-1",),
        log_refs=("log-1",),
        trace_refs=("trace-1",),
    )
    context = ContextWindow(
        context_id=f"event-{suffix}",
        name="Cup final",
        event_type="sports_fixture",
        source="fixtures-api",
        honesty="REAL",
        valid_from=ts - timedelta(minutes=30),
        valid_to=ts + timedelta(hours=3),
        expected_delta={"frontend.request_rate": 3.0},
        trust_score=0.95,
    )
    frame = DecompFrame(
        frame_id=f"frame-{suffix}",
        observation_id=observation.observation_id,
        ts=ts,
        service=observation.service,
        signal=observation.signal,
        observed=412.5,
        explained_base=100.0,
        explained_event=300.0,
        residual=12.5,
        band_low=380.0,
        band_high=420.0,
        residual_score=0.2,
        context_ids=(context.context_id,),
    )
    symptom = Symptom(
        symptom_id=f"symptom-{suffix}",
        kind=SymptomKind.RESIDUAL_EXCEED,
        service=observation.service,
        signal=observation.signal,
        onset_ts=ts,
        score=0.84,
        note="Residual exceeded the context-aware upper band.",
        evidence_refs=(frame.frame_id,),
    )

    await repository.write_observations((observation, observation))
    await repository.write_decomp_frames((frame, frame))
    await repository.write_symptoms((symptom, symptom))

    assert await repository.get_observation(observation.observation_id) == observation
    assert await repository.get_decomp_frame(frame.frame_id) == frame
    assert await repository.get_symptom(symptom.symptom_id) == symptom
    listed = await repository.list_observations(
        service="frontend",
        signal="request_rate",
        start=ts - timedelta(seconds=1),
        end=ts + timedelta(seconds=1),
    )
    assert listed == (observation,)

    detector = DetectorConfig(
        version=1,
        feature_window_seconds=60,
        watermark_lateness_seconds=15,
        ewma_alpha=0.15,
        baseline_warmup_points=3,
        baseline_update_gate_ratio=0.25,
        expected_band_relative_tolerance=0.1,
        absolute_noise_floors={"frontend.request_rate": 1.0},
        behavioral_ratios=behavioral_ratio_config(),
        log_templates=log_template_config(),
        change_point_saturation=change_point_saturation_config(),
    )
    engine = DecompositionEngine(configuration=detector, dedup_capacity=100)
    worker = DecompositionWorker(engine=engine, sink=repository)
    for index, value in enumerate((99.0, 100.0, 101.0)):
        result = engine.decompose(
            observation.model_copy(
                update={
                    "observation_id": f"warm-{suffix}-{index}",
                    "ts": ts + timedelta(seconds=index + 1),
                    "value": value,
                }
            )
        )
        assert result.frame is None

    persisted = []
    for index, value in enumerate((102.0, 103.0)):
        result = await worker.handle(
            observation.model_copy(
                update={
                    "observation_id": f"decompose-{suffix}-{index}",
                    "ts": ts + timedelta(seconds=index + 10),
                    "value": value,
                }
            )
        )
        assert result.frame is not None
        persisted.append(result.frame)

    assert persisted[0].frame_id != persisted[1].frame_id
    for persisted_frame in persisted:
        assert await repository.get_decomp_frame(persisted_frame.frame_id) == persisted_frame


async def _round_trip_postgres(config: Settings, pool: PostgresPool, suffix: str) -> None:
    repository = PostgresRepository(pool=pool, schema=config.postgres_schema)
    labels = DevLabelRepository(pool=pool, schema=config.postgres_dev_schema)
    ts = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    incident = IncidentRecord(
        incident_id=f"incident-{suffix}",
        state="open",
        created_at=ts,
        updated_at=ts,
        payload={"service": "frontend", "severity": 2, "confirmed": False},
    )
    audit = AuditRecord(
        entry_id=f"audit-{suffix}",
        ts=ts,
        event_type="incident.opened",
        payload={"incident_id": incident.incident_id},
        prev_hash="0" * 64,
        entry_hash="1" * 64,
    )
    label = DevLabelRecord(
        label_id=f"label-{suffix}",
        run_id=f"run-{suffix}",
        subject_id=incident.incident_id,
        kind="expected_verdict",
        payload={"verdict": "ATTACK"},
        created_at=ts,
    )

    await repository.put_incident(incident)
    await repository.put_incident(incident)
    assert await repository.append_audit(audit) is True
    assert await repository.append_audit(audit) is False
    assert await labels.put(label) is True
    assert await labels.put(label) is False

    assert await repository.get_incident(incident.incident_id) == incident
    assert await repository.get_audit(audit.entry_id) == audit
    assert await labels.get(label.label_id) == label


async def _drop_test_storage(
    config: Settings, client: httpx.AsyncClient, pool: PostgresPool
) -> None:
    await clickhouse_execute(client, f"DROP DATABASE IF EXISTS {config.clickhouse_database}")
    async with pool.connection() as connection:
        await connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(config.postgres_dev_schema)
            )
        )
        await connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(config.postgres_schema)
            )
        )
