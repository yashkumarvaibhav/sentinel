"""Async PostgreSQL repository for low-volume runtime state."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal, Self, cast

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from action.control import (
    ActionControlNotFoundError,
    ActionExecutionClaim,
    ActionExecutionOperation,
    ActionExecutionPhase,
    complete_action_control,
)
from action.control import (
    transition_action_control as apply_action_control_transition,
)
from audit.chain import build_entry
from common.storage.models import (
    AuditRecord,
    IncidentDetailRecord,
    IncidentGraphRecord,
    IncidentRecord,
)
from common.storage.pool import PostgresPool
from contracts import (
    ActionControlRequest,
    ActionControlSnapshot,
    ActionOutcome,
    ActionStatus,
    AuditEntry,
    AuditEventKind,
    SymptomEpisode,
)


class PostgresRepository:
    """Persist incidents and immutable audit entries in a runtime-only schema."""

    def __init__(self, *, pool: PostgresPool, schema: str) -> None:
        self._pool = pool
        self._incidents = sql.Identifier(schema, "incidents")
        self._incident_graphs = sql.Identifier(schema, "incident_causal_graphs")
        self._incident_details = sql.Identifier(schema, "incident_details")
        self._incident_action_controls = sql.Identifier(schema, "incident_action_controls")
        self._action_execution_claims = sql.Identifier(schema, "incident_action_execution_claims")
        self._audit_ledger = sql.Identifier(schema, "audit_ledger")
        digest = hashlib.sha256(f"sentinel-audit-ledger:{schema}".encode()).digest()
        self._audit_lock_key = int.from_bytes(digest[:8], "big", signed=True)
        self._audit_entries = sql.Identifier(schema, "audit_entries")
        self._symptom_episodes = sql.Identifier(schema, "symptom_episodes")

    async def put_incident(self, record: IncidentRecord) -> bool:
        """Create or advance an incident; return whether durable state changed."""
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
            RETURNING incident_id
            """
        ).format(table=self._incidents)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                query,
                (
                    record.incident_id,
                    record.state,
                    Jsonb(record.payload),
                    record.created_at,
                    record.updated_at,
                ),
            )
            return await cursor.fetchone() is not None

    async def put_incident_bundle(
        self,
        record: IncidentRecord,
        graph: IncidentGraphRecord,
        detail: IncidentDetailRecord,
        action_control: ActionControlSnapshot | None = None,
    ) -> bool:
        """Atomically advance one incident, its proof, and any evidence-time action plan."""
        if (
            graph.incident_id != record.incident_id
            or detail.incident_id != record.incident_id
            or graph.updated_at != record.updated_at
            or detail.updated_at != record.updated_at
        ):
            raise ValueError("incident, causal graph and detail storage identities must match")
        if action_control is not None and (
            action_control.incident_id != record.incident_id
            or action_control.created_at != record.updated_at
        ):
            raise ValueError(
                "an action control attached to an incident bundle must share its identity "
                "and evidence revision time"
            )
        incident_query = sql.SQL(
            """
            INSERT INTO {table} (incident_id, state, payload, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (incident_id) DO UPDATE SET
                state = EXCLUDED.state,
                payload = EXCLUDED.payload,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at
            WHERE {table}.updated_at < EXCLUDED.updated_at
            RETURNING incident_id
            """
        ).format(table=self._incidents)
        graph_query = sql.SQL(
            """
            INSERT INTO {table} (incident_id, updated_at, payload)
            VALUES (%s, %s, %s)
            ON CONFLICT (incident_id) DO UPDATE SET
                updated_at = EXCLUDED.updated_at,
                payload = EXCLUDED.payload
            WHERE {table}.updated_at < EXCLUDED.updated_at
            RETURNING incident_id
            """
        ).format(table=self._incident_graphs)
        detail_query = sql.SQL(
            """
            INSERT INTO {table} (incident_id, updated_at, payload)
            VALUES (%s, %s, %s)
            ON CONFLICT (incident_id) DO UPDATE SET
                updated_at = EXCLUDED.updated_at,
                payload = EXCLUDED.payload
            WHERE {table}.updated_at < EXCLUDED.updated_at
            RETURNING incident_id
            """
        ).format(table=self._incident_details)
        async with self._pool.connection() as connection:
            incident_cursor = await connection.execute(
                incident_query,
                (
                    record.incident_id,
                    record.state,
                    Jsonb(record.payload),
                    record.created_at,
                    record.updated_at,
                ),
            )
            if await incident_cursor.fetchone() is None:
                return False
            graph_cursor = await connection.execute(
                graph_query,
                (
                    graph.incident_id,
                    graph.updated_at,
                    Jsonb(graph.payload),
                ),
            )
            if await graph_cursor.fetchone() is None:
                raise RuntimeError("causal graph could not advance with its incident")
            detail_cursor = await connection.execute(
                detail_query,
                (
                    detail.incident_id,
                    detail.updated_at,
                    Jsonb(detail.payload),
                ),
            )
            if await detail_cursor.fetchone() is None:
                raise RuntimeError("incident detail could not advance with its incident")
            if action_control is not None:
                await self._insert_action_control(connection, action_control)
            return True

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

    async def list_incidents(self, *, limit: int) -> tuple[IncidentRecord, ...]:
        """Return a bounded latest-first snapshot with deterministic tie order."""
        if not 1 <= limit <= 50:
            raise ValueError("incident list limit must be within [1, 50]")
        query = sql.SQL(
            """
            SELECT incident_id, state, created_at, updated_at, payload
            FROM {}
            ORDER BY updated_at DESC, incident_id ASC
            LIMIT %s
            """
        ).format(self._incidents)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query, (limit,))
            rows = await cursor.fetchall()
        return tuple(
            IncidentRecord(
                incident_id=cast(str, row[0]),
                state=cast(str, row[1]),
                created_at=cast(datetime, row[2]),
                updated_at=cast(datetime, row[3]),
                payload=cast(dict[str, JsonValue], row[4]),
            )
            for row in rows
        )

    async def latest_incident_graph(self) -> IncidentGraphRecord | None:
        """Return the newest graph whose owning incident is still unresolved."""
        query = sql.SQL(
            """
            SELECT graph.incident_id, graph.updated_at, graph.payload
            FROM {graphs} AS graph
            JOIN {incidents} AS incident USING (incident_id)
            WHERE incident.state IN ('OPEN', 'MITIGATING', 'MONITORING')
            ORDER BY graph.updated_at DESC, graph.incident_id ASC
            LIMIT 1
            """
        ).format(graphs=self._incident_graphs, incidents=self._incidents)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query)
            row = await cursor.fetchone()
        if row is None:
            return None
        return IncidentGraphRecord(
            incident_id=cast(str, row[0]),
            updated_at=cast(datetime, row[1]),
            payload=cast(dict[str, JsonValue], row[2]),
        )

    async def get_incident_detail(self, incident_id: str) -> IncidentDetailRecord | None:
        """Fetch one evidence-complete proof snapshot by stable incident id."""
        query = sql.SQL(
            """
            SELECT incident_id, updated_at, payload
            FROM {}
            WHERE incident_id = %s
            """
        ).format(self._incident_details)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query, (incident_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return IncidentDetailRecord(
            incident_id=cast(str, row[0]),
            updated_at=cast(datetime, row[1]),
            payload=cast(dict[str, JsonValue], row[2]),
        )

    async def put_action_control(self, control: ActionControlSnapshot) -> bool:
        """Append one immutable plan revision, monotonically per incident.

        The advisory transaction lock closes the only race that matters here:
        two workers materializing different "next" plans for the same incident.
        Replaying the exact same revision is idempotent; reusing a revision for
        different evidence is refused rather than silently replacing history.
        """
        async with self._pool.connection() as connection, connection.transaction():
            return await self._insert_action_control(connection, control)

    async def _insert_action_control(
        self,
        connection: AsyncConnection[TupleRow],
        control: ActionControlSnapshot,
    ) -> bool:
        """Insert one revision on the caller's transaction and incident lock."""
        lock_query = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
        latest_query = sql.SQL(
            """
            SELECT plan_revision, payload::text
            FROM {}
            WHERE incident_id = %s
            ORDER BY plan_revision DESC
            LIMIT 1
            """
        ).format(self._incident_action_controls)
        insert_query = sql.SQL(
            """
            INSERT INTO {}
                (incident_id, plan_revision, state, created_at, updated_at, payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            """
        ).format(self._incident_action_controls)
        await connection.execute(lock_query, (control.incident_id,))
        cursor = await connection.execute(latest_query, (control.incident_id,))
        row = await cursor.fetchone()
        latest_revision = 0 if row is None else cast(int, row[0])
        if control.plan_revision == latest_revision:
            if row is None:
                raise AssertionError("revision zero cannot be materialized")
            existing = ActionControlSnapshot.model_validate_json(cast(str, row[1]))
            if existing != control:
                raise ValueError(
                    f"plan revision {control.plan_revision} already names different "
                    "server-held action state"
                )
            return False
        expected = latest_revision + 1
        if control.plan_revision != expected:
            raise ValueError(
                f"the next revision for {control.incident_id} is {expected}, "
                f"not {control.plan_revision}"
            )
        await connection.execute(
            insert_query,
            (
                control.incident_id,
                control.plan_revision,
                control.state.value,
                control.created_at,
                control.updated_at,
                Jsonb(control.model_dump(mode="json")),
            ),
        )
        return True

    async def get_action_control(
        self,
        incident_id: str,
        *,
        plan_revision: int | None = None,
    ) -> ActionControlSnapshot | None:
        """Read one exact plan revision, or the incident's latest revision."""
        if plan_revision is None:
            query = sql.SQL(
                """
                SELECT payload::text
                FROM {}
                WHERE incident_id = %s
                ORDER BY plan_revision DESC
                LIMIT 1
                """
            ).format(self._incident_action_controls)
            parameters: tuple[object, ...] = (incident_id,)
        else:
            if plan_revision < 1:
                raise ValueError("plan_revision must be positive")
            query = sql.SQL(
                """
                SELECT payload::text
                FROM {}
                WHERE incident_id = %s AND plan_revision = %s
                """
            ).format(self._incident_action_controls)
            parameters = (incident_id, plan_revision)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query, parameters)
            row = await cursor.fetchone()
        if row is None:
            return None
        return ActionControlSnapshot.model_validate_json(cast(str, row[0]))

    async def transition_action_control(
        self,
        request: ActionControlRequest,
        *,
        actor: str,
        ts: datetime,
    ) -> tuple[bool, ActionControlSnapshot]:
        """Serialize and commit one intent against the exact requested revision."""
        select_query = sql.SQL(
            """
            SELECT payload::text
            FROM {}
            WHERE incident_id = %s
            ORDER BY plan_revision DESC
            LIMIT 1
            FOR UPDATE
            """
        ).format(self._incident_action_controls)
        update_query = sql.SQL(
            """
            UPDATE {}
            SET state = %s, updated_at = %s, payload = %s
            WHERE incident_id = %s AND plan_revision = %s
            """
        ).format(self._incident_action_controls)
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (request.incident_id,),
            )
            cursor = await connection.execute(
                select_query,
                (request.incident_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ActionControlNotFoundError(
                    f"no action control for {request.incident_id} revision {request.plan_revision}"
                )
            current = ActionControlSnapshot.model_validate_json(cast(str, row[0]))
            transitioned = apply_action_control_transition(
                current,
                request,
                actor=actor,
                ts=ts,
            )
            if transitioned == current:
                return False, current
            await connection.execute(
                update_query,
                (
                    transitioned.state.value,
                    transitioned.updated_at,
                    Jsonb(transitioned.model_dump(mode="json")),
                    transitioned.incident_id,
                    transitioned.plan_revision,
                ),
            )
            return True, transitioned

    async def claim_action_control(
        self,
        *,
        worker_id: str,
        ts: datetime,
        lease_seconds: int,
    ) -> ActionExecutionClaim | None:
        """Lease one requested plan without exposing worker state to the browser."""
        if not worker_id:
            raise ValueError("an action execution claim needs a worker identity")
        if lease_seconds < 1:
            raise ValueError("an action execution lease must last at least one second")
        select = sql.SQL(
            """
            SELECT c.payload::text, q.claim_id, q.phase
            FROM {controls} AS c
            LEFT JOIN {claims} AS q
              ON q.incident_id = c.incident_id
             AND q.plan_revision = c.plan_revision
            WHERE c.state IN ('APPLY_REQUESTED', 'ROLLBACK_REQUESTED')
              AND c.plan_revision = (
                  SELECT MAX(latest.plan_revision)
                  FROM {controls} AS latest
                  WHERE latest.incident_id = c.incident_id
              )
              AND (q.claim_id IS NULL OR q.expires_at <= %s)
            ORDER BY c.updated_at ASC, c.incident_id ASC, c.plan_revision ASC
            FOR UPDATE OF c SKIP LOCKED
            LIMIT 1
            """
        ).format(controls=self._incident_action_controls, claims=self._action_execution_claims)
        upsert = sql.SQL(
            """
            INSERT INTO {claims}
                (incident_id, plan_revision, claim_id, worker_id, operation,
                 phase, claimed_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (incident_id, plan_revision) DO UPDATE SET
                claim_id = EXCLUDED.claim_id,
                worker_id = EXCLUDED.worker_id,
                operation = EXCLUDED.operation,
                phase = EXCLUDED.phase,
                claimed_at = EXCLUDED.claimed_at,
                expires_at = EXCLUDED.expires_at
            """
        ).format(claims=self._action_execution_claims)
        expires_at = ts + timedelta(seconds=lease_seconds)
        async with self._pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(select, (ts,))
            row = await cursor.fetchone()
            if row is None:
                return None
            control = ActionControlSnapshot.model_validate_json(cast(str, row[0]))
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (control.incident_id,),
            )
            latest_cursor = await connection.execute(
                sql.SQL("SELECT MAX(plan_revision) FROM {} WHERE incident_id = %s").format(
                    self._incident_action_controls
                ),
                (control.incident_id,),
            )
            latest_row = await latest_cursor.fetchone()
            if latest_row is None or cast(int, latest_row[0]) != control.plan_revision:
                return None
            recovered = row[1] is not None
            operation = (
                ActionExecutionOperation.APPLY
                if control.state.value == "APPLY_REQUESTED"
                else ActionExecutionOperation.ROLLBACK
            )
            phase = (
                ActionExecutionPhase(cast(str, row[2]))
                if recovered
                else ActionExecutionPhase.CLAIMED
            )
            claim_id = _claim_id(control, worker_id=worker_id, ts=ts)
            await connection.execute(
                upsert,
                (
                    control.incident_id,
                    control.plan_revision,
                    claim_id,
                    worker_id,
                    operation.value,
                    phase.value,
                    ts,
                    expires_at,
                ),
            )
        return ActionExecutionClaim(
            claim_id=claim_id,
            worker_id=worker_id,
            operation=operation,
            phase=phase,
            claimed_at=ts,
            expires_at=expires_at,
            control=control,
            recovered=recovered,
        )

    async def mark_action_dispatched(
        self,
        claim: ActionExecutionClaim,
        *,
        ts: datetime,
    ) -> ActionExecutionClaim:
        """Persist the side-effect boundary before the actuator is called."""
        duration = claim.expires_at - claim.claimed_at
        expires_at = ts + duration
        update = sql.SQL(
            """
            UPDATE {claims}
            SET phase = 'DISPATCHED', expires_at = %s
            WHERE incident_id = %s AND plan_revision = %s
              AND claim_id = %s AND worker_id = %s
              AND expires_at > %s
            RETURNING claim_id
            """
        ).format(claims=self._action_execution_claims)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                update,
                (
                    expires_at,
                    claim.control.incident_id,
                    claim.control.plan_revision,
                    claim.claim_id,
                    claim.worker_id,
                    ts,
                ),
            )
            if await cursor.fetchone() is None:
                raise RuntimeError("the action execution claim expired or changed before dispatch")
        return ActionExecutionClaim(
            claim_id=claim.claim_id,
            worker_id=claim.worker_id,
            operation=claim.operation,
            phase=ActionExecutionPhase.DISPATCHED,
            claimed_at=claim.claimed_at,
            expires_at=expires_at,
            control=claim.control,
            recovered=claim.recovered,
        )

    async def complete_action_execution(
        self,
        claim: ActionExecutionClaim,
        outcome: ActionOutcome,
        *,
        ts: datetime,
    ) -> ActionControlSnapshot:
        """Atomically commit terminal control state, audit evidence, and claim release."""
        claim_query = sql.SQL(
            """
            SELECT claim_id, worker_id, expires_at
            FROM {claims}
            WHERE incident_id = %s AND plan_revision = %s
            FOR UPDATE
            """
        ).format(claims=self._action_execution_claims)
        control_query = sql.SQL(
            """
            SELECT payload::text
            FROM {controls}
            WHERE incident_id = %s AND plan_revision = %s
            FOR UPDATE
            """
        ).format(controls=self._incident_action_controls)
        update = sql.SQL(
            """
            UPDATE {controls}
            SET state = %s, updated_at = %s, payload = %s
            WHERE incident_id = %s AND plan_revision = %s
            """
        ).format(controls=self._incident_action_controls)
        delete = sql.SQL(
            "DELETE FROM {claims} WHERE incident_id = %s AND plan_revision = %s"
        ).format(claims=self._action_execution_claims)
        identity = (claim.control.incident_id, claim.control.plan_revision)
        async with self._pool.connection() as connection, connection.transaction():
            claim_cursor = await connection.execute(claim_query, identity)
            claim_row = await claim_cursor.fetchone()
            if (
                claim_row is None
                or cast(str, claim_row[0]) != claim.claim_id
                or cast(str, claim_row[1]) != claim.worker_id
                or cast(datetime, claim_row[2]) <= ts
            ):
                raise RuntimeError("the action execution claim expired or changed before commit")
            control_cursor = await connection.execute(control_query, identity)
            control_row = await control_cursor.fetchone()
            if control_row is None:
                raise ActionControlNotFoundError(
                    f"no action control for {identity[0]} revision {identity[1]}"
                )
            current = ActionControlSnapshot.model_validate_json(cast(str, control_row[0]))
            completed = complete_action_control(
                current,
                operation=claim.operation,
                outcome=outcome,
                ts=ts,
            )
            await connection.execute(
                update,
                (
                    completed.state.value,
                    completed.updated_at,
                    Jsonb(completed.model_dump(mode="json")),
                    *identity,
                ),
            )
            await self._append_action_audit(
                connection,
                claim=claim,
                outcome=outcome,
                ts=ts,
            )
            await connection.execute(delete, identity)
        return completed

    async def _append_action_audit(
        self,
        connection: AsyncConnection[TupleRow],
        *,
        claim: ActionExecutionClaim,
        outcome: ActionOutcome,
        ts: datetime,
    ) -> AuditEntry:
        """Append the control completion to the global chain on the same transaction."""
        await connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (self._audit_lock_key,),
        )
        head_query = sql.SQL("SELECT {columns} FROM {table} ORDER BY sequence DESC LIMIT 1").format(
            columns=_AUDIT_COLUMNS, table=self._audit_ledger
        )
        head_cursor = await connection.execute(head_query)
        head_row = await head_cursor.fetchone()
        previous = None if head_row is None else _audit_entry(head_row)
        kind = _action_audit_kind(outcome)
        plan = claim.control.plan
        entry = build_entry(
            previous=previous,
            ts=ts,
            kind=kind,
            actor=claim.worker_id,
            summary=(
                f"{outcome.status.value.lower()} {plan.action_kind.value} on "
                f"{plan.target_ref}: {outcome.detail}"
            ),
            body={
                "claim_id": claim.claim_id,
                "operation": claim.operation.value,
                "outcome_id": outcome.outcome_id,
                "status": outcome.status.value,
                "idempotency_key": outcome.idempotency_key,
                "gates_passed": list(outcome.gates_passed),
                "observed": list(outcome.observed),
            },
            incident_id=plan.incident_id,
            decision_id=plan.decision_id,
            plan_id=plan.plan_id,
            honesty=outcome.honesty,
        )
        insert = sql.SQL(
            """
            INSERT INTO {table}
                (entry_id, sequence, ts, kind, actor, summary, incident_id,
                 decision_id, plan_id, body, previous_hash, entry_hash, honesty)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        ).format(table=self._audit_ledger)
        await connection.execute(
            insert,
            (
                entry.entry_id,
                entry.sequence,
                entry.ts,
                entry.kind.value,
                entry.actor,
                entry.summary,
                entry.incident_id,
                entry.decision_id,
                entry.plan_id,
                Jsonb(entry.body),
                entry.previous_hash,
                entry.entry_hash,
                entry.honesty,
            ),
        )
        return entry

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


class IncidentSignatureRecord(BaseModel):
    """One incident's stored four-dimensional signature and its provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    incident_id: str = Field(min_length=1, max_length=255)
    recorded_at: datetime
    vector: tuple[float, ...] = Field(min_length=1)
    severity: str = Field(min_length=1, max_length=64)
    origin_service: str | None = None
    verdict_class: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_signature_record(self) -> Self:
        if self.recorded_at.utcoffset() != timedelta(0):
            raise ValueError("recorded_at must be timezone-aware UTC")
        for value in self.vector:
            if not 0.0 <= value <= 1.0:
                raise ValueError("every signature dimension is a probability")
        return self


class NeighbourRecord(BaseModel):
    """A stored signature and its euclidean distance from a probe."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    record: IncidentSignatureRecord
    distance: float = Field(ge=0.0)


class IncidentMemoryRepository:
    """Store and search incident signatures with pgvector's exact nearest search."""

    def __init__(self, *, pool: PostgresPool, schema: str) -> None:
        self._pool = pool
        self._signatures = sql.Identifier(schema, "incident_signatures")

    async def remember(self, record: IncidentSignatureRecord) -> None:
        """Idempotently record or refresh one incident's signature."""
        query = sql.SQL(
            """
            INSERT INTO {table}
                (incident_id, recorded_at, origin_service, verdict_class,
                 severity, signature, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (incident_id) DO UPDATE SET
                recorded_at = EXCLUDED.recorded_at,
                origin_service = EXCLUDED.origin_service,
                verdict_class = EXCLUDED.verdict_class,
                severity = EXCLUDED.severity,
                signature = EXCLUDED.signature,
                payload = EXCLUDED.payload
            WHERE {table}.recorded_at <= EXCLUDED.recorded_at
            """
        ).format(table=self._signatures)
        async with self._pool.connection() as connection:
            await connection.execute(
                query,
                (
                    record.incident_id,
                    record.recorded_at,
                    record.origin_service,
                    record.verdict_class,
                    record.severity,
                    _vector_literal(record.vector),
                    Jsonb(record.payload),
                ),
            )

    async def size(self) -> int:
        """How many incidents the memory holds, for the verifier's cold start."""
        query = sql.SQL("SELECT count(*) FROM {}").format(self._signatures)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query)
            row = await cursor.fetchone()
        return 0 if row is None else cast(int, row[0])

    async def nearest(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        exclude_incident_id: str | None = None,
    ) -> tuple[NeighbourRecord, ...]:
        """Return the closest stored signatures, nearest first.

        The probe's own incident is excluded explicitly: an incident is never
        its own precedent, and letting it match itself would make every verdict
        look familiar.
        """
        if limit < 1:
            raise ValueError("limit must be at least one")
        query = sql.SQL(
            """
            SELECT incident_id, recorded_at, origin_service, verdict_class,
                   severity, payload, signature <-> %s AS distance
            FROM {table}
            WHERE %s::text IS NULL OR incident_id <> %s
            ORDER BY distance ASC, incident_id ASC
            LIMIT %s
            """
        ).format(table=self._signatures)
        probe = _vector_literal(tuple(vector))
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                query, (probe, exclude_incident_id, exclude_incident_id, limit)
            )
            rows = await cursor.fetchall()
        return tuple(
            NeighbourRecord(
                record=IncidentSignatureRecord(
                    incident_id=cast(str, row[0]),
                    recorded_at=cast(datetime, row[1]),
                    vector=_stored_vector(row[5]),
                    severity=cast(str, row[4]),
                    origin_service=cast("str | None", row[2]),
                    verdict_class=cast("str | None", row[3]),
                    payload=cast(dict[str, JsonValue], row[5]),
                ),
                distance=float(cast(float, row[6])),
            )
            for row in rows
        )


def _vector_literal(vector: tuple[float, ...]) -> str:
    """Render a vector in pgvector's text input form."""
    if not vector:
        raise ValueError("a signature vector cannot be empty")
    return "[" + ",".join(format(value, ".12g") for value in vector) + "]"


def _stored_vector(payload: object) -> tuple[float, ...]:
    """Recover the signature from the payload written alongside it."""
    if isinstance(payload, dict):
        stored = payload.get("signature")
        if isinstance(stored, list):
            return tuple(float(value) for value in stored)
    raise ValueError("a stored signature must carry its vector in the payload")


def _claim_id(control: ActionControlSnapshot, *, worker_id: str, ts: datetime) -> str:
    digest = hashlib.sha256(
        (
            f"{control.incident_id}:{control.plan_revision}:{control.state.value}:"
            f"{worker_id}:{ts.isoformat()}"
        ).encode()
    ).hexdigest()
    return f"action-claim-{digest[:32]}"


def _action_audit_kind(outcome: ActionOutcome) -> AuditEventKind:
    return {
        ActionStatus.SIMULATED: AuditEventKind.ACTION_PLANNED,
        ActionStatus.APPLIED: AuditEventKind.ACTION_APPLIED,
        ActionStatus.VERIFIED: AuditEventKind.ACTION_VERIFIED,
        ActionStatus.REVERTED: AuditEventKind.ACTION_REVERTED,
        ActionStatus.FAILED: AuditEventKind.ACTION_REFUSED,
    }[outcome.status]


class AuditLedgerRepository:
    """The hash-chained ledger, appended by exactly one writer at a time.

    Everything about *what* an entry contains is decided by ``audit.chain``, so
    the durable ledger and the in-process one agree by construction. What this
    class adds is the single thing a process-local chain cannot have: an append
    that is serialised **across** processes.

    The serialisation is a Postgres advisory lock taken inside the transaction,
    so it is released when the transaction ends however it ends - including a
    crash, which is exactly when a lock you have to remember to release becomes
    an outage. The head is re-read *under* the lock; reading it outside would
    make the lock decorative, because the value it protects would already be
    stale by the time it was used.

    The table's ``UNIQUE (previous_hash)`` is the second line: even with no lock
    at all, two writers building on one head cannot both succeed.
    """

    def __init__(self, *, pool: PostgresPool, schema: str) -> None:
        self._pool = pool
        self._schema = schema
        self._ledger = sql.Identifier(schema, "audit_ledger")
        # One lock per schema, derived from its name so a test schema and a
        # runtime schema never contend with each other. Postgres advisory locks
        # are a single 64-bit space shared by the whole database.
        digest = hashlib.sha256(f"sentinel-audit-ledger:{schema}".encode()).digest()
        self._lock_key = int.from_bytes(digest[:8], "big", signed=True)

    async def append(
        self,
        *,
        ts: datetime,
        kind: AuditEventKind,
        actor: str,
        summary: str,
        body: dict[str, JsonValue] | None = None,
        incident_id: str | None = None,
        decision_id: str | None = None,
        plan_id: str | None = None,
        honesty: Literal["REAL", "SIMULATED"] = "REAL",
    ) -> AuditEntry:
        """Append one entry to the end of the chain, alone."""
        insert = sql.SQL(
            """
            INSERT INTO {table}
                (entry_id, sequence, ts, kind, actor, summary, incident_id,
                 decision_id, plan_id, body, previous_hash, entry_hash, honesty)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        ).format(table=self._ledger)
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key,))
            entry = build_entry(
                previous=await self._head(connection),
                ts=ts,
                kind=kind,
                actor=actor,
                summary=summary,
                body=dict(body or {}),
                incident_id=incident_id,
                decision_id=decision_id,
                plan_id=plan_id,
                honesty=honesty,
            )
            await connection.execute(
                insert,
                (
                    entry.entry_id,
                    entry.sequence,
                    entry.ts,
                    entry.kind.value,
                    entry.actor,
                    entry.summary,
                    entry.incident_id,
                    entry.decision_id,
                    entry.plan_id,
                    Jsonb(entry.body),
                    entry.previous_hash,
                    entry.entry_hash,
                    entry.honesty,
                ),
            )
        return entry

    async def head(self) -> AuditEntry | None:
        """The most recent entry, or nothing if the ledger has not started."""
        async with self._pool.connection() as connection:
            return await self._head(connection)

    async def entries(
        self, *, limit: int = 100, after_sequence: int | None = None
    ) -> tuple[AuditEntry, ...]:
        """A run of the chain in order, oldest first, for reading and verifying."""
        if limit < 1:
            raise ValueError("limit must be at least one")
        query = sql.SQL(
            """
            SELECT {columns} FROM {table}
            WHERE %s::bigint IS NULL OR sequence > %s
            ORDER BY sequence ASC
            LIMIT %s
            """
        ).format(columns=_AUDIT_COLUMNS, table=self._ledger)
        async with self._pool.connection() as connection:
            cursor = await connection.execute(query, (after_sequence, after_sequence, limit))
            rows = await cursor.fetchall()
        return tuple(_audit_entry(row) for row in rows)

    async def _head(self, connection: AsyncConnection[TupleRow]) -> AuditEntry | None:
        query = sql.SQL("SELECT {columns} FROM {table} ORDER BY sequence DESC LIMIT 1").format(
            columns=_AUDIT_COLUMNS, table=self._ledger
        )
        cursor = await connection.execute(query)
        row = await cursor.fetchone()
        return None if row is None else _audit_entry(row)


_AUDIT_COLUMNS = sql.SQL(
    "entry_id, sequence, ts, kind, actor, summary, incident_id, decision_id, "
    "plan_id, body, previous_hash, entry_hash, honesty"
)


def _audit_entry(row: Sequence[object]) -> AuditEntry:
    """Rebuild one entry, which re-validates its hash on the way out.

    Validation on read is not redundant: a row edited directly in the database is
    exactly the attack the ledger exists to detect, and it is caught here before
    the entry reaches anything that might act on it.
    """
    return AuditEntry(
        entry_id=cast(str, row[0]),
        sequence=cast(int, row[1]),
        ts=cast(datetime, row[2]),
        kind=AuditEventKind(cast(str, row[3])),
        actor=cast(str, row[4]),
        summary=cast(str, row[5]),
        incident_id=cast("str | None", row[6]),
        decision_id=cast("str | None", row[7]),
        plan_id=cast("str | None", row[8]),
        body=cast(dict[str, JsonValue], row[9]),
        previous_hash=cast(str, row[10]),
        entry_hash=cast(str, row[11]),
        honesty=cast('Literal["REAL", "SIMULATED"]', row[12]),
    )
