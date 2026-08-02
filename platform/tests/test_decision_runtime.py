"""Live detection drives real judgements into the one atomic publication seam."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lab.scoring.decisions import load_decision_configs

from action.actuators import SimulatedActuator
from action.planner import LiveActionPlanner
from api.incidents import IncidentFeedPublisher, IncidentPublishResult
from api.invalidation import snapshot_invalidation
from common.config import SentinelConfig, load_config
from common.storage import (
    IncidentDetailRecord,
    IncidentGraphRecord,
    IncidentRecord,
    IncidentSecurityRecord,
    LiveProducerCheckpoint,
)
from contracts import (
    ActionControlSnapshot,
    Decision,
    DecisionAction,
    DecompFrame,
    EpisodeStatus,
    IncidentSeverity,
    Observation,
    SnapshotInvalidation,
    SnapshotResource,
    SymptomEpisode,
    SymptomKind,
    VerdictClass,
)
from decision.runtime import LiveDecisionRuntime
from detection.live import LiveTick

START = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _Store:
    """Records bundles and applies the runtime store's stale-revision rule."""

    revisions: dict[str, datetime] = field(default_factory=dict)
    writes: list[IncidentRecord] = field(default_factory=list)
    controls: list[ActionControlSnapshot] = field(default_factory=list)
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
        if action_control is not None:
            self.controls.append(action_control)
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
class _RecordingPlanner:
    """Records what the runtime asked, and always earns a plan.

    Whether a judgement *earns* a plan is the planner's decision and is tested
    against the real ladder in `test_action_planner.py`. What is under test
    here is the runtime's own rule: ask once per incident, publish once, never
    re-materialize.
    """

    asked: list[str] = field(default_factory=list)
    confirmed: list[bool] = field(default_factory=list)

    def plan(self, decision: Decision, *, ts: datetime) -> ActionControlSnapshot | None:
        self.asked.append(decision.decision_id)
        self.confirmed.append(decision.confirmed)
        return _control_for(decision.incident_id, ts=ts)


def _control_for(incident_id: str, *, ts: datetime) -> ActionControlSnapshot:
    """One guarded control against the adapter that touches nothing."""
    adapter = SimulatedActuator()
    decision = Decision(
        decision_id=f"decision-{incident_id}",
        ts=ts,
        incident_id=incident_id,
        action=DecisionAction.ACT,
        rule_id="rule-under-test",
        reason="Verified evidence selected an absolute target.",
        evidence_ts=ts,
        severity=IncidentSeverity.HIGH,
        confirmed=True,
        verification_id=f"verification-{incident_id}",
        requires_human_approval=False,
        verdict_class=VerdictClass.OPERATIONAL_FAULT,
        verdict_id=f"verdict-{incident_id}",
        # Below every rung that needs a real cluster adapter, so the ladder
        # lands on the one rung that touches nothing. Less certainty means a
        # gentler action, which is the confidence-gated downgrade working.
        confidence=0.5,
        target_service="frontend",
    )
    planner = LiveActionPlanner(
        config=load_config(REPO_ROOT / "config"),
        config_root=REPO_ROOT / "config",
        actuators=[adapter],
    )
    control = planner.plan(decision, ts=ts)
    assert control is not None, "the committed ladder must offer a simulated rung"
    return control


@dataclass
class _Broker:
    events: list[SnapshotInvalidation] = field(default_factory=list)

    def publish(self, event: SnapshotInvalidation) -> int:
        self.events.append(event)
        return len(self.events)


@dataclass
class _Frames:
    writes: list[DecompFrame] = field(default_factory=list)

    async def write_decomp_frames(self, records: tuple[DecompFrame, ...]) -> None:
        self.writes.extend(records)


def test_each_persisted_decomposition_tick_invalidates_the_live_chart() -> None:
    store = _Store()
    broker = _Broker()
    frames = _Frames()
    runtime = _runtime(
        store,
        broker,
        frames=frames,
        frame_invalidation=lambda: broker.publish(
            snapshot_invalidation(SnapshotResource.DECOMPOSITION)
        ),
    )

    _drive(runtime, quiet_ticks=40, surge_ticks=0)

    assert frames.writes
    decomposition_events = [
        event for event in broker.events if event.resources == (SnapshotResource.DECOMPOSITION,)
    ]
    assert decomposition_events, "a stored frame must make the mounted chart refetch"


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


def test_a_producer_with_no_planner_creates_no_action_plan() -> None:
    """The posture every deployment starts in: incidents, and nothing to claim."""
    store = _Store()
    runtime = _runtime(store, _Broker())

    _drive(runtime, quiet_ticks=40, surge_ticks=40)

    assert runtime.published_action_controls == 0
    assert store.controls == []


def test_an_armed_producer_freezes_one_plan_per_incident_and_never_a_second() -> None:
    """A plan is immutable evidence about a moment, not a mutable intention.

    The store refuses a revision that names different state, so re-publishing a
    control the worker has already advanced would be an error rather than an
    update - which is exactly why this freezes once and then stops.
    """
    store = _Store()
    planner = _RecordingPlanner()
    runtime = _runtime(store, _Broker(), planner=planner)

    _drive(runtime, quiet_ticks=40, surge_ticks=40)

    assert planner.asked, "an armed producer must ask about its acting judgements"
    assert runtime.published_action_controls == len(store.controls)
    assert store.controls, "a verified acting judgement must freeze a plan"
    frozen = [control.incident_id for control in store.controls]
    assert len(frozen) == len(set(frozen)), "an incident may freeze only one plan"
    assert all(control.plan_revision == 1 for control in store.controls)
    # The target is the decision's, which the causal collapse computed from
    # evidence. Nothing in the planner may choose it.
    assert all(control.plan.target_service for control in store.controls)
    assert all(control.guard_results for control in store.controls)


def test_an_unverified_judgement_freezes_no_plan() -> None:
    """A hypothesis nothing confirmed against telemetry is not actionable."""
    store = _Store()
    planner = _RecordingPlanner()
    runtime = _runtime(store, _Broker(), planner=planner)

    _drive(runtime, quiet_ticks=40, surge_ticks=40)

    assert planner.confirmed, "the planner was never consulted at all"
    assert all(planner.confirmed), (
        "the runtime must not ask a planner about an unconfirmed decision"
    )


def test_the_checkpoint_advances_only_after_the_bundle_is_durable() -> None:
    store = _Store()
    runtime = _runtime(store, _Broker())

    _drive(runtime, quiet_ticks=6, surge_ticks=0)

    assert store.checkpoints
    latest = store.checkpoints[-1]
    assert latest.producer_id == "test-producer"
    assert latest.tick_ts == START + timedelta(seconds=12)
    assert latest.anchor_ts == START


def test_a_late_slow_window_revision_is_judged_at_the_current_live_tick() -> None:
    """Older evidence may arrive later without moving the decision clock backwards."""
    store = _Store()
    runtime = _runtime(store, _Broker())
    ticks = [
        _live_tick(
            START + timedelta(seconds=20),
            _episode("fast-window", event_ts=START + timedelta(seconds=19)),
        ),
        _live_tick(
            START + timedelta(seconds=30),
            # A slower detector closes at :30 but describes a condition whose
            # last breach predates the already-judged fast-window revision.
            _episode("slow-window", event_ts=START + timedelta(seconds=5)),
        ),
    ]
    runtime._processors = _ScriptedProcessors(ticks)  # type: ignore[assignment]

    asyncio.run(runtime.advance(observations=(), tick_ts=ticks[0].ts))
    asyncio.run(runtime.advance(observations=(), tick_ts=ticks[1].ts))

    assert store.checkpoints[-1].tick_ts == START + timedelta(seconds=30)
    assert store.writes[-1].updated_at == START + timedelta(seconds=30)


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


def _runtime(
    store: _Store,
    broker: _Broker,
    *,
    planner: object | None = None,
    frames: object | None = None,
    frame_invalidation: object | None = None,
) -> LiveDecisionRuntime:
    config = _config()
    return LiveDecisionRuntime(
        config=config,
        decisions=load_decision_configs(REPO_ROOT / "config"),
        publisher=IncidentFeedPublisher(store=store, broker=broker),
        checkpoints=store,
        producer_id="test-producer",
        anchor_ts=START,
        stimulus_honesty="SIMULATED",
        planner=planner,  # type: ignore[arg-type]
        frames=frames,  # type: ignore[arg-type]
        frame_invalidation=frame_invalidation,  # type: ignore[arg-type]
    )


class _ScriptedProcessors:
    """A detector script that exposes revisions in live observation order."""

    def __init__(self, ticks: list[LiveTick]) -> None:
        self._ticks = iter(ticks)
        self.anchor_ts = START
        self.base_tick_seconds = 2
        self.covered_kinds = frozenset({SymptomKind.SATURATION})
        self.covered_services = frozenset({"frontend"})

    def advance(self, **_: object) -> LiveTick:
        return next(self._ticks)


def _live_tick(ts: datetime, *episodes: SymptomEpisode) -> LiveTick:
    return LiveTick(
        ts=ts,
        episodes=episodes,
        frames=(),
        advanced_processors=frozenset({"resource"}),
        covered_kinds=frozenset({SymptomKind.SATURATION}),
        covered_services=frozenset({"frontend"}),
    )


def _episode(episode_id: str, *, event_ts: datetime) -> SymptomEpisode:
    return SymptomEpisode(
        episode_id=episode_id,
        kind=SymptomKind.SATURATION,
        service="frontend",
        signal="resource.cpu.utilization",
        status=EpisodeStatus.ACTIVE,
        opened_ts=event_ts,
        confirmed_ts=event_ts,
        last_breach_ts=event_ts,
        peak_score=0.9,
        breach_tick_count=3,
        revision=1,
        opening_symptom_id=f"{episode_id}-open",
        peak_symptom_id=f"{episode_id}-peak",
        latest_symptom_id=f"{episode_id}-latest",
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
