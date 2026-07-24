"""Deterministic seeded synthetic history for the learned envelopes.

Runtime captures are only minutes long, so they cannot teach a model the
diurnal + weekly + event seasonality the envelopes must learn. This generator
composes that seasonality from the operator-owned `ml-training.yml` config and a
stdlib PRNG: fully deterministic per seed, so the dataset it feeds is
content-hashable. Every value it produces is labeled SIMULATED.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from random import Random

from ml.config import MlTrainingConfig, SignalSpec, SyntheticEvent
from ml.features import ActiveEvent, aware_features
from ml.frames import TrainingFrame

_SYNTHETIC_SCENARIO_ID = "synthetic_history"


def _diurnal_factor(ts: datetime, spec: SignalSpec) -> float:
    """A smooth daily cycle peaking at the configured hour, always positive."""
    hour = ts.hour + ts.minute / 60.0 + ts.second / 3600.0
    angle = 2.0 * math.pi * (hour - spec.diurnal_peak_hour) / 24.0
    return 1.0 + spec.diurnal_amplitude * math.cos(angle)


def _weekly_factor(ts: datetime, spec: SignalSpec) -> float:
    return spec.weekend_factor if ts.weekday() >= 5 else 1.0


def _active_events(
    events: tuple[SyntheticEvent, ...],
    *,
    signal: str,
    day_index: int,
    hour: float,
) -> tuple[ActiveEvent, ...]:
    """The trusted events covering this tick, ordered by id for determinism."""
    return tuple(
        ActiveEvent(multiplier=event.multiplier, trust_score=event.trust_score)
        for event in sorted(events, key=lambda item: item.event_id)
        if event.signal == signal
        and day_index in event.day_indices
        and event.start_hour <= hour < event.start_hour + event.duration_hours
    )


def generate_synthetic_history(
    config: MlTrainingConfig,
    *,
    seed: int,
) -> tuple[TrainingFrame, ...]:
    """Generate one seed's worth of labeled synthetic training frames.

    The PRNG is drawn in a fixed (tick, signal) order, so a given config + seed
    always yields byte-identical frames regardless of how many seeds are built.
    """
    history = config.history
    # Deterministic synthetic data, not a security context: the stdlib Mersenne
    # Twister is stable across CPython versions, so the same seed reproduces bytes.
    rng = Random(seed)
    tick = timedelta(seconds=history.tick_seconds)
    ticks_per_day = 86_400 // history.tick_seconds
    total_ticks = ticks_per_day * history.horizon_days
    frames: list[TrainingFrame] = []
    for index in range(total_ticks):
        ts = history.start + tick * index
        day_index = index // ticks_per_day
        hour = ts.hour + ts.minute / 60.0 + ts.second / 3600.0
        for spec in config.signals:
            active = _active_events(
                config.events,
                signal=spec.signal,
                day_index=day_index,
                hour=hour,
            )
            seasonal = spec.base_level * _diurnal_factor(ts, spec) * _weekly_factor(ts, spec)
            lift = math.fsum(sorted(event.explained_lift() for event in active))
            mean = seasonal * (1.0 + lift)
            noise = rng.gauss(0.0, spec.noise_fraction)
            value = max(spec.floor, mean * (1.0 + noise))
            frames.append(
                TrainingFrame(
                    signal_key=spec.signal,
                    ts=ts,
                    value=value,
                    features=aware_features(ts, active),
                    source="synthetic",
                    honesty="SIMULATED",
                    origin_seed=seed,
                    scenario_id=_SYNTHETIC_SCENARIO_ID,
                    seed_purpose="synthetic",
                )
            )
    return tuple(frames)


def generate_all_synthetic_history(config: MlTrainingConfig) -> tuple[TrainingFrame, ...]:
    """Generate the full synthetic history across every configured seed."""
    frames: list[TrainingFrame] = []
    for seed in config.history.seeds:
        frames.extend(generate_synthetic_history(config, seed=seed))
    return tuple(frames)
