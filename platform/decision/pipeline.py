"""The decision loop: durable episodes in, judged incidents out.

Phase 4 built six independent pieces - four evidence agents, a fusion rule
table, an incident tracker, a causal collapse, an incident memory and a
verifier. Nothing ran them. This module is the loop that does, and it is the
only place their order is stated:

1. the four agents assess the tick, each from its own claimed evidence only;
2. the incident tracker clusters the same episodes into the problems a person
   is actually dealing with, taking its impact estimate from the business
   agent's own numbers;
3. fusion reads the four axis scores and names the diagnosis, or refuses to;
4. every incident is checked by the non-LLM verifier before anything may act on
   it, and recognition of a remembered shape nudges - never carries - its
   confidence;
5. the policy gate says what happens about each incident, restricted by guards
   computed from that same evidence.

Two seams are deliberate and worth stating plainly.

**The change feed's coverage is owned here, not by the caller.** An agent is
insufficient when its evidence was never consulted, and whether the change feed
was consulted is a fact about this loop. A caller that claimed
``DEPLOY_MARKER`` coverage while no feed was wired would be asserting evidence
that does not exist, so that is rejected rather than believed.

**A tick has one verdict and may have several incidents.** The agents measure
the whole tick, so the diagnosis is tick-wide; the verification, the origin and
(from the policy gate onward) the decision are per incident. A combination is
therefore one COMBINATION verdict over two separately verified incidents, which
is what an operator sees too.

Nothing here proposes anything, nothing calls a model, and nothing reads a
clock: the same episode stream always produces the same judgements.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import groupby

from common.config import TopologyConfig
from contracts import (
    AgentAssessment,
    ChangeEvent,
    Decision,
    EvidenceAxis,
    FusionStatus,
    Incident,
    IncidentState,
    SymptomEpisode,
    SymptomKind,
    Verdict,
    Verification,
)
from decision.agents import (
    CHANGE_COVERAGE_KIND,
    AgentEvidenceWindow,
    BusinessImpactEvidenceAgent,
    ChangeConfigEvidenceAgent,
    EvidenceAgent,
    ReliabilityEvidenceAgent,
    SecurityEvidenceAgent,
)
from decision.changes import ChangeFeed
from decision.config import (
    ActionPolicyConfig,
    EvidenceAgentsConfig,
    IncidentsConfig,
    VerdictRulesConfig,
)
from decision.decide import PolicyGate
from decision.incidents import IncidentTracker
from decision.memory import (
    IncidentSignature,
    SimilarIncident,
    build_signature,
    nearest,
    recognized,
)
from decision.verdict import EvidenceFusion, FusionResult
from decision.verifier import verify_incident


@dataclass(frozen=True, slots=True)
class EpisodeSnapshot:
    """Every durable episode as it stood at one event time."""

    ts: datetime
    episodes: tuple[SymptomEpisode, ...]


@dataclass(frozen=True, slots=True)
class IncidentOutcome:
    """Everything the decision plane concluded about one incident at one tick.

    ``verdict`` is the tick's diagnosis after this incident's own memory
    recognition has been folded in, so two incidents in the same tick can carry
    the same class with different confidence. It is ``None`` exactly when
    fusion refused to name a class at all.

    ``decision`` always exists: a gate that declined to answer would leave an
    incident with no stated handling, so even "nothing is happening" is a
    recorded decision with a reason.
    """

    incident: Incident
    verdict: Verdict | None
    verification: Verification
    decision: Decision
    matches: tuple[SimilarIncident, ...]
    signature: IncidentSignature | None

    @property
    def confirmed(self) -> bool:
        """Whether every required check held, the only gate action may pass."""
        return self.verification.confirmed


@dataclass(frozen=True, slots=True)
class DecisionTick:
    """One pass of the loop: what was measured, what it means, what was checked."""

    ts: datetime
    assessments: tuple[AgentAssessment, ...]
    fusion: FusionResult
    outcomes: tuple[IncidentOutcome, ...]
    changes: tuple[ChangeEvent, ...] = ()

    @property
    def verdict(self) -> Verdict | None:
        """The tick-wide diagnosis, or None when fusion refused to name one."""
        return self.fusion.verdict

    def assessment(self, axis: EvidenceAxis) -> AgentAssessment:
        """This tick's assessment of one axis."""
        for assessment in self.assessments:
            if assessment.axis is axis:
                return assessment
        raise KeyError(f"axis was not assessed this tick: {axis.value}")


class DecisionPipeline:
    """Run the whole decision plane over a durable episode stream, one tick at a time.

    The pipeline is stateful across ticks because the plane is: agents compare
    an axis with its own previous score, incident identity survives a storm
    growing around it, and incident memory only grows. It holds no clock and no
    I/O, so a replay of the same stream reproduces every judgement exactly.
    """

    def __init__(
        self,
        *,
        agents: EvidenceAgentsConfig,
        verdict_rules: VerdictRulesConfig,
        incidents: IncidentsConfig,
        policy: ActionPolicyConfig,
        topology: TopologyConfig,
        changes: ChangeFeed | None = None,
        memory: Sequence[IncidentSignature] = (),
    ) -> None:
        criticality = {service.service: service.criticality for service in topology.services}
        self._security = SecurityEvidenceAgent(configuration=agents.axis(EvidenceAxis.SECURITY))
        self._reliability = ReliabilityEvidenceAgent(
            configuration=agents.axis(EvidenceAxis.RELIABILITY)
        )
        self._change = ChangeConfigEvidenceAgent(
            configuration=agents.axis(EvidenceAxis.CHANGE_CONFIG)
        )
        self._business = BusinessImpactEvidenceAgent(
            configuration=agents.axis(EvidenceAxis.BUSINESS_IMPACT),
            criticality=criticality,
        )
        change_pressure = agents.axis(EvidenceAxis.CHANGE_CONFIG).change_pressure
        if change_pressure is None:  # defensive: config validation already requires it
            raise ValueError("CHANGE_CONFIG configuration must carry change_pressure settings")
        self._lookback_seconds = change_pressure.correlation_window_seconds
        self._fusion = EvidenceFusion(configuration=verdict_rules)
        self._tracker = IncidentTracker(configuration=incidents, topology=topology)
        self._gate = PolicyGate(configuration=policy, topology=topology)
        self._incidents = incidents
        self._topology = topology
        self._changes = changes
        self._memory: list[IncidentSignature] = list(memory)
        self._peaks: dict[str, tuple[float, IncidentSignature]] = {}
        self._remembered: set[str] = set()
        self._last_ts: datetime | None = None

    @property
    def memory(self) -> tuple[IncidentSignature, ...]:
        """Every remembered incident shape, in the order it was learned."""
        return tuple(self._memory)

    @property
    def gate(self) -> PolicyGate:
        """The policy gate every decision in this run was taken through."""
        return self._gate

    @property
    def agents(self) -> tuple[EvidenceAgent, ...]:
        """The four independent agents, in axis order."""
        return (self._business, self._change, self._reliability, self._security)

    def observe(
        self,
        *,
        ts: datetime,
        episodes: Sequence[SymptomEpisode],
        covered_kinds: frozenset[SymptomKind],
        covered_services: frozenset[str],
    ) -> DecisionTick:
        """Judge one tick of the durable episode stream, end to end."""
        moment = _utc(ts)
        if self._last_ts is not None and moment < self._last_ts:
            raise ValueError("decision ticks must not move backwards in event time")
        if CHANGE_COVERAGE_KIND in covered_kinds:
            raise ValueError(
                f"{CHANGE_COVERAGE_KIND.value} coverage is owned by the pipeline: it is claimed "
                "exactly when a change feed was consulted, never asserted by the caller"
            )
        window, changes = self._window(moment, episodes, covered_kinds)
        assessments = tuple(agent.assess(window) for agent in self.agents)
        business = next(
            assessment
            for assessment in assessments
            if assessment.axis is EvidenceAxis.BUSINESS_IMPACT
        )
        incidents = self._tracker.observe(ts=moment, episodes=window.episodes, business=business)
        fusion = self._fusion.fuse(assessments)
        outcomes = tuple(
            self._judge(
                incident,
                ts=moment,
                episodes=window.episodes,
                assessments=assessments,
                fusion=fusion,
                covered_services=covered_services,
            )
            for incident in incidents
        )
        self._remember(outcomes)
        self._last_ts = moment
        return DecisionTick(
            ts=moment,
            assessments=assessments,
            fusion=fusion,
            outcomes=outcomes,
            changes=changes,
        )

    def _window(
        self,
        ts: datetime,
        episodes: Sequence[SymptomEpisode],
        covered_kinds: frozenset[SymptomKind],
    ) -> tuple[AgentEvidenceWindow, tuple[ChangeEvent, ...]]:
        """Assemble the tick's evidence, claiming change coverage only if consulted."""
        if self._changes is None:
            return (
                AgentEvidenceWindow(
                    ts=ts,
                    episodes=tuple(episodes),
                    covered_kinds=covered_kinds,
                ),
                (),
            )
        changes = self._changes.within(ts, lookback_seconds=self._lookback_seconds)
        return (
            AgentEvidenceWindow(
                ts=ts,
                episodes=tuple(episodes),
                covered_kinds=covered_kinds | {CHANGE_COVERAGE_KIND},
                changes=changes,
            ),
            changes,
        )

    def _judge(
        self,
        incident: Incident,
        *,
        ts: datetime,
        episodes: Sequence[SymptomEpisode],
        assessments: Sequence[AgentAssessment],
        fusion: FusionResult,
        covered_services: frozenset[str],
    ) -> IncidentOutcome:
        """Check one incident, let memory nudge it, and decide what happens about it."""
        verdict = fusion.verdict if fusion.status is FusionStatus.DECIDED else None
        signature = build_signature(incident, assessments=assessments, verdict=verdict)
        matches = (
            ()
            if signature is None
            else nearest(signature, self._memory, configuration=self._incidents.memory)
        )
        verification = verify_incident(
            incident,
            episodes=episodes,
            covered_services=covered_services,
            matches=matches,
            memory_size=len(self._memory),
            topology=self._topology,
            configuration=self._incidents.verification,
            memory=self._incidents.memory,
            causal=self._incidents.causal,
        )
        recognised = (
            verdict
            if verdict is None or not matches
            else recognized(verdict, matches, configuration=self._incidents.memory)
        )
        decision = self._gate.decide(
            incident,
            ts=ts,
            verdict=recognised,
            verification=verification,
            fusion_status=fusion.status,
        )
        return IncidentOutcome(
            incident=incident,
            verdict=recognised,
            verification=verification,
            decision=decision,
            matches=matches,
            signature=signature,
        )

    def _remember(self, outcomes: Sequence[IncidentOutcome]) -> None:
        """Learn an incident's shape at its peak, and only once it is over.

        The signature memory.py describes is the shape at the incident's peak,
        not the calm it decays into, so the strongest signature seen while the
        incident ran is the one kept. It is committed when the incident
        resolves: a problem still unfolding is not yet a precedent.
        """
        for outcome in outcomes:
            incident_id = outcome.incident.incident_id
            if incident_id in self._remembered:
                continue
            signature = outcome.signature
            if signature is not None:
                strength = sum(signature.vector)
                best = self._peaks.get(incident_id)
                if best is None or strength > best[0]:
                    self._peaks[incident_id] = (strength, signature)
            if outcome.incident.state is not IncidentState.RESOLVED:
                continue
            peak = self._peaks.pop(incident_id, None)
            self._remembered.add(incident_id)
            if peak is not None:
                self._memory.append(peak[1])


def episode_timeline(revisions: Sequence[SymptomEpisode]) -> tuple[EpisodeSnapshot, ...]:
    """Turn a stream of durable episode revisions into the ticks a decision loop sees.

    A revision becomes true at the moment it describes - the tick that breached,
    or the moment the episode closed - so the loop is driven by the detection
    plane's own event time rather than by a wall clock. Each snapshot carries
    every episode known so far at its latest revision, including closed ones,
    because an incident ages out on the evidence that it stopped rather than on
    the evidence disappearing.
    """
    ordered = sorted(
        revisions,
        key=lambda episode: (_effective(episode), episode.revision, episode.episode_id),
    )
    snapshots: list[EpisodeSnapshot] = []
    latest: dict[str, SymptomEpisode] = {}
    for ts, group in groupby(ordered, key=_effective):
        for episode in group:
            previous = latest.get(episode.episode_id)
            if previous is not None and previous.revision >= episode.revision:
                continue
            latest[episode.episode_id] = episode
        snapshots.append(
            EpisodeSnapshot(
                ts=ts,
                episodes=tuple(
                    sorted(
                        latest.values(),
                        key=lambda episode: (episode.opened_ts, episode.episode_id),
                    )
                ),
            )
        )
    return tuple(snapshots)


def _effective(episode: SymptomEpisode) -> datetime:
    """The event time at which one episode revision became true."""
    return _utc(episode.closed_ts or episode.last_breach_ts)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("ts must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError("ts must be timezone-aware UTC")
    return value.astimezone(UTC)
