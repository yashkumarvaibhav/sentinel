"""The shared, deterministic machinery every independent evidence agent runs on.

An agent turns the detection plane's durable episodes into one 0-1 score for a
single axis. Three properties are structural rather than conventional:

* **Independence.** An agent is constructed with its own axis configuration and
  is handed telemetry-derived evidence only. There is no parameter through
  which another agent's score could reach it, so an attacker cannot keep one
  axis quiet in order to quiet another.
* **Justification.** Score is a fold over per-episode contributions, and every
  contribution above the configured floor is emitted as an ``EvidenceItem``. A
  positive score without evidence is rejected by the contract itself.
* **Honest insufficiency.** The caller states which detector kinds produced a
  result for the tick. If none of an axis's claimed kinds were covered, the
  agent reports ``INSUFFICIENT`` instead of a calm zero.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from contracts import (
    AgentAssessment,
    AgentStatus,
    AgentTrend,
    ChangeEvent,
    EpisodeStatus,
    EvidenceAxis,
    EvidenceDirection,
    EvidenceItem,
    SymptomEpisode,
    SymptomKind,
)
from decision.config import EvidenceAxisConfig

# A calm axis is scored against a zero baseline: with no episode of a claimed
# kind, the expected contribution of that kind to the axis is exactly nothing.
CALM_BASELINE = 0.0

# The kind an agent claims when its evidence is the operator change feed rather
# than a detector: coverage means the feed was consulted for this tick.
CHANGE_COVERAGE_KIND = SymptomKind.DEPLOY_MARKER


@dataclass(frozen=True, slots=True)
class AgentEvidenceWindow:
    """Everything an agent may look at for one event-time tick.

    ``covered_kinds`` is the caller's statement of which detector kinds actually
    produced a result for this tick. It is evidence in its own right and is
    never inferred from the episodes present, because "no episode" and "the
    detector never ran" mean opposite things. Consulting the operator change
    feed is stated the same way, as coverage of ``DEPLOY_MARKER``.
    """

    ts: datetime
    episodes: tuple[SymptomEpisode, ...]
    covered_kinds: frozenset[SymptomKind]
    changes: tuple[ChangeEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ts, datetime):
            raise TypeError("ts must be a datetime")
        if self.ts.utcoffset() != timedelta(0):
            raise ValueError("ts must be timezone-aware UTC")
        if not isinstance(self.episodes, tuple):
            raise TypeError("episodes must be a tuple")
        if not isinstance(self.changes, tuple):
            raise TypeError("changes must be a tuple")
        if not isinstance(self.covered_kinds, frozenset):
            raise TypeError("covered_kinds must be a frozenset")
        seen: set[str] = set()
        for episode in self.episodes:
            if not isinstance(episode, SymptomEpisode):
                raise TypeError("episodes must contain SymptomEpisode values")
            if episode.episode_id in seen:
                raise ValueError(
                    "duplicate episode revisions must be collapsed before assessment: "
                    f"{episode.episode_id}"
                )
            seen.add(episode.episode_id)
            if episode.kind not in self.covered_kinds:
                raise ValueError(
                    f"episode of kind {episode.kind.value} contradicts the stated coverage"
                )
            if episode.opened_ts > self.ts:
                raise ValueError("an episode cannot open after the tick that observes it")
        changed: set[str] = set()
        for change in self.changes:
            if not isinstance(change, ChangeEvent):
                raise TypeError("changes must contain ChangeEvent values")
            if change.change_id in changed:
                raise ValueError(f"duplicate change event: {change.change_id}")
            changed.add(change.change_id)
            if CHANGE_COVERAGE_KIND not in self.covered_kinds:
                raise ValueError(
                    "a change event contradicts the stated coverage: the change feed was "
                    f"not reported as covered for {change.change_id}"
                )
            if change.ts > self.ts:
                raise ValueError("a change cannot happen after the tick that observes it")

    @property
    def symptomatic_services(self) -> frozenset[str]:
        """Services carrying an active episode, whatever kind it is."""
        return frozenset(
            episode.service for episode in self.episodes if episode.status is EpisodeStatus.ACTIVE
        )


@dataclass(frozen=True, slots=True)
class Contribution:
    """One justified share of an axis score, with the item that explains it."""

    service: str
    item: EvidenceItem
    kind: SymptomKind

    @property
    def contribution(self) -> float:
        """How much of the axis score this evidence carries."""
        return self.item.contribution


class EvidenceAgent:
    """Base agent: scores exactly one axis from exactly its own claimed evidence."""

    axis: EvidenceAxis

    def __init__(self, *, configuration: EvidenceAxisConfig) -> None:
        expected = type(self).axis
        if configuration.axis is not expected:
            raise ValueError(
                f"{type(self).__name__} requires {expected.value} configuration, "
                f"received {configuration.axis.value}"
            )
        self._configuration = configuration
        self._last_ts: datetime | None = None
        self._last_score: float | None = None
        self._previous_score: float | None = None

    @property
    def configuration(self) -> EvidenceAxisConfig:
        """The single axis this agent is allowed to reason about."""
        return self._configuration

    def assess(self, window: AgentEvidenceWindow) -> AgentAssessment:
        """Score this axis for one tick, justified item by item."""
        if not isinstance(window, AgentEvidenceWindow):
            raise TypeError("window must be an AgentEvidenceWindow")
        ts = window.ts.astimezone(UTC)
        if self._last_ts is not None and ts < self._last_ts:
            raise ValueError("assessment ticks must not move backwards in event time")
        redelivery = self._last_ts is not None and ts == self._last_ts

        covered = tuple(
            sorted(
                self._configuration.claimed_kinds & window.covered_kinds,
                key=lambda kind: kind.value,
            )
        )
        if not covered:
            return self._insufficient(ts)

        contributions = self._contributions(window)
        score = _noisy_or(contribution.contribution for contribution in contributions)
        # A redelivered tick is compared with the same predecessor the first
        # delivery saw, so an exact repeat produces an identical assessment.
        trend = self._trend(score, self._previous_score if redelivery else self._last_score)
        evidence = tuple(contribution.item for contribution in contributions)
        services = tuple(sorted({contribution.service for contribution in contributions}))
        contributing = tuple(
            sorted(
                {contribution.kind for contribution in contributions},
                key=lambda kind: kind.value,
            )
        )
        note = (
            f"{self.axis.value.lower()} scored from {len(evidence)} active "
            f"{'episode' if len(evidence) == 1 else 'episodes'} across "
            f"{len(covered)} of {len(self._configuration.claimed_kinds)} claimed symptom kinds"
        )
        assessment = AgentAssessment(
            assessment_id=self._assessment_id(ts, score, evidence),
            axis=self.axis,
            ts=ts,
            status=AgentStatus.SCORED,
            score=score,
            trend=trend,
            covered_kinds=covered,
            contributing_kinds=contributing,
            services=services,
            evidence=evidence,
            note=note,
        )
        if not redelivery:
            self._previous_score = self._last_score
            self._last_score = score
            self._last_ts = ts
        return assessment

    def _insufficient(self, ts: datetime) -> AgentAssessment:
        note = (
            f"{self.axis.value.lower()} insufficient: no claimed detector kind reported "
            "coverage for this tick"
        )
        return AgentAssessment(
            assessment_id=self._assessment_id(ts, 0.0, ()),
            axis=self.axis,
            ts=ts,
            status=AgentStatus.INSUFFICIENT,
            score=0.0,
            trend=AgentTrend.UNKNOWN,
            note=note,
        )

    def _contributions(self, window: AgentEvidenceWindow) -> tuple[Contribution, ...]:
        """Turn this axis's claimed episodes into justified contributions."""
        found: list[Contribution] = []
        for episode in window.episodes:
            if episode.status is not EpisodeStatus.ACTIVE:
                continue
            weight = self._configuration.weight_for(episode.kind, episode.signal)
            if weight is None:
                continue
            multiplier = self._episode_multiplier(episode)
            contribution = weight * float(episode.peak_score) * multiplier
            if contribution < self._configuration.minimum_contribution:
                continue
            found.append(
                Contribution(
                    service=episode.service,
                    kind=episode.kind,
                    item=self._episode_item(
                        episode,
                        weight=weight,
                        multiplier=multiplier,
                        contribution=min(contribution, 1.0),
                    ),
                )
            )
        return order_contributions(found)

    def _episode_multiplier(self, episode: SymptomEpisode) -> float:
        """Axis-specific scaling of one episode; the base agent scales nothing."""
        del episode
        return 1.0

    def _episode_item(
        self,
        episode: SymptomEpisode,
        *,
        weight: float,
        multiplier: float,
        contribution: float,
    ) -> EvidenceItem:
        feature = f"{episode.service}.{episode.signal}"
        scaling = "" if multiplier == 1.0 else f", impact scaling {multiplier:.2f}"
        note = (
            f"{episode.kind.value} episode on {feature} peaked at "
            f"{episode.peak_score:.3f} over {episode.breach_tick_count} breaching "
            f"{'tick' if episode.breach_tick_count == 1 else 'ticks'}; weight "
            f"{weight:.2f}{scaling}"
        )
        return EvidenceItem(
            feature=feature,
            value=float(episode.peak_score),
            baseline=CALM_BASELINE,
            direction=EvidenceDirection.ABOVE_BASELINE,
            contribution=contribution,
            note=note,
            evidence_refs=(episode.episode_id,),
        )

    def _trend(self, score: float, previous: float | None) -> AgentTrend:
        if previous is None:
            return AgentTrend.UNKNOWN
        deadband = self._configuration.trend_deadband
        if score - previous > deadband:
            return AgentTrend.RISING
        if previous - score > deadband:
            return AgentTrend.FALLING
        return AgentTrend.STEADY

    def _assessment_id(
        self,
        ts: datetime,
        score: float,
        evidence: Sequence[EvidenceItem],
    ) -> str:
        identity = {
            "axis": self.axis.value,
            "ts": ts.isoformat(),
            "score": format(score, ".12g"),
            "evidence": [
                {
                    "feature": item.feature,
                    "contribution": format(item.contribution, ".12g"),
                    "refs": list(item.evidence_refs),
                }
                for item in evidence
            ],
        }
        rendered = json.dumps(identity, allow_nan=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def order_contributions(contributions: list[Contribution]) -> tuple[Contribution, ...]:
    """Fold in a stable order so the same evidence always yields the same float."""
    return tuple(
        sorted(contributions, key=lambda found: (found.item.feature, found.item.evidence_refs))
    )


def _noisy_or(contributions: Iterable[float]) -> float:
    """Combine independent contributions so corroboration raises, never lowers, a score.

    Each contribution is read as the probability that this evidence alone would
    justify the axis. Independent evidence therefore combines as
    ``1 - prod(1 - c)``: one strong item dominates, several weak items still add
    up, and no item can ever pull the score back down.
    """
    remaining = 1.0
    for contribution in contributions:
        remaining *= 1.0 - contribution
    return min(max(1.0 - remaining, 0.0), 1.0)
