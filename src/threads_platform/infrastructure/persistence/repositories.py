import hashlib
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.ports.repositories import (
    AccountExecutionLeaseRepository,
    AccountRepository,
    AccountWorkerAssignmentRepository,
    BrowserProfileRepository,
    CommandAttemptRepository,
    CommandRepository,
    CommandRouteDecisionRepository,
    DiscoveryRepository,
    IntegrationDeliveryRepository,
    NetworkProfileRepository,
    OutboxEventRepository,
    PostRepository,
    ReplyRepository,
    SyncStateRepository,
    WorkerAccountSessionRepository,
    WorkerCapabilityRepository,
    WorkerInterventionRepository,
    WorkerJobAttemptRepository,
    WorkerJobRepository,
    WorkerRepository,
    WorkerSecurityRepository,
)
from threads_platform.domain.account_execution import (
    AccountExecutionLease,
    AccountExecutionOwnerType,
)
from threads_platform.domain.accounts import (
    AccountExecutionMode,
    AccountStatus,
    ThreadsAccount,
)
from threads_platform.domain.capabilities import (
    CapabilityExecutionClass,
    CapabilityExecutor,
    CapabilityRouteDecision,
    OperationClass,
    RouteTarget,
)
from threads_platform.domain.commands import (
    AttemptStatus,
    Command,
    CommandAttempt,
    CommandStatus,
)
from threads_platform.domain.discovery import (
    DiscoveredAuthor,
    DiscoveredThread,
    DiscoveryCampaign,
    DiscoveryEnrichmentStatus,
    DiscoveryEvidenceSource,
    DiscoveryRun,
    DiscoveryRunCursor,
    DiscoverySourceEvidence,
    LeadCandidate,
    LeadCandidateEvidence,
    LeadCandidateTransition,
    SearchQuery,
)
from threads_platform.domain.outbox import (
    DeliveryStatus,
    IntegrationDelivery,
    OutboxEvent,
    OutboxStatus,
)
from threads_platform.domain.publishing import ThreadPost, ThreadReply
from threads_platform.domain.sync import SyncState
from threads_platform.domain.time import normalize_utc
from threads_platform.domain.worker_jobs import (
    WorkerIntervention,
    WorkerInterventionStatus,
    WorkerJob,
    WorkerJobAttempt,
    WorkerJobAttemptStatus,
    WorkerJobRetrySafety,
    WorkerJobStatus,
)
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    BrowserSessionState,
    NetworkProfile,
    NetworkProtocol,
    WorkerAccountSession,
    WorkerAuditEvent,
    WorkerAuthChallenge,
    WorkerCapability,
    WorkerEnrollment,
    WorkerNode,
    WorkerSession,
    WorkerStatus,
)
from threads_platform.infrastructure.persistence.models import (
    AccountExecutionLeaseRecord,
    AccountRecord,
    AccountWorkerAssignmentRecord,
    BrowserProfileRecord,
    CommandAttemptRecord,
    CommandRecord,
    CommandRouteDecisionRecord,
    DiscoveredAuthorRecord,
    DiscoveredThreadRecord,
    DiscoveryCampaignRecord,
    DiscoveryRunCursorRecord,
    DiscoveryRunRecord,
    DiscoverySourceEvidenceRecord,
    IntegrationDeliveryRecord,
    LeadCandidateEvidenceRecord,
    LeadCandidateRecord,
    LeadCandidateTransitionRecord,
    NetworkProfileRecord,
    OutboxEventRecord,
    PostRecord,
    ReplyRecord,
    SearchQueryRecord,
    SyncStateRecord,
    WorkerAccountSessionRecord,
    WorkerAuditEventRecord,
    WorkerAuthChallengeRecord,
    WorkerCapabilityRecord,
    WorkerEnrollmentRecord,
    WorkerInterventionRecord,
    WorkerJobAttemptRecord,
    WorkerJobRecord,
    WorkerNodeRecord,
    WorkerSessionRecord,
)


class SQLAlchemyAccountRepository(AccountRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, account: ThreadsAccount) -> None:
        self._session.add(
            AccountRecord(
                id=account.id,
                threads_user_id=account.threads_user_id,
                username=account.username,
                display_name=account.display_name,
                status=account.status,
                execution_mode=account.execution_mode,
                created_at=account.created_at,
                updated_at=account.updated_at,
            )
        )
        await self._session.flush()

    async def get(self, account_id: UUID) -> ThreadsAccount | None:
        record = await self._session.get(AccountRecord, account_id)
        if record is None:
            return None
        return ThreadsAccount(
            id=record.id,
            threads_user_id=record.threads_user_id,
            username=record.username,
            display_name=record.display_name,
            status=AccountStatus(record.status),
            execution_mode=AccountExecutionMode(record.execution_mode),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    async def get_for_update(self, account_id: UUID) -> ThreadsAccount | None:
        record = await self._session.scalar(
            select(AccountRecord).where(AccountRecord.id == account_id).with_for_update()
        )
        if record is None:
            return None
        return ThreadsAccount(
            id=record.id,
            threads_user_id=record.threads_user_id,
            username=record.username,
            display_name=record.display_name,
            status=AccountStatus(record.status),
            execution_mode=AccountExecutionMode(record.execution_mode),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    async def update(self, account: ThreadsAccount) -> None:
        record = await self._session.get(AccountRecord, account.id)
        if record is None:
            raise LookupError(f"account not found: {account.id}")
        record.threads_user_id = account.threads_user_id
        record.username = account.username
        record.display_name = account.display_name
        record.status = account.status
        record.execution_mode = account.execution_mode
        record.updated_at = account.updated_at
        await self._session.flush()


class SQLAlchemyAccountExecutionLeaseRepository(AccountExecutionLeaseRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_acquire(
        self,
        account_id: UUID,
        owner_type: AccountExecutionOwnerType,
        owner_id: str,
        operation_class: OperationClass,
        now: datetime,
        lease_expires_at: datetime,
    ) -> AccountExecutionLease | None:
        occurred_at = normalize_utc(now)
        expires_at = normalize_utc(lease_expires_at)
        if operation_class is OperationClass.READ:
            raise ValueError("READ operations do not acquire account execution leases")
        if not owner_id.strip() or expires_at <= occurred_at:
            raise ValueError("account execution lease owner and future expiry are required")
        await self._session.execute(
            postgres_insert(AccountExecutionLeaseRecord)
            .values(
                account_id=account_id,
                owner_type=owner_type,
                owner_id=owner_id,
                operation_class=operation_class,
                fencing_generation=0,
                lease_expires_at=occurred_at,
                updated_at=occurred_at,
            )
            .on_conflict_do_nothing(index_elements=[AccountExecutionLeaseRecord.account_id])
        )
        record = await self._session.scalar(
            select(AccountExecutionLeaseRecord)
            .where(AccountExecutionLeaseRecord.account_id == account_id)
            .with_for_update()
        )
        if record is None:
            raise RuntimeError("account execution lease row disappeared")
        if record.lease_expires_at > occurred_at:
            return None
        record.owner_type = owner_type
        record.owner_id = owner_id
        record.operation_class = operation_class
        record.fencing_generation += 1
        record.lease_expires_at = expires_at
        record.updated_at = occurred_at
        await self._session.flush()
        return self._domain(record)

    async def get_active(self, account_id: UUID, now: datetime) -> AccountExecutionLease | None:
        occurred_at = normalize_utc(now)
        record = await self._session.scalar(
            select(AccountExecutionLeaseRecord).where(
                AccountExecutionLeaseRecord.account_id == account_id,
                AccountExecutionLeaseRecord.lease_expires_at > occurred_at,
            )
        )
        return self._domain(record) if record is not None else None

    async def renew(
        self,
        account_id: UUID,
        owner_type: AccountExecutionOwnerType,
        owner_id: str,
        fencing_generation: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        occurred_at = normalize_utc(now)
        expires_at = normalize_utc(lease_expires_at)
        result = await self._session.scalar(
            update(AccountExecutionLeaseRecord)
            .where(
                AccountExecutionLeaseRecord.account_id == account_id,
                AccountExecutionLeaseRecord.owner_type == owner_type,
                AccountExecutionLeaseRecord.owner_id == owner_id,
                AccountExecutionLeaseRecord.fencing_generation == fencing_generation,
                AccountExecutionLeaseRecord.lease_expires_at > occurred_at,
            )
            .values(lease_expires_at=expires_at, updated_at=occurred_at)
            .returning(AccountExecutionLeaseRecord.account_id)
        )
        return result is not None

    async def owns(
        self,
        account_id: UUID,
        owner_type: AccountExecutionOwnerType,
        owner_id: str,
        fencing_generation: int,
        now: datetime,
    ) -> bool:
        occurred_at = normalize_utc(now)
        result = await self._session.scalar(
            select(AccountExecutionLeaseRecord.account_id)
            .where(
                AccountExecutionLeaseRecord.account_id == account_id,
                AccountExecutionLeaseRecord.owner_type == owner_type,
                AccountExecutionLeaseRecord.owner_id == owner_id,
                AccountExecutionLeaseRecord.fencing_generation == fencing_generation,
                AccountExecutionLeaseRecord.lease_expires_at > occurred_at,
            )
            .with_for_update()
        )
        return result is not None

    async def release(
        self,
        account_id: UUID,
        owner_type: AccountExecutionOwnerType,
        owner_id: str,
        fencing_generation: int,
        now: datetime,
    ) -> bool:
        occurred_at = normalize_utc(now)
        result = await self._session.scalar(
            update(AccountExecutionLeaseRecord)
            .where(
                AccountExecutionLeaseRecord.account_id == account_id,
                AccountExecutionLeaseRecord.owner_type == owner_type,
                AccountExecutionLeaseRecord.owner_id == owner_id,
                AccountExecutionLeaseRecord.fencing_generation == fencing_generation,
                AccountExecutionLeaseRecord.lease_expires_at > occurred_at,
            )
            .values(lease_expires_at=occurred_at, updated_at=occurred_at)
            .returning(AccountExecutionLeaseRecord.account_id)
        )
        return result is not None

    @staticmethod
    def _domain(record: AccountExecutionLeaseRecord) -> AccountExecutionLease:
        return AccountExecutionLease(
            account_id=record.account_id,
            owner_type=AccountExecutionOwnerType(record.owner_type),
            owner_id=record.owner_id,
            operation_class=OperationClass(record.operation_class),
            fencing_generation=record.fencing_generation,
            lease_expires_at=record.lease_expires_at,
            updated_at=record.updated_at,
        )


class SQLAlchemyCommandRepository(CommandRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, command: Command) -> None:
        self._session.add(self._record(command))
        await self._session.flush()

    async def add_if_absent(self, command: Command) -> bool:
        statement = (
            postgres_insert(CommandRecord)
            .values(**self._values(command))
            .on_conflict_do_nothing(index_elements=[CommandRecord.command_id])
            .returning(CommandRecord.id)
        )
        inserted_id = await self._session.scalar(statement)
        return inserted_id is not None

    async def get_by_command_id(self, command_id: str) -> Command | None:
        record = await self._session.scalar(
            select(CommandRecord).where(CommandRecord.command_id == command_id)
        )
        if record is None:
            return None
        return self._domain(record)

    async def get_by_command_id_for_update(self, command_id: str) -> Command | None:
        record = await self._session.scalar(
            select(CommandRecord).where(CommandRecord.command_id == command_id).with_for_update()
        )
        if record is None:
            return None
        return self._domain(record)

    async def get_next_ready_for_update(self, now: datetime) -> Command | None:
        ready = or_(
            CommandRecord.status == CommandStatus.RECEIVED,
            and_(
                CommandRecord.status == CommandStatus.FAILED_RETRYABLE,
                CommandRecord.next_retry_at <= now,
            ),
            and_(
                CommandRecord.status == CommandStatus.PROCESSING,
                or_(
                    CommandRecord.execution_lease_expires_at.is_(None),
                    CommandRecord.execution_lease_expires_at <= now,
                ),
            ),
            and_(
                CommandRecord.status == CommandStatus.WAITING_EXECUTION,
                ~exists(
                    select(WorkerJobRecord.id).where(
                        WorkerJobRecord.command_id == CommandRecord.command_id
                    )
                ),
            ),
        )
        record = await self._session.scalar(
            select(CommandRecord)
            .where(ready)
            .order_by(CommandRecord.received_at, CommandRecord.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return self._domain(record) if record is not None else None

    async def save_checkpoint_if_leased(
        self,
        command_id: str,
        lease_token: UUID,
        now: datetime,
        checkpoint: dict[str, object],
    ) -> bool:
        result = await self._session.scalar(
            update(CommandRecord)
            .where(
                CommandRecord.command_id == command_id,
                CommandRecord.status == CommandStatus.PROCESSING,
                CommandRecord.execution_lease_token == lease_token,
                CommandRecord.execution_lease_expires_at > now,
            )
            .values(checkpoint=checkpoint)
            .returning(CommandRecord.id)
        )
        return result is not None

    async def renew_execution_lease(
        self,
        command_id: str,
        lease_token: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = await self._session.scalar(
            update(CommandRecord)
            .where(
                CommandRecord.command_id == command_id,
                CommandRecord.status == CommandStatus.PROCESSING,
                CommandRecord.execution_lease_token == lease_token,
                CommandRecord.execution_lease_expires_at > now,
            )
            .values(execution_lease_expires_at=lease_expires_at)
            .returning(CommandRecord.id)
        )
        return result is not None

    async def update(self, command: Command) -> None:
        record = await self._session.get(CommandRecord, command.id)
        if record is None:
            raise LookupError(f"command not found: {command.command_id}")
        record.status = command.status
        record.validated_at = command.validated_at
        record.started_at = command.started_at
        record.completed_at = command.completed_at
        record.next_retry_at = command.next_retry_at
        record.result = command.result
        record.error_code = command.error_code
        record.execution_lease_token = command.execution_lease_token
        record.execution_lease_expires_at = command.execution_lease_expires_at
        record.checkpoint = command.checkpoint
        await self._session.flush()

    @staticmethod
    def _values(command: Command) -> dict[str, object]:
        return {
            "id": command.id,
            "command_id": command.command_id,
            "correlation_id": command.correlation_id,
            "protocol_version": command.protocol_version,
            "account_id": command.account_id,
            "command_type": command.command_type,
            "payload": command.payload,
            "status": command.status,
            "created_at": command.created_at,
            "received_at": command.received_at,
            "deadline_at": command.deadline_at,
            "next_retry_at": command.next_retry_at,
            "validated_at": command.validated_at,
            "started_at": command.started_at,
            "completed_at": command.completed_at,
            "result": command.result,
            "error_code": command.error_code,
            "execution_lease_token": command.execution_lease_token,
            "execution_lease_expires_at": command.execution_lease_expires_at,
            "checkpoint": command.checkpoint,
        }

    @classmethod
    def _record(cls, command: Command) -> CommandRecord:
        return CommandRecord(**cls._values(command))

    @staticmethod
    def _domain(record: CommandRecord) -> Command:
        return Command(
            id=record.id,
            command_id=record.command_id,
            correlation_id=record.correlation_id,
            account_id=record.account_id,
            protocol_version=record.protocol_version,
            command_type=record.command_type,
            payload=record.payload,
            status=CommandStatus(record.status),
            created_at=record.created_at,
            received_at=record.received_at,
            deadline_at=record.deadline_at,
            next_retry_at=record.next_retry_at,
            validated_at=record.validated_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            result=record.result,
            error_code=record.error_code,
            execution_lease_token=record.execution_lease_token,
            execution_lease_expires_at=record.execution_lease_expires_at,
            checkpoint=record.checkpoint,
        )


class SQLAlchemyCommandRouteDecisionRepository(CommandRouteDecisionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, decision: CapabilityRouteDecision) -> None:
        self._session.add(
            CommandRouteDecisionRecord(
                id=uuid4(),
                command_id=decision.command_id,
                account_id=decision.account_id,
                capability_name=decision.capability_name,
                capability_version=decision.capability_version,
                execution_class=decision.execution_class,
                operation_class=decision.operation_class,
                account_mode=decision.account_mode,
                target=decision.target,
                executor=decision.executor,
                reason_code=decision.reason_code,
                attempt_count=decision.attempt_count,
                created_at=decision.created_at,
            )
        )
        await self._session.flush()

    async def get_latest_for_command(self, command_id: str) -> CapabilityRouteDecision | None:
        record = await self._session.scalar(
            select(CommandRouteDecisionRecord)
            .where(CommandRouteDecisionRecord.command_id == command_id)
            .order_by(
                CommandRouteDecisionRecord.attempt_count.desc(),
                CommandRouteDecisionRecord.created_at.desc(),
                CommandRouteDecisionRecord.id.desc(),
            )
            .limit(1)
        )
        if record is None:
            return None
        return CapabilityRouteDecision(
            command_id=record.command_id,
            account_id=record.account_id,
            capability_name=record.capability_name,
            capability_version=record.capability_version,
            execution_class=CapabilityExecutionClass(record.execution_class),
            operation_class=OperationClass(record.operation_class),
            account_mode=AccountExecutionMode(record.account_mode),
            target=RouteTarget(record.target),
            executor=CapabilityExecutor(record.executor) if record.executor is not None else None,
            reason_code=record.reason_code,
            attempt_count=record.attempt_count,
            created_at=record.created_at,
        )

    async def get_latest_execution_for_command(
        self, command_id: str
    ) -> CapabilityRouteDecision | None:
        record = await self._session.scalar(
            select(CommandRouteDecisionRecord)
            .where(
                CommandRouteDecisionRecord.command_id == command_id,
                CommandRouteDecisionRecord.executor.is_not(None),
            )
            .order_by(
                CommandRouteDecisionRecord.attempt_count.desc(),
                CommandRouteDecisionRecord.created_at.desc(),
                CommandRouteDecisionRecord.id.desc(),
            )
            .limit(1)
        )
        if record is None:
            return None
        return CapabilityRouteDecision(
            command_id=record.command_id,
            account_id=record.account_id,
            capability_name=record.capability_name,
            capability_version=record.capability_version,
            execution_class=CapabilityExecutionClass(record.execution_class),
            operation_class=OperationClass(record.operation_class),
            account_mode=AccountExecutionMode(record.account_mode),
            target=RouteTarget(record.target),
            executor=CapabilityExecutor(record.executor),
            reason_code=record.reason_code,
            attempt_count=record.attempt_count,
            created_at=record.created_at,
        )


class SQLAlchemyPostRepository(PostRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, post: ThreadPost) -> None:
        self._session.add(
            PostRecord(
                id=post.id,
                account_id=post.account_id,
                threads_post_id=post.threads_post_id,
                text=post.text,
                permalink=post.permalink,
                published_at=post.published_at,
                created_at=post.created_at,
                updated_at=post.updated_at,
                metadata_json=post.metadata,
            )
        )
        await self._session.flush()

    async def add_if_absent(self, post: ThreadPost) -> bool:
        statement = (
            postgres_insert(PostRecord)
            .values(**self._values(post))
            .on_conflict_do_nothing(constraint="uq_posts_account_threads_id")
            .returning(PostRecord.id)
        )
        return (await self._session.scalar(statement)) is not None

    async def get_by_external_id(self, account_id: UUID, threads_post_id: str) -> ThreadPost | None:
        record = await self._session.scalar(
            select(PostRecord).where(
                PostRecord.account_id == account_id,
                PostRecord.threads_post_id == threads_post_id,
            )
        )
        if record is None:
            return None
        return self._domain(record)

    @staticmethod
    def _values(post: ThreadPost) -> dict[str, object]:
        return {
            "id": post.id,
            "account_id": post.account_id,
            "threads_post_id": post.threads_post_id,
            "text": post.text,
            "permalink": post.permalink,
            "published_at": post.published_at,
            "created_at": post.created_at,
            "updated_at": post.updated_at,
            "metadata_json": post.metadata,
        }

    @staticmethod
    def _domain(record: PostRecord) -> ThreadPost:
        return ThreadPost(
            id=record.id,
            account_id=record.account_id,
            threads_post_id=record.threads_post_id,
            text=record.text,
            permalink=record.permalink,
            published_at=record.published_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
            metadata=record.metadata_json,
        )


class SQLAlchemyReplyRepository(ReplyRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, reply: ThreadReply) -> None:
        self._session.add(
            ReplyRecord(
                id=reply.id,
                account_id=reply.account_id,
                threads_reply_id=reply.threads_reply_id,
                root_post_id=reply.root_post_id,
                parent_reply_id=reply.parent_reply_id,
                text=reply.text,
                replied_at=reply.replied_at,
                created_at=reply.created_at,
            )
        )
        await self._session.flush()

    async def add_if_absent(self, reply: ThreadReply) -> bool:
        statement = (
            postgres_insert(ReplyRecord)
            .values(**self._values(reply))
            .on_conflict_do_nothing(constraint="uq_replies_account_threads_id")
            .returning(ReplyRecord.id)
        )
        return (await self._session.scalar(statement)) is not None

    async def get_by_external_id(
        self, account_id: UUID, threads_reply_id: str
    ) -> ThreadReply | None:
        record = await self._session.scalar(
            select(ReplyRecord).where(
                ReplyRecord.account_id == account_id,
                ReplyRecord.threads_reply_id == threads_reply_id,
            )
        )
        if record is None:
            return None
        return self._domain(record)

    async def list_for_root(self, account_id: UUID, root_post_id: UUID) -> list[ThreadReply]:
        records = await self._session.scalars(
            select(ReplyRecord).where(
                ReplyRecord.account_id == account_id,
                ReplyRecord.root_post_id == root_post_id,
            )
        )
        return [self._domain(record) for record in records]

    async def list_for_discovered_thread(
        self, account_id: UUID, discovered_thread_id: UUID
    ) -> list[ThreadReply]:
        records = await self._session.scalars(
            select(ReplyRecord).where(
                ReplyRecord.account_id == account_id,
                ReplyRecord.discovered_thread_id == discovered_thread_id,
            )
        )
        return [self._domain(record) for record in records]

    @staticmethod
    def _values(reply: ThreadReply) -> dict[str, object]:
        return {
            "id": reply.id,
            "account_id": reply.account_id,
            "threads_reply_id": reply.threads_reply_id,
            "root_post_id": reply.root_post_id,
            "discovered_thread_id": reply.discovered_thread_id,
            "parent_reply_id": reply.parent_reply_id,
            "text": reply.text,
            "replied_at": reply.replied_at,
            "created_at": reply.created_at,
        }

    @staticmethod
    def _domain(record: ReplyRecord) -> ThreadReply:
        return ThreadReply(
            id=record.id,
            account_id=record.account_id,
            threads_reply_id=record.threads_reply_id,
            root_post_id=record.root_post_id,
            parent_reply_id=record.parent_reply_id,
            discovered_thread_id=record.discovered_thread_id,
            text=record.text,
            replied_at=record.replied_at,
            created_at=record.created_at,
        )


class SQLAlchemyDiscoveryRepository(DiscoveryRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_campaign(self, campaign: DiscoveryCampaign) -> None:
        self._session.add(
            DiscoveryCampaignRecord(
                id=campaign.id,
                account_id=campaign.account_id,
                name=campaign.name,
                status=campaign.status,
                created_at=campaign.created_at,
                updated_at=campaign.updated_at,
            )
        )
        await self._session.flush()

    async def get_campaign(self, campaign_id: UUID) -> DiscoveryCampaign | None:
        record = await self._session.get(DiscoveryCampaignRecord, campaign_id)
        return self._campaign_domain(record) if record is not None else None

    async def get_campaign_for_update(self, campaign_id: UUID) -> DiscoveryCampaign | None:
        record = await self._session.scalar(
            select(DiscoveryCampaignRecord)
            .where(DiscoveryCampaignRecord.id == campaign_id)
            .with_for_update()
        )
        return self._campaign_domain(record) if record is not None else None

    async def update_campaign(self, campaign: DiscoveryCampaign) -> None:
        record = await self._session.get(DiscoveryCampaignRecord, campaign.id)
        if record is None:
            raise LookupError("discovery campaign not found")
        record.name = campaign.name
        record.status = campaign.status
        record.updated_at = campaign.updated_at
        await self._session.flush()

    async def get_or_create_query(self, query: SearchQuery) -> SearchQuery:
        fingerprint = hashlib.sha256(query.identity_key().encode("utf-8")).hexdigest()
        statement = (
            postgres_insert(SearchQueryRecord)
            .values(
                id=query.id,
                campaign_id=query.campaign_id,
                kind=query.kind,
                query_text=query.query_text,
                search_mode=query.search_mode,
                search_type=query.search_type,
                username=query.username,
                thread_remote_id=query.thread_remote_id,
                since=query.since,
                until=query.until,
                query_fingerprint=fingerprint,
                created_at=query.created_at,
            )
            .on_conflict_do_nothing(constraint="uq_discovery_query_identity")
            .returning(SearchQueryRecord.id)
        )
        query_id = await self._session.scalar(statement)
        record = await self._session.get(SearchQueryRecord, query_id) if query_id else None
        if record is None:
            record = await self._session.scalar(
                select(SearchQueryRecord).where(
                    SearchQueryRecord.campaign_id == query.campaign_id,
                    SearchQueryRecord.query_fingerprint == fingerprint,
                )
            )
        if record is None:
            raise RuntimeError("discovery query disappeared after upsert")
        return self._query_domain(record)

    async def get_query(self, query_id: UUID) -> SearchQuery | None:
        record = await self._session.get(SearchQueryRecord, query_id)
        return self._query_domain(record) if record is not None else None

    async def add_run_if_absent(self, run: DiscoveryRun) -> bool:
        statement = (
            postgres_insert(DiscoveryRunRecord)
            .values(**self._run_values(run))
            .on_conflict_do_nothing(constraint="uq_discovery_runs_command_id")
            .returning(DiscoveryRunRecord.id)
        )
        return (await self._session.scalar(statement)) is not None

    async def get_run(self, run_id: UUID) -> DiscoveryRun | None:
        record = await self._session.get(DiscoveryRunRecord, run_id)
        return self._run_domain(record) if record is not None else None

    async def get_run_for_update(self, run_id: UUID) -> DiscoveryRun | None:
        record = await self._session.scalar(
            select(DiscoveryRunRecord).where(DiscoveryRunRecord.id == run_id).with_for_update()
        )
        return self._run_domain(record) if record is not None else None

    async def get_run_by_command_id(self, command_id: str) -> DiscoveryRun | None:
        record = await self._session.scalar(
            select(DiscoveryRunRecord).where(DiscoveryRunRecord.command_id == command_id)
        )
        return self._run_domain(record) if record is not None else None

    async def update_run(self, run: DiscoveryRun) -> None:
        record = await self._session.get(DiscoveryRunRecord, run.id)
        if record is None:
            raise LookupError("discovery run not found")
        record.status = run.status
        record.cursor = run.cursor
        record.profile_lookup_complete = run.profile_lookup_complete
        record.pages_processed = run.pages_processed
        record.items_processed = run.items_processed
        record.items_skipped = run.items_skipped
        record.error_code = run.error_code
        record.updated_at = run.updated_at
        record.finished_at = run.finished_at
        await self._session.flush()

    async def add_run_cursor(self, cursor: DiscoveryRunCursor) -> bool:
        statement = (
            postgres_insert(DiscoveryRunCursorRecord)
            .values(
                id=cursor.id,
                run_id=cursor.run_id,
                cursor_digest=cursor.cursor_digest,
                page_number=cursor.page_number,
            )
            .on_conflict_do_nothing(constraint="uq_discovery_run_cursor_digest")
            .returning(DiscoveryRunCursorRecord.id)
        )
        return (await self._session.scalar(statement)) is not None

    async def upsert_author(self, author: DiscoveredAuthor) -> DiscoveredAuthor:
        statement = postgres_insert(DiscoveredAuthorRecord).values(**self._author_values(author))
        excluded = statement.excluded
        statement = statement.on_conflict_do_update(
            constraint="uq_discovered_authors_remote_id",
            set_={
                "username": excluded.username,
                "display_name": func.coalesce(
                    excluded.display_name, DiscoveredAuthorRecord.display_name
                ),
                "biography": func.coalesce(excluded.biography, DiscoveredAuthorRecord.biography),
                "profile_picture_url": func.coalesce(
                    excluded.profile_picture_url, DiscoveredAuthorRecord.profile_picture_url
                ),
                "enrichment_status": case(
                    (
                        excluded.enrichment_status == DiscoveryEnrichmentStatus.ENRICHED,
                        DiscoveryEnrichmentStatus.ENRICHED,
                    ),
                    else_=DiscoveredAuthorRecord.enrichment_status,
                ),
                "last_enriched_at": func.coalesce(
                    excluded.last_enriched_at, DiscoveredAuthorRecord.last_enriched_at
                ),
                "updated_at": excluded.updated_at,
            },
        )
        await self._session.execute(statement)
        record = await self._session.scalar(
            select(DiscoveredAuthorRecord).where(
                DiscoveredAuthorRecord.remote_author_id == author.remote_author_id
            )
        )
        if record is None:
            raise RuntimeError("discovered author disappeared after upsert")
        return self._author_domain(record)

    async def get_author_by_remote_id(self, remote_author_id: str) -> DiscoveredAuthor | None:
        record = await self._session.scalar(
            select(DiscoveredAuthorRecord).where(
                DiscoveredAuthorRecord.remote_author_id == remote_author_id
            )
        )
        return self._author_domain(record) if record is not None else None

    async def get_author_by_username(self, username: str) -> DiscoveredAuthor | None:
        record = await self._session.scalar(
            select(DiscoveredAuthorRecord)
            .where(DiscoveredAuthorRecord.username == username)
            .order_by(DiscoveredAuthorRecord.created_at, DiscoveredAuthorRecord.id)
            .limit(1)
        )
        return self._author_domain(record) if record is not None else None

    async def get_profile_author_for_run(self, run_id: UUID) -> DiscoveredAuthor | None:
        record = await self._session.scalar(
            select(DiscoveredAuthorRecord)
            .join(
                DiscoverySourceEvidenceRecord,
                DiscoverySourceEvidenceRecord.author_id == DiscoveredAuthorRecord.id,
            )
            .where(
                DiscoverySourceEvidenceRecord.run_id == run_id,
                DiscoverySourceEvidenceRecord.source
                == DiscoveryEvidenceSource.PUBLIC_PROFILE_LOOKUP,
            )
            .limit(1)
        )
        return self._author_domain(record) if record is not None else None

    async def attach_author_to_threads_by_username(self, username: str, author_id: UUID) -> None:
        await self._session.execute(
            update(DiscoveredThreadRecord)
            .where(
                DiscoveredThreadRecord.username == username,
                DiscoveredThreadRecord.author_id.is_(None),
            )
            .values(author_id=author_id, updated_at=func.now())
        )
        await self._session.flush()

    async def list_evidence_for_author(self, account_id: UUID, author_id: UUID) -> list[UUID]:
        records = await self._session.scalars(
            select(DiscoverySourceEvidenceRecord.id)
            .join(
                DiscoveryRunRecord,
                DiscoverySourceEvidenceRecord.run_id == DiscoveryRunRecord.id,
            )
            .outerjoin(
                DiscoveredThreadRecord,
                DiscoverySourceEvidenceRecord.thread_id == DiscoveredThreadRecord.id,
            )
            .where(
                DiscoveryRunRecord.account_id == account_id,
                or_(
                    DiscoverySourceEvidenceRecord.author_id == author_id,
                    DiscoveredThreadRecord.author_id == author_id,
                ),
            )
            .distinct()
        )
        return list(records)

    async def upsert_thread(self, thread: DiscoveredThread) -> DiscoveredThread:
        statement = postgres_insert(DiscoveredThreadRecord).values(**self._thread_values(thread))
        excluded = statement.excluded
        statement = statement.on_conflict_do_update(
            constraint="uq_discovered_threads_remote_id",
            set_={
                "author_id": func.coalesce(excluded.author_id, DiscoveredThreadRecord.author_id),
                "username": func.coalesce(excluded.username, DiscoveredThreadRecord.username),
                "text": func.coalesce(excluded.text, DiscoveredThreadRecord.text),
                "permalink": func.coalesce(excluded.permalink, DiscoveredThreadRecord.permalink),
                "media_type": func.coalesce(excluded.media_type, DiscoveredThreadRecord.media_type),
                "remote_created_at": func.coalesce(
                    excluded.remote_created_at, DiscoveredThreadRecord.remote_created_at
                ),
                "is_quote_post": func.coalesce(
                    excluded.is_quote_post, DiscoveredThreadRecord.is_quote_post
                ),
                "has_replies": func.coalesce(
                    excluded.has_replies, DiscoveredThreadRecord.has_replies
                ),
                "enrichment_status": case(
                    (
                        excluded.enrichment_status == DiscoveryEnrichmentStatus.ENRICHED,
                        DiscoveryEnrichmentStatus.ENRICHED,
                    ),
                    else_=DiscoveredThreadRecord.enrichment_status,
                ),
                "updated_at": excluded.updated_at,
            },
        )
        await self._session.execute(statement)
        record = await self._session.scalar(
            select(DiscoveredThreadRecord).where(
                DiscoveredThreadRecord.remote_thread_id == thread.remote_thread_id
            )
        )
        if record is None:
            raise RuntimeError("discovered thread disappeared after upsert")
        return self._thread_domain(record)

    async def get_thread_by_remote_id(self, remote_thread_id: str) -> DiscoveredThread | None:
        record = await self._session.scalar(
            select(DiscoveredThreadRecord).where(
                DiscoveredThreadRecord.remote_thread_id == remote_thread_id
            )
        )
        return self._thread_domain(record) if record is not None else None

    async def update_thread(self, thread: DiscoveredThread) -> None:
        record = await self._session.get(DiscoveredThreadRecord, thread.id)
        if record is None:
            raise LookupError("discovered thread not found")
        record.author_id = thread.author_id
        record.username = thread.username
        record.text = thread.text
        record.permalink = thread.permalink
        record.media_type = thread.media_type
        record.remote_created_at = thread.remote_created_at
        record.is_quote_post = thread.is_quote_post
        record.has_replies = thread.has_replies
        record.enrichment_status = thread.enrichment_status
        record.conversation_status = thread.conversation_status
        record.updated_at = thread.updated_at
        await self._session.flush()

    async def add_evidence(self, evidence: DiscoverySourceEvidence) -> bool:
        values = self._evidence_values(evidence)
        statement = postgres_insert(DiscoverySourceEvidenceRecord).values(**values)
        constraint = (
            "uq_discovery_evidence_thread"
            if evidence.thread_id is not None
            else "uq_discovery_evidence_author"
        )
        result = await self._session.scalar(
            statement.on_conflict_do_nothing(constraint=constraint).returning(
                DiscoverySourceEvidenceRecord.id
            )
        )
        if result is not None:
            return True
        record = await self._session.scalar(
            select(DiscoverySourceEvidenceRecord).where(
                DiscoverySourceEvidenceRecord.run_id == evidence.run_id,
                DiscoverySourceEvidenceRecord.source == evidence.source,
                DiscoverySourceEvidenceRecord.page_number == evidence.page_number,
                DiscoverySourceEvidenceRecord.thread_id == evidence.thread_id,
                DiscoverySourceEvidenceRecord.author_id == evidence.author_id,
            )
        )
        if record is not None:
            evidence.id = record.id
        return False

    async def add_candidate_if_absent(self, candidate: LeadCandidate) -> bool:
        statement = (
            postgres_insert(LeadCandidateRecord)
            .values(**self._candidate_values(candidate))
            .on_conflict_do_nothing(constraint="uq_lead_candidate_account_author")
            .returning(LeadCandidateRecord.id)
        )
        return (await self._session.scalar(statement)) is not None

    async def get_candidate_for_update(self, candidate_id: UUID) -> LeadCandidate | None:
        record = await self._session.scalar(
            select(LeadCandidateRecord)
            .where(LeadCandidateRecord.id == candidate_id)
            .with_for_update()
        )
        return self._candidate_domain(record) if record is not None else None

    async def get_candidate_by_author(
        self, account_id: UUID, author_id: UUID
    ) -> LeadCandidate | None:
        record = await self._session.scalar(
            select(LeadCandidateRecord).where(
                LeadCandidateRecord.account_id == account_id,
                LeadCandidateRecord.author_id == author_id,
            )
        )
        return self._candidate_domain(record) if record is not None else None

    async def update_candidate(self, candidate: LeadCandidate) -> None:
        record = await self._session.get(LeadCandidateRecord, candidate.id)
        if record is None:
            raise LookupError("lead candidate not found")
        record.status = candidate.status
        record.updated_at = candidate.updated_at
        await self._session.flush()

    async def add_candidate_evidence(self, link: LeadCandidateEvidence) -> bool:
        statement = (
            postgres_insert(LeadCandidateEvidenceRecord)
            .values(id=link.id, candidate_id=link.candidate_id, evidence_id=link.evidence_id)
            .on_conflict_do_nothing(constraint="uq_lead_candidate_evidence")
            .returning(LeadCandidateEvidenceRecord.id)
        )
        return (await self._session.scalar(statement)) is not None

    async def add_candidate_transition(self, transition: LeadCandidateTransition) -> None:
        self._session.add(
            LeadCandidateTransitionRecord(
                id=uuid4(),
                candidate_id=transition.candidate_id,
                command_id=transition.command_id,
                previous_status=transition.previous_status,
                next_status=transition.next_status,
                reason_code=transition.reason_code,
                occurred_at=transition.occurred_at,
            )
        )
        await self._session.flush()

    async def has_candidate_transition(self, command_id: str) -> bool:
        return (
            await self._session.scalar(
                select(LeadCandidateTransitionRecord.id).where(
                    LeadCandidateTransitionRecord.command_id == command_id
                )
            )
        ) is not None

    @staticmethod
    def _campaign_domain(record: DiscoveryCampaignRecord) -> DiscoveryCampaign:
        return DiscoveryCampaign(
            id=record.id,
            account_id=record.account_id,
            name=record.name,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _query_domain(record: SearchQueryRecord) -> SearchQuery:
        return SearchQuery(
            id=record.id,
            campaign_id=record.campaign_id,
            kind=record.kind,
            query_text=record.query_text,
            search_mode=record.search_mode,
            search_type=record.search_type,
            username=record.username,
            thread_remote_id=record.thread_remote_id,
            since=record.since,
            until=record.until,
            created_at=record.created_at,
        )

    @staticmethod
    def _run_values(run: DiscoveryRun) -> dict[str, object]:
        return {
            "id": run.id,
            "account_id": run.account_id,
            "campaign_id": run.campaign_id,
            "query_id": run.query_id,
            "command_id": run.command_id,
            "status": run.status,
            "cursor": run.cursor,
            "profile_lookup_complete": run.profile_lookup_complete,
            "pages_processed": run.pages_processed,
            "items_processed": run.items_processed,
            "items_skipped": run.items_skipped,
            "error_code": run.error_code,
            "started_at": run.started_at,
            "updated_at": run.updated_at,
            "finished_at": run.finished_at,
        }

    @classmethod
    def _run_domain(cls, record: DiscoveryRunRecord) -> DiscoveryRun:
        return DiscoveryRun(
            id=record.id,
            account_id=record.account_id,
            campaign_id=record.campaign_id,
            query_id=record.query_id,
            command_id=record.command_id,
            status=record.status,
            cursor=record.cursor,
            profile_lookup_complete=record.profile_lookup_complete,
            pages_processed=record.pages_processed,
            items_processed=record.items_processed,
            items_skipped=record.items_skipped,
            error_code=record.error_code,
            started_at=record.started_at,
            updated_at=record.updated_at,
            finished_at=record.finished_at,
        )

    @staticmethod
    def _author_values(author: DiscoveredAuthor) -> dict[str, object]:
        return {
            "id": author.id,
            "remote_author_id": author.remote_author_id,
            "username": author.username,
            "display_name": author.display_name,
            "biography": author.biography,
            "profile_picture_url": author.profile_picture_url,
            "enrichment_status": author.enrichment_status,
            "last_enriched_at": author.last_enriched_at,
            "created_at": author.created_at,
            "updated_at": author.updated_at,
        }

    @staticmethod
    def _author_domain(record: DiscoveredAuthorRecord) -> DiscoveredAuthor:
        return DiscoveredAuthor(
            id=record.id,
            remote_author_id=record.remote_author_id,
            username=record.username,
            display_name=record.display_name,
            biography=record.biography,
            profile_picture_url=record.profile_picture_url,
            enrichment_status=record.enrichment_status,
            last_enriched_at=record.last_enriched_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _thread_values(thread: DiscoveredThread) -> dict[str, object]:
        return {
            "id": thread.id,
            "remote_thread_id": thread.remote_thread_id,
            "author_id": thread.author_id,
            "username": thread.username,
            "text": thread.text,
            "permalink": thread.permalink,
            "media_type": thread.media_type,
            "remote_created_at": thread.remote_created_at,
            "is_quote_post": thread.is_quote_post,
            "has_replies": thread.has_replies,
            "enrichment_status": thread.enrichment_status,
            "conversation_status": thread.conversation_status,
            "created_at": thread.created_at,
            "updated_at": thread.updated_at,
        }

    @staticmethod
    def _thread_domain(record: DiscoveredThreadRecord) -> DiscoveredThread:
        return DiscoveredThread(
            id=record.id,
            remote_thread_id=record.remote_thread_id,
            author_id=record.author_id,
            username=record.username,
            text=record.text,
            permalink=record.permalink,
            media_type=record.media_type,
            remote_created_at=record.remote_created_at,
            is_quote_post=record.is_quote_post,
            has_replies=record.has_replies,
            enrichment_status=record.enrichment_status,
            conversation_status=record.conversation_status,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _evidence_values(evidence: DiscoverySourceEvidence) -> dict[str, object]:
        return {
            "id": evidence.id,
            "run_id": evidence.run_id,
            "thread_id": evidence.thread_id,
            "author_id": evidence.author_id,
            "source": evidence.source,
            "page_number": evidence.page_number,
            "observed_at": evidence.observed_at,
            "evidence_class": evidence.evidence_class,
        }

    @staticmethod
    def _candidate_values(candidate: LeadCandidate) -> dict[str, object]:
        return {
            "id": candidate.id,
            "account_id": candidate.account_id,
            "author_id": candidate.author_id,
            "status": candidate.status,
            "created_at": candidate.created_at,
            "updated_at": candidate.updated_at,
        }

    @staticmethod
    def _candidate_domain(record: LeadCandidateRecord) -> LeadCandidate:
        return LeadCandidate(
            id=record.id,
            account_id=record.account_id,
            author_id=record.author_id,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SQLAlchemySyncStateRepository(SyncStateRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, account_id: UUID, sync_type: str) -> SyncState | None:
        record = await self._session.scalar(
            select(SyncStateRecord).where(
                SyncStateRecord.account_id == account_id,
                SyncStateRecord.sync_type == sync_type,
            )
        )
        if record is None:
            return None
        return SyncState(
            id=record.id,
            account_id=record.account_id,
            sync_type=record.sync_type,
            cursor=record.cursor,
            last_synced_at=record.last_synced_at,
            updated_at=record.updated_at,
        )

    async def advance_if_current(self, state: SyncState, expected_cursor: str | None) -> bool:
        condition = (
            SyncStateRecord.cursor.is_(None)
            if expected_cursor is None
            else SyncStateRecord.cursor == expected_cursor
        )
        updated_id = await self._session.scalar(
            update(SyncStateRecord)
            .where(
                SyncStateRecord.account_id == state.account_id,
                SyncStateRecord.sync_type == state.sync_type,
                condition,
            )
            .values(
                cursor=state.cursor,
                last_synced_at=state.last_synced_at,
                updated_at=state.updated_at,
            )
            .returning(SyncStateRecord.id)
        )
        if updated_id is not None:
            return True

        if expected_cursor is not None:
            return False
        statement = (
            postgres_insert(SyncStateRecord)
            .values(
                id=state.id,
                account_id=state.account_id,
                sync_type=state.sync_type,
                cursor=state.cursor,
                last_synced_at=state.last_synced_at,
                updated_at=state.updated_at,
            )
            .on_conflict_do_nothing(constraint="uq_sync_states_account_type")
            .returning(SyncStateRecord.id)
        )
        return (await self._session.scalar(statement)) is not None


class SQLAlchemyCommandAttemptRepository(CommandAttemptRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, attempt: CommandAttempt) -> None:
        self._session.add(
            CommandAttemptRecord(
                id=attempt.id,
                command_id=attempt.command_id,
                attempt_number=attempt.attempt_number,
                status=attempt.status,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                error_code=attempt.error_code,
            )
        )
        await self._session.flush()

    async def count_for_command(self, command_id: str) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(CommandAttemptRecord)
            .where(CommandAttemptRecord.command_id == command_id)
        )
        return int(count or 0)

    async def get_processing_for_command_for_update(self, command_id: str) -> CommandAttempt | None:
        record = await self._session.scalar(
            select(CommandAttemptRecord)
            .where(
                CommandAttemptRecord.command_id == command_id,
                CommandAttemptRecord.status == AttemptStatus.PROCESSING,
            )
            .order_by(CommandAttemptRecord.attempt_number.desc())
            .with_for_update()
            .limit(1)
        )
        if record is None:
            return None
        return CommandAttempt(
            id=record.id,
            command_id=record.command_id,
            attempt_number=record.attempt_number,
            status=AttemptStatus(record.status),
            started_at=record.started_at,
            finished_at=record.finished_at,
            error_code=record.error_code,
        )

    async def update(self, attempt: CommandAttempt) -> None:
        record = await self._session.get(CommandAttemptRecord, attempt.id)
        if record is None:
            raise LookupError(f"command attempt not found: {attempt.id}")
        record.status = attempt.status
        record.finished_at = attempt.finished_at
        record.error_code = attempt.error_code
        await self._session.flush()


class SQLAlchemyOutboxEventRepository(OutboxEventRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: OutboxEvent) -> None:
        self._session.add(self._record(event))
        await self._session.flush()

    async def get(self, event_id: UUID) -> OutboxEvent | None:
        record = await self._session.get(OutboxEventRecord, event_id)
        return self.to_domain(record) if record is not None else None

    async def update(self, event: OutboxEvent) -> None:
        record = await self._session.get(OutboxEventRecord, event.id)
        if record is None:
            raise LookupError(f"outbox event not found: {event.id}")
        record.status = event.status
        record.attempt_count = event.attempt_count
        record.available_at = event.available_at
        record.delivered_at = event.delivered_at
        await self._session.flush()

    @staticmethod
    def _record(event: OutboxEvent) -> OutboxEventRecord:
        return OutboxEventRecord(
            id=event.id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            correlation_id=event.correlation_id,
            payload=event.payload,
            status=event.status,
            attempt_count=event.attempt_count,
            created_at=event.created_at,
            available_at=event.available_at,
            delivered_at=event.delivered_at,
        )

    @staticmethod
    def to_domain(record: OutboxEventRecord) -> OutboxEvent:
        return OutboxEvent(
            id=record.id,
            aggregate_type=record.aggregate_type,
            aggregate_id=record.aggregate_id,
            event_type=record.event_type,
            correlation_id=record.correlation_id,
            payload=record.payload,
            status=OutboxStatus(record.status),
            attempt_count=record.attempt_count,
            created_at=record.created_at,
            available_at=record.available_at,
            delivered_at=record.delivered_at,
        )


class SQLAlchemyIntegrationDeliveryRepository(IntegrationDeliveryRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, delivery: IntegrationDelivery) -> None:
        self._session.add(self._record(delivery))
        await self._session.flush()

    async def claim_next(
        self,
        destination: str,
        now: datetime,
        lease_token: UUID,
        lease_expires_at: datetime,
    ) -> tuple[IntegrationDelivery, OutboxEvent] | None:
        due_or_abandoned = or_(
            and_(
                IntegrationDeliveryRecord.status.in_(
                    [DeliveryStatus.PENDING, DeliveryStatus.FAILED_RETRYABLE]
                ),
                IntegrationDeliveryRecord.next_attempt_at <= now,
            ),
            and_(
                IntegrationDeliveryRecord.status == DeliveryStatus.PROCESSING,
                IntegrationDeliveryRecord.lease_expires_at <= now,
            ),
        )
        result = await self._session.execute(
            select(IntegrationDeliveryRecord, OutboxEventRecord)
            .join(OutboxEventRecord, OutboxEventRecord.id == IntegrationDeliveryRecord.event_id)
            .where(
                IntegrationDeliveryRecord.destination == destination,
                OutboxEventRecord.status == OutboxStatus.PENDING,
                due_or_abandoned,
            )
            .order_by(IntegrationDeliveryRecord.next_attempt_at, IntegrationDeliveryRecord.id)
            .with_for_update(skip_locked=True, of=IntegrationDeliveryRecord)
            .limit(1)
        )
        row = result.first()
        if row is None:
            return None
        delivery_record, event_record = row
        delivery_record.status = DeliveryStatus.PROCESSING
        delivery_record.attempt_count += 1
        delivery_record.lease_token = lease_token
        delivery_record.lease_expires_at = lease_expires_at
        delivery_record.updated_at = now
        claimed_delivery = self._domain(delivery_record)
        claimed_event = SQLAlchemyOutboxEventRepository.to_domain(event_record)
        await self._session.flush()
        return claimed_delivery, claimed_event

    async def get_for_update(self, delivery_id: UUID) -> IntegrationDelivery | None:
        record = await self._session.scalar(
            select(IntegrationDeliveryRecord)
            .where(IntegrationDeliveryRecord.id == delivery_id)
            .with_for_update()
        )
        return self._domain(record) if record is not None else None

    async def update(self, delivery: IntegrationDelivery) -> None:
        record = await self._session.get(IntegrationDeliveryRecord, delivery.id)
        if record is None:
            raise LookupError(f"integration delivery not found: {delivery.id}")
        record.status = delivery.status
        record.attempt_count = delivery.attempt_count
        record.next_attempt_at = delivery.next_attempt_at
        record.delivery_deadline_at = delivery.delivery_deadline_at
        record.lease_token = delivery.lease_token
        record.lease_expires_at = delivery.lease_expires_at
        record.delivered_at = delivery.delivered_at
        record.remote_delivery_id = delivery.remote_delivery_id
        record.error_code = delivery.error_code
        record.updated_at = delivery.updated_at
        await self._session.flush()

    @staticmethod
    def _record(delivery: IntegrationDelivery) -> IntegrationDeliveryRecord:
        return IntegrationDeliveryRecord(
            id=delivery.id,
            event_id=delivery.event_id,
            destination=delivery.destination,
            status=delivery.status,
            attempt_count=delivery.attempt_count,
            next_attempt_at=delivery.next_attempt_at,
            delivery_deadline_at=delivery.delivery_deadline_at,
            lease_token=delivery.lease_token,
            lease_expires_at=delivery.lease_expires_at,
            delivered_at=delivery.delivered_at,
            remote_delivery_id=delivery.remote_delivery_id,
            error_code=delivery.error_code,
            created_at=delivery.created_at,
            updated_at=delivery.updated_at,
        )

    @staticmethod
    def _domain(record: IntegrationDeliveryRecord) -> IntegrationDelivery:
        return IntegrationDelivery(
            id=record.id,
            event_id=record.event_id,
            destination=record.destination,
            status=DeliveryStatus(record.status),
            attempt_count=record.attempt_count,
            next_attempt_at=record.next_attempt_at,
            delivery_deadline_at=record.delivery_deadline_at,
            lease_token=record.lease_token,
            lease_expires_at=record.lease_expires_at,
            delivered_at=record.delivered_at,
            remote_delivery_id=record.remote_delivery_id,
            error_code=record.error_code,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SQLAlchemyWorkerRepository(WorkerRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, worker: WorkerNode) -> None:
        self._session.add(self._record(worker))
        await self._session.flush()

    async def get(self, worker_id: UUID) -> WorkerNode | None:
        record = await self._session.get(WorkerNodeRecord, worker_id)
        return self._domain(record) if record is not None else None

    async def get_for_update(self, worker_id: UUID) -> WorkerNode | None:
        record = await self._session.scalar(
            select(WorkerNodeRecord)
            .where(WorkerNodeRecord.worker_id == worker_id)
            .with_for_update()
        )
        return self._domain(record) if record is not None else None

    async def update(self, worker: WorkerNode) -> None:
        record = await self._session.get(WorkerNodeRecord, worker.worker_id)
        if record is None:
            raise LookupError(f"worker not found: {worker.worker_id}")
        record.display_name = worker.display_name
        record.hostname = worker.hostname
        record.platform = worker.platform
        record.agent_version = worker.agent_version
        record.protocol_version = worker.protocol_version
        record.capabilities_schema_version = worker.capabilities_schema_version
        record.public_key = worker.public_key
        record.status = worker.status
        record.max_concurrent_jobs = worker.max_concurrent_jobs
        record.max_browser_sessions = worker.max_browser_sessions
        record.active_browser_sessions = worker.active_browser_sessions
        record.last_heartbeat_at = worker.last_heartbeat_at
        record.presence_expires_at = worker.presence_expires_at
        record.updated_at = worker.updated_at
        await self._session.flush()

    async def list_expired_presence(self, now: datetime) -> list[WorkerNode]:
        result = await self._session.scalars(
            select(WorkerNodeRecord).where(
                WorkerNodeRecord.status.in_([WorkerStatus.ONLINE, WorkerStatus.DEGRADED]),
                WorkerNodeRecord.presence_expires_at <= now,
            )
        )
        return [self._domain(record) for record in result]

    @staticmethod
    def _record(worker: WorkerNode) -> WorkerNodeRecord:
        return WorkerNodeRecord(
            worker_id=worker.worker_id,
            display_name=worker.display_name,
            hostname=worker.hostname,
            platform=worker.platform,
            agent_version=worker.agent_version,
            protocol_version=worker.protocol_version,
            capabilities_schema_version=worker.capabilities_schema_version,
            public_key=worker.public_key,
            status=worker.status,
            max_concurrent_jobs=worker.max_concurrent_jobs,
            max_browser_sessions=worker.max_browser_sessions,
            active_browser_sessions=worker.active_browser_sessions,
            last_heartbeat_at=worker.last_heartbeat_at,
            presence_expires_at=worker.presence_expires_at,
            created_at=worker.created_at,
            updated_at=worker.updated_at,
        )

    @staticmethod
    def _domain(record: WorkerNodeRecord) -> WorkerNode:
        return WorkerNode(
            worker_id=record.worker_id,
            display_name=record.display_name,
            hostname=record.hostname,
            platform=record.platform,
            agent_version=record.agent_version,
            protocol_version=record.protocol_version,
            capabilities_schema_version=record.capabilities_schema_version,
            public_key=record.public_key,
            status=WorkerStatus(record.status),
            max_concurrent_jobs=record.max_concurrent_jobs,
            max_browser_sessions=record.max_browser_sessions,
            active_browser_sessions=record.active_browser_sessions,
            last_heartbeat_at=record.last_heartbeat_at,
            presence_expires_at=record.presence_expires_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SQLAlchemyWorkerCapabilityRepository(WorkerCapabilityRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_worker(
        self, worker_id: UUID, capabilities: list[WorkerCapability]
    ) -> None:
        await self._session.execute(
            delete(WorkerCapabilityRecord).where(WorkerCapabilityRecord.worker_id == worker_id)
        )
        self._session.add_all(
            [
                WorkerCapabilityRecord(
                    id=capability.id,
                    worker_id=worker_id,
                    capability_name=capability.name,
                    capability_version=capability.version,
                    metadata_json=capability.metadata,
                    advertised_at=capability.advertised_at,
                )
                for capability in capabilities
            ]
        )
        await self._session.flush()

    async def list_for_worker(self, worker_id: UUID) -> list[WorkerCapability]:
        result = await self._session.scalars(
            select(WorkerCapabilityRecord)
            .where(WorkerCapabilityRecord.worker_id == worker_id)
            .order_by(
                WorkerCapabilityRecord.capability_name, WorkerCapabilityRecord.capability_version
            )
        )
        return [self._domain(record) for record in result]

    async def has(self, worker_id: UUID, name: str, version: int) -> bool:
        capability_id = await self._session.scalar(
            select(WorkerCapabilityRecord.id).where(
                WorkerCapabilityRecord.worker_id == worker_id,
                WorkerCapabilityRecord.capability_name == name,
                WorkerCapabilityRecord.capability_version == version,
            )
        )
        return capability_id is not None

    async def list_worker_ids(self, name: str, version: int) -> list[UUID]:
        result = await self._session.scalars(
            select(WorkerCapabilityRecord.worker_id)
            .where(
                WorkerCapabilityRecord.capability_name == name,
                WorkerCapabilityRecord.capability_version == version,
            )
            .order_by(WorkerCapabilityRecord.worker_id)
        )
        return list(result)

    @staticmethod
    def _domain(record: WorkerCapabilityRecord) -> WorkerCapability:
        return WorkerCapability(
            id=record.id,
            worker_id=record.worker_id,
            name=record.capability_name,
            version=record.capability_version,
            metadata=record.metadata_json,
            advertised_at=record.advertised_at,
        )


class SQLAlchemyBrowserProfileRepository(BrowserProfileRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, profile: BrowserProfile) -> None:
        self._session.add(
            BrowserProfileRecord(
                id=profile.id,
                worker_id=profile.worker_id,
                profile_ref=profile.profile_ref,
                display_name=profile.display_name,
                metadata_json=profile.metadata,
                created_at=profile.created_at,
                updated_at=profile.updated_at,
            )
        )
        await self._session.flush()

    async def get(self, worker_id: UUID, profile_ref: str) -> BrowserProfile | None:
        record = await self._session.scalar(
            select(BrowserProfileRecord).where(
                BrowserProfileRecord.worker_id == worker_id,
                BrowserProfileRecord.profile_ref == profile_ref,
            )
        )
        return self._domain(record) if record is not None else None

    @staticmethod
    def _domain(record: BrowserProfileRecord) -> BrowserProfile:
        return BrowserProfile(
            id=record.id,
            worker_id=record.worker_id,
            profile_ref=record.profile_ref,
            display_name=record.display_name,
            metadata=record.metadata_json,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SQLAlchemyWorkerAccountSessionRepository(WorkerAccountSessionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, session: WorkerAccountSession) -> None:
        self._session.add(
            WorkerAccountSessionRecord(
                account_id=session.account_id,
                worker_id=session.worker_id,
                profile_ref=session.profile_ref,
                session_id=session.session_id,
                state=session.state,
                intervention_required=session.requires_intervention,
                revision=session.revision,
                updated_at=session.updated_at,
            )
        )
        await self._session.flush()

    async def get(self, account_id: UUID) -> WorkerAccountSession | None:
        record = await self._session.get(WorkerAccountSessionRecord, account_id)
        return self._domain(record) if record is not None else None

    async def get_for_update(self, account_id: UUID) -> WorkerAccountSession | None:
        record = await self._session.scalar(
            select(WorkerAccountSessionRecord)
            .where(WorkerAccountSessionRecord.account_id == account_id)
            .with_for_update()
        )
        return self._domain(record) if record is not None else None

    async def update(self, session: WorkerAccountSession) -> None:
        record = await self._session.get(WorkerAccountSessionRecord, session.account_id)
        if record is None:
            raise LookupError(f"worker account session not found: {session.account_id}")
        record.worker_id = session.worker_id
        record.profile_ref = session.profile_ref
        record.session_id = session.session_id
        record.state = session.state
        record.intervention_required = session.requires_intervention
        record.revision = session.revision
        record.updated_at = session.updated_at
        await self._session.flush()

    @staticmethod
    def _domain(record: WorkerAccountSessionRecord) -> WorkerAccountSession:
        return WorkerAccountSession(
            account_id=record.account_id,
            worker_id=record.worker_id,
            profile_ref=record.profile_ref,
            session_id=record.session_id,
            state=BrowserSessionState(record.state),
            revision=record.revision,
            updated_at=record.updated_at,
        )


class SQLAlchemyNetworkProfileRepository(NetworkProfileRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, profile: NetworkProfile) -> None:
        self._session.add(
            NetworkProfileRecord(
                id=profile.id,
                account_id=profile.account_id,
                name=profile.name,
                protocol=profile.protocol,
                host=profile.host,
                port=profile.port,
                credential_ref=profile.credential_ref,
                created_at=profile.created_at,
                updated_at=profile.updated_at,
            )
        )
        await self._session.flush()

    async def get(self, account_id: UUID, profile_id: UUID) -> NetworkProfile | None:
        record = await self._session.scalar(
            select(NetworkProfileRecord).where(
                NetworkProfileRecord.account_id == account_id,
                NetworkProfileRecord.id == profile_id,
            )
        )
        return self._domain(record) if record is not None else None

    @staticmethod
    def _domain(record: NetworkProfileRecord) -> NetworkProfile:
        return NetworkProfile(
            id=record.id,
            account_id=record.account_id,
            name=record.name,
            protocol=NetworkProtocol(record.protocol),
            host=record.host,
            port=record.port,
            credential_ref=record.credential_ref,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SQLAlchemyAccountWorkerAssignmentRepository(AccountWorkerAssignmentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, assignment: AccountWorkerAssignment) -> None:
        await self._session.scalar(
            select(AccountRecord).where(AccountRecord.id == assignment.account_id).with_for_update()
        )
        self._session.add(
            AccountWorkerAssignmentRecord(
                id=assignment.id,
                account_id=assignment.account_id,
                worker_id=assignment.worker_id,
                profile_ref=assignment.profile_ref,
                network_profile_id=assignment.network_profile_id,
                is_active=assignment.is_active,
                assigned_at=assignment.assigned_at,
                ended_at=assignment.ended_at,
            )
        )
        await self._session.flush()

    async def get_active(self, account_id: UUID) -> AccountWorkerAssignment | None:
        record = await self._session.scalar(
            select(AccountWorkerAssignmentRecord).where(
                AccountWorkerAssignmentRecord.account_id == account_id,
                AccountWorkerAssignmentRecord.is_active.is_(True),
            )
        )
        return self._domain(record) if record is not None else None

    async def update(self, assignment: AccountWorkerAssignment) -> None:
        record = await self._session.get(AccountWorkerAssignmentRecord, assignment.id)
        if record is None:
            raise LookupError(f"account-worker assignment not found: {assignment.id}")
        await self._session.scalar(
            select(AccountRecord).where(AccountRecord.id == record.account_id).with_for_update()
        )
        record.is_active = assignment.is_active
        record.ended_at = assignment.ended_at
        await self._session.flush()

    @staticmethod
    def _domain(record: AccountWorkerAssignmentRecord) -> AccountWorkerAssignment:
        return AccountWorkerAssignment(
            id=record.id,
            account_id=record.account_id,
            worker_id=record.worker_id,
            profile_ref=record.profile_ref,
            network_profile_id=record.network_profile_id,
            is_active=record.is_active,
            assigned_at=record.assigned_at,
            ended_at=record.ended_at,
        )


class SQLAlchemyWorkerSecurityRepository(WorkerSecurityRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_enrollment(self, enrollment: WorkerEnrollment) -> None:
        self._session.add(
            WorkerEnrollmentRecord(
                id=enrollment.id,
                token_digest=enrollment.token_digest,
                created_at=enrollment.created_at,
                expires_at=enrollment.expires_at,
                consumed_at=enrollment.consumed_at,
                created_by=enrollment.created_by,
            )
        )
        await self._session.flush()

    async def get_enrollment_for_update(self, token_digest: str) -> WorkerEnrollment | None:
        record = await self._session.scalar(
            select(WorkerEnrollmentRecord)
            .where(WorkerEnrollmentRecord.token_digest == token_digest)
            .with_for_update()
        )
        return self._enrollment_domain(record) if record is not None else None

    async def update_enrollment(self, enrollment: WorkerEnrollment) -> None:
        record = await self._session.get(WorkerEnrollmentRecord, enrollment.id)
        if record is None:
            raise LookupError(f"worker enrollment not found: {enrollment.id}")
        record.consumed_at = enrollment.consumed_at
        await self._session.flush()

    async def add_challenge(self, challenge: WorkerAuthChallenge) -> None:
        self._session.add(
            WorkerAuthChallengeRecord(
                id=challenge.id,
                worker_id=challenge.worker_id,
                nonce=challenge.nonce,
                issued_at=challenge.issued_at,
                expires_at=challenge.expires_at,
                used_at=challenge.used_at,
            )
        )
        await self._session.flush()

    async def get_challenge_for_update(self, challenge_id: UUID) -> WorkerAuthChallenge | None:
        record = await self._session.scalar(
            select(WorkerAuthChallengeRecord)
            .where(WorkerAuthChallengeRecord.id == challenge_id)
            .with_for_update()
        )
        return self._challenge_domain(record) if record is not None else None

    async def update_challenge(self, challenge: WorkerAuthChallenge) -> None:
        record = await self._session.get(WorkerAuthChallengeRecord, challenge.id)
        if record is None:
            raise LookupError(f"worker auth challenge not found: {challenge.id}")
        record.used_at = challenge.used_at
        await self._session.flush()

    async def add_session(self, session: WorkerSession) -> None:
        self._session.add(
            WorkerSessionRecord(
                id=session.id,
                worker_id=session.worker_id,
                token_digest=session.token_digest,
                issued_at=session.issued_at,
                expires_at=session.expires_at,
                revoked_at=session.revoked_at,
            )
        )
        await self._session.flush()

    async def get_active_session(self, token_digest: str, now: datetime) -> WorkerSession | None:
        record = await self._session.scalar(
            select(WorkerSessionRecord).where(
                WorkerSessionRecord.token_digest == token_digest,
                WorkerSessionRecord.expires_at > now,
                WorkerSessionRecord.revoked_at.is_(None),
            )
        )
        return self._session_domain(record) if record is not None else None

    async def add_audit_event(self, event: WorkerAuditEvent) -> None:
        self._session.add(
            WorkerAuditEventRecord(
                id=event.id,
                worker_id=event.worker_id,
                enrollment_id=event.enrollment_id,
                event_type=event.event_type,
                detail_code=event.detail_code,
                created_at=event.created_at,
            )
        )
        await self._session.flush()

    async def list_audit_events(self, worker_id: UUID) -> list[WorkerAuditEvent]:
        result = await self._session.scalars(
            select(WorkerAuditEventRecord)
            .where(WorkerAuditEventRecord.worker_id == worker_id)
            .order_by(WorkerAuditEventRecord.created_at, WorkerAuditEventRecord.id)
        )
        return [self._audit_domain(record) for record in result]

    @staticmethod
    def _enrollment_domain(record: WorkerEnrollmentRecord) -> WorkerEnrollment:
        return WorkerEnrollment(
            id=record.id,
            token_digest=record.token_digest,
            created_at=record.created_at,
            expires_at=record.expires_at,
            consumed_at=record.consumed_at,
            created_by=record.created_by,
        )

    @staticmethod
    def _challenge_domain(record: WorkerAuthChallengeRecord) -> WorkerAuthChallenge:
        return WorkerAuthChallenge(
            id=record.id,
            worker_id=record.worker_id,
            nonce=record.nonce,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            used_at=record.used_at,
        )

    @staticmethod
    def _session_domain(record: WorkerSessionRecord) -> WorkerSession:
        return WorkerSession(
            id=record.id,
            worker_id=record.worker_id,
            token_digest=record.token_digest,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            revoked_at=record.revoked_at,
        )

    @staticmethod
    def _audit_domain(record: WorkerAuditEventRecord) -> WorkerAuditEvent:
        return WorkerAuditEvent(
            id=record.id,
            worker_id=record.worker_id,
            enrollment_id=record.enrollment_id,
            event_type=record.event_type,
            detail_code=record.detail_code,
            created_at=record.created_at,
        )


class SQLAlchemyWorkerJobRepository(WorkerJobRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, job: WorkerJob) -> None:
        self._session.add(self._record(job))
        await self._session.flush()

    async def get(self, job_id: UUID) -> WorkerJob | None:
        record = await self._session.get(WorkerJobRecord, job_id)
        return self._domain(record) if record is not None else None

    async def get_for_update(self, job_id: UUID) -> WorkerJob | None:
        record = await self._session.scalar(
            select(WorkerJobRecord).where(WorkerJobRecord.id == job_id).with_for_update()
        )
        return self._domain(record) if record is not None else None

    async def get_by_command_id(self, command_id: str) -> WorkerJob | None:
        record = await self._session.scalar(
            select(WorkerJobRecord).where(WorkerJobRecord.command_id == command_id)
        )
        return self._domain(record) if record is not None else None

    async def update(self, job: WorkerJob) -> None:
        record = await self._session.get(WorkerJobRecord, job.id)
        if record is None:
            raise LookupError(f"WorkerJob not found: {job.id}")
        self._write(record, job)
        await self._session.flush()

    async def list_claimable(
        self,
        worker: WorkerNode,
        now: datetime,
        limit: int = 50,
    ) -> list[WorkerJob]:
        occurred_at = normalize_utc(now)
        if limit < 1:
            raise ValueError("WorkerJob claim limit must be positive")
        if (
            worker.status is not WorkerStatus.ONLINE
            or worker.presence_expires_at is None
            or worker.presence_expires_at <= occurred_at
        ):
            return []
        active_count = await self._session.scalar(
            select(func.count())
            .select_from(WorkerJobRecord)
            .where(
                WorkerJobRecord.lease_worker_id == worker.worker_id,
                WorkerJobRecord.status == WorkerJobStatus.RUNNING,
                WorkerJobRecord.lease_expires_at > occurred_at,
            )
        )
        if (active_count or 0) >= worker.max_concurrent_jobs:
            return []

        has_capability = exists(
            select(WorkerCapabilityRecord.id).where(
                WorkerCapabilityRecord.worker_id == worker.worker_id,
                WorkerCapabilityRecord.capability_name == WorkerJobRecord.capability_name,
                WorkerCapabilityRecord.capability_version == WorkerJobRecord.capability_version,
            )
        )
        has_current_assignment = exists(
            select(AccountWorkerAssignmentRecord.id).where(
                AccountWorkerAssignmentRecord.account_id == WorkerJobRecord.account_id,
                AccountWorkerAssignmentRecord.worker_id == worker.worker_id,
                AccountWorkerAssignmentRecord.is_active.is_(True),
            )
        )
        available_status = or_(
            and_(
                WorkerJobRecord.status.in_(
                    [WorkerJobStatus.QUEUED, WorkerJobStatus.FAILED_RETRYABLE]
                ),
                WorkerJobRecord.scheduled_at <= occurred_at,
            ),
            and_(
                WorkerJobRecord.status == WorkerJobStatus.RUNNING,
                WorkerJobRecord.lease_expires_at <= occurred_at,
            ),
        )
        candidates = await self._session.scalars(
            select(WorkerJobRecord)
            .where(
                available_status,
                or_(
                    WorkerJobRecord.deadline_at.is_(None),
                    WorkerJobRecord.deadline_at > occurred_at,
                ),
                WorkerJobRecord.attempt_count < WorkerJobRecord.max_attempts,
                or_(
                    WorkerJobRecord.attempt_count == 0,
                    WorkerJobRecord.retry_safety == WorkerJobRetrySafety.SAFE_TO_RETRY,
                    WorkerJobRecord.retry_authorized_by_operator.is_(True),
                ),
                or_(
                    WorkerJobRecord.assigned_worker_id.is_(None),
                    WorkerJobRecord.assigned_worker_id == worker.worker_id,
                ),
                or_(
                    WorkerJobRecord.account_affinity_required.is_(False),
                    has_current_assignment,
                ),
                has_capability,
            )
            .order_by(
                WorkerJobRecord.priority.desc(),
                WorkerJobRecord.scheduled_at,
                WorkerJobRecord.created_at,
                WorkerJobRecord.id,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        return [self._domain(record) for record in candidates]

    async def claim(
        self,
        job: WorkerJob,
        worker_id: UUID,
        now: datetime,
        lease_expires_at: datetime,
        lease_token: UUID,
        account_coordination_generation: int | None,
    ) -> WorkerJob | None:
        occurred_at = normalize_utc(now)
        record = await self._session.scalar(
            select(WorkerJobRecord).where(WorkerJobRecord.id == job.id).with_for_update()
        )
        if record is None:
            return None
        if record.status is WorkerJobStatus.RUNNING:
            previous = await self._session.scalar(
                select(WorkerJobAttemptRecord)
                .where(
                    WorkerJobAttemptRecord.worker_job_id == record.id,
                    WorkerJobAttemptRecord.status == WorkerJobAttemptStatus.RUNNING,
                )
                .order_by(WorkerJobAttemptRecord.attempt_number.desc())
                .with_for_update()
            )
            if previous is not None:
                previous.status = WorkerJobAttemptStatus.ABANDONED
                previous.finished_at = occurred_at
                previous.error_code = "LEASE_EXPIRED"

        claimed = self._domain(record)
        try:
            claimed.claim(
                worker_id,
                occurred_at,
                lease_expires_at,
                lease_token,
                account_coordination_generation=account_coordination_generation,
            )
        except ValueError:
            return None
        self._write(record, claimed)
        await self._session.flush()
        return claimed

    async def list_expired_for_update(self, now: datetime, limit: int) -> list[WorkerJob]:
        occurred_at = normalize_utc(now)
        should_reconcile = or_(
            WorkerJobRecord.retry_safety == WorkerJobRetrySafety.RECONCILIATION_REQUIRED,
            WorkerJobRecord.attempt_count >= WorkerJobRecord.max_attempts,
        )
        result = await self._session.scalars(
            select(WorkerJobRecord)
            .where(
                WorkerJobRecord.status.in_(
                    [
                        WorkerJobStatus.QUEUED,
                        WorkerJobStatus.FAILED_RETRYABLE,
                        WorkerJobStatus.RUNNING,
                        WorkerJobStatus.WAITING_INTERVENTION,
                    ]
                ),
                or_(
                    WorkerJobRecord.deadline_at <= occurred_at,
                    and_(
                        WorkerJobRecord.status.in_(
                            [WorkerJobStatus.QUEUED, WorkerJobStatus.FAILED_RETRYABLE]
                        ),
                        WorkerJobRecord.attempt_count >= WorkerJobRecord.max_attempts,
                    ),
                    and_(
                        WorkerJobRecord.status == WorkerJobStatus.RUNNING,
                        WorkerJobRecord.lease_expires_at <= occurred_at,
                        should_reconcile,
                    ),
                ),
            )
            .order_by(WorkerJobRecord.deadline_at, WorkerJobRecord.updated_at)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        return [self._domain(record) for record in result]

    async def list_for_reconcile(self, worker_id: UUID, now: datetime) -> list[WorkerJob]:
        occurred_at = normalize_utc(now)
        has_capability = exists(
            select(WorkerCapabilityRecord.id).where(
                WorkerCapabilityRecord.worker_id == worker_id,
                WorkerCapabilityRecord.capability_name == WorkerJobRecord.capability_name,
                WorkerCapabilityRecord.capability_version == WorkerJobRecord.capability_version,
            )
        )
        has_current_assignment = exists(
            select(AccountWorkerAssignmentRecord.id).where(
                AccountWorkerAssignmentRecord.account_id == WorkerJobRecord.account_id,
                AccountWorkerAssignmentRecord.worker_id == worker_id,
                AccountWorkerAssignmentRecord.is_active.is_(True),
            )
        )
        result = await self._session.scalars(
            select(WorkerJobRecord)
            .where(
                or_(
                    and_(
                        WorkerJobRecord.status == WorkerJobStatus.RUNNING,
                        WorkerJobRecord.lease_worker_id == worker_id,
                    ),
                    and_(
                        WorkerJobRecord.status == WorkerJobStatus.WAITING_INTERVENTION,
                        WorkerJobRecord.assigned_worker_id == worker_id,
                    ),
                    and_(
                        WorkerJobRecord.status.in_(
                            [WorkerJobStatus.QUEUED, WorkerJobStatus.FAILED_RETRYABLE]
                        ),
                        WorkerJobRecord.scheduled_at <= occurred_at,
                        or_(
                            WorkerJobRecord.deadline_at.is_(None),
                            WorkerJobRecord.deadline_at > occurred_at,
                        ),
                        or_(
                            WorkerJobRecord.assigned_worker_id == worker_id,
                            and_(
                                WorkerJobRecord.assigned_worker_id.is_(None),
                                WorkerJobRecord.account_affinity_required.is_(False),
                            ),
                        ),
                        or_(
                            WorkerJobRecord.account_affinity_required.is_(False),
                            has_current_assignment,
                        ),
                        has_capability,
                    ),
                )
            )
            .order_by(WorkerJobRecord.priority.desc(), WorkerJobRecord.scheduled_at)
        )
        return [self._domain(record) for record in result]

    @staticmethod
    def _record(job: WorkerJob) -> WorkerJobRecord:
        record = WorkerJobRecord(id=job.id)
        SQLAlchemyWorkerJobRepository._write(record, job)
        return record

    @staticmethod
    def _write(record: WorkerJobRecord, job: WorkerJob) -> None:
        record.command_id = job.command_id
        record.account_id = job.account_id
        record.assigned_worker_id = job.assigned_worker_id
        record.account_affinity_required = job.account_affinity_required
        record.capability_name = job.capability_name
        record.capability_version = job.capability_version
        record.status = job.status
        record.priority = job.priority
        record.preemptible = job.preemptible
        record.scheduled_at = job.scheduled_at
        record.deadline_at = job.deadline_at
        record.attempt_count = job.attempt_count
        record.max_attempts = job.max_attempts
        record.retry_safety = job.retry_safety
        record.retry_authorized_by_operator = job.retry_authorized_by_operator
        record.lease_worker_id = job.lease_worker_id
        record.lease_token = job.lease_token
        record.lease_expires_at = job.lease_expires_at
        record.operation_class = job.operation_class
        record.account_coordination_generation = job.account_coordination_generation
        record.checkpoint = job.checkpoint
        record.result = job.result
        record.error_code = job.error_code
        record.created_at = job.created_at
        record.updated_at = job.updated_at
        record.completed_at = job.completed_at

    @staticmethod
    def _domain(record: WorkerJobRecord) -> WorkerJob:
        return WorkerJob(
            id=record.id,
            command_id=record.command_id,
            account_id=record.account_id,
            assigned_worker_id=record.assigned_worker_id,
            account_affinity_required=record.account_affinity_required,
            capability_name=record.capability_name,
            capability_version=record.capability_version,
            operation_class=OperationClass(record.operation_class),
            status=WorkerJobStatus(record.status),
            priority=record.priority,
            preemptible=record.preemptible,
            scheduled_at=record.scheduled_at,
            deadline_at=record.deadline_at,
            attempt_count=record.attempt_count,
            max_attempts=record.max_attempts,
            retry_safety=WorkerJobRetrySafety(record.retry_safety),
            retry_authorized_by_operator=record.retry_authorized_by_operator,
            lease_worker_id=record.lease_worker_id,
            lease_token=record.lease_token,
            lease_expires_at=record.lease_expires_at,
            account_coordination_generation=record.account_coordination_generation,
            checkpoint=record.checkpoint,
            result=record.result,
            error_code=record.error_code,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
        )


class SQLAlchemyWorkerJobAttemptRepository(WorkerJobAttemptRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, attempt: WorkerJobAttempt) -> None:
        self._session.add(
            WorkerJobAttemptRecord(
                id=attempt.id,
                worker_job_id=attempt.worker_job_id,
                attempt_number=attempt.attempt_number,
                worker_id=attempt.worker_id,
                lease_token=attempt.lease_token,
                status=attempt.status,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                error_code=attempt.error_code,
            )
        )
        await self._session.flush()

    async def get_running_for_update(self, job_id: UUID) -> WorkerJobAttempt | None:
        record = await self._session.scalar(
            select(WorkerJobAttemptRecord)
            .where(
                WorkerJobAttemptRecord.worker_job_id == job_id,
                WorkerJobAttemptRecord.status == WorkerJobAttemptStatus.RUNNING,
            )
            .order_by(WorkerJobAttemptRecord.attempt_number.desc())
            .with_for_update()
        )
        return self._domain(record) if record is not None else None

    async def list_for_job(self, job_id: UUID) -> list[WorkerJobAttempt]:
        result = await self._session.scalars(
            select(WorkerJobAttemptRecord)
            .where(WorkerJobAttemptRecord.worker_job_id == job_id)
            .order_by(WorkerJobAttemptRecord.attempt_number)
        )
        return [self._domain(record) for record in result]

    async def update(self, attempt: WorkerJobAttempt) -> None:
        record = await self._session.get(WorkerJobAttemptRecord, attempt.id)
        if record is None:
            raise LookupError(f"WorkerJobAttempt not found: {attempt.id}")
        record.status = attempt.status
        record.finished_at = attempt.finished_at
        record.error_code = attempt.error_code
        await self._session.flush()

    @staticmethod
    def _domain(record: WorkerJobAttemptRecord) -> WorkerJobAttempt:
        return WorkerJobAttempt(
            id=record.id,
            worker_job_id=record.worker_job_id,
            attempt_number=record.attempt_number,
            worker_id=record.worker_id,
            lease_token=record.lease_token,
            status=WorkerJobAttemptStatus(record.status),
            started_at=record.started_at,
            finished_at=record.finished_at,
            error_code=record.error_code,
        )


class SQLAlchemyWorkerInterventionRepository(WorkerInterventionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, intervention: WorkerIntervention) -> None:
        self._session.add(
            WorkerInterventionRecord(
                id=intervention.id,
                worker_job_id=intervention.worker_job_id,
                account_id=intervention.account_id,
                worker_id=intervention.worker_id,
                intervention_type=intervention.intervention_type,
                status=intervention.status,
                detail_code=intervention.detail_code,
                created_at=intervention.created_at,
                resolved_at=intervention.resolved_at,
                resolved_by=intervention.resolved_by,
            )
        )
        await self._session.flush()

    async def get_for_update(self, intervention_id: UUID) -> WorkerIntervention | None:
        record = await self._session.scalar(
            select(WorkerInterventionRecord)
            .where(WorkerInterventionRecord.id == intervention_id)
            .with_for_update()
        )
        return self._domain(record) if record is not None else None

    async def get_open_for_job(self, job_id: UUID) -> WorkerIntervention | None:
        record = await self._session.scalar(
            select(WorkerInterventionRecord).where(
                WorkerInterventionRecord.worker_job_id == job_id,
                WorkerInterventionRecord.status == WorkerInterventionStatus.OPEN,
            )
        )
        return self._domain(record) if record is not None else None

    async def list_for_worker(self, worker_id: UUID) -> list[WorkerIntervention]:
        result = await self._session.scalars(
            select(WorkerInterventionRecord)
            .where(WorkerInterventionRecord.worker_id == worker_id)
            .order_by(WorkerInterventionRecord.created_at)
        )
        return [self._domain(record) for record in result]

    async def update(self, intervention: WorkerIntervention) -> None:
        record = await self._session.get(WorkerInterventionRecord, intervention.id)
        if record is None:
            raise LookupError(f"WorkerIntervention not found: {intervention.id}")
        record.status = intervention.status
        record.detail_code = intervention.detail_code
        record.resolved_at = intervention.resolved_at
        record.resolved_by = intervention.resolved_by
        await self._session.flush()

    @staticmethod
    def _domain(record: WorkerInterventionRecord) -> WorkerIntervention:
        return WorkerIntervention(
            id=record.id,
            worker_job_id=record.worker_job_id,
            account_id=record.account_id,
            worker_id=record.worker_id,
            intervention_type=record.intervention_type,
            status=WorkerInterventionStatus(record.status),
            detail_code=record.detail_code,
            created_at=record.created_at,
            resolved_at=record.resolved_at,
            resolved_by=record.resolved_by,
        )
