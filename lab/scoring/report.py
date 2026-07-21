"""Stable, honest Markdown proof for the Phase 1 decomposition gate."""

from __future__ import annotations

from typing import Literal

from lab.scoring.evaluator import RunScore
from lab.scoring.gates import GateResult, ScoreGateConfig
from lab.scoring.metrics import MetricValue


def render_report(
    *,
    runs: tuple[RunScore, ...],
    gate: GateResult,
    config: ScoreGateConfig,
    config_fingerprint: str,
    evidence_mode: Literal["live", "capture"] = "live",
) -> str:
    evidence = (
        "- Telemetry evidence: **REAL** OpenTelemetry ingress spans from the contained "
        "Astronomy Shop testbed, read back from ClickHouse after Collector -> Redpanda -> "
        "ingest normalization."
        if evidence_mode == "live"
        else "- Telemetry evidence: **REAL** OpenTelemetry ingress spans recorded at exact "
        "raw-topic offsets from the contained Astronomy Shop testbed, checksum-verified and "
        "re-normalized during bit-exact replay."
    )
    lines = [
        "# Phase 1 decomposition scoring proof",
        "",
        f"**Gate: {'PASS' if gate.passed else 'FAIL'}**",
        "",
        evidence,
        "- Workload and event context: **SIMULATED**, deterministic, seed-controlled and "
        "capped at 50 requests/s inside the testbed namespace.",
        "- Seed discipline: every row below is from the committed **HELD-OUT** set; "
        "development tests use disjoint seeds.",
        "- Label discipline: the engine receives only `Observation` plus `ContextWindow`; "
        "private residual intervals are applied afterward by `lab/scoring`.",
        f"- Runtime config fingerprint: `{config_fingerprint}`.",
        "",
        "## Held-out runs",
        "",
        "| Profile | Seed | Real spans | Complete | Ticks | TP | FP | FN | TN | Precision | "
        "Recall | FP rate | Detect p50/p95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        lines.append(
            "| "
            + " | ".join(
                (
                    run.scenario_id,
                    str(run.seed),
                    f"{run.actual_span_count}/{run.expected_span_count}",
                    f"{run.telemetry_completeness:.3f}",
                    str(run.sample_count),
                    str(run.metrics.true_positive),
                    str(run.metrics.false_positive),
                    str(run.metrics.false_negative),
                    str(run.metrics.true_negative),
                    _metric(run.metrics.precision),
                    _metric(run.metrics.recall),
                    _metric(run.metrics.false_positive_rate),
                    _latencies(run),
                )
            )
            + " |"
        )
    minimum_completeness = min((run.telemetry_completeness for run in runs), default=0.0)
    completeness_passed = bool(runs) and all(
        run.telemetry_completeness >= config.telemetry_completeness_min for run in runs
    )
    lines.extend(
        [
            "",
            "## Gate summary",
            "",
            "| Metric | Actual | Requirement | Result |",
            "|---|---:|---:|---|",
            _gate_row(
                "Residual precision",
                gate.overall.precision,
                f">= {config.residual_precision_min:.3f}",
                gate.overall.precision.value is not None
                and gate.overall.precision.value >= config.residual_precision_min,
            ),
            _gate_row(
                "Residual recall",
                gate.overall.recall,
                f">= {config.residual_recall_min:.3f}",
                gate.overall.recall.value is not None
                and gate.overall.recall.value >= config.residual_recall_min,
            ),
            _gate_row(
                "Quiet-day false-positive rate",
                gate.quiet_day.false_positive_rate,
                f"<= {config.quiet_day_false_positive_rate_max:.3f}",
                gate.quiet_day.false_positive_rate.value is not None
                and gate.quiet_day.false_positive_rate.value
                <= config.quiet_day_false_positive_rate_max,
            ),
            f"| Per-run telemetry completeness | {minimum_completeness:.3f} | "
            f">= {config.telemetry_completeness_min:.3f} | "
            f"{'PASS' if completeness_passed else 'FAIL'} |",
        ]
    )
    if gate.failures:
        lines.extend(["", "## Failures", ""])
        lines.extend(
            f"- `{failure.metric}` in `{failure.scope}`: {_optional(failure.actual)}; "
            f"requires {failure.requirement}."
            for failure in gate.failures
        )
    lines.extend(
        [
            "",
            "## Scope of this proof",
            "",
            "This Phase 1 gate scores residual extraction only: event-explained volume, "
            "injected unexplained offset, and quiet-day false positives. Decision, reason, "
            "origin, action, calibration and resolution metrics remain `insufficient` until "
            "their owning phases land; no zeroes are fabricated for them.",
            "",
        ]
    )
    return "\n".join(lines)


def _metric(metric: MetricValue) -> str:
    return "insufficient" if metric.value is None else f"{metric.value:.3f}"


def _optional(value: float | None) -> str:
    return "insufficient" if value is None else f"{value:.3f}"


def _latencies(run: RunScore) -> str:
    if run.detection_latency_p50 is None or run.detection_latency_p95 is None:
        return "insufficient"
    return f"{run.detection_latency_p50:.1f}s/{run.detection_latency_p95:.1f}s"


def _gate_row(name: str, metric: MetricValue, requirement: str, passed: bool) -> str:
    return f"| {name} | {_metric(metric)} | {requirement} | {'PASS' if passed else 'FAIL'} |"
