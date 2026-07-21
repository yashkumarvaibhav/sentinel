"""Storage DDL is versioned, retained, and keeps answer keys out of runtime schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.settings import Settings
from common.storage import __all__ as runtime_storage_exports
from common.storage.migrations import load_migrations, render_migration


def test_clickhouse_migration_is_versioned_idempotent_and_retained() -> None:
    migrations = load_migrations("clickhouse")

    assert [migration.version for migration in migrations] == ["0001"]
    sql = render_migration(
        migrations[0],
        database="sentinel_test",
        retention_days=15,
    )
    assert sql.count("CREATE TABLE IF NOT EXISTS sentinel_test.") == 3
    assert "sentinel_test.observations" in sql
    assert "sentinel_test.decomp_frames" in sql
    assert "sentinel_test.symptoms" in sql
    assert sql.count("ReplacingMergeTree(stored_at)") == 3
    assert sql.count("INTERVAL 15 DAY DELETE") == 3


def test_postgres_migration_separates_runtime_and_dev_label_schemas() -> None:
    migrations = load_migrations("postgres")

    assert [migration.version for migration in migrations] == ["0001"]
    sql = render_migration(
        migrations[0],
        schema="sentinel_test",
        dev_schema="sentinel_test_dev",
    )
    assert "sentinel_test.incidents" in sql
    assert "sentinel_test.audit_entries" in sql
    assert "sentinel_test_dev.labels" in sql
    assert "sentinel_test.labels" not in sql


def test_migration_versions_are_sorted_and_unique() -> None:
    for engine in ("clickhouse", "postgres"):
        versions = [migration.version for migration in load_migrations(engine)]
        assert versions == sorted(set(versions))


def test_storage_identifiers_and_pool_bounds_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(POSTGRES_SCHEMA="sentinel; DROP SCHEMA public")

    with pytest.raises(ValidationError):
        Settings(STORAGE_POOL_MIN_SIZE=5, STORAGE_POOL_MAX_SIZE=2)

    with pytest.raises(ValidationError):
        Settings(REDPANDA_BROKERS=",")

    with pytest.raises(ValidationError):
        Settings(FOOTBALL_DATA_BASE_URL="http://api.football-data.org")


def test_dev_label_repository_is_not_a_runtime_storage_export() -> None:
    assert "DevLabelRepository" not in runtime_storage_exports
    assert "DevLabelRecord" not in runtime_storage_exports
