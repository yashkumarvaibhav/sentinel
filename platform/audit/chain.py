"""Building and checking the audit chain, independently of where it is stored.

Two operations, and keeping them apart is the point:

* ``AuditChain.append`` mints the next entry from the current head. It is the
  only place an entry is constructed, so the sequence and the previous hash can
  never be chosen by a caller.
* ``verify_chain`` re-derives every hash from the bodies and reports the first
  place the chain stops being consistent. It shares no state with the appender -
  it reads entries as data and checks them arithmetically, which is what makes
  it able to catch a bad appender rather than agree with one.

The storage layer's job is separate and smaller: make the append **single
writer**, so two appenders cannot both build on the same head and fork the
chain. That belongs to the database transaction, not here, because a lock that
lives in a process only protects one process.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from contracts import (
    GENESIS_HASH,
    AuditEntry,
    AuditEventKind,
    audit_entry_hash,
    canonical_body,
)


class AuditChainError(RuntimeError):
    """The chain is not what it claims to be."""


class ChainForkedError(AuditChainError):
    """Two entries claim the same place in the chain.

    The failure a single-writer append exists to make impossible, raised where
    it is detectable so a forked chain is never quietly readable as a valid one.
    """


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """Whether a run of entries is internally consistent, and where it stops being."""

    intact: bool
    entries: int
    head_hash: str
    broken_at: int | None = None
    detail: str | None = None


def build_entry(
    *,
    previous: AuditEntry | None,
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
    """Mint the entry that follows ``previous``, hashing it into place."""
    sequence = 0 if previous is None else previous.sequence + 1
    previous_hash = GENESIS_HASH if previous is None else previous.entry_hash
    payload = dict(body or {})
    stamped = ts.isoformat()
    entry_hash = audit_entry_hash(
        previous_hash=previous_hash,
        sequence=sequence,
        ts=stamped,
        kind=kind.value,
        actor=actor,
        summary=summary,
        body=payload,
        incident_id=incident_id,
        decision_id=decision_id,
        plan_id=plan_id,
    )
    return AuditEntry(
        # Derived from the hash rather than from a counter: an id that depends on
        # the content cannot be reused for different content.
        entry_id=f"audit-{entry_hash[:24]}",
        sequence=sequence,
        ts=ts,
        kind=kind,
        actor=actor,
        summary=summary,
        incident_id=incident_id,
        decision_id=decision_id,
        plan_id=plan_id,
        body=payload,
        previous_hash=previous_hash,
        entry_hash=entry_hash,
        honesty=honesty,
    )


class AuditChain:
    """An in-process chain: the reference implementation, and the test double.

    The durable ledger keeps the same shape and adds one thing this cannot have -
    an append that is serialised across processes. Everything about *what* an
    entry contains is decided here so that both agree by construction.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries: Iterable[AuditEntry] = ()) -> None:
        self._entries: list[AuditEntry] = []
        for entry in entries:
            self._adopt(entry)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        """An empty chain is still a chain. See the journal's note; same trap."""
        return True

    @property
    def head(self) -> AuditEntry | None:
        """The most recent entry, or nothing if the chain has not started."""
        return self._entries[-1] if self._entries else None

    @property
    def head_hash(self) -> str:
        """The single value an outside system keeps to detect a rewrite."""
        head = self.head
        return GENESIS_HASH if head is None else head.entry_hash

    def entries(self) -> tuple[AuditEntry, ...]:
        """Every entry, oldest first."""
        return tuple(self._entries)

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
        """Add one entry to the end of the chain."""
        entry = build_entry(
            previous=self.head,
            ts=ts,
            kind=kind,
            actor=actor,
            summary=summary,
            body=body,
            incident_id=incident_id,
            decision_id=decision_id,
            plan_id=plan_id,
            honesty=honesty,
        )
        self._entries.append(entry)
        return entry

    def adopt(self, entry: AuditEntry) -> None:
        """Take an entry minted elsewhere, refusing one that does not follow."""
        self._adopt(entry)

    def _adopt(self, entry: AuditEntry) -> None:
        if not entry.follows(self.head):
            raise ChainForkedError(
                f"{entry.entry_id} claims sequence {entry.sequence} after "
                f"{'nothing' if self.head is None else self.head.sequence}; an entry that does "
                "not follow the head would fork the chain"
            )
        self._entries.append(entry)


def verify_chain(entries: Sequence[AuditEntry]) -> ChainVerification:
    """Re-derive every hash and report the first place the chain breaks.

    Deliberately independent of ``AuditChain``: it treats entries as data and
    checks them arithmetically, so it can catch a bad appender rather than agree
    with one.
    """
    if not entries:
        return ChainVerification(intact=True, entries=0, head_hash=GENESIS_HASH)
    previous: AuditEntry | None = None
    for entry in entries:
        expected_hash = audit_entry_hash(
            previous_hash=entry.previous_hash,
            sequence=entry.sequence,
            ts=entry.ts.isoformat(),
            kind=entry.kind.value,
            actor=entry.actor,
            summary=entry.summary,
            body=dict(entry.body),
            incident_id=entry.incident_id,
            decision_id=entry.decision_id,
            plan_id=entry.plan_id,
        )
        if entry.entry_hash != expected_hash:
            return _broken(entries, entry, "its hash does not match its own content")
        if not entry.follows(previous):
            expected = "the genesis hash" if previous is None else previous.entry_hash[:12]
            return _broken(
                entries,
                entry,
                f"it does not follow the entry before it (expected to build on {expected})",
            )
        previous = entry
    return ChainVerification(intact=True, entries=len(entries), head_hash=entries[-1].entry_hash)


def _broken(entries: Sequence[AuditEntry], entry: AuditEntry, why: str) -> ChainVerification:
    return ChainVerification(
        intact=False,
        entries=len(entries),
        head_hash=entries[-1].entry_hash,
        broken_at=entry.sequence,
        detail=f"entry {entry.sequence} ({entry.entry_id}) is not trustworthy: {why}",
    )


def body_digest(body: dict[str, Any]) -> str:
    """A stable digest of an entry body, for comparing two records of one event."""
    return hashlib.sha256(canonical_body(body).encode("utf-8")).hexdigest()
