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
from threads_platform.application.account_activity_recurrence import (
    generate_due_account_activity_occurrences,
)
from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.conversation_sync_scheduling import (
    dispatch_due_conversation_syncs,
)
from threads_platform.application.outbox_delivery import (
    OutboxDeliveryWorker,
    deliver_due_outbox,
)
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.worker_control import WorkerControlService
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
    activity_occurrences_generated: int = 0
    conversation_syncs_dispatched: int = 0
    worker_presences_expired: int = 0
    outbox_deliveries_attempted: int = 0
    outbox_deliveries_succeeded: int = 0


@dataclass(frozen=True, slots=True)
class SchedulerRunnerConfig:
    poll_interval: timedelta = timedelta(seconds=15)
    presence_expiry_limit: int = 50
    generation_limit: int = 50
    conversation_sync_limit: int = 50
    activity_limit: int = 50
    command_limit: int = 50
    recovery_limit: int = 50
    outbox_delivery_limit: int = 50

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
        validate_scheduler_limit("generation_limit", self.generation_limit)
        validate_scheduler_limit("presence_expiry_limit", self.presence_expiry_limit)
        validate_scheduler_limit("conversation_sync_limit", self.conversation_sync_limit)
        validate_scheduler_limit("activity_limit", self.activity_limit)
        validate_scheduler_limit("command_limit", self.command_limit)
        validate_scheduler_limit("recovery_limit", self.recovery_limit)
        validate_scheduler_limit("outbox_delivery_limit", self.outbox_delivery_limit)


class SchedulerTick(Protocol):
    async def __call__(
        self,
        *,
        now: datetime,
        presence_expiry_limit: int,
        generation_limit: int,
        conversation_sync_limit: int,
        activity_limit: int,
        command_limit: int,
        recovery_limit: int,
        outbox_delivery_limit: int,
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
    worker_control_service: WorkerControlService | None = None,
    outbox_delivery_worker: OutboxDeliveryWorker | None = None,
    presence_expiry_limit: int = 50,
    generation_limit: int = 50,
    conversation_sync_limit: int = 50,
    activity_limit: int,
    command_limit: int,
    recovery_limit: int,
    outbox_delivery_limit: int = 50,
) -> SchedulerTickResult:
    """Process a bounded snapshot of durable work without retaining tick state."""
    validate_scheduler_limit("activity_limit", activity_limit)
    validate_scheduler_limit("presence_expiry_limit", presence_expiry_limit)
    validate_scheduler_limit("generation_limit", generation_limit)
    validate_scheduler_limit("conversation_sync_limit", conversation_sync_limit)
    validate_scheduler_limit("command_limit", command_limit)
    validate_scheduler_limit("recovery_limit", recovery_limit)
    validate_scheduler_limit("outbox_delivery_limit", outbox_delivery_limit)
    occurred_at = normalize_utc(now)
    resolved_worker_control_service = worker_control_service or WorkerControlService(
        unit_of_work_factory
    )
    logger = structlog.get_logger(__name__)
    started_at = time.perf_counter()
    activities_materialized = 0
    worker_presences_expired = 0
    activity_occurrences_generated = 0
    conversation_syncs_dispatched = 0
    commands_processed = 0
    worker_jobs_recovered = 0
    outbox_deliveries_attempted = 0
    outbox_deliveries_succeeded = 0
    error: Exception | None = None

    try:
        try:
            worker_presences_expired = await resolved_worker_control_service.expire_presence(
                now=occurred_at,
                limit=presence_expiry_limit,
            )
        except Exception as caught:
            if error is None:
                error = caught
            logger.error(
                "scheduler_tick_stage_failed",
                stage="worker_presence_expiry",
                error_type=type(caught).__name__,
            )

        try:
            activities = await generate_due_account_activity_occurrences(
                unit_of_work_factory,
                now=occurred_at,
                limit=generation_limit,
            )
            activity_occurrences_generated = len(activities)
        except Exception as caught:
            if error is None:
                error = caught
            logger.error(
                "scheduler_tick_stage_failed",
                stage="activity_recurrence_generation",
                error_type=type(caught).__name__,
            )

        try:
            conversation_sync_commands = await dispatch_due_conversation_syncs(
                unit_of_work_factory,
                now=occurred_at,
                limit=conversation_sync_limit,
            )
            conversation_syncs_dispatched = len(conversation_sync_commands)
        except Exception as caught:
            if error is None:
                error = caught
            logger.error(
                "scheduler_tick_stage_failed",
                stage="conversation_sync_dispatch",
                error_type=type(caught).__name__,
            )

        try:
            activities = await materialize_due_account_activities(
                unit_of_work_factory,
                now=occurred_at,
                limit=activity_limit,
            )
            activities_materialized = len(activities)
        except Exception as caught:
            if error is None:
                error = caught
            logger.error(
                "scheduler_tick_stage_failed",
                stage="activity_materialization",
                error_type=type(caught).__name__,
            )

        attempted_command_ids: set[str] = set()
        for _ in range(command_limit):
            try:
                result = await command_runtime.process_next(
                    now=occurred_at,
                    exclude_command_ids=frozenset(attempted_command_ids),
                )
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
            if result.command_id in attempted_command_ids:
                logger.error(
                    "scheduler_reselected_command_within_tick",
                    command_id=result.command_id,
                )
                break
            attempted_command_ids.add(result.command_id)
            commands_processed = len(attempted_command_ids)

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

        if outbox_delivery_worker is not None:
            try:
                delivery_result = await deliver_due_outbox(
                    outbox_delivery_worker,
                    limit=outbox_delivery_limit,
                )
                outbox_deliveries_attempted = delivery_result.attempted
                outbox_deliveries_succeeded = delivery_result.succeeded
            except Exception as caught:
                if error is None:
                    error = caught
                logger.error(
                    "scheduler_tick_stage_failed",
                    stage="outbox_delivery",
                    error_type=type(caught).__name__,
                    outbox_deliveries_attempted=outbox_deliveries_attempted,
                )

        if error is not None:
            raise error
        return SchedulerTickResult(
            worker_presences_expired=worker_presences_expired,
            activity_occurrences_generated=activity_occurrences_generated,
            conversation_syncs_dispatched=conversation_syncs_dispatched,
            activities_materialized=activities_materialized,
            commands_processed=commands_processed,
            worker_jobs_recovered=worker_jobs_recovered,
            outbox_deliveries_attempted=outbox_deliveries_attempted,
            outbox_deliveries_succeeded=outbox_deliveries_succeeded,
        )
    finally:
        logger.info(
            "scheduler_tick_finished",
            worker_presences_expired=worker_presences_expired,
            activity_occurrences_generated=activity_occurrences_generated,
            conversation_syncs_dispatched=conversation_syncs_dispatched,
            activities_materialized=activities_materialized,
            commands_processed=commands_processed,
            worker_jobs_recovered=worker_jobs_recovered,
            outbox_deliveries_attempted=outbox_deliveries_attempted,
            outbox_deliveries_succeeded=outbox_deliveries_succeeded,
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
                    presence_expiry_limit=self._config.presence_expiry_limit,
                    generation_limit=self._config.generation_limit,
                    conversation_sync_limit=self._config.conversation_sync_limit,
                    activity_limit=self._config.activity_limit,
                    command_limit=self._config.command_limit,
                    recovery_limit=self._config.recovery_limit,
                    outbox_delivery_limit=self._config.outbox_delivery_limit,
                )
            except Exception as error:
                failures += 1
                delay = min(
                    self._config.poll_interval.total_seconds() * 2 ** min(failures, 12),
                    MAX_SCHEDULER_ERROR_BACKOFF_SECONDS,
                )
                self._logger.error(
                    "scheduler_backoff_scheduled",
                    error_type=type(error).__name__,
                    consecutive_failures=failures,
                    retry_delay_seconds=delay,
                )
            else:
                failures = 0
                delay = self._config.poll_interval.total_seconds()

            if not stop_event.is_set():
                await self._wait_for_stop(stop_event, delay)
