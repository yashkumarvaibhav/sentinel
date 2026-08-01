"""Publish one label-free capture decision through the runtime's atomic seam.

This is deliberately narrower than the final always-on producer. Phase 6.9a
needs one honest bridge from a recorded public capture to the already-built
runtime snapshot bundle; live bus consumption, widening a canary, rollback and
the demo endpoint remain later slices.

The replay is evaluated before this module sees it. We select the first
verified acting incident in event-time order, reconstruct only that incident's
public episode revisions, and terminally *simulate* its evidence-owned primary
rung. The resulting action control can be inspected but cannot be claimed by
the action worker, so this bridge never turns a replay into a production side
effect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from action import (
    BlastRadiusGuard,
    RemediationLadder,
    load_action_config,
    load_ladder_config,
    materialize_action_control,
)
from action.actuators import Actuator, SimulatedActuator
from api.incidents import IncidentFeedPublisher
from common.config import SentinelConfig, load_config
from common.settings import Settings
from common.storage import PostgresRepository, create_postgres_pool
from contracts import (
    ACTING_ACTIONS,
    ActionControlSnapshot,
    ActuatorKind,
    SymptomEpisode,
)
from decision import IncidentOutcome
from decision.config import load_incidents
from lab.scoring.decisions import load_decision_configs, replay_capture_decisions
from lab.scoring.remediate import FlagStandIn, KubernetesStandIn, MeshStandIn


class ReplayTick(Protocol):
    """The decision fields this publisher reads from one event-time tick."""

    @property
    def ts(self) -> datetime: ...

    @property
    def outcomes(self) -> Sequence[IncidentOutcome]: ...


class PublicDecisionReplay(Protocol):
    """The public, label-free part of a deterministic capture replay."""

    @property
    def capture_id(self) -> str: ...

    @property
    def scenario_id(self) -> str: ...

    @property
    def seed(self) -> int: ...

    @property
    def seed_purpose(self) -> str: ...

    @property
    def ticks(self) -> Sequence[ReplayTick]: ...

    @property
    def episode_revisions(self) -> Sequence[SymptomEpisode]: ...


class CapturePublicationError(RuntimeError):
    """A replay cannot support the bounded publication claim."""


@dataclass(frozen=True, slots=True)
class CapturePublication:
    """The durable checkpoint represented by one incident bundle revision."""

    capture_id: str
    incident_id: str
    action_control: ActionControlSnapshot
    persisted: bool


class CaptureReplayPublisher:
    """Materialize the first verified acting revision of one development replay."""

    def __init__(self, *, config: SentinelConfig, config_root: Path) -> None:
        action = load_action_config(config_root / "action.yml")
        self._config = config
        self._ladder = RemediationLadder(
            load_ladder_config(config_root / "ladders.yml"),
            flags=action.flags,
        )
        # Matching-kind stand-ins preserve the committed rung and safety fields
        # while making both the plan and its outcome explicitly SIMULATED.
        self._guard = BlastRadiusGuard(config.cohorts)
        self._dependency_signal_prefix = load_incidents(
            config_root / "incidents.yml"
        ).causal.dependency_signal_prefix

    async def publish(
        self,
        replay: PublicDecisionReplay,
        *,
        publisher: IncidentFeedPublisher,
    ) -> CapturePublication:
        """Persist one atomic replay bundle; an exact retry is a durable no-op."""
        if replay.seed_purpose != "development":
            raise CapturePublicationError(
                "runtime capture publication accepts only committed development replays"
            )
        if replay.scenario_id != "combo_night":
            raise CapturePublicationError(
                "Phase 6.9a publishes only the bounded combo_night north-star replay"
            )
        tick, outcome = _first_verified_acting_outcome(replay.ticks)
        episodes = _incident_episodes(
            replay.episode_revisions,
            outcome=outcome,
            ts=tick.ts,
        )
        control = self._simulated_control(outcome, ts=outcome.decision.ts)
        result = await publisher.persist(
            outcome,
            episodes=episodes,
            topology=self._config.topology,
            dependency_signal_prefix=self._dependency_signal_prefix,
            honesty="REAL",
            stimulus_honesty="SIMULATED",
            mode="REPLAY",
            capture_id=replay.capture_id,
            seed=replay.seed,
            action_control=control,
        )
        return CapturePublication(
            capture_id=replay.capture_id,
            incident_id=result.item.incident_id,
            action_control=control,
            persisted=result.persisted,
        )

    def _simulated_control(
        self,
        outcome: IncidentOutcome,
        *,
        ts: datetime,
    ) -> ActionControlSnapshot:
        selection = self._ladder.select(outcome.decision)
        choice = selection.primary
        # A stand-in records calls, so sharing one across replays would leak
        # process history into its outcome identity. A fresh instance makes the
        # materialized bytes a function of evidence + config only.
        adapter = _stand_in(choice.actuator)
        plan = adapter.plan(
            outcome.decision,
            action_kind=choice.action_kind,
            parameters=choice.parameters,
            ts=ts,
        )
        simulated = adapter.simulate(plan, ts=ts)
        return materialize_action_control(
            plan_revision=1,
            choice=choice,
            plan=plan,
            guard=self._guard,
            ts=ts,
            latest_outcome=simulated,
        )


def _first_verified_acting_outcome(
    ticks: Sequence[ReplayTick],
) -> tuple[ReplayTick, IncidentOutcome]:
    for tick in sorted(ticks, key=lambda item: item.ts):
        for outcome in sorted(tick.outcomes, key=lambda item: item.incident.incident_id):
            if outcome.decision.action in ACTING_ACTIONS and outcome.confirmed:
                return tick, outcome
    raise CapturePublicationError("capture produced no verified acting incident revision")


def _incident_episodes(
    revisions: Sequence[SymptomEpisode],
    *,
    outcome: IncidentOutcome,
    ts: datetime,
) -> tuple[SymptomEpisode, ...]:
    latest: dict[str, SymptomEpisode] = {}
    ordered = sorted(
        revisions,
        key=lambda episode: (
            _effective_ts(episode),
            episode.revision,
            episode.episode_id,
        ),
    )
    for episode in ordered:
        if _effective_ts(episode) > ts:
            break
        previous = latest.get(episode.episode_id)
        if previous is None or previous.revision < episode.revision:
            latest[episode.episode_id] = episode
    missing = tuple(
        episode_id for episode_id in outcome.incident.episode_ids if episode_id not in latest
    )
    if missing:
        raise CapturePublicationError(
            "capture publication is missing incident episode evidence: " + ", ".join(missing)
        )
    return tuple(latest[episode_id] for episode_id in outcome.incident.episode_ids)


def _effective_ts(episode: SymptomEpisode) -> datetime:
    return episode.closed_ts or episode.last_breach_ts


def _stand_in(kind: ActuatorKind) -> Actuator:
    if kind is ActuatorKind.MESH:
        return MeshStandIn(blast_fraction=0.20)
    if kind is ActuatorKind.KUBERNETES:
        return KubernetesStandIn(blast_fraction=0.50)
    if kind is ActuatorKind.FEATURE_FLAG:
        return FlagStandIn(blast_fraction=1.0)
    return SimulatedActuator(blast_fraction=0.05)


@dataclass(slots=True)
class _InvalidationCounter:
    """CLI proof that the post-commit publisher crossed its invalidation seam."""

    count: int = 0

    def publish(self, event: object) -> int:
        del event
        self.count += 1
        return 0


async def commit_capture_publication(
    *,
    replay: PublicDecisionReplay,
    producer: CaptureReplayPublisher,
    runtime: Settings,
) -> tuple[CapturePublication, int]:
    pool = create_postgres_pool(runtime)
    await pool.open(wait=True)
    try:
        broker = _InvalidationCounter()
        publisher = IncidentFeedPublisher(
            store=PostgresRepository(pool=pool, schema=runtime.postgres_schema),
            broker=broker,
        )
        result = await producer.publish(replay, publisher=publisher)
        return result, broker.count
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Replay one north-star capture and checkpoint its terminal simulation."""
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.publish")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument(
        "--commit-runtime",
        action="store_true",
        help="required acknowledgement that the runtime Postgres snapshot will change",
    )
    args = parser.parse_args(argv)
    if not args.commit_runtime:
        parser.error("--commit-runtime is required; capture publication changes runtime state")
    repo_root = args.repo_root.resolve()
    config_root = repo_root / "config"
    config = load_config(config_root)
    replay = replay_capture_decisions(
        args.capture.resolve(),
        config=config,
        decisions=load_decision_configs(config_root),
    )
    runtime = Settings().model_copy(update={"config_dir": config_root})
    result, invalidations = asyncio.run(
        commit_capture_publication(
            replay=replay,
            producer=CaptureReplayPublisher(
                config=config,
                config_root=config_root,
            ),
            runtime=runtime,
        )
    )
    print(
        json.dumps(
            {
                "action_state": result.action_control.state.value,
                "capture_id": result.capture_id,
                "incident_id": result.incident_id,
                "invalidations": invalidations,
                "persisted": result.persisted,
            },
            allow_nan=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
