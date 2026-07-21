"""Datastore migration command."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

import httpx

from common.settings import settings
from common.storage.migrations import migrate_storage
from common.storage.pool import create_postgres_pool


async def _migrate() -> None:
    config = settings()
    auth = (config.clickhouse_user, config.clickhouse_password.get_secret_value())
    pool = create_postgres_pool(config)
    async with httpx.AsyncClient(
        base_url=config.clickhouse_url,
        auth=auth,
        timeout=config.storage_timeout_seconds,
    ) as client:
        await pool.open(wait=True, timeout=config.storage_timeout_seconds)
        try:
            await migrate_storage(config, clickhouse_client=client, postgres_pool=pool)
        finally:
            await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Run storage maintenance commands."""
    parser = argparse.ArgumentParser(prog="python -m common.storage")
    parser.add_argument("command", choices=("migrate",))
    args = parser.parse_args(argv)
    if args.command == "migrate":
        asyncio.run(_migrate())
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


raise SystemExit(main())
