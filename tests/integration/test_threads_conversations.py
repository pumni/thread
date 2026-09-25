import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.threads_test_support import (
    FakeThreadsAPI,
    FixedClock,
    command_body,
    make_runtime,
    remote_reply,
    seed_account_and_post,
)
from threads_platform.application.ports.threads import (
    ReplyPage,
    ThreadsAPIError,
    ThreadsTransportError,
)
from threads_platform.domain.commands import CommandStatus
from threads_platform.domain.publishing import ThreadReply
from threads_platform.domain.sync import SyncState
from threads_platform.infrastructure.persistence.models import (
    ReplyRecord,
    SyncStateRecord,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


async def test_conversation_pagination_cursor_resume_nested_mapping_and_dedupe(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    first = remote_reply("reply-a", root.threads_post_id, root.threads_post_id)
    second = remote_reply("reply-b", root.threads_post_id, "reply-a")
    third = remote_reply("reply-c", root.threads_post_id, "reply-b")
    api.page_responses = [
        ReplyPage((first,), "cursor-1", has_more=True),
        ReplyPage((first, second), "cursor-2", has_more=False),
        ReplyPage((third,), "cursor-3", has_more=False),
        ReplyPage((third,), "cursor-3", has_more=False),
    ]
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)

    first_receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )
    first_run = await runtime.process(first_receipt.command_id)
    second_receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )
    second_run = await runtime.process(second_receipt.command_id)
    third_receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )
    third_run = await runtime.process(third_receipt.command_id)
    records = list(
        await db_session.scalars(select(ReplyRecord).order_by(ReplyRecord.threads_reply_id))
    )
    sync_state = await db_session.scalar(
        select(SyncStateRecord).where(SyncStateRecord.account_id == account_id)
    )

    assert first_run.status is CommandStatus.SUCCEEDED
    assert second_run.status is CommandStatus.SUCCEEDED
    assert third_run.status is CommandStatus.SUCCEEDED
    assert api.page_after_values == [None, "cursor-1", "cursor-2", "cursor-3"]
    assert len(records) == 3
    by_remote_id = {record.threads_reply_id: record for record in records}
    assert by_remote_id["reply-a"].root_post_id == root.id
    assert by_remote_id["reply-b"].parent_reply_id == by_remote_id["reply-a"].id
    assert by_remote_id["reply-c"].parent_reply_id == by_remote_id["reply-b"].id
    assert sync_state is not None
    assert sync_state.cursor == "cursor-3"


async def test_concurrent_sync_commands_cannot_regress_persisted_cursor(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    sync_type = f"conversation:{root.threads_post_id}"
    async with unit_of_work_factory() as unit_of_work:
        inserted = await unit_of_work.sync_states.advance_if_current(
            SyncState(account_id=account_id, sync_type=sync_type, cursor="base-cursor"), None
        )
        assert inserted
    api = FakeThreadsAPI()
    api.conversation_release = asyncio.Event()
    reply = remote_reply("concurrent-reply", root.threads_post_id, root.threads_post_id)
    api.page_responses = [
        ReplyPage((reply,), "cursor-one", has_more=False),
        ReplyPage((reply,), "cursor-two", has_more=False),
        ReplyPage((reply,), "cursor-two", has_more=False),
    ]
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    first = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )
    second = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )
    first_task = asyncio.create_task(runtime.process(first.command_id))
    second_task = asyncio.create_task(runtime.process(second.command_id))
    await api.conversation_ready.wait()
    api.conversation_release.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)
    retryable_id = next(
        command_id
        for command_id, result in (
            (first.command_id, first_result),
            (second.command_id, second_result),
        )
        if result.status is CommandStatus.FAILED_RETRYABLE
    )
    clock.advance(timedelta(seconds=1))
    recovered = await runtime.process(retryable_id)
    sync_state = await db_session.scalar(
        select(SyncStateRecord).where(
            SyncStateRecord.account_id == account_id,
            SyncStateRecord.sync_type == sync_type,
        )
    )
    reply_count = await db_session.scalar(select(func.count()).select_from(ReplyRecord))

    assert {first_result.status, second_result.status} == {
        CommandStatus.SUCCEEDED,
        CommandStatus.FAILED_RETRYABLE,
    }
    assert recovered.status is CommandStatus.SUCCEEDED
    assert sync_state is not None
    assert sync_state.cursor == "cursor-two"
    assert reply_count == 1


async def test_sync_replies_edge_missing_remote_root_and_moderation_contract(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    root_id = root.threads_post_id
    api = FakeThreadsAPI()
    api.reply_pages = [ReplyPage((remote_reply("top-reply", root_id, root_id),), None, False)]
    api.page_responses = []
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    replies_receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root_id, "sync_kind": "replies"},
        )
    )
    replies_result = await runtime.process(replies_receipt.command_id)

    assert replies_result.status is CommandStatus.SUCCEEDED
    assert api.page_after_values == [None]


async def test_reply_to_post_and_reply_to_reply_are_persisted_relationally(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    parent = ThreadReply(
        account_id=account_id,
        threads_reply_id="existing-parent-reply",
        root_post_id=root.id,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.replies.add(parent)
    api = FakeThreadsAPI()
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)

    to_post = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.create_reply",
            {"threads_post_id": root.threads_post_id, "text": "Reply to post"},
        )
    )
    post_reply_result = await runtime.process(to_post.command_id)
    to_reply = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.create_reply",
            {
                "threads_post_id": root.threads_post_id,
                "reply_to_reply_id": "existing-parent-reply",
                "text": "Reply to reply",
            },
        )
    )
    nested_reply_result = await runtime.process(to_reply.command_id)
    created_replies = list(
        await db_session.scalars(
            select(ReplyRecord).where(ReplyRecord.threads_reply_id.like("published-%"))
        )
    )

    assert post_reply_result.status is CommandStatus.SUCCEEDED
    assert nested_reply_result.status is CommandStatus.SUCCEEDED
    assert api.created[0].reply_to_id == root.threads_post_id
    assert api.created[1].reply_to_id == "existing-parent-reply"
    assert len(created_replies) == 2
    assert all(reply.root_post_id == root.id for reply in created_replies)
    assert (
        next(
            reply for reply in created_replies if reply.threads_reply_id.endswith("-2")
        ).parent_reply_id
        == parent.id
    )


async def test_sync_missing_or_deleted_remote_object_is_a_typed_success_result(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.conversation_error = ThreadsAPIError("THREADS_OBJECT_NOT_FOUND")
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )

    outcome = await runtime.process(receipt.command_id)
    sync_state_count = await db_session.scalar(select(func.count()).select_from(SyncStateRecord))

    assert outcome.status is CommandStatus.SUCCEEDED
    assert sync_state_count == 0
    assert api.page_after_values == [None]


async def test_sync_skips_reply_with_missing_deleted_parent_without_nesting_corruption(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.page_responses = [
        ReplyPage(
            (remote_reply("reply-with-deleted-parent", root.threads_post_id, "deleted-parent"),),
            "cursor-1",
            has_more=False,
        )
    ]
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.sync_conversation",
            {"threads_post_id": root.threads_post_id},
        )
    )

    outcome = await runtime.process(receipt.command_id)
    reply_count = await db_session.scalar(select(func.count()).select_from(ReplyRecord))

    assert outcome.status is CommandStatus.SUCCEEDED
    assert reply_count == 0


async def test_moderation_success_permission_error_and_timeout_are_not_blindly_retried(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_id, root = await seed_account_and_post(unit_of_work_factory)
    top_level = ThreadReply(
        account_id=account_id,
        threads_reply_id="reply-doc-example",
        root_post_id=root.id,
    )
    nested = ThreadReply(
        account_id=account_id,
        threads_reply_id="nested-reply-doc-example",
        root_post_id=root.id,
        parent_reply_id=top_level.id,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.replies.add(top_level)
        await unit_of_work.replies.add(nested)
    api = FakeThreadsAPI()
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock, max_attempts=2)
    hide = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.moderate_reply",
            {"threads_reply_id": "reply-doc-example", "action": "hide"},
        )
    )
    hide_result = await runtime.process(hide.command_id)
    assert hide_result.status is CommandStatus.SUCCEEDED
    assert api.moderation_calls == [("manage_reply", "reply-doc-example", True)]

    nested_hide = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.moderate_reply",
            {"threads_reply_id": "nested-reply-doc-example", "action": "hide"},
        )
    )
    nested_result = await runtime.process(nested_hide.command_id)
    assert nested_result.status is CommandStatus.FAILED_FINAL
    assert api.moderation_calls == [("manage_reply", "reply-doc-example", True)]

    api.moderation_error = ThreadsAPIError("THREADS_PERMISSION_DENIED")
    permission = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.moderate_reply",
            {"threads_reply_id": "reply-doc-example", "action": "approve"},
        )
    )
    permission_result = await runtime.process(permission.command_id)
    assert permission_result.status is CommandStatus.FAILED_FINAL

    api.moderation_error = ThreadsTransportError()
    ambiguous = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.moderate_reply",
            {"threads_reply_id": "reply-doc-example", "action": "unhide"},
        )
    )
    first_ambiguous_result = await runtime.process(ambiguous.command_id)
    clock.advance(timedelta(seconds=1))
    terminal_ambiguous_result = await runtime.process(ambiguous.command_id)

    assert first_ambiguous_result.status is CommandStatus.FAILED_RETRYABLE
    assert terminal_ambiguous_result.status is CommandStatus.FAILED_FINAL
    assert api.moderation_attempts == 3
