from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration resolved from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    app_name: str = Field("Decision Runtime Core")
    app_version: str = Field("1.0.0")
    debug: bool = Field(False)
    log_level: str = Field("INFO")
    flow_dir: str = Field("backend/flows", description="Directory containing YAML flow definitions")

    # Ledger Core v2 integration
    ledger_enabled: bool = Field(False, description="Enable Decision Trace Ledger Core v2 integration")
    ledger_mode: str = Field("parallel", description="parallel: ledger failure does not block DecisionResult; strict: ledger failure raises")
    ledger_schema_version: str = Field("1.0", description="Schema version tag written into every LedgerEvent")
    ledger_backend: str = Field("memory", description="Ledger storage backend: memory / postgres")
    ledger_database_url: Optional[str] = Field(None, description="PostgreSQL connection URL (required when ledger_backend=postgres)")

    # EventBus
    event_bus_backend: str = Field("memory", description="EventBus storage backend: memory / redis")
    redis_url: Optional[str] = Field(None, description="Redis connection URL (required when event_bus_backend=redis)")
    redis_event_stream: str = Field("runtime:events", description="Redis Streams key for published RuntimeEvents")

    # ExecutionPublisher
    execution_publisher_backend: str = Field("noop", description="Execution publisher backend: noop / kafka")
    kafka_bootstrap_servers: Optional[str] = Field(None, description="Kafka bootstrap servers (required when execution_publisher_backend=kafka)")
    kafka_execution_topic: str = Field("runtime.execution.requested", description="Kafka topic for EXECUTION_REQUESTED events")

    # Auth / RBAC
    auth_enabled: bool = Field(False, description="Enable X-Api-Key authentication for human gate actions")
    api_key_role_map: dict = Field(default_factory=dict, description="Maps API key strings to {actor_id, roles} dicts")

    # Observability
    observability_enabled: bool = Field(True, description="Master switch for all observability features")
    metrics_enabled: bool = Field(True, description="Enable Prometheus-compatible /metrics endpoint")
    structured_logging_enabled: bool = Field(True, description="Enable structured JSON access logging middleware")


settings = Settings()
