"""Scorer-only application of private labels to deterministic capture transcripts."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from common.config import DetectorConfig, load_config
from contracts import SymptomEpisode, SymptomKind
from detection.pipeline import SymptomEpisodePipeline
from lab.captures import (
    DecompositionReplay,
    load_private_labels,
    load_runtime_capture,
    replay_decomposition,
)
from lab.captures.edge_transcript import replay_edge_detection
from lab.captures.liveness_transcript import replay_liveness_detection
from lab.captures.log_transcript import replay_log_detection
from lab.captures.ratio_transcript import replay_ingress_ratios
from lab.captures.resource_transcript import replay_resource_detection
from lab.scenarios import load_profile
from lab.scenarios.models import LabModel, ResidualLabelInterval, SymptomLabelInterval
from lab.scoring.evaluator import EpisodeRunScore, RunScore, score_symptom_episodes
from lab.scoring.gates import evaluate_gates, evaluate_symptom_gates, load_gate_config
from lab.scoring.metrics import binary_metrics
from lab.scoring.report import render_report, render_symptom_report


class CaptureLabels(LabModel):
    version: Literal[1]
    scenario_id: str = Field(min_length=1, max_length=128)
    seed: int
    seed_purpose: Literal["development", "held_out"]
    intervals: tuple[ResidualLabelInterval, ...]
    symptom_intervals: tuple[SymptomLabelInterval, ...] = ()


@dataclass(frozen=True)
class DetectionEpisodeReplay:
    """The label-free episode stream from one capture's six detector paths.

    Everything here is derived purely from public capture telemetry; no private
    label artifact has been opened. Both the per-symptom scorer and the
    characterization diagnostic build on this so the runtime replay is identical.
    """

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    anchor_ts: datetime
    evaluation_end_ts: datetime
    logical_service: str
    logical_signal: str
    episodes: tuple[SymptomEpisode, ...]


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
    labels = _capture_labels(
        private_labels,
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
    )
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


def score_residual_episode_capture(
    root: Path,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> EpisodeRunScore:
    """Finish public decomposition/episode replay before opening private labels."""
    capture = load_runtime_capture(root)
    replay = replay_decomposition(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    pipeline = SymptomEpisodePipeline(configuration=detector.episodes)
    revisions: list[SymptomEpisode] = []
    for step in replay.steps:
        if step.frame is None:
            continue
        transition = pipeline.observe_frame(step.frame)
        if transition.episode is not None:
            revisions.append(transition.episode)
    revisions.extend(pipeline.active_episodes())
    evaluation_end = replay.anchor_ts + timedelta(
        seconds=len(replay.steps) * capture.manifest.telemetry.tick_seconds
    )

    # This is deliberately after the complete runtime replay above.
    labels = _capture_labels(
        load_private_labels(root),
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
    )
    residual_labels = tuple(
        SymptomLabelInterval(
            label_id=label.label_id,
            kind=SymptomKind.RESIDUAL_EXCEED.value,
            service=capture.manifest.telemetry.logical_service,
            signal=capture.manifest.telemetry.logical_signal,
            start_offset_seconds=float(label.start_offset_seconds),
            end_offset_seconds=float(label.end_offset_seconds),
        )
        for label in labels.intervals
    )
    explicit_residual = tuple(
        label
        for label in labels.symptom_intervals
        if label.kind == SymptomKind.RESIDUAL_EXCEED.value
    )
    return score_symptom_episodes(
        capture_id=replay.capture_id,
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
        anchor_ts=replay.anchor_ts,
        evaluation_end_ts=evaluation_end,
        episodes=tuple(revisions),
        labels=residual_labels + explicit_residual,
    )


def score_edge_episode_capture(
    root: Path,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> EpisodeRunScore:
    """Finish public edge/episode replay before opening private labels."""
    capture = load_runtime_capture(root)
    replay = replay_edge_detection(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    revisions = (
        tuple(
            result.transition.episode
            for step in replay.steps
            for result in step.results
            if result.transition is not None and result.transition.episode is not None
        )
        + replay.active_episodes
    )
    if not replay.steps:
        raise ValueError("edge replay produced no completed evaluation ticks")

    # This is deliberately after the complete runtime replay above.
    labels = _capture_labels(
        load_private_labels(root),
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
    )
    return score_symptom_episodes(
        capture_id=replay.capture_id,
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
        anchor_ts=replay.anchor_ts,
        evaluation_end_ts=replay.steps[-1].tick_ts,
        episodes=revisions,
        labels=tuple(
            label
            for label in labels.symptom_intervals
            if label.kind == SymptomKind.EDGE_DEGRADED.value
        ),
    )


def replay_detection_episodes(
    root: Path,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> DetectionEpisodeReplay:
    """Replay every public deterministic detector path into one episode stream.

    Runs decomposition + edge/log/ratio/liveness/resource replay and collects the
    durable episode revisions each emits. This is deliberately label-free: no
    private label artifact is opened, so the same stream can be scored or dumped
    for characterization without any risk of leakage.
    """
    capture = load_runtime_capture(root)
    decomposition = replay_decomposition(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    residual_pipeline = SymptomEpisodePipeline(configuration=detector.episodes)
    revisions: list[SymptomEpisode] = []
    for step in decomposition.steps:
        if step.frame is None:
            continue
        transition = residual_pipeline.observe_frame(step.frame)
        if transition.episode is not None:
            revisions.append(transition.episode)
    revisions.extend(residual_pipeline.active_episodes())

    edge = replay_edge_detection(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    logs = replay_log_detection(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    ratios = replay_ingress_ratios(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    liveness = replay_liveness_detection(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    resources = replay_resource_detection(
        capture,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )
    public_replays = (edge, logs, ratios, liveness, resources)
    if any(
        (
            replay.capture_id,
            replay.scenario_id,
            replay.seed,
            replay.seed_purpose,
            replay.anchor_ts,
        )
        != (
            decomposition.capture_id,
            decomposition.scenario_id,
            decomposition.seed,
            decomposition.seed_purpose,
            decomposition.anchor_ts,
        )
        for replay in public_replays
    ):
        raise ValueError("detector replay identities do not describe one capture")
    for replay in public_replays:
        revisions.extend(
            result.transition.episode
            for step in replay.steps
            for result in step.results
            if result.transition is not None and result.transition.episode is not None
        )
        revisions.extend(replay.active_episodes)
    evaluation_end = decomposition.anchor_ts + timedelta(
        seconds=len(liveness.steps) * capture.manifest.telemetry.tick_seconds
    )
    return DetectionEpisodeReplay(
        capture_id=decomposition.capture_id,
        scenario_id=decomposition.scenario_id,
        seed=decomposition.seed,
        seed_purpose=decomposition.seed_purpose,
        anchor_ts=decomposition.anchor_ts,
        evaluation_end_ts=evaluation_end,
        logical_service=capture.manifest.telemetry.logical_service,
        logical_signal=capture.manifest.telemetry.logical_signal,
        episodes=tuple(revisions),
    )


def score_detection_episode_capture(
    root: Path,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> EpisodeRunScore:
    """Replay every public deterministic path before applying private labels once."""
    replay = replay_detection_episodes(
        root,
        detector=detector,
        replay_config_fingerprint=replay_config_fingerprint,
    )

    # The complete decomposition + edge/log/ratio/liveness/resource runtime replay
    # above is label-free. Only the scorer crosses into the private artifact.
    labels = _capture_labels(
        load_private_labels(root),
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
    )
    residual_labels = tuple(
        SymptomLabelInterval(
            label_id=label.label_id,
            kind=SymptomKind.RESIDUAL_EXCEED.value,
            service=replay.logical_service,
            signal=replay.logical_signal,
            start_offset_seconds=float(label.start_offset_seconds),
            end_offset_seconds=float(label.end_offset_seconds),
        )
        for label in labels.intervals
    )
    return score_symptom_episodes(
        capture_id=replay.capture_id,
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
        anchor_ts=replay.anchor_ts,
        evaluation_end_ts=replay.evaluation_end_ts,
        episodes=replay.episodes,
        labels=residual_labels + labels.symptom_intervals,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.capture")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--captures-root", type=Path)
    parser.add_argument("--held-out-symptom-captures-root", type=Path)
    parser.add_argument("--development-residual-capture", type=Path)
    parser.add_argument("--development-edge-capture", type=Path)
    parser.add_argument("--development-combo-capture", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    development_paths = (
        args.development_residual_capture,
        args.development_edge_capture,
    )
    development_requested = any(path is not None for path in development_paths) or (
        args.development_combo_capture is not None
    )
    if args.held_out_symptom_captures_root is not None:
        if development_requested or args.captures_root is not None:
            parser.error(
                "held-out symptom scoring takes only --held-out-symptom-captures-root and --report"
            )
        return _held_out_symptom_main(
            repo_root=repo_root,
            captures_root=args.held_out_symptom_captures_root.resolve(),
            report_path=args.report.resolve(),
        )
    if development_requested:
        if (
            not all(path is not None for path in development_paths)
            or args.captures_root is not None
        ):
            parser.error(
                "development symptom scoring requires both development capture paths and no "
                "--captures-root"
            )
        return _development_symptom_main(
            repo_root=repo_root,
            residual_capture=args.development_residual_capture,
            edge_capture=args.development_edge_capture,
            combo_capture=args.development_combo_capture,
            report_path=args.report.resolve(),
        )
    if args.captures_root is None:
        parser.error("--captures-root is required for the held-out decomposition gate")
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


def _development_symptom_main(
    *,
    repo_root: Path,
    residual_capture: Path,
    edge_capture: Path,
    combo_capture: Path | None,
    report_path: Path,
) -> int:
    config = load_config(repo_root / "config")
    capture_paths = (residual_capture, edge_capture) + (
        () if combo_capture is None else (combo_capture,)
    )
    runs = tuple(
        score_detection_episode_capture(
            path.resolve(),
            detector=config.detectors,
            replay_config_fingerprint=config.fingerprint,
        )
        for path in capture_paths
    )
    if any(run.seed_purpose != "development" for run in runs):
        raise ValueError("development symptom proof cannot consume held-out captures")
    gate_config = load_gate_config(repo_root / "lab" / "scoring" / "config.yml")
    # A combined combo capture supplies honest labels for every runtime kind,
    # so its presence gates all seven; without it only the two proven kinds
    # are required, exactly as before.
    required_kinds = (
        (SymptomKind.RESIDUAL_EXCEED, SymptomKind.EDGE_DEGRADED) if combo_capture is None else None
    )
    gate = (
        evaluate_symptom_gates(
            runs, gate_config, required_kinds=required_kinds, topology=config.topology
        )
        if required_kinds is not None
        else evaluate_symptom_gates(runs, gate_config, topology=config.topology)
    )
    report = render_symptom_report(
        runs=runs,
        gate=gate,
        config=gate_config,
        config_fingerprint=config.fingerprint,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 0 if gate.passed else 1


_HELD_OUT_SYMPTOM_PROFILES = ("cascade_night", "combo_night")


def _held_out_symptom_matrix(repo_root: Path) -> frozenset[tuple[str, int]]:
    """The exact set of sealed (profile, seed) pairs the Phase 2 closure must score."""
    return frozenset(
        (profile_name, seed)
        for profile_name in _HELD_OUT_SYMPTOM_PROFILES
        for seed in load_profile(
            repo_root / "lab" / "scenarios" / f"{profile_name}.yml"
        ).seeds.held_out
    )


def _require_held_out_symptom_matrix(
    runs: tuple[EpisodeRunScore, ...],
    expected: frozenset[tuple[str, int]],
) -> None:
    """Fail closed unless the scored runs are exactly the sealed held-out seeds."""
    if any(run.seed_purpose != "held_out" for run in runs):
        raise ValueError("held-out symptom closure cannot consume development captures")
    actual = tuple((run.scenario_id, run.seed) for run in runs)
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError(
            "held-out symptom matrix must exactly match sealed seeds: "
            f"missing={sorted(expected - set(actual))}, extra={sorted(set(actual) - expected)}"
        )


def _held_out_symptom_main(
    *,
    repo_root: Path,
    captures_root: Path,
    report_path: Path,
) -> int:
    """Score every sealed held-out cascade/combo capture through the combined scorer once."""
    config = load_config(repo_root / "config")
    capture_dirs = tuple(
        sorted(
            path
            for path in captures_root.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
    )
    runs = tuple(
        sorted(
            (
                score_detection_episode_capture(
                    path,
                    detector=config.detectors,
                    replay_config_fingerprint=config.fingerprint,
                )
                for path in capture_dirs
            ),
            key=lambda item: (item.scenario_id, item.seed),
        )
    )
    _require_held_out_symptom_matrix(runs, _held_out_symptom_matrix(repo_root))
    gate_config = load_gate_config(repo_root / "lab" / "scoring" / "config.yml")
    gate = evaluate_symptom_gates(runs, gate_config, topology=config.topology)
    report = render_symptom_report(
        runs=runs,
        gate=gate,
        config=gate_config,
        config_fingerprint=config.fingerprint,
        mode="held_out",
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 0 if gate.passed else 1


def _is_labeled(offset: float, labels: tuple[ResidualLabelInterval, ...]) -> bool:
    return any(label.start_offset_seconds <= offset < label.end_offset_seconds for label in labels)


def _first_detection_latency(
    predicted_offsets: tuple[float, ...],
    *,
    start_offset: float,
    end_offset: float,
) -> float | None:
    first = next(
        (offset for offset in predicted_offsets if start_offset <= offset < end_offset),
        None,
    )
    return None if first is None else first - start_offset


def _capture_labels(
    private_labels: bytes,
    *,
    scenario_id: str,
    seed: int,
    seed_purpose: Literal["development", "held_out"],
) -> CaptureLabels:
    labels = CaptureLabels.model_validate_json(private_labels)
    if (
        labels.scenario_id != scenario_id
        or labels.seed != seed
        or labels.seed_purpose != seed_purpose
    ):
        raise ValueError("private label identity does not match capture transcript")
    return labels


if __name__ == "__main__":
    raise SystemExit(main())
