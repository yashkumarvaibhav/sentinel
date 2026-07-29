"""Validated records persisted by Sentinel's runtime repositories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator

type RecordId = Annotated[str, Field(min_length=1, max_length=255)]
type RecordText = Annotated[str, Field(min_length=1, max_length=4096)]
type Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


type UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class StorageRecord(BaseModel):
    """Strict immutable boundary for durable runtime state."""

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class IncidentRecord(StorageRecord):
    """Current incident state and the evidence-derived payload behind it."""

    incident_id: RecordId
    state: RecordText
    created_at: UtcDatetime
    updated_at: UtcDatetime
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        return self


class IncidentGraphRecord(StorageRecord):
    """One materialized current-incident graph stored beside its incident."""

    incident_id: RecordId
    updated_at: UtcDatetime
    payload: dict[str, JsonValue]


class IncidentDetailRecord(StorageRecord):
    """One evidence-complete proof snapshot stored beside its incident."""

    incident_id: RecordId
    updated_at: UtcDatetime
    payload: dict[str, JsonValue]


class IncidentSecurityRecord(StorageRecord):
    """One evidence-only security snapshot stored beside its incident."""

    incident_id: RecordId
    updated_at: UtcDatetime
    payload: dict[str, JsonValue]


class AuditRecord(StorageRecord):
    """One immutable hash-linked runtime audit entry."""

    entry_id: RecordId
    ts: UtcDatetime
    event_type: RecordText
    payload: dict[str, JsonValue]
    prev_hash: Sha256Hex
    entry_hash: Sha256Hex
