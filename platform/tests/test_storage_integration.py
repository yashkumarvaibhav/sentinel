"""Real ClickHouse and Postgres repository round trips against the compose stack."""

from __future__ import annotations

import asyncio
import math
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from psycopg import sql

from audit import verify_chain
from common.config import DetectorConfig
from common.settings import Settings
from common.storage import (
    AuditLedgerRepository,
    AuditRecord,
    ClickHouseRepository,
    IncidentGraphRecord,
    IncidentMemoryRepository,
    IncidentRecord,
    IncidentSignatureRecord,
    PostgresPool,
    PostgresRepository,
    create_postgres_pool,
)
from common.storage._clickhouse import execute as clickhouse_execute
from common.storage.dev_labels import DevLabelRecord, DevLabelRepository
from common.storage.migrations import migrate_storage
from contracts import (
    AuditEventKind,
    ContextWindow,
    DecompFrame,
    EpisodeStatus,
    Observation,
    Symptom,
    SymptomEpisode,
    SymptomKind,
)
from decision.memory import similarity_from_distance
from detection.decompose import DecompositionEngine, DecompositionWorker
from detection.pipeline import EpisodeWorker, SymptomEpisodePipeline
from tests.factories import (
    behavioral_ratio_config,
    change_point_saturation_config,
    edge_degradation_config,
    episode_config,
    liveness_config,
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
            await _round_trip_incident_memory(config, pool, suffix)
            await _exercise_audit_ledger(config, pool)
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
        liveness=liveness_config(),
        edge_degradation=edge_degradation_config(),
        episodes=episode_config(),
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

    assert await repository.put_incident(incident) is True
    assert await repository.put_incident(incident) is False
    advanced_incident = incident.model_copy(
        update={
            "state": "monitoring",
            "updated_at": ts + timedelta(seconds=1),
            "payload": incident.payload | {"confirmed": True},
        }
    )
    assert await repository.put_incident(advanced_incident) is True
    assert await repository.put_incident(incident) is False
    assert await repository.append_audit(audit) is True
    assert await repository.append_audit(audit) is False
    assert await labels.put(label) is True
    assert await labels.put(label) is False

    assert await repository.get_incident(incident.incident_id) == advanced_incident
    assert await repository.list_incidents(limit=20) == (advanced_incident,)
    assert await repository.get_audit(audit.entry_id) == audit
    assert await labels.get(label.label_id) == label

    await _round_trip_incident_graph_bundle(config, repository, pool, ts, suffix)
    await _round_trip_episode(repository, ts, suffix)
    await _round_trip_pipeline_episode(repository, ts, suffix)


async def _round_trip_incident_graph_bundle(
    config: Settings,
    repository: PostgresRepository,
    pool: PostgresPool,
    ts: datetime,
    suffix: str,
) -> None:
    incident = IncidentRecord(
        incident_id=f"graph-incident-{suffix}",
        state="OPEN",
        created_at=ts,
        updated_at=ts + timedelta(seconds=10),
        payload={"incident_id": f"graph-incident-{suffix}", "state": "OPEN"},
    )
    graph = IncidentGraphRecord(
        incident_id=incident.incident_id,
        updated_at=incident.updated_at,
        payload={
            "incident_id": incident.incident_id,
            "incident_state": "OPEN",
            "updated_at": incident.updated_at.isoformat(),
        },
    )
    assert await repository.put_incident_bundle(incident, graph) is True
    assert await repository.put_incident_bundle(incident, graph) is False
    assert await repository.latest_incident_graph() == graph

    # Make only the graph artificially newer, then prove the bundle refuses to
    # advance half of the pair and rolls its incident write back.
    future_graph_time = ts + timedelta(seconds=12)
    table = sql.Identifier(config.postgres_schema, "incident_causal_graphs")
    async with pool.connection() as connection:
        await connection.execute(
            sql.SQL("UPDATE {} SET updated_at = %s WHERE incident_id = %s").format(table),
            (future_graph_time, incident.incident_id),
        )
    half_advance = incident.model_copy(
        update={
            "state": "MONITORING",
            "updated_at": ts + timedelta(seconds=11),
            "payload": incident.payload | {"state": "MONITORING"},
        }
    )
    half_graph = graph.model_copy(
        update={
            "updated_at": half_advance.updated_at,
            "payload": graph.payload
            | {
                "incident_state": "MONITORING",
                "updated_at": half_advance.updated_at.isoformat(),
            },
        }
    )
    with pytest.raises(RuntimeError, match="could not advance"):
        await repository.put_incident_bundle(half_advance, half_graph)
    assert await repository.get_incident(incident.incident_id) == incident

    resolved = incident.model_copy(
        update={
            "state": "RESOLVED",
            "updated_at": ts + timedelta(seconds=13),
            "payload": incident.payload | {"state": "RESOLVED"},
        }
    )
    resolved_graph = graph.model_copy(
        update={
            "updated_at": resolved.updated_at,
            "payload": graph.payload
            | {
                "incident_state": "RESOLVED",
                "updated_at": resolved.updated_at.isoformat(),
            },
        }
    )
    assert await repository.put_incident_bundle(resolved, resolved_graph) is True
    assert await repository.latest_incident_graph() is None


async def _round_trip_pipeline_episode(
    repository: PostgresRepository, ts: datetime, suffix: str
) -> None:
    """Drive real decomposition frames through the pipeline into real Postgres."""
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
        liveness=liveness_config(),
        edge_degradation=edge_degradation_config(),
        episodes=episode_config(open_after_ticks=3, close_after_ticks=3),
    )
    engine = DecompositionEngine(configuration=detector, dedup_capacity=64)
    pipeline = SymptomEpisodePipeline(configuration=detector.episodes)
    worker = EpisodeWorker(pipeline=pipeline, sink=repository)

    values = [4.0, 4.0, 4.0] + [16.0] * 4 + [4.0] * 3
    closed = None
    for index, value in enumerate(values):
        observation = Observation(
            observation_id=f"pipe-{suffix}-{index}",
            ts=ts + timedelta(seconds=2 * index),
            service="frontend",
            signal="request_rate",
            value=value,
            unit="requests/s",
        )
        result = engine.decompose(observation)
        if result.frame is None:
            continue
        transition = await worker.handle_frame(result.frame)
        if transition.episode is not None and transition.episode.status is EpisodeStatus.CLOSED:
            closed = transition.episode

    assert closed is not None
    assert await repository.get_episode(closed.episode_id) == closed


async def _round_trip_episode(repository: PostgresRepository, ts: datetime, suffix: str) -> None:
    episode = SymptomEpisode(
        episode_id=f"episode-{suffix}",
        kind=SymptomKind.RESIDUAL_EXCEED,
        service="frontend",
        signal="request_rate",
        status=EpisodeStatus.ACTIVE,
        opened_ts=ts,
        confirmed_ts=ts + timedelta(seconds=2),
        last_breach_ts=ts + timedelta(seconds=2),
        peak_score=0.9,
        breach_tick_count=3,
        revision=1,
        opening_symptom_id=f"symptom-{suffix}-0",
        peak_symptom_id=f"symptom-{suffix}-0",
        latest_symptom_id=f"symptom-{suffix}-2",
        evidence_refs=(f"frame-{suffix}-0",),
    )
    advanced = episode.model_copy(
        update={
            "status": EpisodeStatus.CLOSED,
            "last_breach_ts": ts + timedelta(seconds=2),
            "closed_ts": ts + timedelta(seconds=5),
            "revision": 3,
        }
    )

    assert await repository.put_episode(episode) is True
    assert await repository.put_episode(episode) is False  # identical revision is a no-op
    assert await repository.get_episode(episode.episode_id) == episode

    assert await repository.put_episode(advanced) is True  # higher revision advances
    assert await repository.put_episode(episode) is False  # stale revision cannot regress
    assert await repository.get_episode(episode.episode_id) == advanced


async def _round_trip_incident_memory(config: Settings, pool: PostgresPool, suffix: str) -> None:
    """Prove pgvector nearest-neighbour search agrees with the in-memory metric."""
    memory = IncidentMemoryRepository(pool=pool, schema=config.postgres_schema)
    ts = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    shapes = {
        "near": (0.15, 0.85, 0.10, 0.50),
        "middling": (0.40, 0.60, 0.30, 0.40),
        "opposite": (1.00, 0.00, 1.00, 0.00),
    }
    assert await memory.size() == 0
    for name, vector in shapes.items():
        await memory.remember(
            IncidentSignatureRecord(
                incident_id=f"{name}-{suffix}",
                recorded_at=ts,
                vector=vector,
                severity="HIGH",
                origin_service="payment",
                verdict_class="OPERATIONAL_FAULT",
                payload={"signature": list(vector)},
            )
        )
    # An identical rewrite is idempotent, not a second row.
    await memory.remember(
        IncidentSignatureRecord(
            incident_id=f"near-{suffix}",
            recorded_at=ts,
            vector=shapes["near"],
            severity="HIGH",
            origin_service="payment",
            verdict_class="OPERATIONAL_FAULT",
            payload={"signature": list(shapes["near"])},
        )
    )
    assert await memory.size() == len(shapes)

    probe = (0.10, 0.90, 0.10, 0.50)
    neighbours = await memory.nearest(probe, limit=3)
    assert [neighbour.record.incident_id for neighbour in neighbours] == [
        f"near-{suffix}",
        f"middling-{suffix}",
        f"opposite-{suffix}",
    ]
    # pgvector's distance and the pure-Python similarity must describe one metric.
    expected = math.dist(probe, shapes["near"])
    assert neighbours[0].distance == pytest.approx(expected, abs=1e-6)
    assert similarity_from_distance(neighbours[0].distance) == pytest.approx(
        similarity_from_distance(expected)
    )
    assert neighbours[0].record.vector == shapes["near"]

    excluded = await memory.nearest(probe, limit=3, exclude_incident_id=f"near-{suffix}")
    assert [neighbour.record.incident_id for neighbour in excluded] == [
        f"middling-{suffix}",
        f"opposite-{suffix}",
    ]


async def _exercise_audit_ledger(config: Settings, pool: PostgresPool) -> None:
    """The ledger against a real database, where the interesting failures live.

    Two properties cannot be shown by a unit test at all: that concurrent
    appends serialise into one chain rather than two, and that a row edited
    directly in the database is caught on the way back out.
    """
    ledger = AuditLedgerRepository(pool=pool, schema=config.postgres_schema)
    ts = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

    assert await ledger.head() is None, "a fresh ledger has no head"
    assert await ledger.entries() == ()

    first = await ledger.append(
        ts=ts,
        kind=AuditEventKind.DECISION,
        actor="sentinel",
        summary="decided to restrain the cohort",
        body={"confidence": 0.88},
        incident_id="incident-1",
    )
    assert first.sequence == 0
    head = await ledger.head()
    assert head is not None
    assert head.entry_hash == first.entry_hash

    # --- the property a lock exists for ------------------------------------
    # Twelve appenders released at once against one head. True parallelism is
    # bounded by the pool (max_size 2 here), which is all it takes: two appends
    # running at the same instant would read the same head and mint the same
    # sequence without the advisory lock. With it they queue on the lock, and the
    # result is one chain with no gaps and no repeats.
    concurrent = 12
    appended = await asyncio.gather(
        *(
            ledger.append(
                ts=ts + timedelta(seconds=index + 1),
                kind=AuditEventKind.ACTION_APPLIED,
                actor=f"writer-{index}",
                summary=f"applied step {index}",
                body={"step": index},
                incident_id="incident-1",
            )
            for index in range(concurrent)
        )
    )
    sequences = sorted(entry.sequence for entry in appended)
    assert sequences == list(range(1, concurrent + 1)), "the chain forked or skipped a place"
    assert len({entry.previous_hash for entry in appended}) == concurrent, (
        "two entries built on the same head"
    )

    stored = await ledger.entries(limit=100)
    assert len(stored) == concurrent + 1
    verification = verify_chain(stored)
    assert verification.intact, verification.detail
    assert verification.head_hash == stored[-1].entry_hash

    paged = await ledger.entries(limit=100, after_sequence=stored[2].sequence)
    assert [entry.sequence for entry in paged] == [entry.sequence for entry in stored[3:]]

    # --- the property tamper evidence exists for ---------------------------
    # Edit a row the way somebody with database access would, and confirm the
    # ledger refuses to hand it back as if nothing had happened.
    async with pool.connection() as connection:
        await connection.execute(
            sql.SQL("UPDATE {} SET summary = %s WHERE sequence = %s").format(
                sql.Identifier(config.postgres_schema, "audit_ledger")
            ),
            ("nothing happened here", 2),
        )
    with pytest.raises(ValueError, match="must be the digest of this entry's own content"):
        await ledger.entries(limit=100)


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
