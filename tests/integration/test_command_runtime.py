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
from threads_platform.application.commands.handlers import CommandExecutionContext
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.crm_protocol_v1 import (
    CommandEnvelopeV1,
    CRMCommandResultV1,
)
from threads_platform.application.errors import CRMUnavailable, RetryableCommandError
from threads_platform.application.outbox_delivery import OutboxDeliveryWorker
from threads_platform.application.retry import RetryPolicy
from threads_platform.config.settings import Settings
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import CommandStatus
from threads_platform.domain.outbox import DeliveryStatus, OutboxEvent, OutboxStatus
from threads_platform.domain.publishing import ThreadPost
from threads_platform.infrastructure.persistence.models import (
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
    ) -> dict[str, object]:
        self.calls += 1
        await context.posts.add(
            ThreadPost(
                account_id=command.account_id,
                threads_post_id=f"local-result-{command.command_id}",
                text=command.payload.text,
            )
        )
        return {"local_result_id": command.command_id}


class RetryOnceHandler(RecordingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> dict[str, object]:
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


async def test_business_state_and_outbox_roll_back_atomically_on_outbox_conflict(
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
    assert command.status is CommandStatus.RECEIVED
    assert attempt_count == 0
    assert post is None
    assert existing_event is not None


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
