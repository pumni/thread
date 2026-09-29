from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.integration.threads_test_support import (
    FakeThreadsAPI,
    FixedClock,
    TokenProvider,
    seed_account_and_post,
)
from threads_platform.application.commands.composition import compose_command_runtime
from threads_platform.application.conversation_sync_scheduling import (
    dispatch_due_conversation_syncs,
)
from threads_platform.application.ports.threads import ReplyPage
from threads_platform.application.scheduler import run_scheduler_tick
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.domain.accounts import AccountExecutionMode, ThreadsAccount
from threads_platform.domain.capabilities import RouteTarget
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.conversation_sync import (
    ConversationSyncDispatch,
    ConversationSyncKind,
    ConversationSyncSchedule,
    ConversationSyncScheduleStatus,
)
from threads_platform.domain.publishing import ThreadPost
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.models import (
    CommandAttemptRecord,
    CommandRecord,
    CommandRouteDecisionRecord,
    ConversationSyncDispatchRecord,
    ConversationSyncScheduleRecord,
    OutboxEventRecord,
    SyncStateRecord,
)
from threads_platform.infrastructure.persistence.repositories import (
    SQLAlchemyCommandRepository,
    SQLAlchemyConversationSyncDispatchRepository,
    SQLAlchemyConversationSyncScheduleRepository,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration

_ANCHOR = datetime(2026, 9, 29, 10, tzinfo=UTC)


def _new_schedule(
    account_id: UUID,
    threads_post_id: str,
    *,
    anchor_at: datetime = _ANCHOR,
    interval_seconds: int = 3_600,
    sync_kind: ConversationSyncKind = ConversationSyncKind.CONVERSATION,
) -> ConversationSyncSchedule:
    created_at = anchor_at
    return ConversationSyncSchedule(
        account_id=account_id,
        threads_post_id=threads_post_id,
        sync_kind=sync_kind,
        anchor_at=anchor_at,
        interval_seconds=interval_seconds,
        next_due_at=anchor_at + timedelta(seconds=interval_seconds),
        created_at=created_at,
        updated_at=created_at,
    )


async def _create_schedule(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    *,
    anchor_at: datetime = _ANCHOR,
    interval_seconds: int = 3_600,
    sync_kind: ConversationSyncKind = ConversationSyncKind.CONVERSATION,
    threads_post_id: str | None = None,
) -> tuple[UUID, ThreadPost, ConversationSyncSchedule]:
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    schedule = _new_schedule(
        account_id,
        threads_post_id or root.threads_post_id,
        anchor_at=anchor_at,
        interval_seconds=interval_seconds,
        sync_kind=sync_kind,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.conversation_sync_schedules.add(schedule)
    return account_id, root, schedule


async def _get_schedule(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory, schedule_id: UUID
) -> ConversationSyncSchedule:
    async with unit_of_work_factory() as unit_of_work:
        schedule = await unit_of_work.conversation_sync_schedules.get(schedule_id)
    assert schedule is not None
    return schedule


async def _terminalize_command(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    command_id: str,
    status: CommandStatus,
    at: datetime,
) -> None:
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.commands.get_by_command_id_for_update(command_id)
        assert stored is not None
        if status in {CommandStatus.REJECTED, CommandStatus.EXPIRED}:
            stored.transition(status, at)
        elif status is CommandStatus.CANCELLED:
            stored.transition(CommandStatus.VALIDATED, at)
            stored.transition(CommandStatus.WAITING_EXECUTION, at + timedelta(seconds=1))
            stored.transition(CommandStatus.CANCELLED, at + timedelta(seconds=2))
        else:
            stored.transition(CommandStatus.VALIDATED, at)
            stored.transition(CommandStatus.PROCESSING, at + timedelta(seconds=1))
            stored.transition(status, at + timedelta(seconds=2), result={"done": True})
        await unit_of_work.commands.update(stored)


async def test_due_schedule_creates_one_deterministic_internal_command_and_dispatch(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, root, schedule = await _create_schedule(
        unit_of_work_factory, sync_kind=ConversationSyncKind.REPLIES
    )
    due_at = schedule.next_due_at

    dispatched = await dispatch_due_conversation_syncs(unit_of_work_factory, now=due_at, limit=1)

    assert len(dispatched) == 1
    command = dispatched[0]
    due_identity = due_at.strftime("%Y%m%dT%H%M%S.%fZ")
    assert command.command_id == f"conversation-sync:{schedule.id}:{due_identity}"
    assert command.correlation_id == f"conversation-sync-correlation:{schedule.id}:{due_identity}"
    assert command.command_type == "threads.sync_conversation"
    assert command.account_id == account_id
    assert command.payload == {
        "threads_post_id": root.threads_post_id,
        "sync_kind": "replies",
    }
    assert command.priority == 0
    assert command.deadline_at is None

    stored_schedule = await _get_schedule(unit_of_work_factory, schedule.id)
    async with unit_of_work_factory() as unit_of_work:
        dispatches = await unit_of_work.conversation_sync_dispatches.list_for_schedule(schedule.id)
    assert len(dispatches) == 1
    assert dispatches[0].schedule_revision == 1
    assert dispatches[0].due_at == due_at
    assert dispatches[0].command_id == command.command_id
    assert stored_schedule.last_dispatched_due_at == due_at
    assert stored_schedule.last_command_id == command.command_id
    assert stored_schedule.next_due_at == due_at + timedelta(hours=1)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ConversationSyncScheduleRecord)
            .where(ConversationSyncScheduleRecord.id == schedule.id)
        )
        == 1
    )


async def test_new_dispatcher_instance_resumes_from_persisted_schedule_cursor(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    _, _, schedule = await _create_schedule(unit_of_work_factory)
    first = await dispatch_due_conversation_syncs(
        unit_of_work_factory, now=schedule.next_due_at, limit=1
    )
    assert len(first) == 1
    restarted_at = schedule.next_due_at + timedelta(hours=3, minutes=30)
    await _terminalize_command(
        unit_of_work_factory, first[0].command_id, CommandStatus.SUCCEEDED, restarted_at
    )

    restarted_engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    restarted_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(restarted_engine))
    try:
        after_restart = await dispatch_due_conversation_syncs(
            restarted_factory, now=restarted_at, limit=1
        )
        persisted = await _get_schedule(restarted_factory, schedule.id)
        async with restarted_factory() as unit_of_work:
            dispatches = await unit_of_work.conversation_sync_dispatches.list_for_schedule(
                schedule.id
            )
    finally:
        await restarted_engine.dispose()

    assert len(after_restart) == 1
    assert after_restart[0].command_id != first[0].command_id
    assert [item.due_at for item in dispatches] == [
        schedule.next_due_at,
        schedule.next_due_at + timedelta(hours=1),
    ]
    assert persisted.next_due_at == schedule.next_due_at + timedelta(hours=4)


async def test_future_paused_and_disabled_schedules_do_not_dispatch_or_advance(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id, root, future = await _create_schedule(unit_of_work_factory)
    future_attempt = await dispatch_due_conversation_syncs(
        unit_of_work_factory, now=future.next_due_at - timedelta(microseconds=1), limit=1
    )
    assert future_attempt == []
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.conversation_sync_schedules.get_for_update(future.id)
        assert stored is not None
        stored.transition(
            ConversationSyncScheduleStatus.DISABLED,
            _ANCHOR + timedelta(minutes=1),
            reason="replace test schedule",
        )
        await unit_of_work.conversation_sync_schedules.update(stored)

    _, _, paused = await _create_schedule(unit_of_work_factory)
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.conversation_sync_schedules.get_for_update(paused.id)
        assert stored is not None
        stored.transition(
            ConversationSyncScheduleStatus.PAUSED,
            _ANCHOR + timedelta(minutes=1),
            reason="operator pause",
        )
        await unit_of_work.conversation_sync_schedules.update(stored)

    paused_attempt = await dispatch_due_conversation_syncs(
        unit_of_work_factory, now=future.next_due_at + timedelta(hours=4), limit=1
    )
    assert paused_attempt == []
    assert (await _get_schedule(unit_of_work_factory, paused.id)).next_due_at == paused.next_due_at

    _, _, disabled = await _create_schedule(unit_of_work_factory)
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.conversation_sync_schedules.get_for_update(disabled.id)
        assert stored is not None
        stored.transition(
            ConversationSyncScheduleStatus.DISABLED,
            _ANCHOR + timedelta(minutes=1),
            reason="operator disabled",
        )
        await unit_of_work.conversation_sync_schedules.update(stored)

    disabled_attempt = await dispatch_due_conversation_syncs(
        unit_of_work_factory, now=future.next_due_at + timedelta(hours=4), limit=1
    )
    assert disabled_attempt == []
    assert (await _get_schedule(unit_of_work_factory, disabled.id)).next_due_at == (
        disabled.next_due_at
    )
    assert (await _get_schedule(unit_of_work_factory, future.id)).account_id == account_id
    assert (await _get_schedule(unit_of_work_factory, future.id)).threads_post_id == (
        root.threads_post_id
    )


async def test_resume_coalesces_pause_backlog_to_one_dispatch(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    _, _, schedule = await _create_schedule(unit_of_work_factory)
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.conversation_sync_schedules.get_for_update(schedule.id)
        assert stored is not None
        stored.transition(
            ConversationSyncScheduleStatus.PAUSED,
            schedule.created_at + timedelta(minutes=1),
            reason="operator pause",
        )
        await unit_of_work.conversation_sync_schedules.update(stored)

    resumed_at = _ANCHOR + timedelta(hours=4, minutes=30)
    assert (
        await dispatch_due_conversation_syncs(unit_of_work_factory, now=resumed_at, limit=1) == []
    )
    paused_schedule = await _get_schedule(unit_of_work_factory, schedule.id)
    assert paused_schedule.next_due_at == schedule.next_due_at

    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.conversation_sync_schedules.get_for_update(schedule.id)
        assert stored is not None
        stored.transition(
            ConversationSyncScheduleStatus.ACTIVE,
            resumed_at,
            reason="resume",
        )
        await unit_of_work.conversation_sync_schedules.update(stored)

    dispatched = await dispatch_due_conversation_syncs(
        unit_of_work_factory, now=resumed_at, limit=1
    )
    assert len(dispatched) == 1
    async with unit_of_work_factory() as unit_of_work:
        audit = await unit_of_work.conversation_sync_dispatches.list_for_schedule(schedule.id)
    assert len(audit) == 1
    assert audit[0].due_at == schedule.next_due_at
    resumed = await _get_schedule(unit_of_work_factory, schedule.id)
    assert resumed.status is ConversationSyncScheduleStatus.ACTIVE
    assert resumed.revision == 3
    assert resumed.next_due_at == _ANCHOR + timedelta(hours=5)


async def test_missing_local_root_does_not_create_or_advance_schedule(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    _, _, schedule = await _create_schedule(
        unit_of_work_factory, threads_post_id="missing-local-root"
    )

    dispatched = await dispatch_due_conversation_syncs(
        unit_of_work_factory, now=schedule.next_due_at, limit=1
    )

    assert dispatched == []
    assert (await _get_schedule(unit_of_work_factory, schedule.id)).next_due_at == (
        schedule.next_due_at
    )
    assert await db_session.scalar(select(func.count()).select_from(CommandRecord)) == 0
    assert (
        await db_session.scalar(select(func.count()).select_from(ConversationSyncDispatchRecord))
        == 0
    )


async def test_nonterminal_schedule_does_not_starve_later_due_schedule(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    _, _, blocked_schedule = await _create_schedule(unit_of_work_factory)
    previous = await dispatch_due_conversation_syncs(
        unit_of_work_factory, now=blocked_schedule.next_due_at, limit=1
    )
    assert len(previous) == 1

    _, _, later_schedule = await _create_schedule(
        unit_of_work_factory,
        anchor_at=blocked_schedule.next_due_at + timedelta(minutes=5),
    )
    blocked_due = blocked_schedule.next_due_at + timedelta(hours=1)
    later_due = later_schedule.next_due_at
    assert blocked_due < later_due
    now = later_due + timedelta(minutes=30)

    dispatched = await dispatch_due_conversation_syncs(unit_of_work_factory, now=now, limit=1)

    assert len(dispatched) == 1
    assert dispatched[0].account_id == later_schedule.account_id
    blocked_after = await _get_schedule(unit_of_work_factory, blocked_schedule.id)
    later_after = await _get_schedule(unit_of_work_factory, later_schedule.id)
    assert blocked_after.next_due_at == blocked_due
    assert blocked_after.status is ConversationSyncScheduleStatus.ACTIVE
    assert blocked_after.last_dispatched_due_at == blocked_schedule.next_due_at
    assert blocked_after.last_command_id == previous[0].command_id
    assert later_after.next_due_at == later_due + timedelta(hours=1)
    assert await _counts_for_schedule(unit_of_work_factory, blocked_schedule, db_session) == (
        1,
        1,
        1,
    )
    assert await _counts_for_schedule(unit_of_work_factory, later_schedule, db_session) == (
        1,
        1,
        1,
    )

    await _terminalize_command(
        unit_of_work_factory, previous[0].command_id, CommandStatus.SUCCEEDED, now
    )
    resumed = await dispatch_due_conversation_syncs(unit_of_work_factory, now=now, limit=1)

    assert len(resumed) == 1
    assert resumed[0].account_id == blocked_schedule.account_id
    assert (await _get_schedule(unit_of_work_factory, blocked_schedule.id)).next_due_at == (
        blocked_due + timedelta(hours=1)
    )
    assert await _counts_for_schedule(unit_of_work_factory, blocked_schedule, db_session) == (
        2,
        2,
        2,
    )


async def test_missing_root_schedule_does_not_starve_later_due_schedule(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, _, missing_root_schedule = await _create_schedule(
        unit_of_work_factory, threads_post_id="missing-local-root"
    )
    _, _, later_schedule = await _create_schedule(
        unit_of_work_factory,
        anchor_at=missing_root_schedule.next_due_at + timedelta(minutes=5),
    )
    missing_root_due = missing_root_schedule.next_due_at
    later_due = later_schedule.next_due_at
    assert missing_root_due < later_due
    now = later_due + timedelta(minutes=30)

    dispatched = await dispatch_due_conversation_syncs(unit_of_work_factory, now=now, limit=1)

    assert len(dispatched) == 1
    assert dispatched[0].account_id == later_schedule.account_id
    missing_after = await _get_schedule(unit_of_work_factory, missing_root_schedule.id)
    later_after = await _get_schedule(unit_of_work_factory, later_schedule.id)
    assert missing_after.next_due_at == missing_root_due
    assert missing_after.status is ConversationSyncScheduleStatus.ACTIVE
    assert missing_after.last_dispatched_due_at is None
    assert missing_after.last_command_id is None
    assert later_after.next_due_at == later_due + timedelta(hours=1)
    assert await _counts_for_schedule(unit_of_work_factory, missing_root_schedule, db_session) == (
        0,
        0,
        0,
    )
    assert await _counts_for_schedule(unit_of_work_factory, later_schedule, db_session) == (
        1,
        1,
        1,
    )

    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.posts.add(
            ThreadPost(account_id=account_id, threads_post_id="missing-local-root")
        )
    resumed = await dispatch_due_conversation_syncs(unit_of_work_factory, now=now, limit=1)

    assert len(resumed) == 1
    assert resumed[0].account_id == missing_root_schedule.account_id
    assert (await _get_schedule(unit_of_work_factory, missing_root_schedule.id)).next_due_at == (
        missing_root_due + timedelta(hours=1)
    )
    assert await _counts_for_schedule(unit_of_work_factory, missing_root_schedule, db_session) == (
        1,
        1,
        1,
    )


async def test_partial_unique_index_allows_history_only_for_disabled_schedules(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    first = _new_schedule(account_id, root.threads_post_id)
    second = _new_schedule(account_id, root.threads_post_id)
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.conversation_sync_schedules.add(first)
    with pytest.raises(IntegrityError):
        async with unit_of_work_factory() as unit_of_work:
            await unit_of_work.conversation_sync_schedules.add(second)

    disabled_at = _ANCHOR + timedelta(minutes=1)
    async with unit_of_work_factory() as unit_of_work:
        stored_first = await unit_of_work.conversation_sync_schedules.get_for_update(first.id)
        assert stored_first is not None
        stored_first.transition(
            ConversationSyncScheduleStatus.DISABLED,
            disabled_at,
            reason="replace cadence",
        )
        await unit_of_work.conversation_sync_schedules.update(stored_first)
    second.transition(
        ConversationSyncScheduleStatus.DISABLED,
        disabled_at,
        reason="historical duplicate",
    )
    third = _new_schedule(account_id, root.threads_post_id)
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.conversation_sync_schedules.add(second)
        await unit_of_work.conversation_sync_schedules.add(third)
    schedules = await _get_schedules(unit_of_work_factory, account_id)
    assert len(schedules) == 3


async def _get_schedules(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory, account_id: UUID
) -> list[ConversationSyncSchedule]:
    async with unit_of_work_factory() as unit_of_work:
        return await unit_of_work.conversation_sync_schedules.list_for_account(account_id)


async def test_nonterminal_command_keeps_schedule_due_until_terminal(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    _, _, schedule = await _create_schedule(unit_of_work_factory)
    first_due = schedule.next_due_at
    first = await dispatch_due_conversation_syncs(unit_of_work_factory, now=first_due, limit=1)
    assert len(first) == 1

    now = _ANCHOR + timedelta(hours=4, minutes=30)
    blocked = await dispatch_due_conversation_syncs(unit_of_work_factory, now=now, limit=1)
    assert blocked == []
    assert (await _get_schedule(unit_of_work_factory, schedule.id)).next_due_at == (
        first_due + timedelta(hours=1)
    )

    await _terminalize_command(
        unit_of_work_factory, first[0].command_id, CommandStatus.SUCCEEDED, now
    )
    resumed = await dispatch_due_conversation_syncs(unit_of_work_factory, now=now, limit=1)
    assert len(resumed) == 1
    assert resumed[0].command_id != first[0].command_id
    updated = await _get_schedule(unit_of_work_factory, schedule.id)
    assert updated.last_dispatched_due_at == first_due + timedelta(hours=1)
    assert updated.next_due_at == _ANCHOR + timedelta(hours=5)


@pytest.mark.parametrize(
    "terminal_status",
    [
        CommandStatus.SUCCEEDED,
        CommandStatus.REJECTED,
        CommandStatus.EXPIRED,
        CommandStatus.FAILED_FINAL,
        CommandStatus.CANCELLED,
    ],
)
async def test_each_existing_terminal_status_releases_outstanding_gate(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    terminal_status: CommandStatus,
) -> None:
    _, _, schedule = await _create_schedule(unit_of_work_factory)
    first = await dispatch_due_conversation_syncs(
        unit_of_work_factory, now=schedule.next_due_at, limit=1
    )
    assert len(first) == 1
    due_again = schedule.next_due_at + timedelta(hours=1)
    await _terminalize_command(
        unit_of_work_factory, first[0].command_id, terminal_status, due_again
    )

    later = await dispatch_due_conversation_syncs(unit_of_work_factory, now=due_again, limit=1)

    assert len(later) == 1
    assert later[0].command_id != first[0].command_id


@pytest.mark.parametrize("failure_point", ["after_command", "after_dispatch"])
async def test_command_dispatch_and_cursor_roll_back_as_one_transaction(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    _, _, schedule = await _create_schedule(unit_of_work_factory)
    if failure_point == "after_command":
        original = SQLAlchemyCommandRepository.add_if_absent

        async def fail_after_command(
            repository: SQLAlchemyCommandRepository, command: Command
        ) -> bool:
            await original(repository, command)
            raise RuntimeError("simulated failure after Command insert")

        monkeypatch.setattr(SQLAlchemyCommandRepository, "add_if_absent", fail_after_command)
        failure_text = "after Command insert"
    else:
        original_dispatch_add = SQLAlchemyConversationSyncDispatchRepository.add_if_absent

        async def fail_after_dispatch(
            repository: SQLAlchemyConversationSyncDispatchRepository,
            dispatch: ConversationSyncDispatch,
        ) -> ConversationSyncDispatch:
            await original_dispatch_add(repository, dispatch)
            raise RuntimeError("simulated failure after dispatch insert")

        monkeypatch.setattr(
            SQLAlchemyConversationSyncDispatchRepository,
            "add_if_absent",
            fail_after_dispatch,
        )
        failure_text = "after dispatch insert"

    with pytest.raises(RuntimeError, match=failure_text):
        await dispatch_due_conversation_syncs(
            unit_of_work_factory, now=schedule.next_due_at, limit=1
        )

    assert (await _get_schedule(unit_of_work_factory, schedule.id)).next_due_at == (
        schedule.next_due_at
    )
    async with unit_of_work_factory() as unit_of_work:
        dispatches = await unit_of_work.conversation_sync_dispatches.list_for_schedule(schedule.id)
    assert dispatches == []
    async with unit_of_work_factory() as unit_of_work:
        command = await unit_of_work.commands.get_by_command_id(
            _command_id(schedule.id, schedule.next_due_at)
        )
    assert command is None


def _command_id(schedule_id: UUID, due_at: datetime) -> str:
    return f"conversation-sync:{schedule_id}:{due_at.strftime('%Y%m%dT%H%M%S.%fZ')}"


async def test_clean_migration_downgrade_and_history_refusal(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    config = Config("alembic.ini")
    await asyncio.to_thread(command.downgrade, config, "20260929_0015")
    await asyncio.to_thread(command.upgrade, config, "head")

    _, _, schedule = await _create_schedule(unit_of_work_factory)
    with pytest.raises(RuntimeError, match="CONVERSATION_SYNC_SCHEDULING_DOWNGRADE_BLOCKED"):
        await asyncio.to_thread(command.downgrade, config, "20260929_0015")
    await asyncio.to_thread(command.upgrade, config, "head")
    assert (await _get_schedule(unit_of_work_factory, schedule.id)).id == schedule.id


async def _create_api_only_schedule(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    *,
    anchor_at: datetime,
    interval_seconds: int = 900,
) -> tuple[UUID, ThreadPost, ConversationSyncSchedule]:
    account = ThreadsAccount(
        threads_user_id=f"conversation-sync-user-{uuid4()}",
        username="conversation_sync_scheduler_test",
        execution_mode=AccountExecutionMode.API_ONLY,
    )
    root = ThreadPost(
        account_id=account.id,
        threads_post_id=f"root-{uuid4()}",
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.posts.add(root)
    schedule = _new_schedule(
        account.id,
        root.threads_post_id,
        anchor_at=anchor_at,
        interval_seconds=interval_seconds,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.conversation_sync_schedules.add(schedule)
    return account.id, root, schedule


def _independent_uow_factories() -> tuple[
    SQLAlchemyUnitOfWorkFactory,
    SQLAlchemyUnitOfWorkFactory,
    AsyncEngine,
    AsyncEngine,
]:
    database_url = os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"]
    first_engine = create_database_engine(database_url)
    second_engine = create_database_engine(database_url)
    return (
        SQLAlchemyUnitOfWorkFactory(create_session_factory(first_engine)),
        SQLAlchemyUnitOfWorkFactory(create_session_factory(second_engine)),
        first_engine,
        second_engine,
    )


async def _counts_for_schedule(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    schedule: ConversationSyncSchedule,
    db_session: AsyncSession,
) -> tuple[int, int, int]:
    async with unit_of_work_factory() as unit_of_work:
        dispatches = await unit_of_work.conversation_sync_dispatches.list_for_schedule(schedule.id)
    command_count = await db_session.scalar(
        select(func.count())
        .select_from(CommandRecord)
        .where(CommandRecord.account_id == schedule.account_id)
    )
    return (
        len(dispatches),
        command_count or 0,
        len({item.command_id for item in dispatches}),
    )


async def test_concurrent_schedulers_dispatch_one_due_schedule_once(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, schedule = await _create_schedule(unit_of_work_factory)
    first_factory, second_factory, first_engine, second_engine = _independent_uow_factories()
    selected = asyncio.Event()
    release = asyncio.Event()
    original = SQLAlchemyConversationSyncScheduleRepository.get_due_for_update
    calls = 0
    calls_lock = asyncio.Lock()

    async def hold_first_selection(
        repository: SQLAlchemyConversationSyncScheduleRepository,
        now: datetime,
        limit: int,
    ) -> list[ConversationSyncSchedule]:
        nonlocal calls
        result = await original(repository, now, limit)
        async with calls_lock:
            calls += 1
            is_first = calls == 1
        if is_first:
            assert [item.id for item in result] == [schedule.id]
            selected.set()
            await release.wait()
        return result

    monkeypatch.setattr(
        SQLAlchemyConversationSyncScheduleRepository,
        "get_due_for_update",
        hold_first_selection,
    )
    first_task = asyncio.create_task(
        dispatch_due_conversation_syncs(first_factory, now=schedule.next_due_at, limit=1)
    )
    try:
        await selected.wait()
        second = await dispatch_due_conversation_syncs(
            second_factory, now=schedule.next_due_at, limit=1
        )
    finally:
        release.set()
    first = await first_task

    assert len(first) == 1
    assert second == []
    stored = await _get_schedule(unit_of_work_factory, schedule.id)
    assert stored.next_due_at == schedule.next_due_at + timedelta(hours=1)
    assert await _counts_for_schedule(unit_of_work_factory, schedule, db_session) == (1, 1, 1)
    await first_engine.dispose()
    await second_engine.dispose()


async def test_distinct_due_schedules_can_be_split_between_scheduler_instances(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, first_schedule = await _create_schedule(unit_of_work_factory)
    _, _, second_schedule = await _create_schedule(
        unit_of_work_factory,
        anchor_at=_ANCHOR + timedelta(hours=1),
    )
    first_factory, second_factory, first_engine, second_engine = _independent_uow_factories()
    selected = asyncio.Event()
    release = asyncio.Event()
    original = SQLAlchemyConversationSyncScheduleRepository.get_due_for_update
    calls = 0
    calls_lock = asyncio.Lock()

    async def hold_first_selection(
        repository: SQLAlchemyConversationSyncScheduleRepository,
        now: datetime,
        limit: int,
    ) -> list[ConversationSyncSchedule]:
        nonlocal calls
        result = await original(repository, now, limit)
        async with calls_lock:
            calls += 1
            is_first = calls == 1
        if is_first:
            assert [item.id for item in result] == [first_schedule.id]
            selected.set()
            await release.wait()
        return result

    monkeypatch.setattr(
        SQLAlchemyConversationSyncScheduleRepository,
        "get_due_for_update",
        hold_first_selection,
    )
    now = second_schedule.next_due_at
    first_task = asyncio.create_task(
        dispatch_due_conversation_syncs(first_factory, now=now, limit=1)
    )
    try:
        await selected.wait()
        second = await dispatch_due_conversation_syncs(second_factory, now=now, limit=1)
    finally:
        release.set()
    first = await first_task

    assert len(first) == len(second) == 1
    assert first[0].payload["threads_post_id"] == first_schedule.threads_post_id
    assert second[0].payload["threads_post_id"] == second_schedule.threads_post_id
    assert await _counts_for_schedule(unit_of_work_factory, first_schedule, db_session) == (
        1,
        1,
        1,
    )
    assert await _counts_for_schedule(unit_of_work_factory, second_schedule, db_session) == (
        1,
        1,
        1,
    )
    await first_engine.dispose()
    await second_engine.dispose()


async def test_outstanding_command_race_stays_due_until_terminal(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    _, _, schedule = await _create_schedule(unit_of_work_factory)
    first = await dispatch_due_conversation_syncs(
        unit_of_work_factory, now=schedule.next_due_at, limit=1
    )
    assert len(first) == 1
    first_due_after = schedule.next_due_at + timedelta(hours=1)
    first_factory, second_factory, first_engine, second_engine = _independent_uow_factories()
    now = first_due_after + timedelta(hours=3, minutes=30)
    both_ready = asyncio.Event()
    ready_count = 0
    ready_lock = asyncio.Lock()

    async def race_dispatch(factory: SQLAlchemyUnitOfWorkFactory) -> list[Command]:
        nonlocal ready_count
        async with ready_lock:
            ready_count += 1
            if ready_count == 2:
                both_ready.set()
        await both_ready.wait()
        return await dispatch_due_conversation_syncs(factory, now=now, limit=1)

    try:
        first_race, second = await asyncio.gather(
            race_dispatch(first_factory), race_dispatch(second_factory)
        )
    finally:
        await first_engine.dispose()
        await second_engine.dispose()

    assert first_race == second == []
    assert (await _get_schedule(unit_of_work_factory, schedule.id)).next_due_at == (first_due_after)
    assert await _counts_for_schedule(unit_of_work_factory, schedule, db_session) == (1, 1, 1)

    await _terminalize_command(
        unit_of_work_factory, first[0].command_id, CommandStatus.SUCCEEDED, now
    )
    resumed = await dispatch_due_conversation_syncs(unit_of_work_factory, now=now, limit=1)
    assert len(resumed) == 1
    assert resumed[0].command_id != first[0].command_id
    updated = await _get_schedule(unit_of_work_factory, schedule.id)
    assert updated.last_dispatched_due_at == first_due_after
    assert updated.next_due_at == _ANCHOR + timedelta(hours=6)
    assert await _counts_for_schedule(unit_of_work_factory, schedule, db_session) == (2, 2, 2)


async def test_dispatch_racing_command_processing_has_one_command_and_result(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _ANCHOR + timedelta(hours=1)
    account_id, root, schedule = await _create_api_only_schedule(
        unit_of_work_factory, anchor_at=_ANCHOR
    )
    clock = FixedClock()
    clock.current_time = now
    worker_jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    api = FakeThreadsAPI()
    api.page_responses = [ReplyPage((), "cursor-1", has_more=False)]
    composition = compose_command_runtime(
        unit_of_work_factory,
        worker_jobs,
        threads_api_gateway=api,
        threads_access_token_provider=TokenProvider(),
        clock=clock,
    )
    inserted = asyncio.Event()
    release = asyncio.Event()
    original = SQLAlchemyCommandRepository.add_if_absent

    async def pause_after_uncommitted_insert(
        repository: SQLAlchemyCommandRepository, command_value: Command
    ) -> bool:
        result = await original(repository, command_value)
        if command_value.command_type == "threads.sync_conversation":
            inserted.set()
            await release.wait()
        return result

    monkeypatch.setattr(
        SQLAlchemyCommandRepository, "add_if_absent", pause_after_uncommitted_insert
    )
    dispatch_task = asyncio.create_task(
        dispatch_due_conversation_syncs(unit_of_work_factory, now=now, limit=1)
    )
    await inserted.wait()
    raced_processing = await composition.command_runtime.process_next(now=now)
    assert raced_processing is None
    release.set()
    dispatched = await dispatch_task
    assert len(dispatched) == 1

    result = await composition.command_runtime.process_next(now=now)
    assert result is not None and result.status is CommandStatus.SUCCEEDED
    assert result.command_id == dispatched[0].command_id
    decisions = list(
        (
            await db_session.scalars(
                select(CommandRouteDecisionRecord).where(
                    CommandRouteDecisionRecord.command_id == result.command_id
                )
            )
        ).all()
    )
    attempts = list(
        (
            await db_session.scalars(
                select(CommandAttemptRecord).where(
                    CommandAttemptRecord.command_id == result.command_id
                )
            )
        ).all()
    )
    outbox_count = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == result.command_id)
    )
    assert api.page_after_values == [None]
    assert len(decisions) == 1 and decisions[0].target is RouteTarget.LOCAL_API
    assert len(attempts) == 1
    assert outbox_count == 1
    assert await _counts_for_schedule(unit_of_work_factory, schedule, db_session) == (1, 1, 1)
    assert account_id == schedule.account_id and root.threads_post_id == schedule.threads_post_id


async def test_scheduler_sync_dispatch_reuses_syncstate_cursor_after_coalescing(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, root, schedule = await _create_api_only_schedule(
        unit_of_work_factory, anchor_at=_ANCHOR
    )
    runtime_clock = FixedClock()
    runtime_clock.current_time = schedule.next_due_at
    worker_jobs = WorkerJobService(unit_of_work_factory, clock=runtime_clock)
    api = FakeThreadsAPI()
    api.page_responses = [
        ReplyPage((), "remote-cursor-one", has_more=False),
        ReplyPage((), "remote-cursor-two", has_more=False),
    ]
    composition = compose_command_runtime(
        unit_of_work_factory,
        worker_jobs,
        threads_api_gateway=api,
        threads_access_token_provider=TokenProvider(),
        clock=runtime_clock,
    )

    first_tick = await run_scheduler_tick(
        unit_of_work_factory,
        composition.command_runtime,
        worker_jobs,
        now=schedule.next_due_at,
        generation_limit=1,
        conversation_sync_limit=1,
        activity_limit=1,
        command_limit=2,
        recovery_limit=1,
    )
    catch_up_now = schedule.next_due_at + timedelta(hours=4, minutes=30)
    runtime_clock.current_time = catch_up_now
    second_tick = await run_scheduler_tick(
        unit_of_work_factory,
        composition.command_runtime,
        worker_jobs,
        now=catch_up_now,
        generation_limit=1,
        conversation_sync_limit=1,
        activity_limit=1,
        command_limit=2,
        recovery_limit=1,
    )

    async with unit_of_work_factory() as unit_of_work:
        dispatches = await unit_of_work.conversation_sync_dispatches.list_for_schedule(schedule.id)
        commands: list[Command] = []
        for dispatch in dispatches:
            stored_command = await unit_of_work.commands.get_by_command_id(dispatch.command_id)
            assert stored_command is not None
            commands.append(stored_command)
    sync_state = await db_session.scalar(
        select(SyncStateRecord).where(
            SyncStateRecord.account_id == account_id,
            SyncStateRecord.sync_type == f"conversation:{root.threads_post_id}",
        )
    )
    outbox_count = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id.in_([item.command_id for item in dispatches]))
    )
    assert first_tick.conversation_syncs_dispatched == 1
    assert first_tick.commands_processed == 1
    assert second_tick.conversation_syncs_dispatched == 1
    assert second_tick.commands_processed == 1
    assert [item.due_at for item in dispatches] == [
        schedule.next_due_at,
        schedule.next_due_at + timedelta(minutes=15),
    ]
    assert api.page_after_values == [None, "remote-cursor-one"]
    assert sync_state is not None and sync_state.cursor == "remote-cursor-two"
    assert len(commands) == len(dispatches) == 2
    assert all(item.status is CommandStatus.SUCCEEDED for item in commands)
    assert outbox_count == 2
    stored = await _get_schedule(unit_of_work_factory, schedule.id)
    assert stored.next_due_at == schedule.next_due_at + timedelta(hours=4, minutes=45)
