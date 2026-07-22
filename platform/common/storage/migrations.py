"""Versioned, idempotent migrations for Sentinel's two durable stores."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import httpx
from psycopg import sql

from common.settings import Settings
from common.storage._clickhouse import execute as clickhouse_execute
from common.storage._clickhouse import rows as clickhouse_rows
from common.storage.pool import PostgresPool

type StorageEngine = Literal["clickhouse", "postgres"]

_SQL_ROOT = Path(__file__).with_name("sql")
_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")
_TEMPLATE_TOKEN = re.compile(r"{{(?P<name>[a-z_]+)}}")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Migration:
    """One ordered SQL migration loaded from the repository."""

    version: str
    path: Path
    sql: str


def load_migrations(engine: StorageEngine) -> tuple[Migration, ...]:
    """Load an engine's migrations in version order, rejecting ambiguous versions."""
    directory = _SQL_ROOT / engine
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"invalid migration filename: {path.name}")
        migrations.append(
            Migration(
                version=match.group("version"),
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )
    versions = [migration.version for migration in migrations]
    if versions != sorted(set(versions)):
        raise ValueError(f"duplicate or unordered {engine} migration versions")
    return tuple(migrations)


def render_migration(migration: Migration, **values: str | int) -> str:
    """Render only validated identifiers and integer retention values into owned SQL."""
    required = {match.group("name") for match in _TEMPLATE_TOKEN.finditer(migration.sql)}
    if required != values.keys():
        missing = sorted(required - values.keys())
        extra = sorted(values.keys() - required)
        raise ValueError(f"migration template mismatch; missing={missing}, extra={extra}")

    rendered = migration.sql
    for name, value in values.items():
        if isinstance(value, int):
            if name != "retention_days" or not 1 <= value <= 365:
                raise ValueError(f"invalid integer migration value: {name}")
            replacement = str(value)
        else:
            if _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"invalid migration identifier: {name}")
            replacement = value
        rendered = rendered.replace(f"{{{{{name}}}}}", replacement)
    return rendered


async def migrate_storage(
    config: Settings,
    *,
    clickhouse_client: httpx.AsyncClient,
    postgres_pool: PostgresPool,
) -> None:
    """Bring ClickHouse and PostgreSQL to the repository's current schema versions."""
    await _migrate_clickhouse(config, clickhouse_client)
    await _migrate_postgres(config, postgres_pool)


async def _migrate_clickhouse(config: Settings, client: httpx.AsyncClient) -> None:
    database = config.clickhouse_database
    await clickhouse_execute(client, f"CREATE DATABASE IF NOT EXISTS {database}")
    await clickhouse_execute(
        client,
        f"""
        CREATE TABLE IF NOT EXISTS {database}.schema_migrations
        (
            version String,
            applied_at DateTime64(6, 'UTC') DEFAULT now64(6)
        )
        ENGINE = ReplacingMergeTree(applied_at)
        ORDER BY version
        """,
    )
    rows = await clickhouse_rows(
        client,
        f"SELECT version FROM {database}.schema_migrations FINAL FORMAT JSONEachRow",
    )
    applied = {cast(str, row["version"]) for row in rows}
    for migration in load_migrations("clickhouse"):
        if migration.version in applied:
            continue
        rendered = render_migration(
            migration,
            database=database,
            retention_days=config.clickhouse_retention_days,
        )
        for statement in _split_sql_statements(rendered):
            await clickhouse_execute(client, statement)
        await clickhouse_execute(
            client,
            f"INSERT INTO {database}.schema_migrations (version) VALUES ('{migration.version}')",
        )


async def _migrate_postgres(config: Settings, pool: PostgresPool) -> None:
    schema = config.postgres_schema
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
            )
            await connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.schema_migrations
                    (
                        version text PRIMARY KEY,
                        applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                    )
                    """
                ).format(sql.Identifier(schema))
            )
            cursor = await connection.execute(
                sql.SQL("SELECT version FROM {}.schema_migrations").format(sql.Identifier(schema))
            )
            applied = {cast(str, row[0]) async for row in cursor}

        available: dict[str, str] = {
            "schema": schema,
            "dev_schema": config.postgres_dev_schema,
        }
        for migration in load_migrations("postgres"):
            if migration.version in applied:
                continue
            tokens = {match.group("name") for match in _TEMPLATE_TOKEN.finditer(migration.sql)}
            unknown = sorted(tokens - available.keys())
            if unknown:
                raise ValueError(
                    f"{migration.path.name}: unknown template tokens: {', '.join(unknown)}"
                )
            rendered = render_migration(
                migration,
                **{name: available[name] for name in tokens},
            )
            async with connection.transaction():
                await connection.execute(rendered, prepare=False)
                await connection.execute(
                    sql.SQL("INSERT INTO {}.schema_migrations (version) VALUES (%s)").format(
                        sql.Identifier(schema)
                    ),
                    (migration.version,),
                )


def _split_sql_statements(script: str) -> tuple[str, ...]:
    """Split repository-owned ClickHouse DDL without requesting HTTP multiquery mode."""
    statements = tuple(statement.strip() for statement in script.split(";") if statement.strip())
    if not statements:
        raise ValueError("migration contains no SQL statements")
    return statements
