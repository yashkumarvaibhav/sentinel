"""Held-out evaluation metrics for the quantile envelopes.

Two questions matter for a learned band: is it calibrated (does the p90 sit above
~90% of values), and is it sharp (low pinball loss). These pure metrics answer
both, and `holdout_metrics` reports them on a temporal split trained separately
from the shipped bundle, so the numbers describe generalization rather than fit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ml.config import EnvelopeParamsConfig
from ml.envelopes import SignalEnvelope, train_envelopes
from ml.frames import TrainingFrame


@dataclass(frozen=True)
class QuantileMetric:
    """Calibration + sharpness for one trained quantile."""

    quantile: float
    coverage: float  # fraction of observed values at or below the predicted quantile
    pinball_loss: float


@dataclass(frozen=True)
class EnvelopeMetrics:
    """Per-signal evaluation of an envelope on a set of held-out frames."""

    signal_key: str
    eval_rows: int
    per_quantile: tuple[QuantileMetric, ...]
    interval_coverage: float  # fraction of values inside [lowest, highest] aware quantile


def pinball_loss(observed: float, predicted: float, *, alpha: float) -> float:
    """The quantile (pinball) loss for a single prediction."""
    delta = observed - predicted
    return alpha * delta if delta >= 0.0 else (alpha - 1.0) * delta


def evaluate_signal_envelope(
    envelope: SignalEnvelope,
    frames: Sequence[TrainingFrame],
) -> EnvelopeMetrics:
    """Score one signal's envelope against held-out frames of that signal."""
    rows = [frame for frame in frames if frame.signal_key == envelope.signal_key]
    if not rows:
        raise ValueError(f"no evaluation frames for signal {envelope.signal_key}")
    bands = [envelope.predict(frame.features) for frame in rows]
    values = [frame.value for frame in rows]
    per_quantile = tuple(
        QuantileMetric(
            quantile=quantile,
            coverage=sum(
                value <= band.aware[quantile] for value, band in zip(values, bands, strict=True)
            )
            / len(rows),
            pinball_loss=sum(
                pinball_loss(value, band.aware[quantile], alpha=quantile)
                for value, band in zip(values, bands, strict=True)
            )
            / len(rows),
        )
        for quantile in envelope.quantiles
    )
    interval_coverage = sum(
        band.lower <= value <= band.upper for value, band in zip(values, bands, strict=True)
    ) / len(rows)
    return EnvelopeMetrics(
        signal_key=envelope.signal_key,
        eval_rows=len(rows),
        per_quantile=per_quantile,
        interval_coverage=interval_coverage,
    )


def time_split(
    frames: Sequence[TrainingFrame],
    *,
    holdout_fraction: float,
) -> tuple[tuple[TrainingFrame, ...], tuple[TrainingFrame, ...]]:
    """Deterministically split frames into an earlier train set and a later holdout."""
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be strictly between 0 and 1")
    ordered = sorted(frames, key=lambda frame: (frame.ts, frame.origin_seed, frame.signal_key))
    cut = int(len(ordered) * (1.0 - holdout_fraction))
    return tuple(ordered[:cut]), tuple(ordered[cut:])


def holdout_metrics(
    frames: Sequence[TrainingFrame],
    *,
    params: EnvelopeParamsConfig,
    holdout_fraction: float = 0.2,
) -> tuple[EnvelopeMetrics, ...]:
    """Train on the earlier frames and report metrics on the later holdout, per signal."""
    train, holdout = time_split(frames, holdout_fraction=holdout_fraction)
    model = train_envelopes(train, params=params)
    return tuple(
        evaluate_signal_envelope(model.signals[signal_key], holdout)
        for signal_key in sorted(model.signals)
        if any(frame.signal_key == signal_key for frame in holdout)
    )
