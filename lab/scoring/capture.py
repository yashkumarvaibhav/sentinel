"""Scorer-only application of private labels to deterministic capture transcripts."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field

from common.config import DetectorConfig, load_config
from lab.captures import (
    DecompositionReplay,
    load_private_labels,
    load_runtime_capture,
    replay_decomposition,
)
from lab.scenarios import load_profile
from lab.scenarios.models import LabModel, ResidualLabelInterval, SymptomLabelInterval
from lab.scoring.evaluator import RunScore
from lab.scoring.gates import evaluate_gates, load_gate_config
from lab.scoring.metrics import binary_metrics
from lab.scoring.report import render_report


class CaptureLabels(LabModel):
    version: Literal[1]
    scenario_id: str = Field(min_length=1, max_length=128)
    seed: int
    seed_purpose: Literal["held_out"]
    intervals: tuple[ResidualLabelInterval, ...]
    symptom_intervals: tuple[SymptomLabelInterval, ...] = ()


def score_capture(
    root: Path,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> RunScore:
    replay = replay_decomposition(
        load_runtime_capture(root),
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    return score_decomposition_replay(replay, private_labels=load_private_labels(root))


def score_decomposition_replay(
    replay: DecompositionReplay,
    *,
    private_labels: bytes,
) -> RunScore:
    labels = CaptureLabels.model_validate_json(private_labels)
    if labels.scenario_id != replay.scenario_id or labels.seed != replay.seed:
        raise ValueError("private label identity does not match capture transcript")
    scored = tuple(item for item in replay.steps if item.frame is not None)
    offsets = tuple((item.observation.ts - replay.anchor_ts).total_seconds() for item in scored)
    predicted = tuple(item.frame.residual_score > 0.0 for item in scored if item.frame is not None)
    expected = tuple(_is_labeled(offset, labels.intervals) for offset in offsets)
    predicted_offsets = tuple(
        offset for offset, prediction in zip(offsets, predicted, strict=True) if prediction
    )
    latencies = tuple(
        latency
        for label in labels.intervals
        if (
            latency := _first_detection_latency(
                predicted_offsets,
                start_offset=label.start_offset_seconds,
                end_offset=label.end_offset_seconds,
            )
        )
        is not None
    )
    return RunScore(
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=labels.seed_purpose,
        sample_count=len(predicted),
        expected_span_count=replay.expected_span_count,
        actual_span_count=replay.actual_span_count,
        telemetry_completeness=replay.telemetry_completeness,
        predicted=predicted,
        expected=expected,
        metrics=binary_metrics(predicted=predicted, expected=expected),
        detection_latency_seconds=latencies,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.capture")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--captures-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    captures = tuple(
        sorted(
            path
            for path in args.captures_root.resolve().iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
    )
    expected = {
        (profile_name, seed)
        for profile_name in ("quiet_day", "match_night")
        for seed in load_profile(
            repo_root / "lab" / "scenarios" / f"{profile_name}.yml"
        ).seeds.held_out
    }
    config = load_config(repo_root / "config")
    runs = tuple(
        sorted(
            (
                score_capture(
                    root,
                    detector=config.detectors,
                    replay_config_fingerprint=config.fingerprint,
                )
                for root in captures
            ),
            key=lambda item: (item.scenario_id, item.seed),
        )
    )
    actual = [(run.scenario_id, run.seed) for run in runs]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError(
            f"capture matrix must exactly match held-out seeds: "
            f"missing={sorted(expected - set(actual))}, extra={sorted(set(actual) - expected)}"
        )
    gate_config = load_gate_config(repo_root / "lab" / "scoring" / "config.yml")
    gate = evaluate_gates(runs, gate_config)
    report = render_report(
        runs=runs,
        gate=gate,
        config=gate_config,
        config_fingerprint=config.fingerprint,
        evidence_mode="capture",
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 0 if gate.passed else 1


def _is_labeled(offset: float, labels: tuple[ResidualLabelInterval, ...]) -> bool:
    return any(label.start_offset_seconds <= offset < label.end_offset_seconds for label in labels)


def _first_detection_latency(
    predicted_offsets: tuple[float, ...],
    *,
    start_offset: int,
    end_offset: int,
) -> float | None:
    first = next(
        (offset for offset in predicted_offsets if start_offset <= offset < end_offset),
        None,
    )
    return None if first is None else first - start_offset


if __name__ == "__main__":
    raise SystemExit(main())
