from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry
from pydantic import BaseModel

from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.operational_metrics import (
    OperationalMetricsProbe,
    OperationalMetricsSnapshot,
)
from threads_platform.application.ports.threads import ThreadsAccessTokenProvider, ThreadsAPI
from threads_platform.application.readiness import (
    DatabaseReadiness,
    FleetReadiness,
    OperationalReadinessProbe,
    OverallReadiness,
    ReadinessSnapshot,
    database_unavailable_snapshot,
)
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.application.worker_sessions import WorkerSessionService
from threads_platform.config.settings import Settings, get_settings
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.operational_metrics import (
    PostgresOperationalMetricsProbe,
)
from threads_platform.infrastructure.persistence.readiness import (
    PostgresOperationalReadinessProbe,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.infrastructure.threads_api.composition import compose_process_command_runtime
from threads_platform.observability.logging import configure_logging
from threads_platform.observability.metrics import ControlPlaneMetrics
from threads_platform.transport.http.auth import BearerTokenAuthenticator, CommandAuthenticator
from threads_platform.transport.http.commands import create_command_router
from threads_platform.transport.http.worker_tls import WorkerTransportTLSMiddleware
from threads_platform.transport.http.workers import create_worker_router


class HealthResponse(BaseModel):
    status: str


class WorkerReadinessCountsResponse(BaseModel):
    total: int
    online: int
    degraded: int
    draining: int
    offline: int
    registering: int
    upgrade_required: int


class ReadinessResponse(BaseModel):
    overall: OverallReadiness
    database: DatabaseReadiness
    fleet: FleetReadiness
    workers_available: bool
    workers: WorkerReadinessCountsResponse


class _DatabaseUnavailableReadinessProbe:
    async def snapshot(self) -> ReadinessSnapshot:
        return database_unavailable_snapshot()


class _DatabaseUnavailableOperationalMetricsProbe:
    async def snapshot(self) -> OperationalMetricsSnapshot:
        raise RuntimeError("operational metrics database is unavailable")


def create_app(
    settings: Settings | None = None,
    *,
    command_runtime: CommandRuntime | None = None,
    authenticator: CommandAuthenticator | None = None,
    threads_access_token_provider: ThreadsAccessTokenProvider | None = None,
    threads_api_gateway: ThreadsAPI | None = None,
    worker_control_service: WorkerControlService | None = None,
    worker_job_service: WorkerJobService | None = None,
    worker_session_service: WorkerSessionService | None = None,
    worker_notifications: WorkerNotificationHub | None = None,
    readiness_probe: OperationalReadinessProbe | None = None,
    operational_metrics_probe: OperationalMetricsProbe | None = None,
    metrics_registry: CollectorRegistry | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    if (
        resolved_settings.threads_token_provider_mode == "environment"
        and resolved_settings.database_url is None
    ):
        raise RuntimeError(
            "THREADS_PLATFORM_DATABASE_URL must be configured for environment token mode"
        )
    engine = None
    http_client = None
    resolved_worker_service = worker_control_service
    resolved_job_service = worker_job_service
    resolved_session_service = worker_session_service
    resolved_readiness_probe = readiness_probe
    resolved_metrics_probe = operational_metrics_probe
    metrics = ControlPlaneMetrics(registry=metrics_registry)
    notifications = worker_notifications or WorkerNotificationHub()
    if resolved_settings.database_url is not None and (
        resolved_readiness_probe is None
        or command_runtime is None
        or resolved_worker_service is None
        or resolved_job_service is None
        or resolved_session_service is None
        or resolved_metrics_probe is None
    ):
        engine = create_database_engine(resolved_settings.database_url)
        session_factory = create_session_factory(engine)
        unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(session_factory)
        if resolved_readiness_probe is None:
            resolved_readiness_probe = PostgresOperationalReadinessProbe(
                session_factory,
                timeout_seconds=resolved_settings.readiness_timeout_seconds,
            )
        if resolved_metrics_probe is None:
            resolved_metrics_probe = PostgresOperationalMetricsProbe(
                session_factory,
                timeout_seconds=resolved_settings.readiness_timeout_seconds,
            )
        if resolved_job_service is None:
            resolved_job_service = WorkerJobService(
                unit_of_work_factory,
                notifications=notifications,
            )
        if command_runtime is None:
            process_composition = compose_process_command_runtime(
                resolved_settings,
                unit_of_work_factory,
                resolved_job_service,
                threads_api_gateway=threads_api_gateway,
                threads_access_token_provider=threads_access_token_provider,
            )
            http_client = process_composition.http_client
            command_runtime = process_composition.command_runtime
        if resolved_worker_service is None:
            resolved_worker_service = WorkerControlService(unit_of_work_factory)
        if resolved_session_service is None:
            resolved_session_service = WorkerSessionService(unit_of_work_factory)
    if resolved_readiness_probe is None:
        resolved_readiness_probe = _DatabaseUnavailableReadinessProbe()
    if resolved_metrics_probe is None:
        resolved_metrics_probe = _DatabaseUnavailableOperationalMetricsProbe()

    if command_runtime is not None:
        command_runtime.set_execution_duration_observer(metrics.observe_command_execution_duration)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            try:
                if http_client is not None:
                    await http_client.aclose()
            finally:
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
            resolved_session_service,
        )
    )

    @application.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @application.get("/ready", response_model=ReadinessResponse, tags=["health"])
    async def ready(response: Response) -> ReadinessResponse:
        try:
            snapshot = await resolved_readiness_probe.snapshot()
        except Exception as error:
            structlog.get_logger(__name__).warning(
                "readiness_probe_failed",
                error_type=type(error).__name__,
            )
            snapshot = database_unavailable_snapshot()
        response.status_code = 503 if snapshot.overall is OverallReadiness.NOT_READY else 200
        return ReadinessResponse(
            overall=snapshot.overall,
            database=snapshot.database,
            fleet=snapshot.fleet,
            workers_available=snapshot.workers_available,
            workers=WorkerReadinessCountsResponse(
                total=snapshot.workers.total,
                online=snapshot.workers.online,
                degraded=snapshot.workers.degraded,
                draining=snapshot.workers.draining,
                offline=snapshot.workers.offline,
                registering=snapshot.workers.registering,
                upgrade_required=snapshot.workers.upgrade_required,
            ),
        )

    @application.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        snapshot: OperationalMetricsSnapshot | None
        try:
            snapshot = await resolved_metrics_probe.snapshot()
        except Exception:
            snapshot = None
        return Response(
            content=metrics.render(snapshot),
            media_type=CONTENT_TYPE_LATEST,
        )

    return application


app = create_app()
