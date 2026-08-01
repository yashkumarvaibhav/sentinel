"""Claim one queued scenario run and carry it out.

This is the process the public API is not. The gateway records intent into a
Postgres queue and stops there, because its image carries no ``lab/`` at all -
that boundary is what keeps scenario ground truth unreachable from the process
serving the public API. This runner has the repo mounted, so it can read
captures, drive k6 and inject chaos, and it is deliberately not reachable from
a browser.

Two modes, and they are genuinely different work:

* **REPLAY** re-drives a committed development capture through the decision
  plane and publishes the incident it reaches. Instant and bit-exact, which is
  what makes it the mode a demo can be given in front of people.
* **LIVE** drives real load and real injected faults at the running testbed and
  lets the always-on producer judge what comes back. Real minutes, and only
  statistically reproducible.

A run that fails says so. The one thing this must never do is finish quietly
after doing nothing, because the launcher's whole claim is that a button did
something real.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from contracts import LabRunMode, LabRunSnapshot, LabRunState

LOGGER = logging.getLogger(__name__)

# Long enough that a live combo_night (about seventeen minutes of load, plus
# span settling) never looks abandoned, short enough that a runner killed
# mid-run frees the slot the same afternoon.
DEFAULT_CLAIM_SECONDS = 3_600


class LabRunQueue(Protocol):
    """The durable queue this runner claims from and reports back to."""

    async def claim_lab_run(
        self,
        *,
        worker_id: str,
        ts: datetime,
        lease_seconds: int,
    ) -> LabRunSnapshot | None: ...

    async def complete_lab_run(self, run: LabRunSnapshot) -> bool: ...


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one execution achieved, in the terms the launcher reports."""

    succeeded: bool
    detail: str
    incident_id: str | None = None


class LabRunner:
    """Claim at most one run, execute it, and record what happened."""

    def __init__(
        self,
        *,
        queue: LabRunQueue,
        execute: Callable[[LabRunSnapshot], Awaitable[RunOutcome]],
        worker_id: str,
        notify: Callable[[], Awaitable[int]] | None = None,
        claim_seconds: int = DEFAULT_CLAIM_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._queue = queue
        self._execute = execute
        self._worker_id = worker_id
        self._notify = notify
        self._claim_seconds = claim_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self) -> LabRunSnapshot | None:
        """Take the queued run if there is one, and finish it either way."""
        claimed = await self._queue.claim_lab_run(
            worker_id=self._worker_id,
            ts=self._clock(),
            lease_seconds=self._claim_seconds,
        )
        if claimed is None:
            return None
        await self._announce()
        try:
            outcome = await self._execute(claimed)
        except Exception as error:
            # A crash here must still free the one in-flight slot, or the
            # launcher stays busy forever over a run nobody is doing.
            LOGGER.exception("lab run %s failed", claimed.run_id)
            outcome = RunOutcome(succeeded=False, detail=f"the run failed: {error}")
        finished = _finish(claimed, outcome, ts=self._clock())
        await self._queue.complete_lab_run(finished)
        await self._announce()
        return finished

    async def _announce(self) -> None:
        if self._notify is not None:
            await self._notify()


def _finish(run: LabRunSnapshot, outcome: RunOutcome, *, ts: datetime) -> LabRunSnapshot:
    """The terminal row, with a time that cannot precede the start.

    Clamped against the request as well as the start, because those are two
    separate clocks: the request was timed by the gateway and the claim by this
    runner, and a row that finished before it was asked for is one the contract
    refuses to store at all - which would strand the run mid-flight.
    """
    began = max(run.requested_at, run.started_at or run.requested_at)
    return run.model_copy(
        update={
            "state": LabRunState.SUCCEEDED if outcome.succeeded else LabRunState.FAILED,
            "finished_at": max(ts, began),
            "incident_id": outcome.incident_id if outcome.succeeded else None,
            "detail": outcome.detail,
        }
    )


async def execute_run(run: LabRunSnapshot, *, repo_root: Path) -> RunOutcome:
    """Do the work one claimed run asks for.

    Imported lazily on purpose: the replay path pulls in the whole decision
    plane and the live path shells out to kubectl, and a runner idling on an
    empty queue should not be holding either.
    """
    if run.mode is LabRunMode.REPLAY:
        return await _replay(run, repo_root=repo_root)
    return await _live(run, repo_root=repo_root)


async def _replay(run: LabRunSnapshot, *, repo_root: Path) -> RunOutcome:
    import asyncio

    from common.config import load_config
    from common.settings import Settings
    from lab.scoring.decisions import load_decision_configs, replay_capture_decisions
    from lab.scoring.publish import CaptureReplayPublisher, commit_capture_publication

    capture = _newest_capture(repo_root, scenario_id=run.scenario_id)
    if capture is None:
        return RunOutcome(
            succeeded=False,
            detail=(
                f"no committed development capture of {run.scenario_id} is present, so there "
                "is nothing to replay; record one with `make capture` first"
            ),
        )
    config_root = repo_root / "config"
    config = load_config(config_root)
    replay = await asyncio.to_thread(
        replay_capture_decisions,
        capture,
        config=config,
        decisions=load_decision_configs(config_root),
    )
    result, _ = await commit_capture_publication(
        replay=replay,
        producer=CaptureReplayPublisher(config=config, config_root=config_root),
        runtime=Settings().model_copy(update={"config_dir": config_root}),
    )
    return RunOutcome(
        succeeded=True,
        detail=(
            f"Replayed {capture.name} and published incident {result.incident_id[:12]}. "
            "Telemetry is real and recorded; the plan is terminally simulated."
        ),
        incident_id=result.incident_id,
    )


async def _live(run: LabRunSnapshot, *, repo_root: Path) -> RunOutcome:
    import asyncio

    from lab.scenarios import SeedPurpose, compile_profile, load_profile
    from lab.scoring.live import run_live_scenario

    profile = load_profile(repo_root / "lab" / "scenarios" / f"{run.scenario_id}.yml")
    seeds = profile.seeds.development
    if not seeds:
        return RunOutcome(
            succeeded=False,
            detail=f"{run.scenario_id} commits no development seed, and a demo never spends a "
            "held-out one",
        )
    # Always a development seed. A held-out seed is spent by being used, and a
    # demo button is the last place that decision should be made.
    artifacts = compile_profile(profile, seed=seeds[0], purpose=SeedPurpose.DEVELOPMENT)
    telemetry = await asyncio.to_thread(
        run_live_scenario,
        repo_root=repo_root,
        artifacts=artifacts,
        invocation=run.run_id[-8:],
    )
    return RunOutcome(
        succeeded=True,
        detail=(
            f"Drove {run.scenario_id} at the testbed from {telemetry.start_at.isoformat()} "
            f"with {len(telemetry.stimulus_executions)} injected stimuli. The always-on "
            "producer judged the telemetry it produced."
        ),
    )


def _newest_capture(repo_root: Path, *, scenario_id: str) -> Path | None:
    """The most recently recorded development capture of this scenario.

    Read from the capture store rather than named in configuration: a demo that
    pointed at a capture id would break the first time one was re-recorded, and
    silently replay a stale one if it did not.
    """
    root = repo_root / "var" / "captures"
    if not root.is_dir():
        return None
    candidates = [
        path
        for path in sorted(root.iterdir())
        if path.is_dir() and _describes(path, scenario_id=scenario_id)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _describes(root: Path, *, scenario_id: str) -> bool:
    manifest = root / "manifest.json"
    if not manifest.is_file():
        return False
    try:
        import json

        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(document, dict):
        return False
    return (
        document.get("scenario_id") == scenario_id and document.get("seed_purpose") == "development"
    )
