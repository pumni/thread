from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.threads_test_support import FakeThreadsAPI, TokenProvider
from threads_platform.application.account_activity_materialization import (
    materialize_due_account_activities,
)
from threads_platform.application.account_activity_recurrence import (
    generate_due_account_activity_occurrences,
)
from threads_platform.application.commands.composition import compose_command_runtime
from threads_platform.application.scheduler import run_scheduler_tick
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.domain.account_activities import (
    AccountActivityPlan,
    AccountActivityPlanStatus,
    AccountActivityRecurrenceState,
    AccountActivityTemplate,
    ActivityPriority,
    ActivityRecurrenceKind,
    ScheduledActivity,
    ScheduledActivityMaterializationStatus,
)
from threads_platform.domain.accounts import AccountExecutionMode, ThreadsAccount
from threads_platform.domain.capabilities import RouteTarget
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
    WorkerJobRecord,
)
from threads_platform.infrastructure.persistence.repositories import (
    SQLAlchemyAccountActivityRecurrenceStateRepository,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration

_FEED_CAPABILITY = "threads.browser.feed.browse"


async def _create_fixed_interval_template(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    *,
    anchor_at: datetime,
    interval_seconds: int = 3_600,
    activity_type: str = _FEED_CAPABILITY,
) -> tuple[ThreadsAccount, AccountActivityPlan, AccountActivityTemplate]:
    account = ThreadsAccount(
        threads_user_id=f"recurrence-{uuid4()}",
        username="recurrence_test",
        execution_mode=AccountExecutionMode.BROWSER_ONLY,
    )
    plan = AccountActivityPlan(
        account_id=account.id,
        name="Recurring browse plan",
        created_at=anchor_at,
        updated_at=anchor_at,
    )
    template = AccountActivityTemplate(
        account_id=account.id,
        plan_id=plan.id,
        name="Recurring feed browse",
        activity_type=activity_type,
        configuration={"max_items": 3},
        priority=ActivityPriority.NORMAL,
        change_reason="fixed interval test policy",
        created_at=anchor_at,
        recurrence_kind=ActivityRecurrenceKind.FIXED_INTERVAL,
        anchor_at=anchor_at,
        interval_seconds=interval_seconds,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.activity_plans.add(plan)
        await unit_of_work.activity_templates.add_revision(template)
    return account, plan, template


async def _get_cursor(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    template: AccountActivityTemplate,
) -> AccountActivityRecurrenceState:
    async with unit_of_work_factory() as unit_of_work:
        state = await unit_of_work.activity_recurrence_states.get(template.id, template.revision)
    assert state is not None
    return state


async def _transition_plan(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    plan_id: UUID,
    target: AccountActivityPlanStatus,
    at: datetime,
) -> None:
    async with unit_of_work_factory() as unit_of_work:
        plan = await unit_of_work.activity_plans.get_for_update(plan_id)
        assert plan is not None
        plan.transition(target, at, reason=f"test {target.value.lower()}")
        await unit_of_work.activity_plans.update(plan)


async def test_fixed_interval_catches_up_exact_slots_across_bounded_reconstructed_calls(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    _, _, template = await _create_fixed_interval_template(unit_of_work_factory, anchor_at=anchor)

    first = await generate_due_account_activity_occurrences(
        unit_of_work_factory, now=anchor, limit=1
    )
    late_now = anchor + timedelta(hours=4, minutes=30)
    second = await generate_due_account_activity_occurrences(
        unit_of_work_factory, now=late_now, limit=2
    )
    # Reconstruct the repository factory from the same database after restart.
    restart_engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    restart_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(restart_engine))
    try:
        third = await generate_due_account_activity_occurrences(
            restart_factory, now=late_now, limit=2
        )
    finally:
        await restart_engine.dispose()

    generated = [*first, *second, *third]
    assert [item.due_at for item in generated] == [
        anchor + timedelta(hours=offset) for offset in range(5)
    ]
    state = await _get_cursor(unit_of_work_factory, template)
    assert state.next_due_at == anchor + timedelta(hours=5)
    assert state.last_generated_due_at == anchor + timedelta(hours=4)
    assert state.generated_count == 5


async def test_future_anchor_is_not_replaced_with_a_now_occurrence(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    account, _, template = await _create_fixed_interval_template(
        unit_of_work_factory, anchor_at=anchor
    )

    generated = await generate_due_account_activity_occurrences(
        unit_of_work_factory,
        now=anchor - timedelta(seconds=1),
        limit=1,
    )
    state = await _get_cursor(unit_of_work_factory, template)
    async with unit_of_work_factory() as unit_of_work:
        activities = await unit_of_work.scheduled_activities.list_for_account(account.id)

    assert generated == []
    assert activities == []
    assert state.next_due_at == anchor
    assert state.generated_count == 0


async def test_existing_cursor_slot_is_reconciled_without_duplicate_occurrence(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account, plan, template = await _create_fixed_interval_template(
        unit_of_work_factory,
        anchor_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
    )
    due_at = template.anchor_at
    assert due_at is not None
    preexisting = ScheduledActivity.from_plan_template(plan, template, due_at)
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.scheduled_activities.add_if_absent(preexisting)

    generated = await generate_due_account_activity_occurrences(
        unit_of_work_factory, now=due_at, limit=1
    )

    async with unit_of_work_factory() as unit_of_work:
        activities = await unit_of_work.scheduled_activities.list_for_account(account.id)
    state = await _get_cursor(unit_of_work_factory, template)
    assert generated == []
    assert [item.id for item in activities] == [preexisting.id]
    assert state.next_due_at == due_at + timedelta(hours=1)
    assert state.generated_count == 1


async def test_paused_plan_generates_pending_history_then_materializes_after_resume(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    account, plan, template = await _create_fixed_interval_template(
        unit_of_work_factory, anchor_at=anchor
    )
    await _transition_plan(
        unit_of_work_factory,
        plan.id,
        AccountActivityPlanStatus.PAUSED,
        anchor + timedelta(minutes=1),
    )

    generated = await generate_due_account_activity_occurrences(
        unit_of_work_factory, now=anchor + timedelta(hours=1), limit=2
    )
    assert [item.due_at for item in generated] == [anchor, anchor + timedelta(hours=1)]
    assert all(item.plan_status_snapshot is AccountActivityPlanStatus.PAUSED for item in generated)
    assert (
        await materialize_due_account_activities(
            unit_of_work_factory, now=anchor + timedelta(hours=1), limit=10
        )
        == []
    )

    async with unit_of_work_factory() as unit_of_work:
        pending = await unit_of_work.scheduled_activities.list_for_account(account.id)
    assert len(pending) == 2
    assert all(
        item.materialization_status is ScheduledActivityMaterializationStatus.PENDING
        for item in pending
    )

    await _transition_plan(
        unit_of_work_factory,
        plan.id,
        AccountActivityPlanStatus.ACTIVE,
        anchor + timedelta(hours=1, minutes=1),
    )
    commands = await materialize_due_account_activities(
        unit_of_work_factory,
        now=anchor + timedelta(hours=1, minutes=1),
        limit=10,
    )
    assert len(commands) == 2
    assert len({command.command_id for command in commands}) == 2
    assert template.revision == 1


async def test_disabled_plan_stops_generation_and_marks_existing_occurrences_terminal(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    account, plan, template = await _create_fixed_interval_template(
        unit_of_work_factory, anchor_at=anchor
    )
    first = await generate_due_account_activity_occurrences(
        unit_of_work_factory, now=anchor, limit=1
    )
    await _transition_plan(
        unit_of_work_factory,
        plan.id,
        AccountActivityPlanStatus.DISABLED,
        anchor + timedelta(minutes=1),
    )

    assert (
        await generate_due_account_activity_occurrences(
            unit_of_work_factory, now=anchor + timedelta(hours=3), limit=10
        )
        == []
    )
    assert (
        await materialize_due_account_activities(
            unit_of_work_factory, now=anchor + timedelta(hours=3), limit=10
        )
        == []
    )
    async with unit_of_work_factory() as unit_of_work:
        activities = await unit_of_work.scheduled_activities.list_for_account(account.id)
    state = await _get_cursor(unit_of_work_factory, template)
    assert len(first) == len(activities) == 1
    assert activities[0].materialization_status is (
        ScheduledActivityMaterializationStatus.NON_MATERIALIZABLE
    )
    assert activities[0].materialization_reason == "PLAN_DISABLED"
    assert state.next_due_at == anchor + timedelta(hours=1)
    assert state.generated_count == 1


async def test_new_template_revision_supersedes_old_unmade_slots_only(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    account, plan, first_template = await _create_fixed_interval_template(
        unit_of_work_factory, anchor_at=anchor
    )
    first = await generate_due_account_activity_occurrences(
        unit_of_work_factory, now=anchor, limit=1
    )
    new_anchor = anchor + timedelta(hours=4)
    second_template = AccountActivityTemplate(
        id=first_template.id,
        revision=2,
        account_id=account.id,
        plan_id=plan.id,
        name="Revised recurring feed browse",
        activity_type=first_template.activity_type,
        configuration={"max_items": 2},
        priority=ActivityPriority.LOW,
        change_reason="new recurrence policy",
        created_at=new_anchor,
        recurrence_kind=ActivityRecurrenceKind.FIXED_INTERVAL,
        anchor_at=new_anchor,
        interval_seconds=7_200,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.activity_templates.add_revision(second_template)

    new_occurrences = await generate_due_account_activity_occurrences(
        unit_of_work_factory,
        now=new_anchor + timedelta(minutes=30),
        limit=10,
    )
    async with unit_of_work_factory() as unit_of_work:
        retained = await unit_of_work.scheduled_activities.list_for_account(account.id)
    old_cursor = await _get_cursor(unit_of_work_factory, first_template)
    new_cursor = await _get_cursor(unit_of_work_factory, second_template)

    assert len(first) == 1
    assert [(item.template_revision, item.due_at) for item in new_occurrences] == [(2, new_anchor)]
    assert {(item.template_revision, item.due_at) for item in retained} == {
        (1, anchor),
        (2, new_anchor),
    }
    old_occurrence = next(item for item in retained if item.template_revision == 1)
    revised_occurrence = next(item for item in retained if item.template_revision == 2)
    assert old_occurrence.configuration_snapshot["max_items"] == 3
    assert old_occurrence.priority is ActivityPriority.NORMAL
    assert old_occurrence.materialization_status is ScheduledActivityMaterializationStatus.PENDING
    assert revised_occurrence.configuration_snapshot["max_items"] == 2
    assert revised_occurrence.priority is ActivityPriority.LOW
    assert old_cursor.next_due_at == anchor + timedelta(hours=1)
    assert old_cursor.generated_count == 1
    assert new_cursor.next_due_at == new_anchor + timedelta(hours=2)
    assert new_cursor.generated_count == 1


async def test_generator_rolls_back_occurrence_and_cursor_together(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    account, _, template = await _create_fixed_interval_template(
        unit_of_work_factory, anchor_at=anchor
    )

    async def fail_cursor_update(
        _repository: SQLAlchemyAccountActivityRecurrenceStateRepository, _state: object
    ) -> None:
        raise RuntimeError("simulated failure before recurrence cursor commit")

    monkeypatch.setattr(
        SQLAlchemyAccountActivityRecurrenceStateRepository,
        "update",
        fail_cursor_update,
    )
    with pytest.raises(RuntimeError, match="before recurrence cursor commit"):
        await generate_due_account_activity_occurrences(unit_of_work_factory, now=anchor, limit=1)

    state_after_rollback = await _get_cursor(unit_of_work_factory, template)
    async with unit_of_work_factory() as unit_of_work:
        activities_after_rollback = await unit_of_work.scheduled_activities.list_for_account(
            account.id
        )
    assert state_after_rollback.next_due_at == anchor
    assert state_after_rollback.generated_count == 0
    assert activities_after_rollback == []

    monkeypatch.undo()
    retried = await generate_due_account_activity_occurrences(
        unit_of_work_factory, now=anchor, limit=1
    )
    assert len(retried) == 1
    assert retried[0].due_at == anchor


async def test_two_generators_racing_one_cursor_create_one_occurrence(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    account, _, template = await _create_fixed_interval_template(
        unit_of_work_factory, anchor_at=anchor
    )
    second_engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    second_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(second_engine))
    selected = asyncio.Event()
    release = asyncio.Event()
    original = SQLAlchemyAccountActivityRecurrenceStateRepository.get_next_due_for_update

    async def hold_first(
        repository: SQLAlchemyAccountActivityRecurrenceStateRepository,
        now: datetime,
        *,
        exclude: frozenset[tuple[UUID, int]] = frozenset(),
    ) -> AccountActivityRecurrenceState | None:
        state = await original(repository, now, exclude=exclude)
        if state is not None and not selected.is_set():
            selected.set()
            await release.wait()
        return state

    monkeypatch.setattr(
        SQLAlchemyAccountActivityRecurrenceStateRepository,
        "get_next_due_for_update",
        hold_first,
    )
    try:
        first_task = asyncio.create_task(
            generate_due_account_activity_occurrences(unit_of_work_factory, now=anchor, limit=1)
        )
        await asyncio.wait_for(selected.wait(), timeout=10)
        second_result = await asyncio.wait_for(
            generate_due_account_activity_occurrences(second_factory, now=anchor, limit=1),
            timeout=10,
        )
        release.set()
        first_result = await first_task
        assert len(first_result) == 1
        assert second_result == []
        state = await _get_cursor(unit_of_work_factory, template)
        async with unit_of_work_factory() as unit_of_work:
            activities = await unit_of_work.scheduled_activities.list_for_account(account.id)
        assert len(activities) == 1
        assert activities[0].due_at == anchor
        assert state.generated_count == 1
        assert state.next_due_at == anchor + timedelta(hours=1)
    finally:
        release.set()
        await second_engine.dispose()


async def test_generators_split_distinct_due_states_while_one_cursor_is_locked(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    first_account, _, first_template = await _create_fixed_interval_template(
        unit_of_work_factory, anchor_at=first_anchor
    )
    second_anchor = first_anchor + timedelta(minutes=1)
    second_account, _, second_template = await _create_fixed_interval_template(
        unit_of_work_factory, anchor_at=second_anchor
    )
    second_engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    second_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(second_engine))
    selected = asyncio.Event()
    release = asyncio.Event()
    original = SQLAlchemyAccountActivityRecurrenceStateRepository.get_next_due_for_update

    async def hold_first(
        repository: SQLAlchemyAccountActivityRecurrenceStateRepository,
        now: datetime,
        *,
        exclude: frozenset[tuple[UUID, int]] = frozenset(),
    ) -> AccountActivityRecurrenceState | None:
        state = await original(repository, now, exclude=exclude)
        if state is not None and not selected.is_set():
            selected.set()
            await release.wait()
        return state

    monkeypatch.setattr(
        SQLAlchemyAccountActivityRecurrenceStateRepository,
        "get_next_due_for_update",
        hold_first,
    )
    try:
        first_task = asyncio.create_task(
            generate_due_account_activity_occurrences(
                unit_of_work_factory, now=second_anchor, limit=1
            )
        )
        await asyncio.wait_for(selected.wait(), timeout=10)
        second_result = await asyncio.wait_for(
            generate_due_account_activity_occurrences(second_factory, now=second_anchor, limit=1),
            timeout=10,
        )
        release.set()
        first_result = await first_task
        assert [item.template_id for item in first_result] == [first_template.id]
        assert [item.template_id for item in second_result] == [second_template.id]
        async with unit_of_work_factory() as unit_of_work:
            first_rows = await unit_of_work.scheduled_activities.list_for_account(first_account.id)
            second_rows = await unit_of_work.scheduled_activities.list_for_account(
                second_account.id
            )
        assert [item.due_at for item in first_rows] == [first_anchor]
        assert [item.due_at for item in second_rows] == [second_anchor]
    finally:
        release.set()
        await second_engine.dispose()


async def test_scheduler_tick_generates_materializes_routes_and_creates_one_worker_job(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    anchor = now - timedelta(seconds=900)
    account, plan, template = await _create_fixed_interval_template(
        unit_of_work_factory, anchor_at=anchor, interval_seconds=900
    )
    worker_id = uuid4()
    worker = WorkerNode(
        worker_id=worker_id,
        display_name="Recurrence test worker",
        hostname=f"recurrence-host-{worker_id}",
        platform="windows",
        agent_version="1.0.0",
        protocol_version=1,
        capabilities_schema_version=1,
        status=WorkerStatus.ONLINE,
        max_concurrent_jobs=2,
        last_heartbeat_at=now,
        presence_expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    profile = BrowserProfile(worker_id, f"recurrence-profile-{uuid4()}")
    assignment = AccountWorkerAssignment(account.id, worker_id, profile.profile_ref)
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.workers.add(worker)
        await unit_of_work.browser_profiles.add(profile)
        await unit_of_work.assignments.add(assignment)
        await unit_of_work.worker_capabilities.replace_for_worker(
            worker_id,
            [WorkerCapability(worker_id, _FEED_CAPABILITY, 1, advertised_at=now)],
        )

    worker_jobs = WorkerJobService(unit_of_work_factory)
    runtime = compose_command_runtime(
        unit_of_work_factory,
        worker_jobs,
        threads_api_gateway=FakeThreadsAPI(),
        threads_access_token_provider=TokenProvider(),
    ).command_runtime
    result = await run_scheduler_tick(
        unit_of_work_factory,
        runtime,
        worker_jobs,
        now=now,
        generation_limit=2,
        activity_limit=1,
        command_limit=1,
        recovery_limit=1,
    )
    async with unit_of_work_factory() as unit_of_work:
        activities = await unit_of_work.scheduled_activities.list_for_account(account.id)
        activities = sorted(activities, key=lambda activity: activity.due_at)
        stored_plan = await unit_of_work.activity_plans.get(plan.id)
        stored_template = await unit_of_work.activity_templates.get_revision(
            template.id, template.revision
        )
        command = (
            await unit_of_work.commands.get_by_command_id(f"activity:{activities[0].id}")
            if activities
            else None
        )
        job = (
            await unit_of_work.worker_jobs.get_by_command_id(f"activity:{activities[0].id}")
            if activities
            else None
        )
    assert result.activity_occurrences_generated == 2
    assert result.activities_materialized == 1
    assert result.commands_processed == 1
    assert len(activities) == 2
    assert [activity.due_at for activity in activities] == [anchor, now]
    assert (
        activities[0].materialization_status is ScheduledActivityMaterializationStatus.MATERIALIZED
    )
    assert activities[1].materialization_status is ScheduledActivityMaterializationStatus.PENDING
    assert stored_plan is not None and stored_template is not None
    assert command is not None and job is not None
    assert job.command_id == command.command_id == f"activity:{activities[0].id}"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkerJobRecord)
            .where(WorkerJobRecord.command_id == command.command_id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(CommandRouteDecisionRecord)
            .where(
                CommandRouteDecisionRecord.command_id == command.command_id,
                CommandRouteDecisionRecord.target == RouteTarget.WORKER_JOB,
            )
        )
        == 1
    )


async def test_migration_backfills_existing_template_revisions_to_none(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    account = ThreadsAccount(threads_user_id=f"legacy-{uuid4()}", username="legacy")
    plan = AccountActivityPlan(account_id=account.id, name="Legacy plan")
    template = AccountActivityTemplate(
        account_id=account.id,
        plan_id=plan.id,
        name="Legacy template",
        activity_type="unknown.legacy.activity",
        configuration={},
        priority=ActivityPriority.NORMAL,
        change_reason="before recurrence migration",
        created_at=anchor,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.activity_plans.add(plan)
        await unit_of_work.activity_templates.add_revision(template)

    config = Config("alembic.ini")
    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    try:
        await asyncio.to_thread(command.downgrade, config, "20260929_0014")
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as connection:
            recurrence = await connection.execute(
                text(
                    "SELECT recurrence_kind, anchor_at, interval_seconds "
                    "FROM account_activity_template_revisions "
                    "WHERE template_id = :template_id AND revision = :revision"
                ),
                {"template_id": template.id, "revision": template.revision},
            )
            assert recurrence.one() == ("NONE", None, None)
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
        await engine.dispose()


async def test_recurrence_downgrade_refuses_configuration_and_cursor_history(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    _, _, template = await _create_fixed_interval_template(unit_of_work_factory, anchor_at=anchor)
    config = Config("alembic.ini")
    with pytest.raises(RuntimeError, match="ACCOUNT_ACTIVITY_RECURRENCE_DOWNGRADE_BLOCKED"):
        await asyncio.to_thread(command.downgrade, config, "20260929_0014")

    state = await _get_cursor(unit_of_work_factory, template)
    assert state.next_due_at == anchor
    assert state.generated_count == 0


async def test_database_constraints_reject_invalid_interval_and_cursor_deletion(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    anchor = datetime(2026, 10, 1, 10, tzinfo=UTC)
    _, _, template = await _create_fixed_interval_template(unit_of_work_factory, anchor_at=anchor)
    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE account_activity_template_revisions "
                        "SET interval_seconds = 899 "
                        "WHERE template_id = :template_id AND revision = :revision"
                    ),
                    {"template_id": template.id, "revision": template.revision},
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE account_activity_template_revisions "
                        "SET activity_type = 'threads.browser.media.local_upload' "
                        "WHERE template_id = :template_id AND revision = :revision"
                    ),
                    {"template_id": template.id, "revision": template.revision},
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM account_activity_template_revisions "
                        "WHERE template_id = :template_id AND revision = :revision"
                    ),
                    {"template_id": template.id, "revision": template.revision},
                )
    finally:
        await engine.dispose()
