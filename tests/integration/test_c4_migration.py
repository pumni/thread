from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.threads_test_support import seed_account_and_post
from threads_platform.domain.discovery import (
    DiscoveredAuthor,
    DiscoveredThread,
    DiscoveryCampaign,
    DiscoveryEvidenceClass,
    DiscoveryEvidenceSource,
    DiscoveryQueryKind,
    DiscoveryRun,
    DiscoveryRunCursor,
    DiscoveryRunStatus,
    DiscoverySearchMode,
    DiscoverySearchType,
    DiscoverySourceEvidence,
    LeadCandidate,
    LeadCandidateEvidence,
    LeadCandidateStatus,
    LeadCandidateTransition,
    SearchQuery,
)
from threads_platform.domain.publishing import ThreadReply
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


async def test_c4_migration_downgrade_deletes_seeded_discovery_data_and_discovered_replies(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    account_id, post = await seed_account_and_post(unit_of_work_factory)
    now = datetime.now(UTC)
    campaign = DiscoveryCampaign(account_id, "Migration downgrade seed", created_at=now)
    query = SearchQuery(
        campaign_id=campaign.id,
        kind=DiscoveryQueryKind.SEARCH,
        query_text="downgrade seed",
        search_mode=DiscoverySearchMode.KEYWORD,
        search_type=DiscoverySearchType.RECENT,
        created_at=now,
    )
    author = DiscoveredAuthor(
        remote_author_id=f"migration-author-{uuid4()}",
        username="migration_seed",
        created_at=now,
        updated_at=now,
    )
    thread = DiscoveredThread(
        remote_thread_id=f"migration-thread-{uuid4()}",
        author_id=author.id,
        username=author.username,
        text="Seeded discovery row for downgrade regression",
        created_at=now,
        updated_at=now,
    )
    post_reply = ThreadReply(
        account_id=account_id,
        threads_reply_id=f"migration-post-reply-{uuid4()}",
        root_post_id=post.id,
        text="Post-root reply must survive downgrade",
    )
    discovered_reply = ThreadReply(
        account_id=account_id,
        threads_reply_id=f"migration-discovered-reply-{uuid4()}",
        discovered_thread_id=thread.id,
        text="Discovered-root reply is removed by downgrade",
    )
    discovered_child_reply = ThreadReply(
        account_id=account_id,
        threads_reply_id=f"migration-discovered-child-{uuid4()}",
        parent_reply_id=discovered_reply.id,
        discovered_thread_id=thread.id,
        text="Nested discovered-root reply is also removed",
    )

    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.discovery.add_campaign(campaign)
        stored_query = await unit_of_work.discovery.get_or_create_query(query)
        run = DiscoveryRun(
            account_id=account_id,
            campaign_id=campaign.id,
            query_id=stored_query.id,
            command_id=f"migration-downgrade-{uuid4()}",
            cursor="seed-cursor",
            pages_processed=1,
            items_processed=1,
            started_at=now,
            updated_at=now,
        )
        assert await unit_of_work.discovery.add_run_if_absent(run)
        stored_author = await unit_of_work.discovery.upsert_author(author)
        stored_thread = await unit_of_work.discovery.upsert_thread(thread)
        run.status = DiscoveryRunStatus.PAUSED
        await unit_of_work.discovery.update_run(run)
        source_evidence = DiscoverySourceEvidence(
            run_id=run.id,
            source=DiscoveryEvidenceSource.KEYWORD_SEARCH,
            page_number=1,
            thread_id=stored_thread.id,
            observed_at=now,
            evidence_class=DiscoveryEvidenceClass.DOCUMENTATION_CONTRACT,
        )
        assert await unit_of_work.discovery.add_evidence(source_evidence)
        assert await unit_of_work.discovery.add_run_cursor(
            DiscoveryRunCursor(run.id, sha256(b"seed-cursor").hexdigest(), 1)
        )
        candidate = LeadCandidate(account_id, stored_author.id)
        assert await unit_of_work.discovery.add_candidate_if_absent(candidate)
        assert await unit_of_work.discovery.add_candidate_evidence(
            LeadCandidateEvidence(candidate.id, source_evidence.id)
        )
        await unit_of_work.discovery.add_candidate_transition(
            LeadCandidateTransition(
                candidate_id=candidate.id,
                previous_status=LeadCandidateStatus.ENRICHMENT_PENDING,
                next_status=LeadCandidateStatus.CANDIDATE,
                command_id=f"migration-transition-{uuid4()}",
                reason_code="SEEDED_DOWNGRADE_TEST",
                occurred_at=now,
            )
        )
        candidate.status = LeadCandidateStatus.CANDIDATE
        await unit_of_work.discovery.update_candidate(candidate)
        assert await unit_of_work.replies.add_if_absent(post_reply)
        assert await unit_of_work.replies.add_if_absent(discovered_reply)
        assert await unit_of_work.replies.add_if_absent(discovered_child_reply)

    config = Config("alembic.ini")
    command.downgrade(config, "20260925_0008")

    assert await db_session.scalar(text("SELECT to_regclass('public.discovery_campaigns')")) is None
    surviving_replies = set(await db_session.scalars(text("SELECT threads_reply_id FROM replies")))
    assert surviving_replies == {post_reply.threads_reply_id}

    command.upgrade(config, "head")

    discovery_tables = (
        "discovery_campaigns",
        "discovery_search_queries",
        "discovered_authors",
        "discovered_threads",
        "discovery_runs",
        "discovery_run_cursors",
        "discovery_source_evidence",
        "lead_candidates",
        "lead_candidate_evidence",
        "lead_candidate_transitions",
    )
    for table_name in discovery_tables:
        assert await db_session.scalar(text(f"SELECT count(*) FROM {table_name}")) == 0
    preserved_post_reply = await db_session.execute(
        text(
            "SELECT root_post_id, discovered_thread_id FROM replies "
            "WHERE threads_reply_id = :reply_id"
        ),
        {"reply_id": post_reply.threads_reply_id},
    )
    row = preserved_post_reply.one()
    assert row.root_post_id == post.id
    assert row.discovered_thread_id is None
