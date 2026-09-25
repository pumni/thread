from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.domain.accounts import AccountStatus, ThreadsAccount
from threads_platform.domain.commands import Command
from threads_platform.domain.publishing import ThreadPost, ThreadReply
from threads_platform.infrastructure.persistence.repositories import (
    SQLAlchemyAccountRepository,
    SQLAlchemyCommandRepository,
    SQLAlchemyPostRepository,
    SQLAlchemyReplyRepository,
)

pytestmark = pytest.mark.integration


async def test_initial_migration_creates_all_persistence_tables(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    )
    table_names = set(result.scalars())

    assert {
        "threads_accounts",
        "oauth_credentials",
        "commands",
        "command_attempts",
        "posts",
        "replies",
        "schedules",
        "sync_states",
        "sync_runs",
        "insight_snapshots",
        "outbox_events",
        "integration_deliveries",
        "worker_nodes",
        "worker_capabilities",
        "worker_enrollments",
        "worker_auth_challenges",
        "worker_sessions",
        "worker_account_sessions",
        "worker_audit_events",
        "browser_profiles",
        "network_profiles",
        "account_worker_assignments",
        "worker_jobs",
        "worker_job_attempts",
        "worker_interventions",
    } <= table_names


async def test_account_repository_creates_reads_and_updates(db_session: AsyncSession) -> None:
    accounts = SQLAlchemyAccountRepository(db_session)
    account = ThreadsAccount(threads_user_id=f"account-{uuid4()}", username="initial")
    await accounts.add(account)

    loaded = await accounts.get(account.id)
    assert loaded is not None
    assert loaded.username == "initial"

    loaded.username = "updated"
    loaded.status = AccountStatus.REAUTHORIZATION_REQUIRED
    await accounts.update(loaded)

    updated = await accounts.get(account.id)
    assert updated is not None
    assert updated.username == "updated"
    assert updated.status is AccountStatus.REAUTHORIZATION_REQUIRED


async def test_command_id_is_unique(db_session: AsyncSession) -> None:
    accounts = SQLAlchemyAccountRepository(db_session)
    account = ThreadsAccount(threads_user_id=f"account-{uuid4()}", username="example")
    await accounts.add(account)
    commands = SQLAlchemyCommandRepository(db_session)
    duplicate_id = f"duplicate-{uuid4()}"
    first = Command(
        command_id=duplicate_id,
        correlation_id="correlation-1",
        account_id=account.id,
        command_type="threads.publish_post",
        payload={"text": "one"},
    )
    await commands.add(first)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await commands.add(
                Command(
                    command_id=duplicate_id,
                    correlation_id="correlation-2",
                    account_id=account.id,
                    command_type="threads.publish_post",
                    payload={"text": "two"},
                )
            )

    loaded = await commands.get_by_command_id(duplicate_id)
    assert loaded is not None
    assert loaded.payload == {"text": "one"}


async def test_post_and_reply_external_ids_are_unique_per_account(
    db_session: AsyncSession,
) -> None:
    accounts = SQLAlchemyAccountRepository(db_session)
    account = ThreadsAccount(threads_user_id=f"account-{uuid4()}", username="example")
    await accounts.add(account)
    posts = SQLAlchemyPostRepository(db_session)
    post = ThreadPost(account_id=account.id, threads_post_id="post-1", text="text")
    await posts.add(post)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await posts.add(ThreadPost(account_id=account.id, threads_post_id="post-1"))

    replies = SQLAlchemyReplyRepository(db_session)
    await replies.add(
        ThreadReply(account_id=account.id, threads_reply_id="reply-1", root_post_id=post.id)
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await replies.add(
                ThreadReply(
                    account_id=account.id,
                    threads_reply_id="reply-1",
                    root_post_id=post.id,
                )
            )
