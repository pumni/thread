from datetime import datetime
from uuid import UUID

import structlog

from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.conversation_sync import (
    ConversationSyncDispatch,
)
from threads_platform.domain.time import normalize_utc

MAX_CONVERSATION_SYNC_DISPATCH_BATCH = 100
_TERMINAL_COMMAND_STATUSES = frozenset(
    {
        CommandStatus.SUCCEEDED,
        CommandStatus.REJECTED,
        CommandStatus.EXPIRED,
        CommandStatus.FAILED_FINAL,
        CommandStatus.CANCELLED,
    }
)


async def dispatch_due_conversation_syncs(
    unit_of_work_factory: UnitOfWorkFactory,
    *,
    now: datetime,
    limit: int,
) -> list[Command]:
    """Atomically dispatch a bounded set of due conversation sync schedules."""
    if type(limit) is not int or not 1 <= limit <= MAX_CONVERSATION_SYNC_DISPATCH_BATCH:
        raise ValueError(
            "conversation sync dispatch limit must be between 1 and "
            f"{MAX_CONVERSATION_SYNC_DISPATCH_BATCH}"
        )
    occurred_at = normalize_utc(now)
    logger = structlog.get_logger(__name__)
    dispatched: list[Command] = []

    async with unit_of_work_factory() as unit_of_work:
        schedules = await unit_of_work.conversation_sync_schedules.get_due_for_update(
            occurred_at, limit
        )
        for schedule in schedules:
            latest_dispatch = (
                await unit_of_work.conversation_sync_dispatches.get_latest_for_schedule(schedule.id)
            )
            if latest_dispatch is not None:
                previous_command = await unit_of_work.commands.get_by_command_id(
                    latest_dispatch.command_id
                )
                if previous_command is None:
                    raise RuntimeError("conversation sync dispatch references a missing Command")
                if previous_command.status not in _TERMINAL_COMMAND_STATUSES:
                    continue

            root_post = await unit_of_work.posts.get_by_external_id(
                schedule.account_id, schedule.threads_post_id
            )
            if root_post is None:
                logger.warning(
                    "conversation_sync_schedule_root_missing",
                    schedule_id=str(schedule.id),
                    error_code="LOCAL_ROOT_POST_NOT_FOUND",
                )
                continue

            due_at = schedule.next_due_at
            command_id = _deterministic_command_id(schedule.id, due_at)
            correlation_id = _deterministic_correlation_id(schedule.id, due_at)
            expected_command = Command(
                command_id=command_id,
                correlation_id=correlation_id,
                account_id=schedule.account_id,
                command_type="threads.sync_conversation",
                payload={
                    "threads_post_id": schedule.threads_post_id,
                    "sync_kind": schedule.sync_kind.value,
                },
                created_at=occurred_at,
                received_at=occurred_at,
                priority=0,
            )
            inserted = await unit_of_work.commands.add_if_absent(expected_command)
            if inserted:
                command = expected_command
            else:
                command = await unit_of_work.commands.get_by_command_id(command_id)
                if command is None or not _matches_expected_command(command, expected_command):
                    raise RuntimeError("conversation sync deterministic Command identity conflicts")

            dispatch = schedule.dispatch(command_id, occurred_at)
            stored_dispatch = await unit_of_work.conversation_sync_dispatches.add_if_absent(
                dispatch
            )
            if not _matches_expected_dispatch(stored_dispatch, dispatch):
                raise RuntimeError("conversation sync deterministic dispatch identity conflicts")
            await unit_of_work.conversation_sync_schedules.update(schedule)
            dispatched.append(command)

    return dispatched


def _deterministic_command_id(schedule_id: UUID, due_at: datetime) -> str:
    due_identity = normalize_utc(due_at).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"conversation-sync:{schedule_id}:{due_identity}"


def _deterministic_correlation_id(schedule_id: UUID, due_at: datetime) -> str:
    due_identity = normalize_utc(due_at).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"conversation-sync-correlation:{schedule_id}:{due_identity}"


def _matches_expected_command(actual: Command, expected: Command) -> bool:
    return (
        actual.command_id == expected.command_id
        and actual.correlation_id == expected.correlation_id
        and actual.account_id == expected.account_id
        and actual.command_type == expected.command_type
        and actual.payload == expected.payload
        and actual.priority == 0
        and actual.deadline_at is None
    )


def _matches_expected_dispatch(
    actual: ConversationSyncDispatch, expected: ConversationSyncDispatch
) -> bool:
    return (
        actual.schedule_id == expected.schedule_id
        and actual.schedule_revision == expected.schedule_revision
        and actual.due_at == expected.due_at
        and actual.command_id == expected.command_id
    )
