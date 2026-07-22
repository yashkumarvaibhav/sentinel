"""PELT-proposed change points with deterministic resource saturation gates."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import cast

import numpy as np
from ruptures.detection.pelt import Pelt

from common.config import ChangePointSaturationConfig
from contracts import Symptom, SymptomKind


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """One label-free resource measurement and its replay-stable evidence identity."""

    evidence_id: str
    ts: datetime
    service: str
    signal: str
    used: float
    capacity: float

    def __post_init__(self) -> None:
        for name in ("evidence_id", "service", "signal"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.ts, datetime) or self.ts.utcoffset() != timedelta(0):
            raise ValueError("ts must be timezone-aware UTC")
        object.__setattr__(self, "ts", self.ts.astimezone(UTC))
        used = _finite_number(self.used, name="used")
        capacity = _finite_number(self.capacity, name="capacity")
        if used < 0.0:
            raise ValueError("used must be non-negative")
        if capacity <= 0.0:
            raise ValueError("capacity must be greater than zero")
        if used > capacity:
            raise ValueError("used cannot exceed capacity")
        object.__setattr__(self, "used", used)
        object.__setattr__(self, "capacity", capacity)


@dataclass(frozen=True, slots=True)
class SaturationEvaluation:
    """Auditable PELT and verifier output, including insufficient/stable windows."""

    sample_count: int
    change_index: int | None
    change_ts: datetime | None
    before_level: float | None
    after_level: float | None
    growth_slope_per_second: float | None
    headroom: float | None
    headroom_ratio: float | None
    symptom: Symptom | None


@dataclass(frozen=True, slots=True)
class _GrowthCandidate:
    change_index: int
    before_level: float
    after_level: float
    growth_slope_per_second: float
    utilization_slope_per_second: float
    increasing_fraction: float


class SaturationDetector:
    """Use PELT only as a proposal, then verify monotonic growth and low headroom."""

    def __init__(self, *, configuration: ChangePointSaturationConfig) -> None:
        self._configuration = configuration

    def evaluate(self, samples: Sequence[ResourceSample]) -> SaturationEvaluation:
        """Evaluate one service/resource window without consulting traffic or context."""
        ordered = _validated_samples(samples)
        if len(ordered) < self._configuration.minimum_series_points:
            return _empty_evaluation(len(ordered))

        utilizations = np.asarray(
            [sample.used / sample.capacity for sample in ordered],
            dtype=np.float64,
        ).reshape(-1, 1)
        algorithm = Pelt(  # type: ignore[no-untyped-call]
            model=self._configuration.pelt_model,
            min_size=self._configuration.minimum_segment_points,
            jump=1,
        )
        algorithm.fit(utilizations)
        breakpoints = cast(
            list[int],
            algorithm.predict(  # type: ignore[no-untyped-call]
                pen=self._configuration.pelt_penalty
            ),
        )
        candidate = self._first_verified_growth(ordered, breakpoints[:-1])
        if candidate is None:
            return _empty_evaluation(len(ordered))

        last = ordered[-1]
        headroom = last.capacity - last.used
        headroom_ratio = headroom / last.capacity
        symptom = None
        if headroom_ratio <= self._configuration.maximum_headroom_ratio:
            symptom = self._symptom(
                ordered=ordered,
                candidate=candidate,
                headroom=headroom,
                headroom_ratio=headroom_ratio,
            )
        return SaturationEvaluation(
            sample_count=len(ordered),
            change_index=candidate.change_index,
            change_ts=ordered[candidate.change_index].ts,
            before_level=candidate.before_level,
            after_level=candidate.after_level,
            growth_slope_per_second=candidate.growth_slope_per_second,
            headroom=headroom,
            headroom_ratio=headroom_ratio,
            symptom=symptom,
        )

    def _first_verified_growth(
        self,
        samples: tuple[ResourceSample, ...],
        proposed_indices: Sequence[int],
    ) -> _GrowthCandidate | None:
        capacity = samples[0].capacity
        for index in proposed_indices:
            before = samples[:index]
            after = samples[index:]
            if min(len(before), len(after)) < self._configuration.minimum_segment_points:
                continue
            increasing_fraction = sum(
                current.used > previous.used for previous, current in pairwise(after)
            ) / (len(after) - 1)
            if increasing_fraction < self._configuration.minimum_increasing_fraction:
                continue
            utilization_slope = _slope_per_second(
                tuple(sample.ts for sample in after),
                tuple(sample.used / capacity for sample in after),
            )
            if utilization_slope < self._configuration.minimum_utilization_slope_per_second:
                continue
            before_level = math.fsum(sample.used for sample in before) / len(before)
            after_level = math.fsum(sample.used for sample in after) / len(after)
            if after_level <= before_level:
                continue
            return _GrowthCandidate(
                change_index=index,
                before_level=before_level,
                after_level=after_level,
                growth_slope_per_second=_slope_per_second(
                    tuple(sample.ts for sample in after),
                    tuple(sample.used for sample in after),
                ),
                utilization_slope_per_second=utilization_slope,
                increasing_fraction=increasing_fraction,
            )
        return None

    def _symptom(
        self,
        *,
        ordered: tuple[ResourceSample, ...],
        candidate: _GrowthCandidate,
        headroom: float,
        headroom_ratio: float,
    ) -> Symptom:
        onset = ordered[candidate.change_index]
        evidence_refs = tuple(sample.evidence_id for sample in ordered)
        score = _headroom_score(headroom_ratio, configuration=self._configuration)
        note = (
            f"resource saturation verified after PELT proposal: change_index="
            f"{candidate.change_index}, before_level={_render(candidate.before_level)}, "
            f"after_level={_render(candidate.after_level)}, "
            f"growth_per_second={_render(candidate.growth_slope_per_second)}, "
            f"utilization_growth_per_second="
            f"{_render(candidate.utilization_slope_per_second)}, "
            f"increasing_fraction={_render(candidate.increasing_fraction)}, "
            f"headroom={_render(headroom)}, headroom_ratio={_render(headroom_ratio)}, "
            f"maximum_headroom_ratio="
            f"{_render(self._configuration.maximum_headroom_ratio)}."
        )
        identity = {
            "service": onset.service,
            "signal": onset.signal,
            "change_index": candidate.change_index,
            "change_ts": onset.ts.isoformat(),
            "before_level": candidate.before_level,
            "after_level": candidate.after_level,
            "growth_slope_per_second": candidate.growth_slope_per_second,
            "utilization_slope_per_second": candidate.utilization_slope_per_second,
            "increasing_fraction": candidate.increasing_fraction,
            "headroom": headroom,
            "headroom_ratio": headroom_ratio,
            "evidence_refs": evidence_refs,
        }
        return Symptom(
            symptom_id=_digest(identity),
            kind=SymptomKind.SATURATION,
            service=onset.service,
            signal=onset.signal,
            onset_ts=onset.ts,
            score=score,
            note=note,
            evidence_refs=evidence_refs,
        )


def _validated_samples(samples: Sequence[ResourceSample]) -> tuple[ResourceSample, ...]:
    if not isinstance(samples, Sequence):
        raise TypeError("samples must be a sequence")
    if any(not isinstance(sample, ResourceSample) for sample in samples):
        raise TypeError("samples must contain only ResourceSample values")
    ordered = tuple(sorted(samples, key=lambda sample: (sample.ts, sample.evidence_id)))
    evidence_ids = tuple(sample.evidence_id for sample in ordered)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("evidence_id values must be unique within a window")
    identities = {(sample.service, sample.signal) for sample in ordered}
    if len(identities) > 1:
        raise ValueError("a resource window must contain one service and signal")
    capacities = {sample.capacity for sample in ordered}
    if len(capacities) > 1:
        raise ValueError("a resource window must use one constant capacity")
    timestamps = tuple(sample.ts for sample in ordered)
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("resource sample timestamps must be unique")
    return ordered


def _slope_per_second(timestamps: tuple[datetime, ...], values: tuple[float, ...]) -> float:
    origin = timestamps[0]
    seconds = tuple((timestamp - origin).total_seconds() for timestamp in timestamps)
    mean_seconds = math.fsum(seconds) / len(seconds)
    mean_value = math.fsum(values) / len(values)
    denominator = math.fsum((value - mean_seconds) ** 2 for value in seconds)
    if denominator == 0.0:
        return 0.0
    numerator = math.fsum(
        (second - mean_seconds) * (value - mean_value)
        for second, value in zip(seconds, values, strict=True)
    )
    return numerator / denominator


def _headroom_score(
    headroom_ratio: float,
    *,
    configuration: ChangePointSaturationConfig,
) -> float:
    trigger_score = 1.0 - configuration.maximum_headroom_ratio
    progress = (configuration.maximum_headroom_ratio - headroom_ratio) / (
        configuration.maximum_headroom_ratio - configuration.full_score_headroom_ratio
    )
    return min(trigger_score + (1.0 - trigger_score) * max(progress, 0.0), 1.0)


def _empty_evaluation(sample_count: int) -> SaturationEvaluation:
    return SaturationEvaluation(
        sample_count=sample_count,
        change_index=None,
        change_ts=None,
        before_level=None,
        after_level=None,
        growth_slope_per_second=None,
        headroom=None,
        headroom_ratio=None,
        symptom=None,
    )


def _finite_number(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _render(value: float) -> str:
    return format(value, ".12g")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
