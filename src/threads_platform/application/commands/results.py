from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from threads_platform.application.crm_protocol_v1 import CRMCommandResultV1, CRMErrorV1
from threads_platform.application.ports.repositories import UnitOfWork
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.outbox import IntegrationDelivery, OutboxEvent


async def enqueue_command_result(
    unit_of_work: UnitOfWork,
    command: Command,
    now: datetime,
    *,
    delivery_lifetime: timedelta = timedelta(hours=24),
    crm_destination: str = "crm",
    event_id_factory: Callable[[], UUID] = uuid4,
) -> None:
    error = None
    if command.error_code is not None:
        error = CRMErrorV1(
            code=command.error_code,
            retryable=command.status == CommandStatus.FAILED_RETRYABLE,
        )
    event_id = event_id_factory()
    response = CRMCommandResultV1(
        event_id=event_id,
        command_id=command.command_id,
        correlation_id=command.correlation_id,
        command_type=command.command_type,
        status=command.status,
        completed_at=command.completed_at or now,
        result=command.result,
        error=error,
    )
    event = OutboxEvent(
        id=event_id,
        aggregate_type="command",
        aggregate_id=command.command_id,
        event_type="crm.command_result.v1",
        correlation_id=command.correlation_id,
        payload=response.model_dump(mode="json"),
        created_at=now,
        available_at=now,
    )
    delivery = IntegrationDelivery(
        event_id=event.id,
        destination=crm_destination,
        next_attempt_at=now,
        delivery_deadline_at=now + delivery_lifetime,
        created_at=now,
        updated_at=now,
    )
    await unit_of_work.outbox_events.add(event)
    await unit_of_work.deliveries.add(delivery)
