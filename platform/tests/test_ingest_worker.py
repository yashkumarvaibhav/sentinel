"""The worker commits raw offsets only after every durable output succeeds."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ingest.normalizer import RawSignal
from ingest.worker import BoundedDeduplicator, IngestWorker, RawMessage


def test_success_writes_clickhouse_then_bus_then_commits() -> None:
    asyncio.run(_success_case())


async def _success_case() -> None:
    events: list[str] = []
    sink = _Sink(events)
    producer = _Producer(events)
    committer = _Committer(events)
    worker = IngestWorker(
        sink=sink,
        producer=producer,
        committer=committer,
        deduplicator=BoundedDeduplicator(capacity=100),
    )
    message = RawMessage(
        signal=RawSignal.METRICS,
        topic="otlp.raw.metrics",
        partition=2,
        offset=41,
        value=_metric_bytes(),
    )

    await worker.handle(message)

    assert events == ["sink", "produce:obs.normalized", "commit"]
    assert len(sink.writes[0]) == 1
    topic, key, value = producer.messages[0]
    observation = sink.writes[0][0]
    assert topic == "obs.normalized"
    assert key == b"frontend\x00request_rate"
    assert json.loads(value) == observation.model_dump(mode="json")
    assert committer.messages == [message]


def test_failed_output_does_not_commit_or_poison_retry_dedup() -> None:
    asyncio.run(_retry_case())


async def _retry_case() -> None:
    events: list[str] = []
    sink = _Sink(events)
    producer = _Producer(events, fail=True)
    committer = _Committer(events)
    deduplicator = BoundedDeduplicator(capacity=100)
    worker = IngestWorker(
        sink=sink,
        producer=producer,
        committer=committer,
        deduplicator=deduplicator,
    )
    message = RawMessage(
        signal=RawSignal.METRICS,
        topic="otlp.raw.metrics",
        partition=0,
        offset=7,
        value=_metric_bytes(),
    )

    with pytest.raises(RuntimeError, match="producer failed"):
        await worker.handle(message)
    assert committer.messages == []
    assert deduplicator.size == 0

    producer.fail = False
    await worker.handle(message)
    assert len(sink.writes) == 2  # ClickHouse converges duplicate ids with ReplacingMergeTree.
    assert committer.messages == [message]
    assert deduplicator.size == 1


def test_duplicate_redelivery_commits_without_rewriting_state() -> None:
    asyncio.run(_duplicate_case())


async def _duplicate_case() -> None:
    events: list[str] = []
    sink = _Sink(events)
    producer = _Producer(events)
    committer = _Committer(events)
    worker = IngestWorker(
        sink=sink,
        producer=producer,
        committer=committer,
        deduplicator=BoundedDeduplicator(capacity=100),
    )
    message = RawMessage(
        signal=RawSignal.METRICS,
        topic="otlp.raw.metrics",
        partition=0,
        offset=7,
        value=_metric_bytes(),
    )

    await worker.handle(message)
    await worker.handle(message)

    assert len(sink.writes) == 1
    assert len(producer.messages) == 1
    assert committer.messages == [message, message]
    assert worker.stats().duplicates == 1


def test_malformed_raw_message_goes_to_deterministic_dlq_then_commits() -> None:
    asyncio.run(_dlq_case())


async def _dlq_case() -> None:
    events: list[str] = []
    sink = _Sink(events)
    producer = _Producer(events)
    committer = _Committer(events)
    worker = IngestWorker(
        sink=sink,
        producer=producer,
        committer=committer,
        deduplicator=BoundedDeduplicator(capacity=100),
    )
    message = RawMessage(
        signal=RawSignal.METRICS,
        topic="otlp.raw.metrics",
        partition=1,
        offset=9,
        value=b"broken",
    )

    await worker.handle(message)

    assert events == ["produce:otlp.dlq", "commit"]
    topic, key, encoded = producer.messages[0]
    body = json.loads(encoded)
    assert topic == "otlp.dlq"
    assert key == b"otlp.raw.metrics:1:9"
    assert body["source"] == {"topic": "otlp.raw.metrics", "partition": 1, "offset": 9}
    assert body["payload_sha256"]
    assert "payload" not in body
    assert worker.stats().dead_lettered == 1


class _Sink:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.writes: list[tuple[Any, ...]] = []

    async def write_observations(self, records: tuple[Any, ...]) -> None:
        self.events.append("sink")
        self.writes.append(records)


class _Producer:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.messages: list[tuple[str, bytes, bytes]] = []

    async def send(self, topic: str, *, key: bytes, value: bytes) -> None:
        self.events.append(f"produce:{topic}")
        if self.fail:
            raise RuntimeError("producer failed")
        self.messages.append((topic, key, value))


class _Committer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.messages: list[RawMessage] = []

    async def commit(self, message: RawMessage) -> None:
        self.events.append("commit")
        self.messages.append(message)


def _metric_bytes() -> bytes:
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "frontend"}}]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "request_rate",
                                "unit": "requests/s",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": "1784635200000000000",
                                            "asDouble": 7.5,
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
    return json.dumps(payload, separators=(",", ":")).encode()
