"""Materialize fixed-capacity container gauges into saturation episodes.

Only explicitly configured Kubernetes container working-set and hard-limit
gauges enter this runner. A clear is emitted only after one pod instance has a
contiguous, sufficient resource series. Missing, malformed, contradictory, or
restart-overlap evidence is ``INSUFFICIENT`` and never advances episode state.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from common.config import (
    ChangePointSaturationConfig,
    EpisodeConfig,
    ResourceWindowRuleConfig,
)
from contracts import Observation, SymptomEpisode, SymptomKind
from detection.changepoint import ResourceSample, SaturationDetector, SaturationEvaluation
from detection.episodes import EpisodeKey, EpisodeTransition
from detection.pipeline import SymptomEpisodePipeline


class ResourceWindowStatus(StrEnum):
    """Whether one configured resource series was ready and saturated."""

    WARMING = "WARMING"
    INSUFFICIENT = "INSUFFICIENT"
    CLEAR = "CLEAR"
    BREACH = "BREACH"


@dataclass(frozen=True, slots=True)
class ResourceWindowAdvance:
    """Auditable outcome for one logical resource stream at one tick."""

    key: EpisodeKey
    tick_ts: datetime
    status: ResourceWindowStatus
    instance_id: str | None
    sample_count: int
    capacity: float | None
    used_evidence_id: str | None
    capacity_evidence_id: str | None
    ambiguous_observation_ids: tuple[str, ...]
    evaluation: SaturationEvaluation | None
    transition: EpisodeTransition | None

    def canonical_value(self) -> dict[str, object]:
        """Stable JSON-ready value for capture transcript hashing."""
        evaluation: dict[str, object] | None = None
        if self.evaluation is not None:
            evaluation = {
                "after_level": self.evaluation.after_level,
                "before_level": self.evaluation.before_level,
                "change_index": self.evaluation.change_index,
                "change_ts": (
                    None
                    if self.evaluation.change_ts is None
                    else self.evaluation.change_ts.isoformat()
                ),
                "growth_slope_per_second": self.evaluation.growth_slope_per_second,
                "headroom": self.evaluation.headroom,
                "headroom_ratio": self.evaluation.headroom_ratio,
                "sample_count": self.evaluation.sample_count,
                "symptom": (
                    None
                    if self.evaluation.symptom is None
                    else self.evaluation.symptom.model_dump(mode="json")
                ),
            }
        return {
            "ambiguous_observation_ids": self.ambiguous_observation_ids,
            "capacity": self.capacity,
            "capacity_evidence_id": self.capacity_evidence_id,
            "evaluation": evaluation,
            "instance_id": self.instance_id,
            "key": {
                "kind": self.key.kind.value,
                "service": self.key.service,
                "signal": self.key.signal,
            },
            "sample_count": self.sample_count,
            "status": self.status.value,
            "tick_ts": self.tick_ts.isoformat(),
            "transition": _canonical_transition(self.transition),
            "used_evidence_id": self.used_evidence_id,
        }


@dataclass(slots=True)
class _ResourceState:
    instance_id: str | None = None
    capacity: float | None = None
    capacity_evidence_id: str | None = None
    samples: list[ResourceSample] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _RoutedObservation:
    observation: Observation
    instance_id: str


class ResourceDetectionRunner:
    """Advance configured container resource series through PELT and episodes."""

    def __init__(
        self,
        *,
        configuration: ChangePointSaturationConfig,
        episodes: EpisodeConfig,
    ) -> None:
        self._configuration = configuration
        self._window = configuration.resource_windows
        self._rules = tuple(
            sorted(
                self._window.rules,
                key=lambda item: (item.service, item.detector_signal, item.container),
            )
        )
        self._rules_by_container = {rule.container: rule for rule in self._rules}
        self._states = {_rule_key(rule): _ResourceState() for rule in self._rules}
        self._detector = SaturationDetector(configuration=configuration)
        self._pipeline = SymptomEpisodePipeline(configuration=episodes)
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._last_tick: datetime | None = None
        self._last_batch_signature: tuple[tuple[str, str], ...] | None = None
        self._last_results: tuple[ResourceWindowAdvance, ...] | None = None

    @property
    def advance_seconds(self) -> int:
        return self._window.advance_seconds

    @property
    def monitored_keys(self) -> tuple[EpisodeKey, ...]:
        """Configured service/resource episode identities in stable order."""
        return tuple(_episode_key(rule) for rule in self._rules)

    def active_episodes(self) -> tuple[SymptomEpisode, ...]:
        return self._pipeline.active_episodes()

    def advance(
        self,
        *,
        observations: tuple[Observation, ...],
        tick_ts: datetime,
    ) -> tuple[ResourceWindowAdvance, ...]:
        """Consume one complete event-time tick without inventing resource values."""
        if not isinstance(observations, tuple):
            raise TypeError("observations must be a tuple")
        if any(not isinstance(item, Observation) for item in observations):
            raise TypeError("observations must contain only Observation values")
        tick = _utc(tick_ts, name="tick_ts")
        ordered = tuple(sorted(observations, key=lambda item: (item.ts, item.observation_id)))
        signature = tuple((item.observation_id, _observation_fingerprint(item)) for item in ordered)
        if self._last_tick is not None:
            if tick < self._last_tick:
                raise ValueError("resource ticks must arrive in event-time order")
            if tick == self._last_tick:
                if signature == self._last_batch_signature and self._last_results is not None:
                    return self._last_results
                raise ValueError("conflicting resource advance at the same event time")
            expected = self._last_tick + timedelta(seconds=self.advance_seconds)
            if tick != expected:
                raise ValueError("resource ticks must use complete configured ticks")

        routed: dict[tuple[str, str], list[_RoutedObservation]] = {
            _rule_key(rule): [] for rule in self._rules
        }
        ambiguous: dict[tuple[str, str], list[str]] = {_rule_key(rule): [] for rule in self._rules}
        for observation in ordered:
            rule = self._match_rule(observation)
            if rule is None:
                continue
            key = _rule_key(rule)
            fingerprint = _observation_fingerprint(observation)
            previous = self._seen.get(observation.observation_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("observation_id was reused with different resource evidence")
                self._seen.move_to_end(observation.observation_id)
                continue
            if observation.ts > tick:
                raise ValueError("resource evidence timestamp exceeds its runner tick")
            if self._last_tick is not None and observation.ts <= self._last_tick:
                raise ValueError("new resource evidence must follow the previous runner tick")
            self._remember(observation.observation_id, fingerprint)
            instance = observation.attributes.get("k8s.pod.uid")
            if (
                not _supported_observation(observation, unit=self._window.unit)
                or not isinstance(instance, str)
                or not instance.strip()
            ):
                ambiguous[key].append(observation.observation_id)
                continue
            routed[key].append(_RoutedObservation(observation, instance.strip()))

        results = tuple(
            self._advance_rule(
                rule,
                tick=tick,
                routed=tuple(routed[_rule_key(rule)]),
                initial_ambiguous=tuple(sorted(ambiguous[_rule_key(rule)])),
            )
            for rule in self._rules
        )
        self._last_tick = tick
        self._last_batch_signature = signature
        self._last_results = results
        return results

    def _match_rule(self, observation: Observation) -> ResourceWindowRuleConfig | None:
        if observation.signal not in {self._window.used_signal, self._window.capacity_signal}:
            return None
        if observation.attributes.get("k8s.namespace.name") != self._window.namespace:
            return None
        container = observation.attributes.get("k8s.container.name")
        if not isinstance(container, str):
            return None
        return self._rules_by_container.get(container)

    def _advance_rule(
        self,
        rule: ResourceWindowRuleConfig,
        *,
        tick: datetime,
        routed: tuple[_RoutedObservation, ...],
        initial_ambiguous: tuple[str, ...],
    ) -> ResourceWindowAdvance:
        state = self._states[_rule_key(rule)]
        key = _episode_key(rule)
        ambiguous = list(initial_ambiguous)
        instances = {item.instance_id for item in routed}
        if len(instances) > 1:
            ambiguous.extend(item.observation.observation_id for item in routed)
            _reset_state(state)
            return _non_evaluated(
                key=key,
                tick=tick,
                status=ResourceWindowStatus.INSUFFICIENT,
                state=state,
                ambiguous=tuple(sorted(set(ambiguous))),
            )

        instance = next(iter(instances), None)
        if instance is not None and instance != state.instance_id:
            _reset_state(state, instance_id=instance)
        if ambiguous:
            state.samples.clear()
            return _non_evaluated(
                key=key,
                tick=tick,
                status=ResourceWindowStatus.INSUFFICIENT,
                state=state,
                ambiguous=tuple(sorted(set(ambiguous))),
            )

        used = tuple(
            item.observation
            for item in routed
            if item.observation.signal == self._window.used_signal
        )
        capacities = tuple(
            item.observation
            for item in routed
            if item.observation.signal == self._window.capacity_signal
        )
        capacity_values = {item.value for item in capacities}
        if len(capacity_values) > 1 or any(value <= 0.0 for value in capacity_values):
            ids = tuple(sorted(item.observation_id for item in capacities))
            state.samples.clear()
            state.capacity = None
            state.capacity_evidence_id = None
            return _non_evaluated(
                key=key,
                tick=tick,
                status=ResourceWindowStatus.INSUFFICIENT,
                state=state,
                ambiguous=ids,
            )
        if capacities:
            capacity = next(iter(capacity_values))
            capacity_evidence = min(capacities, key=lambda item: item.observation_id)
            if state.capacity is not None and capacity != state.capacity:
                state.samples.clear()
                state.capacity = None
                state.capacity_evidence_id = None
                return _non_evaluated(
                    key=key,
                    tick=tick,
                    status=ResourceWindowStatus.INSUFFICIENT,
                    state=state,
                    ambiguous=(capacity_evidence.observation_id,),
                )
            state.capacity = capacity
            state.capacity_evidence_id = capacity_evidence.observation_id

        semantic_used = {(item.ts, item.value) for item in used}
        if len(semantic_used) > 1:
            state.samples.clear()
            return _non_evaluated(
                key=key,
                tick=tick,
                status=ResourceWindowStatus.INSUFFICIENT,
                state=state,
                ambiguous=tuple(sorted(item.observation_id for item in used)),
            )
        if not used or state.capacity is None or state.instance_id is None:
            state.samples.clear()
            return _non_evaluated(
                key=key,
                tick=tick,
                status=ResourceWindowStatus.INSUFFICIENT,
                state=state,
                ambiguous=(),
            )

        selected = min(used, key=lambda item: item.observation_id)
        if selected.value < 0.0 or selected.value > state.capacity:
            state.samples.clear()
            return _non_evaluated(
                key=key,
                tick=tick,
                status=ResourceWindowStatus.INSUFFICIENT,
                state=state,
                ambiguous=(selected.observation_id,),
            )
        sample = ResourceSample(
            evidence_id=selected.observation_id,
            ts=selected.ts,
            service=rule.service,
            signal=rule.detector_signal,
            used=selected.value,
            capacity=state.capacity,
            capacity_evidence_id=state.capacity_evidence_id,
        )
        if state.samples and sample.ts <= state.samples[-1].ts:
            state.samples.clear()
            return _non_evaluated(
                key=key,
                tick=tick,
                status=ResourceWindowStatus.INSUFFICIENT,
                state=state,
                ambiguous=(selected.observation_id,),
            )
        state.samples.append(sample)
        del state.samples[: -self._window.maximum_series_points]

        if len(state.samples) < self._configuration.minimum_series_points:
            return _non_evaluated(
                key=key,
                tick=tick,
                status=ResourceWindowStatus.WARMING,
                state=state,
                ambiguous=(),
                used_evidence_id=selected.observation_id,
            )

        evaluation = self._detector.evaluate(tuple(state.samples))
        transition = self._pipeline.observe(
            key=key,
            tick_ts=tick,
            symptom=evaluation.symptom,
        )
        return ResourceWindowAdvance(
            key=key,
            tick_ts=tick,
            status=(
                ResourceWindowStatus.BREACH
                if evaluation.symptom is not None
                else ResourceWindowStatus.CLEAR
            ),
            instance_id=state.instance_id,
            sample_count=len(state.samples),
            capacity=state.capacity,
            used_evidence_id=selected.observation_id,
            capacity_evidence_id=state.capacity_evidence_id,
            ambiguous_observation_ids=(),
            evaluation=evaluation,
            transition=transition,
        )

    def _remember(self, observation_id: str, fingerprint: str) -> None:
        self._seen[observation_id] = fingerprint
        self._seen.move_to_end(observation_id)
        while len(self._seen) > self._window.dedup_capacity:
            self._seen.popitem(last=False)


def _non_evaluated(
    *,
    key: EpisodeKey,
    tick: datetime,
    status: ResourceWindowStatus,
    state: _ResourceState,
    ambiguous: tuple[str, ...],
    used_evidence_id: str | None = None,
) -> ResourceWindowAdvance:
    return ResourceWindowAdvance(
        key=key,
        tick_ts=tick,
        status=status,
        instance_id=state.instance_id,
        sample_count=len(state.samples),
        capacity=state.capacity,
        used_evidence_id=used_evidence_id,
        capacity_evidence_id=state.capacity_evidence_id,
        ambiguous_observation_ids=ambiguous,
        evaluation=None,
        transition=None,
    )


def _reset_state(state: _ResourceState, *, instance_id: str | None = None) -> None:
    state.instance_id = instance_id
    state.capacity = None
    state.capacity_evidence_id = None
    state.samples.clear()


def _rule_key(rule: ResourceWindowRuleConfig) -> tuple[str, str]:
    return rule.service, rule.detector_signal


def _episode_key(rule: ResourceWindowRuleConfig) -> EpisodeKey:
    return EpisodeKey(SymptomKind.SATURATION, rule.service, rule.detector_signal)


def _supported_observation(observation: Observation, *, unit: str) -> bool:
    return (
        observation.unit == unit
        and observation.attributes.get("otel.metric.kind") == "gauge"
        and math.isfinite(observation.value)
    )


def _observation_fingerprint(observation: Observation) -> str:
    return json.dumps(
        observation.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_transition(transition: EpisodeTransition | None) -> dict[str, object] | None:
    if transition is None:
        return None
    return {
        "action": transition.action.value,
        "episode": (
            None if transition.episode is None else transition.episode.model_dump(mode="json")
        ),
        "phase": transition.phase.value,
    }


def _utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def canonical_resource_advances(values: tuple[ResourceWindowAdvance, ...]) -> bytes:
    """Stable byte representation for focused replay/property tests."""
    return (
        json.dumps(
            [item.canonical_value() for item in values],
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
