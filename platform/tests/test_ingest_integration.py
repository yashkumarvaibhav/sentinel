"""Real testbed OTLP -> Redpanda -> normalizer -> ClickHouse/Redpanda proof."""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from common.settings import Settings
from common.storage import ClickHouseRepository
from ingest.kafka import RAW_TOPICS, KafkaCommitter, KafkaProducer, raw_message
from ingest.normalizer import NormalizationError, RawSignal, normalize_otlp_json
from ingest.worker import BoundedDeduplicator, IngestWorker, RawMessage

pytestmark = [pytest.mark.integration, pytest.mark.lab]


def test_real_testbed_signals_cross_both_normalized_sinks() -> None:
    if os.getenv("SENTINEL_LAB_INGEST_INTEGRATION") != "1":
        pytest.skip("set SENTINEL_LAB_INGEST_INTEGRATION=1 with the testbed running")
    asyncio.run(_round_trip())


async def _round_trip() -> None:
    config = Settings()
    suffix = uuid4().hex
    raw_consumer = AIOKafkaConsumer(  # type: ignore[no-untyped-call]
        *RAW_TOPICS,
        bootstrap_servers=config.brokers,
        group_id=f"sentinel-ingest-proof-raw-{suffix}",
        enable_auto_commit=False,
        auto_offset_reset="latest",
    )
    normalized_consumer = AIOKafkaConsumer(  # type: ignore[no-untyped-call]
        "obs.normalized",
        bootstrap_servers=config.brokers,
        group_id=f"sentinel-ingest-proof-normalized-{suffix}",
        enable_auto_commit=False,
        auto_offset_reset="latest",
    )
    producer = AIOKafkaProducer(  # type: ignore[no-untyped-call]
        bootstrap_servers=config.brokers,
        acks="all",
    )
    auth = (config.clickhouse_user, config.clickhouse_password.get_secret_value())
    async with httpx.AsyncClient(
        base_url=config.clickhouse_url,
        auth=auth,
        timeout=config.storage_timeout_seconds,
    ) as client:
        repository = ClickHouseRepository(client=client, database=config.clickhouse_database)
        worker = IngestWorker(
            sink=repository,
            producer=KafkaProducer(producer),
            committer=KafkaCommitter(raw_consumer),
            deduplicator=BoundedDeduplicator(capacity=10_000),
        )
        await raw_consumer.start()  # type: ignore[no-untyped-call]
        await normalized_consumer.start()  # type: ignore[no-untyped-call]
        await producer.start()  # type: ignore[no-untyped-call]
        try:
            messages = await _one_supported_message_per_signal(raw_consumer)
            representatives: set[str] = set()
            normalized_count = 0
            for signal in RawSignal:
                message = messages[signal]
                expected = normalize_otlp_json(message.signal, message.value)
                representatives.add(expected[0].observation_id)
                normalized_count += len(expected)
                await worker.handle(message)

            observed_ids = await _next_expected_ids(normalized_consumer, representatives)
            for observed_id in observed_ids:
                stored = await repository.get_observation(observed_id)
                assert stored is not None
                assert stored.observation_id == observed_id
            assert worker.stats().normalized == normalized_count
        finally:
            await producer.stop()  # type: ignore[no-untyped-call]
            await normalized_consumer.stop()  # type: ignore[no-untyped-call]
            await raw_consumer.stop()  # type: ignore[no-untyped-call]


async def _one_supported_message_per_signal(
    consumer: AIOKafkaConsumer,
) -> dict[RawSignal, RawMessage]:
    messages: dict[RawSignal, RawMessage] = {}
    async with asyncio.timeout(60):
        while len(messages) < len(RawSignal):
            record = await consumer.getone()
            try:
                message = raw_message(record)
                normalize_otlp_json(message.signal, message.value)
            except (NormalizationError, ValueError):
                continue
            messages.setdefault(message.signal, message)
    return messages


async def _next_expected_ids(consumer: AIOKafkaConsumer, expected: set[str]) -> set[str]:
    observed: set[str] = set()

    async def _read() -> None:
        while observed != expected:
            record = await consumer.getone()
            if record.value is None:
                continue
            body = json.loads(record.value)
            observation_id = body.get("observation_id")
            if observation_id in expected:
                observed.add(str(observation_id))

    await asyncio.wait_for(_read(), timeout=30)
    return observed
