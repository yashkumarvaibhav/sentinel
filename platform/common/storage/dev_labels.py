"""Development-only label storage, intentionally absent from runtime exports."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from common.storage.models import RecordId, RecordText, StorageRecord, UtcDatetime
from common.storage.pool import PostgresPool


class DevLabelRecord(StorageRecord):
    """An answer-key record confined to the development label schema."""

    label_id: RecordId
    run_id: RecordId
    subject_id: RecordId
    kind: RecordText
    payload: dict[str, JsonValue]
    created_at: UtcDatetime


class DevLabelRepository:
    """Persist scoring labels without exposing them through runtime storage."""

    def __init__(self, *, pool: PostgresPool, schema: str) -> None:
        self._pool = pool
        self._labels = sql.Identifier(schema, "labels")

    async def put(self, record: DevLabelRecord) -> bool:
        """Insert a label once; return whether this call created it."""
        query = sql.SQL(
            """
            INSERT INTO {} (label_id, run_id, subject_id, kind, payload, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (label_id) DO NOTHING
            RETURNING label_id
            """
        ).format(self._labels)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                query,
                (
                    record.label_id,
                    record.run_id,
                    record.subject_id,
                    record.kind,
                    Jsonb(record.payload),
                    record.created_at,
                ),
            )
            return await cursor.fetchone() is not None

    async def get(self, label_id: str) -> DevLabelRecord | None:
        """Fetch one development label by id."""
        query = sql.SQL(
            """
            SELECT label_id, run_id, subject_id, kind, payload, created_at
            FROM {}
            WHERE label_id = %s
            """
        ).format(self._labels)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query, (label_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return DevLabelRecord(
            label_id=cast(str, row[0]),
            run_id=cast(str, row[1]),
            subject_id=cast(str, row[2]),
            kind=cast(str, row[3]),
            payload=cast(dict[str, JsonValue], row[4]),
            created_at=cast(datetime, row[5]),
        )
