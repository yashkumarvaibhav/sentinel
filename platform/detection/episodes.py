"""Deterministic anti-flapping episode lifecycle over repeated symptoms.

A detector emits a fresh :class:`~contracts.Symptom` every window (tick). Raw
symptoms flap: a borderline condition can appear and vanish tick to tick. An
*episode* is the durable, hysteresis-guarded lifecycle wrapped around the
recurring symptoms of one ``(kind, service, signal)`` key. It opens only after a
symptom persists and closes only after it clears, so a momentary blip can never
open one and a key can never oscillate between states.

Two independent anti-flap mechanisms combine:

* a **score deadband** — a tick counts as a *breach* only at/above
  ``breach_score`` and as a *clear* only at/below ``clear_score``; scores between
  the two (or a symptom absent entirely below breach) hold the current state;
* **persistence ticks** — an episode opens only after ``open_after_ticks``
  consecutive breaches and closes only after ``close_after_ticks`` consecutive
  clears.

The machine reads no wall clock: every timestamp comes from the caller-supplied
event-time tick. Ticks for a key must arrive in event-time order; an exact
redelivery of the most recent tick is an idempotent no-op, so at-least-once bus
delivery never advances state twice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from common.config import EpisodeConfig, EpisodePolicyConfig
from contracts import EpisodeStatus, Symptom, SymptomEpisode, SymptomKind


@dataclass(frozen=True, slots=True)
class EpisodeKey:
    """The identity an episode groups: one symptom kind on one service signal."""

    kind: SymptomKind
    service: str
    signal: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SymptomKind):
            raise TypeError("kind must be a SymptomKind")
        object.__setattr__(self, "service", _identifier(self.service, name="service"))
        object.__setattr__(self, "signal", _identifier(self.signal, name="signal"))

    @classmethod
    def from_symptom(cls, symptom: Symptom) -> EpisodeKey:
        """Derive the episode key a symptom belongs to."""
        return cls(kind=symptom.kind, service=symptom.service, signal=symptom.signal)


class EpisodePhase(StrEnum):
    """Internal lifecycle phase of one key's state machine."""

    IDLE = "IDLE"
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    CLEARING = "CLEARING"
    CLOSED = "CLOSED"


class EpisodeAction(StrEnum):
    """What a single tick did to a key's episode."""

    IGNORED = "IGNORED"
    PENDING = "PENDING"
    OPENED = "OPENED"
    SUSTAINED = "SUSTAINED"
    CLEARING = "CLEARING"
    HELD = "HELD"
    CLOSED = "CLOSED"


_PERSISTED_ACTIONS = frozenset(
    {EpisodeAction.OPENED, EpisodeAction.SUSTAINED, EpisodeAction.CLOSED}
)


@dataclass(frozen=True, slots=True)
class EpisodeTransition:
    """Auditable outcome of one observed tick."""

    key: EpisodeKey
    action: EpisodeAction
    phase: EpisodePhase
    episode: SymptomEpisode | None

    @property
    def persist(self) -> bool:
        """Whether this transition changed the durable episode record."""
        return self.action in _PERSISTED_ACTIONS


@dataclass(slots=True)
class _KeyState:
    """Mutable per-key accumulators; never leaves the machine."""

    phase: EpisodePhase = EpisodePhase.IDLE
    consecutive_breach: int = 0
    consecutive_clear: int = 0
    last_tick_ts: datetime | None = None
    last_signature: tuple[object, ...] | None = None
    last_transition: EpisodeTransition | None = None
    episode_id: str | None = None
    opened_ts: datetime | None = None
    confirmed_ts: datetime | None = None
    last_breach_ts: datetime | None = None
    closed_ts: datetime | None = None
    opening_symptom_id: str | None = None
    peak_symptom_id: str | None = None
    latest_symptom_id: str | None = None
    peak_refs: tuple[str, ...] = ()
    peak_score: float = 0.0
    breach_tick_count: int = 0
    revision: int = 0


class SymptomEpisodeMachine:
    """Turn a per-key stream of symptom ticks into anti-flapping episodes."""

    def __init__(self, *, configuration: EpisodeConfig) -> None:
        self._policies = dict(configuration.policies)
        self._states: dict[EpisodeKey, _KeyState] = {}

    def observe(
        self,
        *,
        key: EpisodeKey,
        tick_ts: datetime,
        symptom: Symptom | None,
    ) -> EpisodeTransition:
        """Advance one key's lifecycle by a single event-time tick."""
        if not isinstance(key, EpisodeKey):
            raise TypeError("key must be an EpisodeKey")
        tick = _utc(tick_ts, name="tick_ts")
        policy = self._policies.get(key.kind.value)
        if policy is None:
            return EpisodeTransition(key, EpisodeAction.IGNORED, EpisodePhase.IDLE, None)

        score = _tick_score(symptom, key)
        state = self._states.setdefault(key, _KeyState())
        signature: tuple[object, ...] = (
            tick.isoformat(),
            None if symptom is None else symptom.symptom_id,
            score,
        )
        if state.last_tick_ts is not None:
            if tick < state.last_tick_ts:
                raise ValueError("episode ticks must arrive in event-time order")
            if tick == state.last_tick_ts:
                if signature == state.last_signature and state.last_transition is not None:
                    return state.last_transition
                raise ValueError("conflicting episode tick at the same event time")

        if symptom is not None and score >= policy.breach_score:
            transition = self._on_breach(state, key, tick, symptom, score, policy)
        elif symptom is None or score <= policy.clear_score:
            transition = self._on_clear(state, key, tick, policy)
        else:
            transition = self._on_hold(state, key, tick)

        state.last_tick_ts = tick
        state.last_signature = signature
        state.last_transition = transition
        return transition

    def active_episodes(self) -> tuple[SymptomEpisode, ...]:
        """Every currently open episode, ordered by stable id."""
        episodes = [
            self._build_episode(state, key, status=EpisodeStatus.ACTIVE)
            for key, state in self._states.items()
            if state.phase in (EpisodePhase.ACTIVE, EpisodePhase.CLEARING)
        ]
        return tuple(sorted(episodes, key=lambda episode: episode.episode_id))

    def _on_breach(
        self,
        state: _KeyState,
        key: EpisodeKey,
        tick: datetime,
        symptom: Symptom,
        score: float,
        policy: EpisodePolicyConfig,
    ) -> EpisodeTransition:
        state.consecutive_clear = 0
        already_open = state.phase in (EpisodePhase.ACTIVE, EpisodePhase.CLEARING)
        if already_open:
            state.consecutive_breach += 1
            state.breach_tick_count += 1
            state.last_breach_ts = tick
            state.latest_symptom_id = symptom.symptom_id
            self._raise_peak(state, symptom, score)
            state.revision += 1
            state.phase = EpisodePhase.ACTIVE
            episode = self._build_episode(state, key, status=EpisodeStatus.ACTIVE)
            return EpisodeTransition(key, EpisodeAction.SUSTAINED, EpisodePhase.ACTIVE, episode)

        if state.consecutive_breach == 0:
            state.opened_ts = tick
            state.opening_symptom_id = symptom.symptom_id
            state.peak_score = score
            state.peak_symptom_id = symptom.symptom_id
            state.peak_refs = symptom.evidence_refs
            state.breach_tick_count = 0
        else:
            self._raise_peak(state, symptom, score)
        state.consecutive_breach += 1
        state.breach_tick_count += 1
        state.last_breach_ts = tick
        state.latest_symptom_id = symptom.symptom_id

        if state.consecutive_breach >= policy.open_after_ticks:
            state.phase = EpisodePhase.ACTIVE
            state.confirmed_ts = tick
            state.revision = 1
            state.episode_id = _episode_id(key, _require(state.opened_ts, name="opened_ts"))
            episode = self._build_episode(state, key, status=EpisodeStatus.ACTIVE)
            return EpisodeTransition(key, EpisodeAction.OPENED, EpisodePhase.ACTIVE, episode)

        state.phase = EpisodePhase.PENDING
        return EpisodeTransition(key, EpisodeAction.PENDING, EpisodePhase.PENDING, None)

    def _on_clear(
        self,
        state: _KeyState,
        key: EpisodeKey,
        tick: datetime,
        policy: EpisodePolicyConfig,
    ) -> EpisodeTransition:
        state.consecutive_breach = 0
        if state.phase in (EpisodePhase.IDLE, EpisodePhase.CLOSED, EpisodePhase.PENDING):
            state.phase = EpisodePhase.IDLE
            state.consecutive_clear = 0
            return EpisodeTransition(key, EpisodeAction.HELD, EpisodePhase.IDLE, None)

        state.consecutive_clear += 1
        if state.consecutive_clear >= policy.close_after_ticks:
            state.closed_ts = tick
            state.revision += 1
            episode = self._build_episode(state, key, status=EpisodeStatus.CLOSED)
            self._reset_after_close(state)
            return EpisodeTransition(key, EpisodeAction.CLOSED, EpisodePhase.CLOSED, episode)

        state.phase = EpisodePhase.CLEARING
        episode = self._build_episode(state, key, status=EpisodeStatus.ACTIVE)
        return EpisodeTransition(key, EpisodeAction.CLEARING, EpisodePhase.CLEARING, episode)

    def _on_hold(
        self,
        state: _KeyState,
        key: EpisodeKey,
        tick: datetime,
    ) -> EpisodeTransition:
        # A deadband tick confirms neither a breach nor a clear: it breaks both
        # consecutive runs and holds whatever state the key is already in.
        state.consecutive_breach = 0
        state.consecutive_clear = 0
        if state.phase in (EpisodePhase.ACTIVE, EpisodePhase.CLEARING):
            state.phase = EpisodePhase.ACTIVE
            episode = self._build_episode(state, key, status=EpisodeStatus.ACTIVE)
            return EpisodeTransition(key, EpisodeAction.HELD, EpisodePhase.ACTIVE, episode)
        state.phase = EpisodePhase.IDLE
        return EpisodeTransition(key, EpisodeAction.HELD, EpisodePhase.IDLE, None)

    @staticmethod
    def _raise_peak(state: _KeyState, symptom: Symptom, score: float) -> None:
        if score > state.peak_score:
            state.peak_score = score
            state.peak_symptom_id = symptom.symptom_id
            state.peak_refs = symptom.evidence_refs

    @staticmethod
    def _reset_after_close(state: _KeyState) -> None:
        state.phase = EpisodePhase.IDLE
        state.consecutive_breach = 0
        state.consecutive_clear = 0
        state.episode_id = None
        state.opened_ts = None
        state.confirmed_ts = None
        state.last_breach_ts = None
        state.closed_ts = None
        state.opening_symptom_id = None
        state.peak_symptom_id = None
        state.latest_symptom_id = None
        state.peak_refs = ()
        state.peak_score = 0.0
        state.breach_tick_count = 0
        state.revision = 0

    @staticmethod
    def _build_episode(
        state: _KeyState, key: EpisodeKey, *, status: EpisodeStatus
    ) -> SymptomEpisode:
        closed_ts = state.closed_ts if status is EpisodeStatus.CLOSED else None
        return SymptomEpisode(
            episode_id=_require(state.episode_id, name="episode_id"),
            kind=key.kind,
            service=key.service,
            signal=key.signal,
            status=status,
            opened_ts=_require(state.opened_ts, name="opened_ts"),
            confirmed_ts=_require(state.confirmed_ts, name="confirmed_ts"),
            last_breach_ts=_require(state.last_breach_ts, name="last_breach_ts"),
            closed_ts=closed_ts,
            peak_score=state.peak_score,
            breach_tick_count=state.breach_tick_count,
            revision=state.revision,
            opening_symptom_id=_require(state.opening_symptom_id, name="opening_symptom_id"),
            peak_symptom_id=_require(state.peak_symptom_id, name="peak_symptom_id"),
            latest_symptom_id=_require(state.latest_symptom_id, name="latest_symptom_id"),
            evidence_refs=tuple(sorted(state.peak_refs)),
        )


def _tick_score(symptom: Symptom | None, key: EpisodeKey) -> float:
    if symptom is None:
        return 0.0
    if not isinstance(symptom, Symptom):
        raise TypeError("symptom must be a Symptom or None")
    if (symptom.kind, symptom.service, symptom.signal) != (key.kind, key.service, key.signal):
        raise ValueError("symptom does not belong to the observed episode key")
    return float(symptom.score)


def _episode_id(key: EpisodeKey, opened_ts: datetime) -> str:
    identity = {
        "kind": key.kind.value,
        "service": key.service,
        "signal": key.signal,
        "opened_ts": opened_ts.isoformat(),
    }
    rendered = json.dumps(identity, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _require[T](value: T | None, *, name: str) -> T:
    if value is None:  # defensive: an open episode always has these set
        raise RuntimeError(f"episode invariant violated: {name} is unset")
    return value
