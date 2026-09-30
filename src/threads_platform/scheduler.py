from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from types import FrameType
from typing import Any

import structlog

from threads_platform.application.clock import SystemClock
from threads_platform.application.scheduler import (
    SchedulerRunner,
    SchedulerRunnerConfig,
    SchedulerTick,
    run_scheduler_tick,
)
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.config.settings import get_settings
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.infrastructure.threads_api.composition import compose_process_command_runtime
from threads_platform.observability.logging import configure_logging
from threads_platform.observability.metrics import SchedulerMetrics
from threads_platform.observability.metrics_server import start_scheduler_metrics_listener


def install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event
) -> Callable[[], None]:
    previous_handlers: dict[signal.Signals, Any] = {}
    loop_handlers: set[signal.Signals] = set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
            loop_handlers.add(signum)
        except NotImplementedError, RuntimeError:
            previous_handlers[signum] = signal.getsignal(signum)

            def request_stop(_signum: int, _frame: FrameType | None) -> None:
                loop.call_soon_threadsafe(stop_event.set)

            signal.signal(signum, request_stop)

    def restore() -> None:
        for signum in loop_handlers:
            loop.remove_signal_handler(signum)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    return restore


async def run_scheduler() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.database_url is None:
        raise RuntimeError("THREADS_PLATFORM_DATABASE_URL must be configured")

    clock = SystemClock()
    engine = create_database_engine(settings.database_url)
    http_client = None
    restore_handlers: Callable[[], None] | None = None
    metrics_server: asyncio.AbstractServer | None = None
    try:
        unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
        worker_job_service = WorkerJobService(unit_of_work_factory, clock=clock)
        worker_control_service = WorkerControlService(unit_of_work_factory, clock=clock)
        composition = compose_process_command_runtime(
            settings,
            unit_of_work_factory,
            worker_job_service,
            clock=clock,
        )
        metrics = SchedulerMetrics()
        composition.command_runtime.set_execution_duration_observer(
            metrics.observe_command_execution_duration
        )
        http_client = composition.http_client
        if not composition.command_handler_registry:
            structlog.get_logger(__name__).error(
                "scheduler_local_api_handlers_unavailable",
                error_code="THREADS_ACCESS_TOKEN_PROVIDER_UNAVAILABLE",
            )
        structlog.get_logger(__name__).error(
            "scheduler_outbox_delivery_unavailable",
            error_code="CRM_RESULT_SINK_UNAVAILABLE",
        )
        tick: SchedulerTick = partial(
            run_scheduler_tick,
            unit_of_work_factory,
            composition.command_runtime,
            worker_job_service,
            worker_control_service=worker_control_service,
            metrics_observer=metrics,
        )
        runner_config = SchedulerRunnerConfig(
            poll_interval=timedelta(seconds=settings.scheduler_poll_interval_seconds),
            presence_expiry_limit=settings.scheduler_presence_expiry_batch_limit,
            generation_limit=settings.scheduler_activity_generation_batch_limit,
            conversation_sync_limit=settings.scheduler_conversation_sync_batch_limit,
            activity_limit=settings.scheduler_activity_batch_limit,
            command_limit=settings.scheduler_command_batch_limit,
            recovery_limit=settings.scheduler_recovery_batch_limit,
            outbox_delivery_limit=settings.scheduler_outbox_delivery_batch_limit,
        )
        runner = SchedulerRunner(
            tick,
            runner_config,
            clock=clock,
            metrics_observer=metrics,
        )
        if settings.scheduler_metrics_enabled:
            metrics_server = await start_scheduler_metrics_listener(
                settings.scheduler_metrics_host,
                settings.scheduler_metrics_port,
                metrics.registry,
            )
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        restore_handlers = install_shutdown_handlers(loop, stop_event)
        await runner.run(stop_event)
    finally:
        try:
            if restore_handlers is not None:
                restore_handlers()
        finally:
            try:
                if metrics_server is not None:
                    metrics_server.close()
                    await metrics_server.wait_closed()
            finally:
                try:
                    if http_client is not None:
                        await http_client.aclose()
                finally:
                    await engine.dispose()


def main() -> None:
    asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()
