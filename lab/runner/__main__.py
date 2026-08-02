"""Long-running lab runner: claim queued scenario runs and carry them out.

Runs beside the stack with the repo mounted, never inside the gateway image.
That separation is the whole safety argument for the demo launcher: the public
API can record what somebody wants and nothing more.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from api.invalidation import snapshot_invalidation
from api.notify import PostgresInvalidationBroker
from common.settings import Settings
from common.storage import PostgresRepository, create_postgres_pool
from contracts import (
    LabRunControl,
    LabRunnerHeartbeat,
    LabRunSnapshot,
    SnapshotResource,
)
from lab.runner.service import (
    DEFAULT_CLAIM_SECONDS,
    LabRunner,
    RunOutcome,
    execute_run_subprocess,
)
from lab.scoring.live import live_preflight

LOGGER = logging.getLogger("sentinel.lab-runner")

# An idle poll is one cheap indexed query, and a demo button that took thirty
# seconds to visibly start would feel broken.
POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 5.0


async def _heartbeat(
    *,
    store: PostgresRepository,
    broker: PostgresInvalidationBroker,
    worker_id: str,
    repo_root: Path,
) -> None:
    """Prove the runner exists and whether LIVE can be accepted right now."""
    previous: tuple[bool, str] | None = None
    while True:
        try:
            live_ready, detail = await asyncio.to_thread(live_preflight, repo_root)
            await store.put_lab_runner_heartbeat(
                LabRunnerHeartbeat(
                    worker_id=worker_id,
                    seen_at=datetime.now(UTC),
                    live_ready=live_ready,
                    detail=detail,
                )
            )
            current = (live_ready, detail)
            if current != previous:
                broker.publish(snapshot_invalidation(SnapshotResource.LAB))
                await broker.drain()
                LOGGER.info("lab runner capability live_ready=%s detail=%s", live_ready, detail)
                previous = current
        except Exception:
            # A transient database or notification failure must not kill the
            # capability loop forever. The old heartbeat naturally goes stale
            # while retries continue, so the gateway still fails closed.
            LOGGER.exception("lab runner heartbeat failed; retrying")
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def run(*, repo_root: Path, poll_seconds: float) -> None:
    """Claim and execute until cancelled."""
    config = Settings().model_copy(update={"config_dir": repo_root / "config"})
    pool = create_postgres_pool(config)
    await pool.open(wait=True)
    try:
        store = PostgresRepository(pool=pool, schema=config.postgres_schema)
        broker = PostgresInvalidationBroker(pool=pool)
        heartbeat_broker = PostgresInvalidationBroker(pool=pool)
        worker_id = f"lab-runner-{socket.gethostname()}"
        heartbeat = asyncio.create_task(
            _heartbeat(
                store=store,
                broker=heartbeat_broker,
                worker_id=worker_id,
                repo_root=repo_root,
            ),
            name="lab-runner-heartbeat",
        )

        async def execute(run_snapshot: LabRunSnapshot) -> RunOutcome:
            return await execute_run_subprocess(run_snapshot, repo_root=repo_root)

        async def requested_control(run_id: str) -> LabRunControl | None:
            current = await store.get_lab_run(run_id)
            return None if current is None else current.control_requested

        runner = LabRunner(
            queue=store,
            execute=execute,
            worker_id=worker_id,
            notify=broker.drain,
            control=requested_control,
            claim_seconds=DEFAULT_CLAIM_SECONDS,
        )
        LOGGER.info("lab runner ready repo=%s poll=%.1fs", repo_root, poll_seconds)
        try:
            while True:
                try:
                    finished = await runner.run_once()
                except Exception:
                    # One bad run never stops the runner; the slot was already
                    # released by the claim's own completion path.
                    LOGGER.exception("a lab run claim failed")
                    finished = None
                if finished is not None:
                    LOGGER.info(
                        "lab run %s finished state=%s detail=%s",
                        finished.run_id,
                        finished.state.value,
                        finished.detail,
                    )
                await asyncio.sleep(poll_seconds if finished is None else 0.0)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.runner")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # The five-second readiness probe is useful state, not five lines a minute
    # saying HTTP 200. Capability changes are logged explicitly by _heartbeat.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(repo_root=args.repo_root.resolve(), poll_seconds=args.poll_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
