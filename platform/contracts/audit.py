"""The audit contract: an append-only chain that cannot be quietly rewritten.

Everything else in this system is designed so a wrong answer is visible. The
ledger is designed so a *changed* answer is visible, which is a different
property and the one that matters when someone asks six months later why a
service was rate-limited at 03:14.

The mechanism is the standard one and its value comes entirely from the details:

* Each entry hashes **the previous entry's hash together with its own body**, so
  altering any entry invalidates every entry after it. Rewriting history means
  rewriting all of it, and the head hash is the one value an outside system has
  to keep to detect that.
* The **body is canonical JSON** - sorted keys, no whitespace, no NaN - because a
  hash over a representation that can vary is not a hash over the content, and
  **every content field is inside it**: the summary a person reads and the actor
  who acted are hashed exactly like the structured payload. Leaving them out
  would have let somebody rewrite the sentence and the name while the chain
  still verified.
* ``entry_hash`` is **recomputed by the contract**, so an entry whose hash
  disagrees with its own body cannot be constructed at all. There is exactly one
  derivation and no way around it, the same rule the idempotency key follows.

The sequence number is part of the hashed body on purpose: without it, two
entries with identical content at identical times would be interchangeable, and
a ledger where an entry can be *moved* is not tamper-evident either.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from contracts._base import ContractModel, HumanText, Identifier, UtcDatetime

# The hash a chain starts from. Stated rather than empty so the first entry is
# hashed exactly like every other one, with no special case to get wrong.
GENESIS_HASH = "0" * 64


class AuditEventKind(StrEnum):
    """What kind of thing the ledger is recording.

    Deliberately small. The ledger records what the platform *decided* and what
    it *did about it*, which is the pair a person needs to reconstruct an
    incident; telemetry lives in the stores that are built for it.
    """

    DECISION = "DECISION"
    ACTION_PLANNED = "ACTION_PLANNED"
    ACTION_APPLIED = "ACTION_APPLIED"
    ACTION_VERIFIED = "ACTION_VERIFIED"
    ACTION_REVERTED = "ACTION_REVERTED"
    ACTION_REFUSED = "ACTION_REFUSED"
    ROLLBACK = "ROLLBACK"
    BREAKER_OPENED = "BREAKER_OPENED"
    APPROVAL = "APPROVAL"


def canonical_body(body: dict[str, Any]) -> str:
    """Render an entry body the one way it is ever hashed."""
    return json.dumps(body, allow_nan=False, separators=(",", ":"), sort_keys=True)


def audit_entry_hash(
    *,
    previous_hash: str,
    sequence: int,
    ts: str,
    kind: str,
    actor: str,
    summary: str,
    body: dict[str, Any],
    incident_id: str | None = None,
    decision_id: str | None = None,
    plan_id: str | None = None,
) -> str:
    """Derive the hash that binds one entry to everything before it.

    **Every content field is hashed, not only the structured body.** An earlier
    shape covered the body and left `summary` and `actor` out, which would have
    let somebody rewrite the sentence a person actually reads - and the name of
    who did it - while the chain still verified. The only fields outside the
    hash are `entry_id`, which is derived FROM it, and `entry_hash` itself.
    """
    content = {
        "actor": actor,
        "body": body,
        "decision_id": decision_id,
        "incident_id": incident_id,
        "kind": kind,
        "plan_id": plan_id,
        "sequence": sequence,
        "summary": summary,
        "ts": ts,
    }
    payload = f"{previous_hash}\n{canonical_body(content)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditEntry(ContractModel):
    """One immutable record, bound to the entry before it by its own hash."""

    entry_id: Identifier
    sequence: int = Field(ge=0)
    ts: UtcDatetime
    kind: AuditEventKind
    actor: Identifier
    summary: HumanText
    incident_id: Identifier | None = None
    decision_id: Identifier | None = None
    plan_id: Identifier | None = None
    body: dict[Identifier, Any] = Field(default_factory=dict)
    previous_hash: Identifier
    entry_hash: Identifier
    honesty: Literal["REAL", "SIMULATED"]

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        """Recompute the hash, so an entry cannot disagree with its own content."""
        if len(self.previous_hash) != 64 or len(self.entry_hash) != 64:
            raise ValueError("audit hashes are 64-character SHA-256 digests")
        if self.sequence == 0 and self.previous_hash != GENESIS_HASH:
            raise ValueError(
                "the first entry of a chain follows the genesis hash; a chain that starts "
                "somewhere else is a chain with a missing beginning"
            )
        expected = audit_entry_hash(
            previous_hash=self.previous_hash,
            sequence=self.sequence,
            ts=self.ts.isoformat(),
            kind=self.kind.value,
            actor=self.actor,
            summary=self.summary,
            body=dict(self.body),
            incident_id=self.incident_id,
            decision_id=self.decision_id,
            plan_id=self.plan_id,
        )
        if self.entry_hash != expected:
            raise ValueError(
                "entry_hash must be the digest of this entry's own content and the hash before "
                "it; a hash that can disagree with its entry is not tamper evidence"
            )
        return self

    def follows(self, previous: AuditEntry | None) -> bool:
        """Whether this entry is the honest successor of the one given."""
        if previous is None:
            return self.sequence == 0 and self.previous_hash == GENESIS_HASH
        return self.sequence == previous.sequence + 1 and self.previous_hash == previous.entry_hash
