"""Gate and report the decision-level score, and the CLI that produces the proof.

The scoring primitives live in ``decision_score`` and the floors in ``gates``;
this module is the one place that puts them together, so the dependency runs in
one direction only.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from common.config import load_config
from lab.scoring.decision_score import (
    HOSTILE_OUTCOMES,
    DecisionScore,
    load_capture_labels,
    score_decisions,
)
from lab.scoring.decisions import load_decision_configs, replay_capture_decisions
from lab.scoring.gates import GateFailure, ScoreGateConfig, check_minimum, load_gate_config
from lab.scoring.metrics import MetricValue, ratio


@dataclass(frozen=True)
class DecisionGateResult:
    passed: bool
    decision_accuracy: MetricValue
    decision_reason_accuracy: MetricValue
    attack_recall: MetricValue
    origin_accuracy: MetricValue
    false_act_count: int
    failures: tuple[GateFailure, ...]


def evaluate_decision_gates(
    scores: tuple[DecisionScore, ...],
    config: ScoreGateConfig,
) -> DecisionGateResult:
    """Gate what the platform concluded, and count what it did that it should not have.

    Every metric here is fail-closed on an empty population: a capture matrix
    that exercises no labelled window has not proved anything, so an absent
    denominator is a failure rather than a perfect score. The exception is the
    false-act count, which is a tally of real mistakes and is meaningful at
    zero - that is the only number whose "nothing happened" is the pass.
    """
    if not scores:
        raise ValueError("decision gating needs at least one scored capture")
    windows = tuple(outcome for score in scores for outcome in score.windows)
    hostile = tuple(
        outcome for outcome in windows if outcome.window.expectation in HOSTILE_OUTCOMES
    )
    graded_origins = tuple(outcome for outcome in windows if outcome.origin_correct is not None)
    accuracy = ratio(sum(1 for item in windows if item.handled_correctly), len(windows))
    reason = ratio(sum(1 for item in windows if item.reason_correct), len(windows))
    recall = ratio(sum(1 for item in hostile if item.recognised_as_hostile), len(hostile))
    origin = ratio(
        sum(1 for item in graded_origins if item.origin_correct),
        len(graded_origins),
    )
    false_acts = sum(score.false_act_count for score in scores)
    failures: list[GateFailure] = []
    check_minimum(failures, "decision_accuracy", accuracy, config.decision_accuracy_min, "overall")
    check_minimum(
        failures,
        "decision_reason_accuracy",
        reason,
        config.decision_reason_accuracy_min,
        "overall",
    )
    check_minimum(failures, "attack_recall", recall, config.attack_recall_min, "overall")
    check_minimum(failures, "origin_accuracy", origin, config.origin_accuracy_min, "overall")
    if false_acts > config.decision_false_acts_max:
        failures.append(
            GateFailure(
                metric="decision_false_acts",
                actual=float(false_acts),
                requirement=f"<={config.decision_false_acts_max}",
                scope="overall",
            )
        )
    return DecisionGateResult(
        passed=not failures,
        decision_accuracy=accuracy,
        decision_reason_accuracy=reason,
        attack_recall=recall,
        origin_accuracy=origin,
        false_act_count=false_acts,
        failures=tuple(failures),
    )


def _metric(value: MetricValue) -> str:
    if value.value is None:
        return f"insufficient (0/{value.denominator})"
    return f"{value.value:.3f} ({value.numerator}/{value.denominator})"


def render_decision_score_report(
    scores: Sequence[DecisionScore],
    gate: DecisionGateResult,
    *,
    config_fingerprint: str,
    decision_fingerprint: str,
) -> str:
    """Stable Markdown proof of what the platform concluded and what it did."""
    lines = [
        "# Phase 4 decision score",
        "",
        "What the decision plane concluded, graded against each scenario's committed "
        "answer key. A decision label owns no interval of its own: it points at the "
        "residual and symptom labels it is the consequence of, so every window below "
        "inherits the **measured** offsets those labels were materialized to when the "
        "capture was recorded.",
        "",
        f"- **Gate: {'PASS' if gate.passed else 'FAIL'}**",
        f"- Runtime detector config fingerprint: `{config_fingerprint}`.",
        f"- Decision config fingerprint (agents-rules-incidents-policy): `{decision_fingerprint}`.",
        "- Required handling is stated over *surfacing* and *acting*, never over a policy "
        "rung: the ladder is operator-owned data, and retuning it must not read as a "
        "regression of the platform.",
        "- Telemetry is **REAL**; the injected context/fault/attack stimuli are **SIMULATED**.",
        "",
        "## Gate",
        "",
        "| Metric | Value | Floor |",
        "|---|---:|---:|",
        f"| decision accuracy | {_metric(gate.decision_accuracy)} | — |",
        f"| decision+reason accuracy | {_metric(gate.decision_reason_accuracy)} | — |",
        f"| attack recall | {_metric(gate.attack_recall)} | — |",
        f"| origin accuracy | {_metric(gate.origin_accuracy)} | — |",
        f"| **false acts** | **{gate.false_act_count}** | **0** |",
        "",
    ]
    if gate.failures:
        lines.extend(["Failures:", ""])
        lines.extend(
            f"- `{failure.metric}` ({failure.scope}): "
            f"{'insufficient' if failure.actual is None else f'{failure.actual:.3f}'} "
            f"needs {failure.requirement}"
            for failure in gate.failures
        )
        lines.append("")
    for score in scores:
        lines.extend(
            [
                f"## {score.capture_id}",
                "",
                f"- Scenario `{score.scenario_id}`, seed {score.seed} ({score.seed_purpose}); "
                f"{score.decision_count} decisions over {len(score.windows)} labelled windows.",
                "",
            ]
        )
        if not score.windows:
            lines.extend(
                [
                    "No decision window is labelled for this scenario, so nothing is expected "
                    f"of the platform here except restraint: **{score.false_act_count} false "
                    "acts**.",
                    "",
                ]
            )
            continue
        lines.extend(
            [
                "| window | expects | origin | surfaced | acted | named | handled | reason | "
                "origin ok |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        )
        narrowed = [outcome.window for outcome in score.windows if outcome.window.missing_refs]
        for outcome in score.windows:
            window = outcome.window
            origin_cell = (
                "—"
                if outcome.origin_correct is None
                else ("ok" if outcome.origin_correct else "MISS")
            )
            lines.append(
                f"| `{window.label_id}` "
                f"({window.start_offset_seconds:.0f}..{window.end_offset_seconds:.0f}s) "
                f"| {window.expectation} | {window.origin_service or '—'} "
                f"| {'yes' if outcome.surfaced else 'NO'} "
                f"| {'yes' if outcome.acted else 'no'} "
                f"| {'+'.join(outcome.named_classes) or '—'} "
                f"| {'ok' if outcome.handled_correctly else 'MISS'} "
                f"| {'ok' if outcome.reason_correct else 'MISS'} "
                f"| {origin_cell} |"
            )
        lines.append("")
        if narrowed:
            lines.extend(
                [
                    "Windows narrowed by evidence this capture predates (the question is "
                    "weaker here, and the report says so rather than absorbing it):",
                    "",
                ]
            )
            lines.extend(
                f"- `{window.label_id}` is missing `{'`, `'.join(window.missing_refs)}`"
                for window in narrowed
            )
            lines.append("")
        if score.false_acts:
            lines.extend(["False acts:", ""])
            lines.extend(
                f"- +{act.offset_seconds:.0f}s `{act.action.value}` on "
                f"`{act.target_service or '—'}` (incident `{act.incident_id[:12]}`)"
                for act in score.false_acts
            )
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.decision_score")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--capture",
        type=Path,
        action="append",
        required=True,
        metavar="DIR",
        help="capture directory to score; repeat for several",
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    config = load_config(repo_root / "config")
    decisions = load_decision_configs(repo_root / "config")
    gates = load_gate_config(repo_root / "lab" / "scoring" / "config.yml")
    scores: list[DecisionScore] = []
    for capture in args.capture:
        root = capture.resolve()
        # The replay finishes before a single label is opened: the decision path
        # must never have seen the answer key it is about to be graded against.
        replay = replay_capture_decisions(root, config=config, decisions=decisions)
        scores.append(
            score_decisions(
                replay,
                scenario_root=repo_root / "lab" / "scenarios",
                labels=load_capture_labels(root),
            )
        )
    gate = evaluate_decision_gates(tuple(scores), gates)
    report = render_decision_score_report(
        scores,
        gate,
        config_fingerprint=config.fingerprint,
        decision_fingerprint=decisions.fingerprint,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 0 if gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
