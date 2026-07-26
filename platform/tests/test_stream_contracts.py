"""The browser stream carries one strict, extensible invalidation contract."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts import SnapshotInvalidation, SnapshotResource, StreamEventKind


def _event(**changes: object) -> SnapshotInvalidation:
    values: dict[str, object] = {
        "event_id": "event-1",
        "ts": datetime(2026, 7, 26, tzinfo=UTC),
        "kind": StreamEventKind.SNAPSHOT_INVALIDATE,
        "resources": (SnapshotResource.HEALTH,),
    }
    values.update(changes)
    return SnapshotInvalidation(**values)  # type: ignore[arg-type]


def test_snapshot_invalidations_are_strict_and_json_round_trip() -> None:
    event = _event()

    assert SnapshotInvalidation.model_validate_json(event.model_dump_json()) == event
    assert event.kind is StreamEventKind.SNAPSHOT_INVALIDATE
    assert event.resources == (SnapshotResource.HEALTH,)


@pytest.mark.parametrize(
    "resources",
    [
        (),
        (SnapshotResource.HEALTH, SnapshotResource.HEALTH),
        (SnapshotResource.ALL, SnapshotResource.HEALTH),
    ],
)
def test_snapshot_invalidations_cannot_be_empty_duplicated_or_ambiguous(
    resources: tuple[SnapshotResource, ...],
) -> None:
    with pytest.raises(ValidationError):
        _event(resources=resources)


def test_stream_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _event(answer_key=True)
