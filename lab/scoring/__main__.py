"""Run every committed held-out decomposition profile against the live lab."""

from __future__ import annotations

import argparse
import secrets
from collections.abc import Sequence
from pathlib import Path

from common.config import load_config
from lab.scenarios import SeedPurpose, compile_profile, load_profile, write_artifacts
from lab.scoring.evaluator import RunScore, build_rate_observations, score_observations
from lab.scoring.gates import evaluate_gates, load_gate_config
from lab.scoring.live import expected_request_count, run_live_scenario
from lab.scoring.report import render_report

_PROFILES = ("quiet_day", "match_night")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    report_path = args.report.resolve()
    configuration = load_config(repo_root / "config")
    gate_config = load_gate_config(repo_root / "lab" / "scoring" / "config.yml")
    invocation = secrets.token_hex(4)
    scores: list[RunScore] = []
    for profile_name in _PROFILES:
        profile = load_profile(repo_root / "lab" / "scenarios" / f"{profile_name}.yml")
        for seed in profile.seeds.held_out:
            print(f"[score] {profile_name} held-out seed {seed}: compiling", flush=True)
            artifacts = compile_profile(profile, seed=seed, purpose=SeedPurpose.HELD_OUT)
            output = repo_root / "var" / "scoring" / invocation / profile_name / str(seed)
            write_artifacts(artifacts, output)
            print(f"[score] {profile_name} held-out seed {seed}: running live", flush=True)
            telemetry = run_live_scenario(
                repo_root=repo_root,
                artifacts=artifacts,
                invocation=invocation,
            )
            observations = build_rate_observations(
                profile=profile,
                run_id=f"{invocation}-{profile_name}-{seed}",
                span_timestamps=telemetry.span_timestamps,
                start_at=telemetry.start_at,
            )
            score = score_observations(
                profile=profile,
                artifacts=artifacts,
                observations=observations,
                detector=configuration.detectors,
                expected_span_count=expected_request_count(artifacts.schedule),
                actual_span_count=len(telemetry.span_timestamps),
            )
            scores.append(score)
            print(
                f"[score] {profile_name} held-out seed {seed}: "
                f"TP={score.metrics.true_positive} FP={score.metrics.false_positive} "
                f"FN={score.metrics.false_negative} "
                f"completeness={score.telemetry_completeness:.3f}",
                flush=True,
            )
    runs = tuple(scores)
    gate = evaluate_gates(runs, gate_config)
    report = render_report(
        runs=runs,
        gate=gate,
        config=gate_config,
        config_fingerprint=configuration.fingerprint,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 0 if gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
