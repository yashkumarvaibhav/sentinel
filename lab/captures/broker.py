"""Exact raw-topic watermark snapshots and bounded Kafka reads."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Protocol, cast

from aiokafka.structs import TopicPartition
from pydantic import Field, model_validator

from lab.captures.models import CaptureModel, RawTopic
from lab.captures.store import CaptureSourceRecord, TopicBounds

RAW_TOPICS: tuple[RawTopic, ...] = (
    "otlp.raw.metrics",
    "otlp.raw.logs",
    "otlp.raw.traces",
)


class TopicPosition(CaptureModel):
    topic: RawTopic
    partition: int = Field(ge=0)
    offset: int = Field(ge=0)


class BrokerSnapshot(CaptureModel):
    version: int = Field(default=1, ge=1, le=1)
    positions: tuple[TopicPosition, ...]

    @model_validator(mode="after")
    def complete_sorted_sources(self) -> BrokerSnapshot:
        sources = tuple((item.topic, item.partition) for item in self.positions)
        if sources != tuple(sorted(sources)) or len(sources) != len(set(sources)):
            raise ValueError("broker snapshot sources must be unique and sorted")
        if {item.topic for item in self.positions} != set(RAW_TOPICS):
            raise ValueError("broker snapshot must include all three raw topics")
        return self


class ConsumerRecord(Protocol):
    @property
    def topic(self) -> str: ...

    @property
    def partition(self) -> int: ...

    @property
    def offset(self) -> int: ...

    @property
    def value(self) -> bytes | None: ...


class BoundedConsumer(Protocol):
    async def end_offsets(
        self, partitions: Sequence[TopicPartition]
    ) -> dict[TopicPartition, int]: ...

    def assign(self, partitions: Sequence[TopicPartition]) -> None: ...

    def seek(self, partition: TopicPartition, offset: int) -> None: ...

    async def getmany(
        self, *, timeout_ms: int, max_records: int
    ) -> dict[TopicPartition, list[ConsumerRecord]]: ...


class PartitionCatalog(Protocol):
    async def topic_partitions(self, topic: RawTopic) -> set[int] | None: ...


async def snapshot_offsets(
    consumer: BoundedConsumer,
    catalog: PartitionCatalog,
) -> BrokerSnapshot:
    partitions: list[TopicPartition] = []
    for topic in RAW_TOPICS:
        topic_partitions = await catalog.topic_partitions(topic)
        if not topic_partitions:
            raise RuntimeError(f"raw topic has no partitions: {topic}")
        partitions.extend(TopicPartition(topic, item) for item in sorted(topic_partitions))
    offsets = await consumer.end_offsets(partitions)
    if set(offsets) != set(partitions):
        raise RuntimeError("broker omitted a raw-topic high watermark")
    return BrokerSnapshot(
        positions=tuple(
            TopicPosition(
                topic=cast(RawTopic, item.topic),
                partition=item.partition,
                offset=offsets[item],
            )
            for item in sorted(partitions)
        )
    )


def bounds_between(before: BrokerSnapshot, after: BrokerSnapshot) -> tuple[TopicBounds, ...]:
    starts = {(item.topic, item.partition): item.offset for item in before.positions}
    ends = {(item.topic, item.partition): item.offset for item in after.positions}
    if starts.keys() != ends.keys():
        raise ValueError("raw-topic partitions changed during capture")
    bounds: list[TopicBounds] = []
    for (topic, partition), start in sorted(starts.items()):
        end = ends[(topic, partition)]
        if end < start:
            raise ValueError(f"raw-topic high watermark moved backwards: {topic}:{partition}")
        bounds.append(
            TopicBounds(
                topic=topic,
                partition=partition,
                start_offset=start,
                end_offset=end,
            )
        )
    return tuple(bounds)


async def read_bounded_records(
    consumer: BoundedConsumer,
    bounds: tuple[TopicBounds, ...],
    *,
    timeout_seconds: float = 30.0,
) -> tuple[CaptureSourceRecord, ...]:
    if timeout_seconds <= 0:
        raise ValueError("bounded read timeout must be positive")
    partitions = [TopicPartition(item.topic, item.partition) for item in bounds]
    consumer.assign(partitions)
    expected = {TopicPartition(item.topic, item.partition): item.start_offset for item in bounds}
    ends = {TopicPartition(item.topic, item.partition): item.end_offset for item in bounds}
    for partition in partitions:
        consumer.seek(partition, expected[partition])
    records: list[CaptureSourceRecord] = []
    deadline = time.monotonic() + timeout_seconds
    while any(expected[item] < ends[item] for item in partitions):
        if time.monotonic() >= deadline:
            missing = ", ".join(
                f"{item.topic}:{item.partition}@{expected[item]}..{ends[item]}"
                for item in partitions
                if expected[item] < ends[item]
            )
            raise TimeoutError(f"raw capture did not reach bounded offsets: {missing}")
        batches = await consumer.getmany(timeout_ms=500, max_records=1000)
        for partition, batch in batches.items():
            if partition not in expected:
                raise RuntimeError(f"broker returned an unassigned partition: {partition}")
            for record in batch:
                if record.offset >= ends[partition]:
                    continue
                wanted = expected[partition]
                if record.offset != wanted:
                    raise RuntimeError(
                        f"raw capture offset gap: {partition.topic}:{partition.partition} "
                        f"expected {wanted}, received {record.offset}"
                    )
                if record.value is None:
                    raise RuntimeError(
                        f"raw capture record has no value: "
                        f"{partition.topic}:{partition.partition}:{record.offset}"
                    )
                records.append(
                    CaptureSourceRecord(
                        topic=cast(RawTopic, partition.topic),
                        partition=partition.partition,
                        offset=record.offset,
                        value=record.value,
                    )
                )
                expected[partition] += 1
    return tuple(sorted(records, key=lambda item: (item.topic, item.partition, item.offset)))
