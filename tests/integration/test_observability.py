import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
import structlog
from structlog.testing import capture_logs

from threads_platform.application.commands.handlers import (
    CommandExecutionContext,
    CommandExecutionOutput,
)
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.crm_protocol_v1 import (
    CommandEnvelopeV1,
    PublishTextPostPayload,
)
from threads_platform.application.errors import RetryableCommandError
from threads_platform.application.readiness import (
    FleetReadiness,
    OverallReadiness,
)
from threads_platform.application.scheduler import run_scheduler_tick
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    WorkerCapability,
    WorkerNode,
    WorkerStatus,
)
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.readiness import (
    PostgresOperationalReadinessProbe,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration

_COMMAND_PAYLOAD_SENTINEL = "SYNTHETIC_COMMAND_PAYLOAD_SENTINEL"
_COMMAND_RESULT_SENTINEL = "SYNTHETIC_COMMAND_RESULT_SENTINEL"
_JOB_INPUT_SENTINEL = "SYNTHETIC_WORKER_JOB_INPUT_SENTINEL"
_JOB_RESULT_SENTINEL = "SYNTHETIC_WORKER_JOB_RESULT_SENTINEL"


class FixedClock:
    def __init__(self, current_time: datetime | None = None) -> None:
        self.current_time = current_time or datetime.now(UTC)

    def now(self) -> datetime:
        return self.current_time


async def _add_account(unit_of_work_factory: SQLAlchemyUnitOfWorkFactory) -> ThreadsAccount:
    account = ThreadsAccount(
        threads_user_id=f"observability-user-{uuid4()}",
        username=f"observability-{uuid4().hex[:12]}",
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
    return account


def _command_body(
    account_id: UUID,
    *,
    command_id: str,
    correlation_id: str,
    text: str = _COMMAND_PAYLOAD_SENTINEL,
    command_type: str = "threads.publish_text",
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "command_id": command_id,
        "correlation_id": correlation_id,
        "account_id": str(account_id),
        "created_at": datetime.now(UTC).isoformat(),
        "command_type": command_type,
        "payload": {"text": text} if command_type == "threads.publish_text" else {},
    }


class LoggingCommandHandler:
    def __init__(self, *, retry_text: str | None = None) -> None:
        self._retry_text = retry_text

    async def execute(
        self,
        command: CommandEnvelopeV1,
        context: CommandExecutionContext,
    ) -> CommandExecutionOutput:
        del context
        command_text = cast(PublishTextPostPayload, command.payload).text
        if command_text == self._retry_text:
            raise RetryableCommandError("RETRYABLE_TEST_ERROR")
        return CommandExecutionOutput(result={"result_value": _COMMAND_RESULT_SENTINEL})


class ConcurrentCommandHandler:
    def __init__(self) -> None:
        self.started_count = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        command: object,
        context: CommandExecutionContext,
    ) -> CommandExecutionOutput:
        del command, context
        self.started_count += 1
        if self.started_count == 2:
            self.both_started.set()
        await self.release.wait()
        return CommandExecutionOutput(result={"status": "done"})


async def test_command_lifecycle_logs_are_correlated_and_payload_free(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account = await _add_account(unit_of_work_factory)
    retry_text = "SYNTHETIC_RETRY_TEXT"
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": LoggingCommandHandler(retry_text=retry_text)},
        clock=FixedClock(),
    )
    success_body = _command_body(
        account.id,
        command_id="cmd-observability-success",
        correlation_id="corr-observability-success",
    )
    retry_body = _command_body(
        account.id,
        command_id="cmd-observability-retry",
        correlation_id="corr-observability-retry",
        text=retry_text,
    )
    rejected_body = _command_body(
        account.id,
        command_id="cmd-observability-rejected",
        correlation_id="corr-observability-rejected",
        command_type="threads.not_registered",
    )

    structlog.contextvars.clear_contextvars()
    try:
        with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
            success_receipt = await runtime.receive(success_body)
            duplicate = await runtime.receive(success_body)
            success_result = await runtime.process(success_receipt.command_id)
            rejected = await runtime.receive(rejected_body)
            retry_receipt = await runtime.receive(retry_body)
            retry_result = await runtime.process(retry_receipt.command_id)

            assert structlog.contextvars.get_contextvars() == {}
    finally:
        structlog.contextvars.clear_contextvars()

    serialized = json.dumps(logs)
    assert success_result.status is CommandStatus.SUCCEEDED
    assert duplicate.duplicate
    assert rejected.status is CommandStatus.REJECTED
    assert retry_result.status is CommandStatus.FAILED_RETRYABLE
    assert _COMMAND_PAYLOAD_SENTINEL not in serialized
    assert _COMMAND_RESULT_SENTINEL not in serialized
    assert all(
        not {"payload", "result", "checkpoint", "lease_token"} & event.keys() for event in logs
    )

    success_events = [
        event for event in logs if event.get("command_id") == success_receipt.command_id
    ]
    assert {event["event"] for event in success_events} >= {
        "command_accepted",
        "command_duplicate",
        "command_processing_started",
        "command_route_selected",
        "command_execution_started",
        "command_execution_outcome",
    }
    for event in success_events:
        assert event["correlation_id"] == "corr-observability-success"
        assert event["account_id"] == str(account.id)
        assert event["command_type"] == "threads.publish_text"
    assert any(
        event.get("error_code") == "UNKNOWN_COMMAND"
        and event.get("status") == CommandStatus.REJECTED.value
        for event in logs
        if event.get("command_id") == rejected.command_id
    )
    assert any(
        event.get("status") == CommandStatus.FAILED_RETRYABLE.value
        and event.get("error_code") == "RETRYABLE_TEST_ERROR"
        for event in logs
        if event.get("command_id") == retry_receipt.command_id
    )


async def test_concurrent_command_context_does_not_cross_contaminate(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account_a = await _add_account(unit_of_work_factory)
    account_b = await _add_account(unit_of_work_factory)
    handler = ConcurrentCommandHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=FixedClock(),
    )
    body_a = _command_body(
        account_a.id,
        command_id="cmd-concurrent-a",
        correlation_id="corr-concurrent-a",
        text="synthetic A payload",
    )
    body_b = _command_body(
        account_b.id,
        command_id="cmd-concurrent-b",
        correlation_id="corr-concurrent-b",
        text="synthetic B payload",
    )

    structlog.contextvars.clear_contextvars()
    try:
        with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
            receipt_a, receipt_b = await asyncio.gather(
                runtime.receive(body_a), runtime.receive(body_b)
            )
            tasks = [
                asyncio.create_task(runtime.process(receipt_a.command_id)),
                asyncio.create_task(runtime.process(receipt_b.command_id)),
            ]
            await asyncio.wait_for(handler.both_started.wait(), timeout=10)
            handler.release.set()
            await asyncio.gather(*tasks)
            assert structlog.contextvars.get_contextvars() == {}
    finally:
        structlog.contextvars.clear_contextvars()

    events_a = [event for event in logs if event.get("command_id") == receipt_a.command_id]
    events_b = [event for event in logs if event.get("command_id") == receipt_b.command_id]
    assert events_a and events_b
    rendered_a = json.dumps(events_a)
    rendered_b = json.dumps(events_b)
    for other_identifier in (
        receipt_b.command_id,
        "corr-concurrent-b",
        str(account_b.id),
    ):
        assert other_identifier not in rendered_a
    for other_identifier in (
        receipt_a.command_id,
        "corr-concurrent-a",
        str(account_a.id),
    ):
        assert other_identifier not in rendered_b
    assert all(event["correlation_id"] == "corr-concurrent-a" for event in events_a)
    assert all(event["correlation_id"] == "corr-concurrent-b" for event in events_b)
    assert structlog.contextvars.get_contextvars() == {}


async def _add_worker_for_observability(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    now: datetime,
) -> tuple[UUID, ThreadsAccount, Command]:
    worker_id = uuid4()
    account = ThreadsAccount(
        threads_user_id=f"observability-job-user-{uuid4()}",
        username=f"observability-job-{uuid4().hex[:12]}",
    )
    worker = WorkerNode(
        worker_id=worker_id,
        display_name=f"Observability worker {worker_id}",
        hostname=f"observability-host-{worker_id}",
        platform="windows",
        protocol_version=1,
        capabilities_schema_version=1,
        status=WorkerStatus.ONLINE,
        last_heartbeat_at=now,
        presence_expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    profile = BrowserProfile(worker_id, f"profile-{uuid4()}")
    command = Command(
        command_id=f"cmd-worker-job-{uuid4()}",
        correlation_id=f"corr-worker-job-{uuid4()}",
        account_id=account.id,
        command_type="threads.publish_text",
        payload={"text": "synthetic command body"},
        created_at=now,
        received_at=now,
        deadline_at=now + timedelta(hours=1),
    )
    command.transition(CommandStatus.VALIDATED, now)
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.workers.add(worker)
        await unit_of_work.worker_capabilities.replace_for_worker(
            worker_id,
            [WorkerCapability(worker_id, "synthetic.observability", 1, advertised_at=now)],
        )
        await unit_of_work.browser_profiles.add(profile)
        await unit_of_work.assignments.add(
            AccountWorkerAssignment(account.id, worker_id, profile.profile_ref, assigned_at=now)
        )
        await unit_of_work.commands.add(command)
    return worker_id, account, command


async def test_worker_job_lifecycle_logs_correlate_without_execution_documents(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock()
    worker_id, account, command = await _add_worker_for_observability(
        unit_of_work_factory, clock.now()
    )
    jobs = WorkerJobService(unit_of_work_factory, clock=clock)

    structlog.contextvars.clear_contextvars()
    try:
        with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
            queued = await jobs.enqueue(
                "synthetic.observability",
                1,
                command_id=command.command_id,
                account_id=account.id,
                assigned_worker_id=worker_id,
                account_affinity_required=True,
                input_data={"sentinel": _JOB_INPUT_SENTINEL},
            )
            claimed = await jobs.claim_next(worker_id)
            assert claimed is not None
            lease_token = claimed.lease_token
            assert lease_token is not None
            completed = await jobs.complete(
                queued.id,
                worker_id,
                lease_token,
                {"sentinel": _JOB_RESULT_SENTINEL},
            )
    finally:
        structlog.contextvars.clear_contextvars()

    serialized = json.dumps(logs)
    assert completed.status.value == "SUCCEEDED"
    assert _JOB_INPUT_SENTINEL not in serialized
    assert _JOB_RESULT_SENTINEL not in serialized
    assert str(lease_token) not in serialized
    lifecycle_events = [
        event
        for event in logs
        if event.get("event")
        in {"worker_job_enqueued", "worker_job_claimed", "worker_job_completed"}
    ]
    assert {event["event"] for event in lifecycle_events} == {
        "worker_job_enqueued",
        "worker_job_claimed",
        "worker_job_completed",
    }
    for event in lifecycle_events:
        assert event["worker_job_id"] == str(queued.id)
        assert event["command_id"] == command.command_id
        assert event["account_id"] == str(account.id)
        assert event["worker_id"] == str(worker_id)
        assert event["capability_name"] == "synthetic.observability"
        assert not {"input_data", "checkpoint", "result", "lease_token"} & event.keys()
    enqueue_event = next(
        event for event in lifecycle_events if event["event"] == "worker_job_enqueued"
    )
    assert enqueue_event["correlation_id"] == command.correlation_id
    assert structlog.contextvars.get_contextvars() == {}


async def test_expired_worker_job_recovery_emits_one_bounded_summary(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock()
    worker_id, account, command = await _add_worker_for_observability(
        unit_of_work_factory, clock.now()
    )
    jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    queued = await jobs.enqueue(
        "synthetic.observability",
        1,
        command_id=command.command_id,
        account_id=account.id,
        assigned_worker_id=worker_id,
        account_affinity_required=True,
        max_attempts=1,
    )
    assert await jobs.claim_next(worker_id) is not None
    recovery_time = clock.now() + timedelta(minutes=3)

    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        recovered = await jobs.recover_expired(now=recovery_time)

    recovery_events = [
        event for event in logs if event.get("event") == "worker_job_expired_lease_recovery"
    ]
    assert recovered == 1
    assert len(recovery_events) == 1
    assert recovery_events[0]["recovered_count"] == 1
    assert recovery_events[0]["status_counts"] == {"FAILED_FINAL": 1}
    assert str(queued.id) not in json.dumps(recovery_events)


async def test_scheduler_emits_one_bounded_tick_summary(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    command_runtime = CommandRuntime(unit_of_work_factory, {}, clock=FixedClock(now))
    worker_jobs = WorkerJobService(unit_of_work_factory, clock=FixedClock(now))

    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        await run_scheduler_tick(
            unit_of_work_factory,
            command_runtime,
            worker_jobs,
            now=now,
            activity_limit=1,
            command_limit=1,
            recovery_limit=1,
        )

    summaries = [event for event in logs if event.get("event") == "scheduler_tick_finished"]
    assert len(summaries) == 1
    assert "scheduler_tick_started" not in {event.get("event") for event in logs}
    assert summaries[0]["commands_processed"] == 0
    assert summaries[0]["worker_jobs_recovered"] == 0
    assert isinstance(summaries[0]["elapsed_seconds"], float)


async def test_postgres_readiness_aggregates_real_worker_statuses_without_mutation(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    statuses = [
        WorkerStatus.ONLINE,
        WorkerStatus.DEGRADED,
        WorkerStatus.DRAINING,
        WorkerStatus.OFFLINE,
        WorkerStatus.REGISTERING,
        WorkerStatus.UPGRADE_REQUIRED,
        WorkerStatus.DISABLED,
    ]
    workers = [
        WorkerNode(
            worker_id=uuid4(),
            display_name=f"Readiness worker {status.value}",
            hostname=f"readiness-host-{status.value}",
            platform="windows",
            status=status,
            created_at=now,
            updated_at=now,
        )
        for status in statuses
    ]
    async with unit_of_work_factory() as unit_of_work:
        for worker in workers:
            await unit_of_work.workers.add(worker)

    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    probe = PostgresOperationalReadinessProbe(create_session_factory(engine))
    try:
        snapshots = await asyncio.gather(*(probe.snapshot() for _ in range(4)))
    finally:
        await engine.dispose()

    snapshot = snapshots[0]
    assert all(item == snapshot for item in snapshots)
    assert snapshot.overall is OverallReadiness.DEGRADED
    assert snapshot.fleet is FleetReadiness.DEGRADED
    assert snapshot.workers_available
    assert snapshot.workers.total == 6
    assert snapshot.workers.online == 1
    assert snapshot.workers.degraded == 1
    assert snapshot.workers.draining == 1
    assert snapshot.workers.offline == 1
    assert snapshot.workers.registering == 1
    assert snapshot.workers.upgrade_required == 1

    async with unit_of_work_factory() as unit_of_work:
        persisted = [await unit_of_work.workers.get(worker.worker_id) for worker in workers]
    assert all(item is not None for item in persisted)
    assert [item.status for item in persisted if item is not None] == statuses
    assert [item.updated_at for item in persisted if item is not None] == [now] * len(statuses)


async def test_postgres_readiness_reports_empty_and_healthy_fleets(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    probe = PostgresOperationalReadinessProbe(create_session_factory(engine))
    try:
        empty = await probe.snapshot()
        now = datetime.now(UTC)
        worker = WorkerNode(
            worker_id=uuid4(),
            display_name="Healthy readiness worker",
            hostname="healthy-readiness-host",
            platform="windows",
            status=WorkerStatus.ONLINE,
            created_at=now,
            updated_at=now,
        )
        async with unit_of_work_factory() as unit_of_work:
            await unit_of_work.workers.add(worker)
        healthy = await probe.snapshot()
    finally:
        await engine.dispose()

    assert empty.overall is OverallReadiness.READY
    assert empty.fleet is FleetReadiness.EMPTY
    assert empty.workers.total == 0
    assert healthy.overall is OverallReadiness.READY
    assert healthy.fleet is FleetReadiness.HEALTHY
    assert healthy.workers.total == 1
    assert healthy.workers.online == 1
