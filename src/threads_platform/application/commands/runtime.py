import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.commands.handlers import (
    CommandCheckpoint,
    CommandExecutionContext,
    CommandExecutionOutput,
    CommandHandler,
)
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
    ExecutionLeaseLost,
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

KNOWN_COMMAND_TYPES = frozenset(
    {
        "threads.publish_text",
        "threads.publish_image",
        "threads.publish_video",
        "threads.publish_carousel",
        "threads.create_reply",
        "threads.sync_conversation",
        "threads.moderate_reply",
    }
)


@dataclass(frozen=True, slots=True)
class CommandExecutionResult:
    command_id: str
    status: CommandStatus
    executed: bool


@dataclass(frozen=True, slots=True)
class _ExecutionClaim:
    command: Command
    lease_token: UUID
    attempt_number: int


class _SyncCursorConflict(Exception):
    pass


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
        execution_lease_duration: timedelta = timedelta(minutes=2),
        crm_destination: str = "crm",
        event_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if (
            max_command_lifetime <= timedelta(0)
            or delivery_lifetime <= timedelta(0)
            or execution_lease_duration <= timedelta(0)
        ):
            raise ValueError("command, delivery, and execution lease lifetimes must be positive")
        self._unit_of_work_factory = unit_of_work_factory
        self._handlers = dict(handlers)
        self._clock = clock or SystemClock()
        self._retry_policy = retry_policy or RetryPolicy()
        self._max_command_lifetime = max_command_lifetime
        self._delivery_lifetime = delivery_lifetime
        self._execution_lease_duration = execution_lease_duration
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
            claim_or_result = await self._claim_locked(
                unit_of_work, command, normalize_utc(self._clock.now())
            )
        if isinstance(claim_or_result, CommandExecutionResult):
            return claim_or_result
        return await self._execute_claim(claim_or_result)

    async def process_next(self) -> CommandExecutionResult | None:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            command = await unit_of_work.commands.get_next_ready_for_update(now)
            if command is None:
                return None
            claim_or_result = await self._claim_locked(unit_of_work, command, now)
        if isinstance(claim_or_result, CommandExecutionResult):
            return claim_or_result
        return await self._execute_claim(claim_or_result)

    async def _claim_locked(
        self,
        unit_of_work: UnitOfWork,
        command: Command,
        now: datetime,
    ) -> _ExecutionClaim | CommandExecutionResult:
        if command.status in self._terminal_statuses():
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        previous_attempt: CommandAttempt | None = None
        if command.status == CommandStatus.PROCESSING:
            if (
                command.execution_lease_expires_at is not None
                and command.execution_lease_expires_at > now
            ):
                return CommandExecutionResult(command.command_id, command.status, executed=False)
            previous_attempt = await unit_of_work.attempts.get_processing_for_command_for_update(
                command.command_id
            )
            if previous_attempt is None:
                raise RuntimeError("expired command lease has no processing attempt")

        if command.deadline_at is not None and now >= command.deadline_at:
            if previous_attempt is not None:
                previous_attempt.status = AttemptStatus.FAILED_FINAL
                previous_attempt.finished_at = now
                previous_attempt.error_code = "COMMAND_EXPIRED"
                await unit_of_work.attempts.update(previous_attempt)
            command.transition(
                CommandStatus.EXPIRED,
                now,
                error_code="COMMAND_EXPIRED",
            )
            self._clear_execution_lease(command)
            await unit_of_work.commands.update(command)
            await self._enqueue_result(unit_of_work, command, now)
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        if (
            command.status == CommandStatus.FAILED_RETRYABLE
            and command.next_retry_at is not None
            and now < command.next_retry_at
        ):
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        attempt_count = await unit_of_work.attempts.count_for_command(command.command_id)
        if previous_attempt is not None:
            if attempt_count >= self._retry_policy.max_attempts:
                previous_attempt.status = AttemptStatus.FAILED_FINAL
                previous_attempt.finished_at = now
                previous_attempt.error_code = "EXECUTION_LEASE_EXPIRED"
                await unit_of_work.attempts.update(previous_attempt)
                command.transition(
                    CommandStatus.FAILED_FINAL,
                    now,
                    error_code="EXECUTION_LEASE_EXPIRED",
                )
                self._clear_execution_lease(command)
                await unit_of_work.commands.update(command)
                await self._enqueue_result(unit_of_work, command, now)
                return CommandExecutionResult(command.command_id, command.status, executed=False)
            previous_attempt.status = AttemptStatus.FAILED_RETRYABLE
            previous_attempt.finished_at = now
            previous_attempt.error_code = "EXECUTION_LEASE_EXPIRED"
            await unit_of_work.attempts.update(previous_attempt)

        if command.status == CommandStatus.RECEIVED:
            command.transition(CommandStatus.VALIDATED, now)

        handler = self._handlers.get(command.command_type)
        if handler is None:
            unavailable_status = (
                CommandStatus.FAILED_FINAL
                if command.status == CommandStatus.PROCESSING
                else CommandStatus.REJECTED
            )
            command.transition(
                unavailable_status,
                now,
                error_code="HANDLER_UNAVAILABLE",
            )
            self._clear_execution_lease(command)
            await unit_of_work.commands.update(command)
            await self._enqueue_result(unit_of_work, command, now)
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        if previous_attempt is not None:
            command.reclaim(now)
        else:
            command.transition(CommandStatus.PROCESSING, now)
        attempt_number = attempt_count + 1
        lease_token = uuid4()
        command.execution_lease_token = lease_token
        command.execution_lease_expires_at = now + self._execution_lease_duration
        attempt = CommandAttempt(command.command_id, attempt_number, started_at=now)
        await unit_of_work.attempts.add(attempt)
        await unit_of_work.commands.update(command)
        return _ExecutionClaim(command, lease_token, attempt_number)

    async def _execute_claim(self, claim: _ExecutionClaim) -> CommandExecutionResult:
        command = claim.command
        handler = self._handlers[command.command_type]

        async def persist_checkpoint(data: dict[str, object]) -> bool:
            now = normalize_utc(self._clock.now())
            async with self._unit_of_work_factory() as unit_of_work:
                return await unit_of_work.commands.save_checkpoint_if_leased(
                    command.command_id, claim.lease_token, now, data
                )

        context = CommandExecutionContext(
            attempt_number=claim.attempt_number,
            checkpoint=CommandCheckpoint(dict(command.checkpoint or {}), persist_checkpoint),
        )
        try:
            output = await self._execute_with_heartbeat(
                handler, self._rebuild_envelope(command), context, claim
            )
        except ExecutionLeaseLost:
            return await self._current_result(command.command_id)
        except RetryableCommandError as error:
            return await self._finish_failure(
                claim, error.code, retryable=True, retry_after=error.retry_after
            )
        except PermanentCommandError as error:
            return await self._finish_failure(claim, error.code, retryable=False)
        except Exception:
            return await self._finish_failure(claim, "UNEXPECTED_HANDLER_ERROR", retryable=True)
        try:
            return await self._finish_success(claim, output)
        except _SyncCursorConflict:
            return await self._finish_failure(claim, "SYNC_CURSOR_ADVANCED", retryable=True)

    async def _execute_with_heartbeat(
        self,
        handler: CommandHandler,
        command: CommandEnvelopeV1,
        context: CommandExecutionContext,
        claim: _ExecutionClaim,
    ) -> CommandExecutionOutput:
        handler_task = asyncio.create_task(handler.execute(command, context))
        heartbeat_task = asyncio.create_task(self._maintain_lease(claim))
        try:
            done, _ = await asyncio.wait(
                {handler_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat_task in done:
                await heartbeat_task
            return await handler_task
        finally:
            for task in (handler_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(handler_task, heartbeat_task, return_exceptions=True)

    async def _maintain_lease(self, claim: _ExecutionClaim) -> None:
        interval = max(self._execution_lease_duration.total_seconds() / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            now = normalize_utc(self._clock.now())
            async with self._unit_of_work_factory() as unit_of_work:
                renewed = await unit_of_work.commands.renew_execution_lease(
                    claim.command.command_id,
                    claim.lease_token,
                    now,
                    now + self._execution_lease_duration,
                )
            if not renewed:
                raise ExecutionLeaseLost("command execution lease could not be renewed")

    async def _finish_success(
        self, claim: _ExecutionClaim, output: CommandExecutionOutput
    ) -> CommandExecutionResult:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            command = await unit_of_work.commands.get_by_command_id_for_update(
                claim.command.command_id
            )
            attempt = await unit_of_work.attempts.get_processing_for_command_for_update(
                claim.command.command_id
            )
            if command is None or attempt is None:
                return CommandExecutionResult(
                    claim.command.command_id, CommandStatus.FAILED_FINAL, executed=False
                )
            if not self._owns_claim(command, attempt, claim, now):
                return CommandExecutionResult(
                    claim.command.command_id,
                    command.status,
                    executed=False,
                )
            for post in output.posts:
                await unit_of_work.posts.add_if_absent(post)
            for reply in output.replies:
                await unit_of_work.replies.add_if_absent(reply)
            for sync_state in output.sync_states:
                if not await unit_of_work.sync_states.advance_if_current(
                    sync_state.state, sync_state.expected_cursor
                ):
                    raise _SyncCursorConflict
            command.transition(CommandStatus.SUCCEEDED, now, result=output.result)
            self._clear_execution_lease(command)
            attempt.status = AttemptStatus.SUCCEEDED
            attempt.finished_at = now
            await unit_of_work.attempts.update(attempt)
            await unit_of_work.commands.update(command)
            await self._enqueue_result(unit_of_work, command, now)
            return CommandExecutionResult(command.command_id, command.status, executed=True)

    async def _finish_failure(
        self,
        claim: _ExecutionClaim,
        error_code: str,
        *,
        retryable: bool,
        retry_after: timedelta | None = None,
    ) -> CommandExecutionResult:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            command = await unit_of_work.commands.get_by_command_id_for_update(
                claim.command.command_id
            )
            attempt = await unit_of_work.attempts.get_processing_for_command_for_update(
                claim.command.command_id
            )
            if command is None or attempt is None:
                return CommandExecutionResult(
                    claim.command.command_id, CommandStatus.FAILED_FINAL, executed=False
                )
            if not self._owns_claim(command, attempt, claim, now):
                return CommandExecutionResult(
                    claim.command.command_id,
                    command.status,
                    executed=False,
                )
            deadline = command.deadline_at or now + self._max_command_lifetime
            retry_at = (
                self._retry_policy.next_attempt_at(
                    claim.attempt_number,
                    now,
                    deadline,
                    retry_after=retry_after,
                )
                if retryable
                else None
            )
            if retry_at is None:
                command.transition(CommandStatus.FAILED_FINAL, now, error_code=error_code)
                attempt.status = AttemptStatus.FAILED_FINAL
            else:
                command.transition(
                    CommandStatus.FAILED_RETRYABLE,
                    now,
                    error_code=error_code,
                    next_retry_at=retry_at,
                )
                attempt.status = AttemptStatus.FAILED_RETRYABLE
            self._clear_execution_lease(command)
            attempt.finished_at = now
            attempt.error_code = error_code
            await unit_of_work.attempts.update(attempt)
            await unit_of_work.commands.update(command)
            if command.status == CommandStatus.FAILED_FINAL:
                await self._enqueue_result(unit_of_work, command, now)
            return CommandExecutionResult(command.command_id, command.status, executed=True)

    async def _current_result(self, command_id: str) -> CommandExecutionResult:
        async with self._unit_of_work_factory() as unit_of_work:
            command = await unit_of_work.commands.get_by_command_id(command_id)
        if command is None:
            raise CommandNotFound(command_id)
        return CommandExecutionResult(command_id, command.status, executed=False)

    @staticmethod
    def _owns_claim(
        command: Command | None,
        attempt: CommandAttempt | None,
        claim: _ExecutionClaim,
        now: datetime,
    ) -> bool:
        return (
            command is not None
            and attempt is not None
            and attempt.attempt_number == claim.attempt_number
            and command.status == CommandStatus.PROCESSING
            and command.execution_lease_token == claim.lease_token
            and command.execution_lease_expires_at is not None
            and command.execution_lease_expires_at > now
        )

    @staticmethod
    def _clear_execution_lease(command: Command) -> None:
        command.execution_lease_token = None
        command.execution_lease_expires_at = None

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
