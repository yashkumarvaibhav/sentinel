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

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from contracts import LabRunControl, LabRunMode, LabRunSnapshot, LabRunState

LOGGER = logging.getLogger(__name__)

# Long enough that a live combo_night (about seventeen minutes of load, plus
# span settling) never looks abandoned, short enough that a runner killed
# mid-run frees the slot the same afternoon.
DEFAULT_CLAIM_SECONDS = 3_600
CONTROL_POLL_SECONDS = 0.5
# combo_night's unchanged six-detector decision replay takes about nine minutes
# on the demo host. Spread its 378 recorded frames across that work so the
# command centre keeps moving for the verifier's real lifetime. The loop exits
# as soon as verification does, so faster hosts never wait for presentation.
REPLAY_PRESENTATION_SECONDS = 600


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


class RunProgressReporter(Protocol):
    async def __call__(
        self,
        *,
        progress: float,
        detail: str,
        evidence_start_at: datetime,
        evidence_end_at: datetime,
        evidence_cursor_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one execution achieved, in the terms the launcher reports."""

    succeeded: bool
    detail: str
    incident_id: str | None = None
    state: LabRunState | None = None

    @classmethod
    def paused(cls, detail: str) -> RunOutcome:
        return cls(succeeded=False, detail=detail, state=LabRunState.PAUSED)

    @classmethod
    def stopped(cls, detail: str) -> RunOutcome:
        return cls(succeeded=False, detail=detail, state=LabRunState.STOPPED)

    def payload(self) -> dict[str, object]:
        return {
            "succeeded": self.succeeded,
            "detail": self.detail,
            "incident_id": self.incident_id,
            "state": None if self.state is None else self.state.value,
        }

    @classmethod
    def from_payload(cls, value: object) -> RunOutcome:
        if not isinstance(value, dict):
            raise ValueError("lab execution result must be an object")
        succeeded = value.get("succeeded")
        detail = value.get("detail")
        incident_id = value.get("incident_id")
        state = value.get("state")
        if not isinstance(succeeded, bool) or not isinstance(detail, str) or not detail.strip():
            raise ValueError("lab execution result is malformed")
        if incident_id is not None and not isinstance(incident_id, str):
            raise ValueError("lab execution incident id is malformed")
        parsed_state = None if state is None else LabRunState(state)
        return cls(
            succeeded=succeeded,
            detail=detail,
            incident_id=incident_id,
            state=parsed_state,
        )


class LabRunner:
    """Claim at most one run, execute it, and record what happened."""

    def __init__(
        self,
        *,
        queue: LabRunQueue,
        execute: Callable[[LabRunSnapshot], Awaitable[RunOutcome]],
        worker_id: str,
        notify: Callable[[], Awaitable[int]] | None = None,
        control: Callable[[str], Awaitable[LabRunControl | None]] | None = None,
        claim_seconds: int = DEFAULT_CLAIM_SECONDS,
        control_poll_seconds: float = CONTROL_POLL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._queue = queue
        self._execute = execute
        self._worker_id = worker_id
        self._notify = notify
        self._control = control
        self._claim_seconds = claim_seconds
        if control_poll_seconds <= 0:
            raise ValueError("control poll interval must be positive")
        self._control_poll_seconds = control_poll_seconds
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
            outcome = await self._execute_with_controls(claimed)
        except Exception as error:
            # A crash here must still free the one in-flight slot, or the
            # launcher stays busy forever over a run nobody is doing.
            LOGGER.exception("lab run %s failed", claimed.run_id)
            outcome = RunOutcome(succeeded=False, detail=f"the run failed: {error}")
        finished = _finish(claimed, outcome, ts=self._clock())
        await self._queue.complete_lab_run(finished)
        await self._announce()
        return finished

    async def _execute_with_controls(self, run: LabRunSnapshot) -> RunOutcome:
        """Run until completion or a durable pause/stop intent arrives."""
        if self._control is None:
            return await self._execute(run)
        execution: asyncio.Future[RunOutcome] = asyncio.ensure_future(self._execute(run))
        try:
            while True:
                done, _ = await asyncio.wait(
                    {execution},
                    timeout=self._control_poll_seconds,
                )
                if done:
                    return execution.result()
                requested = await self._control(run.run_id)
                if requested not in {LabRunControl.PAUSE, LabRunControl.STOP}:
                    continue
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                if requested is LabRunControl.PAUSE:
                    return RunOutcome.paused(
                        "Paused safely after cleanup. Resume restarts the authored schedule "
                        "from the beginning so its evidence windows remain valid."
                    )
                return RunOutcome.stopped(
                    "Stopped by the operator after the runner removed every owned stimulus."
                )
        except BaseException:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise

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
    state = outcome.state or (LabRunState.SUCCEEDED if outcome.succeeded else LabRunState.FAILED)
    terminal = state is not LabRunState.PAUSED
    return run.model_copy(
        update={
            "state": state,
            "finished_at": max(ts, began) if terminal else None,
            "incident_id": outcome.incident_id if state is LabRunState.SUCCEEDED else None,
            "control_requested": None,
            "detail": outcome.detail,
        }
    )


async def execute_run(
    run: LabRunSnapshot,
    *,
    repo_root: Path,
    progress: RunProgressReporter | None = None,
) -> RunOutcome:
    """Do the work one claimed run asks for.

    Imported lazily on purpose: the replay path pulls in the whole decision
    plane and the live path shells out to kubectl, and a runner idling on an
    empty queue should not be holding either.
    """
    if run.mode is LabRunMode.REPLAY:
        return await _replay(run, repo_root=repo_root, progress=progress)
    return await _live(run, repo_root=repo_root)


async def execute_run_subprocess(run: LabRunSnapshot, *, repo_root: Path) -> RunOutcome:
    """Execute in a killable child whose signal handler owns safe cleanup.

    Replay is CPU-bound and live execution blocks inside kubectl. An asyncio
    task around either cannot stop the underlying work. A process boundary is
    what makes a durable pause/stop intent able to interrupt the real work.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lab.runner.execute",
        "--repo-root",
        str(repo_root),
        "--run-id",
        run.run_id,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await process.communicate()
    except asyncio.CancelledError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=120.0)
        except TimeoutError:
            LOGGER.critical("lab execution %s did not finish cleanup after SIGTERM", run.run_id)
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        return RunOutcome(
            succeeded=False,
            detail=f"the isolated lab executor exited with status {process.returncode}",
        )
    try:
        return RunOutcome.from_payload(json.loads(stdout))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return RunOutcome(
            succeeded=False, detail=f"the lab executor returned no valid result: {error}"
        )


async def _replay(
    run: LabRunSnapshot,
    *,
    repo_root: Path,
    progress: RunProgressReporter | None = None,
) -> RunOutcome:
    if progress is None:
        return await replay_decision(run, repo_root=repo_root)

    import httpx

    from common.config import load_config
    from common.settings import Settings
    from common.storage import ClickHouseRepository
    from lab.captures import load_runtime_capture, replay_decomposition

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
    decomposition = replay_decomposition(
        load_runtime_capture(capture),
        detector=config.detectors,
        replay_config_fingerprint=config.fingerprint,
    )
    frames = tuple(step.frame for step in decomposition.steps if step.frame is not None)
    if not frames:
        return RunOutcome(succeeded=False, detail="the capture produced no decomposition frames")
    runtime = Settings().model_copy(update={"config_dir": config_root})
    auth = (runtime.clickhouse_user, runtime.clickhouse_password.get_secret_value())
    async with httpx.AsyncClient(
        base_url=runtime.clickhouse_url,
        auth=auth,
        timeout=runtime.storage_timeout_seconds,
    ) as client:
        await ClickHouseRepository(
            client=client,
            database=runtime.clickhouse_database,
        ).write_decomp_frames(frames)

    decision = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lab.runner.replay_decision",
        "--repo-root",
        str(repo_root),
        "--run-id",
        run.run_id,
        stdout=asyncio.subprocess.PIPE,
    )
    communication = asyncio.create_task(decision.communicate())
    start = frames[0].ts
    end = frames[-1].ts
    try:
        steps = min(max(len(frames), 1), REPLAY_PRESENTATION_SECONDS)
        for step in range(steps):
            fraction = (step + 1) / steps
            index = min(round(fraction * (len(frames) - 1)), len(frames) - 1)
            cursor = frames[index].ts
            await progress(
                progress=0.05 + 0.85 * fraction,
                detail=(
                    "Playing recorded testbed evidence through the command centre "
                    f"({index + 1}/{len(frames)} frames)."
                ),
                evidence_start_at=start,
                evidence_end_at=end,
                evidence_cursor_at=cursor,
            )
            if communication.done():
                break
            if step + 1 < steps:
                await asyncio.sleep(REPLAY_PRESENTATION_SECONDS / steps)
        if not communication.done():
            await progress(
                progress=0.95,
                detail=(
                    "Recorded telemetry is fully drawn; the deterministic verifier is finishing."
                ),
                evidence_start_at=start,
                evidence_end_at=end,
                evidence_cursor_at=end,
            )
        stdout, _ = await communication
    finally:
        if decision.returncode is None:
            decision.terminate()
            try:
                await asyncio.wait_for(decision.wait(), timeout=30.0)
            except TimeoutError:
                decision.kill()
                await decision.wait()
        if not communication.done():
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
    if decision.returncode != 0:
        return RunOutcome(
            succeeded=False,
            detail=f"the deterministic replay verifier exited with status {decision.returncode}",
        )
    try:
        outcome = RunOutcome.from_payload(json.loads(stdout))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return RunOutcome(
            succeeded=False, detail=f"the replay verifier returned no result: {error}"
        )
    await progress(
        progress=1.0,
        detail=outcome.detail,
        evidence_start_at=start,
        evidence_end_at=end,
        evidence_cursor_at=end,
    )
    return outcome


async def replay_decision(run: LabRunSnapshot, *, repo_root: Path) -> RunOutcome:
    """Compute and publish the deterministic terminal decision for one capture."""
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
    replay = replay_capture_decisions(
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
    telemetry = run_live_scenario(
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
