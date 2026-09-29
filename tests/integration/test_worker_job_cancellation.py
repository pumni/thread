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
from threads_platform.application.capability_router import CapabilityRouter
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.ports.repositories import UnitOfWork
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobControlError, WorkerJobService
from threads_platform.domain.account_activities import (
    AccountActivityPlan,
    AccountActivityTemplate,
    ActivityPriority,
    ScheduledActivity,
)
from threads_platform.domain.accounts import AccountExecutionMode, ThreadsAccount
from threads_platform.domain.capabilities import (
    BusinessCapabilityPolicy,
    CapabilityExecutionClass,
    CapabilityExecutor,
    OperationClass,
    RouteTarget,
)
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.worker_jobs import (
    WorkerJob,
    WorkerJobAttemptStatus,
    WorkerJobCancelRequest,
    WorkerJobCancelRequestStatus,
    WorkerJobPreemption,
    WorkerJobPreemptionStatus,
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
from threads_platform.infrastructure.persistence.repositories import (
    SQLAlchemyWorkerJobPreemptionRepository,
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
        threads_user_id=f"cancel-test-{uuid4()}",
        username="cancel_test_account",
        execution_mode=AccountExecutionMode.BROWSER_ONLY,
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
    capabilities = [capability]
    if "threads.browser.feed.browse" not in capabilities:
        capabilities.append("threads.browser.feed.browse")
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.workers.add(worker)
        await unit_of_work.browser_profiles.add(profile)
        await unit_of_work.assignments.add(assignment)
        await unit_of_work.worker_capabilities.replace_for_worker(
            worker_id,
            [
                WorkerCapability(worker_id, name, 1, advertised_at=clock.now())
                for name in capabilities
            ],
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


async def _materialize_priority_activity(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    scenario: Scenario,
    priority: ActivityPriority,
) -> str:
    async with unit_of_work_factory() as unit_of_work:
        source_activity = await unit_of_work.scheduled_activities.get(scenario.activity_id)
        assert source_activity is not None
        plan = await unit_of_work.activity_plans.get(source_activity.plan_id)
        assert plan is not None
        template = AccountActivityTemplate(
            account_id=scenario.account_id,
            plan_id=plan.id,
            name=f"{priority.value} preemption candidate",
            activity_type="threads.browser.feed.browse",
            configuration={"max_items": 5},
            priority=priority,
            change_reason="preemption integration test",
            created_at=scenario.clock.now(),
        )
        activity = ScheduledActivity.from_plan_template(
            plan,
            template,
            scenario.clock.now() - timedelta(seconds=1),
            created_at=scenario.clock.now(),
        )
        await unit_of_work.activity_templates.add_revision(template)
        await unit_of_work.scheduled_activities.add_if_absent(activity)
    commands = await materialize_due_account_activities(
        unit_of_work_factory, now=scenario.clock.now(), limit=10
    )
    matching = [command for command in commands if command.command_id == f"activity:{activity.id}"]
    assert len(matching) == 1
    assert matching[0].priority == priority.worker_job_priority
    return matching[0].command_id


async def _route_materialized_activity(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    scenario: Scenario,
    command_id: str,
) -> WorkerJob:
    runtime = CommandRuntime(
        unit_of_work_factory,
        {},
        clock=scenario.clock,
        worker_job_service=scenario.service,
    )
    result = await runtime.process(command_id)
    assert result.status is CommandStatus.WAITING_EXECUTION
    async with unit_of_work_factory() as unit_of_work:
        job = await unit_of_work.worker_jobs.get_by_command_id(command_id)
    assert job is not None
    return job


async def _enqueue_high_activity(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    scenario: Scenario,
    *,
    deadline_in: timedelta | None = None,
) -> WorkerJob:
    if deadline_in is not None:
        now = scenario.clock.now()
        command = Command(
            command_id=f"internal-high-feed:{uuid4()}",
            correlation_id=f"internal-high-feed-correlation:{uuid4()}",
            account_id=scenario.account_id,
            command_type="threads.browser.feed.browse",
            payload={"max_items": 5},
            priority=ActivityPriority.HIGH.worker_job_priority,
            created_at=now,
            received_at=now,
            deadline_at=now + deadline_in,
        )
        command.transition(CommandStatus.VALIDATED, now)
        async with unit_of_work_factory() as unit_of_work:
            await unit_of_work.commands.add(command)
        return await _route_materialized_activity(
            unit_of_work_factory, scenario, command.command_id
        )

    command_id = await _materialize_priority_activity(
        unit_of_work_factory, scenario, ActivityPriority.HIGH
    )
    return await _route_materialized_activity(unit_of_work_factory, scenario, command_id)


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
async def test_preemption_migration_upgrade_preserves_existing_command_and_attempt_rows(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    pending = await _cancel_request(scenario)
    config = Config("alembic.ini")
    await asyncio.to_thread(alembic_command.downgrade, config, "20260929_0013")
    await asyncio.to_thread(alembic_command.upgrade, config, "head")

    async with unit_of_work_factory() as unit_of_work:
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        attempts = await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id)
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
        request = await unit_of_work.worker_job_cancel_requests.get(pending.id)
    assert command is not None and command.status is CommandStatus.WAITING_EXECUTION
    assert attempts[0].status is WorkerJobAttemptStatus.RUNNING
    assert job is not None and job.status is WorkerJobStatus.RUNNING
    assert request is not None and request.status is WorkerJobCancelRequestStatus.PENDING


@pytest.mark.asyncio
async def test_high_preemption_waits_for_safe_ack_then_claims(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    high_job = await _enqueue_high_activity(unit_of_work_factory, scenario)

    async with unit_of_work_factory() as unit_of_work:
        victim = await unit_of_work.worker_jobs.get(scenario.job_id)
        high_command = await unit_of_work.commands.get_by_command_id(high_job.command_id or "")
        preemptions = await unit_of_work.worker_job_preemptions.list_waiting_for_account(
            scenario.account_id
        )
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
    assert victim is not None and victim.status is WorkerJobStatus.RUNNING
    assert victim.lease_token == scenario.lease_token
    assert high_command is not None and high_command.priority == high_job.priority == 100
    assert high_job.status is WorkerJobStatus.QUEUED
    assert len(preemptions) == 1
    assert preemptions[0].preemptor_worker_job_id == high_job.id
    assert preemptions[0].victim_worker_job_id == scenario.job_id
    assert request is not None
    assert preemptions[0].cancel_request_id == request.id
    assert await scenario.service.claim_next(scenario.worker_id) is None

    checkpointed = await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        {"phase": "FEED_READY"},
    )
    assert checkpointed.pending_cancel_request is not None
    await scenario.service.acknowledge_cancel(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        cancel_request_id=request.id,
        generation=request.generation,
        checkpoint_phase="FEED_READY",
    )

    async with unit_of_work_factory() as unit_of_work:
        victim = await unit_of_work.worker_jobs.get(scenario.job_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        attempts = await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id)
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_job.id, scenario.job_id
        )
    assert victim is not None and victim.status is WorkerJobStatus.CANCELLED
    assert command is not None and command.status is CommandStatus.CANCELLED
    assert attempts[0].status is WorkerJobAttemptStatus.CANCELLED
    assert preemption is not None
    assert preemption.status is WorkerJobPreemptionStatus.SATISFIED
    claimed_high = await scenario.service.claim_next(scenario.worker_id)
    assert claimed_high is not None and claimed_high.id == high_job.id


@pytest.mark.asyncio
async def test_presence_expiry_blocks_new_claims_and_preserves_queued_job(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory, claim=False)
    async with unit_of_work_factory() as unit_of_work:
        worker = await unit_of_work.workers.get_for_update(scenario.worker_id)
        assert worker is not None
        worker.presence_expires_at = scenario.clock.now() - timedelta(seconds=1)
        await unit_of_work.workers.update(worker)

    expired = await WorkerControlService(
        unit_of_work_factory, clock=scenario.clock
    ).expire_presence(now=scenario.clock.now(), limit=1)
    with pytest.raises(WorkerJobControlError, match="WORKER_NOT_ELIGIBLE"):
        await scenario.service.claim_next(scenario.worker_id)
    async with unit_of_work_factory() as unit_of_work:
        queued = await unit_of_work.worker_jobs.get(scenario.job_id)
        attempts = await unit_of_work.worker_job_attempts.list_for_job(scenario.job_id)

    assert expired == 1
    assert queued is not None and queued.status is WorkerJobStatus.QUEUED
    assert attempts == []


@pytest.mark.asyncio
async def test_presence_expiry_preserves_running_lease_and_unresolved_preemption(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    high_job = await _enqueue_high_activity(unit_of_work_factory, scenario)
    async with unit_of_work_factory() as unit_of_work:
        before = await unit_of_work.worker_jobs.get(scenario.job_id)
    assert before is not None
    async with unit_of_work_factory() as unit_of_work:
        worker = await unit_of_work.workers.get_for_update(scenario.worker_id)
        assert worker is not None
        worker.presence_expires_at = scenario.clock.now() - timedelta(seconds=1)
        await unit_of_work.workers.update(worker)

    service = WorkerControlService(unit_of_work_factory, clock=scenario.clock)
    assert await service.expire_presence(now=scenario.clock.now(), limit=1) == 1
    assert await scenario.service.recover_expired(now=scenario.clock.now()) == 0

    async with unit_of_work_factory() as unit_of_work:
        worker = await unit_of_work.workers.get(scenario.worker_id)
        victim = await unit_of_work.worker_jobs.get(scenario.job_id)
        attempt = await unit_of_work.worker_job_attempts.get_running_for_update(scenario.job_id)
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_job.id, scenario.job_id
        )
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)

    assert worker is not None and worker.status is WorkerStatus.OFFLINE
    assert victim is not None and victim.status is WorkerJobStatus.RUNNING
    assert victim.lease_worker_id == scenario.worker_id
    assert victim.lease_token == scenario.lease_token
    assert victim.lease_expires_at == before.lease_expires_at
    assert victim.account_coordination_generation == before.account_coordination_generation
    assert attempt is not None and attempt.status is WorkerJobAttemptStatus.RUNNING
    assert preemption is not None
    assert preemption.status is WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE
    assert request is not None and request.status is WorkerJobCancelRequestStatus.PENDING


@pytest.mark.asyncio
async def test_expired_victim_remains_blocking_until_reclaim_and_new_generation_ack(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    old_lease_token = scenario.lease_token
    assert old_lease_token is not None
    high_job = await _enqueue_high_activity(unit_of_work_factory, scenario)
    async with unit_of_work_factory() as unit_of_work:
        first_request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(
            scenario.job_id
        )
    assert first_request is not None and first_request.generation == 1

    scenario.clock.advance(timedelta(seconds=6))
    reclaimed = await scenario.service.claim_next(scenario.worker_id)
    assert reclaimed is not None and reclaimed.id == scenario.job_id
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_token is not None and reclaimed.lease_token != old_lease_token
    new_request = reclaimed.pending_cancel_request
    assert new_request is not None
    assert new_request.id != first_request.id
    assert new_request.generation == first_request.generation + 1

    async with unit_of_work_factory() as unit_of_work:
        old_stored = await unit_of_work.worker_job_cancel_requests.get(first_request.id)
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_job.id, scenario.job_id
        )
    assert old_stored is not None
    assert old_stored.status is WorkerJobCancelRequestStatus.SUPERSEDED
    assert preemption is not None
    assert preemption.status is WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE
    assert preemption.cancel_request_id == new_request.id

    with pytest.raises(WorkerJobControlError, match="WORKER_JOB_LEASE_LOST"):
        await scenario.service.acknowledge_cancel(
            scenario.job_id,
            scenario.worker_id,
            old_lease_token,
            cancel_request_id=first_request.id,
            generation=first_request.generation,
            checkpoint_phase="BEFORE_NAVIGATION",
        )
    assert await scenario.service.claim_next(scenario.worker_id) is None

    assert reclaimed.lease_token is not None
    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        reclaimed.lease_token,
        {"phase": "BEFORE_NAVIGATION"},
    )
    await scenario.service.acknowledge_cancel(
        scenario.job_id,
        scenario.worker_id,
        reclaimed.lease_token,
        cancel_request_id=new_request.id,
        generation=new_request.generation,
        checkpoint_phase="BEFORE_NAVIGATION",
    )
    async with unit_of_work_factory() as unit_of_work:
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_job.id, scenario.job_id
        )
    assert preemption is not None and preemption.status is WorkerJobPreemptionStatus.SATISFIED
    claimed_high = await scenario.service.claim_next(scenario.worker_id)
    assert claimed_high is not None and claimed_high.id == high_job.id


@pytest.mark.asyncio
async def test_simultaneous_high_arrivals_share_one_cancel_and_claim_gate_serializes(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    command_ids = [
        await _materialize_priority_activity(unit_of_work_factory, scenario, ActivityPriority.HIGH),
        await _materialize_priority_activity(unit_of_work_factory, scenario, ActivityPriority.HIGH),
    ]
    runtime = CommandRuntime(
        unit_of_work_factory,
        {},
        clock=scenario.clock,
        worker_job_service=scenario.service,
    )
    await asyncio.gather(*(runtime.process(command_id) for command_id in command_ids))
    async with unit_of_work_factory() as unit_of_work:
        preemptions = await unit_of_work.worker_job_preemptions.list_waiting_for_account(
            scenario.account_id
        )
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
        high_jobs = [
            await unit_of_work.worker_jobs.get_by_command_id(command_id)
            for command_id in command_ids
        ]
    assert len(preemptions) == 2
    assert request is not None
    assert {preemption.cancel_request_id for preemption in preemptions} == {request.id}
    assert all(job is not None and job.status is WorkerJobStatus.QUEUED for job in high_jobs)
    assert await scenario.service.claim_next(scenario.worker_id) is None

    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        {"phase": "FEED_READY"},
    )
    await scenario.service.acknowledge_cancel(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        cancel_request_id=request.id,
        generation=request.generation,
        checkpoint_phase="FEED_READY",
    )
    claims = await asyncio.gather(
        scenario.service.claim_next(scenario.worker_id),
        WorkerJobService(unit_of_work_factory, clock=scenario.clock).claim_next(scenario.worker_id),
    )
    claimed = [job for job in claims if job is not None]
    assert len(claimed) == 1
    ordered_ids = sorted(job.id for job in high_jobs if job is not None)
    assert claimed[0].id == ordered_ids[0]
    assert await scenario.service.claim_next(scenario.worker_id) is None

    assert claimed[0].lease_token is not None
    await scenario.service.complete(
        claimed[0].id,
        scenario.worker_id,
        claimed[0].lease_token,
        {"result_version": 1, "observations": [], "truncated": False},
    )
    second = await scenario.service.claim_next(scenario.worker_id)
    assert second is not None and second.id == ordered_ids[1]


@pytest.mark.asyncio
async def test_ended_high_preemptor_does_not_supersede_shared_victim_request(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    high_a = await _enqueue_high_activity(
        unit_of_work_factory, scenario, deadline_in=timedelta(seconds=1)
    )
    high_b = await _enqueue_high_activity(unit_of_work_factory, scenario)

    async with unit_of_work_factory() as unit_of_work:
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
        relationship_a = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_a.id, scenario.job_id
        )
        relationship_b = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_b.id, scenario.job_id
        )
    assert request is not None
    assert request.status is WorkerJobCancelRequestStatus.PENDING
    assert relationship_a is not None
    assert relationship_a.status is WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE
    assert relationship_a.cancel_request_id == request.id
    assert relationship_b is not None
    assert relationship_b.status is WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE
    assert relationship_b.cancel_request_id == request.id

    scenario.clock.advance(timedelta(seconds=2))
    assert await scenario.service.recover_expired() >= 1

    async with unit_of_work_factory() as unit_of_work:
        command_a = await unit_of_work.commands.get_by_command_id(high_a.command_id or "")
        high_a_after = await unit_of_work.worker_jobs.get(high_a.id)
        relationship_a = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_a.id, scenario.job_id
        )
        relationship_b = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_b.id, scenario.job_id
        )
        request_after = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(
            scenario.job_id
        )
        victim = await unit_of_work.worker_jobs.get(scenario.job_id)
    assert command_a is not None and command_a.status is CommandStatus.EXPIRED
    assert high_a_after is not None and high_a_after.status is WorkerJobStatus.EXPIRED
    assert relationship_a is not None
    assert relationship_a.status is WorkerJobPreemptionStatus.SUPERSEDED
    assert relationship_b is not None
    assert relationship_b.status is WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE
    assert request_after is not None and request_after.id == request.id
    assert request_after.status is WorkerJobCancelRequestStatus.PENDING
    assert relationship_b.cancel_request_id == request.id
    assert victim is not None and victim.status is WorkerJobStatus.RUNNING
    assert victim.lease_token == scenario.lease_token
    assert await scenario.service.claim_next(scenario.worker_id) is None

    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        {"phase": "FEED_READY"},
    )
    await scenario.service.acknowledge_cancel(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        cancel_request_id=request.id,
        generation=request.generation,
        checkpoint_phase="FEED_READY",
    )

    async with unit_of_work_factory() as unit_of_work:
        victim_command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        victim_after = await unit_of_work.worker_jobs.get(scenario.job_id)
        victim_attempt = await unit_of_work.worker_job_attempts.get_running_for_update(
            scenario.job_id
        )
        relationship_b = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_b.id, scenario.job_id
        )
    assert victim_command is not None and victim_command.status is CommandStatus.CANCELLED
    assert victim_after is not None and victim_after.status is WorkerJobStatus.CANCELLED
    assert victim_attempt is None
    attempts = await scenario.service.attempts(scenario.job_id)
    assert len(attempts) == 1 and attempts[0].status is WorkerJobAttemptStatus.CANCELLED
    assert relationship_b is not None
    assert relationship_b.status is WorkerJobPreemptionStatus.SATISFIED

    claimed_b = await scenario.service.claim_next(scenario.worker_id)
    assert claimed_b is not None and claimed_b.id == high_b.id


@pytest.mark.asyncio
async def test_media_running_blocks_high_without_receiving_cancel_request(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(
        unit_of_work_factory,
        capability="threads.browser.media.local_upload",
        preemptible=False,
    )
    assert scenario.lease_token is not None
    high_job = await _enqueue_high_activity(unit_of_work_factory, scenario)
    async with unit_of_work_factory() as unit_of_work:
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_job.id, scenario.job_id
        )
    assert request is None
    assert preemption is not None
    assert preemption.status is WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE
    assert await scenario.service.claim_next(scenario.worker_id) is None

    await scenario.service.complete(
        scenario.job_id,
        scenario.worker_id,
        scenario.lease_token,
        {"result_version": 1, "staged": True},
    )
    async with unit_of_work_factory() as unit_of_work:
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_job.id, scenario.job_id
        )
    assert preemption is not None and preemption.status is WorkerJobPreemptionStatus.SATISFIED
    claimed_high = await scenario.service.claim_next(scenario.worker_id)
    assert claimed_high is not None and claimed_high.id == high_job.id


@pytest.mark.asyncio
async def test_unlinked_browser_job_blocks_profile_without_receiving_cancel_request(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(
        unit_of_work_factory,
        capability="threads.browser.legacy.read",
        preemptible=True,
    )
    generic = await scenario.service.get(scenario.job_id)
    assert scenario.lease_token is not None

    high_job = await _enqueue_high_activity(unit_of_work_factory, scenario)
    async with unit_of_work_factory() as unit_of_work:
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(generic.id)
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(high_job.id, generic.id)
    assert request is None
    assert preemption is not None
    assert preemption.status is WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE
    assert await scenario.service.claim_next(scenario.worker_id) is None

    await scenario.service.complete(
        generic.id,
        scenario.worker_id,
        scenario.lease_token,
        {"result_version": 1},
    )
    async with unit_of_work_factory() as unit_of_work:
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(high_job.id, generic.id)
    assert preemption is not None and preemption.status is WorkerJobPreemptionStatus.SATISFIED
    claimed_high = await scenario.service.claim_next(scenario.worker_id)
    assert claimed_high is not None and claimed_high.id == high_job.id


@pytest.mark.asyncio
async def test_low_and_normal_do_not_preempt_and_high_arrival_races_claim_safely(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    low_and_normal_ids = [
        await _materialize_priority_activity(unit_of_work_factory, scenario, ActivityPriority.LOW),
        await _materialize_priority_activity(
            unit_of_work_factory, scenario, ActivityPriority.NORMAL
        ),
    ]
    runtime = CommandRuntime(
        unit_of_work_factory,
        {},
        clock=scenario.clock,
        worker_job_service=scenario.service,
    )
    await asyncio.gather(*(runtime.process(command_id) for command_id in low_and_normal_ids))
    async with unit_of_work_factory() as unit_of_work:
        requests = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(
            scenario.job_id
        )
        preemptions = await unit_of_work.worker_job_preemptions.list_waiting_for_account(
            scenario.account_id
        )
    assert requests is None
    assert preemptions == []
    assert await scenario.service.claim_next(scenario.worker_id) is None

    high_command_id = await _materialize_priority_activity(
        unit_of_work_factory, scenario, ActivityPriority.HIGH
    )
    high_runtime = CommandRuntime(
        unit_of_work_factory,
        {},
        clock=scenario.clock,
        worker_job_service=scenario.service,
    )
    claim_result, route_result = await asyncio.gather(
        scenario.service.claim_next(scenario.worker_id),
        high_runtime.process(high_command_id),
    )
    assert claim_result is None
    assert route_result.status is CommandStatus.WAITING_EXECUTION
    async with unit_of_work_factory() as unit_of_work:
        high_job = await unit_of_work.worker_jobs.get_by_command_id(high_command_id)
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
        victim = await unit_of_work.worker_jobs.get(scenario.job_id)
    assert high_job is not None and high_job.status is WorkerJobStatus.QUEUED
    assert request is not None
    assert victim is not None and victim.status is WorkerJobStatus.RUNNING
    assert victim.lease_token == scenario.lease_token


@pytest.mark.asyncio
async def test_high_arbitration_rolls_back_with_command_route_and_cancel_request(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    high_command_id = await _materialize_priority_activity(
        unit_of_work_factory, scenario, ActivityPriority.HIGH
    )
    service = scenario.service
    original = SQLAlchemyWorkerJobPreemptionRepository.add_if_absent

    async def fail_after_arbitration(
        repository: SQLAlchemyWorkerJobPreemptionRepository,
        preemption: WorkerJobPreemption,
    ) -> WorkerJobPreemption:
        await original(repository, preemption)
        raise RuntimeError("simulated transaction abort")

    monkeypatch.setattr(
        SQLAlchemyWorkerJobPreemptionRepository,
        "add_if_absent",
        fail_after_arbitration,
    )
    runtime = CommandRuntime(
        unit_of_work_factory,
        {},
        clock=scenario.clock,
        worker_job_service=service,
    )
    with pytest.raises(RuntimeError, match="simulated transaction abort"):
        await runtime.process(high_command_id)

    async with unit_of_work_factory() as unit_of_work:
        high_job = await unit_of_work.worker_jobs.get_by_command_id(high_command_id)
        high_command = await unit_of_work.commands.get_by_command_id(high_command_id)
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
        preemptions = await unit_of_work.worker_job_preemptions.list_waiting_for_account(
            scenario.account_id
        )
        routes = await unit_of_work.command_route_decisions.get_latest_for_command(high_command_id)
    assert high_job is None
    assert high_command is not None and high_command.status is CommandStatus.RECEIVED
    assert request is None
    assert preemptions == []
    assert routes is None


@pytest.mark.asyncio
async def test_preemption_migration_preserves_rows_and_refuses_history_downgrade(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    high_job = await _enqueue_high_activity(unit_of_work_factory, scenario)
    config = Config("alembic.ini")
    with pytest.raises(RuntimeError, match="WORKER_JOB_PREEMPTION_DOWNGRADE_BLOCKED"):
        await asyncio.to_thread(alembic_command.downgrade, config, "20260929_0013")

    await asyncio.to_thread(alembic_command.upgrade, config, "head")
    async with unit_of_work_factory() as unit_of_work:
        victim_command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        victim_job = await unit_of_work.worker_jobs.get(scenario.job_id)
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_job.id, scenario.job_id
        )
    assert victim_command is not None and victim_command.status is CommandStatus.WAITING_EXECUTION
    assert victim_job is not None and victim_job.status is WorkerJobStatus.RUNNING
    assert preemption is not None
    assert preemption.status is WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE


@pytest.mark.asyncio
async def test_high_does_not_preempt_high_and_profile_gate_is_account_local(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    victim = await _running_activity_job(unit_of_work_factory)
    assert victim.lease_token is not None
    high_one = await _enqueue_high_activity(unit_of_work_factory, victim)
    async with unit_of_work_factory() as unit_of_work:
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(victim.job_id)
    assert request is not None
    await victim.service.checkpoint(
        victim.job_id,
        victim.worker_id,
        victim.lease_token,
        {"phase": "FEED_READY"},
    )
    await victim.service.acknowledge_cancel(
        victim.job_id,
        victim.worker_id,
        victim.lease_token,
        cancel_request_id=request.id,
        generation=request.generation,
        checkpoint_phase="FEED_READY",
    )
    running_high = await victim.service.claim_next(victim.worker_id)
    assert running_high is not None and running_high.id == high_one.id
    assert running_high.lease_token is not None

    high_two = await _enqueue_high_activity(unit_of_work_factory, victim)
    assert high_two.status is WorkerJobStatus.QUEUED
    async with unit_of_work_factory() as unit_of_work:
        high_cancel = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(high_one.id)
        relationships = await unit_of_work.worker_job_preemptions.list_waiting_for_account(
            victim.account_id
        )
    assert high_cancel is None
    assert relationships == []
    assert await victim.service.claim_next(victim.worker_id) is None

    separate_account = await _running_activity_job(unit_of_work_factory, claim=False)
    independent = await separate_account.service.claim_next(separate_account.worker_id)
    assert independent is not None and independent.id == separate_account.job_id


@pytest.mark.asyncio
async def test_nonbrowser_job_can_claim_while_account_browser_job_runs(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    async with unit_of_work_factory() as unit_of_work:
        capabilities = await unit_of_work.worker_capabilities.list_for_worker(scenario.worker_id)
        capabilities.append(
            WorkerCapability(
                scenario.worker_id,
                "synthetic.echo",
                1,
                advertised_at=scenario.clock.now(),
            )
        )
        await unit_of_work.worker_capabilities.replace_for_worker(scenario.worker_id, capabilities)
    unrelated = await scenario.service.enqueue(
        "synthetic.echo",
        1,
        account_id=scenario.account_id,
        assigned_worker_id=scenario.worker_id,
        account_affinity_required=False,
    )
    claimed = await scenario.service.claim_next(scenario.worker_id)
    assert claimed is not None and claimed.id == unrelated.id


@pytest.mark.asyncio
async def test_high_priority_requires_matching_command_and_trusted_worker_route(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory, claim=False)
    command = Command(
        command_id=f"internal-high-{uuid4()}",
        correlation_id=f"internal-high-correlation-{uuid4()}",
        account_id=scenario.account_id,
        command_type="threads.browser.feed.browse",
        payload={"max_items": 5},
        priority=100,
        created_at=scenario.clock.now(),
        received_at=scenario.clock.now(),
    )
    command.transition(CommandStatus.VALIDATED, scenario.clock.now())
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.commands.add(command)

    async def enqueue(priority: int) -> WorkerJob:
        return await scenario.service.enqueue(
            "threads.browser.feed.browse",
            1,
            command_id=command.command_id,
            account_id=scenario.account_id,
            assigned_worker_id=scenario.worker_id,
            account_affinity_required=True,
            priority=priority,
            preemptible=True,
            scheduled_at=scenario.clock.now(),
            input_data={"max_items": 5},
        )

    with pytest.raises(WorkerJobControlError, match="COMMAND_WORKER_JOB_PRIORITY_MISMATCH"):
        await enqueue(0)
    with pytest.raises(WorkerJobControlError, match="HIGH_PRIORITY_ROUTE_NOT_TRUSTED"):
        await enqueue(100)

    async with unit_of_work_factory() as unit_of_work:
        stored_command = await unit_of_work.commands.get_by_command_id(command.command_id)
        job = await unit_of_work.worker_jobs.get_by_command_id(command.command_id)
        relations = await unit_of_work.worker_job_preemptions.list_waiting_for_account(
            scenario.account_id
        )
    assert stored_command is not None and stored_command.status is CommandStatus.VALIDATED
    assert job is None
    assert relations == []


@pytest.mark.asyncio
async def test_trusted_high_nonbrowser_worker_route_does_not_preempt_or_wait_for_browser(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    assert scenario.lease_token is not None
    synthetic_capability = "synthetic.echo"
    policy = BusinessCapabilityPolicy(
        command_type=synthetic_capability,
        capability_name=synthetic_capability,
        capability_version=1,
        execution_class=CapabilityExecutionClass.HYBRID,
        operation_class=OperationClass.READ,
        preferred_executor=CapabilityExecutor.WORKER,
        worker_capability_name=synthetic_capability,
        worker_capability_version=1,
    )
    router = CapabilityRouter({synthetic_capability: policy})
    service = WorkerJobService(
        unit_of_work_factory,
        clock=scenario.clock,
        capability_router=router,
    )
    async with unit_of_work_factory() as unit_of_work:
        capabilities = await unit_of_work.worker_capabilities.list_for_worker(scenario.worker_id)
        capabilities.append(
            WorkerCapability(
                scenario.worker_id,
                synthetic_capability,
                1,
                advertised_at=scenario.clock.now(),
            )
        )
        await unit_of_work.worker_capabilities.replace_for_worker(scenario.worker_id, capabilities)
        command = Command(
            command_id=f"internal-high-{uuid4()}",
            correlation_id=f"internal-high-correlation-{uuid4()}",
            account_id=scenario.account_id,
            command_type=synthetic_capability,
            payload={},
            priority=100,
            created_at=scenario.clock.now(),
            received_at=scenario.clock.now(),
        )
        command.transition(CommandStatus.VALIDATED, scenario.clock.now())
        await unit_of_work.commands.add(command)

    runtime = CommandRuntime(
        unit_of_work_factory,
        {},
        clock=scenario.clock,
        worker_job_service=service,
        capability_router=router,
    )
    result = await runtime.process(command.command_id)
    assert result.status is CommandStatus.WAITING_EXECUTION
    async with unit_of_work_factory() as unit_of_work:
        route = await unit_of_work.command_route_decisions.get_latest_execution_for_command(
            command.command_id
        )
        high_job = await unit_of_work.worker_jobs.get_by_command_id(command.command_id)
        relationship = (
            await unit_of_work.worker_job_preemptions.get_for_pair(high_job.id, scenario.job_id)
            if high_job is not None
            else None
        )
        cancel_request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(
            scenario.job_id
        )
        victim = await unit_of_work.worker_jobs.get(scenario.job_id)
    assert route is not None and route.target is RouteTarget.WORKER_JOB
    assert route.executor is CapabilityExecutor.WORKER
    assert high_job is not None and high_job.priority == 100
    assert high_job.command_id == command.command_id
    assert high_job.account_affinity_required
    assert relationship is None
    assert cancel_request is None
    assert victim is not None and victim.status is WorkerJobStatus.RUNNING
    assert victim.lease_token == scenario.lease_token

    claimed_high = await service.claim_next(scenario.worker_id)
    assert claimed_high is not None and claimed_high.id == high_job.id
    async with unit_of_work_factory() as unit_of_work:
        victim_after_claim = await unit_of_work.worker_jobs.get(scenario.job_id)
        relationships_after_claim = (
            await unit_of_work.worker_job_preemptions.list_waiting_for_account(scenario.account_id)
        )
    assert victim_after_claim is not None
    assert victim_after_claim.status is WorkerJobStatus.RUNNING
    assert victim_after_claim.lease_token == scenario.lease_token
    assert relationships_after_claim == []


@pytest.mark.parametrize("outcome", ["fail", "intervention"])
@pytest.mark.asyncio
async def test_explicit_attempt_end_satisfies_preemption_without_relabeling_outcome(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    outcome: str,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    lease_token = scenario.lease_token
    assert lease_token is not None
    high_job = await _enqueue_high_activity(unit_of_work_factory, scenario)
    async with unit_of_work_factory() as unit_of_work:
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
    assert request is not None

    if outcome == "fail":
        await scenario.service.fail(
            scenario.job_id,
            scenario.worker_id,
            lease_token,
            error_code="READ_FAILED",
            retryable=True,
        )
        expected_status = WorkerJobStatus.FAILED_RETRYABLE
    else:
        await scenario.service.request_intervention(
            scenario.job_id,
            scenario.worker_id,
            lease_token,
            intervention_type="LOGIN_REQUIRED",
            detail_code="SESSION_EXPIRED",
        )
        expected_status = WorkerJobStatus.WAITING_INTERVENTION

    async with unit_of_work_factory() as unit_of_work:
        victim = await unit_of_work.worker_jobs.get(scenario.job_id)
        stored_request = await unit_of_work.worker_job_cancel_requests.get(request.id)
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_job.id, scenario.job_id
        )
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
    assert victim is not None and victim.status is expected_status
    assert stored_request is not None
    assert stored_request.status is WorkerJobCancelRequestStatus.SUPERSEDED
    assert preemption is not None
    assert preemption.status is WorkerJobPreemptionStatus.SATISFIED
    assert command is not None and command.status is not CommandStatus.CANCELLED
    claimed_high = await scenario.service.claim_next(scenario.worker_id)
    assert claimed_high is not None and claimed_high.id == high_job.id


@pytest.mark.asyncio
async def test_completion_and_cancel_ack_race_has_one_terminal_winner(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    lease_token = scenario.lease_token
    assert lease_token is not None
    high_job = await _enqueue_high_activity(unit_of_work_factory, scenario)
    async with unit_of_work_factory() as unit_of_work:
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(scenario.job_id)
    assert request is not None
    await scenario.service.checkpoint(
        scenario.job_id,
        scenario.worker_id,
        lease_token,
        {"phase": "FEED_READY"},
    )
    results = await asyncio.gather(
        scenario.service.acknowledge_cancel(
            scenario.job_id,
            scenario.worker_id,
            lease_token,
            cancel_request_id=request.id,
            generation=request.generation,
            checkpoint_phase="FEED_READY",
        ),
        scenario.service.complete(
            scenario.job_id,
            scenario.worker_id,
            lease_token,
            {"result_version": 1, "observations": [], "truncated": False},
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert sum(isinstance(result, WorkerJobControlError) for result in results) == 1
    async with unit_of_work_factory() as unit_of_work:
        victim = await unit_of_work.worker_jobs.get(scenario.job_id)
        preemption = await unit_of_work.worker_job_preemptions.get_for_pair(
            high_job.id, scenario.job_id
        )
        stored_request = await unit_of_work.worker_job_cancel_requests.get(request.id)
    assert victim is not None
    assert victim.status in {WorkerJobStatus.CANCELLED, WorkerJobStatus.SUCCEEDED}
    assert preemption is not None and preemption.status is WorkerJobPreemptionStatus.SATISFIED
    assert stored_request is not None
    assert stored_request.status in {
        WorkerJobCancelRequestStatus.ACKNOWLEDGED,
        WorkerJobCancelRequestStatus.SUPERSEDED,
    }
    claimed_high = await scenario.service.claim_next(scenario.worker_id)
    assert claimed_high is not None and claimed_high.id == high_job.id


@pytest.mark.asyncio
async def test_preemption_revision_upgrade_preserves_existing_activity_rows(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    scenario = await _running_activity_job(unit_of_work_factory)
    config = Config("alembic.ini")
    await asyncio.to_thread(alembic_command.downgrade, config, "20260929_0013")
    await asyncio.to_thread(alembic_command.upgrade, config, "head")
    async with unit_of_work_factory() as unit_of_work:
        activity = await unit_of_work.scheduled_activities.get(scenario.activity_id)
        command = await unit_of_work.commands.get_by_command_id(scenario.command_id)
        job = await unit_of_work.worker_jobs.get(scenario.job_id)
    assert activity is not None
    assert command is not None and command.status is CommandStatus.WAITING_EXECUTION
    assert job is not None and job.status is WorkerJobStatus.RUNNING


async def _cancel_request_record(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory, request_id: UUID
) -> WorkerJobCancelRequest:
    async with unit_of_work_factory() as unit_of_work:
        record = await unit_of_work.worker_job_cancel_requests.get(request_id)
    assert record is not None
    return record
