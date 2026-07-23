"""Route decomposed rate frames through drop and silence episode lifecycles.

The runner consumes only deterministic ``DecompFrame`` evidence. A measured
frame may prove a drop or clear; its absence never becomes a synthetic zero and
can only advance the separate event-time silence path. Ambiguous or malformed
evidence advances neither path.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from common.config import EpisodeConfig, LivenessConfig
from contracts import DecompFrame, Symptom, SymptomEpisode, SymptomKind
from detection.episodes import EpisodeKey, EpisodeTransition
from detection.liveness import DropEvaluation, LivenessDetector, SilenceEvaluation
from detection.pipeline import SymptomEpisodePipeline


class LivenessWindowStatus(StrEnum):
    """Whether one liveness path had enough evidence and breached."""

    INSUFFICIENT = "INSUFFICIENT"
    CLEAR = "CLEAR"
    BREACH = "BREACH"


@dataclass(frozen=True, slots=True)
class LivenessWindowAdvance:
    """Auditable outcome for one stream and one liveness symptom kind."""

    key: EpisodeKey
    tick_ts: datetime
    status: LivenessWindowStatus
    frame_id: str | None
    expected_since_ts: datetime
    last_seen_ts: datetime | None
    ambiguous_frame_ids: tuple[str, ...]
    evaluation: DropEvaluation | SilenceEvaluation | None
    transition: EpisodeTransition | None

    def canonical_value(self) -> dict[str, object]:
        """Stable JSON-ready form for capture transcript hashing."""
        evaluation: dict[str, object] | None
        if isinstance(self.evaluation, DropEvaluation):
            evaluation = {
                "expected_value": self.evaluation.expected_value,
                "observed_value": self.evaluation.observed_value,
                "relative_drop": self.evaluation.relative_drop,
                "symptom": _canonical_symptom(self.evaluation.symptom),
                "type": "drop",
            }
        elif isinstance(self.evaluation, SilenceEvaluation):
            evaluation = {
                "reference_ts": (
                    None
                    if self.evaluation.reference_ts is None
                    else self.evaluation.reference_ts.isoformat()
                ),
                "stale_age_seconds": self.evaluation.stale_age_seconds,
                "symptom": _canonical_symptom(self.evaluation.symptom),
                "type": "silence",
            }
        else:
            evaluation = None
        return {
            "ambiguous_frame_ids": self.ambiguous_frame_ids,
            "evaluation": evaluation,
            "expected_since_ts": self.expected_since_ts.isoformat(),
            "frame_id": self.frame_id,
            "key": {
                "kind": self.key.kind.value,
                "service": self.key.service,
                "signal": self.key.signal,
            },
            "last_seen_ts": (None if self.last_seen_ts is None else self.last_seen_ts.isoformat()),
            "status": self.status.value,
            "tick_ts": self.tick_ts.isoformat(),
            "transition": _canonical_transition(self.transition),
        }


@dataclass(slots=True)
class _StreamState:
    expected_since_ts: datetime
    last_seen_ts: datetime | None = None
    last_frame_id: str | None = None


class LivenessDetectionRunner:
    """Advance configured decomposed streams through drop/silence episodes."""

    def __init__(
        self,
        *,
        configuration: LivenessConfig,
        episodes: EpisodeConfig,
        expected_since_ts: datetime,
    ) -> None:
        self._configuration = configuration
        self._expected_since = _utc(expected_since_ts, name="expected_since_ts")
        self._streams = tuple(sorted((item.service, item.signal) for item in configuration.streams))
        self._states = {
            stream: _StreamState(expected_since_ts=self._expected_since) for stream in self._streams
        }
        self._detector = LivenessDetector(configuration=configuration)
        self._pipeline = SymptomEpisodePipeline(configuration=episodes)
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._last_tick: datetime | None = None
        self._last_batch_signature: tuple[tuple[str, str], ...] | None = None
        self._last_results: tuple[LivenessWindowAdvance, ...] | None = None

    @property
    def advance_seconds(self) -> int:
        return self._configuration.window_seconds

    @property
    def monitored_keys(self) -> tuple[EpisodeKey, ...]:
        """Configured stream/kind episode identities in deterministic order."""
        return tuple(
            EpisodeKey(kind, service, signal)
            for service, signal in self._streams
            for kind in self._configured_kinds(service, signal)
        )

    def active_episodes(self) -> tuple[SymptomEpisode, ...]:
        return self._pipeline.active_episodes()

    def advance(
        self,
        *,
        frames: tuple[DecompFrame, ...],
        tick_ts: datetime,
    ) -> tuple[LivenessWindowAdvance, ...]:
        """Consume one exact event-time tick without inventing absent measurements."""
        if not isinstance(frames, tuple):
            raise TypeError("frames must be a tuple")
        if any(not isinstance(item, DecompFrame) for item in frames):
            raise TypeError("frames must contain only DecompFrame values")
        tick = _utc(tick_ts, name="tick_ts")
        if tick < self._expected_since:
            raise ValueError("liveness tick cannot precede expected_since_ts")
        elapsed = (tick - self._expected_since).total_seconds()
        if not elapsed.is_integer() or int(elapsed) % self.advance_seconds != 0:
            raise ValueError("liveness ticks must align to complete configured ticks")
        ordered = tuple(sorted(frames, key=lambda item: (item.ts, item.frame_id)))
        signature = tuple((item.frame_id, _frame_fingerprint(item)) for item in ordered)
        if self._last_tick is not None:
            if tick < self._last_tick:
                raise ValueError("liveness ticks must arrive in event-time order")
            if tick == self._last_tick:
                if signature == self._last_batch_signature and self._last_results is not None:
                    return self._last_results
                raise ValueError("conflicting liveness advance at the same event time")
            expected = self._last_tick + timedelta(seconds=self.advance_seconds)
            if tick != expected:
                raise ValueError("liveness runner requires complete configured ticks")

        by_stream: dict[tuple[str, str], list[DecompFrame]] = {
            stream: [] for stream in self._streams
        }
        for frame in ordered:
            stream = (frame.service, frame.signal)
            if stream not in by_stream:
                continue
            fingerprint = _frame_fingerprint(frame)
            previous = self._seen.get(frame.frame_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ValueError("frame_id was reused with different liveness evidence")
                self._seen.move_to_end(frame.frame_id)
                continue
            if frame.ts != tick:
                raise ValueError("liveness frame timestamp must equal its runner tick")
            self._remember(frame.frame_id, fingerprint)
            by_stream[stream].append(frame)

        results = tuple(
            advance
            for stream in self._streams
            for advance in self._advance_stream(
                stream,
                frames=tuple(by_stream[stream]),
                tick=tick,
            )
        )
        self._last_tick = tick
        self._last_batch_signature = signature
        self._last_results = results
        return results

    def _advance_stream(
        self,
        stream: tuple[str, str],
        *,
        frames: tuple[DecompFrame, ...],
        tick: datetime,
    ) -> tuple[LivenessWindowAdvance, ...]:
        service, signal = stream
        state = self._states[stream]
        ambiguous_ids = tuple(sorted(item.frame_id for item in frames)) if len(frames) > 1 else ()
        frame = frames[0] if len(frames) == 1 else None
        if frame is not None:
            expected = frame.explained_base + frame.explained_event
            if frame.observed < 0.0 or expected < 0.0 or not math.isfinite(expected):
                ambiguous_ids = (frame.frame_id,)
                frame = None

        if ambiguous_ids:
            return tuple(
                _insufficient_advance(
                    key=EpisodeKey(kind, service, signal),
                    tick=tick,
                    state=state,
                    ambiguous_ids=ambiguous_ids,
                    transition=self._pipeline.hold(
                        key=EpisodeKey(kind, service, signal),
                        tick_ts=tick,
                    ),
                )
                for kind in self._configured_kinds(service, signal)
            )

        if frame is not None:
            state.last_seen_ts = frame.ts
            state.last_frame_id = frame.frame_id

        advances: list[LivenessWindowAdvance] = []
        stream_key = f"{service}.{signal}"
        drop_rule = self._configuration.drop_rules.get(stream_key)
        if drop_rule is not None:
            if frame is None:
                key = EpisodeKey(SymptomKind.DROP, service, signal)
                advances.append(
                    _insufficient_advance(
                        key=key,
                        tick=tick,
                        state=state,
                        ambiguous_ids=(),
                        transition=self._pipeline.hold(key=key, tick_ts=tick),
                    )
                )
            else:
                expected = frame.explained_base + frame.explained_event
                drop_evaluation = self._detector.evaluate_drop(
                    service=service,
                    signal=signal,
                    onset_ts=frame.ts,
                    observed_value=frame.observed,
                    expected_value=expected,
                    evidence_refs=(frame.frame_id,),
                )
                sufficient = expected >= drop_rule.minimum_expected_value
                transition = None
                if sufficient:
                    transition = self._pipeline.observe(
                        key=EpisodeKey(SymptomKind.DROP, service, signal),
                        tick_ts=tick,
                        symptom=drop_evaluation.symptom,
                    )
                else:
                    transition = self._pipeline.hold(
                        key=EpisodeKey(SymptomKind.DROP, service, signal),
                        tick_ts=tick,
                    )
                advances.append(
                    LivenessWindowAdvance(
                        key=EpisodeKey(SymptomKind.DROP, service, signal),
                        tick_ts=tick,
                        status=(
                            LivenessWindowStatus.INSUFFICIENT
                            if not sufficient
                            else (
                                LivenessWindowStatus.BREACH
                                if drop_evaluation.symptom is not None
                                else LivenessWindowStatus.CLEAR
                            )
                        ),
                        frame_id=frame.frame_id,
                        expected_since_ts=state.expected_since_ts,
                        last_seen_ts=state.last_seen_ts,
                        ambiguous_frame_ids=(),
                        evaluation=drop_evaluation,
                        transition=transition,
                    )
                )

        if stream_key in self._configuration.silence_rules:
            refs = tuple(
                sorted(
                    {
                        _expectation_ref(stream_key),
                        *(() if state.last_frame_id is None else (state.last_frame_id,)),
                    }
                )
            )
            silence_evaluation = self._detector.evaluate_silence(
                service=service,
                signal=signal,
                expected_since_ts=state.expected_since_ts,
                last_seen_ts=state.last_seen_ts,
                watermark_ts=tick,
                evidence_refs=refs,
            )
            key = EpisodeKey(SymptomKind.SILENCE, service, signal)
            transition = self._pipeline.observe(
                key=key,
                tick_ts=tick,
                symptom=silence_evaluation.symptom,
            )
            advances.append(
                LivenessWindowAdvance(
                    key=key,
                    tick_ts=tick,
                    status=(
                        LivenessWindowStatus.BREACH
                        if silence_evaluation.symptom is not None
                        else LivenessWindowStatus.CLEAR
                    ),
                    frame_id=None if frame is None else frame.frame_id,
                    expected_since_ts=state.expected_since_ts,
                    last_seen_ts=state.last_seen_ts,
                    ambiguous_frame_ids=(),
                    evaluation=silence_evaluation,
                    transition=transition,
                )
            )
        return tuple(advances)

    def _configured_kinds(self, service: str, signal: str) -> tuple[SymptomKind, ...]:
        key = f"{service}.{signal}"
        return tuple(
            kind
            for kind, configured in (
                (SymptomKind.DROP, key in self._configuration.drop_rules),
                (SymptomKind.SILENCE, key in self._configuration.silence_rules),
            )
            if configured
        )

    def _remember(self, frame_id: str, fingerprint: str) -> None:
        self._seen[frame_id] = fingerprint
        self._seen.move_to_end(frame_id)
        while len(self._seen) > self._configuration.dedup_capacity:
            self._seen.popitem(last=False)


def _insufficient_advance(
    *,
    key: EpisodeKey,
    tick: datetime,
    state: _StreamState,
    ambiguous_ids: tuple[str, ...],
    transition: EpisodeTransition,
) -> LivenessWindowAdvance:
    return LivenessWindowAdvance(
        key=key,
        tick_ts=tick,
        status=LivenessWindowStatus.INSUFFICIENT,
        frame_id=None,
        expected_since_ts=state.expected_since_ts,
        last_seen_ts=state.last_seen_ts,
        ambiguous_frame_ids=ambiguous_ids,
        evaluation=None,
        transition=transition,
    )


def _frame_fingerprint(frame: DecompFrame) -> str:
    return json.dumps(
        frame.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_symptom(value: Symptom | None) -> dict[str, object] | None:
    return None if value is None else value.model_dump(mode="json")


def _expectation_ref(stream_key: str) -> str:
    digest = hashlib.sha256(stream_key.encode()).hexdigest()
    return f"liveness-expectation-{digest}"


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


def canonical_liveness_advances(values: tuple[LivenessWindowAdvance, ...]) -> bytes:
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
