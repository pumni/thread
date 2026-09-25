from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.ports.repositories import (
    AccountRepository,
    CommandAttemptRepository,
    CommandRepository,
    IntegrationDeliveryRepository,
    OutboxEventRepository,
    PostRepository,
    ReplyRepository,
)
from threads_platform.domain.accounts import AccountStatus, ThreadsAccount
from threads_platform.domain.commands import (
    Command,
    CommandAttempt,
    CommandStatus,
)
from threads_platform.domain.outbox import (
    DeliveryStatus,
    IntegrationDelivery,
    OutboxEvent,
    OutboxStatus,
)
from threads_platform.domain.publishing import ThreadPost, ThreadReply
from threads_platform.infrastructure.persistence.models import (
    AccountRecord,
    CommandAttemptRecord,
    CommandRecord,
    IntegrationDeliveryRecord,
    OutboxEventRecord,
    PostRecord,
    ReplyRecord,
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
        record.updated_at = account.updated_at
        await self._session.flush()


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
        )
        record = await self._session.scalar(
            select(CommandRecord)
            .where(ready)
            .order_by(CommandRecord.received_at, CommandRecord.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return self._domain(record) if record is not None else None

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

    async def get_by_external_id(self, account_id: UUID, threads_post_id: str) -> ThreadPost | None:
        record = await self._session.scalar(
            select(PostRecord).where(
                PostRecord.account_id == account_id,
                PostRecord.threads_post_id == threads_post_id,
            )
        )
        if record is None:
            return None
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
        return ThreadReply(
            id=record.id,
            account_id=record.account_id,
            threads_reply_id=record.threads_reply_id,
            root_post_id=record.root_post_id,
            parent_reply_id=record.parent_reply_id,
            text=record.text,
            replied_at=record.replied_at,
            created_at=record.created_at,
        )


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
