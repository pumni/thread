from contextlib import AbstractAsyncContextManager
from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID

from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import Command, CommandAttempt
from threads_platform.domain.outbox import IntegrationDelivery, OutboxEvent
from threads_platform.domain.publishing import ThreadPost, ThreadReply


class AccountRepository(Protocol):
    async def add(self, account: ThreadsAccount) -> None: ...

    async def get(self, account_id: UUID) -> ThreadsAccount | None: ...

    async def update(self, account: ThreadsAccount) -> None: ...


class CommandRepository(Protocol):
    async def add(self, command: Command) -> None: ...

    async def add_if_absent(self, command: Command) -> bool: ...

    async def get_by_command_id(self, command_id: str) -> Command | None: ...

    async def get_by_command_id_for_update(self, command_id: str) -> Command | None: ...

    async def get_next_ready_for_update(self, now: datetime) -> Command | None: ...

    async def save_checkpoint_if_leased(
        self,
        command_id: str,
        lease_token: UUID,
        now: datetime,
        checkpoint: dict[str, object],
    ) -> bool: ...

    async def renew_execution_lease(
        self,
        command_id: str,
        lease_token: UUID,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool: ...

    async def update(self, command: Command) -> None: ...


class PostRepository(Protocol):
    async def add(self, post: ThreadPost) -> None: ...

    async def get_by_external_id(
        self, account_id: UUID, threads_post_id: str
    ) -> ThreadPost | None: ...


class ReplyRepository(Protocol):
    async def add(self, reply: ThreadReply) -> None: ...

    async def get_by_external_id(
        self, account_id: UUID, threads_reply_id: str
    ) -> ThreadReply | None: ...


class CommandAttemptRepository(Protocol):
    async def add(self, attempt: CommandAttempt) -> None: ...

    async def count_for_command(self, command_id: str) -> int: ...

    async def get_processing_for_command_for_update(
        self, command_id: str
    ) -> CommandAttempt | None: ...

    async def update(self, attempt: CommandAttempt) -> None: ...


class OutboxEventRepository(Protocol):
    async def add(self, event: OutboxEvent) -> None: ...

    async def get(self, event_id: UUID) -> OutboxEvent | None: ...

    async def update(self, event: OutboxEvent) -> None: ...


class IntegrationDeliveryRepository(Protocol):
    async def add(self, delivery: IntegrationDelivery) -> None: ...

    async def claim_next(
        self,
        destination: str,
        now: datetime,
        lease_token: UUID,
        lease_expires_at: datetime,
    ) -> tuple[IntegrationDelivery, OutboxEvent] | None: ...

    async def get_for_update(self, delivery_id: UUID) -> IntegrationDelivery | None: ...

    async def update(self, delivery: IntegrationDelivery) -> None: ...


class UnitOfWork(Protocol):
    accounts: AccountRepository
    commands: CommandRepository
    attempts: CommandAttemptRepository
    posts: PostRepository
    replies: ReplyRepository
    outbox_events: OutboxEventRepository
    deliveries: IntegrationDeliveryRepository

    def savepoint(self) -> AbstractAsyncContextManager[object]: ...

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[UnitOfWork]: ...
