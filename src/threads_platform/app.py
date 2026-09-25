from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.commands.threads_handlers import create_threads_command_handlers
from threads_platform.application.ports.threads import ThreadsAccessTokenProvider, ThreadsAPI
from threads_platform.config.settings import Settings, get_settings
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI
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
    threads_access_token_provider: ThreadsAccessTokenProvider | None = None,
    threads_api_gateway: ThreadsAPI | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    engine = None
    http_client = None
    if command_runtime is None and resolved_settings.database_url is not None:
        engine = create_database_engine(resolved_settings.database_url)
        session_factory = create_session_factory(engine)
        unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(session_factory)
        handlers = {}
        if threads_access_token_provider is not None:
            api = threads_api_gateway
            if api is None:
                http_client = httpx.AsyncClient(
                    base_url=str(resolved_settings.threads_api_base_url),
                    timeout=httpx.Timeout(15.0),
                    follow_redirects=False,
                )
                api = HttpThreadsAPI(http_client)
            handlers = create_threads_command_handlers(
                api, threads_access_token_provider, unit_of_work_factory
            )
        command_runtime = CommandRuntime(unit_of_work_factory, handlers)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        if http_client is not None:
            await http_client.aclose()
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
