from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError

from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.crm_protocol_v1 import CRMCommandResultV1
from threads_platform.application.errors import CRMUnavailable
from threads_platform.application.ports.crm import CRMResultSink
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.retry import RetryPolicy
from threads_platform.domain.outbox import DeliveryStatus, OutboxStatus
from threads_platform.domain.time import normalize_utc


@dataclass(frozen=True, slots=True)
class DeliveryAttemptResult:
    delivery_id: UUID | None
    event_id: UUID | None
    status: DeliveryStatus | None
    delivered: bool


class OutboxDeliveryWorker:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        result_sink: CRMResultSink,
        *,
        clock: Clock | None = None,
        retry_policy: RetryPolicy | None = None,
        lease_duration: timedelta = timedelta(minutes=2),
        destination: str = "crm",
        lease_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._unit_of_work_factory = unit_of_work_factory
        self._result_sink = result_sink
        self._clock = clock or SystemClock()
        self._retry_policy = retry_policy or RetryPolicy(
            max_attempts=10,
            base_delay=timedelta(seconds=2),
            max_delay=timedelta(minutes=10),
        )
        self._lease_duration = lease_duration
        self._destination = destination
        self._lease_id_factory = lease_id_factory

    async def deliver_one(self) -> DeliveryAttemptResult:
        now = normalize_utc(self._clock.now())
        lease_token = self._lease_id_factory()
        async with self._unit_of_work_factory() as unit_of_work:
            claimed = await unit_of_work.deliveries.claim_next(
                self._destination,
                now,
                lease_token,
                now + self._lease_duration,
            )
        if claimed is None:
            return DeliveryAttemptResult(None, None, None, delivered=False)

        delivery, event = claimed
        if delivery.delivery_deadline_at is not None and now >= delivery.delivery_deadline_at:
            return await self._mark_final_failure(
                delivery.id,
                event.id,
                lease_token,
                "DELIVERY_DEADLINE_EXCEEDED",
                now,
            )

        try:
            result = CRMCommandResultV1.model_validate(event.payload)
        except ValidationError as error:
            return await self._mark_final_failure(
                delivery.id,
                event.id,
                lease_token,
                "INVALID_OUTBOX_RESULT",
                now,
                cause=error,
            )

        try:
            remote_delivery_id = await self._result_sink.deliver_result(result)
        except CRMUnavailable as error:
            return await self._mark_unavailable(
                delivery.id,
                event.id,
                lease_token,
                delivery.attempt_count,
                delivery.delivery_deadline_at or now,
                now,
                error,
            )

        async with self._unit_of_work_factory() as unit_of_work:
            current = await unit_of_work.deliveries.get_for_update(delivery.id)
            if current is None or current.lease_token != lease_token:
                return DeliveryAttemptResult(
                    delivery.id, event.id, current.status if current else None, delivered=False
                )
            current.status = DeliveryStatus.DELIVERED
            current.delivered_at = now
            current.remote_delivery_id = remote_delivery_id
            current.lease_token = None
            current.lease_expires_at = None
            current.error_code = None
            current.updated_at = now
            await unit_of_work.deliveries.update(current)

            outbox_event = await unit_of_work.outbox_events.get(event.id)
            if outbox_event is None:
                raise RuntimeError("claimed outbox event disappeared")
            outbox_event.status = OutboxStatus.DELIVERED
            outbox_event.delivered_at = now
            await unit_of_work.outbox_events.update(outbox_event)
        return DeliveryAttemptResult(
            delivery.id, event.id, DeliveryStatus.DELIVERED, delivered=True
        )

    async def _mark_unavailable(
        self,
        delivery_id: UUID,
        event_id: UUID,
        lease_token: UUID,
        attempt_number: int,
        deadline_at: datetime,
        now: datetime,
        error: CRMUnavailable,
    ) -> DeliveryAttemptResult:
        retry_at = self._retry_policy.next_attempt_at(
            attempt_number,
            now,
            deadline_at,
            retry_after=error.retry_after,
        )
        async with self._unit_of_work_factory() as unit_of_work:
            delivery = await unit_of_work.deliveries.get_for_update(delivery_id)
            if delivery is None or delivery.lease_token != lease_token:
                return DeliveryAttemptResult(
                    delivery_id, event_id, delivery.status if delivery else None, delivered=False
                )
            delivery.lease_token = None
            delivery.lease_expires_at = None
            delivery.updated_at = now
            delivery.error_code = error.code
            if retry_at is None:
                delivery.status = DeliveryStatus.FAILED_FINAL
                event_status = OutboxStatus.FAILED_FINAL
            else:
                delivery.status = DeliveryStatus.FAILED_RETRYABLE
                delivery.next_attempt_at = retry_at
                event_status = OutboxStatus.PENDING
            await unit_of_work.deliveries.update(delivery)
            if event_status == OutboxStatus.FAILED_FINAL:
                event = await unit_of_work.outbox_events.get(event_id)
                if event is None:
                    raise RuntimeError("claimed outbox event disappeared")
                event.status = event_status
                await unit_of_work.outbox_events.update(event)
            return DeliveryAttemptResult(delivery_id, event_id, delivery.status, delivered=False)

    async def _mark_final_failure(
        self,
        delivery_id: UUID,
        event_id: UUID,
        lease_token: UUID,
        error_code: str,
        now: datetime,
        *,
        cause: BaseException | None = None,
    ) -> DeliveryAttemptResult:
        async with self._unit_of_work_factory() as unit_of_work:
            delivery = await unit_of_work.deliveries.get_for_update(delivery_id)
            if delivery is None or delivery.lease_token != lease_token:
                return DeliveryAttemptResult(
                    delivery_id, event_id, delivery.status if delivery else None, delivered=False
                )
            delivery.status = DeliveryStatus.FAILED_FINAL
            delivery.error_code = error_code
            delivery.lease_token = None
            delivery.lease_expires_at = None
            delivery.updated_at = now
            await unit_of_work.deliveries.update(delivery)
            event = await unit_of_work.outbox_events.get(event_id)
            if event is None:
                raise RuntimeError("claimed outbox event disappeared") from cause
            event.status = OutboxStatus.FAILED_FINAL
            await unit_of_work.outbox_events.update(event)
        return DeliveryAttemptResult(
            delivery_id, event_id, DeliveryStatus.FAILED_FINAL, delivered=False
        )
