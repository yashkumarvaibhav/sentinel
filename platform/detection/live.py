"""Drive every deterministic detector path from one live event-time tick.

A capture replay walks each detector over a recorded window on its own; a live
run has to advance all of them together, in event time, from the same stream of
normalized observations. This module is that fan-out and nothing more: it makes
no judgement, opens no incident and reads no label.

Two statements of coverage are made here, and both are facts rather than
assumptions:

* **Kinds** come from the processors that are actually running — each runner
  reports the episode identities it monitors, and only those kinds are claimed.
  A kind nobody is watching must stay insufficient downstream instead of being
  read as calm.
* **Services** come from telemetry that actually arrived, under both the name
  it arrived as and the name the detectors judge it by. A service is not
  covered because configuration mentions it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from common.config import DetectorConfig
from contracts import ContextWindow, DecompFrame, Observation, SymptomEpisode, SymptomKind
from detection.decompose import DecompositionEngine, DecompositionEnvelope
from detection.episodes import EpisodeKey, EpisodeTransition
from detection.liveness_runner import LivenessDetectionRunner
from detection.log_runner import LogDetectionRunner
from detection.pipeline import SymptomEpisodePipeline
from detection.rate import IngressRateReconstructor
from detection.ratio_runner import IngressRatioDetectionRunner
from detection.resource_runner import ResourceDetectionRunner
from detection.runner import EdgeDetectionRunner

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# The reconstructed rate stream is decomposed against its expected band, so the
# residual path is running exactly when that stream is being produced.
RESIDUAL_PATH = "rate"


class MonitoredProcessor(Protocol):
    """A detector path that can state which episode identities it watches."""

    @property
    def advance_seconds(self) -> int: ...

    @property
    def monitored_keys(self) -> tuple[EpisodeKey, ...]: ...


@dataclass(frozen=True, slots=True)
class LiveTick:
    """What one complete event-time tick measured, and what it could measure."""

    ts: datetime
    episodes: tuple[SymptomEpisode, ...]
    frames: tuple[DecompFrame, ...]
    advanced_processors: frozenset[str]
    covered_kinds: frozenset[SymptomKind]
    covered_services: frozenset[str]


def base_tick_seconds(detector: DetectorConfig) -> int:
    """The shortest complete tick every configured detector path divides into."""
    return _base_tick(
        (
            detector.ingress_rate.tick_seconds,
            detector.liveness.window_seconds,
            detector.edge_degradation.advance_seconds,
            detector.log_templates.window_seconds,
            detector.behavioral_ratios.ingress_windows.window_seconds,
            detector.change_point_saturation.resource_windows.advance_seconds,
        )
    )


def floor_to_tick(value: datetime, *, tick_seconds: int) -> datetime:
    """Round an arrival clock down to the tick boundary it falls inside."""
    moment = _utc(value, name="value")
    elapsed = (moment - _EPOCH).total_seconds()
    return _EPOCH + timedelta(seconds=math.floor(elapsed / tick_seconds) * tick_seconds)


def covered_symptom_kinds(processors: Sequence[MonitoredProcessor]) -> frozenset[SymptomKind]:
    """The kinds these processors watch — an empty set when none of them run."""
    return frozenset(key.kind for processor in processors for key in processor.monitored_keys)


class LiveDetectionProcessors:
    """Advance every configured detector path on one shared event-time tick."""

    def __init__(
        self,
        *,
        detector: DetectorConfig,
        anchor_ts: datetime,
        envelope: DecompositionEnvelope | None = None,
    ) -> None:
        self._detector = detector
        self._anchor = _utc(anchor_ts, name="anchor_ts")
        self._rate = IngressRateReconstructor(configuration=detector.ingress_rate)
        self._engine = DecompositionEngine(
            configuration=detector,
            dedup_capacity=detector.liveness.dedup_capacity,
            envelope=envelope,
        )
        self._residual = SymptomEpisodePipeline(configuration=detector.episodes)
        self._liveness = LivenessDetectionRunner(
            configuration=detector.liveness,
            episodes=detector.episodes,
            expected_since_ts=self._anchor,
        )
        self._edge = EdgeDetectionRunner(
            configuration=detector.edge_degradation,
            episodes=detector.episodes,
        )
        self._log = LogDetectionRunner(
            configuration=detector.log_templates,
            episodes=detector.episodes,
        )
        self._ratio = IngressRatioDetectionRunner(
            configuration=detector.behavioral_ratios,
            episodes=detector.episodes,
        )
        self._resource = ResourceDetectionRunner(
            configuration=detector.change_point_saturation,
            episodes=detector.episodes,
        )
        self._window_runners: dict[str, MonitoredProcessor] = {
            "edge": self._edge,
            "log": self._log,
            "ratio": self._ratio,
            "resource": self._resource,
        }
        self._base_tick_seconds = _base_tick(
            (
                self._rate.advance_seconds,
                self._liveness.advance_seconds,
                *(runner.advance_seconds for runner in self._window_runners.values()),
            )
        )
        self._service_mappings = {
            **detector.behavioral_ratios.ingress_windows.service_mappings,
            **detector.log_templates.service_mappings,
        }
        self._covered_services: set[str] = set()
        self._last_tick: datetime | None = None
        self._last_result: LiveTick | None = None

    @property
    def anchor_ts(self) -> datetime:
        return self._anchor

    @property
    def base_tick_seconds(self) -> int:
        """The shortest complete tick every configured processor divides into."""
        return self._base_tick_seconds

    @property
    def monitored_processors(self) -> tuple[MonitoredProcessor, ...]:
        return (self._liveness, *self._window_runners.values())

    @property
    def covered_kinds(self) -> frozenset[SymptomKind]:
        """Only the kinds a running processor is watching for."""
        kinds = covered_symptom_kinds(self.monitored_processors)
        if self._rate.streams:
            kinds = kinds | {SymptomKind.RESIDUAL_EXCEED}
        return kinds

    @property
    def covered_services(self) -> frozenset[str]:
        """Services whose telemetry has actually arrived, under every name they use."""
        return frozenset(self._covered_services)

    def active_episodes(self) -> tuple[SymptomEpisode, ...]:
        """Every episode currently open across all paths, in stable order."""
        return tuple(
            sorted(
                (
                    *self._residual.active_episodes(),
                    *self._liveness.active_episodes(),
                    *self._edge.active_episodes(),
                    *self._log.active_episodes(),
                    *self._ratio.active_episodes(),
                    *self._resource.active_episodes(),
                ),
                key=lambda episode: (episode.opened_ts, episode.episode_id),
            )
        )

    def advance(
        self,
        *,
        observations: tuple[Observation, ...],
        tick_ts: datetime,
        contexts: tuple[ContextWindow, ...] = (),
    ) -> LiveTick:
        """Consume everything measured in ``(tick_ts - base, tick_ts]`` exactly once."""
        if not isinstance(observations, tuple):
            raise TypeError("observations must be a tuple")
        if any(not isinstance(item, Observation) for item in observations):
            raise TypeError("observations must contain only Observation values")
        tick = self._validate_tick(tick_ts)
        if self._last_result is not None and tick == self._last_tick:
            return self._last_result

        for observation in observations:
            self._covered_services.add(observation.service)
            mapped = self._service_mappings.get(observation.service)
            if mapped is not None:
                self._covered_services.add(mapped)

        advanced = {RESIDUAL_PATH, "liveness"}
        episodes: list[SymptomEpisode] = []
        frames = self._decompose(observations, tick=tick, contexts=contexts, episodes=episodes)
        # Liveness judges the window the rate reconstruction just closed, which
        # is the one starting a base tick back.
        for advance in self._liveness.advance(
            frames=frames,
            tick_ts=tick - timedelta(seconds=self._base_tick_seconds),
        ):
            _collect(advance.transition, episodes)

        elapsed = int((tick - self._anchor).total_seconds())
        for name, runner in self._window_runners.items():
            if elapsed % runner.advance_seconds:
                continue
            advanced.add(name)
            for transition in self._advance_window(name, observations=observations, tick=tick):
                _collect(transition, episodes)

        measured = LiveTick(
            ts=tick,
            episodes=tuple(episodes),
            frames=frames,
            advanced_processors=frozenset(advanced),
            covered_kinds=self.covered_kinds,
            covered_services=self.covered_services,
        )
        self._last_tick = tick
        self._last_result = measured
        return measured

    def _validate_tick(self, tick_ts: datetime) -> datetime:
        tick = _utc(tick_ts, name="tick_ts")
        elapsed = (tick - self._anchor).total_seconds()
        if elapsed <= 0:
            raise ValueError("a live tick must follow the anchor it is measured from")
        if not elapsed.is_integer() or int(elapsed) % self._base_tick_seconds:
            raise ValueError("live ticks must align to complete base ticks")
        if self._last_tick is not None:
            if tick < self._last_tick:
                raise ValueError("live ticks must arrive in event-time order")
            expected = self._last_tick + timedelta(seconds=self._base_tick_seconds)
            if tick > expected:
                raise ValueError("live ticks must be complete and consecutive")
        return tick

    def _decompose(
        self,
        observations: tuple[Observation, ...],
        *,
        tick: datetime,
        contexts: tuple[ContextWindow, ...],
        episodes: list[SymptomEpisode],
    ) -> tuple[DecompFrame, ...]:
        frames: list[DecompFrame] = []
        for rate in self._rate.advance(observations=observations, tick_ts=tick):
            result = self._engine.decompose(rate, contexts=contexts)
            if result.frame is None:
                continue
            frames.append(result.frame)
            _collect(self._residual.observe_frame(result.frame), episodes)
        return tuple(frames)

    def _advance_window(
        self,
        name: str,
        *,
        observations: tuple[Observation, ...],
        tick: datetime,
    ) -> tuple[EpisodeTransition | None, ...]:
        if name == "edge":
            return tuple(
                item.transition
                for item in self._edge.advance(observations=observations, tick_ts=tick)
            )
        if name == "log":
            return tuple(
                item.transition
                for item in self._log.advance(observations=observations, tick_ts=tick)
            )
        if name == "ratio":
            return tuple(
                item.transition
                for item in self._ratio.advance(observations=observations, tick_ts=tick)
            )
        return tuple(
            item.transition
            for item in self._resource.advance(observations=observations, tick_ts=tick)
        )


def _collect(transition: EpisodeTransition | None, episodes: list[SymptomEpisode]) -> None:
    if transition is not None and transition.episode is not None:
        episodes.append(transition.episode)


def _base_tick(periods: tuple[int, ...]) -> int:
    base = 0
    for period in periods:
        if period < 1:
            raise ValueError("every detector path must advance on a positive period")
        base = math.gcd(base, period)
    return base


def _utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)
