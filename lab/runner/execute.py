"""One isolated, interruptible execution claimed by the durable lab runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from api.invalidation import snapshot_invalidation
from api.notify import PostgresInvalidationBroker
from common.settings import Settings
from common.storage import PostgresRepository, create_postgres_pool
from contracts import SnapshotResource
from lab.runner.service import execute_run


class _OperatorInterrupt(BaseException):
    """Leave normal exception handlers alone while still running cleanup."""


def _interrupt(_signum: int, _frame: object) -> None:
    raise _OperatorInterrupt


async def _execute(*, repo_root: Path, run_id: str) -> int:
    config = Settings().model_copy(update={"config_dir": repo_root / "config"})
    pool = create_postgres_pool(config)
    await pool.open(wait=True)
    try:
        store = PostgresRepository(pool=pool, schema=config.postgres_schema)
        broker = PostgresInvalidationBroker(pool=pool)
        run = await store.get_lab_run(run_id)
        if run is None:
            raise RuntimeError(f"lab run {run_id} disappeared before execution")

        async def report(
            *,
            progress: float,
            detail: str,
            evidence_start_at: datetime,
            evidence_end_at: datetime,
            evidence_cursor_at: datetime,
        ) -> None:
            await store.update_lab_run_progress(
                run_id=run_id,
                progress=progress,
                detail=detail,
                evidence_start_at=evidence_start_at,
                evidence_end_at=evidence_end_at,
                evidence_cursor_at=evidence_cursor_at,
            )
            broker.publish(
                snapshot_invalidation(SnapshotResource.LAB, SnapshotResource.DECOMPOSITION)
            )
            await broker.drain()

        outcome = await execute_run(run, repo_root=repo_root, progress=report)
        print(json.dumps(outcome.payload(), allow_nan=False, sort_keys=True), flush=True)
        return 0
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.runner.execute")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        return asyncio.run(_execute(repo_root=args.repo_root.resolve(), run_id=args.run_id))
    except _OperatorInterrupt:
        # No success payload: the parent owns the PAUSED/STOPPED transition,
        # after this process has unwound every live resource in its finally blocks.
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
