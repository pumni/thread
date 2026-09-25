from contextlib import AbstractAsyncContextManager
from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID

from threads_platform.domain.account_execution import (
    AccountExecutionLease,
    AccountExecutionOwnerType,
)
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.capabilities import CapabilityRouteDecision, OperationClass
from threads_platform.domain.commands import Command, CommandAttempt
from threads_platform.domain.discovery import (
    DiscoveredAuthor,
    DiscoveredThread,
    DiscoveryCampaign,
    DiscoveryRun,
    DiscoveryRunCursor,
    DiscoverySourceEvidence,
    LeadCandidate,
    LeadCandidateEvidence,
    LeadCandidateTransition,
    SearchQuery,
)
from threads_platform.domain.outbox import IntegrationDelivery, OutboxEvent
from threads_platform.domain.publishing import ThreadPost, ThreadReply
from threads_platform.domain.sync import SyncState
from threads_platform.domain.worker_jobs import (
    WorkerIntervention,
    WorkerJob,
    WorkerJobAttempt,
)
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    NetworkProfile,
    WorkerAccountSession,
    WorkerAuditEvent,
    WorkerAuthChallenge,
    WorkerCapability,
    WorkerEnrollment,
    WorkerNode,
    WorkerSession,
)


class AccountRepository(Protocol):
    async def add(self, account: ThreadsAccount) -> None: ...

    async def get(self, account_id: UUID) -> ThreadsAccount | None: ...

    async def get_for_update(self, account_id: UUID) -> ThreadsAccount | None: ...

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

    async def add_if_absent(self, post: ThreadPost) -> bool: ...

    async def get_by_external_id(
        self, account_id: UUID, threads_post_id: str
    ) -> ThreadPost | None: ...


class ReplyRepository(Protocol):
    async def add(self, reply: ThreadReply) -> None: ...

    async def add_if_absent(self, reply: ThreadReply) -> bool: ...

    async def get_by_external_id(
        self, account_id: UUID, threads_reply_id: str
    ) -> ThreadReply | None: ...

    async def list_for_root(self, account_id: UUID, root_post_id: UUID) -> list[ThreadReply]: ...

    async def list_for_discovered_thread(
        self, account_id: UUID, discovered_thread_id: UUID
    ) -> list[ThreadReply]: ...


class DiscoveryRepository(Protocol):
    async def add_campaign(self, campaign: DiscoveryCampaign) -> None: ...

    async def get_campaign(self, campaign_id: UUID) -> DiscoveryCampaign | None: ...

    async def get_campaign_for_update(self, campaign_id: UUID) -> DiscoveryCampaign | None: ...

    async def update_campaign(self, campaign: DiscoveryCampaign) -> None: ...

    async def get_or_create_query(self, query: SearchQuery) -> SearchQuery: ...

    async def get_query(self, query_id: UUID) -> SearchQuery | None: ...

    async def add_run_if_absent(self, run: DiscoveryRun) -> bool: ...

    async def get_run(self, run_id: UUID) -> DiscoveryRun | None: ...

    async def get_run_for_update(self, run_id: UUID) -> DiscoveryRun | None: ...

    async def get_run_by_command_id(self, command_id: str) -> DiscoveryRun | None: ...

    async def update_run(self, run: DiscoveryRun) -> None: ...

    async def add_run_cursor(self, cursor: DiscoveryRunCursor) -> bool: ...

    async def upsert_author(self, author: DiscoveredAuthor) -> DiscoveredAuthor: ...

    async def get_author_by_remote_id(self, remote_author_id: str) -> DiscoveredAuthor | None: ...

    async def get_author_by_username(self, username: str) -> DiscoveredAuthor | None: ...

    async def get_profile_author_for_run(self, run_id: UUID) -> DiscoveredAuthor | None: ...

    async def attach_author_to_threads_by_username(
        self, username: str, author_id: UUID
    ) -> None: ...

    async def list_evidence_for_author(self, account_id: UUID, author_id: UUID) -> list[UUID]: ...

    async def upsert_thread(self, thread: DiscoveredThread) -> DiscoveredThread: ...

    async def get_thread_by_remote_id(self, remote_thread_id: str) -> DiscoveredThread | None: ...

    async def update_thread(self, thread: DiscoveredThread) -> None: ...

    async def add_evidence(self, evidence: DiscoverySourceEvidence) -> bool: ...

    async def add_candidate_if_absent(self, candidate: LeadCandidate) -> bool: ...

    async def get_candidate_for_update(self, candidate_id: UUID) -> LeadCandidate | None: ...

    async def get_candidate_by_author(
        self, account_id: UUID, author_id: UUID
    ) -> LeadCandidate | None: ...

    async def update_candidate(self, candidate: LeadCandidate) -> None: ...

    async def add_candidate_evidence(self, link: LeadCandidateEvidence) -> bool: ...

    async def add_candidate_transition(self, transition: LeadCandidateTransition) -> None: ...

    async def has_candidate_transition(self, command_id: str) -> bool: ...


class SyncStateRepository(Protocol):
    async def get(self, account_id: UUID, sync_type: str) -> SyncState | None: ...

    async def advance_if_current(self, state: SyncState, expected_cursor: str | None) -> bool: ...


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


class WorkerRepository(Protocol):
    async def add(self, worker: WorkerNode) -> None: ...

    async def get(self, worker_id: UUID) -> WorkerNode | None: ...

    async def get_for_update(self, worker_id: UUID) -> WorkerNode | None: ...

    async def update(self, worker: WorkerNode) -> None: ...

    async def list_expired_presence(self, now: datetime) -> list[WorkerNode]: ...


class WorkerCapabilityRepository(Protocol):
    async def replace_for_worker(
        self, worker_id: UUID, capabilities: list[WorkerCapability]
    ) -> None: ...

    async def list_for_worker(self, worker_id: UUID) -> list[WorkerCapability]: ...

    async def has(self, worker_id: UUID, name: str, version: int) -> bool: ...

    async def list_worker_ids(self, name: str, version: int) -> list[UUID]: ...


class BrowserProfileRepository(Protocol):
    async def add(self, profile: BrowserProfile) -> None: ...

    async def get(self, worker_id: UUID, profile_ref: str) -> BrowserProfile | None: ...


class WorkerAccountSessionRepository(Protocol):
    async def add(self, session: WorkerAccountSession) -> None: ...

    async def get_for_update(self, account_id: UUID) -> WorkerAccountSession | None: ...

    async def get(self, account_id: UUID) -> WorkerAccountSession | None: ...

    async def update(self, session: WorkerAccountSession) -> None: ...


class NetworkProfileRepository(Protocol):
    async def add(self, profile: NetworkProfile) -> None: ...

    async def get(self, account_id: UUID, profile_id: UUID) -> NetworkProfile | None: ...


class AccountWorkerAssignmentRepository(Protocol):
    async def add(self, assignment: AccountWorkerAssignment) -> None: ...

    async def get_active(self, account_id: UUID) -> AccountWorkerAssignment | None: ...

    async def update(self, assignment: AccountWorkerAssignment) -> None: ...


class WorkerSecurityRepository(Protocol):
    async def add_enrollment(self, enrollment: WorkerEnrollment) -> None: ...

    async def get_enrollment_for_update(self, token_digest: str) -> WorkerEnrollment | None: ...

    async def update_enrollment(self, enrollment: WorkerEnrollment) -> None: ...

    async def add_challenge(self, challenge: WorkerAuthChallenge) -> None: ...

    async def get_challenge_for_update(self, challenge_id: UUID) -> WorkerAuthChallenge | None: ...

    async def update_challenge(self, challenge: WorkerAuthChallenge) -> None: ...

    async def add_session(self, session: WorkerSession) -> None: ...

    async def get_active_session(
        self, token_digest: str, now: datetime
    ) -> WorkerSession | None: ...

    async def add_audit_event(self, event: WorkerAuditEvent) -> None: ...

    async def list_audit_events(self, worker_id: UUID) -> list[WorkerAuditEvent]: ...


class WorkerJobRepository(Protocol):
    async def add(self, job: WorkerJob) -> None: ...

    async def get(self, job_id: UUID) -> WorkerJob | None: ...

    async def get_for_update(self, job_id: UUID) -> WorkerJob | None: ...

    async def get_by_command_id(self, command_id: str) -> WorkerJob | None: ...

    async def update(self, job: WorkerJob) -> None: ...

    async def list_claimable(
        self,
        worker: WorkerNode,
        now: datetime,
        limit: int = 50,
    ) -> list[WorkerJob]: ...

    async def claim(
        self,
        job: WorkerJob,
        worker_id: UUID,
        now: datetime,
        lease_expires_at: datetime,
        lease_token: UUID,
        account_coordination_generation: int | None,
    ) -> WorkerJob | None: ...

    async def list_expired_for_update(self, now: datetime, limit: int) -> list[WorkerJob]: ...

    async def list_for_reconcile(self, worker_id: UUID, now: datetime) -> list[WorkerJob]: ...


class AccountExecutionLeaseRepository(Protocol):
    async def try_acquire(
        self,
        account_id: UUID,
        owner_type: AccountExecutionOwnerType,
        owner_id: str,
        operation_class: OperationClass,
        now: datetime,
        lease_expires_at: datetime,
    ) -> AccountExecutionLease | None: ...

    async def get_active(self, account_id: UUID, now: datetime) -> AccountExecutionLease | None: ...

    async def renew(
        self,
        account_id: UUID,
        owner_type: AccountExecutionOwnerType,
        owner_id: str,
        fencing_generation: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool: ...

    async def owns(
        self,
        account_id: UUID,
        owner_type: AccountExecutionOwnerType,
        owner_id: str,
        fencing_generation: int,
        now: datetime,
    ) -> bool: ...

    async def release(
        self,
        account_id: UUID,
        owner_type: AccountExecutionOwnerType,
        owner_id: str,
        fencing_generation: int,
        now: datetime,
    ) -> bool: ...


class CommandRouteDecisionRepository(Protocol):
    async def add(self, decision: CapabilityRouteDecision) -> None: ...

    async def get_latest_for_command(self, command_id: str) -> CapabilityRouteDecision | None: ...

    async def get_latest_execution_for_command(
        self, command_id: str
    ) -> CapabilityRouteDecision | None: ...


class WorkerJobAttemptRepository(Protocol):
    async def add(self, attempt: WorkerJobAttempt) -> None: ...

    async def get_running_for_update(self, job_id: UUID) -> WorkerJobAttempt | None: ...

    async def list_for_job(self, job_id: UUID) -> list[WorkerJobAttempt]: ...

    async def update(self, attempt: WorkerJobAttempt) -> None: ...


class WorkerInterventionRepository(Protocol):
    async def add(self, intervention: WorkerIntervention) -> None: ...

    async def get_for_update(self, intervention_id: UUID) -> WorkerIntervention | None: ...

    async def get_open_for_job(self, job_id: UUID) -> WorkerIntervention | None: ...

    async def list_for_worker(self, worker_id: UUID) -> list[WorkerIntervention]: ...

    async def update(self, intervention: WorkerIntervention) -> None: ...


class UnitOfWork(Protocol):
    accounts: AccountRepository
    commands: CommandRepository
    attempts: CommandAttemptRepository
    posts: PostRepository
    replies: ReplyRepository
    discovery: DiscoveryRepository
    sync_states: SyncStateRepository
    outbox_events: OutboxEventRepository
    deliveries: IntegrationDeliveryRepository
    workers: WorkerRepository
    worker_capabilities: WorkerCapabilityRepository
    browser_profiles: BrowserProfileRepository
    worker_account_sessions: WorkerAccountSessionRepository
    network_profiles: NetworkProfileRepository
    assignments: AccountWorkerAssignmentRepository
    worker_security: WorkerSecurityRepository
    worker_jobs: WorkerJobRepository
    worker_job_attempts: WorkerJobAttemptRepository
    worker_interventions: WorkerInterventionRepository
    account_execution_leases: AccountExecutionLeaseRepository
    command_route_decisions: CommandRouteDecisionRepository

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
