"""Materialize normalized trace windows and run deterministic edge detection.

The raw OTLP normalizer is the only component that understands collector wire
shapes. This layer consumes its public ``Observation`` contract, maps configured
client RPC spans to topology edges, freezes a context-blind bootstrap baseline,
and advances every monitored edge on caller-supplied event-time ticks.

An absent symptom is a clear only after the detector received a sufficient,
unambiguous call window. Sparse, malformed, or contradictory telemetry produces
an explicit ``INSUFFICIENT`` result and does not advance episode state.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from common.config import (
    EdgeDegradationConfig,
    EdgeDegradationRuleConfig,
    EpisodeConfig,
)
from contracts import Observation, SymptomEpisode, SymptomKind
from detection.edges import (
    DependencyCall,
    EdgeDegradationDetector,
    EdgeDegradationEvaluation,
)
from detection.episodes import EpisodeKey, EpisodeTransition
from detection.pipeline import SymptomEpisodePipeline

type Edge = tuple[str, str]
type TelemetryEdge = tuple[str, str]


class EdgeWindowStatus(StrEnum):
    """Whether one monitored edge tick had enough evidence and breached."""

    WARMING = "WARMING"
    INSUFFICIENT = "INSUFFICIENT"
    CLEAR = "CLEAR"
    BREACH = "BREACH"


@dataclass(frozen=True, slots=True)
class EdgeWindowAdvance:
    """Auditable result of one event-time advance for one monitored edge."""

    key: EpisodeKey
    tick_ts: datetime
    status: EdgeWindowStatus
    baseline_sample_count: int
    baseline_latency_p95_ms: float | None
    baseline_error_rate: float | None
    window_sample_count: int
    ambiguous_observation_ids: tuple[str, ...]
    evaluation: EdgeDegradationEvaluation | None
    transition: EpisodeTransition | None

    def canonical_value(self) -> dict[str, object]:
        """Stable JSON-ready form for capture transcript hashing."""
        evaluation: dict[str, object] | None = None
        if self.evaluation is not None:
            evaluation = {
                "baseline_error_rate": self.evaluation.baseline_error_rate,
                "baseline_latency_p95_ms": self.evaluation.baseline_latency_p95_ms,
                "caller": self.evaluation.caller,
                "downstream": self.evaluation.downstream,
                "error_rate": self.evaluation.error_rate,
                "error_relative_rise": self.evaluation.error_relative_rise,
                "latency_p95_ms": self.evaluation.latency_p95_ms,
                "latency_relative_rise": self.evaluation.latency_relative_rise,
                "sample_count": self.evaluation.sample_count,
                "symptom": (
                    None
                    if self.evaluation.symptom is None
                    else self.evaluation.symptom.model_dump(mode="json")
                ),
            }
        transition: dict[str, object] | None = None
        if self.transition is not None:
            transition = {
                "action": self.transition.action.value,
                "episode": (
                    None
                    if self.transition.episode is None
                    else self.transition.episode.model_dump(mode="json")
                ),
                "phase": self.transition.phase.value,
            }
        return {
            "ambiguous_observation_ids": self.ambiguous_observation_ids,
            "baseline_error_rate": self.baseline_error_rate,
            "baseline_latency_p95_ms": self.baseline_latency_p95_ms,
            "baseline_sample_count": self.baseline_sample_count,
            "evaluation": evaluation,
            "key": {
                "kind": self.key.kind.value,
                "service": self.key.service,
                "signal": self.key.signal,
            },
            "status": self.status.value,
            "tick_ts": self.tick_ts.isoformat(),
            "transition": transition,
            "window_sample_count": self.window_sample_count,
        }


@dataclass(slots=True)
class _EdgeState:
    baseline: list[DependencyCall] = field(default_factory=list)
    current: list[DependencyCall] = field(default_factory=list)
    ambiguous: list[tuple[datetime, str]] = field(default_factory=list)


class EdgeDetectionRunner:
    """Advance configured trace edges through detector and episode layers."""

    def __init__(
        self,
        *,
        configuration: EdgeDegradationConfig,
        episodes: EpisodeConfig,
    ) -> None:
        self._configuration = configuration
        self._rules = tuple(
            sorted(configuration.rules, key=lambda item: (item.caller, item.downstream))
        )
        self._rules_by_edge: dict[Edge, EdgeDegradationRuleConfig] = {
            (rule.caller, rule.downstream): rule for rule in self._rules
        }
        self._rules_by_telemetry: dict[TelemetryEdge, EdgeDegradationRuleConfig] = {
            (rule.caller, rule.rpc_service): rule for rule in self._rules
        }
        self._states = {edge: _EdgeState() for edge in self._rules_by_edge}
        self._detector = EdgeDegradationDetector(configuration=configuration)
        self._pipeline = SymptomEpisodePipeline(configuration=episodes)
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._last_tick: datetime | None = None
        self._last_batch_signature: tuple[tuple[str, str], ...] | None = None
        self._last_results: tuple[EdgeWindowAdvance, ...] | None = None

    @property
    def advance_seconds(self) -> int:
        return self._configuration.advance_seconds

    @property
    def monitored_keys(self) -> tuple[EpisodeKey, ...]:
        """Configured edge episode identities in deterministic order."""
        return tuple(_episode_key(rule) for rule in self._rules)

    def active_episodes(self) -> tuple[SymptomEpisode, ...]:
        return self._pipeline.active_episodes()

    def advance(
        self,
        *,
        observations: tuple[Observation, ...],
        tick_ts: datetime,
    ) -> tuple[EdgeWindowAdvance, ...]:
        """Consume one tick's observations and advance every monitored key once."""
        if not isinstance(observations, tuple):
            raise TypeError("observations must be a tuple")
        if any(not isinstance(item, Observation) for item in observations):
            raise TypeError("observations must contain only Observation values")
        tick = _utc(tick_ts, name="tick_ts")
        ordered = tuple(sorted(observations, key=lambda item: (item.ts, item.observation_id)))
        signature = tuple((item.observation_id, _observation_fingerprint(item)) for item in ordered)
        if self._last_tick is not None:
            if tick < self._last_tick:
                raise ValueError("edge runner ticks must arrive in event-time order")
            if tick == self._last_tick:
                if signature == self._last_batch_signature and self._last_results is not None:
                    return self._last_results
                raise ValueError("conflicting edge runner advance at the same event time")
            expected = self._last_tick + timedelta(seconds=self._configuration.advance_seconds)
            if tick != expected:
                raise ValueError("edge runner ticks must use the configured advance_seconds")

        for observation in ordered:
            matched = self._match_rule(observation)
            if matched is None:
                continue
            rule, state = matched
            fingerprint = _observation_fingerprint(observation)
            previous = self._seen.get(observation.observation_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("observation_id was reused with different edge evidence")
                self._seen.move_to_end(observation.observation_id)
                continue
            if observation.ts > tick:
                raise ValueError("edge evidence timestamp exceeds its runner tick")
            if self._last_tick is not None and observation.ts <= self._last_tick:
                raise ValueError("new edge evidence must follow the previous runner tick")
            self._remember(observation.observation_id, fingerprint)
            call = _dependency_call(observation, rule)
            if call is None:
                state.ambiguous.append((observation.ts, observation.observation_id))
            elif len(state.baseline) < self._configuration.baseline_warmup_samples:
                state.baseline.append(call)
                state.baseline.sort(key=lambda item: (item.ts, item.evidence_id))
            else:
                state.current.append(call)
                state.current.sort(key=lambda item: (item.ts, item.evidence_id))

        results = tuple(self._advance_edge(rule, tick) for rule in self._rules)
        self._last_tick = tick
        self._last_batch_signature = signature
        self._last_results = results
        return results

    def _match_rule(
        self, observation: Observation
    ) -> tuple[EdgeDegradationRuleConfig, _EdgeState] | None:
        if observation.signal != "span.duration_ms" or observation.unit != "ms":
            return None
        if observation.attributes.get("span.kind") not in (3, "3"):
            return None
        rpc_service = observation.attributes.get("rpc.service")
        if not isinstance(rpc_service, str):
            return None
        rule = self._rules_by_telemetry.get((observation.service, rpc_service))
        if rule is None:
            return None
        return rule, self._states[(rule.caller, rule.downstream)]

    def _advance_edge(
        self,
        rule: EdgeDegradationRuleConfig,
        tick: datetime,
    ) -> EdgeWindowAdvance:
        state = self._states[(rule.caller, rule.downstream)]
        cutoff = tick - timedelta(seconds=self._configuration.window_seconds)
        state.current = [call for call in state.current if call.ts > cutoff]
        state.ambiguous = [item for item in state.ambiguous if item[0] > cutoff]
        baseline_count = len(state.baseline)
        baseline_latency: float | None = None
        baseline_errors: float | None = None
        if baseline_count >= self._configuration.baseline_warmup_samples:
            baseline_latency = _nearest_rank_p95(tuple(call.latency_ms for call in state.baseline))
            baseline_errors = sum(call.failed for call in state.baseline) / baseline_count

        key = _episode_key(rule)
        ambiguous_ids = tuple(item[1] for item in sorted(state.ambiguous))
        if baseline_latency is None or baseline_errors is None:
            return EdgeWindowAdvance(
                key=key,
                tick_ts=tick,
                status=EdgeWindowStatus.WARMING,
                baseline_sample_count=baseline_count,
                baseline_latency_p95_ms=None,
                baseline_error_rate=None,
                window_sample_count=len(state.current),
                ambiguous_observation_ids=ambiguous_ids,
                evaluation=None,
                transition=None,
            )
        if ambiguous_ids:
            return EdgeWindowAdvance(
                key=key,
                tick_ts=tick,
                status=EdgeWindowStatus.INSUFFICIENT,
                baseline_sample_count=baseline_count,
                baseline_latency_p95_ms=baseline_latency,
                baseline_error_rate=baseline_errors,
                window_sample_count=len(state.current),
                ambiguous_observation_ids=ambiguous_ids,
                evaluation=None,
                transition=None,
            )
        evaluation = (
            None
            if not state.current
            else self._detector.evaluate(
                tuple(state.current),
                baseline_latency_p95_ms=baseline_latency,
                baseline_error_rate=baseline_errors,
            )
        )
        if evaluation is None or evaluation.latency_relative_rise is None:
            return EdgeWindowAdvance(
                key=key,
                tick_ts=tick,
                status=EdgeWindowStatus.INSUFFICIENT,
                baseline_sample_count=baseline_count,
                baseline_latency_p95_ms=baseline_latency,
                baseline_error_rate=baseline_errors,
                window_sample_count=len(state.current),
                ambiguous_observation_ids=(),
                evaluation=evaluation,
                transition=None,
            )
        symptom = evaluation.symptom
        transition = self._pipeline.observe(key=key, tick_ts=tick, symptom=symptom)
        return EdgeWindowAdvance(
            key=key,
            tick_ts=tick,
            status=(EdgeWindowStatus.BREACH if symptom is not None else EdgeWindowStatus.CLEAR),
            baseline_sample_count=baseline_count,
            baseline_latency_p95_ms=baseline_latency,
            baseline_error_rate=baseline_errors,
            window_sample_count=len(state.current),
            ambiguous_observation_ids=(),
            evaluation=evaluation,
            transition=transition,
        )

    def _remember(self, observation_id: str, fingerprint: str) -> None:
        self._seen[observation_id] = fingerprint
        self._seen.move_to_end(observation_id)
        while len(self._seen) > self._configuration.dedup_capacity:
            self._seen.popitem(last=False)


def _dependency_call(
    observation: Observation,
    rule: EdgeDegradationRuleConfig,
) -> DependencyCall | None:
    if observation.attributes.get("span.kind") not in (3, "3"):
        return None
    if len(observation.trace_refs) != 1 or observation.value < 0.0:
        return None
    grpc_status = _status_code(observation.attributes.get("rpc.grpc.status_code"))
    span_status = _status_code(observation.attributes.get("span.status_code"), optional=True)
    if grpc_status is None:
        return None
    failed = grpc_status != 0
    if span_status == 1 and failed:
        return None
    if span_status == 2 and not failed:
        return None
    try:
        return DependencyCall(
            evidence_id=observation.observation_id,
            ts=observation.ts,
            caller=rule.caller,
            downstream=rule.downstream,
            latency_ms=observation.value,
            failed=failed,
        )
    except (TypeError, ValueError):
        return None


def _status_code(value: object, *, optional: bool = False) -> int | None:
    if value is None:
        return 0 if optional else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        code = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        code = int(value)
    else:
        return None
    return code if 0 <= code <= 255 else None


def _nearest_rank_p95(values: tuple[float, ...]) -> float:
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[rank - 1]


def _episode_key(rule: EdgeDegradationRuleConfig) -> EpisodeKey:
    return EpisodeKey(
        SymptomKind.EDGE_DEGRADED,
        rule.caller,
        f"dependency.{rule.downstream}",
    )


def _observation_fingerprint(observation: Observation) -> str:
    return json.dumps(
        observation.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def canonical_edge_advances(values: tuple[EdgeWindowAdvance, ...]) -> bytes:
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
