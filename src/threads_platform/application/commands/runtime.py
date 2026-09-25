from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.commands.handlers import CommandExecutionContext, CommandHandler
from threads_platform.application.crm_protocol_v1 import (
    COMMAND_ENVELOPE_ADAPTER,
    CommandEnvelopeHeader,
    CommandEnvelopeV1,
    CommandReceiptV1,
    CRMCommandResultV1,
    CRMErrorV1,
)
from threads_platform.application.errors import (
    CommandInputError,
    CommandNotFound,
    IdempotencyConflict,
    PermanentCommandError,
    RetryableCommandError,
)
from threads_platform.application.ports.repositories import UnitOfWork, UnitOfWorkFactory
from threads_platform.application.retry import RetryPolicy
from threads_platform.domain.commands import (
    AttemptStatus,
    Command,
    CommandAttempt,
    CommandStatus,
)
from threads_platform.domain.outbox import IntegrationDelivery, OutboxEvent
from threads_platform.domain.time import normalize_utc

KNOWN_COMMAND_TYPES = frozenset({"threads.publish_text", "threads.create_reply"})


@dataclass(frozen=True, slots=True)
class CommandExecutionResult:
    command_id: str
    status: CommandStatus
    executed: bool


class CommandRuntime:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        handlers: Mapping[str, CommandHandler],
        *,
        clock: Clock | None = None,
        retry_policy: RetryPolicy | None = None,
        max_command_lifetime: timedelta = timedelta(minutes=15),
        delivery_lifetime: timedelta = timedelta(hours=24),
        crm_destination: str = "crm",
        event_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if max_command_lifetime <= timedelta(0) or delivery_lifetime <= timedelta(0):
            raise ValueError("command and delivery lifetimes must be positive")
        self._unit_of_work_factory = unit_of_work_factory
        self._handlers = dict(handlers)
        self._clock = clock or SystemClock()
        self._retry_policy = retry_policy or RetryPolicy()
        self._max_command_lifetime = max_command_lifetime
        self._delivery_lifetime = delivery_lifetime
        self._crm_destination = crm_destination
        self._event_id_factory = event_id_factory

    async def receive(self, raw_command: dict[str, object]) -> CommandReceiptV1:
        try:
            header = CommandEnvelopeHeader.model_validate(raw_command)
        except ValidationError as error:
            raise CommandInputError("INVALID_ENVELOPE") from error

        now = normalize_utc(self._clock.now())
        deadline = header.deadline_at or header.created_at + self._max_command_lifetime
        typed_command: CommandEnvelopeV1 | None = None
        rejection_code: str | None = None
        if header.protocol_version != 1:
            rejection_code = "UNSUPPORTED_PROTOCOL_VERSION"
        elif header.command_type not in KNOWN_COMMAND_TYPES:
            rejection_code = "UNKNOWN_COMMAND"
        else:
            try:
                typed_command = COMMAND_ENVELOPE_ADAPTER.validate_python(raw_command)
            except ValidationError:
                rejection_code = "INVALID_COMMAND"

        payload = (
            typed_command.payload.model_dump(mode="json")
            if typed_command is not None
            else header.payload
        )
        command = Command(
            command_id=header.command_id,
            correlation_id=header.correlation_id,
            account_id=header.account_id,
            command_type=header.command_type,
            protocol_version=header.protocol_version,
            payload=payload,
            created_at=header.created_at,
            received_at=now,
            deadline_at=deadline,
        )
        if rejection_code is not None:
            command.transition(
                CommandStatus.REJECTED,
                now,
                error_code=rejection_code,
            )
        elif deadline <= now:
            command.transition(CommandStatus.EXPIRED, now, error_code="COMMAND_EXPIRED")

        async with self._unit_of_work_factory() as unit_of_work:
            if await unit_of_work.accounts.get(header.account_id) is None:
                raise CommandInputError("UNKNOWN_ACCOUNT")
            inserted = await unit_of_work.commands.add_if_absent(command)
            if not inserted:
                existing = await unit_of_work.commands.get_by_command_id(command.command_id)
                if existing is None:
                    raise RuntimeError("idempotency conflict row disappeared")
                if not self._same_command(existing, command):
                    raise IdempotencyConflict(command.command_id)
                return CommandReceiptV1(
                    command_id=existing.command_id,
                    correlation_id=existing.correlation_id,
                    status=existing.status,
                    duplicate=True,
                )

            if command.completed_at is not None:
                await self._enqueue_result(unit_of_work, command, now)
            return CommandReceiptV1(
                command_id=command.command_id,
                correlation_id=command.correlation_id,
                status=command.status,
            )

    async def process(self, command_id: str) -> CommandExecutionResult:
        async with self._unit_of_work_factory() as unit_of_work:
            command = await unit_of_work.commands.get_by_command_id_for_update(command_id)
            if command is None:
                raise CommandNotFound(command_id)
            return await self._process_locked(unit_of_work, command)

    async def process_next(self) -> CommandExecutionResult | None:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            command = await unit_of_work.commands.get_next_ready_for_update(now)
            if command is None:
                return None
            return await self._process_locked(unit_of_work, command)

    async def _process_locked(
        self, unit_of_work: UnitOfWork, command: Command
    ) -> CommandExecutionResult:
        if command.status in self._terminal_statuses():
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        now = normalize_utc(self._clock.now())
        if command.deadline_at is not None and now >= command.deadline_at:
            command.transition(
                CommandStatus.EXPIRED,
                now,
                error_code="COMMAND_EXPIRED",
            )
            await unit_of_work.commands.update(command)
            await self._enqueue_result(unit_of_work, command, now)
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        if (
            command.status == CommandStatus.FAILED_RETRYABLE
            and command.next_retry_at is not None
            and now < command.next_retry_at
        ):
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        if command.status == CommandStatus.RECEIVED:
            command.transition(CommandStatus.VALIDATED, now)
            await unit_of_work.commands.update(command)

        handler = self._handlers.get(command.command_type)
        if handler is None:
            command.transition(
                CommandStatus.REJECTED,
                now,
                error_code="HANDLER_UNAVAILABLE",
            )
            await unit_of_work.commands.update(command)
            await self._enqueue_result(unit_of_work, command, now)
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        typed_command = self._rebuild_envelope(command)
        command.transition(CommandStatus.PROCESSING, now)
        await unit_of_work.commands.update(command)
        attempt_number = await unit_of_work.attempts.count_for_command(command.command_id) + 1
        attempt = CommandAttempt(command.command_id, attempt_number, started_at=now)
        await unit_of_work.attempts.add(attempt)
        context = CommandExecutionContext(posts=unit_of_work.posts, replies=unit_of_work.replies)

        try:
            async with unit_of_work.savepoint():
                result = await handler.execute(typed_command, context)
        except RetryableCommandError as error:
            retry_at = self._retry_policy.next_attempt_at(
                attempt_number,
                now,
                command.deadline_at or now + self._max_command_lifetime,
                retry_after=error.retry_after,
            )
            if retry_at is None:
                command.transition(
                    CommandStatus.FAILED_FINAL,
                    now,
                    error_code=error.code,
                )
                attempt.status = AttemptStatus.FAILED_FINAL
            else:
                command.transition(
                    CommandStatus.FAILED_RETRYABLE,
                    now,
                    error_code=error.code,
                    next_retry_at=retry_at,
                )
                attempt.status = AttemptStatus.FAILED_RETRYABLE
            attempt.finished_at = now
            attempt.error_code = error.code
            await unit_of_work.attempts.update(attempt)
            await unit_of_work.commands.update(command)
            if command.status == CommandStatus.FAILED_FINAL:
                await self._enqueue_result(unit_of_work, command, now)
            return CommandExecutionResult(command.command_id, command.status, executed=True)
        except PermanentCommandError as error:
            command.transition(
                CommandStatus.FAILED_FINAL,
                now,
                error_code=error.code,
            )
            attempt.status = AttemptStatus.FAILED_FINAL
            attempt.finished_at = now
            attempt.error_code = error.code
            await unit_of_work.attempts.update(attempt)
            await unit_of_work.commands.update(command)
            await self._enqueue_result(unit_of_work, command, now)
            return CommandExecutionResult(command.command_id, command.status, executed=True)

        command.transition(CommandStatus.SUCCEEDED, now, result=result)
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.finished_at = now
        await unit_of_work.attempts.update(attempt)
        await unit_of_work.commands.update(command)
        await self._enqueue_result(unit_of_work, command, now)
        return CommandExecutionResult(command.command_id, command.status, executed=True)

    async def _enqueue_result(
        self,
        unit_of_work: UnitOfWork,
        command: Command,
        now: datetime,
    ) -> None:
        error = None
        if command.error_code is not None:
            error = CRMErrorV1(
                code=command.error_code,
                retryable=command.status == CommandStatus.FAILED_RETRYABLE,
            )
        event_id = self._event_id_factory()
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
            destination=self._crm_destination,
            next_attempt_at=now,
            delivery_deadline_at=now + self._delivery_lifetime,
            created_at=now,
            updated_at=now,
        )
        await unit_of_work.outbox_events.add(event)
        await unit_of_work.deliveries.add(delivery)

    @staticmethod
    def _same_command(existing: Command, received: Command) -> bool:
        return (
            existing.command_id == received.command_id
            and existing.correlation_id == received.correlation_id
            and existing.account_id == received.account_id
            and existing.protocol_version == received.protocol_version
            and existing.command_type == received.command_type
            and existing.payload == received.payload
            and existing.created_at == received.created_at
            and existing.deadline_at == received.deadline_at
        )

    @staticmethod
    def _rebuild_envelope(command: Command) -> CommandEnvelopeV1:
        raw: dict[str, Any] = {
            "protocol_version": command.protocol_version,
            "command_id": command.command_id,
            "correlation_id": command.correlation_id,
            "account_id": command.account_id,
            "created_at": command.created_at,
            "deadline_at": command.deadline_at,
            "command_type": command.command_type,
            "payload": command.payload,
        }
        try:
            return COMMAND_ENVELOPE_ADAPTER.validate_python(raw)
        except ValidationError as error:
            raise RuntimeError("persisted validated command cannot be parsed") from error

    @staticmethod
    def _terminal_statuses() -> frozenset[CommandStatus]:
        return frozenset(
            {
                CommandStatus.SUCCEEDED,
                CommandStatus.REJECTED,
                CommandStatus.EXPIRED,
                CommandStatus.FAILED_FINAL,
            }
        )
