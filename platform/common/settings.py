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
_DEFAULT_SCORE_PROOF_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "reports" / "latest-score-proof.json"
)


class Settings(BaseSettings):
    """Process-wide settings. Read once via :func:`settings`."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    env: Literal["dev", "scoring", "lab", "prod"] = Field(default="dev", alias="SENTINEL_ENV")
    log_level: str = Field(default="info", alias="SENTINEL_LOG_LEVEL")
    config_dir: Path = Field(default=_DEFAULT_CONFIG_DIR, alias="SENTINEL_CONFIG_DIR")
    score_proof_path: Path = Field(
        default=_DEFAULT_SCORE_PROOF_PATH,
        alias="SENTINEL_SCORE_PROOF_PATH",
    )

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
    redpanda_brokers: str = Field(default="redpanda:9092", alias="REDPANDA_BROKERS", min_length=1)
    ingest_group_id: str = Field(
        default="sentinel-ingest-v1", alias="INGEST_GROUP_ID", min_length=1, max_length=255
    )
    ingest_dedup_capacity: int = Field(
        default=100_000, alias="INGEST_DEDUP_CAPACITY", ge=1_000, le=10_000_000
    )
    football_data_api_key: SecretStr = Field(default=SecretStr(""), alias="FOOTBALL_DATA_API_KEY")
    football_data_base_url: str = Field(
        default="https://api.football-data.org",
        alias="FOOTBALL_DATA_BASE_URL",
        pattern=r"^https://[^\s/]+(?:/[^\s]*)?$",
    )
    context_http_timeout_seconds: float = Field(
        default=5.0, alias="CONTEXT_HTTP_TIMEOUT_SECONDS", gt=0.0, le=30.0
    )

    @property
    def brokers(self) -> tuple[str, ...]:
        """Normalized Kafka bootstrap endpoints."""
        return tuple(item.strip() for item in self.redpanda_brokers.split(",") if item.strip())

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
    interim_operator_id: str = Field(
        default="interim-operator",
        alias="SENTINEL_INTERIM_OPERATOR_ID",
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$",
    )
    action_worker_enabled: bool = Field(
        default=False,
        alias="SENTINEL_ACTION_WORKER_ENABLED",
    )
    action_worker_id: str = Field(
        default="gateway-action-worker",
        alias="SENTINEL_ACTION_WORKER_ID",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    action_poll_interval_seconds: float = Field(
        default=1.0,
        alias="SENTINEL_ACTION_POLL_INTERVAL_SECONDS",
        ge=0.1,
        le=60.0,
    )
    action_poll_batch_limit: int = Field(
        default=8,
        alias="SENTINEL_ACTION_POLL_BATCH_LIMIT",
        ge=1,
        le=100,
    )
    action_settlement_delay_seconds: int = Field(
        default=15,
        alias="SENTINEL_ACTION_SETTLEMENT_DELAY_SECONDS",
        ge=1,
        le=3600,
    )
    action_slo_window_seconds: int = Field(
        default=300,
        alias="SENTINEL_ACTION_SLO_WINDOW_SECONDS",
        ge=30,
        le=86_400,
    )
    live_producer_id: str = Field(
        default="live-producer",
        alias="SENTINEL_LIVE_PRODUCER_ID",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    live_producer_group_id: str = Field(
        default="sentinel-live-producer-v1",
        alias="SENTINEL_LIVE_PRODUCER_GROUP_ID",
        min_length=1,
        max_length=128,
    )
    # An honesty label is an operator statement about the world. A contained
    # testbed under injected chaos is a SIMULATED stimulus over REAL telemetry;
    # only whoever started the deployment knows which it is, so it is declared
    # rather than inferred, and the honest default is the cautious one.
    live_producer_stimulus_honesty: Literal["REAL", "SIMULATED"] = Field(
        default="SIMULATED",
        alias="SENTINEL_LIVE_PRODUCER_STIMULUS_HONESTY",
    )
    live_producer_buffer_capacity: int = Field(
        default=200_000,
        alias="SENTINEL_LIVE_PRODUCER_BUFFER_CAPACITY",
        ge=1_000,
        le=10_000_000,
    )
    # How long the feed may go without a judged tick before it says so. This is
    # a display honesty bound, not a detector parameter: it decides when the UI
    # stops calling itself live, never what counts as a symptom.
    observation_expected_within_seconds: float = Field(
        default=120.0,
        alias="SENTINEL_OBSERVATION_EXPECTED_WITHIN_SECONDS",
        gt=0.0,
        le=86_400.0,
    )
    live_producer_context_refresh_seconds: int = Field(
        default=60,
        alias="SENTINEL_LIVE_PRODUCER_CONTEXT_REFRESH_SECONDS",
        ge=5,
        le=3_600,
    )
    # Whether a live judgement freezes a plan an operator can act on. Off by
    # default and deliberately separate from `action.yml`'s `dry_run`: that
    # decides whether an effect reaches the world, this decides whether a plan
    # exists to be claimed at all. A deployment that has not turned this on
    # publishes incidents and no plans, so the durable worker has nothing it
    # could act on even by mistake.
    live_producer_plans_actions: bool = Field(
        default=False,
        alias="SENTINEL_LIVE_PRODUCER_PLANS_ACTIONS",
    )

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
        if not self.brokers:
            raise ValueError("REDPANDA_BROKERS must contain at least one endpoint")
        return self


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Cached settings instance."""
    return Settings()
