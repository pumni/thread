import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.worker_jobs import WorkerJobControlError, WorkerJobService
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.worker_jobs import (
    WorkerInterventionStatus,
    WorkerJobAttemptStatus,
    WorkerJobRetrySafety,
    WorkerJobStatus,
)
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    WorkerCapability,
    WorkerNode,
    WorkerStatus,
)
from threads_platform.infrastructure.persistence.models import (
    IntegrationDeliveryRecord,
    OutboxEventRecord,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 25, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, duration: timedelta) -> None:
        self.current += duration


async def add_worker(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    worker_id: UUID | None = None,
    *,
    now: datetime,
    max_concurrent_jobs: int = 4,
) -> UUID:
    identity = worker_id or uuid4()
    worker = WorkerNode(
        worker_id=identity,
        display_name=f"Worker {identity}",
        hostname=f"host-{identity}",
        platform="windows",
        agent_version="1.0.0",
        protocol_version=1,
        capabilities_schema_version=1,
        status=WorkerStatus.ONLINE,
        max_concurrent_jobs=max_concurrent_jobs,
        last_heartbeat_at=now,
        presence_expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.workers.add(worker)
        await unit_of_work.worker_capabilities.replace_for_worker(
            identity,
            [WorkerCapability(identity, "synthetic.echo", 1, advertised_at=now)],
        )
    return identity


async def add_account_assignment(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    worker_id: UUID,
) -> ThreadsAccount:
    account = ThreadsAccount(
        threads_user_id=f"worker-job-account-{uuid4()}",
        username="worker_job_account",
    )
    profile = BrowserProfile(worker_id, f"profile-{uuid4()}")
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.browser_profiles.add(profile)
        await unit_of_work.assignments.add(
            AccountWorkerAssignment(account.id, worker_id, profile.profile_ref)
        )
    return account


async def add_validated_command(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    account_id: UUID,
    now: datetime,
) -> Command:
    command = Command(
        command_id=f"worker-command-{uuid4()}",
        correlation_id=f"worker-correlation-{uuid4()}",
        account_id=account_id,
        command_type="threads.publish_text",
        payload={"text": "synthetic remote command"},
        created_at=now,
        received_at=now,
    )
    command.transition(CommandStatus.VALIDATED, now)
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.commands.add(command)
    return command


async def test_concurrent_claim_has_one_owner(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    worker_ids = [
        await add_worker(unit_of_work_factory, now=clock.now()),
        await add_worker(unit_of_work_factory, now=clock.now()),
    ]
    jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    queued = await jobs.enqueue("synthetic.echo", 1)

    claims = await asyncio.gather(*(jobs.claim_next(worker_id) for worker_id in worker_ids))

    owners = [claim for claim in claims if claim is not None]
    assert len(owners) == 1
    assert owners[0].id == queued.id
    assert owners[0].lease_worker_id in worker_ids
    attempts = await jobs.attempts(queued.id)
    assert len(attempts) == 1
    assert attempts[0].status is WorkerJobAttemptStatus.RUNNING


async def test_account_affinity_and_worker_eligibility_are_enforced(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    assigned_worker = await add_worker(unit_of_work_factory, now=clock.now())
    other_worker = await add_worker(unit_of_work_factory, now=clock.now())
    account = await add_account_assignment(unit_of_work_factory, assigned_worker)
    jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    affine_job = await jobs.enqueue("synthetic.echo", 1, account_id=account.id)

    assert affine_job.account_affinity_required
    assert affine_job.assigned_worker_id == assigned_worker
    assert await jobs.claim_next(other_worker) is None

    general_job = await jobs.enqueue("synthetic.echo", 1)
    for state in (WorkerStatus.OFFLINE, WorkerStatus.DRAINING, WorkerStatus.UPGRADE_REQUIRED):
        async with unit_of_work_factory() as unit_of_work:
            worker = await unit_of_work.workers.get_for_update(other_worker)
            assert worker is not None
            worker.status = state
            await unit_of_work.workers.update(worker)
        with pytest.raises(WorkerJobControlError, match="WORKER_NOT_ELIGIBLE"):
            await jobs.claim_next(other_worker)

    async with unit_of_work_factory() as unit_of_work:
        worker = await unit_of_work.workers.get_for_update(other_worker)
        assert worker is not None
        worker.status = WorkerStatus.ONLINE
        await unit_of_work.workers.update(worker)
    other_claim = await jobs.claim_next(other_worker)
    assert other_claim is not None and other_claim.id == general_job.id

    assigned_claim = await jobs.claim_next(assigned_worker)
    assert assigned_claim is not None and assigned_claim.id == affine_job.id


async def test_renew_reclaim_fencing_and_restart_reconciliation(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    first_worker = await add_worker(unit_of_work_factory, now=clock.now())
    second_worker = await add_worker(unit_of_work_factory, now=clock.now())
    service = WorkerJobService(
        unit_of_work_factory,
        clock=clock,
        lease_duration=timedelta(seconds=5),
    )
    queued = await service.enqueue("synthetic.echo", 1)
    first_claim = await service.claim_next(first_worker)
    assert first_claim is not None and first_claim.lease_token is not None
    old_token = first_claim.lease_token
    await service.checkpoint(queued.id, first_worker, old_token, {"phase": "prepared"})

    clock.advance(timedelta(seconds=2))
    renewed = await service.renew(queued.id, first_worker, old_token)
    assert renewed.lease_expires_at == clock.now() + timedelta(seconds=5)

    restarted_service = WorkerJobService(
        unit_of_work_factory,
        clock=clock,
        lease_duration=timedelta(seconds=5),
    )
    reconciliation = await restarted_service.reconcile(first_worker)
    assert len(reconciliation.jobs) == 1
    assert reconciliation.jobs[0].id == queued.id
    assert reconciliation.jobs[0].lease_token == old_token

    clock.advance(timedelta(seconds=6))
    reclaimed = await restarted_service.claim_next(second_worker)
    assert reclaimed is not None and reclaimed.lease_token is not None
    assert reclaimed.lease_token != old_token
    assert reclaimed.attempt_count == 2
    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_LEASE_LOST"):
        await restarted_service.checkpoint(queued.id, first_worker, old_token, {"phase": "stale"})
    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_LEASE_LOST"):
        await restarted_service.complete(queued.id, first_worker, old_token, {"success": True})

    completed = await restarted_service.complete(
        queued.id,
        second_worker,
        reclaimed.lease_token,
        {"synthetic": "complete"},
    )
    assert completed.status is WorkerJobStatus.SUCCEEDED
    attempts = await restarted_service.attempts(queued.id)
    assert [attempt.status for attempt in attempts] == [
        WorkerJobAttemptStatus.ABANDONED,
        WorkerJobAttemptStatus.SUCCEEDED,
    ]


async def test_intervention_requeue_and_command_outbox_are_transactional(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    clock = MutableClock()
    worker_id = await add_worker(unit_of_work_factory, now=clock.now())
    account = ThreadsAccount(
        threads_user_id=f"command-account-{uuid4()}",
        username="command_account",
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
    command = await add_validated_command(unit_of_work_factory, account.id, clock.now())
    notifications = WorkerNotificationHub()
    jobs = WorkerJobService(unit_of_work_factory, clock=clock, notifications=notifications)
    queued = await jobs.enqueue("synthetic.echo", 1, command_id=command.command_id)
    assert queued.command_id == command.command_id

    claimed = await jobs.claim_next(worker_id)
    assert claimed is not None and claimed.lease_token is not None
    suspended = await jobs.request_intervention(
        queued.id,
        worker_id,
        claimed.lease_token,
        intervention_type="CHALLENGE_REQUIRED",
        detail_code="OPERATOR_ACTION_REQUIRED",
    )
    assert suspended.status is WorkerJobStatus.WAITING_INTERVENTION
    async with unit_of_work_factory() as unit_of_work:
        waiting_command = await unit_of_work.commands.get_by_command_id(command.command_id)
    assert waiting_command is not None
    assert waiting_command.status is CommandStatus.WAITING_INTERVENTION
    interventions = await jobs.interventions(worker_id)
    assert len(interventions) == 1
    assert interventions[0].status is WorkerInterventionStatus.OPEN

    async with notifications.subscribe(worker_id) as notification_queue:
        requeued = await jobs.resolve_intervention(interventions[0].id, requeue=True)
        notification = await asyncio.wait_for(notification_queue.get(), timeout=1)
    assert requeued.status is WorkerJobStatus.QUEUED
    assert notification == {"type": "job.available", "job_id": str(queued.id)}
    async with unit_of_work_factory() as unit_of_work:
        waiting_command = await unit_of_work.commands.get_by_command_id(command.command_id)
    assert waiting_command is not None
    assert waiting_command.status is CommandStatus.WAITING_EXECUTION

    resumed = await jobs.claim_next(worker_id)
    assert resumed is not None and resumed.lease_token is not None
    assert resumed.lease_token != claimed.lease_token
    result: dict[str, object] = {"synthetic": "published"}
    await jobs.complete(queued.id, worker_id, resumed.lease_token, result)

    async with unit_of_work_factory() as unit_of_work:
        completed_command = await unit_of_work.commands.get_by_command_id(command.command_id)
    assert completed_command is not None
    assert completed_command.status is CommandStatus.SUCCEEDED
    assert completed_command.result == result
    event = await db_session.scalar(
        select(OutboxEventRecord).where(OutboxEventRecord.aggregate_id == command.command_id)
    )
    assert event is not None
    assert event.payload["result"] == result
    delivery = await db_session.scalar(
        select(IntegrationDeliveryRecord).where(IntegrationDeliveryRecord.event_id == event.id)
    )
    assert delivery is not None


async def test_command_result_outbox_failure_rolls_back_job_and_command(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    clock = MutableClock()
    worker_id = await add_worker(unit_of_work_factory, now=clock.now())
    account = ThreadsAccount(
        threads_user_id=f"rollback-account-{uuid4()}",
        username="rollback_account",
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
    command = await add_validated_command(unit_of_work_factory, account.id, clock.now())
    enqueue_service = WorkerJobService(unit_of_work_factory, clock=clock)
    queued = await enqueue_service.enqueue("synthetic.echo", 1, command_id=command.command_id)
    claimed = await enqueue_service.claim_next(worker_id)
    assert claimed is not None and claimed.lease_token is not None

    invalid_delivery_service = WorkerJobService(
        unit_of_work_factory,
        clock=clock,
        crm_destination="",
    )
    with pytest.raises(ValueError, match="delivery destination"):
        await invalid_delivery_service.complete(
            queued.id,
            worker_id,
            claimed.lease_token,
            {"synthetic": "result"},
        )

    stored_job = await enqueue_service.get(queued.id)
    assert stored_job.status is WorkerJobStatus.RUNNING
    async with unit_of_work_factory() as unit_of_work:
        stored_command = await unit_of_work.commands.get_by_command_id(command.command_id)
    assert stored_command is not None
    assert stored_command.status is CommandStatus.WAITING_EXECUTION
    event = await db_session.scalar(
        select(OutboxEventRecord).where(OutboxEventRecord.aggregate_id == command.command_id)
    )
    assert event is None


async def test_ambiguous_expired_job_requires_operator_reconciliation_before_retry(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    first_worker = await add_worker(unit_of_work_factory, now=clock.now())
    second_worker = await add_worker(unit_of_work_factory, now=clock.now())
    service = WorkerJobService(
        unit_of_work_factory,
        clock=clock,
        lease_duration=timedelta(seconds=5),
    )
    queued = await service.enqueue(
        "synthetic.echo",
        1,
        retry_safety=WorkerJobRetrySafety.RECONCILIATION_REQUIRED,
    )
    claimed = await service.claim_next(first_worker)
    assert claimed is not None
    clock.advance(timedelta(seconds=6))

    restarted_service = WorkerJobService(
        unit_of_work_factory,
        clock=clock,
        lease_duration=timedelta(seconds=5),
    )
    assert await restarted_service.recover_expired() == 1
    waiting = await restarted_service.get(queued.id)
    assert waiting.status is WorkerJobStatus.WAITING_INTERVENTION
    assert await restarted_service.claim_next(second_worker) is None
    interventions = await restarted_service.interventions(first_worker)
    assert len(interventions) == 1
    assert interventions[0].detail_code == "LEASE_EXPIRED_WITH_RECONCILIATION_REQUIRED"

    with pytest.raises(WorkerJobControlError, match="INTERVENTION_REQUEUE_NOT_SAFE"):
        await restarted_service.resolve_intervention(interventions[0].id, requeue=True)
    requeued = await restarted_service.resolve_intervention(
        interventions[0].id,
        requeue=True,
        confirmed_safe_to_retry=True,
    )
    assert requeued.status is WorkerJobStatus.QUEUED
    assert requeued.retry_authorized_by_operator
    resumed = await restarted_service.claim_next(second_worker)
    assert resumed is not None, (
        f"requeued={requeued!r}, stored={await restarted_service.get(queued.id)!r}"
    )
    assert resumed.attempt_count == 2


async def test_deadline_expires_waiting_intervention_and_command(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    worker_id = await add_worker(unit_of_work_factory, now=clock.now())
    account = ThreadsAccount(
        threads_user_id=f"deadline-account-{uuid4()}",
        username="deadline_account",
    )
    command = Command(
        command_id=f"worker-command-{uuid4()}",
        correlation_id=f"worker-correlation-{uuid4()}",
        account_id=account.id,
        command_type="threads.publish_text",
        payload={"text": "synthetic remote command"},
        deadline_at=clock.now() + timedelta(seconds=5),
        created_at=clock.now(),
        received_at=clock.now(),
    )
    command.transition(CommandStatus.VALIDATED, clock.now())
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.commands.add(command)
    jobs = WorkerJobService(unit_of_work_factory, clock=clock, lease_duration=timedelta(seconds=5))
    queued = await jobs.enqueue("synthetic.echo", 1, command_id=command.command_id)
    claimed = await jobs.claim_next(worker_id)
    assert claimed is not None and claimed.lease_token is not None
    await jobs.request_intervention(
        queued.id,
        worker_id,
        claimed.lease_token,
        intervention_type="CHALLENGE_REQUIRED",
        detail_code="OPERATOR_ACTION_REQUIRED",
    )

    clock.advance(timedelta(seconds=6))
    assert await jobs.recover_expired() == 1
    expired = await jobs.get(queued.id)
    assert expired.status is WorkerJobStatus.EXPIRED
    interventions = await jobs.interventions(worker_id)
    assert len(interventions) == 1
    assert interventions[0].status is WorkerInterventionStatus.CANCELLED
    async with unit_of_work_factory() as unit_of_work:
        expired_command = await unit_of_work.commands.get_by_command_id(command.command_id)
    assert expired_command is not None
    assert expired_command.status is CommandStatus.EXPIRED


async def test_attempt_bound_ends_recovery_after_control_plane_restart(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    worker_id = await add_worker(unit_of_work_factory, now=clock.now())
    service = WorkerJobService(
        unit_of_work_factory,
        clock=clock,
        lease_duration=timedelta(seconds=5),
    )
    queued = await service.enqueue("synthetic.echo", 1, max_attempts=1)
    claimed = await service.claim_next(worker_id)
    assert claimed is not None
    clock.advance(timedelta(seconds=6))

    restarted_service = WorkerJobService(
        unit_of_work_factory,
        clock=clock,
        lease_duration=timedelta(seconds=5),
    )
    assert await restarted_service.recover_expired() == 1
    terminal = await restarted_service.get(queued.id)
    assert terminal.status is WorkerJobStatus.FAILED_FINAL
    assert terminal.error_code == "WORKER_JOB_ATTEMPTS_EXHAUSTED"
    assert await restarted_service.claim_next(worker_id) is None
