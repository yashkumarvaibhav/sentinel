"""The always-on producer: live telemetry in, durable judged incidents out.

Everything either side of this module already existed. The detection plane can
advance every path on a live event-time tick; the decision plane can judge a
stream of durable episodes; the gateway can persist one incident, its causal
graph, its full proof and its security projection in a single transaction and
tell browsers only after the commit. What was missing was the thing that runs
them in order against telemetry nobody recorded first.

Three properties are the point of this module:

* **Nothing here knows what the answer is.** No capture, manifest, seed or
  scenario label is reachable from this path; the only inputs are normalized
  observations and committed configuration.
* **Every incident revision goes through the same seam.** A live judgement is
  written by exactly the publisher the capture bridge and the action worker
  already use, so there is one definition of a stored incident.
* **The checkpoint follows the data.** It advances after a tick's bundles are
  durable, never before, so a crash re-consumes a tick rather than skipping it,
  and the store's own stale-revision rule makes that re-consumption a no-op.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import groupby
from typing import Literal, Protocol

from api.incidents import IncidentFeedPublisher, IncidentPublishResult
from common.config import SentinelConfig
from common.storage.models import LiveProducerCheckpoint
from contracts import (
    ActionControlSnapshot,
    ContextWindow,
    Decision,
    Observation,
    SymptomEpisode,
    SymptomKind,
    VerdictClass,
)
from decision.changes import ChangeFeed
from decision.config import (
    ActionPolicyConfig,
    EvidenceAgentsConfig,
    IncidentsConfig,
    VerdictRulesConfig,
)
from decision.pipeline import DecisionPipeline, IncidentOutcome
from detection.decompose import DecompositionEnvelope
from detection.live import LiveDetectionProcessors

LOGGER = logging.getLogger(__name__)


class DecisionConfigBundle(Protocol):
    """The decision plane's own committed configuration, already validated."""

    @property
    def agents(self) -> EvidenceAgentsConfig: ...

    @property
    def verdict_rules(self) -> VerdictRulesConfig: ...

    @property
    def incidents(self) -> IncidentsConfig: ...

    @property
    def policy(self) -> ActionPolicyConfig: ...


class LiveProducerCheckpointStore(Protocol):
    """The durable position of the always-on producer."""

    async def put_live_producer_checkpoint(self, checkpoint: LiveProducerCheckpoint) -> bool: ...

    async def get_live_producer_checkpoint(
        self, producer_id: str
    ) -> LiveProducerCheckpoint | None: ...


class ActionPlanner(Protocol):
    """Freezes a verified acting decision into one immutable, guarded plan."""

    def plan(self, decision: Decision, *, ts: datetime) -> ActionControlSnapshot | None: ...


class LiveEvidenceError(RuntimeError):
    """A judgement cannot be published because its evidence is not in hand."""


class LiveDecisionRuntime:
    """Advance detection, judge the tick, and persist every incident revision."""

    def __init__(
        self,
        *,
        config: SentinelConfig,
        decisions: DecisionConfigBundle,
        publisher: IncidentFeedPublisher,
        checkpoints: LiveProducerCheckpointStore,
        producer_id: str,
        anchor_ts: datetime,
        stimulus_honesty: Literal["REAL", "SIMULATED"],
        published_baseline: int = 0,
        changes: ChangeFeed | None = None,
        envelope: DecompositionEnvelope | None = None,
        planner: ActionPlanner | None = None,
    ) -> None:
        self._config = config
        self._publisher = publisher
        self._checkpoints = checkpoints
        self._producer_id = producer_id
        # An honesty label is an operator statement about the world, not
        # something this loop can infer: a contained testbed under injected
        # chaos is SIMULATED stimulus over REAL telemetry, and only whoever
        # started it knows which of the two a given deployment is.
        self._stimulus_honesty = stimulus_honesty
        self._processors = LiveDetectionProcessors(
            detector=config.detectors,
            anchor_ts=anchor_ts,
            envelope=envelope,
        )
        self._pipeline = DecisionPipeline(
            agents=decisions.agents,
            verdict_rules=decisions.verdict_rules,
            incidents=decisions.incidents,
            policy=decisions.policy,
            topology=config.topology,
            changes=changes,
        )
        self._dependency_signal_prefix = decisions.incidents.causal.dependency_signal_prefix
        # Optional on purpose. A deployment that has not armed a planner keeps
        # publishing incidents and no plans, which is the posture every
        # environment starts in.
        self._planner = planner
        self._planned: set[str] = set()
        self._latest: dict[str, SymptomEpisode] = {}
        self._published_revisions: dict[str, datetime] = {}
        # The class this producer actually named about each incident. An
        # incident whose symptoms all close loses the evidence its verdict was
        # computed from, and the card would otherwise stop saying what the
        # platform concluded at exactly the moment a person goes looking.
        self._named_classes: dict[str, VerdictClass] = {}
        # Publications by this producer in total, not by this process: a
        # restart that dropped the running count would make the durable row
        # read as though nothing had ever been published.
        self._published_baseline = published_baseline
        self._published = 0
        self._published_action_controls = 0

    @property
    def anchor_ts(self) -> datetime:
        return self._processors.anchor_ts

    @property
    def base_tick_seconds(self) -> int:
        return self._processors.base_tick_seconds

    @property
    def covered_kinds(self) -> frozenset[SymptomKind]:
        return self._processors.covered_kinds

    @property
    def covered_services(self) -> frozenset[str]:
        return self._processors.covered_services

    @property
    def published_action_controls(self) -> int:
        """How many live judgements have frozen a plan an operator can act on.

        Zero when no planner is attached, which is the posture a deployment
        keeps until it deliberately arms one.
        """
        return self._published_action_controls

    async def advance(
        self,
        *,
        observations: tuple[Observation, ...],
        tick_ts: datetime,
        contexts: tuple[ContextWindow, ...] = (),
    ) -> tuple[IncidentPublishResult, ...]:
        """Judge one complete live tick and durably publish what it concluded."""
        measured = self._processors.advance(
            observations=observations,
            tick_ts=tick_ts,
            contexts=contexts,
        )
        published: list[IncidentPublishResult] = []
        for moment, revisions in _by_event_time(measured.episodes):
            for outcome in self._judge(moment, revisions=revisions):
                result = await self._persist(outcome)
                if result is not None:
                    published.append(result)
        await self._checkpoints.put_live_producer_checkpoint(
            LiveProducerCheckpoint(
                producer_id=self._producer_id,
                anchor_ts=self.anchor_ts,
                tick_ts=measured.ts,
                published_incidents=self._published_baseline + self._published,
            )
        )
        return tuple(published)

    def _judge(
        self,
        moment: datetime,
        *,
        revisions: tuple[SymptomEpisode, ...],
    ) -> tuple[IncidentOutcome, ...]:
        for episode in revisions:
            previous = self._latest.get(episode.episode_id)
            if previous is not None and previous.revision >= episode.revision:
                continue
            self._latest[episode.episode_id] = episode
        known = tuple(
            sorted(
                self._latest.values(),
                key=lambda episode: (episode.opened_ts, episode.episode_id),
            )
        )
        tick = self._pipeline.observe(
            ts=moment,
            episodes=known,
            covered_kinds=self._processors.covered_kinds,
            covered_services=self._processors.covered_services,
        )
        return tick.outcomes

    async def _persist(self, outcome: IncidentOutcome) -> IncidentPublishResult | None:
        incident_id = outcome.incident.incident_id
        revision = outcome.decision.ts
        previous = self._published_revisions.get(incident_id)
        if previous is not None and previous >= revision:
            return None
        missing = tuple(
            episode_id
            for episode_id in outcome.incident.episode_ids
            if episode_id not in self._latest
        )
        if missing:
            raise LiveEvidenceError(
                "a live incident names episodes this producer never measured: " + ", ".join(missing)
            )
        named = outcome.verdict.verdict_class if outcome.verdict is not None else None
        if named is not None:
            self._named_classes[incident_id] = named
        control = self._freeze_plan(outcome)
        result = await self._publisher.persist(
            outcome,
            episodes=tuple(self._latest[episode_id] for episode_id in outcome.incident.episode_ids),
            topology=self._config.topology,
            dependency_signal_prefix=self._dependency_signal_prefix,
            honesty="REAL",
            stimulus_honesty=self._stimulus_honesty,
            mode="LIVE",
            concluded_verdict_class=self._named_classes.get(incident_id),
            action_control=control,
        )
        if result.persisted:
            self._published_revisions[incident_id] = revision
            self._published += 1
            if control is not None:
                self._planned.add(incident_id)
                self._published_action_controls += 1
        return result

    def _freeze_plan(self, outcome: IncidentOutcome) -> ActionControlSnapshot | None:
        """Freeze this incident's plan once, on the first verified acting revision.

        Once, because the store refuses a revision that names different state:
        re-publishing a control the worker has already advanced would be an
        error rather than an update. That is the right shape - a plan is
        immutable evidence about what was decided at a moment, not a mutable
        intention that follows the incident around.

        Verified, because a hypothesis nothing has confirmed against telemetry
        is not something this platform is entitled to act on.
        """
        if self._planner is None:
            return None
        if outcome.incident.incident_id in self._planned:
            return None
        if not outcome.confirmed:
            return None
        return self._planner.plan(outcome.decision, ts=outcome.decision.ts)

    async def resume(self) -> LiveProducerCheckpoint | None:
        """The durable position this producer reached, if it has one."""
        return await self._checkpoints.get_live_producer_checkpoint(self._producer_id)


def _by_event_time(
    revisions: Sequence[SymptomEpisode],
) -> tuple[tuple[datetime, tuple[SymptomEpisode, ...]], ...]:
    """Group one tick's revisions into the moments they each became true."""
    ordered = sorted(
        revisions,
        key=lambda episode: (_effective(episode), episode.revision, episode.episode_id),
    )
    return tuple((moment, tuple(group)) for moment, group in groupby(ordered, key=_effective))


def _effective(episode: SymptomEpisode) -> datetime:
    return _utc(episode.closed_ts or episode.last_breach_ts)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("ts must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError("ts must be timezone-aware UTC")
    return value.astimezone(UTC)
