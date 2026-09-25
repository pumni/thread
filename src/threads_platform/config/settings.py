from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, SecretStr
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
    crm_ingress_token: SecretStr | None = None
    threads_api_base_url: AnyHttpUrl = AnyHttpUrl("https://graph.threads.net/v1.0/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
