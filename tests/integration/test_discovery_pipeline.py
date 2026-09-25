import asyncio
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

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
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.ports.threads import (
    DiscoveryPage,
    RemoteDiscoveryThread,
    RemotePublicProfile,
    ReplyPage,
    ThreadsAPIError,
)
from threads_platform.domain.commands import CommandStatus
from threads_platform.domain.discovery import DiscoveredThread, DiscoveryRunStatus
from threads_platform.infrastructure.persistence.models import (
    CommandRecord,
    DiscoveredAuthorRecord,
    DiscoveredThreadRecord,
    DiscoveryRunCursorRecord,
    DiscoveryRunRecord,
    DiscoverySourceEvidenceRecord,
    LeadCandidateEvidenceRecord,
    LeadCandidateRecord,
    LeadCandidateTransitionRecord,
    ReplyRecord,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


def remote_thread(
    remote_id: str,
    *,
    username: str = "sample",
    text: str = "Synthetic public discussion",
) -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(
        remote_thread_id=remote_id,
        author_remote_id=None,
        username=username,
        text=text,
        permalink=f"https://www.threads.net/@{username}/post/{remote_id}",
        media_type="TEXT_POST",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        is_quote_post=False,
        has_replies=True,
    )


async def create_campaign(
    runtime: CommandRuntime, account_id: UUID, clock: FixedClock, name: str = "C4 campaign"
) -> UUID:
    body = command_body(
        account_id,
        clock,
        "threads.discovery.create_campaign",
        {"name": name},
    )
    receipt = await runtime.receive(body)
    result = await runtime.process(receipt.command_id)
    assert result.status is CommandStatus.SUCCEEDED
    return uuid5(
        NAMESPACE_URL,
        f"threads.discovery.campaign:{account_id}:{receipt.command_id}",
    )


async def test_run_page_and_cursor_commit_atomically_and_resume_without_duplicate_threads(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    campaign_id = await create_campaign(runtime, account_id, clock)
    api.discovery_pages = [
        DiscoveryPage((remote_thread("shared-thread"),), "cursor-page-2", True),
        DiscoveryPage(
            (
                remote_thread("shared-thread", text="Overlapping source row"),
                remote_thread("second-thread", username="another"),
            ),
            None,
            False,
        ),
    ]
    search = command_body(
        account_id,
        clock,
        "threads.discovery.search",
        {
            "campaign_id": str(campaign_id),
            "query": "public design",
            "search_mode": "KEYWORD",
            "search_type": "RECENT",
            "max_pages": 1,
        },
    )
    receipt = await runtime.receive(search)
    first_result = await runtime.process(receipt.command_id)
    first_run = await db_session.scalar(
        select(DiscoveryRunRecord).where(DiscoveryRunRecord.command_id == receipt.command_id)
    )
    assert first_result.status is CommandStatus.SUCCEEDED
    assert first_run is not None
    assert first_run.status is DiscoveryRunStatus.PAUSED
    assert first_run.cursor == "cursor-page-2"
    assert first_run.pages_processed == 1
    assert await db_session.scalar(select(func.count()).select_from(DiscoveryRunCursorRecord)) == 1

    resume = command_body(
        account_id,
        clock,
        "threads.discovery.resume",
        {"run_id": str(first_run.id), "max_pages": 1},
    )
    resume_receipt = await runtime.receive(resume)
    resumed_result = await runtime.process(resume_receipt.command_id)
    final_run = await db_session.scalar(
        select(DiscoveryRunRecord)
        .where(DiscoveryRunRecord.id == first_run.id)
        .execution_options(populate_existing=True)
    )
    threads = list(
        await db_session.scalars(
            select(DiscoveredThreadRecord).order_by(DiscoveredThreadRecord.remote_thread_id)
        )
    )
    shared = next(thread for thread in threads if thread.remote_thread_id == "shared-thread")
    shared_evidence = list(
        await db_session.scalars(
            select(DiscoverySourceEvidenceRecord).where(
                DiscoverySourceEvidenceRecord.thread_id == shared.id
            )
        )
    )

    assert resumed_result.status is CommandStatus.SUCCEEDED
    assert api.discovery_after_values == [None, "cursor-page-2"]
    assert len(threads) == 2
    assert len(shared_evidence) == 2
    assert final_run is not None
    assert final_run.status is DiscoveryRunStatus.SUCCEEDED
    assert final_run.pages_processed == 2
    assert final_run.items_processed == 3
    assert final_run.cursor is None


async def test_profile_enrichment_preserves_search_provenance_and_creates_auditable_lead(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.discovery_pages = [DiscoveryPage((remote_thread("profile-thread"),), None, False)]
    api.profile_post_pages = [
        DiscoveryPage((remote_thread("profile-thread", text="Profile post"),), None, False)
    ]
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    campaign_id = await create_campaign(runtime, account_id, clock)
    api.profile = RemotePublicProfile("author-doc-1", "sample", "Sample", None, None)

    search = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.discovery.search",
            {
                "campaign_id": str(campaign_id),
                "query": "sample topic",
                "search_mode": "KEYWORD",
                "search_type": "TOP",
            },
        )
    )
    assert (await runtime.process(search.command_id)).status is CommandStatus.SUCCEEDED
    profile = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.discovery.profile",
            {"campaign_id": str(campaign_id), "username": "sample"},
        )
    )
    assert (await runtime.process(profile.command_id)).status is CommandStatus.SUCCEEDED

    authors = list(await db_session.scalars(select(DiscoveredAuthorRecord)))
    threads = list(await db_session.scalars(select(DiscoveredThreadRecord)))
    candidates = list(await db_session.scalars(select(LeadCandidateRecord)))
    thread_evidence = list(
        await db_session.scalars(
            select(DiscoverySourceEvidenceRecord).where(
                DiscoverySourceEvidenceRecord.thread_id == threads[0].id
            )
        )
    )
    author_evidence = list(
        await db_session.scalars(
            select(DiscoverySourceEvidenceRecord).where(
                DiscoverySourceEvidenceRecord.author_id == authors[0].id
            )
        )
    )
    links = list(await db_session.scalars(select(LeadCandidateEvidenceRecord)))

    assert len(authors) == len(threads) == len(candidates) == 1
    assert threads[0].author_id == authors[0].id
    assert {item.source for item in thread_evidence} == {"KEYWORD_SEARCH", "PROFILE_POSTS"}
    assert {item.source for item in author_evidence} == {
        "PUBLIC_PROFILE_LOOKUP",
        "PROFILE_POSTS",
    }
    assert len(links) == 4
    assert {link.evidence_id for link in links} == {
        *(item.id for item in thread_evidence),
        *(item.id for item in author_evidence),
    }
    assert candidates[0].status == "ENRICHMENT_PENDING"
    profile_command = await db_session.scalar(
        select(CommandRecord).where(CommandRecord.command_id == profile.command_id)
    )
    assert profile_command is not None
    assert profile_command.result is not None
    assert profile_command.result["lead_candidate_id"] == str(candidates[0].id)

    transition = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.discovery.lead_status",
            {
                "candidate_id": str(candidates[0].id),
                "target_status": "CANDIDATE",
                "reason_code": "OPERATOR_REVIEWED",
            },
        )
    )
    assert (await runtime.process(transition.command_id)).status is CommandStatus.SUCCEEDED
    assert (
        await db_session.scalar(select(func.count()).select_from(LeadCandidateTransitionRecord))
        == 1
    )
    assert candidates[0].status == "ENRICHMENT_PENDING"
    await db_session.refresh(candidates[0])
    assert candidates[0].status == "CANDIDATE"


async def test_concurrent_canonical_thread_upserts_use_postgres_identity_constraint(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    async def insert(text_value: str) -> DiscoveredThread:
        async with unit_of_work_factory() as unit_of_work:
            return await unit_of_work.discovery.upsert_thread(
                DiscoveredThread(remote_thread_id="same-remote-id", text=text_value)
            )

    first, second = await asyncio.gather(insert("first source"), insert("second source"))

    assert first.id == second.id
    assert await db_session.scalar(select(func.count()).select_from(DiscoveredThreadRecord)) == 1


async def test_discovered_conversation_reuses_reply_hierarchy_without_own_post_root(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.page_responses = [
        ReplyPage(
            (
                remote_reply("public-reply-a", "root-public-1", "root-public-1"),
                remote_reply("public-reply-b", "root-public-1", "public-reply-a"),
            ),
            None,
            False,
        )
    ]
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    campaign_id = await create_campaign(runtime, account_id, clock)
    receipt = await runtime.receive(
        command_body(
            account_id,
            clock,
            "threads.discovery.conversation",
            {"campaign_id": str(campaign_id), "thread_remote_id": "root-public-1"},
        )
    )
    result = await runtime.process(receipt.command_id)
    replies = list(await db_session.scalars(select(ReplyRecord)))
    replies_by_id = {reply.threads_reply_id: reply for reply in replies}

    assert result.status is CommandStatus.SUCCEEDED
    assert len(replies) == 2
    assert all(reply.root_post_id is None for reply in replies)
    root_reply = replies_by_id["public-reply-a"]
    nested_reply = replies_by_id["public-reply-b"]
    assert root_reply.discovered_thread_id == nested_reply.discovered_thread_id
    assert nested_reply.parent_reply_id == root_reply.id


async def test_repeated_discovery_cursor_fails_with_stable_code(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.discovery_pages = [
        DiscoveryPage((remote_thread("cursor-thread-1"),), "repeat-cursor", True),
        DiscoveryPage((remote_thread("cursor-thread-2"),), "repeat-cursor", True),
    ]
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    campaign_id = await create_campaign(runtime, account_id, clock)
    body = command_body(
        account_id,
        clock,
        "threads.discovery.search",
        {
            "campaign_id": str(campaign_id),
            "query": "stalled cursor",
            "search_mode": "TAG",
            "search_type": "RECENT",
            "max_pages": 3,
        },
    )
    receipt = await runtime.receive(body)
    result = await runtime.process(receipt.command_id)
    run = await db_session.scalar(
        select(DiscoveryRunRecord).where(DiscoveryRunRecord.command_id == receipt.command_id)
    )

    assert result.status is CommandStatus.FAILED_FINAL
    assert run is not None
    assert run.status is DiscoveryRunStatus.FAILED_FINAL
    assert run.error_code == "DISCOVERY_CURSOR_STALLED"
    assert run.pages_processed == 1
    assert await db_session.scalar(select(func.count()).select_from(DiscoveredThreadRecord)) == 1


@pytest.mark.parametrize(
    ("error_code", "expected_command_status", "expected_run_status"),
    [
        (
            "THREADS_SERVER_ERROR",
            CommandStatus.FAILED_RETRYABLE,
            DiscoveryRunStatus.FAILED_RETRYABLE,
        ),
        ("THREADS_PERMISSION_DENIED", CommandStatus.FAILED_FINAL, DiscoveryRunStatus.FAILED_FINAL),
    ],
)
async def test_api_errors_keep_retryable_and_permanent_classification(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
    error_code: str,
    expected_command_status: CommandStatus,
    expected_run_status: DiscoveryRunStatus,
) -> None:
    account_id, _ = await seed_account_and_post(unit_of_work_factory)
    api = FakeThreadsAPI()
    api.discovery_error = ThreadsAPIError(error_code)
    clock = FixedClock()
    runtime = make_runtime(unit_of_work_factory, api, clock)
    campaign_id = await create_campaign(runtime, account_id, clock)
    body = command_body(
        account_id,
        clock,
        "threads.discovery.search",
        {
            "campaign_id": str(campaign_id),
            "query": "error mapping",
            "search_mode": "KEYWORD",
            "search_type": "TOP",
        },
    )
    receipt = await runtime.receive(body)
    result = await runtime.process(receipt.command_id)
    run = await db_session.scalar(
        select(DiscoveryRunRecord).where(DiscoveryRunRecord.command_id == receipt.command_id)
    )

    assert result.status is expected_command_status
    assert run is not None
    assert run.status is expected_run_status
    assert run.error_code == error_code
