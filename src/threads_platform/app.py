import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.commands.threads_handlers import create_threads_command_handlers
from threads_platform.application.ports.threads import ThreadsAccessTokenProvider, ThreadsAPI
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.application.worker_notifications import WorkerNotificationHub
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
from threads_platform.transport.http.worker_tls import WorkerTransportTLSMiddleware
from threads_platform.transport.http.workers import create_worker_router


class HealthResponse(BaseModel):
    status: str


def create_app(
    settings: Settings | None = None,
    *,
    command_runtime: CommandRuntime | None = None,
    authenticator: CommandAuthenticator | None = None,
    threads_access_token_provider: ThreadsAccessTokenProvider | None = None,
    threads_api_gateway: ThreadsAPI | None = None,
    worker_control_service: WorkerControlService | None = None,
    worker_job_service: WorkerJobService | None = None,
    worker_notifications: WorkerNotificationHub | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    engine = None
    http_client = None
    resolved_worker_service = worker_control_service
    resolved_job_service = worker_job_service
    notifications = worker_notifications or WorkerNotificationHub()
    if resolved_settings.database_url is not None and (
        command_runtime is None or resolved_worker_service is None or resolved_job_service is None
    ):
        engine = create_database_engine(resolved_settings.database_url)
        session_factory = create_session_factory(engine)
        unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(session_factory)
        if resolved_job_service is None:
            resolved_job_service = WorkerJobService(
                unit_of_work_factory,
                notifications=notifications,
            )
        if command_runtime is None:
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
            command_runtime = CommandRuntime(
                unit_of_work_factory,
                handlers,
                worker_job_service=resolved_job_service,
            )
        if resolved_worker_service is None:
            resolved_worker_service = WorkerControlService(unit_of_work_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        presence_task = (
            asyncio.create_task(_expire_worker_presence(resolved_worker_service))
            if resolved_worker_service is not None
            else None
        )
        recovery_task = (
            asyncio.create_task(_recover_worker_jobs(resolved_job_service))
            if resolved_job_service is not None
            else None
        )
        try:
            yield
        finally:
            tasks = [task for task in (presence_task, recovery_task) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
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
    application.add_middleware(
        WorkerTransportTLSMiddleware,
        required=resolved_settings.worker_tls_required,
    )
    application.include_router(
        create_worker_router(
            resolved_worker_service,
            BearerTokenAuthenticator(resolved_settings.worker_admin_token),
            notifications,
            resolved_job_service,
        )
    )

    @application.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return application


async def _expire_worker_presence(service: WorkerControlService) -> None:
    while True:
        try:
            await service.expire_presence()
        except Exception as error:
            # Keep token and request data out of the diagnostic event.
            import structlog

            structlog.get_logger(__name__).error(
                "worker_presence_expiry_failed", error_type=type(error).__name__
            )
        await asyncio.sleep(15)


async def _recover_worker_jobs(service: WorkerJobService) -> None:
    while True:
        try:
            await service.recover_expired()
        except Exception as error:
            import structlog

            structlog.get_logger(__name__).error(
                "worker_job_recovery_failed", error_type=type(error).__name__
            )
        await asyncio.sleep(15)


app = create_app()
