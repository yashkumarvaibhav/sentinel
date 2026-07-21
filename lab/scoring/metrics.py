"""Small exact scoring primitives shared by live and capture harnesses."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class MetricValue:
    status: Literal["ok", "insufficient"]
    value: float | None
    numerator: int
    denominator: int


@dataclass(frozen=True)
class BinaryMetrics:
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: MetricValue
    recall: MetricValue
    false_positive_rate: MetricValue


def binary_metrics(*, predicted: tuple[bool, ...], expected: tuple[bool, ...]) -> BinaryMetrics:
    if len(predicted) != len(expected):
        raise ValueError("predicted and expected lengths differ")
    true_positive = sum(
        prediction and label for prediction, label in zip(predicted, expected, strict=True)
    )
    false_positive = sum(
        prediction and not label for prediction, label in zip(predicted, expected, strict=True)
    )
    false_negative = sum(
        not prediction and label for prediction, label in zip(predicted, expected, strict=True)
    )
    true_negative = sum(
        not prediction and not label for prediction, label in zip(predicted, expected, strict=True)
    )
    return BinaryMetrics(
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        true_negative=true_negative,
        precision=_ratio(true_positive, true_positive + false_positive),
        recall=_ratio(true_positive, true_positive + false_negative),
        false_positive_rate=_ratio(false_positive, false_positive + true_negative),
    )


def nearest_rank(values: tuple[float, ...], *, percentile: float) -> MetricValue:
    if not 0.0 < percentile <= 1.0:
        raise ValueError("percentile must be in (0, 1]")
    if not values:
        return MetricValue(status="insufficient", value=None, numerator=0, denominator=0)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("percentile samples must be finite")
    ordered = sorted(values)
    rank = math.ceil(percentile * len(ordered))
    return MetricValue(
        status="ok",
        value=ordered[rank - 1],
        numerator=rank,
        denominator=len(ordered),
    )


def _ratio(numerator: int, denominator: int) -> MetricValue:
    if denominator == 0:
        return MetricValue(
            status="insufficient",
            value=None,
            numerator=numerator,
            denominator=denominator,
        )
    return MetricValue(
        status="ok",
        value=numerator / denominator,
        numerator=numerator,
        denominator=denominator,
    )
