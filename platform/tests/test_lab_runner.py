"""The lab-side runner: claim one run, finish it, and free the slot either way.

The one behaviour that matters most here is the failure path. A runner that
died quietly after claiming would leave the launcher permanently busy over a
run nobody is doing, and the whole point of the one-in-flight rule is that
"busy" means something.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lab.runner import LabRunner, RunOutcome

from contracts import LabRunHonesty, LabRunMode, LabRunSnapshot, LabRunState

TS = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

HONESTY = LabRunHonesty(
    telemetry="REAL — recorded from the testbed.",
    stimulus="SIMULATED — injected.",
    reproducibility="Bit-exact.",
)


def _queued() -> LabRunSnapshot:
    return LabRunSnapshot(
        run_id="run-under-test",
        scenario_id="combo_night",
        mode=LabRunMode.REPLAY,
        state=LabRunState.QUEUED,
        requested_at=TS,
        detail="Queued.",
        honesty=HONESTY,
    )


class _Queue:
    def __init__(self, run: LabRunSnapshot | None) -> None:
        self.pending = run
        self.completed: list[LabRunSnapshot] = []

    async def claim_lab_run(
        self,
        *,
        worker_id: str,
        ts: datetime,
        lease_seconds: int,
    ) -> LabRunSnapshot | None:
        del worker_id, lease_seconds
        if self.pending is None:
            return None
        claimed = self.pending.model_copy(update={"state": LabRunState.RUNNING, "started_at": ts})
        self.pending = None
        return claimed

    async def complete_lab_run(self, run: LabRunSnapshot) -> bool:
        self.completed.append(run)
        return True


def _runner(queue: _Queue, execute: object, *, notified: list[int] | None = None) -> LabRunner:
    async def notify() -> int:
        if notified is not None:
            notified.append(1)
        return 1

    return LabRunner(
        queue=queue,
        execute=execute,  # type: ignore[arg-type]
        worker_id="runner-under-test",
        notify=notify,
        clock=lambda: TS + timedelta(seconds=5),
    )


def test_an_empty_queue_is_not_an_error() -> None:
    queue = _Queue(None)

    async def execute(run: LabRunSnapshot) -> RunOutcome:  # pragma: no cover - never called
        raise AssertionError("nothing was queued")

    assert asyncio.run(_runner(queue, execute).run_once()) is None
    assert queue.completed == []


def test_a_successful_run_records_the_incident_it_produced() -> None:
    queue = _Queue(_queued())

    async def execute(run: LabRunSnapshot) -> RunOutcome:
        return RunOutcome(succeeded=True, detail="Replayed it.", incident_id="incident-42")

    finished = asyncio.run(_runner(queue, execute).run_once())

    assert finished is not None
    assert finished.state is LabRunState.SUCCEEDED
    assert finished.incident_id == "incident-42"
    assert finished.finished_at is not None
    assert queue.completed == [finished]


def test_a_crashing_run_still_frees_the_slot_and_says_what_happened() -> None:
    """Otherwise the launcher stays busy forever over a run nobody is doing."""
    queue = _Queue(_queued())

    async def execute(run: LabRunSnapshot) -> RunOutcome:
        raise RuntimeError("the testbed was unreachable")

    finished = asyncio.run(_runner(queue, execute).run_once())

    assert finished is not None
    assert finished.state is LabRunState.FAILED
    assert "the testbed was unreachable" in finished.detail
    assert finished.finished_at is not None
    assert queue.completed == [finished]


def test_a_failed_run_names_no_incident() -> None:
    queue = _Queue(_queued())

    async def execute(run: LabRunSnapshot) -> RunOutcome:
        return RunOutcome(succeeded=False, detail="nothing to replay", incident_id="incident-9")

    finished = asyncio.run(_runner(queue, execute).run_once())

    assert finished is not None
    assert finished.incident_id is None, "only a succeeded run may claim an incident"


def test_browsers_are_told_when_it_starts_and_when_it_ends() -> None:
    queue = _Queue(_queued())
    notified: list[int] = []

    async def execute(run: LabRunSnapshot) -> RunOutcome:
        return RunOutcome(succeeded=True, detail="done")

    asyncio.run(_runner(queue, execute, notified=notified).run_once())

    assert len(notified) == 2, "a run that only announced its end would look instant"


def test_a_finished_time_never_precedes_the_start() -> None:
    """A clock that moved backwards must not produce a row the contract refuses."""
    queue = _Queue(_queued())

    async def execute(run: LabRunSnapshot) -> RunOutcome:
        return RunOutcome(succeeded=True, detail="done")

    runner = LabRunner(
        queue=queue,
        execute=execute,
        worker_id="runner-under-test",
        clock=lambda: TS - timedelta(hours=1),
    )
    finished = asyncio.run(runner.run_once())

    assert finished is not None
    assert finished.finished_at is not None
    assert finished.finished_at >= finished.requested_at


def test_the_runner_never_spends_a_held_out_seed() -> None:
    """A demo button is the last place that one-way door should be opened."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[2] / "lab" / "runner" / "service.py"
    ).read_text(encoding="utf-8")

    assert "SeedPurpose.DEVELOPMENT" in source
    assert "HELD_OUT" not in source
    assert "held_out" not in source.replace("held-out", "")


def test_replaying_with_no_recorded_capture_fails_rather_than_reporting_success(
    tmp_path: Path,
) -> None:
    """A run that did nothing must never come back green."""
    from lab.runner.service import execute_run

    outcome = asyncio.run(execute_run(_queued(), repo_root=tmp_path))

    assert not outcome.succeeded
    assert "nothing to replay" in outcome.detail
    assert outcome.incident_id is None
