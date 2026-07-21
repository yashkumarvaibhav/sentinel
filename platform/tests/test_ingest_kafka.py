"""Kafka envelopes preserve source coordinates and reject unexpected topics."""

from __future__ import annotations

import pytest
from aiokafka.structs import ConsumerRecord

from ingest.kafka import raw_message
from ingest.normalizer import RawSignal


def test_raw_message_preserves_bus_evidence() -> None:
    record: ConsumerRecord[None, bytes] = ConsumerRecord(
        topic="otlp.raw.metrics",
        partition=2,
        offset=41,
        timestamp=0,
        timestamp_type=0,
        key=None,
        value=b'{"resourceMetrics":[]}',
        checksum=None,
        serialized_key_size=-1,
        serialized_value_size=22,
        headers=(),
    )

    message = raw_message(record)

    assert message.signal is RawSignal.METRICS
    assert (message.topic, message.partition, message.offset, message.value) == (
        "otlp.raw.metrics",
        2,
        41,
        b'{"resourceMetrics":[]}',
    )


def test_raw_message_rejects_unknown_topics_and_tombstones() -> None:
    unknown = _record("some.other.topic", b"{}")
    tombstone = _record("otlp.raw.logs", None)

    with pytest.raises(ValueError, match="unsupported raw topic"):
        raw_message(unknown)
    with pytest.raises(ValueError, match="must have a value"):
        raw_message(tombstone)


def _record(topic: str, value: bytes | None) -> ConsumerRecord[None, bytes]:
    return ConsumerRecord(
        topic=topic,
        partition=0,
        offset=0,
        timestamp=0,
        timestamp_type=0,
        key=None,
        value=value,
        checksum=None,
        serialized_key_size=-1,
        serialized_value_size=-1,
        headers=(),
    )
