"""Label-free decision-plane replay: run the whole loop over a recorded capture.

Phase 4 built the decision plane against a-priori numbers that had never met a
recorded capture. This module is what lets them: it drives the deterministic
detection replay into ``DecisionPipeline`` and dumps every judgement, so axis
weights, rule thresholds, clustering slack, causal priors and similarity floors
can be frozen on **development** evidence before a held-out seed is ever spent.

It reads no private label, so the same replay is safe to characterize, to freeze
as a golden, and to score later - the scorer is the only thing that ever opens
a label artifact.

Two statements of coverage are made here, and both are facts about the capture
rather than assumptions:

* **Kinds.** All six deterministic detector paths are replayed across the whole
  capture window, so every runtime symptom kind had a detector running for
  every tick. The change feed is not one of them - a capture records telemetry,
  not deploys - so the change axis reports insufficiency rather than calm
  unless a feed is supplied explicitly.
* **Services.** A service is covered when telemetry actually arrived for it,
  measured from the capture's own observations and mapped through the committed
  ingress/log service mappings so a service is judged under the name the
  detectors use for it.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Literal

from common.config import DetectorConfig, SentinelConfig, load_config
from contracts import AgentStatus, EvidenceAxis
from decision import ChangeFeed, DecisionPipeline, DecisionTick, episode_timeline
from decision.config import (
    EvidenceAgentsConfig,
    IncidentsConfig,
    VerdictRulesConfig,
    load_evidence_agents,
    load_incidents,
    load_verdict_rules,
)
from lab.captures import load_runtime_capture
from lab.captures.detection_replay import detection_replay_timeline
from lab.captures.store import RuntimeCapture
from lab.scoring.capture import replay_detection_episodes
from lab.scoring.evaluator import SCORED_SYMPTOM_KINDS, latest_episode_revisions

# Every runtime symptom kind is covered by a capture replay: the six
# deterministic detector paths all run over the full capture window.
CAPTURE_COVERED_KINDS = frozenset(SCORED_SYMPTOM_KINDS)


@dataclass(frozen=True)
class DecisionConfigs:
    """The four decision-plane configuration files, loaded and validated once."""

    agents: EvidenceAgentsConfig
    verdict_rules: VerdictRulesConfig
    incidents: IncidentsConfig

    @property
    def fingerprint(self) -> str:
        """Composite identity of the configuration every judgement was made under."""
        return "-".join(
            value[:12]
            for value in (
                self.agents.fingerprint,
                self.verdict_rules.fingerprint,
                self.incidents.fingerprint,
            )
        )


def load_decision_configs(config_root: Path) -> DecisionConfigs:
    """Load the decision plane's own configuration from the committed files."""
    return DecisionConfigs(
        agents=load_evidence_agents(config_root / "decision-agents.yml"),
        verdict_rules=load_verdict_rules(config_root / "verdict-rules.yml"),
        incidents=load_incidents(config_root / "incidents.yml"),
    )


@dataclass(frozen=True)
class DecisionReplay:
    """Every judgement the decision plane made over one capture."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    anchor_ts: datetime
    evaluation_end_ts: datetime
    covered_services: tuple[str, ...]
    episode_count: int
    ticks: tuple[DecisionTick, ...]


def covered_services(capture: RuntimeCapture, *, detector: DetectorConfig) -> frozenset[str]:
    """Services telemetry actually arrived for, under the names detectors use.

    The raw name is kept as well as the mapped one: a capture that reports
    ``frontend-proxy`` covers the logical ``frontend`` the ingress detector
    scores, and both are true statements about what was observed.
    """
    timeline = detection_replay_timeline(capture)
    mappings = {
        **detector.behavioral_ratios.ingress_windows.service_mappings,
        **detector.log_templates.service_mappings,
    }
    observed = {observation.service for observation in timeline.observations}
    return frozenset(observed | {mappings[name] for name in observed if name in mappings})


def replay_capture_decisions(
    root: Path,
    *,
    config: SentinelConfig,
    decisions: DecisionConfigs,
    changes: ChangeFeed | None = None,
) -> DecisionReplay:
    """Drive one capture's durable episode stream through the whole decision loop."""
    replay = replay_detection_episodes(
        root,
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )
    services = covered_services(load_runtime_capture(root), detector=config.detectors)
    pipeline = DecisionPipeline(
        agents=decisions.agents,
        verdict_rules=decisions.verdict_rules,
        incidents=decisions.incidents,
        topology=config.topology,
        changes=changes,
    )
    # The whole revision stream, not the collapsed one: the decision loop has to
    # see the storm grow, not only the state it ended in.
    snapshots = episode_timeline(replay.episodes)
    ticks = [
        pipeline.observe(
            ts=snapshot.ts,
            episodes=snapshot.episodes,
            covered_kinds=CAPTURE_COVERED_KINDS,
            covered_services=services,
        )
        for snapshot in snapshots
    ]
    # The capture window is evidence too: everything known at its end is known
    # to have stopped changing, which is what lets an incident age out of OPEN.
    if snapshots and replay.evaluation_end_ts > snapshots[-1].ts:
        ticks.append(
            pipeline.observe(
                ts=replay.evaluation_end_ts,
                episodes=snapshots[-1].episodes,
                covered_kinds=CAPTURE_COVERED_KINDS,
                covered_services=services,
            )
        )
    return DecisionReplay(
        capture_id=replay.capture_id,
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
        anchor_ts=replay.anchor_ts,
        evaluation_end_ts=replay.evaluation_end_ts,
        covered_services=tuple(sorted(services)),
        episode_count=len(latest_episode_revisions(replay.episodes)),
        ticks=tuple(ticks),
    )


def semantic_transcript(replay: DecisionReplay) -> bytes:
    """Canonical, clock-free rendering of every judgement, for regression diffing.

    Absolute times become offsets from the capture anchor, so a re-recorded
    capture of the same scenario produces the same transcript when the decision
    plane behaves the same way.
    """
    ticks: list[dict[str, object]] = []
    for tick in replay.ticks:
        axes = {
            assessment.axis.value: {
                "status": assessment.status.value,
                "score": _round(assessment.score),
                "trend": assessment.trend.value,
                "contributing_kinds": [kind.value for kind in assessment.contributing_kinds],
                "services": list(assessment.services),
            }
            for assessment in sorted(tick.assessments, key=lambda item: item.axis.value)
        }
        verdict = tick.verdict
        ticks.append(
            {
                "offset_seconds": _round((tick.ts - replay.anchor_ts).total_seconds()),
                "axes": dict(sorted(axes.items())),
                "fusion": {
                    "status": tick.fusion.status.value,
                    "class": None if verdict is None else verdict.verdict_class.value,
                    "rule_id": None if verdict is None else verdict.rule_id,
                    "reason_subtype": (
                        None
                        if verdict is None or verdict.reason_subtype is None
                        else verdict.reason_subtype.value
                    ),
                    "distribution": (
                        {}
                        if verdict is None
                        else {
                            name: _round(mass)
                            for name, mass in sorted(verdict.distribution.items())
                        }
                    ),
                },
                "incidents": [
                    {
                        "anchor_offset_seconds": _round(
                            (outcome.incident.opened_ts - replay.anchor_ts).total_seconds()
                        ),
                        "state": outcome.incident.state.value,
                        "severity": outcome.incident.severity.value,
                        "services": list(outcome.incident.services),
                        "kinds": [kind.value for kind in outcome.incident.kinds],
                        "episode_count": len(outcome.incident.episode_ids),
                        "business_impact": (
                            None
                            if outcome.incident.business_impact is None
                            else _round(outcome.incident.business_impact)
                        ),
                        "origin_service": outcome.incident.origin_service,
                        "origin_confidence": (
                            None
                            if outcome.incident.origin_confidence is None
                            else _round(outcome.incident.origin_confidence)
                        ),
                        "confidence": (
                            None if outcome.verdict is None else _round(outcome.verdict.confidence)
                        ),
                        "confirmed": outcome.verification.confirmed,
                        "checks": {
                            check.name: check.outcome.value
                            for check in sorted(
                                outcome.verification.checks, key=lambda item: item.name
                            )
                        },
                        "matches": len(outcome.matches),
                    }
                    for outcome in tick.outcomes
                ],
            }
        )
    value = {
        "capture_id": replay.capture_id,
        "covered_services": list(replay.covered_services),
        "episode_count": replay.episode_count,
        "honesty": {"stimulus": "SIMULATED", "telemetry": "REAL"},
        "scenario_id": replay.scenario_id,
        "seed": replay.seed,
        "seed_purpose": replay.seed_purpose,
        "ticks": ticks,
        "version": 1,
    }
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _round(value: float) -> float:
    """Fixed precision so a transcript diff shows behavior, not float dust."""
    return round(value + 0.0, 6)


def _axis_column(tick: DecisionTick, axis: EvidenceAxis) -> str:
    assessment = tick.assessment(axis)
    if assessment.status is AgentStatus.INSUFFICIENT:
        return "—"
    return f"{assessment.score:.3f}"


def _tick_row(tick: DecisionTick) -> tuple[str, ...]:
    """One tick's judgement, without its time, so identical ticks can be folded."""
    verdict = tick.verdict
    outcome = tick.outcomes[0] if tick.outcomes else None
    return (
        _axis_column(tick, EvidenceAxis.SECURITY),
        _axis_column(tick, EvidenceAxis.RELIABILITY),
        _axis_column(tick, EvidenceAxis.CHANGE_CONFIG),
        _axis_column(tick, EvidenceAxis.BUSINESS_IMPACT),
        tick.fusion.status.value if verdict is None else verdict.verdict_class.value,
        "—" if outcome is None or outcome.verdict is None else f"{outcome.verdict.confidence:.3f}",
        str(len(tick.outcomes)),
        "—" if outcome is None else (outcome.incident.origin_service or "—"),
        "—" if outcome is None else outcome.incident.severity.value,
        "—" if outcome is None else outcome.incident.state.value,
        "—" if outcome is None else ("yes" if outcome.confirmed else "no"),
    )


def _render_replay(replay: DecisionReplay) -> list[str]:
    lines = [
        f"## {replay.capture_id}",
        "",
        f"- Scenario `{replay.scenario_id}`, seed {replay.seed} ({replay.seed_purpose}).",
        f"- {replay.episode_count} durable episodes became {len(replay.ticks)} decision ticks "
        f"over anchor .. +{(replay.evaluation_end_ts - replay.anchor_ts).total_seconds():.1f}s.",
        f"- Telemetry coverage: {len(replay.covered_services)} services.",
        "",
    ]
    if not replay.ticks:
        lines.extend(["No durable episode was emitted, so the loop never ran.", ""])
        return lines
    lines.extend(
        [
            "Consecutive ticks whose judgement is identical are folded into one row, so every "
            "row below is a change in what the plane concluded.",
            "",
            "| t(s) | ticks | SEC | REL | CHG | BIZ | verdict | conf | incidents | origin "
            "| severity | state | verified |",
            "|---|---:|---:|---:|---:|---:|---|---:|---:|---|---|---|---|",
        ]
    )
    for row, group in groupby(replay.ticks, key=_tick_row):
        held = tuple(group)
        first = (held[0].ts - replay.anchor_ts).total_seconds()
        last = (held[-1].ts - replay.anchor_ts).total_seconds()
        span = f"{first:.0f}" if len(held) == 1 else f"{first:.0f}..{last:.0f}"
        lines.append(f"| {span} | {len(held)} | " + " | ".join(row) + " |")
    lines.append("")
    final = replay.ticks[-1]
    if final.outcomes:
        lines.extend(["Final verification detail:", ""])
        for outcome in final.outcomes:
            services = ", ".join(outcome.incident.services)
            lines.append(f"- Incident over `{services}`:")
            lines.extend(
                f"  - `{check.name}`: **{check.outcome.value}** — {check.detail}"
                for check in outcome.verification.checks
            )
        lines.append("")
    return lines


def render_decision_report(
    replays: Sequence[DecisionReplay],
    *,
    config_fingerprint: str,
    decision_fingerprint: str,
) -> str:
    """Stable Markdown dump of every decision the plane made on these captures."""
    lines = [
        "# Phase 4 decision characterization (label-free evidence)",
        "",
        "Every judgement the decision plane makes on the captures below: the four "
        "independent axis scores, the fused verdict, the incidents the storm collapsed "
        "into, the named origin and the four deterministic verification checks. "
        "**No private label is read.** This is the evidence the Phase-4 a-priori numbers "
        "are frozen on, before any held-out seed is spent. It gates nothing on its own.",
        "",
        f"- Runtime detector config fingerprint: `{config_fingerprint}`.",
        f"- Decision config fingerprint (agents-rules-incidents): `{decision_fingerprint}`.",
        "- Offsets are seconds from each capture's anchor; the replay is deterministic.",
        "- Telemetry is **REAL**; the injected context/fault/attack stimuli are **SIMULATED**.",
        "- `CHG` is `—` on a capture: a capture records telemetry, not deploys, so the "
        "change axis reports insufficiency rather than a calm zero.",
        "- A verdict column of `INSUFFICIENT` means evidence was measured and no signature "
        "accounted for it; `NO_EVIDENCE` means nothing contributed at all. They are different "
        "facts and lead to different decisions.",
        "",
    ]
    for replay in replays:
        lines.extend(_render_replay(replay))
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.decisions")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--capture",
        type=Path,
        action="append",
        required=True,
        metavar="DIR",
        help="capture directory to replay through the decision plane; repeat for several",
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--transcript-dir",
        type=Path,
        help="write each capture's canonical decision transcript here",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    config = load_config(repo_root / "config")
    decisions = load_decision_configs(repo_root / "config")
    replays = tuple(
        replay_capture_decisions(capture.resolve(), config=config, decisions=decisions)
        for capture in args.capture
    )
    # Characterization is what the a-priori numbers are frozen on, so it refuses
    # a sealed seed outright: a held-out capture is spent by being scored, and
    # nothing that shapes a parameter may look at one first.
    spent = tuple(replay.capture_id for replay in replays if replay.seed_purpose != "development")
    if spent:
        raise ValueError(
            "decision characterization runs on development captures only; "
            f"held-out captures refused: {', '.join(spent)}"
        )
    if args.transcript_dir is not None:
        args.transcript_dir.mkdir(parents=True, exist_ok=True)
        for replay in replays:
            (args.transcript_dir / f"{replay.capture_id}.json").write_bytes(
                semantic_transcript(replay)
            )
    report = render_decision_report(
        replays,
        config_fingerprint=config.fingerprint,
        decision_fingerprint=decisions.fingerprint,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
