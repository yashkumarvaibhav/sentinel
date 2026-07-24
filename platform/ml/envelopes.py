"""Context-conditioned quantile envelopes: LightGBM p10/p50/p90 + a blind twin.

The aware model learns the value distribution from the full feature set; a
context-blind twin learns the upper band from the temporal features alone. Their
p90 gap is the part of the band an event explains — the learned counterpart of
the deterministic decomposition's explained_event. Training is deterministic on
CPU (fixed seed, single-threaded, row-wise histograms) so a model bundle is
reproducible from a dataset + the versioned hyperparameters.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np

from ml.config import EnvelopeParamsConfig
from ml.features import AWARE_FEATURES, TEMPORAL_FEATURES
from ml.frames import TrainingFrame

_BUNDLE_VERSION = 1
_MANIFEST_NAME = "manifest.json"


class EnvelopeTrainingError(ValueError):
    """No signal had enough history to train an envelope."""


class EnvelopeBundleError(ValueError):
    """A saved envelope bundle is missing or inconsistent."""


@dataclass(frozen=True)
class QuantileBand:
    """One tick's learned band: aware quantiles plus the context-blind upper twin."""

    quantiles: tuple[float, ...]
    aware: Mapping[float, float]
    blind_upper: float

    @property
    def lower(self) -> float:
        return self.aware[self.quantiles[0]]

    @property
    def upper(self) -> float:
        return self.aware[self.quantiles[-1]]

    @property
    def event_explained_upper(self) -> float:
        """The aware-minus-blind gap in the upper band: the learned event lift."""
        return max(self.upper - self.blind_upper, 0.0)


@dataclass(frozen=True)
class SignalEnvelope:
    """The trained aware quantile models and blind twin for one signal."""

    signal_key: str
    quantiles: tuple[float, ...]
    blind_quantile: float
    aware_models: Mapping[float, lgb.Booster]
    blind_model: lgb.Booster
    train_rows: int

    def predict(self, features: Mapping[str, float]) -> QuantileBand:
        aware_vector = [features[name] for name in AWARE_FEATURES]
        blind_vector = [features[name] for name in TEMPORAL_FEATURES]
        raw = [
            _predict_one(self.aware_models[quantile], aware_vector) for quantile in self.quantiles
        ]
        # Enforce non-crossing quantiles: a monotone envelope is a hard requirement,
        # and quantile regressors can otherwise cross on sparse regions.
        monotone: list[float] = []
        running = float("-inf")
        for value in raw:
            running = max(running, value)
            monotone.append(running)
        aware = dict(zip(self.quantiles, monotone, strict=True))
        blind_upper = _predict_one(self.blind_model, blind_vector)
        return QuantileBand(quantiles=self.quantiles, aware=aware, blind_upper=blind_upper)


@dataclass(frozen=True)
class EnvelopeModel:
    """Every signal's envelope plus the provenance identifying how it was trained."""

    version: int
    params_fingerprint: str
    training_data_hash: str
    training_config_fingerprint: str
    detector_config_fingerprint: str
    quantiles: tuple[float, ...]
    blind_quantile: float
    signals: Mapping[str, SignalEnvelope]

    def predict(self, signal_key: str, features: Mapping[str, float]) -> QuantileBand | None:
        """The learned band for a signal, or None when no envelope was trained for it."""
        envelope = self.signals.get(signal_key)
        return None if envelope is None else envelope.predict(features)


def _predict_one(booster: lgb.Booster, vector: Sequence[float]) -> float:
    matrix = np.asarray([vector], dtype=np.float64)
    prediction = booster.predict(matrix)
    return float(np.asarray(prediction, dtype=np.float64).reshape(-1)[0])


def _fit_quantile(
    features: np.ndarray,
    target: np.ndarray,
    *,
    alpha: float,
    params: EnvelopeParamsConfig,
) -> lgb.Booster:
    dataset = lgb.Dataset(features, label=target, free_raw_data=False)
    booster = lgb.train(
        {
            "objective": "quantile",
            "alpha": alpha,
            "learning_rate": params.learning_rate,
            "num_leaves": params.num_leaves,
            "min_data_in_leaf": params.min_data_in_leaf,
            "max_depth": params.max_depth,
            "bagging_freq": 0,
            "feature_fraction": 1.0,
            "seed": params.seed,
            "deterministic": True,
            "force_row_wise": True,
            "num_threads": 1,
            "verbose": -1,
        },
        dataset,
        num_boost_round=params.n_estimators,
    )
    return booster


def _train_signal(
    signal_key: str,
    frames: Sequence[TrainingFrame],
    params: EnvelopeParamsConfig,
) -> SignalEnvelope:
    aware = np.asarray(
        [[frame.features[name] for name in AWARE_FEATURES] for frame in frames],
        dtype=np.float64,
    )
    blind = np.asarray(
        [[frame.features[name] for name in TEMPORAL_FEATURES] for frame in frames],
        dtype=np.float64,
    )
    target = np.asarray([frame.value for frame in frames], dtype=np.float64)
    aware_models = {
        quantile: _fit_quantile(aware, target, alpha=quantile, params=params)
        for quantile in params.quantiles
    }
    blind_model = _fit_quantile(blind, target, alpha=params.blind_quantile, params=params)
    return SignalEnvelope(
        signal_key=signal_key,
        quantiles=params.quantiles,
        blind_quantile=params.blind_quantile,
        aware_models=aware_models,
        blind_model=blind_model,
        train_rows=len(frames),
    )


def train_envelopes(
    frames: Sequence[TrainingFrame],
    *,
    params: EnvelopeParamsConfig,
    training_data_hash: str = "",
    training_config_fingerprint: str = "",
    detector_config_fingerprint: str = "",
) -> EnvelopeModel:
    """Train one envelope per signal that has at least `min_rows_per_signal` rows."""
    grouped: dict[str, list[TrainingFrame]] = defaultdict(list)
    for frame in frames:
        grouped[frame.signal_key].append(frame)
    signals = {
        signal_key: _train_signal(signal_key, grouped[signal_key], params)
        for signal_key in sorted(grouped)
        if len(grouped[signal_key]) >= params.min_rows_per_signal
    }
    if not signals:
        raise EnvelopeTrainingError(
            f"no signal reached the {params.min_rows_per_signal}-row minimum to train an envelope"
        )
    return EnvelopeModel(
        version=_BUNDLE_VERSION,
        params_fingerprint=params.fingerprint,
        training_data_hash=training_data_hash,
        training_config_fingerprint=training_config_fingerprint,
        detector_config_fingerprint=detector_config_fingerprint,
        quantiles=params.quantiles,
        blind_quantile=params.blind_quantile,
        signals=signals,
    )


def _signal_dir_name(signal_key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", signal_key).strip("_")


def save_envelope_model(directory: Path, model: EnvelopeModel) -> None:
    """Write each booster as LightGBM text plus a manifest tying them to provenance."""
    directory.mkdir(parents=True, exist_ok=True)
    signal_entries: list[dict[str, object]] = []
    for signal_key in sorted(model.signals):
        envelope = model.signals[signal_key]
        sub = _signal_dir_name(signal_key)
        signal_dir = directory / sub
        signal_dir.mkdir(parents=True, exist_ok=True)
        aware_files: dict[str, str] = {}
        for quantile, booster in envelope.aware_models.items():
            name = f"aware_{quantile}.txt"
            booster.save_model(str(signal_dir / name))
            aware_files[repr(quantile)] = name
        envelope.blind_model.save_model(str(signal_dir / "blind.txt"))
        signal_entries.append(
            {
                "signal_key": signal_key,
                "dir": sub,
                "train_rows": envelope.train_rows,
                "aware_models": aware_files,
                "blind_model": "blind.txt",
            }
        )
    manifest = {
        "version": model.version,
        "params_fingerprint": model.params_fingerprint,
        "training_data_hash": model.training_data_hash,
        "training_config_fingerprint": model.training_config_fingerprint,
        "detector_config_fingerprint": model.detector_config_fingerprint,
        "quantiles": list(model.quantiles),
        "blind_quantile": model.blind_quantile,
        "signals": signal_entries,
    }
    (directory / _MANIFEST_NAME).write_text(
        json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_envelope_model(directory: Path) -> EnvelopeModel:
    """Load a saved bundle, reconstructing every booster from its text form."""
    manifest_path = directory / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise EnvelopeBundleError(f"no envelope bundle manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    quantiles = tuple(float(value) for value in manifest["quantiles"])
    signals: dict[str, SignalEnvelope] = {}
    for entry in manifest["signals"]:
        signal_dir = directory / entry["dir"]
        aware_models = {
            float(quantile): lgb.Booster(model_file=str(signal_dir / filename))
            for quantile, filename in entry["aware_models"].items()
        }
        blind_model = lgb.Booster(model_file=str(signal_dir / entry["blind_model"]))
        signals[entry["signal_key"]] = SignalEnvelope(
            signal_key=entry["signal_key"],
            quantiles=quantiles,
            blind_quantile=float(manifest["blind_quantile"]),
            aware_models=aware_models,
            blind_model=blind_model,
            train_rows=int(entry["train_rows"]),
        )
    return EnvelopeModel(
        version=int(manifest["version"]),
        params_fingerprint=str(manifest["params_fingerprint"]),
        training_data_hash=str(manifest["training_data_hash"]),
        training_config_fingerprint=str(manifest["training_config_fingerprint"]),
        detector_config_fingerprint=str(manifest["detector_config_fingerprint"]),
        quantiles=quantiles,
        blind_quantile=float(manifest["blind_quantile"]),
        signals=signals,
    )


def load_envelope_model_or_none(directory: Path) -> EnvelopeModel | None:
    """Load a bundle if present, else None so callers fall back to deterministic bands."""
    if not (directory / _MANIFEST_NAME).is_file():
        return None
    return load_envelope_model(directory)
