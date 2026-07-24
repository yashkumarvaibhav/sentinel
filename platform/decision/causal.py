"""Collapse an incident's symptom storm to the one service that started it.

The loudest symptom is almost never the origin. A payment fault shows up first
as a *checkout* problem, because checkout is the one waiting on the call, and
loudest at the *frontend*, because that is where users are. Naming either of
them sends a responder to the wrong service.

Three independent priors decide instead, each computed from evidence rather
than asserted:

* **Onset.** Whoever started first is the likelier cause - a prior, never a
  rule, because detectors confirm at different speeds and the origin's own log
  burst often lands after the edge symptom it already caused one hop up.
* **Reachability.** A cause sits downstream of its effects on the committed
  dependency graph: the more of the affected services depend on a candidate,
  the better it explains the spread.
* **Accusation.** A degraded ``caller -> dependency.callee`` edge is evidence
  *for* the callee and *against* the caller, because the caller's trouble is
  already explained by the dependency it is waiting on.

When the top two candidates are too close to separate, no origin is named. An
honestly ambiguous storm is more useful than a confident wrong service.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from common.config import TopologyConfig
from contracts import SymptomEpisode, SymptomKind
from decision.config import CausalCollapseConfig


@dataclass(frozen=True, slots=True)
class OriginCandidate:
    """One service's case for being the origin, with the priors that built it."""

    service: str
    score: float
    onset_score: float
    reachability_score: float
    accusation_score: float
    earliest_onset: datetime
    note: str


@dataclass(frozen=True, slots=True)
class CausalCollapse:
    """The ranked candidates and, when the evidence separates them, the origin."""

    origin: OriginCandidate | None
    candidates: tuple[OriginCandidate, ...]
    note: str


def dependents(topology: TopologyConfig) -> Mapping[str, frozenset[str]]:
    """Map each service to everything that depends on it, directly or through others."""
    direct: dict[str, set[str]] = {service.service: set() for service in topology.services}
    for service in topology.services:
        for dependency in service.dependencies:
            direct.setdefault(dependency, set()).add(service.service)
    resolved: dict[str, frozenset[str]] = {}
    for origin in direct:
        reached: set[str] = set()
        frontier = [origin]
        while frontier:
            following: list[str] = []
            for node in frontier:
                for dependent in sorted(direct.get(node, set())):
                    if dependent not in reached and dependent != origin:
                        reached.add(dependent)
                        following.append(dependent)
            frontier = following
        resolved[origin] = frozenset(reached)
    return resolved


def collapse_to_origin(
    episodes: Sequence[SymptomEpisode],
    *,
    configuration: CausalCollapseConfig,
    topology: TopologyConfig,
) -> CausalCollapse:
    """Rank the affected services and name an origin when the evidence separates them."""
    if not episodes:
        return CausalCollapse(origin=None, candidates=(), note="no episodes to collapse")
    services = sorted({episode.service for episode in episodes})
    onsets = {
        service: min(episode.opened_ts for episode in episodes if episode.service == service)
        for service in services
    }
    downstream = dependents(topology)
    accused, explained, edge_count = _edge_evidence(
        episodes, prefix=configuration.dependency_signal_prefix
    )

    earliest = min(onsets.values())
    span = (max(onsets.values()) - earliest).total_seconds()
    candidates = []
    for service in services:
        lead = (onsets[service] - earliest).total_seconds()
        onset_score = 1.0 if span <= 0.0 else 1.0 - lead / span
        others = [candidate for candidate in services if candidate != service]
        covered = [other for other in others if other in downstream.get(service, frozenset())]
        reachability = 1.0 if not others else len(covered) / len(others)
        net = accused.get(service, 0) - explained.get(service, 0)
        accusation = 0.0 if edge_count == 0 else max(net, 0) / edge_count
        score = (
            configuration.onset_weight * onset_score
            + configuration.reachability_weight * reachability
            + configuration.accusation_weight * accusation
        )
        candidates.append(
            OriginCandidate(
                service=service,
                score=score,
                onset_score=onset_score,
                reachability_score=reachability,
                accusation_score=accusation,
                earliest_onset=onsets[service],
                note=(
                    f"started {lead:.0f}s into the storm; "
                    f"{len(covered)} of {len(others)} affected services depend on it; "
                    f"accused by {accused.get(service, 0)} and explained away by "
                    f"{explained.get(service, 0)} of {edge_count} dependency edges"
                ),
            )
        )
    ranked = tuple(sorted(candidates, key=lambda item: (-item.score, item.service)))

    if len(ranked) == 1:
        return CausalCollapse(
            origin=ranked[0],
            candidates=ranked,
            note=f"{ranked[0].service} is the only affected service",
        )
    margin = ranked[0].score - ranked[1].score
    if margin < configuration.minimum_margin:
        return CausalCollapse(
            origin=None,
            candidates=ranked,
            note=(
                f"{ranked[0].service} and {ranked[1].service} are separated by only "
                f"{margin:.3f}, under the {configuration.minimum_margin:.3f} margin"
            ),
        )
    return CausalCollapse(
        origin=ranked[0],
        candidates=ranked,
        note=(f"{ranked[0].service} leads {ranked[1].service} by {margin:.3f}: {ranked[0].note}"),
    )


def _edge_evidence(
    episodes: Sequence[SymptomEpisode],
    *,
    prefix: str,
) -> tuple[dict[str, int], dict[str, int], int]:
    """Count who each degraded dependency edge accuses, and who it exonerates."""
    accused: dict[str, int] = {}
    explained: dict[str, int] = {}
    edges = 0
    for episode in episodes:
        if episode.kind is not SymptomKind.EDGE_DEGRADED:
            continue
        if not episode.signal.startswith(prefix):
            continue
        callee = episode.signal[len(prefix) :]
        if not callee:
            continue
        edges += 1
        accused[callee] = accused.get(callee, 0) + 1
        explained[episode.service] = explained.get(episode.service, 0) + 1
    return accused, explained, edges
