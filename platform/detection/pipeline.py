"""Bridge decomposition frames to anti-flapping episodes, end to end.

Decomposition (`detection/decompose.py`) turns each observation into a
`DecompFrame` carrying a bounded `residual_score`. An upside band breach — the
observed value above the context-aware expected band — is the first deterministic
symptom, `RESIDUAL_EXCEED`. This module maps those frames into symptoms, drives
them through the 2.7 `SymptomEpisodeMachine`, and persists the resulting
episodes. Every frame produces exactly one episode tick for its
`(RESIDUAL_EXCEED, service, signal)` key — a breach when the band is exceeded, a
clear otherwise — so episodes open and close purely from the frame stream with
no separate "quiet tick" bookkeeping.

Downside breaches (a volume drop below the band) are deliberately *not* emitted
here: they are the `DROP`/`SILENCE` liveness detector's concern, wired
separately, so a drop can never sustain a `RESIDUAL_EXCEED` episode.
"""

from __future__ import annotations

import hashlib
import json
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


class ResidualEpisodePipeline:
    """Drive residual band breaches through per-key anti-flapping episodes."""

    def __init__(self, *, configuration: EpisodeConfig) -> None:
        self._machine = SymptomEpisodeMachine(configuration=configuration)

    def observe(self, frame: DecompFrame) -> EpisodeTransition:
        """Turn one decomposition frame into a single residual episode tick."""
        symptom = residual_symptom(frame)
        key = EpisodeKey(SymptomKind.RESIDUAL_EXCEED, frame.service, frame.signal)
        return self._machine.observe(key=key, tick_ts=frame.ts, symptom=symptom)

    def active_episodes(self) -> tuple[SymptomEpisode, ...]:
        """Every currently open residual episode, ordered by stable id."""
        return self._machine.active_episodes()


class EpisodePersister(Protocol):
    async def put_episode(self, episode: SymptomEpisode) -> bool: ...


class ResidualEpisodeWorker:
    """Persist an episode whenever a frame changes its durable record."""

    def __init__(self, *, pipeline: ResidualEpisodePipeline, sink: EpisodePersister) -> None:
        self._pipeline = pipeline
        self._sink = sink

    async def handle(self, frame: DecompFrame) -> EpisodeTransition:
        """Route one frame and persist the episode only when it changed."""
        transition = self._pipeline.observe(frame)
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
