"""Assemble the labeled training dataset: synthetic history + dev baselines.

The dataset is the union of the seeded synthetic history and clean baseline
windows extracted from development captures. Two isolation rules are enforced
fail-closed: no held-out seed may enter training (checked against every live
scenario profile), and the capture extractor never opens a private label
artifact — it reads only the label-free public decomposition replay.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lab.captures import load_runtime_capture, replay_decomposition
from lab.captures.models import CaptureManifest
from lab.scenarios import load_profile
from pydantic import ValidationError

from common.config import DetectorConfig, SentinelConfig, load_config
from contracts import ContextWindow
from ml.config import MlTrainingConfig, load_training_config
from ml.features import (
    AWARE_FEATURES,
    EVENT_FEATURES,
    TEMPORAL_FEATURES,
    ActiveEvent,
    aware_features,
)
from ml.frames import (
    TrainingFrame,
    canonical_frames_bytes,
    frames_data_hash,
    sorted_frames,
)
from ml.synthetic import generate_all_synthetic_history

TRAINING_DATASET_VERSION = 1
_SCENARIOS_SUBPATH = ("lab", "scenarios")


class TrainingDataError(ValueError):
    """A dataset could not be assembled without violating an isolation rule."""


@dataclass(frozen=True)
class HeldOutIndex:
    """Every held-out (scenario, seed) pair and seed value across all profiles."""

    scenario_seed_pairs: frozenset[tuple[str, int]]
    seed_values: frozenset[int]


@dataclass(frozen=True)
class CaptureSource:
    """Provenance for one development capture folded into the dataset."""

    capture_id: str
    scenario_id: str
    seed: int
    rows: int


@dataclass(frozen=True)
class CaptureExtraction:
    source: CaptureSource
    frames: tuple[TrainingFrame, ...]


@dataclass(frozen=True)
class DatasetManifest:
    version: int
    training_config_fingerprint: str
    detector_config_fingerprint: str
    feature_schema: dict[str, tuple[str, ...]]
    synthetic_seeds: tuple[int, ...]
    capture_sources: tuple[CaptureSource, ...]
    row_counts: dict[str, int]
    honesty_counts: dict[str, int]
    data_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "training_config_fingerprint": self.training_config_fingerprint,
            "detector_config_fingerprint": self.detector_config_fingerprint,
            "feature_schema": {name: list(values) for name, values in self.feature_schema.items()},
            "synthetic_seeds": list(self.synthetic_seeds),
            "capture_sources": [
                {
                    "capture_id": source.capture_id,
                    "scenario_id": source.scenario_id,
                    "seed": source.seed,
                    "rows": source.rows,
                    "honesty": "REAL",
                }
                for source in self.capture_sources
            ],
            "row_counts": self.row_counts,
            "honesty_counts": self.honesty_counts,
            "data_hash": self.data_hash,
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_dict(),
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


@dataclass(frozen=True)
class TrainingDataset:
    frames: tuple[TrainingFrame, ...]
    manifest: DatasetManifest


def held_out_index(repo_root: Path) -> HeldOutIndex:
    """Collect every held-out seed from all committed scenario profiles."""
    scenarios_dir = repo_root.joinpath(*_SCENARIOS_SUBPATH)
    pairs: set[tuple[str, int]] = set()
    values: set[int] = set()
    for path in sorted(scenarios_dir.glob("*.yml")):
        profile = load_profile(path)
        for seed in profile.seeds.held_out:
            pairs.add((profile.scenario_id, seed))
            values.add(seed)
    return HeldOutIndex(frozenset(pairs), frozenset(values))


def _clean_baseline_profile(repo_root: Path, scenario_id: str) -> bool:
    """True only for a scenario that injects no residual, symptom or fault stimulus."""
    path = repo_root.joinpath(*_SCENARIOS_SUBPATH, f"{scenario_id}.yml")
    if not path.is_file():
        return False
    profile = load_profile(path)
    return not (profile.residual_labels or profile.symptom_labels or profile.stimuli)


def _try_read_manifest(root: Path) -> CaptureManifest | None:
    """Parse a capture manifest, or None if it does not match the current schema."""
    try:
        return CaptureManifest.model_validate_json((root / "manifest.json").read_bytes())
    except (OSError, ValidationError):
        return None


def discover_baseline_captures(captures_root: Path, repo_root: Path) -> tuple[Path, ...]:
    """Find development captures from clean-baseline scenarios, reading manifests only.

    Foreign or older-schema captures sharing the directory are skipped, not fatal:
    discovery only ever selects a current-schema, clean-baseline development capture.
    """
    if not captures_root.is_dir():
        return ()
    found: list[Path] = []
    for path in sorted(captures_root.iterdir()):
        if not (path / "manifest.json").is_file():
            continue
        manifest = _try_read_manifest(path)
        if manifest is None or manifest.seed_purpose != "development":
            continue
        if not _clean_baseline_profile(repo_root, manifest.scenario_id):
            continue
        found.append(path)
    return tuple(found)


def _active_events(
    contexts: Sequence[ContextWindow],
    ts: datetime,
    signal_key: str,
) -> tuple[ActiveEvent, ...]:
    """Trusted events covering this tick, mirroring the decomposition's lift filter."""
    return tuple(
        ActiveEvent(multiplier=multiplier, trust_score=context.trust_score)
        for context in contexts
        if context.valid_from <= ts < context.valid_to
        and (multiplier := context.expected_delta.get(signal_key)) is not None
        and multiplier > 1.0
        and context.trust_score > 0.0
    )


def extract_capture_frames(
    root: Path,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
    held_out: HeldOutIndex,
) -> CaptureExtraction:
    """Extract label-free baseline frames from one development capture, fail-closed."""
    capture = load_runtime_capture(root)
    manifest = capture.manifest
    if manifest.seed_purpose != "development":
        raise TrainingDataError(
            f"training refuses a non-development capture: {manifest.capture_id} "
            f"({manifest.seed_purpose})"
        )
    if (manifest.scenario_id, manifest.seed) in held_out.scenario_seed_pairs:
        raise TrainingDataError(
            f"training refuses a held-out seed: {manifest.scenario_id}/{manifest.seed}"
        )
    if manifest.seed in held_out.seed_values:
        raise TrainingDataError(f"training refuses a held-out seed value: {manifest.seed}")
    replay = replay_decomposition(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    signal_key = f"{manifest.telemetry.logical_service}.{manifest.telemetry.logical_signal}"
    frames = tuple(
        TrainingFrame(
            signal_key=signal_key,
            ts=step.observation.ts,
            value=step.observation.value,
            features=aware_features(
                step.observation.ts,
                _active_events(replay.contexts, step.observation.ts, signal_key),
            ),
            source="capture",
            honesty="REAL",
            origin_seed=manifest.seed,
            scenario_id=manifest.scenario_id,
            seed_purpose="development",
        )
        for step in replay.steps
    )
    source = CaptureSource(
        capture_id=manifest.capture_id,
        scenario_id=manifest.scenario_id,
        seed=manifest.seed,
        rows=len(frames),
    )
    return CaptureExtraction(source=source, frames=frames)


def build_training_dataset(
    *,
    repo_root: Path,
    training_config: MlTrainingConfig,
    config: SentinelConfig,
    capture_roots: Sequence[Path] = (),
) -> TrainingDataset:
    """Union the synthetic history with development baselines under isolation rules."""
    held_out = held_out_index(repo_root)
    leaking = sorted(set(training_config.history.seeds) & held_out.seed_values)
    if leaking:
        raise TrainingDataError(f"synthetic seeds overlap held-out seeds: {leaking}")

    synthetic = generate_all_synthetic_history(training_config)
    extractions = tuple(
        extract_capture_frames(
            root,
            detector=config.detectors,
            replay_config_fingerprint=config.fingerprint,
            held_out=held_out,
        )
        for root in capture_roots
    )
    capture_frames = tuple(frame for extraction in extractions for frame in extraction.frames)
    frames = sorted_frames((*synthetic, *capture_frames))

    honesty = Counter(frame.honesty for frame in frames)
    manifest = DatasetManifest(
        version=TRAINING_DATASET_VERSION,
        training_config_fingerprint=training_config.fingerprint,
        detector_config_fingerprint=config.fingerprint,
        feature_schema={
            "aware": AWARE_FEATURES,
            "temporal": TEMPORAL_FEATURES,
            "event": EVENT_FEATURES,
        },
        synthetic_seeds=training_config.history.seeds,
        capture_sources=tuple(extraction.source for extraction in extractions),
        row_counts={
            "synthetic": len(synthetic),
            "capture": len(capture_frames),
            "total": len(frames),
        },
        honesty_counts={
            "SIMULATED": honesty.get("SIMULATED", 0),
            "REAL": honesty.get("REAL", 0),
        },
        data_hash=frames_data_hash(frames),
    )
    return TrainingDataset(frames=frames, manifest=manifest)


def write_training_dataset(directory: Path, dataset: TrainingDataset) -> None:
    """Write the canonical dataset rows and its committed manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "data.jsonl").write_bytes(canonical_frames_bytes(dataset.frames))
    (directory / "manifest.json").write_bytes(dataset.manifest.canonical_bytes())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ml.data")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--captures-root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    training_config = load_training_config(repo_root / "config" / "ml-training.yml")
    config = load_config(repo_root / "config")
    capture_roots = (
        discover_baseline_captures(args.captures_root.resolve(), repo_root)
        if args.captures_root is not None
        else ()
    )
    dataset = build_training_dataset(
        repo_root=repo_root,
        training_config=training_config,
        config=config,
        capture_roots=capture_roots,
    )
    write_training_dataset(args.out.resolve(), dataset)
    manifest = dataset.manifest
    print(f"training dataset written to {args.out.resolve()}", flush=True)
    print(f"  data_hash: {manifest.data_hash}", flush=True)
    print(f"  rows: {manifest.row_counts}", flush=True)
    print(f"  honesty: {manifest.honesty_counts}", flush=True)
    print(
        "  capture sources: "
        + (
            ", ".join(f"{s.scenario_id}/{s.seed}({s.rows})" for s in manifest.capture_sources)
            or "none"
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
