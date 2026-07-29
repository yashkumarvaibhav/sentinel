"""Typed browser-stream events.

The stream carries invalidations rather than pretending it is a second
database. A reconnect always asks the browser to refetch authoritative REST
snapshots, so a missed event cannot leave a screen permanently stale.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator

from contracts._base import ContractModel, Identifier, UtcDatetime, ensure_unique


class StreamEventKind(StrEnum):
    """Named SSE events understood by the product surface."""

    SNAPSHOT_INVALIDATE = "snapshot.invalidate"


class SnapshotResource(StrEnum):
    """Authoritative snapshots an invalidation may ask the browser to refetch."""

    ALL = "all"
    HEALTH = "health"
    DECOMPOSITION = "decomposition"
    INCIDENTS = "incidents"
    ACTIONS = "actions"
    SECURITY = "security"
    AUDIT = "audit"


class SnapshotInvalidation(ContractModel):
    """One typed signal that one or more REST snapshots are stale."""

    event_id: Identifier
    ts: UtcDatetime
    kind: StreamEventKind = StreamEventKind.SNAPSHOT_INVALIDATE
    resources: tuple[SnapshotResource, ...] = Field(min_length=1)

    @field_validator("resources")
    @classmethod
    def resources_are_an_unambiguous_set(
        cls, resources: tuple[SnapshotResource, ...]
    ) -> tuple[SnapshotResource, ...]:
        """`all` stands alone; duplicates make refetch ownership ambiguous."""
        ensure_unique(tuple(resource.value for resource in resources), field_name="resources")
        if SnapshotResource.ALL in resources and len(resources) != 1:
            raise ValueError("the all resource must be the only resource")
        return resources
