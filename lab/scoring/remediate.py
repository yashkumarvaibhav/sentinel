"""Drive the remediation loop from a recorded capture, and write down what it did.

The loop's unit tests build a ``Decision`` and hand it over. This drives it from
**real decisions produced by real recorded telemetry**: the same replay the
decision characterization uses, with every tick's incident outcomes fed to the
remediator in order. That is the difference between "the loop composes its
pieces" and "the loop survives a storm", and only the second one is evidence.

Three things this deliberately does NOT do, each for a reason worth stating:

* **Nothing is applied to anything.** Every rung is carried out by a stand-in
  adapter that changes nothing, and every artifact it produces is labelled
  ``SIMULATED`` end to end - in the outcomes, in the ledger and in the report's
  own header. The mesh and Kubernetes adapters reach a cluster while they are
  still *planning* (the edge pod has to be found before a restraint can be
  aimed), so a keyless, clusterless replay cannot use them and must not pretend
  it did.
* **No label is opened.** This is a characterization, not a score. It reads the
  same public replay the scorer reads and stops there, so it stays safe to run
  on any capture without spending anything.
* **No clock is read.** Every timestamp comes from the capture, so two runs over
  one capture produce the same report.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, Literal

from action import (
    ActionExecutor,
    BlastRadiusGuard,
    RemediationBreaker,
    RemediationLadder,
    RemediationRun,
    Remediator,
    RestraintRegistry,
    SloReading,
    VerifiedRollback,
    load_action_config,
    load_ladder_config,
)
from action.actuators import SimulatedActuator
from audit import AuditChain
from common.config import SentinelConfig, load_config
from contracts import ACTING_ACTIONS, ActuatorKind
from lab.scoring.decisions import (
    DecisionConfigs,
    DecisionReplay,
    load_decision_configs,
    replay_capture_decisions,
)

# How long after an effect lands the loop looks for collateral, and how long
# after an undo it looks for recovery. Stated here rather than guessed per run
# so every capture is characterized under the same rule.
SETTLING = timedelta(seconds=60)
RECOVERY = timedelta(seconds=60)


class MeshStandIn(SimulatedActuator):
    """Stands in for the mesh adapter, which reaches the cluster to plan at all.

    A stand-in answers to the real adapter's ``kind``, so the committed ladders
    run exactly as written - the rung that names ``MESH`` is still the rung that
    names ``MESH`` - while touching nothing. ``honesty`` stays ``SIMULATED``,
    which is what keeps this from being a lie: every outcome, every ledger entry
    and the report's own header say the world was never touched.

    The blast fraction is the real adapter's *ceiling* rather than its per-step
    measurement, so a canary's early steps claim more disturbance here than they
    would in production. Conservative is the right direction for a safety number
    a replay cannot measure.
    """

    kind: ClassVar[ActuatorKind] = ActuatorKind.MESH


class KubernetesStandIn(SimulatedActuator):
    """Stands in for the Kubernetes adapter. See `MeshStandIn`."""

    kind: ClassVar[ActuatorKind] = ActuatorKind.KUBERNETES


class FlagStandIn(SimulatedActuator):
    """Stands in for the flag adapter, whose blast radius is the whole mesh.

    Flipping a flag rolls the provider every service evaluates against, so the
    real adapter claims 1.0 and so does this.
    """

    kind: ClassVar[ActuatorKind] = ActuatorKind.FEATURE_FLAG


@dataclass(frozen=True)
class UnreadableSlo:
    """The SLO reader a clusterless replay honestly has: none.

    A capture records telemetry, not SLO positions, and there is no production
    ``SloReader`` until Phase 8. ``None`` is the honest answer, and it has a
    visible consequence the report states plainly: every action is undone,
    because a protected service nobody can read is never a protected service
    known to be fine. Inventing readings here would manufacture exactly the
    clean run this replay exists to avoid claiming.
    """

    def __call__(self, service: str, *, ts: datetime) -> SloReading | None:
        return None


@dataclass(frozen=True)
class RemediationReplay:
    """Every pass the loop made over one capture."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    anchor_ts: datetime
    runs: tuple[tuple[float, RemediationRun], ...]
    expired: int
    ledger_entries: int
    ledger_head: str
    ladder_fingerprint: str
    detector_fingerprint: str
    decision_fingerprint: str

    @property
    def acted(self) -> tuple[tuple[float, RemediationRun], ...]:
        return tuple((offset, run) for offset, run in self.runs if run.acted)


def build_remediator(
    config: SentinelConfig, *, config_root: Path, ledger: AuditChain
) -> Remediator:
    """Wire the loop the way a clusterless replay honestly can."""
    action = load_action_config(config_root / "action.yml")
    if action.breaker is None:  # pragma: no cover - the committed file has one
        raise ValueError("the committed action configuration states no breaker")
    actuators = [
        SimulatedActuator(blast_fraction=0.05),
        MeshStandIn(blast_fraction=0.20),
        KubernetesStandIn(blast_fraction=0.50),
        FlagStandIn(blast_fraction=1.0),
    ]
    executor = ActionExecutor(
        actuators=actuators, configuration=action, dry_run=False, ledger=ledger
    )
    return Remediator(
        ladder=RemediationLadder(
            load_ladder_config(config_root / "ladders.yml"), flags=action.flags
        ),
        actuators=actuators,
        executor=executor,
        guard=BlastRadiusGuard(config.cohorts),
        breaker=RemediationBreaker(action.breaker),
        rollback=VerifiedRollback(executor=executor, slos=config.slos, reader=UnreadableSlo()),
        registry=RestraintRegistry(),
        ledger=ledger,
    )


def replay_capture_remediation(
    root: Path,
    *,
    config: SentinelConfig,
    config_root: Path,
    decisions: DecisionConfigs,
) -> RemediationReplay:
    """Run every acting decision one capture produced through the loop, in order."""
    replay = replay_capture_decisions(root, config=config, decisions=decisions)
    ledger = AuditChain()
    remediator = build_remediator(config, config_root=config_root, ledger=ledger)
    return _run(replay, remediator=remediator, ledger=ledger)


def _run(
    replay: DecisionReplay, *, remediator: Remediator, ledger: AuditChain
) -> RemediationReplay:
    runs: list[tuple[float, RemediationRun]] = []
    expired = 0
    for tick in replay.ticks:
        # Give back what has outlived its rung before considering anything new,
        # so a TTL that passed during this tick is honoured at the moment it
        # passed rather than one pass late.
        expired += len(remediator.expire(tick.ts))
        for outcome in tick.outcomes:
            if outcome.decision.action not in ACTING_ACTIONS:
                continue
            run = remediator.consider(
                outcome.decision,
                ts=tick.ts,
                settled_at=tick.ts + SETTLING,
                recovered_at=tick.ts + SETTLING + RECOVERY,
            )
            runs.append(((tick.ts - replay.anchor_ts).total_seconds(), run))
    # The capture's end is evidence too: everything still standing at it is
    # something the platform would have gone on holding.
    expired += len(remediator.expire(replay.evaluation_end_ts))
    return RemediationReplay(
        capture_id=replay.capture_id,
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
        anchor_ts=replay.anchor_ts,
        runs=tuple(runs),
        expired=expired,
        ledger_entries=len(ledger),
        ledger_head=ledger.head_hash,
        ladder_fingerprint=remediator.ladder_fingerprint,
        detector_fingerprint=replay.detector_fingerprint,
        decision_fingerprint=replay.decision_fingerprint,
    )


def render_report(replays: Sequence[RemediationReplay]) -> str:
    """A characterization a person can read, with its honesty label at the top."""
    lines = [
        "# Phase 5 — remediation loop replay (characterization)",
        "",
        "**SIMULATED.** Every rung below was carried out by a stand-in adapter that "
        "changed nothing: no cluster, no proxy and no flag provider was contacted. "
        "The decisions are real - produced by the deterministic detection and "
        "decision planes from recorded telemetry - and the loop's choices are real. "
        "What was *done* about them was not.",
        "",
        "There is no production `SloReader` until Phase 8, so every collateral probe "
        "reports its services as unreadable and every action is consequently undone. "
        "That is the honest behaviour - a protected service nobody can read is never "
        "one known to be fine - and it is why `undone` below equals `acted`.",
        "",
    ]
    for replay in replays:
        acted = replay.acted
        undone = sum(1 for _, run in acted if run.rollback is not None)
        refused = sum(1 for _, run in replay.runs if not run.acted)
        lines.extend(
            [
                f"## `{replay.capture_id}` — {replay.scenario_id}, seed {replay.seed} "
                f"({replay.seed_purpose})",
                "",
                f"- Acting decisions considered: **{len(replay.runs)}**",
                f"- Passes that acted: **{len(acted)}**; that took no action: **{refused}**",
                f"- Actions undone by the collateral probe: **{undone}**",
                f"- Restraints given back on expiry: **{replay.expired}**",
                f"- Ledger entries written: **{replay.ledger_entries}** "
                f"(head `{replay.ledger_head[:12]}…`)",
                f"- Ladders `{replay.ladder_fingerprint[:12]}…` · detectors "
                f"`{replay.detector_fingerprint[:12]}…` · decisions "
                f"`{replay.decision_fingerprint}`",
                "",
            ]
        )
        if acted:
            lines.extend(
                [
                    "| offset | incident | rung | effects | gates | outcome |",
                    "|---|---|---|---|---|---|",
                ]
            )
            for offset, run in acted:
                primary = run.selection.primary if run.selection is not None else None
                gates = sorted(
                    {gate for effect in run.applied for gate in effect.outcome.gates_passed}
                )
                lines.append(
                    f"| +{offset:.0f}s | `{run.incident_id[:12]}` "
                    f"| `{primary.rung_id if primary else '—'}` "
                    f"| {len(run.applied)} "
                    f"| {len(gates)} "
                    f"| {'undone' if run.rollback is not None else 'held'} |"
                )
            lines.append("")
        reasons: dict[str, int] = {}
        for _, run in replay.runs:
            for reason in run.refusals:
                reasons[_summarise(reason)] = reasons.get(_summarise(reason), 0) + 1
        if reasons:
            lines.extend(["Why passes declined, by reason:", ""])
            lines.extend(
                f"- {count}x {reason}"
                for reason, count in sorted(reasons.items(), key=lambda item: -item[1])
            )
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _summarise(reason: str) -> str:
    """Fold a refusal to its shape, so a count means something."""
    for marker in ("is already standing on", "reaches no rung of", "cannot be compared with"):
        if marker in reason:
            return marker
    head, _, _ = reason.partition(";")
    return head[:110]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.remediate")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--capture",
        type=Path,
        action="append",
        required=True,
        metavar="DIR",
        help="capture directory to replay; repeat for several",
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    config = load_config(repo_root / "config")
    decisions = load_decision_configs(repo_root / "config")
    ledger = AuditChain()
    replays: list[RemediationReplay] = []
    for capture in args.capture:
        root = capture.resolve()
        replay = replay_capture_decisions(root, config=config, decisions=decisions)
        remediator = build_remediator(config, config_root=repo_root / "config", ledger=ledger)
        replays.append(_run(replay, remediator=remediator, ledger=ledger))
    report = render_report(replays)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
