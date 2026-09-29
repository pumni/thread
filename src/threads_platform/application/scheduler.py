from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import structlog

from threads_platform.application.account_activity_materialization import (
    materialize_due_account_activities,
)
from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.domain.time import normalize_utc

MAX_SCHEDULER_BATCH_SIZE = 100
MAX_SCHEDULER_POLL_INTERVAL_SECONDS = 3_600
MAX_SCHEDULER_ERROR_BACKOFF_SECONDS = 3_600


@dataclass(frozen=True, slots=True)
class SchedulerTickResult:
    activities_materialized: int
    commands_processed: int
    worker_jobs_recovered: int


@dataclass(frozen=True, slots=True)
class SchedulerRunnerConfig:
    poll_interval: timedelta = timedelta(seconds=15)
    activity_limit: int = 50
    command_limit: int = 50
    recovery_limit: int = 50

    def __post_init__(self) -> None:
        seconds = self.poll_interval.total_seconds()
        if (
            not math.isfinite(seconds)
            or seconds <= 0
            or seconds > MAX_SCHEDULER_POLL_INTERVAL_SECONDS
        ):
            raise ValueError(
                "scheduler poll interval must be positive and at most "
                f"{MAX_SCHEDULER_POLL_INTERVAL_SECONDS} seconds"
            )
        validate_scheduler_limit("activity_limit", self.activity_limit)
        validate_scheduler_limit("command_limit", self.command_limit)
        validate_scheduler_limit("recovery_limit", self.recovery_limit)


class SchedulerTick(Protocol):
    async def __call__(
        self,
        *,
        now: datetime,
        activity_limit: int,
        command_limit: int,
        recovery_limit: int,
    ) -> SchedulerTickResult: ...


SchedulerWait = Callable[[asyncio.Event, float], Awaitable[None]]


def validate_scheduler_limit(name: str, value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_SCHEDULER_BATCH_SIZE:
        raise ValueError(f"{name} must be between 1 and {MAX_SCHEDULER_BATCH_SIZE}")


async def run_scheduler_tick(
    unit_of_work_factory: UnitOfWorkFactory,
    command_runtime: CommandRuntime,
    worker_job_service: WorkerJobService,
    *,
    now: datetime,
    activity_limit: int,
    command_limit: int,
    recovery_limit: int,
) -> SchedulerTickResult:
    """Process a bounded snapshot of durable work without retaining tick state."""
    validate_scheduler_limit("activity_limit", activity_limit)
    validate_scheduler_limit("command_limit", command_limit)
    validate_scheduler_limit("recovery_limit", recovery_limit)
    occurred_at = normalize_utc(now)
    logger = structlog.get_logger(__name__)
    started_at = time.perf_counter()
    activities_materialized = 0
    commands_processed = 0
    worker_jobs_recovered = 0
    error: Exception | None = None

    logger.info(
        "scheduler_tick_started",
        now=occurred_at.isoformat(),
        activity_limit=activity_limit,
        command_limit=command_limit,
        recovery_limit=recovery_limit,
    )
    try:
        try:
            activities = await materialize_due_account_activities(
                unit_of_work_factory,
                now=occurred_at,
                limit=activity_limit,
            )
            activities_materialized = len(activities)
        except Exception as caught:
            error = caught
            logger.error(
                "scheduler_tick_stage_failed",
                stage="activity_materialization",
                error_type=type(caught).__name__,
            )

        for _ in range(command_limit):
            try:
                result = await command_runtime.process_next(now=occurred_at)
            except Exception as caught:
                if error is None:
                    error = caught
                logger.error(
                    "scheduler_tick_stage_failed",
                    stage="command_processing",
                    error_type=type(caught).__name__,
                )
                break
            if result is None:
                break
            commands_processed += 1

        try:
            worker_jobs_recovered = await worker_job_service.recover_expired(
                limit=recovery_limit,
                now=occurred_at,
            )
        except Exception as caught:
            if error is None:
                error = caught
            logger.error(
                "scheduler_tick_stage_failed",
                stage="worker_job_recovery",
                error_type=type(caught).__name__,
            )

        if error is not None:
            raise error
        return SchedulerTickResult(
            activities_materialized=activities_materialized,
            commands_processed=commands_processed,
            worker_jobs_recovered=worker_jobs_recovered,
        )
    finally:
        logger.info(
            "scheduler_tick_finished",
            activities_materialized=activities_materialized,
            commands_processed=commands_processed,
            worker_jobs_recovered=worker_jobs_recovered,
            elapsed_seconds=round(time.perf_counter() - started_at, 6),
            error_type=type(error).__name__ if error is not None else None,
        )


async def _wait_for_stop(stop_event: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        pass


class SchedulerRunner:
    """Sequential poll loop; PostgreSQL remains the only durable work cursor."""

    def __init__(
        self,
        tick: SchedulerTick,
        config: SchedulerRunnerConfig | None = None,
        *,
        clock: Clock | None = None,
        wait_for_stop: SchedulerWait = _wait_for_stop,
    ) -> None:
        self._tick = tick
        self._config = config or SchedulerRunnerConfig()
        self._clock = clock or SystemClock()
        self._wait_for_stop = wait_for_stop
        self._logger = structlog.get_logger(__name__)

    async def run(self, stop_event: asyncio.Event) -> None:
        failures = 0
        while not stop_event.is_set():
            try:
                await self._tick(
                    now=normalize_utc(self._clock.now()),
                    activity_limit=self._config.activity_limit,
                    command_limit=self._config.command_limit,
                    recovery_limit=self._config.recovery_limit,
                )
            except Exception as error:
                failures += 1
                delay = min(
                    self._config.poll_interval.total_seconds() * 2 ** min(failures, 12),
                    MAX_SCHEDULER_ERROR_BACKOFF_SECONDS,
                )
                self._logger.error(
                    "scheduler_tick_failed",
                    error_type=type(error).__name__,
                    consecutive_failures=failures,
                    retry_delay_seconds=delay,
                )
            else:
                failures = 0
                delay = self._config.poll_interval.total_seconds()

            if not stop_event.is_set():
                await self._wait_for_stop(stop_event, delay)
