from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import threads_platform.application.worker_jobs as worker_jobs_module
from threads_platform.application.account_activity_materialization import (
    materialize_due_account_activities,
)
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.ports.repositories import UnitOfWork
from threads_platform.application.worker_jobs import WorkerJobControlError, WorkerJobService
from threads_platform.domain.account_activities import (
    AccountActivityPlan,
    AccountActivityTemplate,
    ActivityPriority,
    ScheduledActivity,
)
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.worker_jobs import (
    WorkerJobAttemptStatus,
    WorkerJobCancelRequest,
    WorkerJobCancelRequestStatus,
    WorkerJobStatus,
)
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    WorkerCapability,
    WorkerNode,
    WorkerStatus,
)
from threads_platform.infrastructure.persistence.database import create_database_engine
from threads_platform.infrastructure.persistence.models import (
    CommandAttemptRecord,
    CommandRouteDecisionRecord,
    IntegrationDeliveryRecord,
    OutboxEventRecord,
    WorkerJobAttemptRecord,
    WorkerJobCancelRequestRecord,
    WorkerJobRecord,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration

_SAFE_CHECKPOINTS = {
    "threads.browser.feed.browse": ("BEFORE_NAVIGATION", "FEED_READY", "ITEM_BATCH"),
    "threads.browser.thread.open": ("BEFORE_NAVIGATION", "THREAD_READY"),
    "threads.browser.profile.open": (
        "BEFORE_NAVIGATION",
        "BEFORE_PROFILE_INSPECTION",
        "PROFILE_READY",
    ),
}


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 29, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, duration: timedelta) -> None:
        self.current += duration


@dataclass(slots=True)
class Scenario:
    clock: MutableClock
    service: WorkerJobService
    worker_id: UUID
    activity_id: UUID
    command_id: str
    account_id: UUID
    job_id: UUID
    lease_token: UUID | None


async def _running_activity_job(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    *,
    capability: str = "threads.browser.feed.browse",
    preemptible: bool = True,
    claim: bool = True,
    deadline_in: timedelta | None = None,
    lease_duration: timedelta = timedelta(seconds=5),
) -> Scenario:
    clock = MutableClock()
    worker_id = uuid4()
    account = ThreadsAccount(
        threads_user_id=f"cancel-test-{uuid4()}", username="cancel_test_account"
    )
    profile = BrowserProfile(worker_id, f"profile-{uuid4()}")
    assignment = AccountWorkerAssignment(account.id, worker_id, profile.profile_ref)
    worker = WorkerNode(
        worker_id=worker_id,
        display_name=f"Cancellation worker {worker_id}",
        hostname=f"cancel-host-{worker_id}",
        platform="windows",
        agent_version="1.0.0",
        protocol_version=1,
        capabilities_schema_version=1,
        status=WorkerStatus.ONLINE,
        max_concurrent_jobs=2,
        last_heartbeat_at=clock.now(),
        presence_expires_at=clock.now() + timedelta(hours=1),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    plan = AccountActivityPlan(account_id=account.id, name="Cancellation plan")
    configuration: dict[str, object]
    if capability == "threads.browser.thread.open":
        configuration = {"thread_ref": "/@alice/post/post-1"}
    elif capability == "threads.browser.profile.open":
        configuration = {"profile_ref": "/@alice/"}
    else:
        configuration = {"max_items": 5}
    template = AccountActivityTemplate(
        account_id=account.id,
        plan_id=plan.id,
        name="Cancellation activity",
        activity_type=capability,
        configuration=configuration,
        priority=ActivityPriority.NORMAL,
        change_reason="cancellation test",
    )
    activity = ScheduledActivity.from_plan_template(
        plan,
        template,
        clock.now() - timedelta(seconds=1),
        created_at=clock.now() - timedelta(seconds=2),
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.workers.add(worker)
        await unit_of_work.browser_profiles.add(profile)
        await unit_of_work.assignments.add(assignment)
        await unit_of_work.worker_capabilities.replace_for_worker(
            worker_id,
            [WorkerCapability(worker_id, capability, 1, advertised_at=clock.now())],
        )
        await unit_of_work.activity_plans.add(plan)
        await unit_of_work.activity_templates.add_revision(template)
        await unit_of_work.scheduled_activities.add_if_absent(activity)

    commands = await materialize_due_account_activities(
        unit_of_work_factory, now=clock.now(), limit=10
    )
    command: Command | None = commands[0] if commands else None
    if capability in _SAFE_CHECKPOINTS:
        assert command is not None
        assert command.command_id == f"activity:{activity.id}"
        command_id = command.command_id
    else:
        assert command is None
        command_id = None
    if command_id is not None:
        async with unit_of_work_factory() as unit_of_work:
            routed_command = await unit_of_work.commands.get_by_command_id_for_update(command_id)
            assert routed_command is not None
            routed_command.transition(CommandStatus.VALIDATED, clock.now())
            routed_command.transition(CommandStatus.WAITING_EXECUTION, clock.now())
            await unit_of_work.commands.update(routed_command)
    service = WorkerJobService(
        unit_of_work_factory,
        clock=clock,
        lease_duration=lease_duration,
    )
    queued = await service.enqueue(
        capability,
        1,
        command_id=command_id,
        account_id=account.id,
        assigned_worker_id=worker_id,
        account_affinity_required=True,
        preemptible=preemptible,
        scheduled_at=clock.now(),
        deadline_at=(clock.now() + deadline_in) if deadline_in is not None else None,
        input_data=configuration,
    )
    lease_token = None
    if claim:
        claimed = await service.claim_next(worker_id)
        assert claimed is not None
        lease_token = claimed.lease_token
    return Scenario(
        clock,
        service,
        worker_id,
        activity.id,
        command_id or "",
        account.id,
        queued.id,
        lease_token,
    )


async def _cancel_request(
    scenario: Scenario, reason_code: str = "OPERATOR_REQUESTED"
) -> WorkerJobCancelRequest:
    return await scenario.service.request_cancel(
        scenario.job_id,
        reason_code=reason_code,
        now=scenario.clock.now(),
    )


@pytest.mark.asyncio
async def test_request_is_idempotent_durable_and_visible_after_restart(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    request_time = scenario.clock.now()
    async with unit_of_work_factory() as unit_of_work:
        before = await unit_of_work.worker_jobs.get(scenario.job_id)
    assert before is not None

    restarted = WorkerJobService(unit_of_work_factory, clock=scenario.clock)
    requests = await asyncio.gather(
        scenario.service.request_cancel(
            scenario.job_id, reason_code="OPERATOR_REQUESTED", now=request_time
        ),
        restarted.request_cancel(
            scenario.job_id, reason_code="OPERATOR_REQUESTED", now=request_time
        ),
    )
    assert requests[0].id == requests[1].id
    assert requests[0].generation == 1
    assert requests[0].target_attempt_number == 1

    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.worker_jobs.get(scenario.job_id)
        attempt = await unit_of_work.worker_job_attempts.get_running_for_update(scenario.job_id)
    assert stored is not None and attempt is not None
    assert stored.status is WorkerJobStatus.RUNNING
    assert stored.lease_worker_id == before.lease_worker_id == scenario.worker_id
    assert stored.lease_token == before.lease_token == scenario.lease_token
    assert stored.lease_expires_at == before.lease_expires_at
    assert stored.account_coordination_generation == before.account_coordination_generation
    assert stored.updated_at == before.updated_at
    assert attempt.status is WorkerJobAttemptStatus.RUNNING

    reconciliation = await restarted.reconcile(scenario.worker_id)
    assert len(reconciliation.jobs) == 1
    assert reconciliation.jobs[0].pending_cancel_request is not None
    assert reconciliation.jobs[0].pending_cancel_request.id == requests[0].id
    assert reconciliation.jobs[0].pending_cancel_request.requested_at == request_time

    checkpointed = await restarted.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        {"phase": "BEFORE_NAVIGATION"},
    )
    renewed = await restarted.renew(scenario.job_id, scenario.worker_id, scenario.lease_token)
    for snapshot in (checkpointed, renewed):
        assert snapshot.pending_cancel_request is not None
        assert snapshot.pending_cancel_request.id == requests[0].id


@pytest.mark.parametrize(
    ("capability", "phase"),
    [(capability, phase) for capability, phases in _SAFE_CHECKPOINTS.items() for phase in phases],
)
@pytest.mark.asyncio
async def test_acknowledgement_atomically_cancels_job_attempt_command_and_outbox(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
    capability: str,
    phase: str,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory, capability=capability)
    assert scenario.lease_token is not None
    request = await _cancel_request(scenario)
    snapshot = await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        {"phase": phase},
    )
    assert snapshot.pending_cancel_request is not None
    assert snapshot.pending_cancel_request.id == request.id

    cancelled = await scenario.service.acknowledge_cancel(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        cancel_request_id=request.id,
        generation=request.generation,
        checkpoint_phase=phase,
    )
    assert cancelled.status is WorkerJobStatus.CANCELLED
    assert cancelled.lease_token is None

    async with unit_of_work_factory() as unit_of_work:
        stored_job = await unit_of_work.worker_jobs.get(scenario.job_id)
        attempts = await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        stored_request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(
            scenario.job_id
        )
    assert stored_job is not None and stored_job.status is WorkerJobStatus.CANCELLED
    assert stored_job.lease_token is None and stored_job.lease_worker_id is None
    assert len(attempts) == 1 and attempts[0].status is WorkerJobAttemptStatus.CANCELLED
    assert attempts[0].finished_at == scenario.clock.now()
    assert command is not None and command.status is CommandStatus.CANCELLED
    assert command.error_code == "OPERATOR_REQUESTED"
    assert stored_request is None
    persisted = await db_session.scalar(
        select(WorkerJobCancelRequestRecord).where(WorkerJobCancelRequestRecord.id == request.id)
    )
    assert persisted is not None
    assert persisted.status is WorkerJobCancelRequestStatus.ACKNOWLEDGED
    assert persisted.acknowledged_at == scenario.clock.now()
    assert persisted.safe_checkpoint == phase
    event_count = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == scenario.command_id)
    )
    assert event_count == 1
    event = await db_session.scalar(
        select(OutboxEventRecord).where(OutboxEventRecord.aggregate_id == scenario.command_id)
    )
    assert event is not None and event.payload["status"] == "CANCELLED"
    delivery_count = await db_session.scalar(
        select(func.count())
        .select_from(IntegrationDeliveryRecord)
        .where(IntegrationDeliveryRecord.event_id == event.id)
    )
    assert delivery_count == 1

    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_LEASE_LOST"):
        await scenario.service.complete(
            scenario.job_id, scenario.worker_id, scenario.lease_token, {"late": True}
        )
    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_LEASE_LOST"):
        await scenario.service.acknowledge_cancel(
            scenario.job_id,
            scenario.worker_id,
            scenario.lease_token,
            cancel_request_id=request.id,
            generation=request.generation,
            checkpoint_phase=phase,
        )


@pytest.mark.asyncio
async def test_runtime_process_treats_cancelled_activity_command_as_terminal_noop(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    request = await _cancel_request(scenario)
    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        {"phase": "BEFORE_NAVIGATION"},
    )
    await scenario.service.acknowledge_cancel(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        cancel_request_id=request.id,
        generation=request.generation,
        checkpoint_phase="BEFORE_NAVIGATION",
    )

    async with unit_of_work_factory() as unit_of_work:
        cancelled = await unit_of_work.commands.get_by_command_id(scenario.command_id)
    assert cancelled is not None and cancelled.status is CommandStatus.CANCELLED

    async def persistent_counts() -> tuple[int | None, ...]:
        return (
            await db_session.scalar(
                select(func.count())
                .select_from(WorkerJobRecord)
                .where(WorkerJobRecord.command_id == scenario.command_id)
            ),
            await db_session.scalar(
                select(func.count())
                .select_from(WorkerJobAttemptRecord)
                .where(WorkerJobAttemptRecord.worker_job_id == scenario.job_id)
            ),
            await db_session.scalar(
                select(func.count())
                .select_from(CommandRouteDecisionRecord)
                .where(CommandRouteDecisionRecord.command_id == scenario.command_id)
            ),
            await db_session.scalar(
                select(func.count())
                .select_from(CommandAttemptRecord)
                .where(CommandAttemptRecord.command_id == scenario.command_id)
            ),
            await db_session.scalar(
                select(func.count())
                .select_from(OutboxEventRecord)
                .where(OutboxEventRecord.aggregate_id == scenario.command_id)
            ),
        )

    counts_before = await persistent_counts()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {},
        clock=scenario.clock,
        worker_job_service=scenario.service,
    )
    result = await runtime.process(scenario.command_id)
    idle = await runtime.process_next()

    assert result.status is CommandStatus.CANCELLED
    assert result.executed is False
    assert idle is None
    async with unit_of_work_factory() as unit_of_work:
        unchanged = await unit_of_work.commands.get_by_command_id(scenario.command_id)
    assert unchanged is not None and unchanged.status is CommandStatus.CANCELLED
    assert await persistent_counts() == counts_before


@pytest.mark.parametrize(
    ("wrong_worker", "wrong_token", "wrong_request", "wrong_generation", "phase"),
    [
        (True, False, False, False, "BEFORE_NAVIGATION"),
        (False, True, False, False, "BEFORE_NAVIGATION"),
        (False, False, True, False, "BEFORE_NAVIGATION"),
        (False, False, False, True, "BEFORE_NAVIGATION"),
        (False, False, False, False, "NOT_A_SAFE_CHECKPOINT"),
        (False, False, False, False, "FEED_READY"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_acknowledgement_is_fenced_and_leaves_request_pending(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    wrong_worker: bool,
    wrong_token: bool,
    wrong_request: bool,
    wrong_generation: bool,
    phase: str,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    request = await _cancel_request(scenario)
    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        {"phase": "BEFORE_NAVIGATION"},
    )
    with pytest.raises(WorkerJobControlError):
        await scenario.service.acknowledge_cancel(
            scenario.job_id,
            uuid4() if wrong_worker else scenario.worker_id,
            uuid4() if wrong_token else scenario.lease_token,
            cancel_request_id=uuid4() if wrong_request else request.id,
            generation=request.generation + 1 if wrong_generation else request.generation,
            checkpoint_phase=phase,
        )
    async with unit_of_work_factory() as unit_of_work:
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
        attempt = await unit_of_work.worker_job_attempts.get_running_for_update(scenario.job_id)
        pending = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
    assert job is not None and job.status is WorkerJobStatus.RUNNING
    assert attempt is not None and attempt.status is WorkerJobAttemptStatus.RUNNING
    assert pending is not None and pending.id == request.id
    assert command is not None and command.status is CommandStatus.WAITING_EXECUTION


@pytest.mark.parametrize(
    "outcome", ("complete", "retryable_failure", "final_failure", "intervention")
)
@pytest.mark.asyncio
async def test_normal_attempt_end_supersedes_pending_request(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    outcome: str,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    request = await _cancel_request(scenario)
    if outcome == "complete":
        await scenario.service.complete(
            scenario.job_id, scenario.worker_id, scenario.lease_token, {"ok": True}
        )
    elif outcome in {"retryable_failure", "final_failure"}:
        await scenario.service.fail(
            scenario.job_id,
            scenario.worker_id,
            scenario.lease_token,
            error_code="TEST_FAILURE",
            retryable=outcome == "retryable_failure",
        )
    else:
        await scenario.service.request_intervention(
            scenario.job_id,
            scenario.worker_id,
            scenario.lease_token,
            intervention_type="CHALLENGE_REQUIRED",
            detail_code="OPERATOR_REQUIRED",
        )
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        attempts = await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id)
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
    assert stored is None
    persisted = await _cancel_request_record(unit_of_work_factory, request.id)
    assert persisted.status is WorkerJobCancelRequestStatus.SUPERSEDED
    assert persisted.superseded_reason == "TARGET_ATTEMPT_ENDED"
    assert job is not None
    assert attempts[0].status is (
        WorkerJobAttemptStatus.SUCCEEDED
        if outcome == "complete"
        else WorkerJobAttemptStatus.FAILED_RETRYABLE
        if outcome == "retryable_failure"
        else WorkerJobAttemptStatus.FAILED_FINAL
        if outcome == "final_failure"
        else WorkerJobAttemptStatus.WAITING_INTERVENTION
    )
    assert command is not None
    assert command.status is (
        CommandStatus.SUCCEEDED
        if outcome == "complete"
        else CommandStatus.WAITING_EXECUTION
        if outcome == "retryable_failure"
        else CommandStatus.FAILED_FINAL
        if outcome == "final_failure"
        else CommandStatus.WAITING_INTERVENTION
    )


@pytest.mark.parametrize("outcome", ("complete", "failure", "intervention"))
@pytest.mark.asyncio
async def test_cancel_request_races_attempt_terminal_outcome(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    outcome: str,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    lease_token = scenario.lease_token
    assert lease_token is not None
    competing_service = WorkerJobService(unit_of_work_factory, clock=scenario.clock)

    async def request_cancel() -> WorkerJobCancelRequest | None:
        try:
            return await scenario.service.request_cancel(
                scenario.job_id,
                reason_code="RACE_REQUEST",
                now=scenario.clock.now(),
            )
        except WorkerJobControlError:
            return None

    async def end_attempt() -> None:
        if outcome == "complete":
            await competing_service.complete(
                scenario.job_id, scenario.worker_id, lease_token, {"ok": True}
            )
        elif outcome == "failure":
            await competing_service.fail(
                scenario.job_id,
                scenario.worker_id,
                lease_token,
                error_code="TEST_FAILURE",
                retryable=False,
            )
        else:
            await competing_service.request_intervention(
                scenario.job_id,
                scenario.worker_id,
                lease_token,
                intervention_type="CHALLENGE_REQUIRED",
                detail_code="OPERATOR_REQUIRED",
            )

    request, _ = await asyncio.gather(request_cancel(), end_attempt())
    async with unit_of_work_factory() as unit_of_work:
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        attempt = (await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id))[0]
        pending = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
    assert pending is None
    assert job is not None and command is not None
    assert job.status is (
        WorkerJobStatus.SUCCEEDED
        if outcome == "complete"
        else WorkerJobStatus.FAILED_FINAL
        if outcome == "failure"
        else WorkerJobStatus.WAITING_INTERVENTION
    )
    assert attempt.status is (
        WorkerJobAttemptStatus.SUCCEEDED
        if outcome == "complete"
        else WorkerJobAttemptStatus.FAILED_FINAL
        if outcome == "failure"
        else WorkerJobAttemptStatus.WAITING_INTERVENTION
    )
    assert command.status is (
        CommandStatus.SUCCEEDED
        if outcome == "complete"
        else CommandStatus.FAILED_FINAL
        if outcome == "failure"
        else CommandStatus.WAITING_INTERVENTION
    )
    if request is not None:
        persisted = await _cancel_request_record(unit_of_work_factory, request.id)
        assert persisted.status is WorkerJobCancelRequestStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_acknowledgement_rolls_back_with_command_outbox_failure(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    lease_token = scenario.lease_token
    assert lease_token is not None
    request = await _cancel_request(scenario)
    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        lease_token,
        {"phase": "BEFORE_NAVIGATION"},
    )

    async def fail_outbox(
        _unit_of_work: UnitOfWork,
        _command: Command,
        _now: datetime,
        *,
        delivery_lifetime: timedelta,
        crm_destination: str,
    ) -> None:
        assert delivery_lifetime > timedelta(0)
        assert crm_destination
        raise RuntimeError("simulated outbox failure")

    monkeypatch.setattr(worker_jobs_module, "enqueue_command_result", fail_outbox)
    with pytest.raises(RuntimeError, match="simulated outbox failure"):
        await scenario.service.acknowledge_cancel(
            scenario.job_id,
            scenario.worker_id,
            lease_token,
            cancel_request_id=request.id,
            generation=request.generation,
            checkpoint_phase="BEFORE_NAVIGATION",
        )

    async with unit_of_work_factory() as unit_of_work:
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        attempt = (await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id))[0]
        pending = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
    assert job is not None and job.status is WorkerJobStatus.RUNNING
    assert command is not None and command.status is CommandStatus.WAITING_EXECUTION
    assert attempt.status is WorkerJobAttemptStatus.RUNNING
    assert pending is not None and pending.id == request.id


@pytest.mark.asyncio
async def test_lease_expiry_hides_and_reclaim_supersedes_old_generation(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    old_token = scenario.lease_token
    first_request = await _cancel_request(scenario)
    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        old_token,
        {"phase": "BEFORE_NAVIGATION"},
    )
    scenario.clock.advance(timedelta(seconds=6))

    restarted = WorkerJobService(
        unit_of_work_factory, clock=scenario.clock, lease_duration=timedelta(seconds=5)
    )
    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_LEASE_LOST"):
        await restarted.acknowledge_cancel(
            scenario.job_id,
            scenario.worker_id,
            old_token,
            cancel_request_id=first_request.id,
            generation=first_request.generation,
            checkpoint_phase="BEFORE_NAVIGATION",
        )
    reconciliation = await restarted.reconcile(scenario.worker_id)
    assert len(reconciliation.jobs) == 1
    assert reconciliation.jobs[0].pending_cancel_request is None
    reclaimed = await restarted.claim_next(scenario.worker_id)
    assert reclaimed is not None and reclaimed.lease_token is not None
    assert reclaimed.lease_token != old_token
    attempts = await restarted.attempts(scenario.job_id)
    assert [attempt.status for attempt in attempts] == [
        WorkerJobAttemptStatus.ABANDONED,
        WorkerJobAttemptStatus.RUNNING,
    ]
    old_record = await _cancel_request_record(unit_of_work_factory, first_request.id)
    assert old_record.status is WorkerJobCancelRequestStatus.SUPERSEDED
    assert old_record.superseded_reason == "TARGET_ATTEMPT_ENDED"

    second_request = await restarted.request_cancel(
        scenario.job_id, reason_code="SECOND_REQUEST", now=scenario.clock.now()
    )
    assert second_request.generation == 2
    assert second_request.target_attempt_id != first_request.target_attempt_id
    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_LEASE_LOST"):
        await restarted.acknowledge_cancel(
            scenario.job_id,
            scenario.worker_id,
            old_token,
            cancel_request_id=first_request.id,
            generation=first_request.generation,
            checkpoint_phase="BEFORE_NAVIGATION",
        )


@pytest.mark.asyncio
async def test_deadline_expiry_supersedes_request_without_cancelling_job(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory, deadline_in=timedelta(seconds=3))
    request = await _cancel_request(scenario)
    scenario.clock.advance(timedelta(seconds=4))
    recovered = await scenario.service.recover_expired()
    assert recovered == 1
    async with unit_of_work_factory() as unit_of_work:
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
    persisted = await _cancel_request_record(unit_of_work_factory, request.id)
    assert job is not None and job.status is WorkerJobStatus.EXPIRED
    assert command is not None and command.status is CommandStatus.EXPIRED
    assert persisted.status is WorkerJobCancelRequestStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_deadline_recovery_races_acknowledgement_with_one_terminal_winner(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    scenario = await _running_activity_job(
        unit_of_work_factory,
        deadline_in=timedelta(seconds=3),
        lease_duration=timedelta(seconds=5),
    )
    lease_token = scenario.lease_token
    assert lease_token is not None
    request = await _cancel_request(scenario)
    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        lease_token,
        {"phase": "BEFORE_NAVIGATION"},
    )
    scenario.clock.advance(timedelta(seconds=4))
    recovery_service = WorkerJobService(unit_of_work_factory, clock=scenario.clock)

    async def acknowledge() -> bool:
        try:
            await scenario.service.acknowledge_cancel(
                scenario.job_id,
                scenario.worker_id,
                lease_token,
                cancel_request_id=request.id,
                generation=request.generation,
                checkpoint_phase="BEFORE_NAVIGATION",
            )
        except WorkerJobControlError:
            return False
        return True

    ack_won, recovered = await asyncio.gather(acknowledge(), recovery_service.recover_expired())
    assert ack_won != (recovered == 1)
    async with unit_of_work_factory() as unit_of_work:
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        attempt = (await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id))[0]
    persisted = await _cancel_request_record(unit_of_work_factory, request.id)
    assert job is not None and command is not None
    if ack_won:
        assert job.status is WorkerJobStatus.CANCELLED
        assert attempt.status is WorkerJobAttemptStatus.CANCELLED
        assert command.status is CommandStatus.CANCELLED
        assert persisted.status is WorkerJobCancelRequestStatus.ACKNOWLEDGED
    else:
        assert recovered == 1
        assert job.status is WorkerJobStatus.EXPIRED
        assert attempt.status is WorkerJobAttemptStatus.FAILED_FINAL
        assert command.status is CommandStatus.EXPIRED
        assert persisted.status is WorkerJobCancelRequestStatus.SUPERSEDED
    outbox_count = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == scenario.command_id)
    )
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_request_scope_rejects_unlinked_queued_nonpreemptible_and_media_jobs(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    nonpreemptible = await _running_activity_job(unit_of_work_factory, preemptible=False)
    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_NOT_PREEMPTIBLE"):
        await _cancel_request(nonpreemptible)

    queued = await _running_activity_job(unit_of_work_factory, claim=False)
    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_NOT_RUNNING"):
        await _cancel_request(queued)

    media = await _running_activity_job(
        unit_of_work_factory,
        capability="threads.browser.media.local_upload",
        preemptible=True,
    )
    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_CANCELLATION_UNSUPPORTED"):
        await _cancel_request(media)

    linked_scenario = await _running_activity_job(unit_of_work_factory)
    unlinked_queued = await linked_scenario.service.enqueue(
        "threads.browser.feed.browse",
        1,
        assigned_worker_id=linked_scenario.worker_id,
        preemptible=True,
    )
    unlinked = await linked_scenario.service.claim_next(linked_scenario.worker_id)
    assert unlinked is not None and unlinked.id == unlinked_queued.id
    assert unlinked.lease_token is not None
    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_NOT_MATERIALIZED_ACTIVITY"):
        await linked_scenario.service.request_cancel(
            unlinked.id,
            reason_code="GENERIC_JOB",
            now=linked_scenario.clock.now(),
        )


@pytest.mark.parametrize("terminal_outcome", ("complete", "failure", "intervention"))
@pytest.mark.asyncio
async def test_terminal_outcome_and_acknowledgement_race_has_one_winner(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
    terminal_outcome: str,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    lease_token = scenario.lease_token
    assert lease_token is not None
    request = await _cancel_request(scenario)
    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        lease_token,
        {"phase": "BEFORE_NAVIGATION"},
    )
    competing_service = WorkerJobService(unit_of_work_factory, clock=scenario.clock)

    async def end_normally() -> bool:
        try:
            if terminal_outcome == "complete":
                await scenario.service.complete(
                    scenario.job_id,
                    scenario.worker_id,
                    lease_token,
                    {"winner": "complete"},
                )
            elif terminal_outcome == "failure":
                await scenario.service.fail(
                    scenario.job_id,
                    scenario.worker_id,
                    lease_token,
                    error_code="RACE_FAILURE",
                    retryable=False,
                )
            else:
                await scenario.service.request_intervention(
                    scenario.job_id,
                    scenario.worker_id,
                    lease_token,
                    intervention_type="CHALLENGE_REQUIRED",
                    detail_code="RACE_INTERVENTION",
                )
        except WorkerJobControlError:
            return False
        return True

    async def cancel() -> bool:
        try:
            await competing_service.acknowledge_cancel(
                scenario.job_id,
                scenario.worker_id,
                lease_token,
                cancel_request_id=request.id,
                generation=request.generation,
                checkpoint_phase="BEFORE_NAVIGATION",
            )
        except WorkerJobControlError:
            return False
        return True

    normal_won, cancel_won = await asyncio.gather(end_normally(), cancel())
    assert normal_won != cancel_won
    async with unit_of_work_factory() as unit_of_work:
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
        attempt = (await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id))[0]
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
    persisted = await _cancel_request_record(unit_of_work_factory, request.id)
    assert job is not None and command is not None
    if cancel_won:
        assert job.status is WorkerJobStatus.CANCELLED
        assert attempt.status is WorkerJobAttemptStatus.CANCELLED
        assert command.status is CommandStatus.CANCELLED
        assert persisted.status is WorkerJobCancelRequestStatus.ACKNOWLEDGED
    else:
        assert job.status is (
            WorkerJobStatus.SUCCEEDED
            if terminal_outcome == "complete"
            else WorkerJobStatus.FAILED_FINAL
            if terminal_outcome == "failure"
            else WorkerJobStatus.WAITING_INTERVENTION
        )
        assert attempt.status is (
            WorkerJobAttemptStatus.SUCCEEDED
            if terminal_outcome == "complete"
            else WorkerJobAttemptStatus.FAILED_FINAL
            if terminal_outcome == "failure"
            else WorkerJobAttemptStatus.WAITING_INTERVENTION
        )
        assert command.status is (
            CommandStatus.SUCCEEDED
            if terminal_outcome == "complete"
            else CommandStatus.FAILED_FINAL
            if terminal_outcome == "failure"
            else CommandStatus.WAITING_INTERVENTION
        )
        assert persisted.status is WorkerJobCancelRequestStatus.SUPERSEDED
    outbox_count = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == scenario.command_id)
    )
    assert outbox_count == int(cancel_won or terminal_outcome != "intervention")


@pytest.mark.asyncio
async def test_downgrade_refuses_pending_and_acknowledged_cancellation_history(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    pending = await _cancel_request(scenario)
    config = Config("alembic.ini")
    with pytest.raises(RuntimeError, match="WORKER_JOB_CANCELLATION_DOWNGRADE_BLOCKED"):
        await asyncio.to_thread(alembic_command.downgrade, config, "20260929_0012")

    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        {"phase": "BEFORE_NAVIGATION"},
    )
    await scenario.service.acknowledge_cancel(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        cancel_request_id=pending.id,
        generation=pending.generation,
        checkpoint_phase="BEFORE_NAVIGATION",
    )
    with pytest.raises(RuntimeError, match="WORKER_JOB_CANCELLATION_DOWNGRADE_BLOCKED"):
        await asyncio.to_thread(alembic_command.downgrade, config, "20260929_0012")


@pytest.mark.asyncio
async def test_downgrade_refuses_cancelled_terminal_states_without_request_history(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    lease_token = scenario.lease_token
    assert lease_token is not None
    request = await _cancel_request(scenario)
    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        lease_token,
        {"phase": "BEFORE_NAVIGATION"},
    )
    await scenario.service.acknowledge_cancel(
        scenario.job_id,
        scenario.worker_id,
        lease_token,
        cancel_request_id=request.id,
        generation=request.generation,
        checkpoint_phase="BEFORE_NAVIGATION",
    )

    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(
                delete(WorkerJobCancelRequestRecord).where(
                    WorkerJobCancelRequestRecord.id == request.id
                )
            )
    finally:
        await engine.dispose()

    async with unit_of_work_factory() as unit_of_work:
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        attempt = (await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id))[0]
        history = await unit_of_work.worker_job_cancel_requests.get(request.id)
    assert job is not None and job.status is WorkerJobStatus.CANCELLED
    assert command is not None and command.status is CommandStatus.CANCELLED
    assert attempt.status is WorkerJobAttemptStatus.CANCELLED
    assert history is None

    config = Config("alembic.ini")
    try:
        with pytest.raises(RuntimeError, match="WORKER_JOB_CANCELLATION_DOWNGRADE_BLOCKED"):
            await asyncio.to_thread(alembic_command.downgrade, config, "20260929_0012")
    finally:
        await asyncio.to_thread(alembic_command.upgrade, config, "head")


@pytest.mark.asyncio
async def test_migration_upgrade_preserves_existing_command_and_attempt_rows(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    config = Config("alembic.ini")
    await asyncio.to_thread(alembic_command.downgrade, config, "20260929_0012")
    await asyncio.to_thread(alembic_command.upgrade, config, "head")

    async with unit_of_work_factory() as unit_of_work:
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        attempts = await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id)
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
    assert command is not None and command.status is CommandStatus.WAITING_EXECUTION
    assert attempts[0].status is WorkerJobAttemptStatus.RUNNING
    assert job is not None and job.status is WorkerJobStatus.RUNNING


async def _cancel_request_record(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory, request_id: UUID
) -> WorkerJobCancelRequest:
    async with unit_of_work_factory() as unit_of_work:
        record = await unit_of_work.worker_job_cancel_requests.get(request_id)
    assert record is not None
    return record
