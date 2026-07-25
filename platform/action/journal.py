"""The idempotency journal: what effect is currently in place, keyed by effect.

Every actuator call is recorded here against the plan's idempotency key, and the
latest outcome for a key is the platform's belief about the world. That belief
is what makes an apply idempotent: asking for an effect that is already in force
returns what already happened instead of doing it again.

Two rules are worth stating plainly, because both are the difference between an
idempotency key that works and one that only looks like it does:

* **A failed apply does not close the key.** It is retryable - that is the whole
  reason the key exists - but it is *not* evidence that the world is unchanged,
  so it never counts as in force either. Only a ``verify`` settles what really
  happened after a failure.
* **An in-force effect is never evicted.** Forgetting a key that names a live
  change is exactly how a system double-applies. When the journal is full of
  effects that are all still in place, that is thousands of outstanding
  un-reverted actions - a runaway, not a capacity problem - and it stops rather
  than forgetting.

This is a per-process journal in front of the durable, hash-chained ledger the
audit plane owns. It is deliberately small and deterministic: replaying a
capture through it produces the same deduplication decisions every time.
"""

from __future__ import annotations

from contracts import ActionOutcome


class ActionJournalFullError(RuntimeError):
    """Every remembered effect is still in force; nothing may be forgotten."""


class ActionJournal:
    """The per-key record of what the action plane last did about one effect."""

    __slots__ = ("_capacity", "_entries")

    def __init__(self, *, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("an action journal must be able to remember at least one effect")
        self._capacity = capacity
        # Insertion-ordered: the eviction sweep drops the oldest released key,
        # and re-recording a key moves it to the end so a busy effect survives.
        self._entries: dict[str, ActionOutcome] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        """An empty journal is still a journal.

        Without this, `__len__` makes a fresh journal falsy and `journal or
        ActionJournal(...)` silently builds a second one that nothing writes to.
        That exact bug cost a debugging session here, so the trap is closed at
        the source rather than documented at each call site.
        """
        return True

    def latest(self, idempotency_key: str) -> ActionOutcome | None:
        """The most recent outcome recorded against this effect."""
        return self._entries.get(idempotency_key)

    def in_force(self, idempotency_key: str) -> bool:
        """Whether this effect is believed to be in place on its target."""
        outcome = self._entries.get(idempotency_key)
        return outcome is not None and outcome.in_force

    def ensure_room(self, idempotency_key: str) -> None:
        """Refuse a new effect the journal could not remember, before it happens.

        Called before the adapter rather than after: applying something and then
        failing to write it down is the one failure mode worse than not applying
        it at all.
        """
        if idempotency_key in self._entries or len(self._entries) < self._capacity:
            return
        if any(not outcome.in_force for outcome in self._entries.values()):
            return
        raise ActionJournalFullError(
            f"all {len(self._entries)} remembered effects are still in force; "
            "recording another one would mean forgetting a live change"
        )

    def record(self, outcome: ActionOutcome) -> None:
        """Record what an actuator call did, evicting only released keys if needed."""
        if outcome.dry_run:
            # A dry run changed nothing, so it says nothing about what is in
            # place. Recording it would let a simulation deduplicate a real
            # apply, which is the one way a dry-run mode could cause an outage.
            return
        self._entries.pop(outcome.idempotency_key, None)
        self._entries[outcome.idempotency_key] = outcome
        self._evict()

    def entries(self) -> tuple[ActionOutcome, ...]:
        """Every remembered outcome, oldest first."""
        return tuple(self._entries.values())

    def _evict(self) -> None:
        while len(self._entries) > self._capacity:
            released = next(
                (key for key, outcome in self._entries.items() if not outcome.in_force),
                None,
            )
            if released is None:
                raise ActionJournalFullError(
                    f"all {len(self._entries)} remembered effects are still in force; "
                    "forgetting one would risk applying it twice"
                )
            del self._entries[released]
