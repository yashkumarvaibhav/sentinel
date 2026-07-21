"""Thin aiokafka adapters that keep broker details out of ingest semantics."""

from __future__ import annotations

from typing import Protocol

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import TopicPartition

from ingest.normalizer import RawSignal
from ingest.worker import RawMessage

RAW_TOPICS: dict[str, RawSignal] = {
    "otlp.raw.metrics": RawSignal.METRICS,
    "otlp.raw.logs": RawSignal.LOGS,
    "otlp.raw.traces": RawSignal.TRACES,
}


class RawConsumerRecord(Protocol):
    topic: str
    partition: int
    offset: int
    value: bytes | None


class KafkaProducer:
    """Await broker acknowledgement for every normalized or DLQ record."""

    def __init__(self, producer: AIOKafkaProducer) -> None:
        self._producer = producer

    async def send(self, topic: str, *, key: bytes, value: bytes) -> None:
        await self._producer.send_and_wait(  # type: ignore[no-untyped-call]
            topic, key=key, value=value
        )


class KafkaCommitter:
    """Commit exactly the next offset for the raw record just completed."""

    def __init__(self, consumer: AIOKafkaConsumer) -> None:
        self._consumer = consumer

    async def commit(self, message: RawMessage) -> None:
        partition = TopicPartition(message.topic, message.partition)
        await self._consumer.commit(  # type: ignore[no-untyped-call]
            {partition: message.offset + 1}
        )


def raw_message(record: RawConsumerRecord) -> RawMessage:
    """Translate broker metadata without interpreting the OTLP payload."""
    signal = RAW_TOPICS.get(record.topic)
    if signal is None:
        raise ValueError(f"unsupported raw topic: {record.topic}")
    if record.value is None:
        raise ValueError("raw telemetry records must have a value")
    return RawMessage(
        signal=signal,
        topic=record.topic,
        partition=record.partition,
        offset=record.offset,
        value=record.value,
    )
