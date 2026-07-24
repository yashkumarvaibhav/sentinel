"""LSTM auth-sequence autoencoder: reconstruction error as an anomaly vote.

A reconstruction autoencoder learns the shape of a NORMAL auth session (slow,
mostly-successful, single-account). A credential-stuffing sequence — rapid, high
failure, spraying new accounts — falls outside what the decoder learned to
reproduce, so its reconstruction error is high. That error is a CORROBORATING
anomaly vote, never load-bearing: a deterministic verifier and the policy gate
confirm before any action (propose-then-verify).

The OTel Demo has no auth flow, so the sequences here are SIMULATED (labeled end
to end), not measured. Training is deterministic on CPU — a fixed seed, single
thread and full-batch order give bit-exact reconstruction scores, so a bundle
reproduces from the config alone. torch is pinned to CPU wheels (pyproject).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
import torch
from sklearn.metrics import roc_auc_score
from torch import nn

from ml.config import AuthRegime, AuthSimulation, AutoencoderParamsConfig

_BUNDLE_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_STATE_NAME = "autoencoder.pt"
_LABEL_NORMAL = 0
_LABEL_ATTACK = 1

FloatArray = npt.NDArray[np.float64]


class AutoencoderError(ValueError):
    """The autoencoder could not be fit from the supplied sequences."""


class AutoencoderBundleError(ValueError):
    """A saved autoencoder bundle is missing or inconsistent."""


@contextmanager
def _single_threaded() -> Iterator[None]:
    """Pin torch to one thread so float reductions are order-stable (bit-exact)."""
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    try:
        yield
    finally:
        torch.set_num_threads(previous)


@dataclass(frozen=True)
class AuthSequenceSample:
    """One labeled auth sequence; honesty is SIMULATED end to end."""

    steps: tuple[tuple[float, ...], ...]  # (seq_len, n_features)
    label: int
    honesty: str


def generate_auth_sequences(
    simulation: AuthSimulation, *, seq_len: int, seed: int
) -> tuple[AuthSequenceSample, ...]:
    """One seed's SIMULATED auth sequences: normal sessions then attack sessions.

    Each step is (inter_arrival, is_failure, is_new_account). numpy's PCG64 default
    generator is stable across platforms and the draw order is fixed, so a config +
    seed always yields the same sequences.
    """
    rng = np.random.default_rng(seed)

    def make(regime: AuthRegime, label: int) -> AuthSequenceSample:
        steps: list[tuple[float, ...]] = []
        for _ in range(seq_len):
            inter_arrival = float(rng.normal(regime.inter_arrival[0], regime.inter_arrival[1]))
            is_failure = 1.0 if rng.random() < regime.failure_prob else 0.0
            is_new_account = 1.0 if rng.random() < regime.new_account_prob else 0.0
            steps.append((inter_arrival, is_failure, is_new_account))
        return AuthSequenceSample(steps=tuple(steps), label=label, honesty="SIMULATED")

    samples = [make(simulation.normal, _LABEL_NORMAL) for _ in range(simulation.normal_sequences)]
    samples.extend(
        make(simulation.attack, _LABEL_ATTACK) for _ in range(simulation.attack_sequences)
    )
    return tuple(samples)


def generate_all_auth_sequences(params: AutoencoderParamsConfig) -> tuple[AuthSequenceSample, ...]:
    """The full SIMULATED auth-sequence dataset across every configured seed."""
    samples: list[AuthSequenceSample] = []
    for seed in params.simulation.seeds:
        samples.extend(
            generate_auth_sequences(params.simulation, seq_len=params.seq_len, seed=seed)
        )
    return tuple(samples)


def sequences_to_array(samples: Sequence[AuthSequenceSample]) -> FloatArray:
    """Stack labeled samples into a (batch, seq_len, n_features) float array."""
    return np.asarray(
        [[list(step) for step in sample.steps] for sample in samples], dtype=np.float64
    )


class _SequenceAutoencoder(nn.Module):
    """A symmetric LSTM encoder/decoder that reconstructs a fixed-length sequence."""

    def __init__(
        self, n_features: int, hidden_size: int, latent_size: int, num_layers: int
    ) -> None:
        super().__init__()
        self.encoder = nn.LSTM(n_features, hidden_size, num_layers, batch_first=True)
        self.to_latent = nn.Linear(hidden_size, latent_size)
        self.from_latent = nn.Linear(latent_size, hidden_size)
        self.decoder = nn.LSTM(hidden_size, hidden_size, num_layers, batch_first=True)
        self.output = nn.Linear(hidden_size, n_features)

    def forward(self, sequence: torch.Tensor) -> torch.Tensor:
        _, (hidden, _) = self.encoder(sequence)
        latent = self.to_latent(hidden[-1])
        seq_len = sequence.shape[1]
        decoded_input = self.from_latent(latent).unsqueeze(1).repeat(1, seq_len, 1)
        decoded, _ = self.decoder(decoded_input)
        reconstruction: torch.Tensor = self.output(decoded)
        return reconstruction


@dataclass(frozen=True)
class AutoencoderModel:
    """A trained sequence autoencoder plus the provenance identifying how it was fit."""

    version: int
    params_fingerprint: str
    feature_names: tuple[str, ...]
    seq_len: int
    hidden_size: int
    latent_size: int
    num_layers: int
    module: _SequenceAutoencoder
    train_rows: int

    def score_many(self, sequences: FloatArray) -> FloatArray:
        """Per-sequence reconstruction error; higher means more anomalous."""
        self.module.eval()
        with _single_threaded(), torch.no_grad():
            tensor = torch.from_numpy(sequences.astype(np.float32))
            reconstruction = self.module(tensor)
            error = ((reconstruction - tensor) ** 2).mean(dim=(1, 2))
        return np.asarray(error.numpy(), dtype=np.float64)

    def score_one(self, sample: AuthSequenceSample) -> float:
        """The reconstruction-error anomaly score for one sequence."""
        return float(self.score_many(sequences_to_array([sample]))[0])


def fit_autoencoder(
    sequences: FloatArray,
    *,
    params: AutoencoderParamsConfig,
) -> AutoencoderModel:
    """Train the autoencoder on normal sequences, deterministically on CPU."""
    if sequences.ndim != 3 or sequences.shape[0] < 1:
        raise AutoencoderError("training needs a non-empty (batch, seq_len, features) array")
    if sequences.shape[1] != params.seq_len or sequences.shape[2] != len(params.features):
        raise AutoencoderError("sequence shape does not match the configured seq_len/features")
    with _single_threaded():
        torch.manual_seed(params.seed)
        module = _SequenceAutoencoder(
            n_features=len(params.features),
            hidden_size=params.hidden_size,
            latent_size=params.latent_size,
            num_layers=params.num_layers,
        )
        optimizer = torch.optim.Adam(module.parameters(), lr=params.learning_rate)
        criterion = nn.MSELoss()
        tensor = torch.from_numpy(sequences.astype(np.float32))
        module.train()
        for _ in range(params.epochs):
            optimizer.zero_grad()
            loss = criterion(module(tensor), tensor)
            loss.backward()
            optimizer.step()
    module.eval()
    return AutoencoderModel(
        version=_BUNDLE_VERSION,
        params_fingerprint=params.fingerprint,
        feature_names=tuple(params.features),
        seq_len=params.seq_len,
        hidden_size=params.hidden_size,
        latent_size=params.latent_size,
        num_layers=params.num_layers,
        module=module,
        train_rows=int(sequences.shape[0]),
    )


@dataclass(frozen=True)
class AutoencoderMetric:
    """Separation of reconstruction error between normal and attack sequences."""

    eval_rows: int
    roc_auc: float
    normal_mean_error: float
    attack_mean_error: float
    separation: float  # attack_mean_error - normal_mean_error


def evaluate_autoencoder(
    model: AutoencoderModel, samples: Sequence[AuthSequenceSample]
) -> AutoencoderMetric:
    """Score labeled sequences and report ROC-AUC + the normal/attack error gap."""
    labels = [sample.label for sample in samples]
    if len(set(labels)) < 2:
        raise AutoencoderError("evaluation needs both normal and attack sequences")
    scores = model.score_many(sequences_to_array(samples))
    normal = [s for s, label in zip(scores, labels, strict=True) if label == _LABEL_NORMAL]
    attack = [s for s, label in zip(scores, labels, strict=True) if label == _LABEL_ATTACK]
    normal_mean = float(np.mean(normal))
    attack_mean = float(np.mean(attack))
    return AutoencoderMetric(
        eval_rows=len(samples),
        roc_auc=float(roc_auc_score(labels, scores)),
        normal_mean_error=normal_mean,
        attack_mean_error=attack_mean,
        separation=attack_mean - normal_mean,
    )


def autoencoder_holdout_metrics(
    params: AutoencoderParamsConfig, *, holdout_fraction: float = 0.2
) -> AutoencoderMetric:
    """Train on an earlier slice of normal sessions, score held-out normal + all attack."""
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be strictly between 0 and 1")
    samples = generate_all_auth_sequences(params)
    normal = [sample for sample in samples if sample.label == _LABEL_NORMAL]
    attack = [sample for sample in samples if sample.label == _LABEL_ATTACK]
    cut = int(len(normal) * (1.0 - holdout_fraction))
    model = fit_autoencoder(sequences_to_array(normal[:cut]), params=params)
    return evaluate_autoencoder(model, (*normal[cut:], *attack))


def save_autoencoder_model(directory: Path, model: AutoencoderModel) -> None:
    """Persist the module weights (torch state_dict) plus a JSON provenance manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    torch.save(model.module.state_dict(), directory / _STATE_NAME)
    manifest = {
        "version": model.version,
        "params_fingerprint": model.params_fingerprint,
        "feature_names": list(model.feature_names),
        "seq_len": model.seq_len,
        "hidden_size": model.hidden_size,
        "latent_size": model.latent_size,
        "num_layers": model.num_layers,
        "train_rows": model.train_rows,
        "state": _STATE_NAME,
    }
    (directory / _MANIFEST_NAME).write_text(
        json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_autoencoder_model(directory: Path) -> AutoencoderModel:
    """Load a saved autoencoder bundle, reconstructing the module from its manifest."""
    manifest_path = directory / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise AutoencoderBundleError(f"no autoencoder bundle manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state_path = directory / manifest["state"]
    if not state_path.is_file():
        raise AutoencoderBundleError(f"no autoencoder weights at {state_path}")
    feature_names = tuple(str(name) for name in manifest["feature_names"])
    module = _SequenceAutoencoder(
        n_features=len(feature_names),
        hidden_size=int(manifest["hidden_size"]),
        latent_size=int(manifest["latent_size"]),
        num_layers=int(manifest["num_layers"]),
    )
    # weights_only=True refuses to unpickle anything but tensors — safe on load.
    module.load_state_dict(torch.load(state_path, weights_only=True))
    module.eval()
    return AutoencoderModel(
        version=int(manifest["version"]),
        params_fingerprint=str(manifest["params_fingerprint"]),
        feature_names=feature_names,
        seq_len=int(manifest["seq_len"]),
        hidden_size=int(manifest["hidden_size"]),
        latent_size=int(manifest["latent_size"]),
        num_layers=int(manifest["num_layers"]),
        module=module,
        train_rows=int(manifest["train_rows"]),
    )


def load_autoencoder_model_or_none(directory: Path) -> AutoencoderModel | None:
    """Load a bundle if present, else None so callers can skip the anomaly vote."""
    if not (directory / _MANIFEST_NAME).is_file():
        return None
    return load_autoencoder_model(directory)
