"""Bounded PostgreSQL connection-pool construction."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from common.settings import Settings

type PostgresPool = AsyncConnectionPool[AsyncConnection[TupleRow]]


def create_postgres_pool(config: Settings) -> PostgresPool:
    """Create a closed async pool; the process lifecycle must open and close it explicitly."""
    conninfo = make_conninfo(
        host=config.postgres_host,
        port=config.postgres_port,
        dbname=config.postgres_database,
        user=config.postgres_user,
        password=config.postgres_password.get_secret_value(),
        connect_timeout=max(1, int(config.storage_timeout_seconds)),
    )
    kwargs: dict[str, Any] = {"autocommit": False}
    return AsyncConnectionPool(
        conninfo,
        connection_class=AsyncConnection,
        kwargs=kwargs,
        min_size=config.storage_pool_min_size,
        max_size=config.storage_pool_max_size,
        open=False,
        timeout=config.storage_timeout_seconds,
        check=AsyncConnectionPool.check_connection,
        name="sentinel-storage",
    )
