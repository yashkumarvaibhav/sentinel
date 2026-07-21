"""The live recorder owns exact, complete raw-topic offset ranges."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

import pytest
from aiokafka.structs import TopicPartition
from lab.captures.broker import (
    BrokerSnapshot,
    ConsumerRecord,
    TopicPosition,
    bounds_between,
    read_bounded_records,
    snapshot_offsets,
)


@dataclass(frozen=True)
class _Record:
    topic: str
    partition: int
    offset: int
    value: bytes | None


class _Consumer:
    def __init__(self) -> None:
        self.assigned: tuple[TopicPartition, ...] = ()
        self.seeks: dict[TopicPartition, int] = {}
        self.batches: list[dict[TopicPartition, list[ConsumerRecord]]] = []

    async def topic_partitions(self, topic: str) -> set[int] | None:
        return {0}

    async def end_offsets(self, partitions: Sequence[TopicPartition]) -> dict[TopicPartition, int]:
        return {partition: index + 10 for index, partition in enumerate(partitions)}

    def assign(self, partitions: Sequence[TopicPartition]) -> None:
        self.assigned = tuple(partitions)

    def seek(self, partition: TopicPartition, offset: int) -> None:
        self.seeks[partition] = offset

    async def getmany(
        self, *, timeout_ms: int, max_records: int
    ) -> dict[TopicPartition, list[ConsumerRecord]]:
        del timeout_ms, max_records
        return self.batches.pop(0) if self.batches else {}


def test_snapshot_covers_all_raw_topics_in_canonical_order() -> None:
    consumer = _Consumer()
    snapshot = asyncio.run(snapshot_offsets(consumer, consumer))

    assert [(item.topic, item.partition) for item in snapshot.positions] == [
        ("otlp.raw.logs", 0),
        ("otlp.raw.metrics", 0),
        ("otlp.raw.traces", 0),
    ]


def test_bounds_reject_partition_drift_and_watermark_rewind() -> None:
    before = _snapshot(10, 20, 30)
    drifted = BrokerSnapshot(
        positions=(
            *before.positions,
            TopicPosition(topic="otlp.raw.traces", partition=1, offset=31),
        )
    )
    with pytest.raises(ValueError, match="partitions changed"):
        bounds_between(before, drifted)
    with pytest.raises(ValueError, match="moved backwards"):
        bounds_between(before, _snapshot(9, 20, 30))


def test_bounded_read_seeks_and_collects_every_declared_offset() -> None:
    before = _snapshot(10, 20, 30)
    bounds = bounds_between(before, _snapshot(12, 20, 31))
    consumer = _Consumer()
    metrics = TopicPartition("otlp.raw.metrics", 0)
    traces = TopicPartition("otlp.raw.traces", 0)
    consumer.batches = [
        {
            metrics: [
                _Record(metrics.topic, metrics.partition, 10, b"m10"),
                _Record(metrics.topic, metrics.partition, 11, b"m11"),
            ],
            traces: [_Record(traces.topic, traces.partition, 30, b"t30")],
        }
    ]

    records = asyncio.run(read_bounded_records(consumer, bounds, timeout_seconds=1))

    assert [(item.topic, item.offset, item.value) for item in records] == [
        ("otlp.raw.metrics", 10, b"m10"),
        ("otlp.raw.metrics", 11, b"m11"),
        ("otlp.raw.traces", 30, b"t30"),
    ]
    assert consumer.seeks[TopicPartition("otlp.raw.logs", 0)] == 20


def test_bounded_read_rejects_offset_gaps() -> None:
    bounds = bounds_between(_snapshot(10, 20, 30), _snapshot(12, 20, 30))
    consumer = _Consumer()
    metrics = TopicPartition("otlp.raw.metrics", 0)
    consumer.batches = [{metrics: [_Record(metrics.topic, metrics.partition, 11, b"m11")]}]

    with pytest.raises(RuntimeError, match="offset gap"):
        asyncio.run(read_bounded_records(consumer, bounds, timeout_seconds=1))


def _snapshot(metrics: int, logs: int, traces: int) -> BrokerSnapshot:
    return BrokerSnapshot(
        positions=(
            TopicPosition(topic="otlp.raw.logs", partition=0, offset=logs),
            TopicPosition(topic="otlp.raw.metrics", partition=0, offset=metrics),
            TopicPosition(topic="otlp.raw.traces", partition=0, offset=traces),
        )
    )
