from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.ports.repositories import (
    AccountRepository,
    CommandRepository,
    PostRepository,
    ReplyRepository,
)
from threads_platform.domain.accounts import AccountStatus, ThreadsAccount
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.publishing import ThreadPost, ThreadReply
from threads_platform.infrastructure.persistence.models import (
    AccountRecord,
    CommandRecord,
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
        self._session.add(
            CommandRecord(
                id=command.id,
                command_id=command.command_id,
                correlation_id=command.correlation_id,
                protocol_version=command.protocol_version,
                account_id=command.account_id,
                command_type=command.command_type,
                payload=command.payload,
                status=command.status,
                received_at=command.received_at,
                deadline_at=command.deadline_at,
                validated_at=command.validated_at,
                started_at=command.started_at,
                completed_at=command.completed_at,
                result=command.result,
                error_code=command.error_code,
            )
        )
        await self._session.flush()

    async def get_by_command_id(self, command_id: str) -> Command | None:
        record = await self._session.scalar(
            select(CommandRecord).where(CommandRecord.command_id == command_id)
        )
        if record is None:
            return None
        return Command(
            id=record.id,
            command_id=record.command_id,
            correlation_id=record.correlation_id,
            account_id=record.account_id,
            protocol_version=record.protocol_version,
            command_type=record.command_type,
            payload=record.payload,
            status=CommandStatus(record.status),
            received_at=record.received_at,
            deadline_at=record.deadline_at,
            validated_at=record.validated_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            result=record.result,
            error_code=record.error_code,
        )

    async def update(self, command: Command) -> None:
        record = await self._session.get(CommandRecord, command.id)
        if record is None:
            raise LookupError(f"command not found: {command.command_id}")
        record.status = command.status
        record.validated_at = command.validated_at
        record.started_at = command.started_at
        record.completed_at = command.completed_at
        record.result = command.result
        record.error_code = command.error_code
        await self._session.flush()


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
