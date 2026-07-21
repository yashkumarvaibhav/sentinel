"""Retry-safe raw telemetry worker ordering durable outputs before offset commits."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from contracts import Observation
from ingest.normalizer import NormalizationError, RawSignal, normalize_otlp_json


@dataclass(frozen=True)
class RawMessage:
    signal: RawSignal
    topic: str
    partition: int
    offset: int
    value: bytes


@dataclass(frozen=True)
class WorkerStats:
    normalized: int
    duplicates: int
    dead_lettered: int


class ObservationSink(Protocol):
    async def write_observations(self, records: tuple[Observation, ...]) -> None: ...


class MessageProducer(Protocol):
    async def send(self, topic: str, *, key: bytes, value: bytes) -> None: ...


class OffsetCommitter(Protocol):
    async def commit(self, message: RawMessage) -> None: ...


class BoundedDeduplicator:
    """An LRU evidence-id set updated only after all outputs succeed."""

    def __init__(self, *, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._ids)

    def contains(self, observation_id: str) -> bool:
        return observation_id in self._ids

    def remember(self, observation_ids: tuple[str, ...]) -> None:
        for observation_id in observation_ids:
            self._ids[observation_id] = None
            self._ids.move_to_end(observation_id)
        while len(self._ids) > self._capacity:
            self._ids.popitem(last=False)


class IngestWorker:
    """Normalize one raw record and advance its offset after every durable output."""

    def __init__(
        self,
        *,
        sink: ObservationSink,
        producer: MessageProducer,
        committer: OffsetCommitter,
        deduplicator: BoundedDeduplicator,
    ) -> None:
        self._sink = sink
        self._producer = producer
        self._committer = committer
        self._deduplicator = deduplicator
        self._normalized = 0
        self._duplicates = 0
        self._dead_lettered = 0

    async def handle(self, message: RawMessage) -> None:
        try:
            normalized = normalize_otlp_json(message.signal, message.value)
        except NormalizationError as exc:
            await self._dead_letter(message, str(exc))
            await self._committer.commit(message)
            self._dead_lettered += 1
            return

        fresh: list[Observation] = []
        batch_ids: set[str] = set()
        for observation in normalized:
            if self._deduplicator.contains(observation.observation_id):
                self._duplicates += 1
                continue
            if observation.observation_id in batch_ids:
                self._duplicates += 1
                continue
            batch_ids.add(observation.observation_id)
            fresh.append(observation)

        records = tuple(fresh)
        if records:
            await self._sink.write_observations(records)
            for observation in records:
                await self._producer.send(
                    "obs.normalized",
                    key=f"{observation.service}\0{observation.signal}".encode(),
                    value=json.dumps(
                        observation.model_dump(mode="json"),
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode(),
                )
            self._deduplicator.remember(tuple(item.observation_id for item in records))
            self._normalized += len(records)
        await self._committer.commit(message)

    def stats(self) -> WorkerStats:
        return WorkerStats(
            normalized=self._normalized,
            duplicates=self._duplicates,
            dead_lettered=self._dead_lettered,
        )

    async def _dead_letter(self, message: RawMessage, reason: str) -> None:
        source = {
            "topic": message.topic,
            "partition": message.partition,
            "offset": message.offset,
        }
        body = {
            "source": source,
            "reason": reason,
            "payload_sha256": hashlib.sha256(message.value).hexdigest(),
        }
        await self._producer.send(
            "otlp.dlq",
            key=f"{message.topic}:{message.partition}:{message.offset}".encode(),
            value=json.dumps(body, separators=(",", ":"), sort_keys=True).encode(),
        )
