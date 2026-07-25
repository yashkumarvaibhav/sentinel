"""What the gateway needs from an audit ledger, and nothing more.

A Protocol rather than an import of the Postgres repository, for the same reason
every other seam in this build is one: the gateway should be testable without a
database, and a route that could only be exercised against real infrastructure
is a route nobody exercises.
"""

from __future__ import annotations

from typing import Protocol

from contracts import AuditEntry


class AuditLedger(Protocol):
    """A readable run of the hash chain, oldest first."""

    async def entries(
        self, *, limit: int = 100, after_sequence: int | None = None
    ) -> tuple[AuditEntry, ...]:
        """Return up to ``limit`` entries after ``after_sequence``, in order."""
