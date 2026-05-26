"""Application configuration via environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    database_url: str = "sqlite:///./data/fraud_platform.db"
    log_level: str = "INFO"

    high_value_threshold_usd: float = 10_000.0
    rapid_transfer_window_minutes: int = 30
    rapid_transfer_min_count: int = 5
    mule_inbound_min_sources: int = 4
    mule_outbound_hours: int = 24
    velocity_spike_multiplier: float = 3.0

    gcp_project_id: str = ""
    bigquery_dataset_id: str = "fraud_investigation"
    bigquery_location: str = "US"
    gcs_bucket_name: str = ""
    use_bigquery: bool = False

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    use_adk: bool = True
    auto_sync_bigquery: bool = False

    port: int = 8080

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key.strip())

    @property
    def gcp_enabled(self) -> bool:
        return bool(self.gcp_project_id.strip()) and self.use_bigquery


@lru_cache
def get_settings() -> Settings:
    return Settings()
