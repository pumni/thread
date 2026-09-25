from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.config.settings import Settings, get_settings
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.observability.logging import configure_logging
from threads_platform.transport.http.auth import BearerTokenAuthenticator, CommandAuthenticator
from threads_platform.transport.http.commands import create_command_router


class HealthResponse(BaseModel):
    status: str


def create_app(
    settings: Settings | None = None,
    *,
    command_runtime: CommandRuntime | None = None,
    authenticator: CommandAuthenticator | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    engine = None
    if command_runtime is None and resolved_settings.database_url is not None:
        engine = create_database_engine(resolved_settings.database_url)
        session_factory = create_session_factory(engine)
        command_runtime = CommandRuntime(SQLAlchemyUnitOfWorkFactory(session_factory), {})

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        if engine is not None:
            await engine.dispose()

    application = FastAPI(
        title="Threads Operations Platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    application.include_router(
        create_command_router(
            command_runtime,
            authenticator or BearerTokenAuthenticator(resolved_settings.crm_ingress_token),
        )
    )

    @application.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return application


app = create_app()
