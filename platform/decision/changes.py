"""The MVP change-evidence source: an operator ledger plus observed cluster change.

Three feeds answer the same question - what did we change, and when:

* the committed ledger (`config/deployments.yml`), which is what the team says
  it shipped;
* Kubernetes rollout events, which are what the cluster actually did;
* flagd flag changes, which are what a feature flip actually did.

All three are normalized to one ``ChangeEvent`` so the change agent reasons
about change, not about where the record came from. Each event keeps its own
honesty label, because the ledger is asserted while the other two are observed.
The full change ledger with real VCS provenance lands with the RCA phase; this
module is deliberately the minimum that is honest.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from contracts import ChangeEvent, ChangeKind
from decision.config import DeploymentLedgerConfig

LEDGER_SOURCE = "operator-ledger"
ROLLOUT_SOURCE = "k8s-rollout-events"
FLAG_SOURCE = "flagd-change-feed"


class UnmappedChangeError(ValueError):
    """An observed change could not be attributed to a logical topology service."""


@dataclass(frozen=True, slots=True)
class RolloutEvent:
    """One workload revision observed rolling out in the cluster."""

    workload: str
    revision: str
    ts: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class FlagChange:
    """One feature-flag variant change observed on the flag provider."""

    flag: str
    previous_variant: str
    current_variant: str
    ts: datetime


class ChangeFeed:
    """Every known change, deduplicated and answerable by event-time window."""

    def __init__(self, changes: Iterable[ChangeEvent] = ()) -> None:
        seen: dict[str, ChangeEvent] = {}
        for change in changes:
            if not isinstance(change, ChangeEvent):
                raise TypeError("a change feed holds ChangeEvent values")
            existing = seen.get(change.change_id)
            if existing is not None and existing != change:
                raise ValueError(f"conflicting records for change {change.change_id}")
            seen[change.change_id] = change
        self._changes = tuple(
            sorted(seen.values(), key=lambda change: (change.ts, change.change_id))
        )

    @property
    def changes(self) -> tuple[ChangeEvent, ...]:
        """Every known change in event-time order."""
        return self._changes

    def within(self, ts: datetime, *, lookback_seconds: float) -> tuple[ChangeEvent, ...]:
        """Return the changes in the half-open window ``(ts - lookback, ts]``.

        The window is half-open on the old side so a change exactly at the
        correlation horizon has already decayed to nothing and is not evidence.
        """
        if lookback_seconds <= 0.0:
            raise ValueError("lookback_seconds must be greater than zero")
        if ts.utcoffset() != timedelta(0):
            raise ValueError("ts must be timezone-aware UTC")
        upper = ts.astimezone(UTC)
        lower = upper - timedelta(seconds=lookback_seconds)
        return tuple(change for change in self._changes if lower < change.ts <= upper)

    def merge(self, changes: Iterable[ChangeEvent]) -> ChangeFeed:
        """Return a new feed carrying this feed's changes plus more."""
        return ChangeFeed((*self._changes, *changes))


def ledger_changes(ledger: DeploymentLedgerConfig) -> tuple[ChangeEvent, ...]:
    """Normalize the committed deployment ledger into change events."""
    return tuple(
        ChangeEvent(
            change_id=record.change_id,
            kind=record.kind,
            service=record.service,
            ts=record.ts,
            summary=record.summary,
            source=LEDGER_SOURCE,
            honesty=record.honesty,
            revision=record.revision,
        )
        for record in ledger.changes
    )


def rollout_changes(
    events: Iterable[RolloutEvent],
    *,
    ledger: DeploymentLedgerConfig,
    honesty: Literal["REAL", "SIMULATED"],
) -> tuple[ChangeEvent, ...]:
    """Normalize observed workload rollouts, refusing to guess a service name."""
    normalized: list[ChangeEvent] = []
    for event in events:
        service = ledger.workload_mappings.get(event.workload)
        if service is None:
            raise UnmappedChangeError(
                f"workload {event.workload} has no configured topology service"
            )
        normalized.append(
            ChangeEvent(
                change_id=f"rollout:{event.workload}:{event.revision}",
                kind=ChangeKind.ROLLOUT,
                service=service,
                ts=_utc(event.ts, name="rollout ts"),
                summary=f"{event.workload} rolled out revision {event.revision} ({event.reason})",
                source=ROLLOUT_SOURCE,
                honesty=honesty,
                revision=event.revision,
            )
        )
    return tuple(normalized)


def flag_changes(
    changes: Iterable[FlagChange],
    *,
    ledger: DeploymentLedgerConfig,
    honesty: Literal["REAL", "SIMULATED"],
) -> tuple[ChangeEvent, ...]:
    """Normalize observed flag flips, refusing to guess a service name."""
    normalized: list[ChangeEvent] = []
    for change in changes:
        service = ledger.flag_mappings.get(change.flag)
        if service is None:
            raise UnmappedChangeError(f"flag {change.flag} has no configured topology service")
        ts = _utc(change.ts, name="flag ts")
        normalized.append(
            ChangeEvent(
                change_id=f"flag:{change.flag}:{ts.isoformat()}",
                kind=ChangeKind.FLAG,
                service=service,
                ts=ts,
                summary=(
                    f"flag {change.flag} moved from {change.previous_variant} "
                    f"to {change.current_variant}"
                ),
                source=FLAG_SOURCE,
                honesty=honesty,
                revision=change.current_variant,
            )
        )
    return tuple(normalized)


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)
