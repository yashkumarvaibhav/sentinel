"""Stable, honest Markdown proof for the Phase 1 decomposition gate."""

from __future__ import annotations

from typing import Literal

from lab.scoring.evaluator import EpisodeRunScore, RunScore, SymptomKindScore
from lab.scoring.gates import GateResult, ScoreGateConfig, SymptomGateResult
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


def render_symptom_report(
    *,
    runs: tuple[EpisodeRunScore, ...],
    gate: SymptomGateResult,
    config: ScoreGateConfig,
    config_fingerprint: str,
) -> str:
    """Render an honest development proof for episode-level symptom metrics."""
    required = set(gate.required_kinds)
    lines = [
        "# Phase 2 per-symptom episode scoring proof",
        "",
        f"**Development gate: {'PASS' if gate.passed else 'FAIL'}**",
        "",
        "- Telemetry evidence: **REAL** OpenTelemetry capture bytes replayed through the "
        "runtime detector and anti-flapping episode paths.",
        "- Workload, event context and injected faults: **SIMULATED** and bounded to the "
        "contained Astronomy Shop testbed.",
        "- Seed discipline: this proof uses **DEVELOPMENT** captures only. It neither runs nor "
        "reads held-out `cascade_night` / `combo_night` seeds.",
        "- Label discipline: public runtime replay completes before scorer-only private labels "
        "are opened. Predictions are matched one-to-one over half-open event-time intervals; "
        "retries of one `episode_id` count once.",
        f"- Runtime config fingerprint: `{config_fingerprint}`.",
        "",
        "## Development captures",
        "",
        "| Capture | Profile | Seed | Kind | Predicted | Expected | TP | FP | FN | Precision | "
        "Recall | Detect p50/p95 |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        visible = tuple(
            score for score in run.by_kind if score.predicted_count > 0 or score.expected_count > 0
        )
        for score in visible:
            lines.append(
                "| "
                + " | ".join(
                    (
                        run.capture_id,
                        run.scenario_id,
                        str(run.seed),
                        score.kind.value,
                        str(score.predicted_count),
                        str(score.expected_count),
                        str(score.metrics.true_positive),
                        str(score.metrics.false_positive),
                        str(score.metrics.false_negative),
                        _metric(score.metrics.precision),
                        _metric(score.metrics.recall),
                        _episode_latencies(score),
                    )
                )
                + " |"
            )

    lines.extend(
        [
            "",
            "## Per-kind development gates",
            "",
            "| Kind | Precision | Floor | Recall | Floor | Scope | Result |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for score in gate.by_kind:
        precision_floor = config.symptom_precision_min[score.kind.value]
        recall_floor = config.symptom_recall_min[score.kind.value]
        gated = score.kind in required
        passed = (
            gated
            and score.metrics.precision.value is not None
            and score.metrics.precision.value >= precision_floor
            and score.metrics.recall.value is not None
            and score.metrics.recall.value >= recall_floor
        )
        result = "PASS" if passed else ("FAIL" if gated else "NOT GATED")
        lines.append(
            f"| {score.kind.value} | {_metric(score.metrics.precision)} | "
            f">= {precision_floor:.3f} | {_metric(score.metrics.recall)} | "
            f">= {recall_floor:.3f} | {'required' if gated else 'pending evidence'} | "
            f"{result} |"
        )
    if gate.failures:
        lines.extend(["", "## Failures", ""])
        lines.extend(
            f"- `{failure.metric}` for `{failure.scope}`: {_optional(failure.actual)}; "
            f"requires {failure.requirement}."
            for failure in gate.failures
        )
    lines.extend(
        [
            "",
            "## Scope of this proof",
            "",
            "This development slice freezes the generic evaluator and the first measured "
            "RESIDUAL_EXCEED / EDGE_DEGRADED baselines. Kinds without an expected or predicted "
            "episode remain `insufficient`; they are not rendered as zero and are not "
            "release-gated until their development scenarios supply honest labels. The final "
            "Phase 2 gate still requires fresh held-out cascade/combo captures and every "
            "configured symptom kind.",
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


def _episode_latencies(score: SymptomKindScore) -> str:
    if score.detection_latency_p50 is None or score.detection_latency_p95 is None:
        return "insufficient"
    return f"{score.detection_latency_p50:.1f}s/{score.detection_latency_p95:.1f}s"


def _gate_row(name: str, metric: MetricValue, requirement: str, passed: bool) -> str:
    return f"| {name} | {_metric(metric)} | {requirement} | {'PASS' if passed else 'FAIL'} |"
