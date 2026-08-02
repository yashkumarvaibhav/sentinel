"""Isolated deterministic decision computation used during visual playback."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from common.settings import Settings
from common.storage import PostgresRepository, create_postgres_pool
from lab.runner.service import replay_decision


async def _run(*, repo_root: Path, run_id: str) -> int:
    config = Settings().model_copy(update={"config_dir": repo_root / "config"})
    pool = create_postgres_pool(config)
    await pool.open(wait=True)
    try:
        store = PostgresRepository(pool=pool, schema=config.postgres_schema)
        run = await store.get_lab_run(run_id)
        if run is None:
            raise RuntimeError(f"lab run {run_id} disappeared before replay")
        outcome = await replay_decision(run, repo_root=repo_root)
        print(json.dumps(outcome.payload(), allow_nan=False, sort_keys=True), flush=True)
        return 0
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.runner.replay_decision")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    return asyncio.run(_run(repo_root=args.repo_root.resolve(), run_id=args.run_id))


if __name__ == "__main__":
    raise SystemExit(main())
