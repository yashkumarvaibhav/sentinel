"""What a component needs from the ledger in order to write to it.

A Protocol, and a synchronous one, for a reason worth stating: the executor is
synchronous and the durable ledger is async, so making the executor depend on
the Postgres repository directly would have turned the whole action plane async
to serve its audit trail. Instead every writer appends to *an* append-only chain
- ``AuditChain`` satisfies this as-is - and where that chain is drained to
storage is a wiring decision the always-on runtime loop makes, not one each
call site makes for itself.

The consequence to be clear about: a component holding an in-process chain has a
**complete and tamper-evident** record that is **not yet durable**. Those are
different properties, and only the second one is missing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from contracts import AuditEntry, AuditEventKind


class AuditSink(Protocol):
    """Somewhere an append-only, hash-chained entry can be written."""

    def append(
        self,
        *,
        ts: datetime,
        kind: AuditEventKind,
        actor: str,
        summary: str,
        body: dict[str, Any] | None = None,
        incident_id: str | None = None,
        decision_id: str | None = None,
        plan_id: str | None = None,
        honesty: Literal["REAL", "SIMULATED"] = "REAL",
    ) -> AuditEntry:
        """Append one entry to the end of the chain."""
