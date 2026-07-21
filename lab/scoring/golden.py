"""Reviewed semantic regression gate over development-seed captures."""

from __future__ import annotations

import argparse
import difflib
import json
from collections.abc import Sequence
from pathlib import Path

from common.config import load_config
from lab.captures import DecompositionReplay, load_runtime_capture, replay_decomposition
from lab.scenarios import load_profile

_PROFILES = ("match_night", "quiet_day")


def semantic_transcript(replay: DecompositionReplay) -> bytes:
    """Remove capture-clock identities while retaining every Phase 1 behavior."""
    contexts: list[dict[str, object]] = []
    for context in replay.contexts:
        contexts.append(
            {
                "context_id": context.context_id,
                "event_type": context.event_type,
                "expected_delta": dict(sorted(context.expected_delta.items())),
                "honesty": context.honesty,
                "name": context.name,
                "source": context.source,
                "start_offset_seconds": (context.valid_from - replay.anchor_ts).total_seconds(),
                "end_offset_seconds": (context.valid_to - replay.anchor_ts).total_seconds(),
                "trust_score": context.trust_score,
            }
        )
    steps: list[dict[str, object]] = []
    for tick, step in enumerate(replay.steps):
        frame: dict[str, object] | None = None
        if step.frame is not None:
            frame = {
                "band_high": step.frame.band_high,
                "band_low": step.frame.band_low,
                "context_ids": step.frame.context_ids,
                "explained_base": step.frame.explained_base,
                "explained_event": step.frame.explained_event,
                "observed": step.frame.observed,
                "residual": step.frame.residual,
                "residual_score": step.frame.residual_score,
                "service": step.frame.service,
                "signal": step.frame.signal,
            }
        steps.append(
            {
                "baseline_updated": step.baseline_updated,
                "evidence_attributes": dict(sorted(step.observation.attributes.items())),
                "frame": frame,
                "observed": step.observation.value,
                "service": step.observation.service,
                "signal": step.observation.signal,
                "status": step.status,
                "tick": tick,
                "unit": step.observation.unit,
            }
        )
    value = {
        "capture": {
            "actual_span_count": replay.actual_span_count,
            "capture_id": replay.capture_id,
            "expected_span_count": replay.expected_span_count,
            "scenario_id": replay.scenario_id,
            "seed": replay.seed,
            "seed_purpose": replay.seed_purpose,
            "stimulus_honesty": "SIMULATED",
            "telemetry_completeness": replay.telemetry_completeness,
            "telemetry_honesty": "REAL",
        },
        "contexts": contexts,
        "scope": "phase-1-decomposition",
        "stats": {
            "baseline_updates": replay.stats.baseline_updates,
            "context_blocked_warmup": replay.stats.context_blocked_warmup,
            "decomposed": replay.stats.decomposed,
            "replays": replay.stats.replays,
            "warming": replay.stats.warming,
        },
        "steps": steps,
        "version": 1,
    }
    return (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode()


def load_candidates(*, repo_root: Path, captures_root: Path) -> dict[str, bytes]:
    """Load exactly one committed development capture per Phase 1 profile."""
    roots = tuple(
        sorted(
            path
            for path in captures_root.resolve().iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
    )
    allowed = {
        profile_name: set(
            load_profile(repo_root / "lab" / "scenarios" / f"{profile_name}.yml").seeds.development
        )
        for profile_name in _PROFILES
    }
    config = load_config(repo_root / "config")
    candidates: dict[str, bytes] = {}
    for root in roots:
        capture = load_runtime_capture(root)
        manifest = capture.manifest
        if manifest.seed_purpose != "development":
            raise ValueError(f"golden capture is not a development seed: {manifest.capture_id}")
        if (
            manifest.scenario_id not in allowed
            or manifest.seed not in allowed[manifest.scenario_id]
        ):
            raise ValueError(
                f"golden capture is outside the committed development set: {manifest.capture_id}"
            )
        if manifest.scenario_id in candidates:
            raise ValueError(f"duplicate golden profile: {manifest.scenario_id}")
        replay = replay_decomposition(
            capture,
            detector=config.detectors,
            replay_config_fingerprint=config.fingerprint,
        )
        candidates[manifest.scenario_id] = semantic_transcript(replay)
    missing = sorted(set(_PROFILES) - set(candidates))
    if missing:
        raise ValueError(f"golden capture matrix is incomplete: missing={missing}")
    return candidates


def write_goldens(candidates: dict[str, bytes], goldens_root: Path) -> None:
    goldens_root.mkdir(parents=True, exist_ok=True)
    for profile_name, value in sorted(candidates.items()):
        (goldens_root / f"{profile_name}.json").write_bytes(value)


def check_goldens(candidates: dict[str, bytes], goldens_root: Path) -> None:
    expected_names = {f"{profile_name}.json" for profile_name in _PROFILES}
    actual_names = {path.name for path in goldens_root.glob("*.json")}
    if actual_names != expected_names:
        raise ValueError(
            "golden files must exactly match Phase 1 profiles: "
            f"missing={sorted(expected_names - actual_names)}, "
            f"extra={sorted(actual_names - expected_names)}"
        )
    for profile_name, actual in sorted(candidates.items()):
        path = goldens_root / f"{profile_name}.json"
        expected = path.read_bytes()
        if actual == expected:
            continue
        diff = "".join(
            difflib.unified_diff(
                expected.decode().splitlines(keepends=True),
                actual.decode().splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"replay:{profile_name}",
                n=3,
            )
        )
        raise ValueError(f"semantic golden changed for {profile_name}:\n{diff[:12000]}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.golden")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--captures-root", type=Path, required=True)
    parser.add_argument("--goldens-root", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    candidates = load_candidates(
        repo_root=repo_root,
        captures_root=args.captures_root,
    )
    if args.write:
        write_goldens(candidates, args.goldens_root.resolve())
        print(f"goldens regenerated: {', '.join(sorted(candidates))}")
    else:
        check_goldens(candidates, args.goldens_root.resolve())
        print(f"golden PASS: {len(candidates)} development capture transcripts unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
