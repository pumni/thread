from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.ports.repositories import (
    AccountRepository,
    AccountWorkerAssignmentRepository,
    BrowserProfileRepository,
    CommandAttemptRepository,
    CommandRepository,
    IntegrationDeliveryRepository,
    NetworkProfileRepository,
    OutboxEventRepository,
    PostRepository,
    ReplyRepository,
    SyncStateRepository,
    WorkerCapabilityRepository,
    WorkerRepository,
)
from threads_platform.infrastructure.persistence.repositories import (
    SQLAlchemyAccountRepository,
    SQLAlchemyAccountWorkerAssignmentRepository,
    SQLAlchemyBrowserProfileRepository,
    SQLAlchemyCommandAttemptRepository,
    SQLAlchemyCommandRepository,
    SQLAlchemyIntegrationDeliveryRepository,
    SQLAlchemyNetworkProfileRepository,
    SQLAlchemyOutboxEventRepository,
    SQLAlchemyPostRepository,
    SQLAlchemyReplyRepository,
    SQLAlchemySyncStateRepository,
    SQLAlchemyWorkerCapabilityRepository,
    SQLAlchemyWorkerRepository,
)


class SQLAlchemyUnitOfWork:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session = session_factory()
        self.accounts: AccountRepository = SQLAlchemyAccountRepository(self._session)
        self.commands: CommandRepository = SQLAlchemyCommandRepository(self._session)
        self.attempts: CommandAttemptRepository = SQLAlchemyCommandAttemptRepository(self._session)
        self.posts: PostRepository = SQLAlchemyPostRepository(self._session)
        self.replies: ReplyRepository = SQLAlchemyReplyRepository(self._session)
        self.sync_states: SyncStateRepository = SQLAlchemySyncStateRepository(self._session)
        self.outbox_events: OutboxEventRepository = SQLAlchemyOutboxEventRepository(self._session)
        self.deliveries: IntegrationDeliveryRepository = SQLAlchemyIntegrationDeliveryRepository(
            self._session
        )
        self.workers: WorkerRepository = SQLAlchemyWorkerRepository(self._session)
        self.worker_capabilities: WorkerCapabilityRepository = SQLAlchemyWorkerCapabilityRepository(
            self._session
        )
        self.browser_profiles: BrowserProfileRepository = SQLAlchemyBrowserProfileRepository(
            self._session
        )
        self.network_profiles: NetworkProfileRepository = SQLAlchemyNetworkProfileRepository(
            self._session
        )
        self.assignments: AccountWorkerAssignmentRepository = (
            SQLAlchemyAccountWorkerAssignmentRepository(self._session)
        )

    async def __aenter__(self) -> SQLAlchemyUnitOfWork:
        await self._session.begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                try:
                    await self._session.commit()
                except BaseException:
                    await self._session.rollback()
                    raise
            else:
                await self._session.rollback()
        finally:
            await self._session.close()

    def savepoint(self) -> AbstractAsyncContextManager[object]:
        return cast(AbstractAsyncContextManager[object], self._session.begin_nested())


class SQLAlchemyUnitOfWorkFactory:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(self._session_factory)
