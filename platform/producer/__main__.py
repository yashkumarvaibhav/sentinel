"""Long-running live producer service: normalized bus in, judged incidents out."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from aiokafka import AIOKafkaConsumer
from aiokafka.structs import TopicPartition

from action.actuators import (
    FlagActuator,
    KubernetesActuator,
    MeshActuator,
    SimulatedActuator,
)
from action.actuators.base import Actuator
from action.config import load_action_config
from action.planner import LiveActionPlanner
from api.incidents import IncidentFeedPublisher
from api.notify import PostgresInvalidationBroker
from common.config import SentinelConfig, load_config
from common.settings import settings
from common.storage import PostgresRepository, create_postgres_pool
from context.service import open_context_service
from contracts import ActuatorKind
from decision.config import (
    load_action_policy,
    load_evidence_agents,
    load_incidents,
    load_verdict_rules,
)
from decision.runtime import DecisionConfigBundle, LiveDecisionRuntime
from detection.live import base_tick_seconds
from detection.watermark import WatermarkBuffer
from producer.service import (
    NORMALIZED_TOPIC,
    BusRecord,
    LiveProducerService,
    plan_resume,
    run_forever,
    silence_horizon_seconds,
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


def _planner(snapshot: SentinelConfig, *, config_dir: Path) -> LiveActionPlanner:
    """Every adapter this deployment has enabled, so a rung is never substituted.

    The planner refuses a rung whose adapter it was not given rather than
    reaching for a different one, so an incomplete set here shows up as an
    unplanned incident instead of an action nobody chose.
    """
    action_config = load_action_config(config_dir / "action.yml")
    enabled = action_config.enabled_actuators()
    actuators: list[Actuator] = []
    if ActuatorKind.SIMULATED in enabled:
        actuators.append(SimulatedActuator())
    if ActuatorKind.KUBERNETES in enabled and action_config.kubernetes is not None:
        actuators.append(KubernetesActuator(configuration=action_config.kubernetes))
    if ActuatorKind.MESH in enabled and action_config.mesh is not None:
        actuators.append(MeshActuator(configuration=action_config.mesh))
    if ActuatorKind.FEATURE_FLAG in enabled and action_config.flags is not None:
        actuators.append(FlagActuator(configuration=action_config.flags))
    return LiveActionPlanner(config=snapshot, config_root=config_dir, actuators=actuators)


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
    pool = create_postgres_pool(config)
    await pool.open(wait=True)
    try:
        store = PostgresRepository(pool=pool, schema=config.postgres_schema)
        broker = PostgresInvalidationBroker(pool=pool)
        # The anchor is read before anything is built: the detector state is
        # anchored at construction, so a producer that has run before has to
        # start its processors on the same origin its durable position names.
        plan = plan_resume(
            await store.get_live_producer_checkpoint(config.live_producer_id),
            now=datetime.now(UTC),
            tick_seconds=tick_seconds,
            max_gap_seconds=silence_horizon_seconds(snapshot.detectors),
        )
        if plan.discontinuity_seconds is not None:
            _LOG.warning(
                "live producer was not running for %.0fs; starting a new stream at %s "
                "rather than replaying a gap it has no evidence for",
                plan.discontinuity_seconds,
                plan.anchor_ts.isoformat(),
            )
        planner = (
            _planner(snapshot, config_dir=config.config_dir)
            if config.live_producer_plans_actions
            else None
        )
        if planner is not None:
            _LOG.info(
                "live judgements will freeze an action plan; whether that plan reaches the "
                "world is action.yml's dry_run, which this switch does not touch"
            )
        async with open_context_service(calendar=snapshot.events, runtime=config) as contexts:
            runtime = LiveDecisionRuntime(
                config=snapshot,
                decisions=decisions,
                publisher=IncidentFeedPublisher(store=store, broker=broker),
                checkpoints=store,
                producer_id=config.live_producer_id,
                anchor_ts=plan.anchor_ts,
                stimulus_honesty=config.live_producer_stimulus_honesty,
                published_baseline=plan.published_baseline,
                planner=planner,
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
                    "live producer ready config=%s producer=%s honesty=%s anchor=%s resumed=%s",
                    snapshot.fingerprint,
                    config.live_producer_id,
                    config.live_producer_stimulus_honesty,
                    plan.anchor_ts.isoformat(),
                    plan.checkpoint is not None,
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
