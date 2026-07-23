"""Label-free episode characterization for Phase 2 closure design (evidence only).

Dumps every durable episode each deterministic detector emits on a capture,
collapsed to its latest revision, with no private label ever opened. This is the
prediction side in isolation: the evidence base for a recall/coverage gate and
for fuller a-priori storm labels. It touches no runtime decision path and gates
nothing.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from common.config import DetectorConfig, TopologyConfig, load_config
from contracts import EpisodeStatus, SymptomKind
from lab.scoring.capture import DetectionEpisodeReplay, replay_detection_episodes
from lab.scoring.evaluator import SCORED_SYMPTOM_KINDS, latest_episode_revisions


@dataclass(frozen=True)
class EpisodeRecord:
    """One durable episode's identity and lifecycle, offset from the anchor."""

    kind: SymptomKind
    service: str
    signal: str
    status: EpisodeStatus
    opened_offset_seconds: float
    confirmed_offset_seconds: float
    closed_offset_seconds: float | None
    peak_score: float
    breach_tick_count: int
    episode_id: str


@dataclass(frozen=True)
class CaptureCharacterization:
    """Every emitted episode for one capture, plus its identity and window."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: str
    anchor_ts: datetime
    evaluation_end_offset_seconds: float
    records: tuple[EpisodeRecord, ...]

    def counts_by_kind(self) -> tuple[tuple[SymptomKind, int], ...]:
        """Episode count per scored kind, including zeros, in scoring order."""
        return tuple(
            (kind, sum(1 for record in self.records if record.kind is kind))
            for kind in SCORED_SYMPTOM_KINDS
        )


def characterize_replay(replay: DetectionEpisodeReplay) -> CaptureCharacterization:
    """Collapse the label-free episode stream to a deterministic characterization."""
    anchor = replay.anchor_ts
    records = tuple(
        EpisodeRecord(
            kind=episode.kind,
            service=episode.service,
            signal=episode.signal,
            status=episode.status,
            opened_offset_seconds=(episode.opened_ts - anchor).total_seconds(),
            confirmed_offset_seconds=(episode.confirmed_ts - anchor).total_seconds(),
            closed_offset_seconds=(
                None if episode.closed_ts is None else (episode.closed_ts - anchor).total_seconds()
            ),
            peak_score=float(episode.peak_score),
            breach_tick_count=episode.breach_tick_count,
            episode_id=episode.episode_id,
        )
        for episode in latest_episode_revisions(replay.episodes)
    )
    return CaptureCharacterization(
        capture_id=replay.capture_id,
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
        anchor_ts=anchor,
        evaluation_end_offset_seconds=(replay.evaluation_end_ts - anchor).total_seconds(),
        records=records,
    )


def characterize_capture(
    root: Path,
    *,
    detector: DetectorConfig,
    replay_config_fingerprint: str,
) -> CaptureCharacterization:
    """Run the label-free replay for one capture, then characterize its episodes."""
    return characterize_replay(
        replay_detection_episodes(
            root,
            detector=detector,
            replay_config_fingerprint=replay_config_fingerprint,
        )
    )


# Every scored kind except RESIDUAL_EXCEED is an injected fault/attack symptom.
# RESIDUAL_EXCEED is a legitimate signal on match_night (injected residual offset)
# and attack_day (the attack surge), so it is not part of the zero-emission check.
NEGATIVE_CONTROL_FAULT_KINDS: tuple[SymptomKind, ...] = tuple(
    kind for kind in SCORED_SYMPTOM_KINDS if kind is not SymptomKind.RESIDUAL_EXCEED
)


@dataclass(frozen=True)
class NegativeControlViolation:
    """A fault-kind episode emitted on a capture whose scenario ran no such fault."""

    capture_id: str
    kind: SymptomKind
    count: int


def negative_control_violations(
    characterizations: Sequence[CaptureCharacterization],
) -> tuple[NegativeControlViolation, ...]:
    """Fault kinds must emit zero episodes on a no-fault capture; report every breach.

    This is anti-spam guard (i): a detector that fires a fault symptom on quiet or
    event-explained traffic is a false positive the recall gate (which only scores
    fault captures) would never see. It is a pure prediction-side property, so it
    reads no private label.
    """
    violations: list[NegativeControlViolation] = []
    for characterization in characterizations:
        counts = dict(characterization.counts_by_kind())
        for kind in NEGATIVE_CONTROL_FAULT_KINDS:
            count = counts[kind]
            if count > 0:
                violations.append(
                    NegativeControlViolation(
                        capture_id=characterization.capture_id,
                        kind=kind,
                        count=count,
                    )
                )
    return tuple(violations)


def _fmt_offset(value: float | None) -> str:
    return "active" if value is None else f"{value:.1f}"


def _render_topology(topology: TopologyConfig) -> list[str]:
    lines = [
        "## Topology (config/topology.yml)",
        "",
        "Direct downstream dependencies, so propagated symptoms below can be traced "
        "to a fault target by hand.",
        "",
    ]
    for service in topology.services:
        dependencies = ", ".join(f"`{name}`" for name in service.dependencies) or "—"
        lines.append(
            f"- `{service.service}` ({service.tier}/{service.criticality}) -> {dependencies}"
        )
    lines.append("")
    return lines


def _render_capture(characterization: CaptureCharacterization) -> list[str]:
    counts = " · ".join(
        f"{kind.value} {count}" for kind, count in characterization.counts_by_kind()
    )
    lines = [
        f"## {characterization.capture_id}",
        "",
        f"- Scenario `{characterization.scenario_id}`, seed {characterization.seed} "
        f"({characterization.seed_purpose}).",
        f"- Window: anchor .. +{characterization.evaluation_end_offset_seconds:.1f}s.",
        f"- Episodes emitted: {len(characterization.records)} total. Per kind: {counts}.",
        "",
    ]
    if not characterization.records:
        lines.extend(["No episodes emitted.", ""])
        return lines
    lines.extend(
        [
            "| kind | service | signal | opened(s) | confirmed(s) | closed(s) | status "
            "| peak | breaches | episode_id |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for record in characterization.records:
        lines.append(
            f"| {record.kind.value} | {record.service} | {record.signal} "
            f"| {record.opened_offset_seconds:.1f} | {record.confirmed_offset_seconds:.1f} "
            f"| {_fmt_offset(record.closed_offset_seconds)} | {record.status.value} "
            f"| {record.peak_score:.3f} | {record.breach_tick_count} "
            f"| `{record.episode_id[:12]}` |"
        )
    lines.append("")
    return lines


def _render_negative_control(
    violations: tuple[NegativeControlViolation, ...],
) -> list[str]:
    verdict = "FAIL" if violations else "PASS"
    lines = [
        f"## Negative control: {verdict}",
        "",
        "Anti-spam guard (i): every fault kind "
        f"({', '.join(kind.value for kind in NEGATIVE_CONTROL_FAULT_KINDS)}) must emit **zero** "
        "episodes on these no-fault captures. RESIDUAL_EXCEED is exempt (a legitimate signal on "
        "event-explained and attack traffic).",
        "",
    ]
    if not violations:
        lines.extend(["No fault-kind episode was emitted on any no-fault capture.", ""])
        return lines
    lines.extend(["| Capture | Kind | Episodes |", "|---|---|---:|"])
    lines.extend(
        f"| {violation.capture_id} | {violation.kind.value} | {violation.count} |"
        for violation in violations
    )
    lines.append("")
    return lines


def render_characterization(
    characterizations: Sequence[CaptureCharacterization],
    *,
    topology: TopologyConfig,
    config_fingerprint: str,
    negative_control: tuple[NegativeControlViolation, ...] | None = None,
) -> str:
    """Stable Markdown dump of the label-free episode stream for each capture."""
    lines = [
        "# Phase 2 episode characterization (label-free evidence)",
        "",
        "Every durable episode each deterministic detector emits on the captures below, "
        "collapsed to its latest revision. **No private label is read** — this is the "
        "prediction side only, the evidence base for the recall/coverage gate and the "
        "fuller a-priori storm labels (BUILD_STATE 6a). It gates nothing on its own.",
        "",
        f"- Runtime config fingerprint: `{config_fingerprint}`.",
        "- Offsets are seconds from each capture's anchor; bit-exact replay keeps them stable.",
        "- Telemetry is **REAL**; the injected context/fault/attack stimuli are **SIMULATED**.",
        "- `episode_id` is shown as a 12-char prefix of the deterministic identity hash.",
        "",
    ]
    if negative_control is not None:
        lines.extend(_render_negative_control(negative_control))
    lines.extend(_render_topology(topology))
    for characterization in characterizations:
        lines.extend(_render_capture(characterization))
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.diagnose")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--capture",
        type=Path,
        action="append",
        required=True,
        metavar="DIR",
        help="capture directory to characterize; repeat for several",
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--assert-no-fault-episodes",
        action="store_true",
        help="treat the captures as no-fault negative controls: fail if any fault kind fires",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    config = load_config(repo_root / "config")
    characterizations = tuple(
        characterize_capture(
            capture.resolve(),
            detector=config.detectors,
            replay_config_fingerprint=config.fingerprint,
        )
        for capture in args.capture
    )
    violations = (
        negative_control_violations(characterizations) if args.assert_no_fault_episodes else None
    )
    report = render_characterization(
        characterizations,
        topology=config.topology,
        config_fingerprint=config.fingerprint,
        negative_control=violations,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
