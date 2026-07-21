"""Shared validation primitives for cross-plane contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


def _require_utc(value: datetime) -> datetime:
    """Reject naive/non-UTC event time and normalize the UTC tzinfo object."""
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


class ContractModel(BaseModel):
    """Strict, field-frozen envelope used at every plane boundary."""

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


type Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
type SignalName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
type HumanText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]
type UnitName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
type FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
type Probability = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False),
]
type UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


def ensure_unique(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    """Require reference collections to behave as ordered sets."""
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values
