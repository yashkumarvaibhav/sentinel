"""Multivariate behavioral anomaly scoring with an Isolation Forest.

The behavioral-ratio detectors (Phase 2) each watch one ratio an event preserves
but an attack deforms. This model watches them *jointly*: an Isolation Forest
learns the normal region of the 5-D behavioral space and scores how isolated a
new vector is. A joint outlier is behavior an event's volume cannot explain — a
CORROBORATING anomaly vote, never load-bearing: a deterministic verifier and the
policy gate confirm before any action (propose-then-verify).

The OTel Demo has no auth flow, so the behavioral space here is SIMULATED (labeled
end to end), not measured — a config-driven normal vs attack-deformed generator.
Scoring is deterministic on CPU: a fixed Isolation Forest ``random_state`` over a
seeded sample draw, so a bundle reproduces from the config alone. sklearn's
``IsolationForest`` is used directly (PyOD, the ARCHITECTURE §2 name, hard-depends
on numba, which conflicts with numpy>=2.1; PyOD's IForest merely wraps this class).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import numpy.typing as npt
from sklearn.ensemble import IsolationForest
from sklearn.metrics import roc_auc_score

from ml.config import AnomalyParamsConfig, AnomalySimulation

_BUNDLE_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_FOREST_NAME = "forest.joblib"

_LABEL_NORMAL = 0
_LABEL_ATTACK = 1

FloatArray = npt.NDArray[np.float64]


class AnomalyFitError(ValueError):
    """The anomaly model could not be fit from the supplied samples."""


class AnomalyBundleError(ValueError):
    """A saved anomaly bundle is missing or inconsistent."""


@dataclass(frozen=True)
class BehavioralSample:
    """One labeled behavioral-ratio vector; honesty is SIMULATED end to end."""

    features: Mapping[str, float]
    label: int  # _LABEL_NORMAL or _LABEL_ATTACK
    honesty: str


def _regime_draw(rng: np.random.Generator, mean_sd: tuple[float, float]) -> float:
    mean, sd = mean_sd
    return float(rng.normal(mean, sd))


def generate_behavioral_samples(
    simulation: AnomalySimulation, feature_order: Sequence[str], *, seed: int
) -> tuple[BehavioralSample, ...]:
    """One seed's SIMULATED behavioral samples: normal then attack-deformed.

    numpy's PCG64 default generator is stable across platforms, and the draw order
    (normal block, then attack block, each feature in ``feature_order``) is fixed,
    so a config + seed always yields the same vectors.
    """
    rng = np.random.default_rng(seed)
    regimes = {regime.feature: regime for regime in simulation.regimes}
    samples: list[BehavioralSample] = []
    for _ in range(simulation.normal_samples):
        features = {name: _regime_draw(rng, regimes[name].normal) for name in feature_order}
        samples.append(
            BehavioralSample(features=features, label=_LABEL_NORMAL, honesty="SIMULATED")
        )
    for _ in range(simulation.attack_samples):
        features = {name: _regime_draw(rng, regimes[name].attack) for name in feature_order}
        samples.append(
            BehavioralSample(features=features, label=_LABEL_ATTACK, honesty="SIMULATED")
        )
    return tuple(samples)


def generate_all_behavioral_samples(params: AnomalyParamsConfig) -> tuple[BehavioralSample, ...]:
    """The full SIMULATED behavioral dataset across every configured seed."""
    samples: list[BehavioralSample] = []
    for seed in params.simulation.seeds:
        samples.extend(generate_behavioral_samples(params.simulation, params.features, seed=seed))
    return tuple(samples)


def _matrix(vectors: Sequence[Mapping[str, float]], feature_names: Sequence[str]) -> FloatArray:
    return np.asarray(
        [[vector[name] for name in feature_names] for vector in vectors], dtype=np.float64
    )


@dataclass(frozen=True)
class AnomalyModel:
    """A fitted Isolation Forest plus the provenance identifying how it was fit."""

    version: int
    params_fingerprint: str
    feature_names: tuple[str, ...]
    forest: IsolationForest
    train_rows: int

    def score_one(self, features: Mapping[str, float]) -> float:
        """The anomaly score for one vector; higher means more anomalous."""
        return float(self.score_many(_matrix([features], self.feature_names))[0])

    def score_many(self, matrix: FloatArray) -> FloatArray:
        """Anomaly scores for a feature matrix; higher means more anomalous.

        sklearn's ``score_samples`` returns higher for *more normal*, so the sign is
        flipped to give the intuitive "higher is more anomalous" orientation.
        """
        scores = self.forest.score_samples(matrix)  # type: ignore[no-untyped-call]
        return -np.asarray(scores, dtype=np.float64)


def fit_anomaly_model(
    vectors: Sequence[Mapping[str, float]],
    *,
    params: AnomalyParamsConfig,
) -> AnomalyModel:
    """Fit an Isolation Forest on (mostly-normal) behavioral vectors, deterministically."""
    if len(vectors) < params.max_samples:
        raise AnomalyFitError(
            f"need at least max_samples={params.max_samples} vectors to fit, got {len(vectors)}"
        )
    forest = IsolationForest(  # type: ignore[no-untyped-call]
        n_estimators=params.n_estimators,
        max_samples=params.max_samples,
        contamination=params.contamination,
        random_state=params.random_state,
        n_jobs=1,
    )
    forest.fit(_matrix(vectors, params.features))
    return AnomalyModel(
        version=_BUNDLE_VERSION,
        params_fingerprint=params.fingerprint,
        feature_names=tuple(params.features),
        forest=forest,
        train_rows=len(vectors),
    )


@dataclass(frozen=True)
class AnomalyMetric:
    """Separation of an anomaly model's scores between normal and attack samples."""

    eval_rows: int
    roc_auc: float
    normal_mean_score: float
    attack_mean_score: float
    separation: float  # attack_mean_score - normal_mean_score


def evaluate_anomaly(model: AnomalyModel, samples: Sequence[BehavioralSample]) -> AnomalyMetric:
    """Score labeled samples and report ROC-AUC + the normal/attack score gap."""
    labels = [sample.label for sample in samples]
    if len(set(labels)) < 2:
        raise AnomalyFitError("evaluation needs both normal and attack samples")
    scores = model.score_many(_matrix([sample.features for sample in samples], model.feature_names))
    normal = [score for score, label in zip(scores, labels, strict=True) if label == _LABEL_NORMAL]
    attack = [score for score, label in zip(scores, labels, strict=True) if label == _LABEL_ATTACK]
    normal_mean = float(np.mean(normal))
    attack_mean = float(np.mean(attack))
    return AnomalyMetric(
        eval_rows=len(samples),
        roc_auc=float(roc_auc_score(labels, scores)),
        normal_mean_score=normal_mean,
        attack_mean_score=attack_mean,
        separation=attack_mean - normal_mean,
    )


def anomaly_holdout_metrics(
    params: AnomalyParamsConfig, *, holdout_fraction: float = 0.2
) -> AnomalyMetric:
    """Fit on an earlier slice of normal behavior, score held-out normal + all attack."""
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be strictly between 0 and 1")
    samples = generate_all_behavioral_samples(params)
    normal = [sample for sample in samples if sample.label == _LABEL_NORMAL]
    attack = [sample for sample in samples if sample.label == _LABEL_ATTACK]
    cut = int(len(normal) * (1.0 - holdout_fraction))
    model = fit_anomaly_model([sample.features for sample in normal[:cut]], params=params)
    return evaluate_anomaly(model, (*normal[cut:], *attack))


def save_anomaly_model(directory: Path, model: AnomalyModel) -> None:
    """Persist the forest (joblib) plus a JSON manifest of its provenance.

    joblib is sklearn's standard serialization; there is no portable text form. The
    bundle is only ever loaded from our own git-ignored artifact, so the pickle risk
    is bounded to trusted input.
    """
    directory.mkdir(parents=True, exist_ok=True)
    joblib.dump(model.forest, directory / _FOREST_NAME)  # type: ignore[no-untyped-call]
    manifest = {
        "version": model.version,
        "params_fingerprint": model.params_fingerprint,
        "feature_names": list(model.feature_names),
        "train_rows": model.train_rows,
        "forest": _FOREST_NAME,
    }
    (directory / _MANIFEST_NAME).write_text(
        json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_anomaly_model(directory: Path) -> AnomalyModel:
    """Load a saved anomaly bundle, reconstructing the fitted forest."""
    manifest_path = directory / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise AnomalyBundleError(f"no anomaly bundle manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    forest_path = directory / manifest["forest"]
    if not forest_path.is_file():
        raise AnomalyBundleError(f"no forest artifact at {forest_path}")
    forest = joblib.load(forest_path)  # type: ignore[no-untyped-call]
    return AnomalyModel(
        version=int(manifest["version"]),
        params_fingerprint=str(manifest["params_fingerprint"]),
        feature_names=tuple(str(name) for name in manifest["feature_names"]),
        forest=forest,
        train_rows=int(manifest["train_rows"]),
    )


def load_anomaly_model_or_none(directory: Path) -> AnomalyModel | None:
    """Load a bundle if present, else None so callers can skip the anomaly vote."""
    if not (directory / _MANIFEST_NAME).is_file():
        return None
    return load_anomaly_model(directory)
