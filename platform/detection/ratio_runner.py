"""Materialize normalized HTTP ingress windows into behavioral-ratio episodes.

Only explicitly routed server spans enter this runner. Each complete event-time
window yields independent path-entropy and inter-arrival-variation results per
logical service, backed by frozen context-blind baselines. Empty, sparse,
malformed, duplicate, or degenerate evidence is ``INSUFFICIENT`` and never
advances episode state.
"""

from __future__ import annotations

import json
import math
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from urllib.parse import urlsplit

from common.config import BehavioralRatioConfig, EpisodeConfig
from contracts import Observation, SymptomEpisode, SymptomKind
from detection.episodes import EpisodeKey, EpisodeTransition
from detection.pipeline import SymptomEpisodePipeline
from detection.ratios import (
    BehavioralRatio,
    BehavioralRatioMonitor,
    RatioEvaluation,
    coefficient_of_variation,
    shannon_entropy,
)

_METRICS = (
    BehavioralRatio.PATH_ENTROPY,
    BehavioralRatio.INTERARRIVAL_VARIATION,
)
_SIGNALS = {
    BehavioralRatio.PATH_ENTROPY: "path_entropy",
    BehavioralRatio.INTERARRIVAL_VARIATION: "interarrival_cv",
}


class RatioWindowStatus(StrEnum):
    """Whether one service-ratio window was sufficient and deformed."""

    WARMING = "WARMING"
    INSUFFICIENT = "INSUFFICIENT"
    CLEAR = "CLEAR"
    BREACH = "BREACH"


@dataclass(frozen=True, slots=True)
class RatioWindowAdvance:
    """Auditable outcome for one logical service and one ingress ratio."""

    key: EpisodeKey
    metric: BehavioralRatio
    tick_ts: datetime
    status: RatioWindowStatus
    baseline_window_count: int
    baseline_value: float | None
    window_request_count: int
    ambiguous_observation_ids: tuple[str, ...]
    current: float | None
    evaluation: RatioEvaluation | None
    transition: EpisodeTransition | None

    def canonical_value(self) -> dict[str, object]:
        """Stable JSON-ready form for capture transcript hashing."""
        evaluation: dict[str, object] | None = None
        if self.evaluation is not None:
            evaluation = {
                "baseline": self.evaluation.baseline,
                "current": self.evaluation.current,
                "metric": self.evaluation.metric.value,
                "relative_deformation": self.evaluation.relative_deformation,
                "symptom": (
                    None
                    if self.evaluation.symptom is None
                    else self.evaluation.symptom.model_dump(mode="json")
                ),
            }
        return {
            "ambiguous_observation_ids": self.ambiguous_observation_ids,
            "baseline_value": self.baseline_value,
            "baseline_window_count": self.baseline_window_count,
            "current": self.current,
            "evaluation": evaluation,
            "key": {
                "kind": self.key.kind.value,
                "service": self.key.service,
                "signal": self.key.signal,
            },
            "metric": self.metric.value,
            "status": self.status.value,
            "tick_ts": self.tick_ts.isoformat(),
            "transition": _canonical_transition(self.transition),
            "window_request_count": self.window_request_count,
        }


@dataclass(frozen=True, slots=True)
class _IngressRequest:
    observation_id: str
    ts: datetime
    path: str


@dataclass(slots=True)
class _RatioState:
    baseline_values: list[float] = field(default_factory=list)


class IngressRatioDetectionRunner:
    """Advance configured ingress ratios through deterministic episodes."""

    def __init__(
        self,
        *,
        configuration: BehavioralRatioConfig,
        episodes: EpisodeConfig,
    ) -> None:
        self._configuration = configuration
        self._window = configuration.ingress_windows
        self._source_to_service = dict(self._window.service_mappings)
        self._services = tuple(sorted(set(self._source_to_service.values())))
        self._states = {
            (service, metric): _RatioState() for service in self._services for metric in _METRICS
        }
        self._monitor = BehavioralRatioMonitor(configuration=configuration)
        self._pipeline = SymptomEpisodePipeline(configuration=episodes)
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._last_tick: datetime | None = None
        self._last_batch_signature: tuple[tuple[str, str], ...] | None = None
        self._last_results: tuple[RatioWindowAdvance, ...] | None = None

    @property
    def advance_seconds(self) -> int:
        return self._window.window_seconds

    @property
    def monitored_keys(self) -> tuple[EpisodeKey, ...]:
        """Configured service/ratio episode identities in deterministic order."""
        return tuple(
            _episode_key(service, metric) for service in self._services for metric in _METRICS
        )

    def active_episodes(self) -> tuple[SymptomEpisode, ...]:
        return self._pipeline.active_episodes()

    def advance(
        self,
        *,
        observations: tuple[Observation, ...],
        tick_ts: datetime,
    ) -> tuple[RatioWindowAdvance, ...]:
        """Consume one complete tumbling window and advance all configured ratios."""
        if not isinstance(observations, tuple):
            raise TypeError("observations must be a tuple")
        if any(not isinstance(item, Observation) for item in observations):
            raise TypeError("observations must contain only Observation values")
        tick = _utc(tick_ts, name="tick_ts")
        ordered = tuple(sorted(observations, key=lambda item: (item.ts, item.observation_id)))
        signature = tuple((item.observation_id, _observation_fingerprint(item)) for item in ordered)
        if self._last_tick is not None:
            if tick < self._last_tick:
                raise ValueError("ingress ratio ticks must arrive in event-time order")
            if tick == self._last_tick:
                if signature == self._last_batch_signature and self._last_results is not None:
                    return self._last_results
                raise ValueError("conflicting ingress ratio advance at the same event time")
            expected = self._last_tick + timedelta(seconds=self._window.window_seconds)
            if tick != expected:
                raise ValueError("ingress ratio ticks must use complete configured windows")

        requests: dict[str, list[_IngressRequest]] = {service: [] for service in self._services}
        ambiguous: dict[str, list[str]] = {service: [] for service in self._services}
        for observation in ordered:
            service = self._match_service(observation)
            if service is None:
                continue
            fingerprint = _observation_fingerprint(observation)
            previous = self._seen.get(observation.observation_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("observation_id was reused with different ingress evidence")
                self._seen.move_to_end(observation.observation_id)
                continue
            if observation.ts > tick:
                raise ValueError("ingress evidence timestamp exceeds its runner tick")
            if self._last_tick is not None and observation.ts <= self._last_tick:
                raise ValueError("new ingress evidence must follow the previous runner tick")
            self._remember(observation.observation_id, fingerprint)
            request = _ingress_request(observation)
            if request is None:
                ambiguous[service].append(observation.observation_id)
                continue
            requests[service].append(request)

        results = tuple(
            advance
            for service in self._services
            for advance in self._advance_service(
                service,
                tick=tick,
                requests=tuple(requests[service]),
                ambiguous_ids=tuple(sorted(ambiguous[service])),
            )
        )
        self._last_tick = tick
        self._last_batch_signature = signature
        self._last_results = results
        return results

    def _match_service(self, observation: Observation) -> str | None:
        if observation.signal != "span.duration_ms" or observation.unit != "ms":
            return None
        if observation.attributes.get("span.kind") not in (2, "2"):
            return None
        return self._source_to_service.get(observation.service)

    def _advance_service(
        self,
        service: str,
        *,
        tick: datetime,
        requests: tuple[_IngressRequest, ...],
        ambiguous_ids: tuple[str, ...],
    ) -> tuple[RatioWindowAdvance, ...]:
        ordered = tuple(sorted(requests, key=lambda item: (item.ts, item.observation_id)))
        enough_requests = len(ordered) >= self._window.minimum_window_requests
        path_counts = Counter(item.path for item in ordered)
        gaps = tuple(
            (current.ts - previous.ts).total_seconds() for previous, current in pairwise(ordered)
        )
        current_values = {
            BehavioralRatio.PATH_ENTROPY: shannon_entropy(path_counts),
            BehavioralRatio.INTERARRIVAL_VARIATION: (
                coefficient_of_variation(gaps)
                if len(gaps) >= self._configuration.interarrival_variation.minimum_points
                else None
            ),
        }
        return tuple(
            self._advance_metric(
                service,
                metric=metric,
                tick=tick,
                requests=ordered,
                ambiguous_ids=ambiguous_ids,
                enough_requests=enough_requests,
                path_counts=path_counts,
                gaps=gaps,
                current=current_values[metric],
            )
            for metric in _METRICS
        )

    def _advance_metric(
        self,
        service: str,
        *,
        metric: BehavioralRatio,
        tick: datetime,
        requests: tuple[_IngressRequest, ...],
        ambiguous_ids: tuple[str, ...],
        enough_requests: bool,
        path_counts: Counter[str],
        gaps: tuple[float, ...],
        current: float | None,
    ) -> RatioWindowAdvance:
        state = self._states[(service, metric)]
        key = _episode_key(service, metric)
        baseline = _baseline_value(state)
        if ambiguous_ids or not enough_requests or current is None:
            return RatioWindowAdvance(
                key=key,
                metric=metric,
                tick_ts=tick,
                status=RatioWindowStatus.INSUFFICIENT,
                baseline_window_count=len(state.baseline_values),
                baseline_value=baseline,
                window_request_count=len(requests),
                ambiguous_observation_ids=ambiguous_ids,
                current=current,
                evaluation=None,
                transition=None,
            )

        if len(state.baseline_values) < self._window.baseline_warmup_windows:
            state.baseline_values.append(current)
            return RatioWindowAdvance(
                key=key,
                metric=metric,
                tick_ts=tick,
                status=RatioWindowStatus.WARMING,
                baseline_window_count=len(state.baseline_values),
                baseline_value=_baseline_value(state),
                window_request_count=len(requests),
                ambiguous_observation_ids=(),
                current=current,
                evaluation=None,
                transition=None,
            )

        if baseline is None:  # protected by the warmup branch
            raise RuntimeError("ingress ratio baseline is unexpectedly absent")
        onset = requests[0].ts
        evidence_refs = tuple(item.observation_id for item in requests)
        if metric is BehavioralRatio.PATH_ENTROPY:
            evaluation = self._monitor.evaluate_path_entropy(
                service=service,
                onset_ts=onset,
                path_counts=path_counts,
                baseline_entropy=baseline,
                evidence_refs=evidence_refs,
            )
        else:
            evaluation = self._monitor.evaluate_interarrival_variation(
                service=service,
                onset_ts=onset,
                interarrival_seconds=gaps,
                baseline_cv=baseline,
                evidence_refs=evidence_refs,
            )
        transition = self._pipeline.observe(
            key=key,
            tick_ts=tick,
            symptom=evaluation.symptom,
        )
        return RatioWindowAdvance(
            key=key,
            metric=metric,
            tick_ts=tick,
            status=(
                RatioWindowStatus.BREACH
                if evaluation.symptom is not None
                else RatioWindowStatus.CLEAR
            ),
            baseline_window_count=len(state.baseline_values),
            baseline_value=baseline,
            window_request_count=len(requests),
            ambiguous_observation_ids=(),
            current=current,
            evaluation=evaluation,
            transition=transition,
        )

    def _remember(self, observation_id: str, fingerprint: str) -> None:
        self._seen[observation_id] = fingerprint
        self._seen.move_to_end(observation_id)
        while len(self._seen) > self._window.dedup_capacity:
            self._seen.popitem(last=False)


def _baseline_value(state: _RatioState) -> float | None:
    if not state.baseline_values:
        return None
    return math.fsum(state.baseline_values) / len(state.baseline_values)


def _ingress_request(observation: Observation) -> _IngressRequest | None:
    if observation.value < 0.0 or len(observation.trace_refs) != 1:
        return None
    path = _http_path(observation.attributes.get("http.url"))
    if path is None:
        return None
    return _IngressRequest(
        observation_id=observation.observation_id,
        ts=observation.ts,
        path=path,
    )


def _http_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in ("http", "https")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
        or not parsed.path.startswith("/")
    ):
        return None
    return parsed.path or "/"


def _episode_key(service: str, metric: BehavioralRatio) -> EpisodeKey:
    return EpisodeKey(SymptomKind.RATIO_DEFORM, service, _SIGNALS[metric])


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


def canonical_ratio_advances(values: tuple[RatioWindowAdvance, ...]) -> bytes:
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
