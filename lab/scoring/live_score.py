"""Grade what the always-on producer *stored*, not what a replay concludes.

Every scorer before this one drives a recorded capture back through the code
and grades the transcript. That proves the logic. It does not prove the
product: an operator never sees a transcript, they see the rows the live
producer wrote while the traffic was actually happening, and those rows are
what the MVP acceptance item is about.

**The join is the run itself.** Recording a capture while the producer is up
yields two things from one stretch of traffic: durable incidents judged live,
and a recording whose private labels were materialized to the *measured*
offsets of the stimuli that caused them. So the same anchor dates both, and the
answer key can be laid over the stored rows without either one being told about
the other. Nothing here is reachable from the runtime; the labels are opened
only after the rows have been read.

**What a stored row is, and is not.** The incident store keeps the current
state of each incident, not its trajectory - a newer revision replaces an older
one by design. So this scorer grades the *last thing said* about each incident,
where the replay scorer grades everything ever said. That is a weaker question
and a more honest one: it is exactly the claim the product puts in front of a
person. Where the two can disagree is a window whose incident was surfaced
correctly and then aged out of view before the run ended; the report states the
distinction rather than hiding it.

**Preconditions fail closed.** A producer that started after the scenario, or
stopped before it finished, did not observe the run - and grading a partial
observation would credit the platform for windows nobody was watching. Both are
refused rather than scored. So is a held-out seed without an explicit
acknowledgement: a held-out seed is spent by being scored, and that is a
one-way door the operator has to open deliberately.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from common.storage.models import IncidentRecord, LiveProducerCheckpoint
from contracts import IncidentFeedItem
from lab.captures.detection_replay import scenario_bounds
from lab.captures.store import RuntimeCapture, load_runtime_capture
from lab.scoring.decision_score import (
    DecisionScore,
    Judgement,
    load_capture_labels,
    resolve_scenario_windows,
    score_judgements,
)


class LiveRunScoringError(RuntimeError):
    """The stored rows cannot be graded against this run, and will not be."""


class StoredIncidentReader(Protocol):
    """The durable state this scorer reads, and the position that dates it."""

    async def list_incidents(self, *, limit: int) -> tuple[IncidentRecord, ...]: ...

    async def get_live_producer_checkpoint(
        self, producer_id: str
    ) -> LiveProducerCheckpoint | None: ...


@dataclass(frozen=True, slots=True)
class RunBounds:
    """The stretch of real time one recorded scenario run occupied."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: Literal["development", "held_out"]
    anchor_ts: datetime
    end_ts: datetime

    def offset(self, moment: datetime) -> float:
        return round((moment - self.anchor_ts).total_seconds(), 6)


@dataclass(frozen=True, slots=True)
class ObservationCoverage:
    """Proof that the producer was actually watching for the whole run."""

    producer_id: str
    anchor_ts: datetime
    tick_ts: datetime
    published_incidents: int

    def covers(self, bounds: RunBounds) -> bool:
        return self.anchor_ts <= bounds.anchor_ts and self.tick_ts >= bounds.end_ts


@dataclass(frozen=True, slots=True)
class LiveRunScore:
    """One live run's stored state, graded, with the provenance that dates it."""

    bounds: RunBounds
    coverage: ObservationCoverage
    score: DecisionScore
    stored_incidents: int
    considered_incidents: int
    # Incidents that were already open when the run began. They are graded,
    # because they are genuinely on the operator's board and answering for the
    # services this run asks about - but a run that starts on a dirty board is
    # asking a harder question than one that starts clean, and the report has
    # to say which was measured rather than leaving it to be discovered.
    inherited_incidents: tuple[str, ...] = ()


def run_bounds(capture: RuntimeCapture) -> RunBounds:
    """When the recorded run started and ended, from public capture data only."""
    anchor_ts, end_ts = scenario_bounds(capture)
    manifest = capture.manifest
    return RunBounds(
        capture_id=manifest.capture_id,
        scenario_id=manifest.scenario_id,
        seed=manifest.seed,
        seed_purpose=manifest.seed_purpose,
        anchor_ts=anchor_ts,
        end_ts=end_ts,
    )


def stored_judgements(
    records: Sequence[IncidentRecord],
    *,
    bounds: RunBounds,
) -> tuple[Judgement, ...]:
    """Read the durable rows that belong to this run, in grading terms.

    A row that never overlapped the run is not evidence about the run - the
    store is a lifetime record and other traffic put incidents in it - so it is
    dropped here rather than being graded or counted as a mistake.

    The card carries no action *target*, because a card is what a decision
    concluded and not what an actuator was pointed at. A false act read from
    the store therefore names the incident and the action but no target, which
    is the whole of what the store knows.
    """
    horizon = bounds.offset(bounds.end_ts)
    judgements: list[Judgement] = []
    for record in records:
        item = _item(record)
        opened = bounds.offset(item.opened_at)
        updated = bounds.offset(item.updated_at)
        if updated < 0.0 or opened > horizon:
            continue
        judgements.append(
            Judgement(
                # A stored row is dated by the last judgement written into it,
                # which is the only moment the store retains.
                offset_seconds=updated,
                incident_id=item.incident_id,
                opened_offset_seconds=opened,
                last_activity_offset_seconds=updated,
                services=frozenset(item.services),
                verdict_class=(None if item.verdict_class is None else item.verdict_class.value),
                action=item.action.decision_action,
                origin_service=item.origin_service,
                target_service=None,
            )
        )
    return tuple(sorted(judgements, key=lambda item: (item.offset_seconds, item.incident_id)))


def score_live_run(
    records: Sequence[IncidentRecord],
    checkpoint: LiveProducerCheckpoint | None,
    *,
    bounds: RunBounds,
    scenario_root: Path,
    labels: Mapping[str, object],
    spend_held_out_seed: bool = False,
) -> LiveRunScore:
    """Grade the stored incidents one live run produced against its answer key."""
    if bounds.seed_purpose == "held_out" and not spend_held_out_seed:
        raise LiveRunScoringError(
            f"{bounds.capture_id} carries a held-out seed, which is spent by being "
            "scored; that has to be an explicit decision, not a default"
        )
    coverage = _coverage(checkpoint, bounds=bounds)
    windows = resolve_scenario_windows(
        bounds.scenario_id,
        scenario_root=scenario_root,
        labels=labels,
    )
    judgements = stored_judgements(records, bounds=bounds)
    outcomes, false_acts = score_judgements(judgements, windows)
    return LiveRunScore(
        bounds=bounds,
        coverage=coverage,
        score=DecisionScore(
            capture_id=bounds.capture_id,
            scenario_id=bounds.scenario_id,
            seed=bounds.seed,
            seed_purpose=bounds.seed_purpose,
            windows=outcomes,
            false_acts=false_acts,
            decision_count=len(judgements),
        ),
        stored_incidents=len(records),
        considered_incidents=len(judgements),
        inherited_incidents=tuple(
            judgement.incident_id
            for judgement in judgements
            if judgement.opened_offset_seconds < 0.0
        ),
    )


async def read_live_run(
    reader: StoredIncidentReader,
    *,
    producer_id: str,
    limit: int,
) -> tuple[tuple[IncidentRecord, ...], LiveProducerCheckpoint | None]:
    """Read the durable state and the position that says how far it is current."""
    records = await reader.list_incidents(limit=limit)
    if len(records) >= limit:
        # A full page is not a complete read. Grading it would quietly score a
        # truncated view of the store and call the windows it never saw missed.
        raise LiveRunScoringError(
            f"the incident feed returned its full page of {limit} rows, so this run's "
            "stored state may be truncated; it is not graded rather than graded partially"
        )
    checkpoint = await reader.get_live_producer_checkpoint(producer_id)
    return records, checkpoint


def load_run_labels(root: Path) -> Mapping[str, object]:
    """Open the run's answer key. Call this only after the rows have been read."""
    return load_capture_labels(root)


def load_run_capture(root: Path) -> RuntimeCapture:
    """The public recording of the run whose stored state is being graded."""
    return load_runtime_capture(root)


def _coverage(
    checkpoint: LiveProducerCheckpoint | None,
    *,
    bounds: RunBounds,
) -> ObservationCoverage:
    if checkpoint is None:
        raise LiveRunScoringError(
            "no live producer checkpoint exists, so nothing was watching this run; "
            "an empty feed is not evidence that nothing was wrong"
        )
    coverage = ObservationCoverage(
        producer_id=checkpoint.producer_id,
        anchor_ts=checkpoint.anchor_ts,
        tick_ts=checkpoint.tick_ts,
        published_incidents=checkpoint.published_incidents,
    )
    if not coverage.covers(bounds):
        raise LiveRunScoringError(
            "the live producer did not observe the whole run "
            f"({bounds.anchor_ts.isoformat()} .. {bounds.end_ts.isoformat()}): it was "
            f"anchored at {coverage.anchor_ts.isoformat()} and durably reached "
            f"{coverage.tick_ts.isoformat()}"
        )
    return coverage


def _item(record: IncidentRecord) -> IncidentFeedItem:
    """Re-read a stored row through its own contract, refusing an incoherent one."""
    item = IncidentFeedItem.model_validate_json(
        json.dumps(record.payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    )
    if item.incident_id != record.incident_id:
        raise LiveRunScoringError("stored incident id disagrees with its payload")
    if item.state.value != record.state:
        raise LiveRunScoringError("stored incident state disagrees with its payload")
    if item.opened_at != record.created_at or item.updated_at != record.updated_at:
        raise LiveRunScoringError("stored incident timestamps disagree with its payload")
    return item


__all__ = [
    "LiveRunScore",
    "LiveRunScoringError",
    "ObservationCoverage",
    "RunBounds",
    "StoredIncidentReader",
    "load_run_capture",
    "load_run_labels",
    "read_live_run",
    "run_bounds",
    "score_live_run",
    "stored_judgements",
]
