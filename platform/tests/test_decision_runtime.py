"""Live detection drives real judgements into the one atomic publication seam."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lab.scoring.decisions import load_decision_configs

from api.incidents import IncidentFeedPublisher, IncidentPublishResult
from common.config import SentinelConfig, load_config
from common.storage import (
    IncidentDetailRecord,
    IncidentGraphRecord,
    IncidentRecord,
    IncidentSecurityRecord,
    LiveProducerCheckpoint,
)
from contracts import ActionControlSnapshot, Observation, SnapshotInvalidation, SymptomKind
from decision.runtime import LiveDecisionRuntime

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _Store:
    """Records bundles and applies the runtime store's stale-revision rule."""

    revisions: dict[str, datetime] = field(default_factory=dict)
    writes: list[IncidentRecord] = field(default_factory=list)
    checkpoints: list[LiveProducerCheckpoint] = field(default_factory=list)

    async def put_incident_bundle(
        self,
        record: IncidentRecord,
        graph: IncidentGraphRecord,
        detail: IncidentDetailRecord,
        security: IncidentSecurityRecord,
        action_control: ActionControlSnapshot | None = None,
    ) -> bool:
        previous = self.revisions.get(record.incident_id)
        if previous is not None and previous >= record.updated_at:
            return False
        self.revisions[record.incident_id] = record.updated_at
        self.writes.append(record)
        return True

    async def put_live_producer_checkpoint(self, checkpoint: LiveProducerCheckpoint) -> bool:
        self.checkpoints.append(checkpoint)
        return True

    async def get_live_producer_checkpoint(self, producer_id: str) -> LiveProducerCheckpoint | None:
        for checkpoint in reversed(self.checkpoints):
            if checkpoint.producer_id == producer_id:
                return checkpoint
        return None


@dataclass
class _Broker:
    events: list[SnapshotInvalidation] = field(default_factory=list)

    def publish(self, event: SnapshotInvalidation) -> int:
        self.events.append(event)
        return len(self.events)


def test_a_live_surge_reaches_storage_as_an_evidence_backed_incident() -> None:
    store = _Store()
    broker = _Broker()
    runtime = _runtime(store, broker)

    results = _drive(runtime, quiet_ticks=40, surge_ticks=40)

    assert results, "a sustained live deformation must reach the incident store"
    assert store.writes
    published = results[-1]
    assert published.item.honesty == "REAL"
    assert published.item.services
    assert broker.events, "browsers are told only after the bundle is committed"


def test_the_producer_claims_only_the_coverage_its_processors_measured() -> None:
    runtime = _runtime(_Store(), _Broker())

    assert runtime.covered_kinds == frozenset(
        {
            SymptomKind.RESIDUAL_EXCEED,
            SymptomKind.RATIO_DEFORM,
            SymptomKind.LOG_BURST,
            SymptomKind.EDGE_DEGRADED,
            SymptomKind.SATURATION,
            SymptomKind.DROP,
            SymptomKind.SILENCE,
        }
    )
    assert runtime.covered_services == frozenset()


def test_replaying_the_same_ticks_publishes_no_second_revision() -> None:
    store = _Store()
    broker = _Broker()
    runtime = _runtime(store, broker)
    _drive(runtime, quiet_ticks=40, surge_ticks=40)
    committed = len(store.writes)
    invalidations = len(broker.events)

    replayed = _runtime(store, broker)
    _drive(replayed, quiet_ticks=40, surge_ticks=40)

    assert len(store.writes) == committed
    assert len(broker.events) == invalidations


def test_no_action_plan_is_created_from_a_live_judgement_yet() -> None:
    store = _Store()
    runtime = _runtime(store, _Broker())

    _drive(runtime, quiet_ticks=40, surge_ticks=40)

    assert runtime.published_action_controls == 0


def test_the_checkpoint_advances_only_after_the_bundle_is_durable() -> None:
    store = _Store()
    runtime = _runtime(store, _Broker())

    _drive(runtime, quiet_ticks=6, surge_ticks=0)

    assert store.checkpoints
    latest = store.checkpoints[-1]
    assert latest.producer_id == "test-producer"
    assert latest.tick_ts == START + timedelta(seconds=12)
    assert latest.anchor_ts == START


def test_a_tick_that_cannot_be_stored_advances_nothing() -> None:
    class _Failing(_Store):
        async def put_incident_bundle(self, *args: object, **kwargs: object) -> bool:
            raise RuntimeError("postgres is unavailable")

    store = _Failing()
    broker = _Broker()
    runtime = _runtime(store, broker)

    with pytest.raises(RuntimeError):
        _drive(runtime, quiet_ticks=40, surge_ticks=40)

    assert broker.events == []
    assert all(item.tick_ts < START + timedelta(seconds=160) for item in store.checkpoints)


def _runtime(store: _Store, broker: _Broker) -> LiveDecisionRuntime:
    config = _config()
    return LiveDecisionRuntime(
        config=config,
        decisions=load_decision_configs(REPO_ROOT / "config"),
        publisher=IncidentFeedPublisher(store=store, broker=broker),
        checkpoints=store,
        producer_id="test-producer",
        anchor_ts=START,
        stimulus_honesty="SIMULATED",
    )


def _config() -> SentinelConfig:
    return load_config(REPO_ROOT / "config")


def _drive(
    runtime: LiveDecisionRuntime,
    *,
    quiet_ticks: int,
    surge_ticks: int,
) -> list[IncidentPublishResult]:
    published: list[IncidentPublishResult] = []
    step = 0
    for _ in range(quiet_ticks):
        step += 1
        published.extend(_advance(runtime, step=step, requests=20, failures=0))
    for _ in range(surge_ticks):
        step += 1
        published.extend(_advance(runtime, step=step, requests=120, failures=8))
    return published


def _advance(
    runtime: LiveDecisionRuntime,
    *,
    step: int,
    requests: int,
    failures: int,
) -> tuple[IncidentPublishResult, ...]:
    tick = START + timedelta(seconds=2 * step)
    observations = tuple(
        _ingress(f"{step}-{index}", tick - timedelta(milliseconds=index + 1))
        for index in range(requests)
    ) + tuple(
        _dependency_call(
            f"{step}-dep-{index}",
            tick - timedelta(milliseconds=index + 1),
            failed=True,
        )
        for index in range(failures)
    )
    return asyncio.run(runtime.advance(observations=observations, tick_ts=tick))


def _ingress(observation_id: str, ts: datetime) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=ts,
        service="frontend-proxy",
        signal="span.duration_ms",
        value=11.0,
        unit="ms",
        attributes={"span.kind": 2, "http.route": "/", "http.status_code": 200},
        trace_refs=(f"trace-{observation_id}",),
    )


def _dependency_call(observation_id: str, ts: datetime, *, failed: bool) -> Observation:
    return Observation(
        observation_id=observation_id,
        ts=ts,
        service="frontend",
        signal="span.duration_ms",
        value=900.0 if failed else 8.0,
        unit="ms",
        attributes={
            "span.kind": 3,
            "rpc.service": "oteldemo.CheckoutService",
            "rpc.grpc.status_code": 14 if failed else 0,
        },
        trace_refs=(f"trace-{observation_id}",),
    )
