from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.crm_protocol_v1 import CRMCommandResultV1
from threads_platform.application.errors import CRMUnavailable
from threads_platform.application.outbox_delivery import (
    OutboxDeliveryBatchResult,
    OutboxDeliveryWorker,
    deliver_due_outbox,
)
from threads_platform.application.retry import RetryPolicy
from threads_platform.domain.commands import CommandStatus
from threads_platform.domain.outbox import (
    DeliveryStatus,
    IntegrationDelivery,
    OutboxEvent,
    OutboxStatus,
)
from threads_platform.domain.time import normalize_utc
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.models import (
    IntegrationDeliveryRecord,
    OutboxEventRecord,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = normalize_utc(current)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@dataclass(frozen=True, slots=True)
class DeliveryFixture:
    delivery_id: UUID
    event_id: UUID
    command_id: str
    correlation_id: str
    command_type: str


class RecordingSink:
    def __init__(
        self,
        *,
        failures_remaining: dict[str, int] | None = None,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.results: list[CRMCommandResultV1] = []
        self.failures_remaining = failures_remaining or {}
        self.entered = entered
        self.release = release

    async def deliver_result(self, result: CRMCommandResultV1) -> str | None:
        self.results.append(result)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        remaining = self.failures_remaining.get(result.command_id, 0)
        if remaining:
            self.failures_remaining[result.command_id] = remaining - 1
            raise CRMUnavailable(timedelta(0))
        return f"remote-{len(self.results)}"


class DisconnectingSink:
    async def deliver_result(self, result: CRMCommandResultV1) -> str | None:
        raise ConnectionError("simulated transport disconnect")


async def add_delivery(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    now: datetime,
    *,
    command_id: str | None = None,
    next_attempt_at: datetime | None = None,
    status: DeliveryStatus = DeliveryStatus.PENDING,
    event_status: OutboxStatus = OutboxStatus.PENDING,
    lease_expires_at: datetime | None = None,
    lease_token: UUID | None = None,
    delivery_deadline_at: datetime | None = None,
    valid_payload: bool = True,
) -> DeliveryFixture:
    event_id = uuid4()
    command_identity = command_id or f"outbox-command-{uuid4()}"
    correlation_id = f"outbox-correlation-{uuid4()}"
    command_type = "threads.publish_text"
    result = CRMCommandResultV1(
        event_id=event_id,
        command_id=command_identity,
        correlation_id=correlation_id,
        command_type=command_type,
        status=CommandStatus.SUCCEEDED,
        completed_at=now,
    )
    event = OutboxEvent(
        id=event_id,
        aggregate_type="command",
        aggregate_id=command_identity,
        event_type="crm.command_result.v1",
        correlation_id=correlation_id,
        payload=(result.model_dump(mode="json") if valid_payload else {"invalid": "payload"}),
        status=event_status,
        created_at=now,
        available_at=now,
        delivered_at=now if event_status is OutboxStatus.DELIVERED else None,
    )
    delivery = IntegrationDelivery(
        id=uuid4(),
        event_id=event_id,
        destination="crm",
        status=status,
        attempt_count=1 if status is DeliveryStatus.PROCESSING else 0,
        next_attempt_at=next_attempt_at or now,
        delivery_deadline_at=delivery_deadline_at,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        delivered_at=now if status is DeliveryStatus.DELIVERED else None,
        remote_delivery_id="already-delivered" if status is DeliveryStatus.DELIVERED else None,
        error_code="FINAL_ERROR" if status is DeliveryStatus.FAILED_FINAL else None,
        created_at=now,
        updated_at=now,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.outbox_events.add(event)
        await unit_of_work.deliveries.add(delivery)
    return DeliveryFixture(
        delivery.id,
        event.id,
        command_identity,
        correlation_id,
        command_type,
    )


async def get_delivery(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory, delivery_id: UUID
) -> IntegrationDelivery:
    async with unit_of_work_factory() as unit_of_work:
        result = await unit_of_work.deliveries.get_for_update(delivery_id)
    assert result is not None
    return result


def immediate_retry_policy(*, max_attempts: int = 3) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts,
        base_delay=timedelta(seconds=1),
        max_delay=timedelta(seconds=10),
        jitter_ratio=1,
        jitter_source=lambda lower, _upper: lower,
    )


async def test_pump_attempts_retrying_delivery_once_and_does_not_starve_next(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    clock = MutableClock(now)
    first = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="oldest-retryable-delivery",
        next_attempt_at=now - timedelta(seconds=2),
        delivery_deadline_at=now + timedelta(hours=1),
    )
    second = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="next-due-delivery",
        next_attempt_at=now - timedelta(seconds=1),
    )
    sink = RecordingSink(failures_remaining={first.command_id: 1})
    worker = OutboxDeliveryWorker(
        unit_of_work_factory,
        sink,
        clock=clock,
        retry_policy=immediate_retry_policy(),
    )

    batch = await deliver_due_outbox(worker, limit=2)

    assert batch.attempted == 2
    assert batch.succeeded == 1
    assert [result.command_id for result in sink.results] == [first.command_id, second.command_id]
    first_state = await get_delivery(unit_of_work_factory, first.delivery_id)
    second_state = await get_delivery(unit_of_work_factory, second.delivery_id)
    assert first_state.status is DeliveryStatus.FAILED_RETRYABLE
    assert first_state.attempt_count == 1
    assert first_state.next_attempt_at == now
    assert second_state.status is DeliveryStatus.DELIVERED

    later_tick = await deliver_due_outbox(worker, limit=1)
    assert later_tick.attempted == 1
    assert later_tick.succeeded == 1
    assert sink.results[-1].command_id == first.command_id
    assert (
        await get_delivery(unit_of_work_factory, first.delivery_id)
    ).status is DeliveryStatus.DELIVERED


async def test_scheduler_pump_persists_retry_time_then_succeeds_on_later_tick(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    clock = MutableClock(now)
    fixture = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="delayed-retry-delivery",
        delivery_deadline_at=now + timedelta(hours=1),
    )
    sink = RecordingSink(failures_remaining={fixture.command_id: 1})
    policy = RetryPolicy(
        max_attempts=3,
        base_delay=timedelta(seconds=2),
        max_delay=timedelta(seconds=10),
        jitter_ratio=0,
        jitter_source=lambda lower, _upper: lower,
    )
    worker = OutboxDeliveryWorker(unit_of_work_factory, sink, clock=clock, retry_policy=policy)

    failed = await deliver_due_outbox(worker, limit=1)
    after_failure = await get_delivery(unit_of_work_factory, fixture.delivery_id)
    premature = await deliver_due_outbox(worker, limit=1)
    clock.advance(timedelta(seconds=2))
    recovered = await deliver_due_outbox(worker, limit=1)

    assert (failed.attempted, failed.succeeded) == (1, 0)
    assert after_failure.status is DeliveryStatus.FAILED_RETRYABLE
    assert after_failure.next_attempt_at == now + timedelta(seconds=2)
    assert (premature.attempted, premature.succeeded) == (0, 0)
    assert (recovered.attempted, recovered.succeeded) == (1, 1)
    assert len(sink.results) == 2
    assert sink.results[0].event_id == sink.results[1].event_id == fixture.event_id


async def test_unclaimable_statuses_do_not_consume_the_bounded_selection(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    active = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="unexpired-processing",
        next_attempt_at=now - timedelta(hours=1),
        status=DeliveryStatus.PROCESSING,
        lease_token=uuid4(),
        lease_expires_at=now + timedelta(hours=1),
    )
    delivered = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="already-delivered",
        next_attempt_at=now - timedelta(minutes=3),
        status=DeliveryStatus.DELIVERED,
        event_status=OutboxStatus.DELIVERED,
    )
    final = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="already-final",
        next_attempt_at=now - timedelta(minutes=2),
        status=DeliveryStatus.FAILED_FINAL,
        event_status=OutboxStatus.FAILED_FINAL,
    )
    due_first = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="bounded-first",
        next_attempt_at=now - timedelta(minutes=1),
    )
    due_second = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="bounded-second",
        next_attempt_at=now,
    )
    sink = RecordingSink()
    worker = OutboxDeliveryWorker(unit_of_work_factory, sink, clock=MutableClock(now))

    first_batch = await deliver_due_outbox(worker, limit=1)
    second_batch = await deliver_due_outbox(worker, limit=1)
    empty_batch = await deliver_due_outbox(worker, limit=1)

    assert (first_batch.attempted, first_batch.succeeded) == (1, 1)
    assert (second_batch.attempted, second_batch.succeeded) == (1, 1)
    assert (empty_batch.attempted, empty_batch.succeeded) == (0, 0)
    assert [result.command_id for result in sink.results] == [
        due_first.command_id,
        due_second.command_id,
    ]
    assert (await get_delivery(unit_of_work_factory, active.delivery_id)).attempt_count == 1
    assert (await get_delivery(unit_of_work_factory, delivered.delivery_id)).attempt_count == 0
    assert (await get_delivery(unit_of_work_factory, final.delivery_id)).attempt_count == 0


async def test_retry_exhaustion_deadline_and_invalid_payload_are_finalized_by_pump(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    exhausted = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="exhausted-delivery",
        delivery_deadline_at=now + timedelta(hours=1),
    )
    deadline = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="expired-deadline-delivery",
        delivery_deadline_at=now,
    )
    invalid = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="invalid-payload-delivery",
        valid_payload=False,
    )
    sink = RecordingSink(failures_remaining={exhausted.command_id: 1})
    worker = OutboxDeliveryWorker(
        unit_of_work_factory,
        sink,
        clock=MutableClock(now),
        retry_policy=immediate_retry_policy(max_attempts=1),
    )

    batch = await deliver_due_outbox(worker, limit=3)

    assert batch.attempted == 3
    assert batch.succeeded == 0
    assert [result.command_id for result in sink.results] == [exhausted.command_id]
    assert (
        await get_delivery(unit_of_work_factory, exhausted.delivery_id)
    ).status is DeliveryStatus.FAILED_FINAL
    assert (
        await get_delivery(unit_of_work_factory, deadline.delivery_id)
    ).status is DeliveryStatus.FAILED_FINAL
    assert (
        await get_delivery(unit_of_work_factory, invalid.delivery_id)
    ).status is DeliveryStatus.FAILED_FINAL
    assert await count_event_status(
        unit_of_work_factory, exhausted.event_id, OutboxStatus.FAILED_FINAL
    )
    assert await count_event_status(
        unit_of_work_factory, deadline.event_id, OutboxStatus.FAILED_FINAL
    )
    assert await count_event_status(
        unit_of_work_factory, invalid.event_id, OutboxStatus.FAILED_FINAL
    )


async def count_event_status(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    event_id: UUID,
    expected: OutboxStatus,
) -> bool:
    async with unit_of_work_factory() as unit_of_work:
        event = await unit_of_work.outbox_events.get(event_id)
    return event is not None and event.status is expected


async def test_expired_processing_lease_is_reclaimed_after_disconnect(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    clock = MutableClock(now)
    fixture = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="disconnect-reclaim",
        delivery_deadline_at=now + timedelta(hours=1),
    )
    disconnected = OutboxDeliveryWorker(
        unit_of_work_factory,
        DisconnectingSink(),
        clock=clock,
        lease_duration=timedelta(seconds=2),
    )

    with pytest.raises(ConnectionError, match="simulated transport disconnect"):
        await deliver_due_outbox(disconnected, limit=1)
    processing = await get_delivery(unit_of_work_factory, fixture.delivery_id)
    assert processing.status is DeliveryStatus.PROCESSING
    assert processing.lease_token is not None
    assert processing.attempt_count == 1

    clock.advance(timedelta(seconds=2))
    restarted_sink = RecordingSink()
    restarted = OutboxDeliveryWorker(
        unit_of_work_factory,
        restarted_sink,
        clock=clock,
        lease_duration=timedelta(seconds=2),
    )
    recovered = await deliver_due_outbox(restarted, limit=1)

    assert (recovered.attempted, recovered.succeeded) == (1, 1)
    assert restarted_sink.results[0].event_id == fixture.event_id
    assert (await get_delivery(unit_of_work_factory, fixture.delivery_id)).attempt_count == 2


async def test_two_scheduler_instances_cannot_claim_the_same_active_lease(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    fixture = await add_delivery(unit_of_work_factory, now, command_id="single-active-claim")
    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    other_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
    entered = asyncio.Event()
    release = asyncio.Event()
    first_sink = RecordingSink(entered=entered, release=release)
    first = OutboxDeliveryWorker(unit_of_work_factory, first_sink, clock=MutableClock(now))
    second_sink = RecordingSink()
    second = OutboxDeliveryWorker(other_factory, second_sink, clock=MutableClock(now))
    first_task = asyncio.create_task(deliver_due_outbox(first, limit=1))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        second_batch = await deliver_due_outbox(second, limit=1)
        active = await get_delivery(unit_of_work_factory, fixture.delivery_id)
        release.set()
        first_batch = await asyncio.wait_for(first_task, timeout=10)
    finally:
        release.set()
        if not first_task.done():
            await first_task
        await engine.dispose()

    assert (second_batch.attempted, second_batch.succeeded) == (0, 0)
    assert (first_batch.attempted, first_batch.succeeded) == (1, 1)
    assert active.status is DeliveryStatus.PROCESSING
    assert len(first_sink.results) == 1
    assert second_sink.results == []


async def test_distinct_due_deliveries_split_between_scheduler_instances(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    oldest = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="split-oldest",
        next_attempt_at=now - timedelta(seconds=2),
    )
    next_due = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="split-next",
        next_attempt_at=now - timedelta(seconds=1),
    )
    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    other_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
    entered = asyncio.Event()
    release = asyncio.Event()
    first_sink = RecordingSink(entered=entered, release=release)
    second_sink = RecordingSink()
    first = OutboxDeliveryWorker(unit_of_work_factory, first_sink, clock=MutableClock(now))
    second = OutboxDeliveryWorker(other_factory, second_sink, clock=MutableClock(now))
    first_task = asyncio.create_task(deliver_due_outbox(first, limit=1))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        second_batch = await deliver_due_outbox(second, limit=1)
        release.set()
        first_batch = await asyncio.wait_for(first_task, timeout=10)
    finally:
        release.set()
        if not first_task.done():
            await first_task
        await engine.dispose()

    assert (first_batch.attempted, first_batch.succeeded) == (1, 1)
    assert (second_batch.attempted, second_batch.succeeded) == (1, 1)
    assert [item.command_id for item in first_sink.results] == [oldest.command_id]
    assert [item.command_id for item in second_sink.results] == [next_due.command_id]


@pytest.mark.parametrize("stale_outcome", ["success", "retryable"])
async def test_expired_lease_reclaim_fences_stale_finalizer_and_preserves_event_identity(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    stale_outcome: str,
) -> None:
    now = datetime.now(UTC)
    clock = MutableClock(now)
    fixture = await add_delivery(
        unit_of_work_factory,
        now,
        command_id="fenced-delivery",
        delivery_deadline_at=now + timedelta(hours=1),
    )
    first_entered, first_release = asyncio.Event(), asyncio.Event()
    second_entered, second_release = asyncio.Event(), asyncio.Event()
    first_sink = RecordingSink(
        failures_remaining={fixture.command_id: 1} if stale_outcome == "retryable" else None,
        entered=first_entered,
        release=first_release,
    )
    second_sink = RecordingSink(entered=second_entered, release=second_release)
    first = OutboxDeliveryWorker(
        unit_of_work_factory,
        first_sink,
        clock=clock,
        lease_duration=timedelta(seconds=2),
    )
    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    other_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
    second = OutboxDeliveryWorker(
        other_factory,
        second_sink,
        clock=clock,
        lease_duration=timedelta(seconds=2),
    )
    first_task = asyncio.create_task(deliver_due_outbox(first, limit=1))
    second_task: asyncio.Task[OutboxDeliveryBatchResult] | None = None
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=10)
        old_token = (await get_delivery(unit_of_work_factory, fixture.delivery_id)).lease_token
        clock.advance(timedelta(seconds=2))
        second_task = asyncio.create_task(deliver_due_outbox(second, limit=1))
        await asyncio.wait_for(second_entered.wait(), timeout=10)
        new_token = (await get_delivery(unit_of_work_factory, fixture.delivery_id)).lease_token
        assert old_token is not None and new_token is not None and new_token != old_token

        first_release.set()
        first_batch = await asyncio.wait_for(first_task, timeout=10)
        still_owned = await get_delivery(unit_of_work_factory, fixture.delivery_id)
        assert still_owned.status is DeliveryStatus.PROCESSING
        assert still_owned.lease_token == new_token
        assert still_owned.attempt_count == 2
        assert still_owned.error_code is None

        second_release.set()
        second_batch = await asyncio.wait_for(second_task, timeout=10)
    finally:
        first_release.set()
        second_release.set()
        if not first_task.done():
            await first_task
        if second_task is not None and not second_task.done():
            await second_task
        await engine.dispose()

    assert (first_batch.attempted, first_batch.succeeded) == (1, 0)
    assert (second_batch.attempted, second_batch.succeeded) == (1, 1)
    assert [item.event_id for item in (*first_sink.results, *second_sink.results)] == [
        fixture.event_id,
        fixture.event_id,
    ]
    final = await get_delivery(unit_of_work_factory, fixture.delivery_id)
    assert final.status is DeliveryStatus.DELIVERED
    assert final.attempt_count == 2


async def test_scheduler_tick_processes_terminal_command_result_through_outbox_pump(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    db_session: AsyncSession,
) -> None:
    from threads_platform.application.commands.runtime import CommandRuntime
    from threads_platform.application.scheduler import run_scheduler_tick
    from threads_platform.application.worker_jobs import WorkerJobService
    from threads_platform.domain.accounts import ThreadsAccount
    from threads_platform.domain.commands import Command

    now = datetime.now(UTC)
    clock = MutableClock(now)
    account = ThreadsAccount(
        threads_user_id=f"outbox-e2e-{uuid4()}",
        username="outbox_e2e",
    )
    command = Command(
        command_id=f"outbox-terminal-{uuid4()}",
        correlation_id=f"outbox-correlation-{uuid4()}",
        account_id=account.id,
        command_type="threads.publish_text",
        payload={"text": "expired before execution"},
        created_at=now - timedelta(minutes=1),
        received_at=now - timedelta(minutes=1),
        deadline_at=now,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.commands.add(command)
    runtime = CommandRuntime(unit_of_work_factory, {}, clock=clock)
    worker_jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    sink = RecordingSink()
    delivery_worker = OutboxDeliveryWorker(unit_of_work_factory, sink, clock=clock)

    tick = await run_scheduler_tick(
        unit_of_work_factory,
        runtime,
        worker_jobs,
        now=now,
        activity_limit=1,
        command_limit=1,
        recovery_limit=1,
        outbox_delivery_worker=delivery_worker,
        outbox_delivery_limit=1,
    )

    assert tick.commands_processed == 1
    assert tick.outbox_deliveries_attempted == 1
    assert tick.outbox_deliveries_succeeded == 1
    event = await db_session.scalar(
        select(OutboxEventRecord).where(OutboxEventRecord.aggregate_id == command.command_id)
    )
    assert event is not None and event.status is OutboxStatus.DELIVERED
    delivery = await db_session.scalar(
        select(IntegrationDeliveryRecord).where(IntegrationDeliveryRecord.event_id == event.id)
    )
    assert delivery is not None and delivery.status is DeliveryStatus.DELIVERED
    assert len(sink.results) == 1
    received = sink.results[0]
    assert received.event_id == event.id
    assert received.command_id == command.command_id
    assert received.correlation_id == command.correlation_id
    assert received.command_type == command.command_type
    assert received.status is CommandStatus.EXPIRED
    event_count = await db_session.scalar(
        select(func.count())
        .select_from(OutboxEventRecord)
        .where(OutboxEventRecord.aggregate_id == command.command_id)
    )
    delivery_count = await db_session.scalar(
        select(func.count())
        .select_from(IntegrationDeliveryRecord)
        .where(IntegrationDeliveryRecord.event_id == event.id)
    )
    assert event_count == 1
    assert delivery_count == 1
