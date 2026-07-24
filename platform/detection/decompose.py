"""Deterministic baseline + event + residual decomposition."""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Literal, Protocol

from common.config import DetectorConfig
from contracts import ContextWindow, DecompFrame, Observation
from ml.features import ActiveEvent, aware_features

type StreamKey = tuple[str, str]
type DecompositionStatus = Literal["warming", "context_blocked_warmup", "decomposed"]


class DecompositionBand(Protocol):
    """The learned band a decomposition envelope returns for one tick."""

    @property
    def lower(self) -> float: ...

    @property
    def upper(self) -> float: ...

    @property
    def expected(self) -> float: ...


class DecompositionEnvelope(Protocol):
    """A trained model that predicts a learned band for a ``service.signal`` key.

    Structural so the deterministic detection plane never hard-imports the ML
    stack: any object with this ``predict`` (e.g. ``ml.envelopes.EnvelopeModel``)
    can be supplied. ``predict`` returns None when no envelope covers the signal.
    """

    def predict(
        self, signal_key: str, features: Mapping[str, float]
    ) -> DecompositionBand | None: ...


class UnconfiguredSignalError(ValueError):
    """A detector was asked to invent policy for a signal absent from config."""


@dataclass(frozen=True)
class DecompositionResult:
    status: DecompositionStatus
    frame: DecompFrame | None
    baseline_updated: bool
    replayed: bool = False


@dataclass(frozen=True)
class DecompositionStats:
    warming: int
    context_blocked_warmup: int
    decomposed: int
    baseline_updates: int
    replays: int


@dataclass
class _BaselineState:
    warmup: list[float] = field(default_factory=list)
    baseline: float | None = None


class DecompositionSink(Protocol):
    async def write_decomp_frames(self, records: Sequence[DecompFrame]) -> None: ...


class DecompositionEngine:
    """Maintain context-blind EWMA baselines and emit exact decomposition frames."""

    def __init__(
        self,
        *,
        configuration: DetectorConfig,
        dedup_capacity: int,
        envelope: DecompositionEnvelope | None = None,
    ) -> None:
        if dedup_capacity < 1:
            raise ValueError("dedup_capacity must be positive")
        self._configuration = configuration
        self._dedup_capacity = dedup_capacity
        self._envelope = envelope
        self._states: dict[StreamKey, _BaselineState] = {}
        self._results: OrderedDict[str, DecompositionResult] = OrderedDict()
        self._warming = 0
        self._context_blocked = 0
        self._decomposed = 0
        self._baseline_updates = 0
        self._replays = 0

    def decompose(
        self,
        observation: Observation,
        *,
        contexts: tuple[ContextWindow, ...] = (),
    ) -> DecompositionResult:
        """Decompose one ordered observation using state strictly before this tick."""
        cached = self._results.get(observation.observation_id)
        if cached is not None:
            self._results.move_to_end(observation.observation_id)
            self._replays += 1
            return replace(cached, replayed=True)

        signal_key = self._signal_key(observation.service, observation.signal)
        floor = self._configuration.absolute_noise_floors[signal_key]
        lifts = self._active_lifts(observation, signal_key, contexts)

        # A registered envelope replaces the EWMA baseline for its signals; every
        # other signal (and the no-model case) falls through to the deterministic
        # path below unchanged, so the fallback is byte-identical.
        if self._envelope is not None:
            envelope_result = self._envelope_decompose(observation, signal_key, floor, lifts)
            if envelope_result is not None:
                self._remember(observation.observation_id, envelope_result)
                return envelope_result

        stream = (observation.service, observation.signal)
        state = self._states.setdefault(stream, _BaselineState())

        if state.baseline is None:
            if lifts:
                self._context_blocked += 1
                result = DecompositionResult(
                    status="context_blocked_warmup",
                    frame=None,
                    baseline_updated=False,
                )
            else:
                state.warmup.append(observation.value)
                if len(state.warmup) >= self._configuration.baseline_warmup_points:
                    state.baseline = float(median(state.warmup))
                    state.warmup.clear()
                self._warming += 1
                result = DecompositionResult(
                    status="warming",
                    frame=None,
                    baseline_updated=state.baseline is not None,
                )
            self._remember(observation.observation_id, result)
            return result

        baseline = state.baseline
        contributions = [
            baseline * (multiplier - 1.0) * context.trust_score for context, multiplier in lifts
        ]
        explained_event = math.fsum(sorted(contributions))
        expected = baseline + explained_event
        tolerance = max(
            floor,
            abs(expected) * self._configuration.expected_band_relative_tolerance,
        )
        band_low = expected - tolerance
        band_high = expected + tolerance
        residual = observation.value - expected
        excess = max(band_low - observation.value, observation.value - band_high, 0.0)
        residual_score = 0.0 if excess == 0.0 else min(excess / max(tolerance, 1e-12), 1.0)
        context_ids = tuple(sorted(context.context_id for context, _ in lifts))
        frame_identity = {
            "observation_id": observation.observation_id,
            "ts": observation.ts.isoformat(),
            "service": observation.service,
            "signal": observation.signal,
            "observed": observation.value,
            "explained_base": baseline,
            "explained_event": explained_event,
            "residual": residual,
            "band_low": band_low,
            "band_high": band_high,
            "residual_score": residual_score,
            "context_ids": context_ids,
        }
        frame = DecompFrame(
            frame_id=_digest(frame_identity),
            observation_id=observation.observation_id,
            ts=observation.ts,
            service=observation.service,
            signal=observation.signal,
            observed=observation.value,
            explained_base=baseline,
            explained_event=explained_event,
            residual=residual,
            band_low=band_low,
            band_high=band_high,
            residual_score=residual_score,
            context_ids=context_ids,
        )

        update_gate = max(
            floor,
            abs(baseline) * self._configuration.baseline_update_gate_ratio,
        )
        baseline_updated = abs(observation.value - baseline) <= update_gate
        if baseline_updated:
            alpha = self._configuration.ewma_alpha
            state.baseline = alpha * observation.value + (1.0 - alpha) * baseline
            self._baseline_updates += 1

        self._decomposed += 1
        result = DecompositionResult(
            status="decomposed",
            frame=frame,
            baseline_updated=baseline_updated,
        )
        self._remember(observation.observation_id, result)
        return result

    def _envelope_decompose(
        self,
        observation: Observation,
        signal_key: str,
        floor: float,
        lifts: tuple[tuple[ContextWindow, float], ...],
    ) -> DecompositionResult | None:
        """Decompose one tick against a learned band; None if no envelope covers it."""
        assert self._envelope is not None
        active = tuple(
            ActiveEvent(multiplier=multiplier, trust_score=context.trust_score)
            for context, multiplier in lifts
        )
        band = self._envelope.predict(signal_key, aware_features(observation.ts, active))
        if band is None:
            return None

        # The event component is the aware model's own marginal effect: the median
        # it predicts now, minus what it would predict with the events turned off.
        baseline_band = self._envelope.predict(signal_key, aware_features(observation.ts, ()))
        expected = band.expected
        base_expected = expected if baseline_band is None else baseline_band.expected
        explained_event = max(expected - base_expected, 0.0)
        explained_base = expected - explained_event

        band_low = band.lower
        band_high = band.upper
        residual = observation.value - expected
        excess = max(band_low - observation.value, observation.value - band_high, 0.0)
        scale = max(floor, (band_high - band_low) / 2.0)
        residual_score = 0.0 if excess == 0.0 else min(excess / max(scale, 1e-12), 1.0)
        context_ids = tuple(sorted(context.context_id for context, _ in lifts))
        frame_identity = {
            "observation_id": observation.observation_id,
            "ts": observation.ts.isoformat(),
            "service": observation.service,
            "signal": observation.signal,
            "observed": observation.value,
            "explained_base": explained_base,
            "explained_event": explained_event,
            "residual": residual,
            "band_low": band_low,
            "band_high": band_high,
            "residual_score": residual_score,
            "context_ids": context_ids,
            "source": "envelope",
        }
        frame = DecompFrame(
            frame_id=_digest(frame_identity),
            observation_id=observation.observation_id,
            ts=observation.ts,
            service=observation.service,
            signal=observation.signal,
            observed=observation.value,
            explained_base=explained_base,
            explained_event=explained_event,
            residual=residual,
            band_low=band_low,
            band_high=band_high,
            residual_score=residual_score,
            context_ids=context_ids,
        )
        self._decomposed += 1
        return DecompositionResult(status="decomposed", frame=frame, baseline_updated=False)

    def baseline_for(self, *, service: str, signal: str) -> float | None:
        """Inspect one stream's learned baseline without mutating it."""
        self._signal_key(service, signal)
        state = self._states.get((service, signal))
        return None if state is None else state.baseline

    def stats(self) -> DecompositionStats:
        return DecompositionStats(
            warming=self._warming,
            context_blocked_warmup=self._context_blocked,
            decomposed=self._decomposed,
            baseline_updates=self._baseline_updates,
            replays=self._replays,
        )

    def _signal_key(self, service: str, signal: str) -> str:
        key = signal if signal.startswith(f"{service}.") else f"{service}.{signal}"
        if key not in self._configuration.absolute_noise_floors:
            raise UnconfiguredSignalError(f"signal has no configured noise floor: {key}")
        return key

    @staticmethod
    def _active_lifts(
        observation: Observation,
        signal_key: str,
        contexts: tuple[ContextWindow, ...],
    ) -> tuple[tuple[ContextWindow, float], ...]:
        return tuple(
            sorted(
                (
                    (context, multiplier)
                    for context in contexts
                    if context.valid_from <= observation.ts < context.valid_to
                    and (multiplier := context.expected_delta.get(signal_key)) is not None
                    and multiplier > 1.0
                    and context.trust_score > 0.0
                ),
                key=lambda item: item[0].context_id,
            )
        )

    def _remember(self, observation_id: str, result: DecompositionResult) -> None:
        self._results[observation_id] = result
        self._results.move_to_end(observation_id)
        while len(self._results) > self._dedup_capacity:
            self._results.popitem(last=False)


class DecompositionWorker:
    """Persist every post-warmup frame without downsampling."""

    def __init__(self, *, engine: DecompositionEngine, sink: DecompositionSink) -> None:
        self._engine = engine
        self._sink = sink

    async def handle(
        self,
        observation: Observation,
        *,
        contexts: tuple[ContextWindow, ...] = (),
    ) -> DecompositionResult:
        result = self._engine.decompose(observation, contexts=contexts)
        if result.frame is not None:
            await self._sink.write_decomp_frames((result.frame,))
        return result


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
