"""Materialize normalized log windows and route deterministic burst symptoms.

The runner consumes only public ``Observation`` values. Explicit configuration
maps telemetry service names to logical topology services, each complete
event-time window is mined exactly once, and context-blind baseline rates freeze
after a configured number of sufficient warmup windows.

An absent burst is a clear only when the service window contains enough valid,
unambiguous log records. Empty, sparse, malformed, partial, or contradictory
evidence is explicit ``INSUFFICIENT`` and never advances episode state.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from common.config import EpisodeConfig, LogTemplateConfig
from contracts import Observation, Symptom, SymptomEpisode, SymptomKind
from detection.episodes import EpisodeKey, EpisodeTransition
from detection.logs import (
    LogDetectionResult,
    LogLine,
    LogTemplateBurstDetector,
    TemplateFrequency,
)
from detection.pipeline import SymptomEpisodePipeline


class LogWindowStatus(StrEnum):
    """Whether one monitored service window had enough evidence and breached."""

    WARMING = "WARMING"
    INSUFFICIENT = "INSUFFICIENT"
    CLEAR = "CLEAR"
    BREACH = "BREACH"


@dataclass(frozen=True, slots=True)
class LogWindowAdvance:
    """Auditable outcome for one service at one complete event-time window."""

    key: EpisodeKey
    tick_ts: datetime
    status: LogWindowStatus
    baseline_window_count: int
    baseline_rates: tuple[tuple[str, float], ...]
    window_record_count: int
    ambiguous_observation_ids: tuple[str, ...]
    frequencies: tuple[TemplateFrequency, ...]
    symptoms: tuple[Symptom, ...]
    selected_symptom: Symptom | None
    transition: EpisodeTransition | None

    def canonical_value(self) -> dict[str, object]:
        """Stable JSON-ready value for capture transcript hashing."""
        return {
            "ambiguous_observation_ids": self.ambiguous_observation_ids,
            "baseline_rates": [
                {"messages_per_second": rate, "template_id": template_id}
                for template_id, rate in self.baseline_rates
            ],
            "baseline_window_count": self.baseline_window_count,
            "frequencies": [
                {
                    "count": item.count,
                    "evidence_refs": item.evidence_refs,
                    "first_ts": item.first_ts.isoformat(),
                    "messages_per_second": item.messages_per_second,
                    "service": item.service,
                    "template": item.template,
                    "template_id": item.template_id,
                }
                for item in self.frequencies
            ],
            "key": {
                "kind": self.key.kind.value,
                "service": self.key.service,
                "signal": self.key.signal,
            },
            "selected_symptom": (
                None
                if self.selected_symptom is None
                else self.selected_symptom.model_dump(mode="json")
            ),
            "status": self.status.value,
            "symptoms": [item.model_dump(mode="json") for item in self.symptoms],
            "tick_ts": self.tick_ts.isoformat(),
            "transition": _canonical_transition(self.transition),
            "window_record_count": self.window_record_count,
        }


@dataclass(slots=True)
class _LogServiceState:
    detector: LogTemplateBurstDetector
    baseline_window_count: int = 0
    baseline_rate_totals: dict[str, float] = field(default_factory=dict)


class LogDetectionRunner:
    """Advance configured service log windows through detector and episodes."""

    def __init__(
        self,
        *,
        configuration: LogTemplateConfig,
        episodes: EpisodeConfig,
    ) -> None:
        self._configuration = configuration
        self._source_to_service = dict(configuration.service_mappings)
        self._services = tuple(sorted(set(self._source_to_service.values())))
        self._states = {
            service: _LogServiceState(
                detector=LogTemplateBurstDetector(configuration=configuration)
            )
            for service in self._services
        }
        self._pipeline = SymptomEpisodePipeline(configuration=episodes)
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._last_tick: datetime | None = None
        self._last_batch_signature: tuple[tuple[str, str], ...] | None = None
        self._last_results: tuple[LogWindowAdvance, ...] | None = None

    @property
    def advance_seconds(self) -> int:
        return self._configuration.window_seconds

    @property
    def monitored_keys(self) -> tuple[EpisodeKey, ...]:
        """Configured service-level log episode identities in stable order."""
        return tuple(_episode_key(service) for service in self._services)

    def active_episodes(self) -> tuple[SymptomEpisode, ...]:
        return self._pipeline.active_episodes()

    def advance(
        self,
        *,
        observations: tuple[Observation, ...],
        tick_ts: datetime,
    ) -> tuple[LogWindowAdvance, ...]:
        """Consume one complete tumbling window and advance each monitored service."""
        if not isinstance(observations, tuple):
            raise TypeError("observations must be a tuple")
        if any(not isinstance(item, Observation) for item in observations):
            raise TypeError("observations must contain only Observation values")
        tick = _utc(tick_ts, name="tick_ts")
        ordered = tuple(sorted(observations, key=lambda item: (item.ts, item.observation_id)))
        signature = tuple((item.observation_id, _observation_fingerprint(item)) for item in ordered)
        if self._last_tick is not None:
            if tick < self._last_tick:
                raise ValueError("log runner ticks must arrive in event-time order")
            if tick == self._last_tick:
                if signature == self._last_batch_signature and self._last_results is not None:
                    return self._last_results
                raise ValueError("conflicting log runner advance at the same event time")
            expected = self._last_tick + timedelta(seconds=self._configuration.window_seconds)
            if tick != expected:
                raise ValueError("log runner ticks must use complete configured windows")

        records: dict[str, list[LogLine]] = {service: [] for service in self._services}
        ambiguous: dict[str, list[str]] = {service: [] for service in self._services}
        log_ids: dict[str, set[str]] = {service: set() for service in self._services}
        for observation in ordered:
            service = self._match_service(observation)
            if service is None:
                continue
            fingerprint = _observation_fingerprint(observation)
            previous = self._seen.get(observation.observation_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("observation_id was reused with different log evidence")
                self._seen.move_to_end(observation.observation_id)
                continue
            if observation.ts > tick:
                raise ValueError("log evidence timestamp exceeds its runner tick")
            if self._last_tick is not None and observation.ts <= self._last_tick:
                raise ValueError("new log evidence must follow the previous runner tick")
            self._remember(observation.observation_id, fingerprint)
            line = _log_line(observation, logical_service=service)
            if line is None or line.log_id in log_ids[service]:
                ambiguous[service].append(observation.observation_id)
                continue
            log_ids[service].add(line.log_id)
            records[service].append(line)

        results = tuple(
            self._advance_service(
                service,
                tick=tick,
                records=tuple(records[service]),
                ambiguous_ids=tuple(sorted(ambiguous[service])),
            )
            for service in self._services
        )
        self._last_tick = tick
        self._last_batch_signature = signature
        self._last_results = results
        return results

    def _match_service(self, observation: Observation) -> str | None:
        if observation.signal != "log.record" or observation.unit != "record":
            return None
        return self._source_to_service.get(observation.service)

    def _advance_service(
        self,
        service: str,
        *,
        tick: datetime,
        records: tuple[LogLine, ...],
        ambiguous_ids: tuple[str, ...],
    ) -> LogWindowAdvance:
        state = self._states[service]
        key = _episode_key(service)
        baseline_rates = _baseline_rates(state)
        enough_records = len(records) >= self._configuration.minimum_window_records

        if ambiguous_ids:
            frequencies = state.detector.mine_window(
                records,
                window_seconds=self._configuration.window_seconds,
            )
            return _non_evaluated_advance(
                key=key,
                tick=tick,
                status=LogWindowStatus.INSUFFICIENT,
                state=state,
                baseline_rates=baseline_rates,
                records=records,
                ambiguous_ids=ambiguous_ids,
                frequencies=frequencies,
            )

        if state.baseline_window_count < self._configuration.baseline_warmup_windows:
            frequencies = state.detector.mine_window(
                records,
                window_seconds=self._configuration.window_seconds,
            )
            if enough_records:
                for frequency in frequencies:
                    state.baseline_rate_totals[frequency.template_id] = (
                        state.baseline_rate_totals.get(frequency.template_id, 0.0)
                        + frequency.messages_per_second
                    )
                state.baseline_window_count += 1
                baseline_rates = _baseline_rates(state)
            return _non_evaluated_advance(
                key=key,
                tick=tick,
                status=LogWindowStatus.WARMING,
                state=state,
                baseline_rates=baseline_rates,
                records=records,
                ambiguous_ids=(),
                frequencies=frequencies,
            )

        if not enough_records:
            frequencies = state.detector.mine_window(
                records,
                window_seconds=self._configuration.window_seconds,
            )
            return _non_evaluated_advance(
                key=key,
                tick=tick,
                status=LogWindowStatus.INSUFFICIENT,
                state=state,
                baseline_rates=baseline_rates,
                records=records,
                ambiguous_ids=(),
                frequencies=frequencies,
            )

        evaluation = state.detector.detect_window(
            records,
            window_seconds=self._configuration.window_seconds,
            baseline_rates=dict(baseline_rates),
        )
        selected = _select_symptom(
            evaluation,
            baseline_rates=baseline_rates,
            baseline_floor=self._configuration.baseline_rate_floor,
        )
        transition = self._pipeline.observe(key=key, tick_ts=tick, symptom=selected)
        return LogWindowAdvance(
            key=key,
            tick_ts=tick,
            status=(LogWindowStatus.BREACH if evaluation.symptoms else LogWindowStatus.CLEAR),
            baseline_window_count=state.baseline_window_count,
            baseline_rates=baseline_rates,
            window_record_count=len(records),
            ambiguous_observation_ids=(),
            frequencies=evaluation.frequencies,
            symptoms=evaluation.symptoms,
            selected_symptom=selected,
            transition=transition,
        )

    def _remember(self, observation_id: str, fingerprint: str) -> None:
        self._seen[observation_id] = fingerprint
        self._seen.move_to_end(observation_id)
        while len(self._seen) > self._configuration.dedup_capacity:
            self._seen.popitem(last=False)


def _non_evaluated_advance(
    *,
    key: EpisodeKey,
    tick: datetime,
    status: LogWindowStatus,
    state: _LogServiceState,
    baseline_rates: tuple[tuple[str, float], ...],
    records: tuple[LogLine, ...],
    ambiguous_ids: tuple[str, ...],
    frequencies: tuple[TemplateFrequency, ...],
) -> LogWindowAdvance:
    return LogWindowAdvance(
        key=key,
        tick_ts=tick,
        status=status,
        baseline_window_count=state.baseline_window_count,
        baseline_rates=baseline_rates,
        window_record_count=len(records),
        ambiguous_observation_ids=ambiguous_ids,
        frequencies=frequencies,
        symptoms=(),
        selected_symptom=None,
        transition=None,
    )


def _baseline_rates(state: _LogServiceState) -> tuple[tuple[str, float], ...]:
    if state.baseline_window_count == 0:
        return ()
    return tuple(
        (template_id, total / state.baseline_window_count)
        for template_id, total in sorted(state.baseline_rate_totals.items())
    )


def _select_symptom(
    evaluation: LogDetectionResult,
    *,
    baseline_rates: tuple[tuple[str, float], ...],
    baseline_floor: float,
) -> Symptom | None:
    if not evaluation.symptoms:
        return None
    baselines = dict(baseline_rates)
    strength_by_refs = {
        frequency.evidence_refs: max(
            frequency.messages_per_second - baselines.get(frequency.template_id, 0.0),
            0.0,
        )
        / max(baselines.get(frequency.template_id, 0.0), baseline_floor)
        for frequency in evaluation.frequencies
    }
    return min(
        evaluation.symptoms,
        key=lambda item: (
            -strength_by_refs[item.evidence_refs],
            -item.score,
            item.onset_ts,
            item.symptom_id,
        ),
    )


def _log_line(observation: Observation, *, logical_service: str) -> LogLine | None:
    body = observation.attributes.get("log.body")
    if (
        not isinstance(body, str)
        or not body.strip()
        or len(observation.log_refs) != 1
        or observation.value != 1.0
    ):
        return None
    try:
        return LogLine(
            log_id=observation.log_refs[0],
            ts=observation.ts,
            service=logical_service,
            message=body,
        )
    except (TypeError, ValueError):
        return None


def _episode_key(service: str) -> EpisodeKey:
    return EpisodeKey(SymptomKind.LOG_BURST, service, "log_template_rate")


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


def canonical_log_advances(values: tuple[LogWindowAdvance, ...]) -> bytes:
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
