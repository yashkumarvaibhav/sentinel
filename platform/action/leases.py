"""Per-target leases: only one actor may be changing one target at a time.

The idempotency key stops the *same* effect being applied twice. A lease stops
two *different* effects racing on one target - a rate-limit landing halfway
through a rollback is a state nobody planned and nobody can reason about
afterwards.

Two deliberate properties:

* **Acquisition fails fast, it never queues.** An action plane that waits behind
  a lock applies its action late, against telemetry that has moved on. Refusing
  is honest: the caller can retry on the next tick with fresh evidence, which is
  what we want it to do anyway.
* **A lease expires.** An executor that crashes mid-apply must not block
  remediation of that service forever. The TTL is therefore an operator-owned
  number that has to exceed the slowest actuator call, and the configuration
  file says so - if it does not, two actors can overlap and the lease has
  quietly stopped doing its job.

Time is passed in rather than read from the wall clock, so a replay leases and
expires exactly as the live path did.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta


class TargetBusyError(RuntimeError):
    """Another actor holds the lease on this target right now."""


@dataclass(frozen=True, slots=True)
class TargetLease:
    """One actor's exclusive claim on one target, until it is released or expires."""

    target: str
    owner: str
    acquired_ts: datetime
    expires_ts: datetime

    def is_live(self, now: datetime) -> bool:
        """Whether this lease still holds at this moment."""
        return now < self.expires_ts


class LeaseRegistry:
    """The in-process holder of every live target lease.

    The registry is per-process, which is enough for a single action plane and
    is the seam a distributed lock (Postgres advisory lock, alongside the
    single-writer audit chain) slots into unchanged: the interface is acquire,
    release, and a fail-fast refusal.
    """

    __slots__ = ("_held", "_ttl")

    def __init__(self, *, ttl: timedelta) -> None:
        if ttl <= timedelta(0):
            raise ValueError("a lease ttl must be positive")
        self._ttl = ttl
        self._held: dict[str, TargetLease] = {}

    def holder(self, target: str, *, now: datetime) -> TargetLease | None:
        """The live lease on this target, if there is one."""
        lease = self._held.get(target)
        if lease is None:
            return None
        if not lease.is_live(now):
            # A stale lease is not a held target. Dropping it here rather than on
            # a timer keeps expiry a pure function of the time that was passed in.
            del self._held[target]
            return None
        return lease

    def acquire(self, target: str, *, owner: str, now: datetime) -> TargetLease:
        """Claim this target, or refuse at once if somebody else holds it."""
        if not target or not owner:
            raise ValueError("a lease needs both a target and an owner")
        held = self.holder(target, now=now)
        if held is not None:
            if held.owner == owner:
                # The same actor asking again gets what it already has, at the
                # deadline it already has. Refusing would deadlock a revert
                # nested inside an apply; extending would let one actor hold a
                # target indefinitely by asking repeatedly.
                return held
            raise TargetBusyError(
                f"{target} is leased by {held.owner} until {held.expires_ts.isoformat()}"
            )
        lease = TargetLease(target=target, owner=owner, acquired_ts=now, expires_ts=now + self._ttl)
        self._held[target] = lease
        return lease

    def release(self, lease: TargetLease) -> None:
        """Give up a lease. Releasing one that already expired is not an error."""
        held = self._held.get(lease.target)
        if held is None:
            return
        if held.owner != lease.owner:
            raise TargetBusyError(
                f"{lease.target} is held by {held.owner}, not by {lease.owner}; "
                "an actor may only release its own lease"
            )
        del self._held[lease.target]

    @contextmanager
    def hold(self, target: str, *, owner: str, now: datetime) -> Iterator[TargetLease]:
        """Hold a target for the duration of one actuator call.

        A nested hold by the same owner does not release on the way out: the
        outer block still needs the lease it took, and only the block that
        actually acquired the target gives it back.
        """
        held = self.holder(target, now=now)
        reentrant = held is not None and held.owner == owner
        lease = self.acquire(target, owner=owner, now=now)
        try:
            yield lease
        finally:
            if not reentrant:
                self.release(lease)
