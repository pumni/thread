from fastapi import FastAPI
from pydantic import BaseModel

from threads_platform.config.settings import Settings, get_settings
from threads_platform.observability.logging import configure_logging


class HealthResponse(BaseModel):
    status: str


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    application = FastAPI(title="Threads Operations Platform", version="0.1.0")

    @application.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return application


app = create_app()
