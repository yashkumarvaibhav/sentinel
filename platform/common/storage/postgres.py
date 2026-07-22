"""Async PostgreSQL repository for low-volume runtime state."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from common.storage.models import AuditRecord, IncidentRecord
from common.storage.pool import PostgresPool
from contracts import SymptomEpisode


class PostgresRepository:
    """Persist incidents and immutable audit entries in a runtime-only schema."""

    def __init__(self, *, pool: PostgresPool, schema: str) -> None:
        self._pool = pool
        self._incidents = sql.Identifier(schema, "incidents")
        self._audit_entries = sql.Identifier(schema, "audit_entries")
        self._symptom_episodes = sql.Identifier(schema, "symptom_episodes")

    async def put_incident(self, record: IncidentRecord) -> None:
        """Idempotently create or replace the current state for an incident."""
        query = sql.SQL(
            """
            INSERT INTO {table} (incident_id, state, payload, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (incident_id) DO UPDATE SET
                state = EXCLUDED.state,
                payload = EXCLUDED.payload,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at
            WHERE {table}.updated_at < EXCLUDED.updated_at
            """
        ).format(table=self._incidents)
        async with self._pool.connection() as connection:
            await connection.execute(
                query,
                (
                    record.incident_id,
                    record.state,
                    Jsonb(record.payload),
                    record.created_at,
                    record.updated_at,
                ),
            )

    async def get_incident(self, incident_id: str) -> IncidentRecord | None:
        """Fetch one incident by its stable id."""
        query = sql.SQL(
            """
            SELECT incident_id, state, created_at, updated_at, payload
            FROM {}
            WHERE incident_id = %s
            """
        ).format(self._incidents)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query, (incident_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return IncidentRecord(
            incident_id=cast(str, row[0]),
            state=cast(str, row[1]),
            created_at=cast(datetime, row[2]),
            updated_at=cast(datetime, row[3]),
            payload=cast(dict[str, JsonValue], row[4]),
        )

    async def put_episode(self, episode: SymptomEpisode) -> bool:
        """Idempotently persist an episode; skip a stale (lower-revision) write.

        The revision guard makes at-least-once redelivery and out-of-order
        retries safe: a replayed older state can never overwrite a newer one.
        Returns whether this call inserted or advanced the stored record.
        """
        updated_at = episode.closed_ts if episode.closed_ts is not None else episode.last_breach_ts
        query = sql.SQL(
            """
            INSERT INTO {table}
                (episode_id, kind, service, signal, status, revision,
                 opened_at, updated_at, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (episode_id) DO UPDATE SET
                status = EXCLUDED.status,
                revision = EXCLUDED.revision,
                updated_at = EXCLUDED.updated_at,
                payload = EXCLUDED.payload
            WHERE {table}.revision < EXCLUDED.revision
            RETURNING episode_id
            """
        ).format(table=self._symptom_episodes)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                query,
                (
                    episode.episode_id,
                    episode.kind.value,
                    episode.service,
                    episode.signal,
                    episode.status.value,
                    episode.revision,
                    episode.opened_ts,
                    updated_at,
                    Jsonb(episode.model_dump(mode="json")),
                ),
            )
            return await cursor.fetchone() is not None

    async def get_episode(self, episode_id: str) -> SymptomEpisode | None:
        """Fetch and strictly revalidate one episode by its stable id."""
        query = sql.SQL("SELECT payload::text FROM {} WHERE episode_id = %s").format(
            self._symptom_episodes
        )
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query, (episode_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return SymptomEpisode.model_validate_json(cast(str, row[0]))

    async def append_audit(self, record: AuditRecord) -> bool:
        """Append once by entry id; return whether this call inserted the row."""
        query = sql.SQL(
            """
            INSERT INTO {} (entry_id, ts, event_type, payload, prev_hash, entry_hash)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (entry_id) DO NOTHING
            RETURNING entry_id
            """
        ).format(self._audit_entries)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                query,
                (
                    record.entry_id,
                    record.ts,
                    record.event_type,
                    Jsonb(record.payload),
                    record.prev_hash,
                    record.entry_hash,
                ),
            )
            return await cursor.fetchone() is not None

    async def get_audit(self, entry_id: str) -> AuditRecord | None:
        """Fetch one immutable audit entry by id."""
        query = sql.SQL(
            """
            SELECT entry_id, ts, event_type, payload, prev_hash, entry_hash
            FROM {}
            WHERE entry_id = %s
            """
        ).format(self._audit_entries)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query, (entry_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return AuditRecord(
            entry_id=cast(str, row[0]),
            ts=cast(datetime, row[1]),
            event_type=cast(str, row[2]),
            payload=cast(dict[str, JsonValue], row[3]),
            prev_hash=cast(str, row[4]),
            entry_hash=cast(str, row[5]),
        )
