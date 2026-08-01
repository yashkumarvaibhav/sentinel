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
from pathlib import Path

from api.notify import PostgresInvalidationBroker
from common.settings import Settings
from common.storage import PostgresRepository, create_postgres_pool
from lab.runner.service import DEFAULT_CLAIM_SECONDS, LabRunner, RunOutcome, execute_run

LOGGER = logging.getLogger("sentinel.lab-runner")

# An idle poll is one cheap indexed query, and a demo button that took thirty
# seconds to visibly start would feel broken.
POLL_SECONDS = 2.0


async def run(*, repo_root: Path, poll_seconds: float) -> None:
    """Claim and execute until cancelled."""
    config = Settings().model_copy(update={"config_dir": repo_root / "config"})
    pool = create_postgres_pool(config)
    await pool.open(wait=True)
    try:
        store = PostgresRepository(pool=pool, schema=config.postgres_schema)
        broker = PostgresInvalidationBroker(pool=pool)

        async def execute(run_snapshot: object) -> RunOutcome:
            return await execute_run(run_snapshot, repo_root=repo_root)  # type: ignore[arg-type]

        runner = LabRunner(
            queue=store,
            execute=execute,
            worker_id=f"lab-runner-{socket.gethostname()}",
            notify=broker.drain,
            claim_seconds=DEFAULT_CLAIM_SECONDS,
        )
        LOGGER.info("lab runner ready repo=%s poll=%.1fs", repo_root, poll_seconds)
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
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(repo_root=args.repo_root.resolve(), poll_seconds=args.poll_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
