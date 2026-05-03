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


settings = Settings()
