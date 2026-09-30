from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.ports.repositories import (
    AccountActivityPlanRepository,
    AccountActivityRecurrenceStateRepository,
    AccountActivityTemplateRepository,
    AccountExecutionLeaseRepository,
    AccountRepository,
    AccountWorkerAssignmentRepository,
    BrowserProfileRepository,
    CommandAttemptRepository,
    CommandRepository,
    CommandRouteDecisionRepository,
    ConversationSyncDispatchRepository,
    ConversationSyncScheduleRepository,
    DiscoveryRepository,
    IntegrationDeliveryRepository,
    NetworkProfileRepository,
    OAuthCredentialRepository,
    OutboxEventRepository,
    PostRepository,
    ReplyRepository,
    ScheduledActivityRepository,
    SyncStateRepository,
    WorkerAccountSessionRepository,
    WorkerCapabilityRepository,
    WorkerInterventionRepository,
    WorkerJobAttemptRepository,
    WorkerJobCancelRequestRepository,
    WorkerJobPreemptionRepository,
    WorkerJobRepository,
    WorkerRepository,
    WorkerSecurityRepository,
)
from threads_platform.infrastructure.persistence.repositories import (
    SQLAlchemyAccountActivityPlanRepository,
    SQLAlchemyAccountActivityRecurrenceStateRepository,
    SQLAlchemyAccountActivityTemplateRepository,
    SQLAlchemyAccountExecutionLeaseRepository,
    SQLAlchemyAccountRepository,
    SQLAlchemyAccountWorkerAssignmentRepository,
    SQLAlchemyBrowserProfileRepository,
    SQLAlchemyCommandAttemptRepository,
    SQLAlchemyCommandRepository,
    SQLAlchemyCommandRouteDecisionRepository,
    SQLAlchemyConversationSyncDispatchRepository,
    SQLAlchemyConversationSyncScheduleRepository,
    SQLAlchemyDiscoveryRepository,
    SQLAlchemyIntegrationDeliveryRepository,
    SQLAlchemyNetworkProfileRepository,
    SQLAlchemyOAuthCredentialRepository,
    SQLAlchemyOutboxEventRepository,
    SQLAlchemyPostRepository,
    SQLAlchemyReplyRepository,
    SQLAlchemyScheduledActivityRepository,
    SQLAlchemySyncStateRepository,
    SQLAlchemyWorkerAccountSessionRepository,
    SQLAlchemyWorkerCapabilityRepository,
    SQLAlchemyWorkerInterventionRepository,
    SQLAlchemyWorkerJobAttemptRepository,
    SQLAlchemyWorkerJobCancelRequestRepository,
    SQLAlchemyWorkerJobPreemptionRepository,
    SQLAlchemyWorkerJobRepository,
    SQLAlchemyWorkerRepository,
    SQLAlchemyWorkerSecurityRepository,
)


class SQLAlchemyUnitOfWork:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session = session_factory()
        self.accounts: AccountRepository = SQLAlchemyAccountRepository(self._session)
        self.oauth_credentials: OAuthCredentialRepository = SQLAlchemyOAuthCredentialRepository(
            self._session
        )
        self.activity_plans: AccountActivityPlanRepository = (
            SQLAlchemyAccountActivityPlanRepository(self._session)
        )
        self.activity_templates: AccountActivityTemplateRepository = (
            SQLAlchemyAccountActivityTemplateRepository(self._session)
        )
        self.activity_recurrence_states: AccountActivityRecurrenceStateRepository = (
            SQLAlchemyAccountActivityRecurrenceStateRepository(self._session)
        )
        self.scheduled_activities: ScheduledActivityRepository = (
            SQLAlchemyScheduledActivityRepository(self._session)
        )
        self.conversation_sync_schedules: ConversationSyncScheduleRepository = (
            SQLAlchemyConversationSyncScheduleRepository(self._session)
        )
        self.conversation_sync_dispatches: ConversationSyncDispatchRepository = (
            SQLAlchemyConversationSyncDispatchRepository(self._session)
        )
        self.account_execution_leases: AccountExecutionLeaseRepository = (
            SQLAlchemyAccountExecutionLeaseRepository(self._session)
        )
        self.commands: CommandRepository = SQLAlchemyCommandRepository(self._session)
        self.command_route_decisions: CommandRouteDecisionRepository = (
            SQLAlchemyCommandRouteDecisionRepository(self._session)
        )
        self.attempts: CommandAttemptRepository = SQLAlchemyCommandAttemptRepository(self._session)
        self.posts: PostRepository = SQLAlchemyPostRepository(self._session)
        self.replies: ReplyRepository = SQLAlchemyReplyRepository(self._session)
        self.discovery: DiscoveryRepository = SQLAlchemyDiscoveryRepository(self._session)
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
        self.worker_account_sessions: WorkerAccountSessionRepository = (
            SQLAlchemyWorkerAccountSessionRepository(self._session)
        )
        self.network_profiles: NetworkProfileRepository = SQLAlchemyNetworkProfileRepository(
            self._session
        )
        self.assignments: AccountWorkerAssignmentRepository = (
            SQLAlchemyAccountWorkerAssignmentRepository(self._session)
        )
        self.worker_security: WorkerSecurityRepository = SQLAlchemyWorkerSecurityRepository(
            self._session
        )
        self.worker_jobs: WorkerJobRepository = SQLAlchemyWorkerJobRepository(self._session)
        self.worker_job_attempts: WorkerJobAttemptRepository = SQLAlchemyWorkerJobAttemptRepository(
            self._session
        )
        self.worker_job_cancel_requests: WorkerJobCancelRequestRepository = (
            SQLAlchemyWorkerJobCancelRequestRepository(self._session)
        )
        self.worker_job_preemptions: WorkerJobPreemptionRepository = (
            SQLAlchemyWorkerJobPreemptionRepository(self._session)
        )
        self.worker_interventions: WorkerInterventionRepository = (
            SQLAlchemyWorkerInterventionRepository(self._session)
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
