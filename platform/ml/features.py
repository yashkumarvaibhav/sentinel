"""Context-aware and context-blind features for the learned envelopes.

The 3.2 quantile envelopes train an aware model on the full feature set and a
context-blind twin on the temporal features alone; the aware/blind gap is the
part of the volume an event explains. This module is the single owner of the
feature schema so the trainer, the twin and any serving path agree exactly.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

# Ordered, stable feature schema. TEMPORAL features are the seasonal cycle every
# model sees; EVENT features encode the trusted explained-event lift and are the
# only ones withheld from the context-blind twin.
TEMPORAL_FEATURES: tuple[str, ...] = ("tod_sin", "tod_cos", "dow_sin", "dow_cos", "is_weekend")
EVENT_FEATURES: tuple[str, ...] = ("event_lift", "event_count", "event_trust_max")
AWARE_FEATURES: tuple[str, ...] = TEMPORAL_FEATURES + EVENT_FEATURES

_SECONDS_PER_DAY = 86_400.0
_DAYS_PER_WEEK = 7.0
_WEEKEND_WEEKDAY = 5  # Python weekday(): Monday=0 .. Saturday=5, Sunday=6


@dataclass(frozen=True)
class ActiveEvent:
    """One trusted event window overlapping an observation tick."""

    multiplier: float
    trust_score: float

    def explained_lift(self) -> float:
        """The event's contribution to volume, matching decomposition semantics."""
        return max(self.multiplier - 1.0, 0.0) * self.trust_score


def temporal_features(ts: datetime) -> dict[str, float]:
    """Cyclic time-of-day + day-of-week encodings from a real UTC timestamp."""
    seconds_of_day = ts.hour * 3600 + ts.minute * 60 + ts.second + ts.microsecond / 1_000_000
    tod_angle = 2.0 * math.pi * seconds_of_day / _SECONDS_PER_DAY
    weekday = ts.weekday()
    dow_angle = 2.0 * math.pi * weekday / _DAYS_PER_WEEK
    return {
        "tod_sin": math.sin(tod_angle),
        "tod_cos": math.cos(tod_angle),
        "dow_sin": math.sin(dow_angle),
        "dow_cos": math.cos(dow_angle),
        "is_weekend": 1.0 if weekday >= _WEEKEND_WEEKDAY else 0.0,
    }


def event_features(active: Sequence[ActiveEvent]) -> dict[str, float]:
    """Aggregate the active trusted events into order-independent lift features."""
    lift = math.fsum(sorted(event.explained_lift() for event in active))
    trust_max = max((event.trust_score for event in active), default=0.0)
    return {
        "event_lift": lift,
        "event_count": float(len(active)),
        "event_trust_max": trust_max,
    }


def aware_features(ts: datetime, active: Sequence[ActiveEvent]) -> dict[str, float]:
    """The full aware feature vector for one observation tick."""
    return {**temporal_features(ts), **event_features(active)}


def context_blind_view(features: Mapping[str, float]) -> dict[str, float]:
    """Project an aware feature vector onto the context-blind twin's schema."""
    return {name: features[name] for name in TEMPORAL_FEATURES}
