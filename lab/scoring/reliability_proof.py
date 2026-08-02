"""Generate the two post-score reliability KPI readers from executable evidence.

The symptom score proof predates the action plane, so it cannot answer either
"did we act on a quiet day?" or "how long until a real action recovered?".
This command answers them without weakening that stable hosted gate:

* two sealed held-out quiet captures run through the same decision replay and
  ``false_acts`` scorer used elsewhere in the lab;
* one contained Kubernetes scale is applied, observed, reverted and then read
  through the production VictoriaMetrics SLO settlement reader.

The command changes only the named testbed workload and always restores its
original replica count in ``finally``. It never writes a production incident or
fixture. A proof is emitted only when the quiet count is zero and every
post-revert SLO reading is measured and recovered.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from action.actuators.kubernetes import KubernetesActuator
from action.config import load_action_config
from action.executor import ActionExecutor
from action.guards import BlastRadiusGuard, with_gates
from action.ladder import RungChoice
from action.rollback import SloSettlementVerifier, VictoriaMetricsSloReader
from common.config import load_config
from contracts import (
    ActionKind,
    ActionPlan,
    ActionStatus,
    ActuatorKind,
    KpiKey,
    ReliabilityMetricEvidence,
    ReliabilityProof,
    RollbackVerificationStatus,
    action_idempotency_key,
)
from lab.scoring.decision_score import load_capture_labels, score_decisions
from lab.scoring.decisions import load_decision_configs, replay_capture_decisions

REPORT_PATH = "docs/reports/phase-6-reliability-proof.md"
QUIET_CAPTURE_IDS = ("phase1-quiet-7901-v2", "phase1-quiet-7919-v2")


def quiet_day_evidence(
    repo_root: Path,
    *,
    captures: Sequence[Path],
) -> ReliabilityMetricEvidence:
    """Measure autonomous false acts after every replay finishes label-free."""
    runtime = load_config(repo_root / "config")
    decisions = load_decision_configs(repo_root / "config")
    replays = tuple(
        replay_capture_decisions(path, config=runtime, decisions=decisions) for path in captures
    )
    scores = tuple(
        score_decisions(
            replay,
            scenario_root=repo_root / "lab" / "scenarios",
            labels=load_capture_labels(path),
        )
        for path, replay in zip(captures, replays, strict=True)
    )
    if any(replay.scenario_id != "quiet_day" for replay in replays):
        raise ValueError("quiet-day KPI evidence accepts only quiet_day captures")
    if any(replay.seed_purpose != "held_out" for replay in replays):
        raise ValueError("quiet-day KPI evidence accepts only held-out seeds")
    false_acts = sum(score.false_act_count for score in scores)
    if false_acts:
        raise RuntimeError(f"quiet-day action gate failed with {false_acts} false acts")
    return ReliabilityMetricEvidence(
        key=KpiKey.QUIET_DAY_FALSE_ACTS,
        value=float(false_acts),
        unit="actions",
        evidence_start=min(replay.anchor_ts for replay in replays),
        evidence_end=max(replay.evaluation_end_ts for replay in replays),
        sample_count=len(replays),
        evidence_kind="held_out_decision_replay",
        source_ids=tuple(replay.capture_id for replay in replays),
        telemetry_honesty="REAL",
        provenance=f"phase-6-held-out-quiet-actions · {REPORT_PATH}",
    )


def contained_action_evidence(
    repo_root: Path,
    *,
    settlement_seconds: int = 15,
) -> tuple[ReliabilityMetricEvidence, dict[str, object]]:
    """Measure one real, reversible action with the production SLO reader."""
    if settlement_seconds < 1:
        raise ValueError("settlement_seconds must be positive")
    action = load_action_config(repo_root / "config" / "action.yml")
    runtime = load_config(repo_root / "config")
    kubernetes = action.kubernetes
    if kubernetes is None:
        raise RuntimeError("the contained action proof needs the Kubernetes adapter")

    original = int(_kubectl("get", "deployment/payment", "-o", "jsonpath={.spec.replicas}"))
    target = original + 1 if original < kubernetes.maximum_replicas else original - 1
    if target < 1 or target == original:
        raise RuntimeError("no contained payment replica target is available")
    started = datetime.now(UTC)
    suffix = int(started.timestamp())
    parameters: dict[str, str | bool | int | float] = {"replicas": target}
    target_ref = f"{kubernetes.namespace}/deployment/payment"
    plan = ActionPlan(
        plan_id=f"contained-payment-scale-{suffix}",
        ts=started,
        decision_id=f"contained-recovery-decision-{suffix}",
        incident_id=f"contained-recovery-incident-{suffix}",
        actuator=ActuatorKind.KUBERNETES,
        action_kind=ActionKind.SCALE,
        target_service="payment",
        target_ref=target_ref,
        parameters=parameters,
        reason=(
            "A contained automated recovery proof selected an absolute payment replica target."
        ),
        expected_effect="The payment workload reports the requested ready replica count.",
        reversible=True,
        requires_human_approval=False,
        estimated_blast_fraction=round(1.0 / max(len(kubernetes.workloads), 1), 6),
        idempotency_key=action_idempotency_key(
            actuator=ActuatorKind.KUBERNETES,
            action_kind=ActionKind.SCALE,
            target_ref=target_ref,
            parameters=parameters,
        ),
        honesty="REAL",
    )
    choice = RungChoice(
        rung_id="contained-scale-recovery-proof",
        ladder_id="remediate-our-own-fault",
        actuator=plan.actuator,
        action_kind=plan.action_kind,
        parameters=plan.parameters,
        ttl=timedelta(minutes=5),
        requires_human_approval=False,
        maximum_blast_fraction=0.5,
        reason=plan.reason,
    )
    gates = BlastRadiusGuard(runtime.cohorts).check(plan, choice)
    adapter = KubernetesActuator(configuration=kubernetes)
    executor = ActionExecutor(
        actuators=[adapter],
        configuration=action,
        # The reviewed deployment remains dry-run. This bounded proof is the
        # explicit, reversible exception and labels its effect REAL.
        dry_run=False,
    )
    slo_client = httpx.Client(base_url="http://127.0.0.1:8042", timeout=10.0)
    settlement = SloSettlementVerifier(
        slos=runtime.slos,
        reader=VictoriaMetricsSloReader(client=slo_client, window_seconds=60),
    )
    applied = None
    verification = None
    try:
        applied_at = datetime.now(UTC)
        applied = with_gates(
            executor.apply(plan, ts=applied_at, owner="contained-reliability-proof"),
            gates,
        )
        executor.journal.record(applied)
        if applied.status is not ActionStatus.APPLIED:
            raise RuntimeError(f"contained action did not apply: {applied.status.value}")
        _kubectl("rollout", "status", "deployment/payment", "--timeout=90s")
        observed = executor.verify(plan, ts=datetime.now(UTC))
        if observed.status is not ActionStatus.VERIFIED:
            raise RuntimeError(f"contained action was not observed: {observed.status.value}")

        before = settlement.capture(ts=datetime.now(UTC))
        reverted = executor.revert(
            plan,
            ts=datetime.now(UTC),
            owner="contained-reliability-proof",
        )
        if reverted.status is not ActionStatus.REVERTED:
            raise RuntimeError(f"contained action did not revert: {reverted.status.value}")
        _kubectl("rollout", "status", "deployment/payment", "--timeout=90s")
        time.sleep(settlement_seconds)
        verification = settlement.verify_rollback(before=before, ts=datetime.now(UTC))
        if verification.status is not RollbackVerificationStatus.VERIFIED:
            raise RuntimeError(
                "contained recovery was not verified: "
                f"{verification.status.value} — {verification.detail}"
            )
        duration = (verification.verified_at - applied_at).total_seconds()
        evidence = ReliabilityMetricEvidence(
            key=KpiKey.AUTONOMOUS_MTTR,
            value=duration,
            unit="seconds",
            evidence_start=applied_at,
            evidence_end=verification.verified_at,
            sample_count=1,
            evidence_kind="contained_real_testbed_action",
            source_ids=(plan.plan_id,),
            telemetry_honesty="REAL",
            provenance=f"phase-6-contained-action-recovery · {REPORT_PATH}",
        )
        detail = {
            "action_config_fingerprint": action.fingerprint,
            "after": [sample.model_dump(mode="json") for sample in verification.after],
            "before": [sample.model_dump(mode="json") for sample in verification.before],
            "duration_seconds": duration,
            "original_replicas": original,
            "plan_id": plan.plan_id,
            "status": verification.status.value,
            "target_replicas": target,
            "users_restored": verification.users_restored,
        }
        return evidence, detail
    finally:
        slo_client.close()
        current = int(_kubectl("get", "deployment/payment", "-o", "jsonpath={.spec.replicas}"))
        if current != original:
            _kubectl("scale", "deployment/payment", f"--replicas={original}")
        _kubectl("rollout", "status", "deployment/payment", "--timeout=90s")


def build_proof(
    *,
    mttr: ReliabilityMetricEvidence,
    quiet: ReliabilityMetricEvidence,
) -> ReliabilityProof:
    """Assemble the two independent readers in canonical KPI order."""
    if mttr.key != KpiKey.AUTONOMOUS_MTTR or quiet.key != KpiKey.QUIET_DAY_FALSE_ACTS:
        raise ValueError("reliability proof inputs do not match their KPI readers")
    return ReliabilityProof(
        version=1,
        proof_id="phase-6-reliability-readers",
        gate_status="pass",
        metrics=(mttr, quiet),
    )


def render_report(proof: ReliabilityProof, action: dict[str, object]) -> str:
    """Render a compact human audit companion to the machine artifact."""
    mttr, quiet = proof.metrics
    after = action["after"]
    before = action["before"]
    return "\n".join(
        (
            "# Phase 6 reliability KPI proof",
            "",
            "- **Gate: PASS**",
            "- Telemetry is **REAL**. Quiet-day schedules and the contained lab action are "
            "controlled testbed stimuli; no production fixture was written.",
            f"- Autonomous MTTR: **{mttr.value:.3f}s** across {mttr.sample_count} contained "
            "real-testbed action.",
            f"- Quiet-day false acts: **{quiet.value:.0f}** across {quiet.sample_count} sealed "
            "held-out captures.",
            "- Protected-cohort integrity remains insufficient because no production "
            "protected-cohort telemetry reader is attached.",
            "",
            "## Sources",
            "",
            f"- MTTR plan: `{mttr.source_ids[0]}`; window "
            f"`{mttr.evidence_start.isoformat()}` → `{mttr.evidence_end.isoformat()}`.",
            f"- Quiet captures: {', '.join(f'`{item}`' for item in quiet.source_ids)}.",
            f"- Action config fingerprint: `{action['action_config_fingerprint']}`.",
            f"- Replica change: {action['original_replicas']} → {action['target_replicas']} → "
            f"{action['original_replicas']}.",
            "",
            "## SLO settlement",
            "",
            "```json",
            json.dumps({"before": before, "after": after}, indent=2, sort_keys=True),
            "```",
            "",
        )
    )


def _kubectl(*args: str) -> str:
    completed = subprocess.run(
        ["kubectl", "--context", "k3d-sentinel-lab", "-n", "otel-demo", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.reliability_proof")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--settlement-seconds", type=int, default=15)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    captures = tuple(
        repo_root / "data" / "captures" / "phase-1" / "held-out" / capture_id
        for capture_id in QUIET_CAPTURE_IDS
    )
    quiet = quiet_day_evidence(repo_root, captures=captures)
    mttr, action = contained_action_evidence(
        repo_root,
        settlement_seconds=args.settlement_seconds,
    )
    proof = build_proof(mttr=mttr, quiet=quiet)
    args.proof.parent.mkdir(parents=True, exist_ok=True)
    args.proof.write_text(
        json.dumps(proof.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(proof, action), encoding="utf-8")
    print(proof.model_dump_json(indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
