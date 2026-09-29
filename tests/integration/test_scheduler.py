from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.threads_test_support import FakeThreadsAPI, TokenProvider
from threads_platform.application.commands.composition import compose_command_runtime
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.scheduler import SchedulerTickResult, run_scheduler_tick
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.domain.account_activities import (
    AccountActivityPlan,
    AccountActivityTemplate,
    ActivityPriority,
    ScheduledActivity,
    ScheduledActivityMaterializationStatus,
)
from threads_platform.domain.accounts import AccountExecutionMode, ThreadsAccount
from threads_platform.domain.capabilities import RouteTarget
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    WorkerCapability,
    WorkerNode,
    WorkerStatus,
)
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.models import (
    CommandRouteDecisionRecord,
    OutboxEventRecord,
    WorkerJobRecord,
)
from threads_platform.infrastructure.persistence.repositories import (
    SQLAlchemyCommandRepository,
    SQLAlchemyScheduledActivityRepository,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration

_FEED_CAPABILITY = "threads.browser.feed.browse"


class FixedClock:
    def __init__(self, current_time: datetime) -> None:
        self.current_time = current_time

    def now(self) -> datetime:
        return self.current_time


class SimulatedProcessCrash(BaseException):
    pass


@dataclass(frozen=True, slots=True)
class ActivityFixture:
    account_id: UUID
    worker_id: UUID
    activity: ScheduledActivity


def _services(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory, now: datetime
) -> tuple[CommandRuntime, WorkerJobService]:
    clock = FixedClock(now)
    worker_jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    composition = compose_command_runtime(
        unit_of_work_factory,
        worker_jobs,
        threads_api_gateway=None,
        threads_access_token_provider=None,
        clock=clock,
    )
    return composition.command_runtime, worker_jobs


async def _create_activity(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    *,
    now: datetime,
    due_at: datetime | None = None,
    priority: ActivityPriority = ActivityPriority.NORMAL,
    worker_status: WorkerStatus = WorkerStatus.ONLINE,
) -> ActivityFixture:
    worker_id = uuid4()
    account = ThreadsAccount(
        threads_user_id=f"scheduler-user-{uuid4()}",
        username="scheduler_test",
        execution_mode=AccountExecutionMode.BROWSER_ONLY,
    )
    worker = WorkerNode(
        worker_id=worker_id,
        display_name=f"Scheduler worker {worker_id}",
        hostname=f"scheduler-host-{worker_id}",
        platform="windows",
        agent_version="1.0.0",
        protocol_version=1,
        capabilities_schema_version=1,
        status=worker_status,
        max_concurrent_jobs=4,
        last_heartbeat_at=now,
        presence_expires_at=(
            now + timedelta(hours=1)
            if worker_status is WorkerStatus.ONLINE
            else now - timedelta(seconds=1)
        ),
        created_at=now,
        updated_at=now,
    )
    profile = BrowserProfile(worker_id, f"scheduler-profile-{uuid4()}")
    assignment = AccountWorkerAssignment(account.id, worker_id, profile.profile_ref)
    plan = AccountActivityPlan(account_id=account.id, name="Scheduler test plan")
    template = AccountActivityTemplate(
        account_id=account.id,
        plan_id=plan.id,
        name="Scheduler feed browse",
        activity_type=_FEED_CAPABILITY,
        configuration={"max_items": 5},
        priority=priority,
        change_reason="scheduler integration test",
    )
    occurrence_time = due_at or now - timedelta(seconds=1)
    activity = ScheduledActivity.from_plan_template(
        plan,
        template,
        occurrence_time,
        creation_reason="scheduler test occurrence",
        created_at=now - timedelta(minutes=1),
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.workers.add(worker)
        await unit_of_work.browser_profiles.add(profile)
        await unit_of_work.assignments.add(assignment)
        await unit_of_work.worker_capabilities.replace_for_worker(
            worker_id,
            [WorkerCapability(worker_id, _FEED_CAPABILITY, 1, advertised_at=now)],
        )
        await unit_of_work.activity_plans.add(plan)
        await unit_of_work.activity_templates.add_revision(template)
        stored = await unit_of_work.scheduled_activities.add_if_absent(activity)
    return ActivityFixture(account.id, worker_id, stored)


async def _run_tick(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    *,
    now: datetime,
    activity_limit: int = 10,
    command_limit: int = 10,
    recovery_limit: int = 10,
) -> SchedulerTickResult:
    runtime, worker_jobs = _services(unit_of_work_factory, now)
    return await run_scheduler_tick(
        unit_of_work_factory,
        runtime,
        worker_jobs,
        now=now,
        activity_limit=activity_limit,
        command_limit=command_limit,
        recovery_limit=recovery_limit,
    )


async def _count_for_command(
    session: AsyncSession,
    model: type[WorkerJobRecord] | type[CommandRouteDecisionRecord],
    command_id: str,
) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(model).where(model.command_id == command_id)
        )
        or 0
    )


async def test_scheduler_uses_shared_runtime_composition_for_local_api_commands(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    clock = FixedClock(now)
    account = ThreadsAccount(
        threads_user_id=f"scheduler-api-user-{uuid4()}",
        username="scheduler_api_test",
        execution_mode=AccountExecutionMode.API_ONLY,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)

    worker_jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    api = FakeThreadsAPI()
    composition = compose_command_runtime(
        unit_of_work_factory,
        worker_jobs,
        threads_api_gateway=api,
        threads_access_token_provider=TokenProvider(),
        clock=clock,
    )
    receipt = await composition.command_runtime.receive(
        {
            "protocol_version": 1,
            "command_id": f"scheduler-api-{uuid4()}",
            "correlation_id": f"scheduler-api-correlation-{uuid4()}",
            "account_id": str(account.id),
            "created_at": now.isoformat(),
            "command_type": "threads.publish_text",
            "payload": {"text": "handled by the scheduler API composition"},
        }
    )

    result = await run_scheduler_tick(
        unit_of_work_factory,
        composition.command_runtime,
        worker_jobs,
        now=now,
        activity_limit=1,
        command_limit=1,
        recovery_limit=1,
    )

    async with unit_of_work_factory() as unit_of_work:
        command = await unit_of_work.commands.get_by_command_id(receipt.command_id)
    decisions = list(
        (
            await db_session.scalars(
                select(CommandRouteDecisionRecord).where(
                    CommandRouteDecisionRecord.command_id == receipt.command_id
                )
            )
        ).all()
    )
    outbox_results = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == receipt.command_id)
    )
    assert result.commands_processed == 1
    assert command is not None and command.status is CommandStatus.SUCCEEDED
    assert api.publish_calls == 1
    assert len(decisions) == 1 and decisions[0].target is RouteTarget.LOCAL_API
    assert outbox_results == 1


async def test_waiting_execution_command_is_attempted_once_per_tick(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    older_at = now - timedelta(minutes=5)
    api_only_account = ThreadsAccount(
        threads_user_id=f"scheduler-waiting-user-{uuid4()}",
        username="scheduler_waiting_test",
        execution_mode=AccountExecutionMode.API_ONLY,
    )
    waiting_command = Command(
        command_id=f"waiting-{uuid4()}",
        correlation_id=f"waiting-correlation-{uuid4()}",
        account_id=api_only_account.id,
        command_type="threads.publish_text",
        payload={"text": "wait for configured API executor"},
        status=CommandStatus.WAITING_EXECUTION,
        created_at=older_at,
        received_at=older_at,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(api_only_account)
        await unit_of_work.commands.add(waiting_command)

    browser_fixture = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now - timedelta(seconds=1),
    )
    runtime, worker_jobs = _services(unit_of_work_factory, now)

    first_tick = await run_scheduler_tick(
        unit_of_work_factory,
        runtime,
        worker_jobs,
        now=now,
        activity_limit=1,
        command_limit=3,
        recovery_limit=1,
    )

    browser_command_id = f"activity:{browser_fixture.activity.id}"
    async with unit_of_work_factory() as unit_of_work:
        browser_command = await unit_of_work.commands.get_by_command_id(browser_command_id)
        waiting_job = await unit_of_work.worker_jobs.get_by_command_id(waiting_command.command_id)
        browser_job = await unit_of_work.worker_jobs.get_by_command_id(browser_command_id)
    waiting_decisions = list(
        (
            await db_session.scalars(
                select(CommandRouteDecisionRecord).where(
                    CommandRouteDecisionRecord.command_id == waiting_command.command_id
                )
            )
        ).all()
    )
    browser_decisions = list(
        (
            await db_session.scalars(
                select(CommandRouteDecisionRecord).where(
                    CommandRouteDecisionRecord.command_id == browser_command_id
                )
            )
        ).all()
    )
    assert first_tick.commands_processed == 2
    assert browser_command is not None and browser_command.status is CommandStatus.WAITING_EXECUTION
    assert waiting_job is None
    assert browser_job is not None and browser_job.status is WorkerJobStatus.QUEUED
    assert len(waiting_decisions) == 1
    assert waiting_decisions[0].target is RouteTarget.WAITING_EXECUTION
    assert len(browser_decisions) == 1
    assert browser_decisions[0].target is RouteTarget.WORKER_JOB

    second_tick = await run_scheduler_tick(
        unit_of_work_factory,
        runtime,
        worker_jobs,
        now=now,
        activity_limit=1,
        command_limit=1,
        recovery_limit=1,
    )
    waiting_decisions_next_tick = list(
        (
            await db_session.scalars(
                select(CommandRouteDecisionRecord).where(
                    CommandRouteDecisionRecord.command_id == waiting_command.command_id
                )
            )
        ).all()
    )
    assert second_tick.commands_processed == 1
    assert len(waiting_decisions_next_tick) == 2


async def test_tick_materializes_routes_and_reconstructed_tick_does_not_duplicate(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    fixture = await _create_activity(unit_of_work_factory, now=now, priority=ActivityPriority.HIGH)
    runtime, worker_jobs = _services(unit_of_work_factory, now)

    result = await run_scheduler_tick(
        unit_of_work_factory,
        runtime,
        worker_jobs,
        now=now,
        activity_limit=1,
        command_limit=1,
        recovery_limit=1,
    )

    command_id = f"activity:{fixture.activity.id}"
    async with unit_of_work_factory() as unit_of_work:
        activity = await unit_of_work.scheduled_activities.get(fixture.activity.id)
        command = await unit_of_work.commands.get_by_command_id(command_id)
        job = await unit_of_work.worker_jobs.get_by_command_id(command_id)
    assert result.activities_materialized == 1
    assert result.commands_processed == 1
    assert result.worker_jobs_recovered == 0
    assert activity is not None
    assert activity.materialization_status is ScheduledActivityMaterializationStatus.MATERIALIZED
    assert command is not None and command.status is CommandStatus.WAITING_EXECUTION
    assert command.priority == 100
    assert job is not None and job.status is WorkerJobStatus.QUEUED
    assert job.priority == command.priority

    reconstructed = await _run_tick(unit_of_work_factory, now=now)
    assert reconstructed.commands_processed == 0
    assert await _count_for_command(db_session, WorkerJobRecord, command_id) == 1
    assert await _count_for_command(db_session, CommandRouteDecisionRecord, command_id) == 1


async def test_crash_after_materialization_restarts_from_database_state(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    fixture = await _create_activity(unit_of_work_factory, now=now)
    first_runtime, first_jobs = _services(unit_of_work_factory, now)

    async def crash_before_route(
        *, now: datetime | None = None, exclude_command_ids: frozenset[str] = frozenset()
    ) -> None:
        raise SimulatedProcessCrash

    monkeypatch.setattr(first_runtime, "process_next", crash_before_route)
    with pytest.raises(SimulatedProcessCrash):
        await run_scheduler_tick(
            unit_of_work_factory,
            first_runtime,
            first_jobs,
            now=now,
            activity_limit=1,
            command_limit=1,
            recovery_limit=1,
        )

    command_id = f"activity:{fixture.activity.id}"
    async with unit_of_work_factory() as unit_of_work:
        command = await unit_of_work.commands.get_by_command_id(command_id)
        job = await unit_of_work.worker_jobs.get_by_command_id(command_id)
    assert command is not None and command.status is CommandStatus.RECEIVED
    assert job is None

    resumed = await _run_tick(unit_of_work_factory, now=now)
    assert resumed.commands_processed == 1
    after_route = await _run_tick(unit_of_work_factory, now=now)
    assert after_route.commands_processed == 0
    assert await _count_for_command(db_session, WorkerJobRecord, command_id) == 1
    assert await _count_for_command(db_session, CommandRouteDecisionRecord, command_id) == 1


async def test_concurrent_ticks_split_distinct_due_work_without_loss(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    first_activity = await _create_activity(
        unit_of_work_factory, now=now, due_at=now - timedelta(minutes=2)
    )
    second_activity = await _create_activity(
        unit_of_work_factory, now=now, due_at=now - timedelta(minutes=1)
    )
    second_engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    second_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(second_engine))
    first_selected = asyncio.Event()
    release_first = asyncio.Event()
    original_select = SQLAlchemyScheduledActivityRepository.list_due_pending_for_materialization

    async def hold_first_selection(
        repository: SQLAlchemyScheduledActivityRepository,
        selected_at: datetime,
        limit: int,
    ) -> list[ScheduledActivity]:
        selected = await original_select(repository, selected_at, limit)
        if selected and not first_selected.is_set():
            first_selected.set()
            await release_first.wait()
        return selected

    monkeypatch.setattr(
        SQLAlchemyScheduledActivityRepository,
        "list_due_pending_for_materialization",
        hold_first_selection,
    )
    try:
        first_runtime, first_jobs = _services(unit_of_work_factory, now)
        second_runtime, second_jobs = _services(second_factory, now)
        first = asyncio.create_task(
            run_scheduler_tick(
                unit_of_work_factory,
                first_runtime,
                first_jobs,
                now=now,
                activity_limit=1,
                command_limit=1,
                recovery_limit=1,
            )
        )
        await asyncio.wait_for(first_selected.wait(), timeout=10)
        second = asyncio.create_task(
            run_scheduler_tick(
                second_factory,
                second_runtime,
                second_jobs,
                now=now,
                activity_limit=1,
                command_limit=1,
                recovery_limit=1,
            )
        )
        second_result = await asyncio.wait_for(second, timeout=10)
        release_first.set()
        first_result = await asyncio.wait_for(first, timeout=10)
    finally:
        release_first.set()
        await second_engine.dispose()

    assert first_result.activities_materialized == 1
    assert second_result.activities_materialized == 1
    assert first_result.commands_processed == 1
    assert second_result.commands_processed == 1
    async with unit_of_work_factory() as unit_of_work:
        first_job = await unit_of_work.worker_jobs.get_by_command_id(
            f"activity:{first_activity.activity.id}"
        )
        second_job = await unit_of_work.worker_jobs.get_by_command_id(
            f"activity:{second_activity.activity.id}"
        )
    assert first_job is not None
    assert second_job is not None


async def test_two_ticks_racing_one_due_occurrence_create_one_command_and_job(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    fixture = await _create_activity(unit_of_work_factory, now=now)
    second_engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    second_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(second_engine))
    first_selected = asyncio.Event()
    release_first = asyncio.Event()
    original_select = SQLAlchemyScheduledActivityRepository.list_due_pending_for_materialization

    async def hold_first_selection(
        repository: SQLAlchemyScheduledActivityRepository,
        selected_at: datetime,
        limit: int,
    ) -> list[ScheduledActivity]:
        selected = await original_select(repository, selected_at, limit)
        if selected and not first_selected.is_set():
            first_selected.set()
            await release_first.wait()
        return selected

    monkeypatch.setattr(
        SQLAlchemyScheduledActivityRepository,
        "list_due_pending_for_materialization",
        hold_first_selection,
    )
    try:
        first_runtime, first_jobs = _services(unit_of_work_factory, now)
        second_runtime, second_jobs = _services(second_factory, now)
        first = asyncio.create_task(
            run_scheduler_tick(
                unit_of_work_factory,
                first_runtime,
                first_jobs,
                now=now,
                activity_limit=1,
                command_limit=1,
                recovery_limit=1,
            )
        )
        await asyncio.wait_for(first_selected.wait(), timeout=10)
        second = asyncio.create_task(
            run_scheduler_tick(
                second_factory,
                second_runtime,
                second_jobs,
                now=now,
                activity_limit=1,
                command_limit=1,
                recovery_limit=1,
            )
        )
        second_result = await asyncio.wait_for(second, timeout=10)
        release_first.set()
        first_result = await asyncio.wait_for(first, timeout=10)
    finally:
        release_first.set()
        await second_engine.dispose()

    command_id = f"activity:{fixture.activity.id}"
    assert first_result.activities_materialized == 1
    assert second_result.activities_materialized == 0
    assert first_result.commands_processed == 1
    assert second_result.commands_processed == 0
    assert await _count_for_command(db_session, WorkerJobRecord, command_id) == 1
    assert await _count_for_command(db_session, CommandRouteDecisionRecord, command_id) == 1


async def test_concurrent_ticks_route_one_ready_command_once(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    fixture = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now + timedelta(hours=1),
    )
    command = Command(
        command_id=f"ready:{fixture.activity.id}",
        correlation_id=f"ready-correlation:{fixture.activity.id}",
        account_id=fixture.account_id,
        command_type=_FEED_CAPABILITY,
        payload={"max_items": 5},
        created_at=now,
        received_at=now,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.commands.add(command)

    second_engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    second_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(second_engine))
    first_selected = asyncio.Event()
    release_first = asyncio.Event()
    original_select = SQLAlchemyCommandRepository.get_next_ready_for_update

    async def hold_first_command(
        repository: SQLAlchemyCommandRepository,
        selected_at: datetime,
        *,
        exclude_command_ids: frozenset[str] = frozenset(),
    ) -> Command | None:
        selected = await original_select(
            repository, selected_at, exclude_command_ids=exclude_command_ids
        )
        if selected is not None and not first_selected.is_set():
            first_selected.set()
            await release_first.wait()
        return selected

    monkeypatch.setattr(
        SQLAlchemyCommandRepository,
        "get_next_ready_for_update",
        hold_first_command,
    )
    try:
        first_runtime, first_jobs = _services(unit_of_work_factory, now)
        second_runtime, second_jobs = _services(second_factory, now)
        first = asyncio.create_task(
            run_scheduler_tick(
                unit_of_work_factory,
                first_runtime,
                first_jobs,
                now=now,
                activity_limit=1,
                command_limit=1,
                recovery_limit=1,
            )
        )
        await asyncio.wait_for(first_selected.wait(), timeout=10)
        second = asyncio.create_task(
            run_scheduler_tick(
                second_factory,
                second_runtime,
                second_jobs,
                now=now,
                activity_limit=1,
                command_limit=1,
                recovery_limit=1,
            )
        )
        second_result = await asyncio.wait_for(second, timeout=10)
        release_first.set()
        first_result = await asyncio.wait_for(first, timeout=10)
    finally:
        release_first.set()
        await second_engine.dispose()

    assert first_result.commands_processed == 1
    assert second_result.commands_processed == 0
    assert await _count_for_command(db_session, WorkerJobRecord, command.command_id) == 1
    assert await _count_for_command(db_session, CommandRouteDecisionRecord, command.command_id) == 1


async def test_concurrent_ticks_emit_one_terminal_outbox_result(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    fixture = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now + timedelta(hours=1),
    )
    command = Command(
        command_id=f"expired:{fixture.activity.id}",
        correlation_id=f"expired-correlation:{fixture.activity.id}",
        account_id=fixture.account_id,
        command_type=_FEED_CAPABILITY,
        payload={"max_items": 5},
        created_at=now - timedelta(minutes=1),
        received_at=now - timedelta(minutes=1),
        deadline_at=now,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.commands.add(command)

    first_runtime, first_jobs = _services(unit_of_work_factory, now)
    second_runtime, second_jobs = _services(unit_of_work_factory, now)
    await asyncio.gather(
        run_scheduler_tick(
            unit_of_work_factory,
            first_runtime,
            first_jobs,
            now=now,
            activity_limit=1,
            command_limit=1,
            recovery_limit=1,
        ),
        run_scheduler_tick(
            unit_of_work_factory,
            second_runtime,
            second_jobs,
            now=now,
            activity_limit=1,
            command_limit=1,
            recovery_limit=1,
        ),
    )

    outbox_results = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == command.command_id)
    )
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.commands.get_by_command_id(command.command_id)
    assert stored is not None and stored.status is CommandStatus.EXPIRED
    assert outbox_results == 1


async def test_command_limit_drains_ready_work_across_reconstructed_ticks(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    fixtures = [
        await _create_activity(unit_of_work_factory, now=now),
        await _create_activity(unit_of_work_factory, now=now),
    ]

    first = await _run_tick(
        unit_of_work_factory,
        now=now,
        activity_limit=2,
        command_limit=1,
        recovery_limit=1,
    )
    assert first.activities_materialized == 2
    assert first.commands_processed == 1

    second = await _run_tick(
        unit_of_work_factory,
        now=now,
        activity_limit=2,
        command_limit=1,
        recovery_limit=1,
    )
    assert second.activities_materialized == 0
    assert second.commands_processed == 1
    async with unit_of_work_factory() as unit_of_work:
        jobs = [
            await unit_of_work.worker_jobs.get_by_command_id(f"activity:{item.activity.id}")
            for item in fixtures
        ]
    assert all(job is not None for job in jobs)


async def test_offline_assigned_worker_keeps_one_queued_job_until_claimable(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    fixture = await _create_activity(
        unit_of_work_factory,
        now=now,
        worker_status=WorkerStatus.OFFLINE,
    )

    result = await _run_tick(unit_of_work_factory, now=now)
    command_id = f"activity:{fixture.activity.id}"
    assert result.activities_materialized == 1
    assert result.commands_processed == 1
    async with unit_of_work_factory() as unit_of_work:
        command = await unit_of_work.commands.get_by_command_id(command_id)
        job = await unit_of_work.worker_jobs.get_by_command_id(command_id)
    assert command is not None and command.status is CommandStatus.WAITING_EXECUTION
    assert job is not None and job.status is WorkerJobStatus.QUEUED
    assert job.assigned_worker_id == fixture.worker_id
    assert job.account_affinity_required is True

    repeated = await _run_tick(unit_of_work_factory, now=now)
    assert repeated.activities_materialized == 0
    assert repeated.commands_processed == 0
    async with unit_of_work_factory() as unit_of_work:
        worker = await unit_of_work.workers.get_for_update(fixture.worker_id)
        assert worker is not None
        worker.status = WorkerStatus.ONLINE
        worker.last_heartbeat_at = now
        worker.presence_expires_at = now + timedelta(hours=1)
        await unit_of_work.workers.update(worker)
    worker_jobs = WorkerJobService(unit_of_work_factory, clock=FixedClock(now))
    claimed = await worker_jobs.claim_next(fixture.worker_id)
    assert claimed is not None and claimed.command_id == command_id
    assert claimed.status is WorkerJobStatus.RUNNING


async def test_tick_runs_bounded_existing_worker_job_recovery(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    worker_id = uuid4()
    worker = WorkerNode(
        worker_id=worker_id,
        display_name="Recovery worker",
        hostname="recovery-host",
        platform="windows",
        agent_version="1.0.0",
        status=WorkerStatus.ONLINE,
        max_concurrent_jobs=4,
        last_heartbeat_at=now,
        presence_expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    clock = FixedClock(now)
    worker_jobs = WorkerJobService(
        unit_of_work_factory,
        clock=clock,
        lease_duration=timedelta(seconds=30),
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.workers.add(worker)
        await unit_of_work.worker_capabilities.replace_for_worker(
            worker_id,
            [WorkerCapability(worker_id, "synthetic.scheduler-recovery", 1, advertised_at=now)],
        )
    first = await worker_jobs.enqueue(
        "synthetic.scheduler-recovery",
        1,
        assigned_worker_id=worker_id,
        account_affinity_required=False,
        deadline_at=now + timedelta(seconds=1),
    )
    second = await worker_jobs.enqueue(
        "synthetic.scheduler-recovery",
        1,
        assigned_worker_id=worker_id,
        account_affinity_required=False,
        deadline_at=now + timedelta(seconds=2),
    )
    recovery_time = now + timedelta(seconds=2)
    runtime, _ = _services(unit_of_work_factory, recovery_time)

    result = await run_scheduler_tick(
        unit_of_work_factory,
        runtime,
        worker_jobs,
        now=recovery_time,
        activity_limit=1,
        command_limit=1,
        recovery_limit=1,
    )

    assert result.worker_jobs_recovered == 1
    async with unit_of_work_factory() as unit_of_work:
        recovered_first = await unit_of_work.worker_jobs.get(first.id)
        recovered_second = await unit_of_work.worker_jobs.get(second.id)
    assert recovered_first is not None and recovered_first.status is WorkerJobStatus.EXPIRED
    assert recovered_second is not None and recovered_second.status is WorkerJobStatus.QUEUED
    assert await worker_jobs.recover_expired(limit=1, now=recovery_time) == 1
