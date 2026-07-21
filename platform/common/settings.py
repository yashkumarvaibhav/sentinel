"""Runtime configuration.

Everything here comes from the environment, with defaults that point at the
service names on the compose network — so a checkout runs against `make up`
without a `.env` file, and any deployment overrides what it needs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings. Read once via :func:`settings`."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    env: Literal["dev", "scoring", "lab", "prod"] = Field(default="dev", alias="SENTINEL_ENV")
    log_level: str = Field(default="info", alias="SENTINEL_LOG_LEVEL")

    postgres_host: str = Field(default="postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")

    clickhouse_url: str = Field(default="http://clickhouse:8123", alias="CLICKHOUSE_URL")
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


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Cached settings instance."""
    return Settings()
