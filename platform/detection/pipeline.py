"""Route every deterministic detector's symptoms into anti-flapping episodes.

Detectors (2.1 through 2.6) each emit a scored `Symptom` per window; decomposition emits
a `DecompFrame` whose upside band breach becomes a `RESIDUAL_EXCEED` symptom. All
of them converge here: a symptom is routed to the 2.7 `SymptomEpisodeMachine`
under its `(kind, service, signal)` key, so a momentary detection can never open
an incident precursor and a key can never flap. The resulting episodes persist
through any `EpisodePersister` — `PostgresRepository.put_episode` satisfies it —
and only when a tick actually changed the durable record.

The router itself is telemetry-agnostic: a caller ticks it with the symptom a
detector produced (a breach) or with `None` for a monitored key that stayed quiet
that window (a clear), which is how episodes close. The residual path is the one
built-in adapter because decomposition already produces one frame per tick.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Protocol

from common.config import EpisodeConfig
from contracts import DecompFrame, Symptom, SymptomEpisode, SymptomKind
from detection.episodes import EpisodeKey, EpisodeTransition, SymptomEpisodeMachine


def residual_symptom(frame: DecompFrame) -> Symptom | None:
    """Emit a `RESIDUAL_EXCEED` symptom for an upside band breach, else nothing."""
    if frame.residual_score <= 0.0 or frame.residual <= 0.0:
        return None
    identity = {
        "kind": SymptomKind.RESIDUAL_EXCEED.value,
        "frame_id": frame.frame_id,
    }
    note = (
        f"residual exceeded the context-aware band: observed={_render(frame.observed)}, "
        f"band=[{_render(frame.band_low)}, {_render(frame.band_high)}], "
        f"explained_base={_render(frame.explained_base)}, "
        f"explained_event={_render(frame.explained_event)}, "
        f"residual={_render(frame.residual)}, residual_score={_render(frame.residual_score)}."
    )
    return Symptom(
        symptom_id=_digest(identity),
        kind=SymptomKind.RESIDUAL_EXCEED,
        service=frame.service,
        signal=frame.signal,
        onset_ts=frame.ts,
        score=frame.residual_score,
        note=note,
        evidence_refs=(frame.frame_id,),
    )


class SymptomEpisodePipeline:
    """Drive any detector's symptoms through per-key anti-flapping episodes."""

    def __init__(self, *, configuration: EpisodeConfig) -> None:
        self._machine = SymptomEpisodeMachine(configuration=configuration)

    def observe(
        self, *, key: EpisodeKey, tick_ts: datetime, symptom: Symptom | None
    ) -> EpisodeTransition:
        """Advance one key by a single window tick (a symptom or a clear)."""
        return self._machine.observe(key=key, tick_ts=tick_ts, symptom=symptom)

    def observe_symptom(self, symptom: Symptom, *, tick_ts: datetime) -> EpisodeTransition:
        """Route a detector's symptom under its own `(kind, service, signal)` key."""
        return self.observe(key=EpisodeKey.from_symptom(symptom), tick_ts=tick_ts, symptom=symptom)

    def hold(self, *, key: EpisodeKey, tick_ts: datetime) -> EpisodeTransition:
        """Record an insufficient tick without opening or clearing an episode."""
        return self._machine.hold(key=key, tick_ts=tick_ts)

    def observe_frame(self, frame: DecompFrame) -> EpisodeTransition:
        """Turn one decomposition frame into a single residual episode tick."""
        symptom = residual_symptom(frame)
        key = EpisodeKey(SymptomKind.RESIDUAL_EXCEED, frame.service, frame.signal)
        return self.observe(key=key, tick_ts=frame.ts, symptom=symptom)

    def active_episodes(self) -> tuple[SymptomEpisode, ...]:
        """Every currently open episode, ordered by stable id."""
        return self._machine.active_episodes()


class EpisodePersister(Protocol):
    async def put_episode(self, episode: SymptomEpisode) -> bool: ...


class EpisodeWorker:
    """Persist an episode whenever a tick changed its durable record."""

    def __init__(self, *, pipeline: SymptomEpisodePipeline, sink: EpisodePersister) -> None:
        self._pipeline = pipeline
        self._sink = sink

    async def handle(
        self, *, key: EpisodeKey, tick_ts: datetime, symptom: Symptom | None
    ) -> EpisodeTransition:
        """Route one raw tick and persist the episode only when it changed."""
        return await self._persist(
            self._pipeline.observe(key=key, tick_ts=tick_ts, symptom=symptom)
        )

    async def handle_symptom(self, symptom: Symptom, *, tick_ts: datetime) -> EpisodeTransition:
        """Route one detector symptom and persist the episode only when it changed."""
        return await self._persist(self._pipeline.observe_symptom(symptom, tick_ts=tick_ts))

    async def handle_frame(self, frame: DecompFrame) -> EpisodeTransition:
        """Route one decomposition frame and persist the episode only when it changed."""
        return await self._persist(self._pipeline.observe_frame(frame))

    async def _persist(self, transition: EpisodeTransition) -> EpisodeTransition:
        if transition.persist and transition.episode is not None:
            await self._sink.put_episode(transition.episode)
        return transition


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _render(value: float) -> str:
    return format(value, ".12g")
