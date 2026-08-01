"""Long-running live producer service: normalized bus in, judged incidents out."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from aiokafka import AIOKafkaConsumer
from aiokafka.structs import TopicPartition

from api.incidents import IncidentFeedPublisher
from api.notify import PostgresInvalidationBroker
from common.config import load_config
from common.settings import settings
from common.storage import PostgresRepository, create_postgres_pool
from context.service import open_context_service
from decision.config import (
    load_action_policy,
    load_evidence_agents,
    load_incidents,
    load_verdict_rules,
)
from decision.runtime import DecisionConfigBundle, LiveDecisionRuntime
from detection.live import base_tick_seconds, floor_to_tick
from detection.watermark import WatermarkBuffer
from producer.service import (
    NORMALIZED_TOPIC,
    BusRecord,
    LiveProducerService,
    run_forever,
)

_LOG = logging.getLogger("sentinel.producer")


class _DecisionConfigs:
    """The decision plane's committed configuration, loaded once at startup."""

    def __init__(self, root: Path) -> None:
        self.agents = load_evidence_agents(root / "decision-agents.yml")
        self.verdict_rules = load_verdict_rules(root / "verdict-rules.yml")
        self.incidents = load_incidents(root / "incidents.yml")
        self.policy = load_action_policy(root / "policies" / "action-policy.yml")


class _KafkaCommitter:
    def __init__(self, consumer: AIOKafkaConsumer) -> None:
        self._consumer = consumer

    async def commit(self, record: BusRecord) -> None:
        partition = TopicPartition(record.topic, record.partition)
        await self._consumer.commit({partition: record.offset + 1})  # type: ignore[no-untyped-call]


async def _records(consumer: AIOKafkaConsumer) -> object:
    async for record in consumer:
        yield BusRecord(
            topic=record.topic,
            partition=record.partition,
            offset=record.offset,
            value=record.value,
        )


async def run() -> None:
    """Consume the normalized bus until cancelled, judging complete windows."""
    config = settings()
    snapshot = load_config(config.config_dir)
    decisions: DecisionConfigBundle = _DecisionConfigs(config.config_dir)
    consumer = AIOKafkaConsumer(  # type: ignore[no-untyped-call]
        NORMALIZED_TOPIC,
        bootstrap_servers=config.brokers,
        client_id="sentinel-live-producer",
        group_id=config.live_producer_group_id,
        enable_auto_commit=False,
        auto_offset_reset="latest",
        max_partition_fetch_bytes=10 * 1024 * 1024,
    )
    tick_seconds = base_tick_seconds(snapshot.detectors)
    anchor = floor_to_tick(datetime.now(UTC), tick_seconds=tick_seconds)
    pool = create_postgres_pool(config)
    await pool.open(wait=True)
    try:
        store = PostgresRepository(pool=pool, schema=config.postgres_schema)
        broker = PostgresInvalidationBroker(pool=pool)
        async with open_context_service(calendar=snapshot.events, runtime=config) as contexts:
            runtime = LiveDecisionRuntime(
                config=snapshot,
                decisions=decisions,
                publisher=IncidentFeedPublisher(store=store, broker=broker),
                checkpoints=store,
                producer_id=config.live_producer_id,
                # Before any evidence arrives the anchor is the tick boundary
                # this process started inside; a producer that has run before
                # replaces it with its own durable one.
                anchor_ts=anchor,
                stimulus_honesty=config.live_producer_stimulus_honesty,
            )
            service = LiveProducerService(
                runtime=runtime,
                buffer=WatermarkBuffer(
                    tick_seconds=tick_seconds,
                    lateness_seconds=snapshot.detectors.watermark_lateness_seconds,
                    capacity=config.live_producer_buffer_capacity,
                ),
                committer=_KafkaCommitter(consumer),
                notify=broker.drain,
                contexts=contexts,
                context_refresh_seconds=config.live_producer_context_refresh_seconds,
            )
            await service.resume()
            await consumer.start()  # type: ignore[no-untyped-call]
            try:
                _LOG.info(
                    "live producer ready config=%s producer=%s honesty=%s",
                    snapshot.fingerprint,
                    config.live_producer_id,
                    config.live_producer_stimulus_honesty,
                )
                await run_forever(service, _records(consumer), asyncio.Event())
            finally:
                await consumer.stop()  # type: ignore[no-untyped-call]
                _LOG.info("live producer stopped stats=%s", service.stats())
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Start the live producer service."""
    if argv:
        raise SystemExit("the live producer takes no command-line arguments")
    logging.basicConfig(
        level=settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("aiokafka").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    return 0


raise SystemExit(main())
