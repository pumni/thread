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
from threads_platform.application.commands.composition import compose_command_runtime
from threads_platform.application.scheduler import (
    SchedulerRunner,
    SchedulerRunnerConfig,
    SchedulerTick,
    run_scheduler_tick,
)
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.config.settings import get_settings
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.observability.logging import configure_logging


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


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.database_url is None:
        raise RuntimeError("THREADS_PLATFORM_DATABASE_URL must be configured")

    clock = SystemClock()
    engine = create_database_engine(settings.database_url)
    unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
    worker_job_service = WorkerJobService(unit_of_work_factory, clock=clock)
    composition = compose_command_runtime(
        unit_of_work_factory,
        worker_job_service,
        threads_api_gateway=None,
        threads_access_token_provider=None,
        clock=clock,
    )
    if not composition.command_handler_registry:
        structlog.get_logger(__name__).error(
            "scheduler_local_api_handlers_unavailable",
            error_code="THREADS_ACCESS_TOKEN_PROVIDER_UNAVAILABLE",
        )
    command_runtime = composition.command_runtime
    tick: SchedulerTick = partial(
        run_scheduler_tick,
        unit_of_work_factory,
        command_runtime,
        worker_job_service,
    )
    runner_config = SchedulerRunnerConfig(
        poll_interval=timedelta(seconds=settings.scheduler_poll_interval_seconds),
        generation_limit=settings.scheduler_activity_generation_batch_limit,
        activity_limit=settings.scheduler_activity_batch_limit,
        command_limit=settings.scheduler_command_batch_limit,
        recovery_limit=settings.scheduler_recovery_batch_limit,
    )
    runner = SchedulerRunner(tick, runner_config, clock=clock)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    restore_handlers = install_shutdown_handlers(loop, stop_event)
    try:
        await runner.run(stop_event)
    finally:
        restore_handlers()
        await engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
