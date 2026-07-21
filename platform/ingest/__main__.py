"""Long-running raw OTLP normalization service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from common.config import load_config
from common.settings import settings
from common.storage import ClickHouseRepository
from ingest.kafka import RAW_TOPICS, KafkaCommitter, KafkaProducer, raw_message
from ingest.worker import BoundedDeduplicator, IngestWorker

_LOG = logging.getLogger("sentinel.ingest")


async def run() -> None:
    """Consume raw topics until cancelled, preserving output-before-commit ordering."""
    config = settings()
    snapshot = load_config(config.config_dir)
    consumer = AIOKafkaConsumer(  # type: ignore[no-untyped-call]
        *RAW_TOPICS,
        bootstrap_servers=config.brokers,
        client_id="sentinel-ingest",
        group_id=config.ingest_group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_partition_fetch_bytes=10 * 1024 * 1024,
    )
    producer = AIOKafkaProducer(  # type: ignore[no-untyped-call]
        bootstrap_servers=config.brokers,
        client_id="sentinel-ingest",
        acks="all",
        compression_type="gzip",
    )
    auth = (config.clickhouse_user, config.clickhouse_password.get_secret_value())
    async with httpx.AsyncClient(
        base_url=config.clickhouse_url,
        auth=auth,
        timeout=config.storage_timeout_seconds,
    ) as client:
        worker = IngestWorker(
            sink=ClickHouseRepository(client=client, database=config.clickhouse_database),
            producer=KafkaProducer(producer),
            committer=KafkaCommitter(consumer),
            deduplicator=BoundedDeduplicator(capacity=config.ingest_dedup_capacity),
        )
        await producer.start()  # type: ignore[no-untyped-call]
        try:
            await consumer.start()  # type: ignore[no-untyped-call]
            _LOG.info("ingest ready config=%s", snapshot.fingerprint)
            try:
                async for record in consumer:
                    message = raw_message(record)
                    delay = 1.0
                    while True:
                        try:
                            await worker.handle(message)
                            break
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            _LOG.exception(
                                "ingest output failed topic=%s partition=%d offset=%d; retrying",
                                message.topic,
                                message.partition,
                                message.offset,
                            )
                            await asyncio.sleep(delay)
                            delay = min(delay * 2, 30.0)
            finally:
                await consumer.stop()  # type: ignore[no-untyped-call]
        finally:
            await producer.stop()  # type: ignore[no-untyped-call]


def main(argv: Sequence[str] | None = None) -> int:
    """Start the ingest service."""
    if argv:
        raise SystemExit("ingest takes no command-line arguments")
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
