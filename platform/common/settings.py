"""Runtime configuration.

Everything here comes from the environment, with defaults that point at the
service names on the compose network — so a checkout runs against `make up`
without a `.env` file, and any deployment overrides what it needs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

type DatabaseIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]

_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class Settings(BaseSettings):
    """Process-wide settings. Read once via :func:`settings`."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    env: Literal["dev", "scoring", "lab", "prod"] = Field(default="dev", alias="SENTINEL_ENV")
    log_level: str = Field(default="info", alias="SENTINEL_LOG_LEVEL")
    config_dir: Path = Field(default=_DEFAULT_CONFIG_DIR, alias="SENTINEL_CONFIG_DIR")

    postgres_host: str = Field(default="postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_database: DatabaseIdentifier = Field(default="sentinel", alias="POSTGRES_DB")
    postgres_user: str = Field(default="sentinel", alias="POSTGRES_USER", min_length=1)
    postgres_password: SecretStr = Field(default=SecretStr("sentinel"), alias="POSTGRES_PASSWORD")
    postgres_schema: DatabaseIdentifier = Field(default="sentinel", alias="POSTGRES_SCHEMA")
    postgres_dev_schema: DatabaseIdentifier = Field(
        default="sentinel_dev", alias="POSTGRES_DEV_SCHEMA"
    )

    clickhouse_url: str = Field(default="http://clickhouse:8123", alias="CLICKHOUSE_URL")
    clickhouse_database: DatabaseIdentifier = Field(default="sentinel", alias="CLICKHOUSE_DB")
    clickhouse_user: str = Field(default="sentinel", alias="CLICKHOUSE_USER", min_length=1)
    clickhouse_password: SecretStr = Field(
        default=SecretStr("sentinel"), alias="CLICKHOUSE_PASSWORD"
    )
    clickhouse_retention_days: int = Field(
        default=15, alias="CLICKHOUSE_RETENTION_DAYS", ge=1, le=365
    )

    storage_pool_min_size: int = Field(default=1, alias="STORAGE_POOL_MIN_SIZE", ge=0, le=20)
    storage_pool_max_size: int = Field(default=4, alias="STORAGE_POOL_MAX_SIZE", ge=1, le=20)
    storage_timeout_seconds: float = Field(
        default=10.0, alias="STORAGE_TIMEOUT_SECONDS", gt=0.0, le=120.0
    )
    redpanda_brokers: str = Field(default="redpanda:9092", alias="REDPANDA_BROKERS")

    victoriametrics_url: str = Field(
        default="http://victoriametrics:8428", alias="VICTORIAMETRICS_URL"
    )
    loki_url: str = Field(default="http://loki:3100", alias="LOKI_URL")
    tempo_url: str = Field(default="http://tempo:3200", alias="TEMPO_URL")
    collector_url: str = Field(default="http://otel-collector:13133", alias="COLLECTOR_URL")

    probe_timeout_seconds: float = Field(default=2.0, alias="SENTINEL_PROBE_TIMEOUT")

    # Interim access gate. Empty = inert (local/dev). Set before public exposure
    # so mutating and sensitive routes require the secret until OIDC replaces it.
    shared_secret: str = Field(default="", alias="SENTINEL_SHARED_SECRET")

    @property
    def first_broker(self) -> tuple[str, int]:
        """Host and port of the first configured bus broker."""
        host, _, port = self.redpanda_brokers.split(",")[0].partition(":")
        return host, int(port or 9092)

    @model_validator(mode="after")
    def validate_storage_boundaries(self) -> Self:
        """Keep pools bounded and dev labels physically separate from runtime data."""
        if self.storage_pool_max_size < self.storage_pool_min_size:
            raise ValueError("STORAGE_POOL_MAX_SIZE must be greater than or equal to the minimum")
        if self.postgres_dev_schema == self.postgres_schema:
            raise ValueError("POSTGRES_DEV_SCHEMA must differ from POSTGRES_SCHEMA")
        return self


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Cached settings instance."""
    return Settings()
