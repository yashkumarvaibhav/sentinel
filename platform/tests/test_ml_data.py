"""Dataset assembly: determinism, provenance and held-out isolation."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from common.config import load_config
from ml.config import MlTrainingConfig, load_training_config
from ml.data import (
    TrainingDataError,
    build_training_dataset,
    discover_baseline_captures,
    extract_capture_frames,
    held_out_index,
)
from ml.frames import frames_data_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
TRAINING_CONFIG = CONFIG_ROOT / "ml-training.yml"
CAPTURES_ROOT = REPO_ROOT / "var" / "captures"


def _training_config() -> MlTrainingConfig:
    return load_training_config(TRAINING_CONFIG)


def _training_document() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load(TRAINING_CONFIG.read_text(encoding="utf-8")))


def test_held_out_index_covers_every_profile() -> None:
    index = held_out_index(REPO_ROOT)
    assert ("quiet_day", 7901) in index.scenario_seed_pairs
    assert ("match_night", 8923) in index.scenario_seed_pairs
    assert ("combo_night", 9439) in index.scenario_seed_pairs
    assert ("cascade_night", 9403) in index.scenario_seed_pairs
    # every development seed is absent from the held-out index
    assert 101 not in index.seed_values
    assert 40100 not in index.seed_values


def test_synthetic_only_build_is_deterministic() -> None:
    config = load_config(CONFIG_ROOT)
    training = _training_config()
    first = build_training_dataset(repo_root=REPO_ROOT, training_config=training, config=config)
    second = build_training_dataset(repo_root=REPO_ROOT, training_config=training, config=config)
    assert first.manifest.data_hash == second.manifest.data_hash
    assert first.manifest.canonical_bytes() == second.manifest.canonical_bytes()
    assert first.manifest.data_hash == frames_data_hash(first.frames)


def test_synthetic_only_manifest_counts_and_honesty() -> None:
    config = load_config(CONFIG_ROOT)
    dataset = build_training_dataset(
        repo_root=REPO_ROOT, training_config=_training_config(), config=config
    )
    manifest = dataset.manifest
    assert manifest.row_counts["capture"] == 0
    assert manifest.row_counts["synthetic"] == manifest.row_counts["total"] == len(dataset.frames)
    assert manifest.honesty_counts["REAL"] == 0
    assert manifest.honesty_counts["SIMULATED"] == len(dataset.frames)
    assert manifest.capture_sources == ()
    assert manifest.detector_config_fingerprint == config.fingerprint


def test_synthetic_seed_colliding_with_held_out_is_rejected() -> None:
    config = load_config(CONFIG_ROOT)
    document = _training_document()
    # inject a real held-out seed (quiet_day 7901) into the synthetic pool
    document["history"]["seeds"] = [40100, 7901]
    leaking = MlTrainingConfig.model_validate(document)
    with pytest.raises(TrainingDataError, match="held-out"):
        build_training_dataset(repo_root=REPO_ROOT, training_config=leaking, config=config)


def test_discover_on_absent_directory_is_empty() -> None:
    assert discover_baseline_captures(REPO_ROOT / "var" / "does-not-exist", REPO_ROOT) == ()


# --- capture-dependent (skip when the local captures are not present) ----------


def _require_capture(name: str) -> Path:
    root = CAPTURES_ROOT / name
    if not root.is_dir():
        pytest.skip(f"local capture {name} is not present")
    return root


def test_extract_accepts_a_development_baseline_capture() -> None:
    root = _require_capture("phase1-quiet-101-golden-v1")
    config = load_config(CONFIG_ROOT)
    extraction = extract_capture_frames(
        root,
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
        held_out=held_out_index(REPO_ROOT),
    )
    assert extraction.frames
    assert extraction.source.scenario_id == "quiet_day"
    assert extraction.source.seed == 101
    for frame in extraction.frames:
        assert frame.source == "capture"
        assert frame.honesty == "REAL"
        assert frame.seed_purpose == "development"
        assert frame.signal_key == "frontend.request_rate"


def test_extract_refuses_a_held_out_capture() -> None:
    root = _require_capture("phase1-quiet-7901-v2")
    config = load_config(CONFIG_ROOT)
    with pytest.raises(TrainingDataError):
        extract_capture_frames(
            root,
            detector=config.detectors,
            replay_config_fingerprint=config.fingerprint,
            held_out=held_out_index(REPO_ROOT),
        )


def test_discovery_selects_dev_baselines_and_skips_held_out_and_faults() -> None:
    if not CAPTURES_ROOT.is_dir():
        pytest.skip("local captures directory is not present")
    discovered = discover_baseline_captures(CAPTURES_ROOT, REPO_ROOT)
    names = {path.name for path in discovered}
    # a held-out quiet capture and any fault/attack capture must never be selected
    assert "phase1-quiet-7901-v2" not in names
    assert "phase1-quiet-7919-v2" not in names
    assert not any("attack" in name or "cascade" in name or "combo" in name for name in names)
    # every selected capture is a clean-baseline development quiet capture
    for path in discovered:
        assert (CAPTURES_ROOT / path.name / "manifest.json").is_file()
