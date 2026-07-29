"""Bounded always-on polling for durable action-control work."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx

from action.actuators import (
    FlagActuator,
    KubernetesActuator,
    MeshActuator,
    SimulatedActuator,
)
from action.actuators.base import Actuator
from action.config import load_action_config, resolve_dry_run
from action.executor import ActionExecutor
from action.guards import BlastRadiusGuard
from action.orchestrator import ActionControlOrchestrator, ActionOrchestrationStore
from action.rollback import SloSettlementVerifier, VictoriaMetricsSloReader
from common.config import load_config
from contracts import ActionControlSnapshot, ActuatorKind

LOGGER = logging.getLogger(__name__)


class ActionPoller(Protocol):
    """One bounded unit of durable action work."""

    async def run_once(self, *, ts: datetime) -> ActionControlSnapshot | None: ...


class ActionControlRuntime:
    """Poll, drain a bounded batch, and yield even when the queue stays busy."""

    def __init__(
        self,
        *,
        poller: ActionPoller,
        poll_interval_seconds: float,
        batch_limit: int,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0.1 <= poll_interval_seconds <= 60.0:
            raise ValueError("action polling interval must be within [0.1, 60] seconds")
        if not 1 <= batch_limit <= 100:
            raise ValueError("action polling batch must be within [1, 100]")
        self._poller = poller
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_limit = batch_limit
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(self, stop: asyncio.Event) -> None:
        """Run until shutdown; one failed claim never kills the polling process."""
        while not stop.is_set():
            try:
                await self.drain_once()
            except Exception:
                LOGGER.exception("action-control polling iteration failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)

    async def drain_once(self) -> int:
        """Consume at most the configured batch and return the completed count."""
        completed = 0
        for _ in range(self._batch_limit):
            snapshot = await self._poller.run_once(ts=self._clock())
            if snapshot is None:
                break
            completed += 1
        return completed

    def close(self) -> None:
        """Release the synchronous telemetry client after polling has stopped."""
        if self._client is not None:
            self._client.close()


def build_action_runtime(
    *,
    store: ActionOrchestrationStore,
    config_dir: Path,
    victoriametrics_url: str,
    victoriametrics_timeout_seconds: float,
    worker_id: str,
    poll_interval_seconds: float,
    batch_limit: int,
    settlement_delay_seconds: int,
    slo_window_seconds: int,
    after_commit: Callable[[ActionControlSnapshot], None],
) -> ActionControlRuntime:
    """Wire every enabled real adapter and the telemetry verifier from config."""
    action_config = load_action_config(config_dir / "action.yml")
    runtime_config = load_config(config_dir)
    enabled = action_config.enabled_actuators()
    actuators: list[Actuator] = []
    if ActuatorKind.SIMULATED in enabled:
        actuators.append(SimulatedActuator())
    if ActuatorKind.KUBERNETES in enabled:
        assert action_config.kubernetes is not None
        actuators.append(KubernetesActuator(configuration=action_config.kubernetes))
    if ActuatorKind.MESH in enabled:
        assert action_config.mesh is not None
        actuators.append(MeshActuator(configuration=action_config.mesh))
    if ActuatorKind.FEATURE_FLAG in enabled:
        assert action_config.flags is not None
        actuators.append(FlagActuator(configuration=action_config.flags))
    executor = ActionExecutor(
        actuators=actuators,
        configuration=action_config,
        dry_run=resolve_dry_run(action_config, environ=os.environ),
    )
    client = httpx.Client(
        base_url=victoriametrics_url,
        timeout=victoriametrics_timeout_seconds,
    )
    settlement = SloSettlementVerifier(
        slos=runtime_config.slos,
        reader=VictoriaMetricsSloReader(
            client=client,
            window_seconds=slo_window_seconds,
        ),
    )
    identity = f"{worker_id}-{socket.gethostname()}"
    orchestrator = ActionControlOrchestrator(
        store=store,
        executor=executor,
        guard=BlastRadiusGuard(runtime_config.cohorts),
        worker_id=identity,
        claim_ttl=timedelta(seconds=action_config.execution.lease_ttl_seconds),
        settlement_delay=timedelta(seconds=settlement_delay_seconds),
        settlement=settlement,
        after_commit=after_commit,
    )
    return ActionControlRuntime(
        poller=orchestrator,
        poll_interval_seconds=poll_interval_seconds,
        batch_limit=batch_limit,
        client=client,
    )
