"""The hash-chained audit ledger: it must be impossible to change history quietly.

Every test here is a variation on one question - if somebody edited the record,
would anyone find out? The interesting cases are the ones where the edit is
plausible: a changed word in a summary, a body field nudged, an inconvenient
entry removed, two writers building on the same head.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from audit import (
    AuditChain,
    ChainForkedError,
    build_entry,
    verify_chain,
)
from contracts import GENESIS_HASH, AuditEntry, AuditEventKind

TICK = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _chain(count: int = 4) -> AuditChain:
    chain = AuditChain()
    for index in range(count):
        chain.append(
            ts=TICK + timedelta(seconds=index),
            kind=AuditEventKind.ACTION_APPLIED,
            actor="sentinel",
            summary=f"restrained the cohort, step {index}",
            body={"step": index, "target": "frontend"},
            incident_id="incident-1",
        )
    return chain


# --- what a chain is --------------------------------------------------------


def test_a_chain_starts_from_the_genesis_hash() -> None:
    chain = AuditChain()
    assert chain.head is None
    assert chain.head_hash == GENESIS_HASH

    first = chain.append(
        ts=TICK, kind=AuditEventKind.DECISION, actor="sentinel", summary="decided to act"
    )
    assert first.sequence == 0
    assert first.previous_hash == GENESIS_HASH
    assert chain.head_hash == first.entry_hash


def test_every_entry_binds_to_the_one_before_it() -> None:
    entries = _chain().entries()
    assert [entry.sequence for entry in entries] == [0, 1, 2, 3]
    for previous, entry in pairwise(entries):
        assert entry.previous_hash == previous.entry_hash
        assert entry.follows(previous)


def test_an_intact_chain_verifies() -> None:
    result = verify_chain(_chain().entries())
    assert result.intact
    assert result.entries == 4
    assert result.broken_at is None


def test_an_empty_chain_is_intact_and_still_a_chain() -> None:
    assert verify_chain(()).intact
    assert verify_chain(()).head_hash == GENESIS_HASH
    assert bool(AuditChain()) is True


def test_the_same_events_always_produce_the_same_chain() -> None:
    """Determinism is what lets two copies of the ledger be compared at all."""
    assert _chain().head_hash == _chain().head_hash


# --- what a chain refuses ---------------------------------------------------


def test_an_entry_cannot_disagree_with_its_own_hash() -> None:
    """There is one derivation of the hash and no way around it."""
    entry = _chain(1).entries()[0]
    with pytest.raises(ValueError, match="must be the digest of this entry's own content"):
        entry.model_copy(update={"entry_hash": "f" * 64}).model_validate(
            entry.model_dump() | {"entry_hash": "f" * 64}
        )


def test_a_chain_that_starts_somewhere_else_is_refused() -> None:
    entry = _chain(2).entries()[1]
    with pytest.raises(ValueError, match="chain with a missing beginning"):
        AuditEntry.model_validate(entry.model_dump() | {"sequence": 0})


def test_two_writers_building_on_one_head_would_fork_the_chain() -> None:
    """The failure a single-writer append exists to make impossible."""
    chain = _chain(2)
    head = chain.head
    assert head is not None
    # A second writer that read the same head and minted its own successor.
    rival = build_entry(
        previous=head,
        ts=TICK + timedelta(seconds=99),
        kind=AuditEventKind.ACTION_REVERTED,
        actor="another-writer",
        summary="gave the cohort back",
    )
    chain.append(
        ts=TICK + timedelta(seconds=100),
        kind=AuditEventKind.ACTION_VERIFIED,
        actor="sentinel",
        summary="confirmed the restraint",
    )

    with pytest.raises(ChainForkedError, match="would fork the chain"):
        chain.adopt(rival)


# --- what a chain detects ---------------------------------------------------


def _tampered(entries: tuple[AuditEntry, ...], index: int, **changes: object) -> list[AuditEntry]:
    """Rewrite one entry the way somebody covering their tracks would.

    Built with `model_construct` so the contract's own validator does not stop
    the forgery - the point of the test is that the *chain* catches it, not that
    an attacker would politely use the constructor.
    """
    forged = list(entries)
    forged[index] = AuditEntry.model_construct(**(entries[index].model_dump() | changes))
    return forged


def test_editing_the_sentence_a_person_reads_is_caught() -> None:
    """The summary and the actor are hashed like everything else, and must be.

    A ledger that protected its structured payload but let somebody rewrite the
    human-readable line - and the name of who acted - would verify happily while
    saying whatever the last editor wanted.
    """
    entries = _chain().entries()
    assert not verify_chain(_tampered(entries, 1, summary="nothing happened here")).intact
    assert not verify_chain(_tampered(entries, 1, actor="somebody-else")).intact


def test_editing_which_incident_an_entry_belongs_to_is_caught() -> None:
    entries = _chain().entries()
    assert not verify_chain(_tampered(entries, 2, incident_id="incident-9")).intact


def test_editing_a_hashed_body_is_caught_at_that_entry() -> None:
    entries = _chain().entries()
    result = verify_chain(_tampered(entries, 1, body={"step": 99, "target": "frontend"}))

    assert not result.intact
    assert result.broken_at == 1
    assert result.detail is not None
    assert "does not match its own content" in result.detail


def test_editing_a_timestamp_is_caught() -> None:
    entries = _chain().entries()
    result = verify_chain(_tampered(entries, 2, ts=TICK - timedelta(hours=3)))

    assert not result.intact
    assert result.broken_at == 2


def test_removing_an_inconvenient_entry_is_caught() -> None:
    """The most tempting edit of all, and the one the chain exists for."""
    entries = list(_chain().entries())
    del entries[2]

    result = verify_chain(entries)
    assert not result.intact
    assert result.broken_at == 3
    assert result.detail is not None
    assert "does not follow the entry before it" in result.detail


def test_reordering_two_entries_is_caught() -> None:
    entries = list(_chain().entries())
    entries[1], entries[2] = entries[2], entries[1]

    assert not verify_chain(entries).intact


def test_rewriting_history_means_rewriting_all_of_it() -> None:
    """An attacker who re-chains from the edit is only detectable by the head hash.

    That is the honest property to state: the chain makes a *partial* rewrite
    impossible, and a total rewrite detectable to anyone who kept the old head.
    """
    original = _chain()
    rewritten = AuditChain()
    for index, entry in enumerate(original.entries()):
        rewritten.append(
            ts=entry.ts,
            kind=entry.kind,
            actor=entry.actor,
            summary=entry.summary,
            body={"step": index, "target": "frontend"} if index != 1 else {"step": 1},
            incident_id=entry.incident_id,
        )

    assert verify_chain(rewritten.entries()).intact, "a full rewrite is internally consistent"
    assert rewritten.head_hash != original.head_hash, "and it is exactly what the head hash shows"


def test_the_kind_of_an_entry_is_hashed_too() -> None:
    entries = _chain().entries()
    result = verify_chain(_tampered(entries, 1, kind=AuditEventKind.ACTION_REFUSED))

    assert not result.intact
    assert result.broken_at == 1
