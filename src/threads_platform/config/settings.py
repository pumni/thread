from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="THREADS_PLATFORM_",
        env_ignore_empty=True,
        extra="ignore",
    )

    log_level: LogLevel = "INFO"
    database_url: SecretStr | None = None
    scheduler_poll_interval_seconds: float = Field(default=15, gt=0, le=3_600)
    scheduler_activity_generation_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_conversation_sync_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_activity_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_command_batch_limit: int = Field(default=50, ge=1, le=100)
    scheduler_recovery_batch_limit: int = Field(default=50, ge=1, le=100)
    crm_ingress_token: SecretStr | None = None
    worker_admin_token: SecretStr | None = None
    worker_tls_required: bool = True
    threads_api_base_url: AnyHttpUrl = AnyHttpUrl("https://graph.threads.net/v1.0/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
