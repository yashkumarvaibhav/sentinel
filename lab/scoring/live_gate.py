"""Gate and report a live run's stored state, and the CLI that produces the proof.

The scoring primitives live in ``live_score``, the grading rules in
``decision_score`` and the floors in ``gates``; this module is the one place
that puts them together, so the dependency runs in one direction only. The
floors are the same ones a capture replay is held to: a live run is not
allowed a softer definition of correct just because it was harder to produce.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from common.settings import Settings
from common.storage import PostgresRepository, create_postgres_pool
from lab.scoring.decision_gate import DecisionGateResult, evaluate_decision_gates
from lab.scoring.gates import ScoreGateConfig, load_gate_config
from lab.scoring.live_score import (
    LiveRunScore,
    load_run_capture,
    load_run_labels,
    read_live_run,
    run_bounds,
    score_live_run,
)
from lab.scoring.metrics import MetricValue

# The incident reader's own ceiling. It is not a scoring choice: the feed this
# scorer grades is the same bounded read the product serves, and grading more
# rows than the product will ever show would be scoring a different thing.
STORED_INCIDENT_LIMIT = 50


def evaluate_live_run(score: LiveRunScore, config: ScoreGateConfig) -> DecisionGateResult:
    """Hold a live run to the same floors a capture replay is held to."""
    return evaluate_decision_gates((score.score,), config)


def render_live_run_report(score: LiveRunScore, gate: DecisionGateResult) -> str:
    """Stable Markdown proof of what the live producer stored, and how it graded."""
    bounds = score.bounds
    coverage = score.coverage
    lines = [
        "# Phase 6 live-run score",
        "",
        "What the always-on producer **stored** while an authored scenario ran on the "
        "testbed, graded against that scenario's committed answer key. Every scorer "
        "before this one replays a recording and grades the transcript; this one grades "
        "the rows an operator would actually have been looking at.",
        "",
        f"- **Gate: {'PASS' if gate.passed else 'FAIL'}**",
        f"- Run `{bounds.capture_id}`: scenario `{bounds.scenario_id}`, seed {bounds.seed} "
        f"({bounds.seed_purpose}).",
        f"- Anchor `{bounds.anchor_ts.isoformat()}` .. `{bounds.end_ts.isoformat()}`.",
        f"- Producer `{coverage.producer_id}` was anchored at "
        f"`{coverage.anchor_ts.isoformat()}` and durably reached "
        f"`{coverage.tick_ts.isoformat()}`, so it observed the whole run.",
        f"- {score.considered_incidents} of {score.stored_incidents} stored incidents "
        "overlapped the run; the rest belong to other traffic and are not graded.",
        "- The store keeps the **current state** of each incident, not its trajectory, so "
        "this grades the last thing the platform said about each one. That is a weaker "
        "question than a replay transcript answers, and it is the one the product puts "
        "in front of a person.",
        "- Telemetry is **REAL**; the injected context/fault/attack stimuli are **SIMULATED**.",
        "",
        _board_state(score),
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
    lines.extend(
        [
            "## Windows",
            "",
            "| window | expects | origin | surfaced | acted | named | handled | reason | "
            "origin ok |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for outcome in score.score.windows:
        window = outcome.window
        origin_cell = (
            "—" if outcome.origin_correct is None else ("ok" if outcome.origin_correct else "MISS")
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
    if score.score.false_acts:
        lines.extend(["False acts:", ""])
        lines.extend(
            f"- +{act.offset_seconds:.0f}s `{act.action.value}` (incident `{act.incident_id[:12]}`)"
            for act in score.score.false_acts
        )
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _board_state(score: LiveRunScore) -> str:
    """State whether the run began on a clean board or inherited an open incident."""
    if not score.inherited_incidents:
        return (
            "The board was **clean** when the run started: every graded incident was "
            "opened by this run."
        )
    named = ", ".join(f"`{incident_id[:12]}`" for incident_id in score.inherited_incidents)
    return (
        f"⚠ The board was **not clean**: {len(score.inherited_incidents)} incident(s) "
        f"were already open when the run began ({named}). They are graded, because they "
        "were genuinely on the operator's board and answering for services this run asks "
        "about - but the questions below were asked over a busier board than a clean "
        "start would have given, and that is part of the result rather than an excuse "
        "for it."
    )


def _metric(value: MetricValue) -> str:
    if value.value is None:
        return f"insufficient (0/{value.denominator})"
    return f"{value.value:.3f} ({value.numerator}/{value.denominator})"


async def _score(
    *,
    capture_root: Path,
    scenario_root: Path,
    runtime: Settings,
    spend_held_out_seed: bool,
) -> LiveRunScore:
    bounds = run_bounds(load_run_capture(capture_root))
    pool = create_postgres_pool(runtime)
    await pool.open(wait=True)
    try:
        records, checkpoint = await read_live_run(
            PostgresRepository(pool=pool, schema=runtime.postgres_schema),
            producer_id=runtime.live_producer_id,
            limit=STORED_INCIDENT_LIMIT,
        )
    finally:
        await pool.close()
    # The rows are in hand before a single label is opened.
    return score_live_run(
        records,
        checkpoint,
        bounds=bounds,
        scenario_root=scenario_root,
        labels=load_run_labels(capture_root),
        spend_held_out_seed=spend_held_out_seed,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.live_gate")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--capture",
        type=Path,
        required=True,
        metavar="DIR",
        help="the capture recorded from the same traffic the producer judged",
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--spend-held-out-seed",
        action="store_true",
        help="acknowledge that scoring a held-out seed spends it, permanently",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_root = repo_root / "config"
    runtime = Settings().model_copy(update={"config_dir": config_root})
    score = asyncio.run(
        _score(
            capture_root=args.capture.resolve(),
            scenario_root=repo_root / "lab" / "scenarios",
            runtime=runtime,
            spend_held_out_seed=args.spend_held_out_seed,
        )
    )
    gate = evaluate_live_run(score, load_gate_config(repo_root / "lab" / "scoring" / "config.yml"))
    report = render_live_run_report(score, gate)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 0 if gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
