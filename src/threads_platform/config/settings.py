from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ThreadsTokenProviderMode = Literal["disabled", "environment"]
SchedulerMetricsHost = Literal["127.0.0.1", "0.0.0.0"]
WorkerAdminAuthProfile = Literal["operator_only", "legacy_linux_it"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="THREADS_PLATFORM_",
        env_ignore_empty=True,
        extra="ignore",
    )

    log_level: LogLevel = "INFO"
    database_url: SecretStr | None = None
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    scheduler_poll_interval_seconds: float = Field(default=15, gt=0, le=3_600)
    scheduler_presence_expiry_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_activity_generation_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_conversation_sync_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_activity_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_command_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_recovery_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_outbox_delivery_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_metrics_enabled: bool = False
    scheduler_metrics_host: SchedulerMetricsHost = "127.0.0.1"
    scheduler_metrics_port: int = Field(default=9101, ge=1, le=65_535)
    tracing_enabled: bool = False
    crm_ingress_token: SecretStr | None = None
    worker_admin_token: SecretStr | None = None
    worker_admin_auth_profile: WorkerAdminAuthProfile = "operator_only"
    operator_session_ttl_seconds: int = Field(default=28_800, ge=60, le=86_400)
    threads_api_base_url: AnyHttpUrl = AnyHttpUrl("https://graph.threads.net/v1.0/")
    threads_token_provider_mode: ThreadsTokenProviderMode = "disabled"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
