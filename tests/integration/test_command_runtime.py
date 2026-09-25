import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.app import create_app
from threads_platform.application.commands.handlers import (
    CommandExecutionContext,
    CommandExecutionOutput,
)
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.crm_protocol_v1 import (
    CommandEnvelopeV1,
    CRMCommandResultV1,
)
from threads_platform.application.errors import CRMUnavailable, RetryableCommandError
from threads_platform.application.outbox_delivery import OutboxDeliveryWorker
from threads_platform.application.retry import RetryPolicy
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.config.settings import Settings
from threads_platform.domain.account_execution import AccountExecutionOwnerType
from threads_platform.domain.accounts import AccountExecutionMode, ThreadsAccount
from threads_platform.domain.capabilities import CapabilityExecutor, OperationClass
from threads_platform.domain.commands import AttemptStatus, CommandStatus
from threads_platform.domain.outbox import DeliveryStatus, OutboxEvent, OutboxStatus
from threads_platform.domain.publishing import ThreadPost
from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    WorkerCapability,
    WorkerNode,
    WorkerStatus,
)
from threads_platform.infrastructure.persistence.models import (
    CommandAttemptRecord,
    OutboxEventRecord,
    PostRecord,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.transport.http.auth import BearerTokenAuthenticator

pytestmark = pytest.mark.integration


class FixedClock:
    def __init__(self, current_time: datetime) -> None:
        self.current_time = current_time

    def now(self) -> datetime:
        return self.current_time

    def advance(self, duration: timedelta) -> None:
        self.current_time += duration


class RecordingHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> CommandExecutionOutput:
        self.calls += 1
        text = getattr(command.payload, "text", None)
        post = ThreadPost(
            account_id=command.account_id,
            threads_post_id=f"local-result-{command.command_id}",
            text=text if isinstance(text, str) else None,
        )
        return CommandExecutionOutput(result={"local_result_id": command.command_id}, posts=(post,))


class RetryOnceHandler(RecordingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> CommandExecutionOutput:
        if self.failures_remaining:
            self.failures_remaining -= 1
            self.calls += 1
            raise RetryableCommandError("THREADS_TEMPORARILY_UNAVAILABLE")
        return await super().execute(command, context)


class CRMStub:
    def __init__(self, *, unavailable_once: bool = False, disconnect_once: bool = False) -> None:
        self.results: list[CRMCommandResultV1] = []
        self.unavailable_once = unavailable_once
        self.disconnect_once = disconnect_once

    async def deliver_result(self, result: CRMCommandResultV1) -> str | None:
        self.results.append(result)
        if self.unavailable_once:
            self.unavailable_once = False
            raise CRMUnavailable(timedelta(seconds=1))
        if self.disconnect_once:
            self.disconnect_once = False
            raise ConnectionError("simulated CRM connection loss")
        return f"delivery-{len(self.results)}"


class SimulatedProcessCrash(BaseException):
    pass


class CheckpointingHandler(RecordingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.observed_checkpoints: list[dict[str, object]] = []

    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> CommandExecutionOutput:
        self.calls += 1
        self.observed_checkpoints.append(dict(context.checkpoint.data))
        if self.calls == 1:
            await context.checkpoint.save({"remote_container_id": "container-test"})
            raise SimulatedProcessCrash
        assert context.checkpoint.data == {"remote_container_id": "container-test"}
        return CommandExecutionOutput(result={"remote_container_id": "container-test"})


class BlockingHandler(RecordingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> CommandExecutionOutput:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return CommandExecutionOutput(result={"done": True})


class UnexpectedFailureHandler(RecordingHandler):
    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> CommandExecutionOutput:
        self.calls += 1
        raise ValueError("details are deliberately not persisted")


async def add_account(unit_of_work_factory: SQLAlchemyUnitOfWorkFactory) -> UUID:
    account = ThreadsAccount(
        threads_user_id=f"test-user-{uuid4()}",
        username="integration-test",
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
    return account.id


def command_body(
    account_id: UUID,
    clock: FixedClock,
    *,
    command_id: str | None = None,
    command_type: str = "threads.publish_text",
    text: str = "This command is handled by a test adapter.",
    deadline_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "command_id": command_id or f"command-{uuid4()}",
        "correlation_id": f"correlation-{uuid4()}",
        "account_id": str(account_id),
        "created_at": clock.now().isoformat(),
        "deadline_at": deadline_at.isoformat() if deadline_at else None,
        "command_type": command_type,
        "payload": {"text": text} if command_type == "threads.publish_text" else {},
    }


def deterministic_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=3,
        base_delay=timedelta(seconds=1),
        max_delay=timedelta(seconds=4),
        jitter_ratio=0,
        jitter_source=lambda lower, _: lower,
    )


async def test_duplicate_command_never_executes_handler_twice(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = RecordingHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
    )
    body = command_body(account_id, clock)

    first_receipt = await runtime.receive(body)
    duplicate_receipt = await runtime.receive(body)
    first_run = await runtime.process(first_receipt.command_id)
    duplicate_run = await runtime.process(first_receipt.command_id)

    assert first_receipt.status is CommandStatus.RECEIVED
    assert duplicate_receipt.duplicate is True
    assert first_run.executed is True
    assert duplicate_run.executed is False
    assert handler.calls == 1
    assert duplicate_run.status is CommandStatus.SUCCEEDED
    posts = await db_session.scalar(
        select(func.count())
        .select_from(PostRecord)
        .where(PostRecord.threads_post_id == f"local-result-{first_receipt.command_id}")
    )
    results = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == first_receipt.command_id)
    )
    assert posts == 1
    assert results == 1


async def test_unknown_command_is_rejected_without_crashing_worker(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = RecordingHandler()
    runtime = CommandRuntime(unit_of_work_factory, {"threads.publish_text": handler}, clock=clock)
    body = command_body(account_id, clock, command_type="threads.not_registered")

    receipt = await runtime.receive(body)
    outcome = await runtime.process(receipt.command_id)

    assert receipt.status is CommandStatus.REJECTED
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.executed is False
    assert handler.calls == 0


async def test_application_worker_claims_and_executes_next_command(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = RecordingHandler()
    runtime = CommandRuntime(unit_of_work_factory, {"threads.publish_text": handler}, clock=clock)
    receipt = await runtime.receive(command_body(account_id, clock))

    outcome = await runtime.process_next()
    idle = await runtime.process_next()

    assert outcome is not None
    assert outcome.command_id == receipt.command_id
    assert outcome.status is CommandStatus.SUCCEEDED
    assert outcome.executed is True
    assert idle is None
    assert handler.calls == 1


async def test_command_that_expires_in_inbox_never_executes(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    start = datetime.now(UTC)
    clock = FixedClock(start)
    account_id = await add_account(unit_of_work_factory)
    handler = RecordingHandler()
    runtime = CommandRuntime(unit_of_work_factory, {"threads.publish_text": handler}, clock=clock)
    deadline = start + timedelta(seconds=1)
    receipt = await runtime.receive(command_body(account_id, clock, deadline_at=deadline))

    clock.advance(timedelta(seconds=1))
    outcome = await runtime.process_next()

    assert receipt.status is CommandStatus.RECEIVED
    assert outcome is not None
    assert outcome.status is CommandStatus.EXPIRED
    assert outcome.executed is False
    assert handler.calls == 0


async def test_retryable_command_waits_for_typed_bounded_backoff(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    start = datetime.now(UTC)
    clock = FixedClock(start)
    account_id = await add_account(unit_of_work_factory)
    handler = RetryOnceHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
        retry_policy=deterministic_retry_policy(),
    )
    receipt = await runtime.receive(command_body(account_id, clock))

    failed = await runtime.process(receipt.command_id)
    early_retry = await runtime.process(receipt.command_id)
    clock.advance(timedelta(seconds=1))
    recovered = await runtime.process(receipt.command_id)

    assert failed.status is CommandStatus.FAILED_RETRYABLE
    assert early_retry.executed is False
    assert recovered.status is CommandStatus.SUCCEEDED
    assert handler.calls == 2


async def test_business_state_and_result_outbox_roll_back_atomically_on_conflict(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = RecordingHandler()
    colliding_event_id = uuid4()
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.outbox_events.add(
            OutboxEvent(
                id=colliding_event_id,
                aggregate_type="test",
                aggregate_id="existing-event",
                event_type="test.existing",
                payload={},
            )
        )

    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
        event_id_factory=lambda: colliding_event_id,
    )
    receipt = await runtime.receive(command_body(account_id, clock))

    with pytest.raises(IntegrityError):
        await runtime.process(receipt.command_id)

    async with unit_of_work_factory() as unit_of_work:
        command = await unit_of_work.commands.get_by_command_id(receipt.command_id)
        attempt_count = await unit_of_work.attempts.count_for_command(receipt.command_id)
        post = await unit_of_work.posts.get_by_external_id(
            account_id,
            f"local-result-{receipt.command_id}",
        )
        existing_event = await unit_of_work.outbox_events.get(colliding_event_id)

    assert command is not None
    assert command.status is CommandStatus.PROCESSING
    assert command.execution_lease_token is not None
    assert attempt_count == 1
    assert post is None
    assert existing_event is not None


async def test_concurrent_workers_do_not_execute_an_active_claim_twice(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = BlockingHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
        execution_lease_duration=timedelta(seconds=3),
    )
    receipt = await runtime.receive(command_body(account_id, clock))

    first_worker = asyncio.create_task(runtime.process_next())
    await handler.started.wait()
    second_worker = await runtime.process_next()
    duplicate_direct_call = await runtime.process(receipt.command_id)
    handler.release.set()
    first_result = await first_worker

    assert first_result is not None
    assert first_result.status is CommandStatus.SUCCEEDED
    assert second_worker is None
    assert duplicate_direct_call.status is CommandStatus.PROCESSING
    assert duplicate_direct_call.executed is False
    assert handler.calls == 1


async def test_active_execution_heartbeat_extends_the_durable_lease(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = BlockingHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
        execution_lease_duration=timedelta(milliseconds=300),
    )
    receipt = await runtime.receive(command_body(account_id, clock))

    first_worker = asyncio.create_task(runtime.process_next())
    await handler.started.wait()
    async with unit_of_work_factory() as unit_of_work:
        claimed = await unit_of_work.commands.get_by_command_id(receipt.command_id)
    assert claimed is not None
    original_expiry = claimed.execution_lease_expires_at
    assert original_expiry is not None
    clock.advance(timedelta(milliseconds=150))
    for _ in range(100):
        async with unit_of_work_factory() as unit_of_work:
            renewed = await unit_of_work.commands.get_by_command_id(receipt.command_id)
        if (
            renewed is not None
            and renewed.execution_lease_expires_at is not None
            and renewed.execution_lease_expires_at > original_expiry
        ):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the active command lease was not renewed")
    clock.advance(timedelta(milliseconds=200))
    second_worker = asyncio.create_task(runtime.process_next())
    second_result = await second_worker
    handler.release.set()
    first_result = await first_worker

    assert first_result is not None
    assert first_result.status is CommandStatus.SUCCEEDED
    assert second_result is None
    assert handler.calls == 1


async def test_expired_execution_lease_reclaims_and_resumes_from_checkpoint(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = CheckpointingHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
        retry_policy=deterministic_retry_policy(),
        execution_lease_duration=timedelta(seconds=5),
    )
    receipt = await runtime.receive(command_body(account_id, clock))

    with pytest.raises(SimulatedProcessCrash):
        await runtime.process(receipt.command_id)

    async with unit_of_work_factory() as unit_of_work:
        abandoned = await unit_of_work.commands.get_by_command_id(receipt.command_id)
    assert abandoned is not None
    assert abandoned.status is CommandStatus.PROCESSING
    assert abandoned.checkpoint == {"remote_container_id": "container-test"}

    clock.advance(timedelta(seconds=5))
    recovered = await runtime.process_next()
    attempts = list(
        await db_session.scalars(
            select(CommandAttemptRecord)
            .where(CommandAttemptRecord.command_id == receipt.command_id)
            .order_by(CommandAttemptRecord.attempt_number)
        )
    )

    assert recovered is not None
    assert recovered.status is CommandStatus.SUCCEEDED
    assert handler.observed_checkpoints == [{}, {"remote_container_id": "container-test"}]
    assert len(attempts) == 2
    assert attempts[0].status is AttemptStatus.FAILED_RETRYABLE
    assert attempts[0].error_code == "EXECUTION_LEASE_EXPIRED"
    assert attempts[1].status is AttemptStatus.SUCCEEDED


async def test_unexpected_handler_error_is_durable_and_bounded(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = UnexpectedFailureHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
        retry_policy=RetryPolicy(
            max_attempts=2,
            base_delay=timedelta(seconds=1),
            max_delay=timedelta(seconds=1),
            jitter_ratio=0,
            jitter_source=lambda lower, _: lower,
        ),
    )
    receipt = await runtime.receive(command_body(account_id, clock))

    first = await runtime.process(receipt.command_id)
    early_retry = await runtime.process(receipt.command_id)
    clock.advance(timedelta(seconds=1))
    second = await runtime.process(receipt.command_id)
    third = await runtime.process(receipt.command_id)
    attempts = list(
        await db_session.scalars(
            select(CommandAttemptRecord).where(
                CommandAttemptRecord.command_id == receipt.command_id
            )
        )
    )

    assert first.status is CommandStatus.FAILED_RETRYABLE
    assert early_retry.executed is False
    assert second.status is CommandStatus.FAILED_FINAL
    assert third.executed is False
    assert handler.calls == 2
    assert len(attempts) == 2
    assert all(attempt.error_code == "UNEXPECTED_HANDLER_ERROR" for attempt in attempts)


async def test_crm_unavailable_keeps_result_in_outbox_until_reconnect(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = RecordingHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
    )
    receipt = await runtime.receive(command_body(account_id, clock))
    await runtime.process(receipt.command_id)
    crm = CRMStub(unavailable_once=True)
    worker = OutboxDeliveryWorker(
        unit_of_work_factory,
        crm,
        clock=clock,
        retry_policy=deterministic_retry_policy(),
    )

    failed_delivery = await worker.deliver_one()
    assert failed_delivery.event_id is not None
    assert failed_delivery.delivery_id is not None
    async with unit_of_work_factory() as unit_of_work:
        pending_event = await unit_of_work.outbox_events.get(failed_delivery.event_id)
        retry_delivery = await unit_of_work.deliveries.get_for_update(failed_delivery.delivery_id)
    clock.advance(timedelta(seconds=1))
    completed_delivery = await worker.deliver_one()
    assert completed_delivery.event_id is not None
    async with unit_of_work_factory() as unit_of_work:
        completed_event = await unit_of_work.outbox_events.get(completed_delivery.event_id)

    assert failed_delivery.status is DeliveryStatus.FAILED_RETRYABLE
    assert pending_event is not None
    assert pending_event.status is OutboxStatus.PENDING
    assert retry_delivery is not None
    assert retry_delivery.next_attempt_at == clock.now()
    assert completed_delivery.status is DeliveryStatus.DELIVERED
    assert completed_event is not None
    assert completed_event.status is OutboxStatus.DELIVERED
    assert crm.results[0].event_id == crm.results[1].event_id
    assert crm.results[1].correlation_id == receipt.correlation_id


async def test_expired_delivery_lease_is_reclaimed_after_worker_disconnect(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    handler = RecordingHandler()
    runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": handler},
        clock=clock,
    )
    receipt = await runtime.receive(command_body(account_id, clock))
    await runtime.process(receipt.command_id)
    crm = CRMStub(disconnect_once=True)
    worker = OutboxDeliveryWorker(
        unit_of_work_factory,
        crm,
        clock=clock,
        retry_policy=deterministic_retry_policy(),
        lease_duration=timedelta(seconds=3),
    )

    with pytest.raises(ConnectionError, match="simulated CRM connection loss"):
        await worker.deliver_one()
    clock.advance(timedelta(seconds=3))
    recovered_delivery = await worker.deliver_one()

    assert recovered_delivery.status is DeliveryStatus.DELIVERED
    assert crm.results[0].event_id == crm.results[1].event_id


async def test_http_ingress_authenticates_and_only_delegates_to_runtime(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)
    runtime = CommandRuntime(unit_of_work_factory, {}, clock=clock)
    token = secrets.token_urlsafe(32)
    settings = Settings(crm_ingress_token=SecretStr(token), log_level="ERROR")
    app = create_app(
        settings,
        command_runtime=runtime,
        authenticator=BearerTokenAuthenticator(settings.crm_ingress_token),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        unauthorized: Response = await client.post(
            "/v1/commands",
            json=command_body(account_id, clock),
        )
        accepted: Response = await client.post(
            "/v1/commands",
            json=command_body(account_id, clock),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert unauthorized.status_code == 401
    assert accepted.status_code == 202
    assert accepted.json()["status"] == CommandStatus.RECEIVED


async def test_hybrid_router_queues_worker_and_serializes_account_mutations(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account = ThreadsAccount(
        threads_user_id=f"hybrid-user-{uuid4()}",
        username="hybrid-test",
        execution_mode=AccountExecutionMode.HYBRID,
    )
    worker_id = uuid4()
    worker = WorkerNode(
        worker_id=worker_id,
        display_name="Hybrid test worker",
        hostname="test-host",
        platform="windows",
        agent_version="1.0.0",
        protocol_version=2,
        capabilities_schema_version=1,
        status=WorkerStatus.ONLINE,
        last_heartbeat_at=clock.now(),
        presence_expires_at=clock.now() + timedelta(hours=1),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    profile = BrowserProfile(worker_id, f"hybrid-profile-{uuid4()}")
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.workers.add(worker)
        await unit_of_work.browser_profiles.add(profile)
        await unit_of_work.assignments.add(
            AccountWorkerAssignment(account.id, worker_id, profile.profile_ref)
        )
        await unit_of_work.worker_capabilities.replace_for_worker(
            worker_id,
            [WorkerCapability(worker_id, "threads.publish_text", 1, advertised_at=clock.now())],
        )

    worker_jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    remote_runtime = CommandRuntime(
        unit_of_work_factory,
        {},
        clock=clock,
        worker_job_service=worker_jobs,
    )
    remote_receipt = await remote_runtime.receive(command_body(account.id, clock))
    remote_result = await remote_runtime.process(remote_receipt.command_id)
    account.execution_mode = AccountExecutionMode.API_ONLY
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.update(account)
    assert await worker_jobs.claim_next(worker_id) is None
    account.execution_mode = AccountExecutionMode.HYBRID
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.update(account)
    claimed = await worker_jobs.claim_next(worker_id)

    assert remote_result.status is CommandStatus.WAITING_EXECUTION
    assert claimed is not None
    assert claimed.status is WorkerJobStatus.RUNNING
    assert claimed.account_coordination_generation == 1

    api_handler = RecordingHandler()
    api_runtime = CommandRuntime(
        unit_of_work_factory,
        {"threads.publish_text": api_handler},
        clock=clock,
        worker_job_service=worker_jobs,
    )
    api_receipt = await api_runtime.receive(command_body(account.id, clock))
    blocked_api = await api_runtime.process(api_receipt.command_id)

    assert blocked_api.status is CommandStatus.WAITING_EXECUTION
    assert api_handler.calls == 0

    assert claimed.lease_token is not None
    await worker_jobs.complete(
        claimed.id,
        worker_id,
        claimed.lease_token,
        {"remote_result_id": "worker-result"},
    )
    api_result = await api_runtime.process(api_receipt.command_id)

    assert api_result.status is CommandStatus.SUCCEEDED
    assert api_handler.calls == 1
    async with unit_of_work_factory() as unit_of_work:
        worker_route = await unit_of_work.command_route_decisions.get_latest_execution_for_command(
            remote_receipt.command_id
        )
        api_route = await unit_of_work.command_route_decisions.get_latest_execution_for_command(
            api_receipt.command_id
        )
        assert worker_route is not None
        assert api_route is not None
        assert worker_route.executor is CapabilityExecutor.WORKER
        assert api_route.executor is CapabilityExecutor.API


async def test_account_execution_lease_has_one_owner_and_fences_reclaim(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = FixedClock(datetime.now(UTC))
    account_id = await add_account(unit_of_work_factory)

    async def acquire(owner_id: str):
        async with unit_of_work_factory() as unit_of_work:
            return await unit_of_work.account_execution_leases.try_acquire(
                account_id,
                AccountExecutionOwnerType.COMMAND,
                owner_id,
                OperationClass.MUTATION,
                clock.now(),
                clock.now() + timedelta(minutes=1),
            )

    leases = await asyncio.gather(acquire("owner-a"), acquire("owner-b"))
    owners = [lease for lease in leases if lease is not None]

    assert len(owners) == 1
    first_lease = owners[0]
    clock.advance(timedelta(minutes=1))
    async with unit_of_work_factory() as unit_of_work:
        replacement = await unit_of_work.account_execution_leases.try_acquire(
            account_id,
            AccountExecutionOwnerType.WORKER_JOB,
            "job-c",
            OperationClass.MUTATION,
            clock.now(),
            clock.now() + timedelta(minutes=1),
        )
    assert replacement is not None
    assert replacement.fencing_generation == first_lease.fencing_generation + 1

    async with unit_of_work_factory() as unit_of_work:
        assert not await unit_of_work.account_execution_leases.owns(
            account_id,
            first_lease.owner_type,
            first_lease.owner_id,
            first_lease.fencing_generation,
            clock.now(),
        )
        assert await unit_of_work.account_execution_leases.owns(
            account_id,
            replacement.owner_type,
            replacement.owner_id,
            replacement.fencing_generation,
            clock.now(),
        )
