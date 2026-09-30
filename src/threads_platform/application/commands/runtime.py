import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from pydantic import ValidationError

from threads_platform.application.capability_router import CapabilityEvidence, CapabilityRouter
from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.commands.handlers import (
    CommandCheckpoint,
    CommandExecutionContext,
    CommandExecutionOutput,
    CommandHandler,
)
from threads_platform.application.commands.results import enqueue_command_result
from threads_platform.application.crm_protocol_v1 import (
    COMMAND_ENVELOPE_ADAPTER,
    CommandEnvelopeHeader,
    CommandEnvelopeV1,
    CommandReceiptV1,
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
from threads_platform.application.ports.threads import ThreadsCredentialError
from threads_platform.application.retry import RetryPolicy
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.application.worker_protocol import is_worker_protocol_supported
from threads_platform.domain.account_execution import AccountExecutionOwnerType
from threads_platform.domain.capabilities import (
    CapabilityExecutor,
    RouteTarget,
)
from threads_platform.domain.commands import (
    TERMINAL_COMMAND_STATUSES,
    AttemptStatus,
    Command,
    CommandAttempt,
    CommandStatus,
)
from threads_platform.domain.time import normalize_utc
from threads_platform.domain.worker_jobs import WorkerJob, WorkerJobRetrySafety

KNOWN_COMMAND_TYPES = frozenset(
    {
        "threads.publish_text",
        "threads.publish_image",
        "threads.publish_video",
        "threads.publish_carousel",
        "threads.create_reply",
        "threads.sync_conversation",
        "threads.discovery.create_campaign",
        "threads.discovery.complete_campaign",
        "threads.discovery.search",
        "threads.discovery.profile",
        "threads.discovery.mentions",
        "threads.discovery.conversation",
        "threads.discovery.resume",
        "threads.discovery.lead_status",
        "threads.moderate_reply",
        "threads.browser.feed.browse",
        "threads.browser.thread.open",
        "threads.browser.profile.open",
        "threads.browser.media.local_upload",
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
    lease_token: UUID = field(repr=False)
    attempt_number: int
    attempt_id: UUID
    account_coordination_generation: int | None


@dataclass(frozen=True, slots=True)
class _WorkerJobEnqueued:
    result: CommandExecutionResult
    job: WorkerJob
    notification_worker_ids: tuple[UUID, ...]
    occurred_at: datetime


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
        capability_router: CapabilityRouter | None = None,
        worker_job_service: WorkerJobService | None = None,
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
        self._capability_router = capability_router or CapabilityRouter()
        self._worker_job_service = worker_job_service
        self._logger = structlog.get_logger(__name__)

    async def receive(self, raw_command: dict[str, object]) -> CommandReceiptV1:
        try:
            header = CommandEnvelopeHeader.model_validate(raw_command)
        except ValidationError as error:
            self._logger.info("command_rejected", error_code="INVALID_ENVELOPE")
            raise CommandInputError("INVALID_ENVELOPE") from error

        with structlog.contextvars.bound_contextvars(**self._command_log_context(header)):
            return await self._receive_validated(raw_command, header)

    async def _receive_validated(
        self,
        raw_command: dict[str, object],
        header: CommandEnvelopeHeader,
    ) -> CommandReceiptV1:
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
                self._logger.info("command_rejected", error_code="UNKNOWN_ACCOUNT")
                raise CommandInputError("UNKNOWN_ACCOUNT")
            inserted = await unit_of_work.commands.add_if_absent(command)
            if not inserted:
                existing = await unit_of_work.commands.get_by_command_id(command.command_id)
                if existing is None:
                    raise RuntimeError("idempotency conflict row disappeared")
                if not self._same_command(existing, command):
                    self._logger.info("command_rejected", error_code="COMMAND_ID_CONFLICT")
                    raise IdempotencyConflict(command.command_id)
                self._logger.info("command_duplicate", status=existing.status.value)
                return CommandReceiptV1(
                    command_id=existing.command_id,
                    correlation_id=existing.correlation_id,
                    status=existing.status,
                    duplicate=True,
                )

            if command.completed_at is not None:
                await self._enqueue_result(unit_of_work, command, now)
                self._logger.info(
                    "command_rejected",
                    status=command.status.value,
                    error_code=command.error_code,
                )
            else:
                self._logger.info("command_accepted", status=command.status.value)
            return CommandReceiptV1(
                command_id=command.command_id,
                correlation_id=command.correlation_id,
                status=command.status,
            )

    async def process(self, command_id: str) -> CommandExecutionResult:
        async with self._unit_of_work_factory() as unit_of_work:
            command = await unit_of_work.commands.get_by_command_id_for_update(command_id)
            if command is None:
                self._logger.info(
                    "command_processing_rejected",
                    command_id=command_id,
                    error_code="COMMAND_NOT_FOUND",
                )
                raise CommandNotFound(command_id)
            log_context = self._command_log_context(command)
            with structlog.contextvars.bound_contextvars(**log_context):
                self._logger.info("command_processing_started", status=command.status.value)
                claim_or_result = await self._claim_locked(
                    unit_of_work, command, normalize_utc(self._clock.now())
                )
        return await self._continue_processing(command, claim_or_result, log_context)

    async def _continue_processing(
        self,
        command: Command,
        claim_or_result: _ExecutionClaim | _WorkerJobEnqueued | CommandExecutionResult,
        log_context: dict[str, str],
    ) -> CommandExecutionResult:
        with structlog.contextvars.bound_contextvars(**log_context):
            if isinstance(claim_or_result, _WorkerJobEnqueued):
                self._logger.info(
                    "command_waiting_for_worker",
                    status=claim_or_result.result.status.value,
                    worker_job_id=str(claim_or_result.job.id),
                )
                if self._worker_job_service is not None:
                    self._worker_job_service.publish_available(
                        claim_or_result.job,
                        claim_or_result.notification_worker_ids,
                        now=claim_or_result.occurred_at,
                    )
                return claim_or_result.result
            if isinstance(claim_or_result, CommandExecutionResult):
                self._logger.info(
                    "command_processing_outcome",
                    status=claim_or_result.status.value,
                    error_code=command.error_code,
                )
                return claim_or_result
            self._logger.info(
                "command_execution_started",
                status=claim_or_result.command.status.value,
                attempt_number=claim_or_result.attempt_number,
            )
            return await self._execute_claim(claim_or_result)

    async def process_next(
        self,
        *,
        now: datetime | None = None,
        exclude_command_ids: frozenset[str] = frozenset(),
    ) -> CommandExecutionResult | None:
        occurred_at = normalize_utc(now if now is not None else self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            command = await unit_of_work.commands.get_next_ready_for_update(
                occurred_at,
                exclude_command_ids=exclude_command_ids,
            )
            if command is None:
                return None
            log_context = self._command_log_context(command)
            with structlog.contextvars.bound_contextvars(**log_context):
                self._logger.info("command_processing_started", status=command.status.value)
                claim_or_result = await self._claim_locked(unit_of_work, command, occurred_at)
        return await self._continue_processing(command, claim_or_result, log_context)

    async def _claim_locked(
        self,
        unit_of_work: UnitOfWork,
        command: Command,
        now: datetime,
    ) -> _ExecutionClaim | _WorkerJobEnqueued | CommandExecutionResult:
        if command.status in self._terminal_statuses():
            return CommandExecutionResult(command.command_id, command.status, executed=False)
        if command.status is CommandStatus.WAITING_INTERVENTION:
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
            await unit_of_work.commands.update(command)
        return await self._route_and_claim(
            unit_of_work,
            command,
            now,
            attempt_count=attempt_count,
            previous_attempt=previous_attempt,
        )

    async def _route_and_claim(
        self,
        unit_of_work: UnitOfWork,
        command: Command,
        now: datetime,
        *,
        attempt_count: int,
        previous_attempt: CommandAttempt | None,
    ) -> _ExecutionClaim | _WorkerJobEnqueued | CommandExecutionResult:
        account = await unit_of_work.accounts.get_for_update(command.account_id)
        if account is None:
            raise CommandInputError("UNKNOWN_ACCOUNT")

        policy = self._capability_router.policy_for(command.command_type)
        assignment = await unit_of_work.assignments.get_active(command.account_id)
        worker = (
            await unit_of_work.workers.get(assignment.worker_id) if assignment is not None else None
        )
        advertises_capability = False
        if (
            policy is not None
            and policy.worker_capability_name is not None
            and assignment is not None
        ):
            advertises_capability = await unit_of_work.worker_capabilities.has(
                assignment.worker_id,
                policy.worker_capability_name,
                policy.worker_capability_version or 1,
            )
        existing_job = await unit_of_work.worker_jobs.get_by_command_id(command.command_id)
        previous_route = (
            await unit_of_work.command_route_decisions.get_latest_execution_for_command(
                command.command_id
            )
        )
        previous_executor = (
            previous_route.executor
            if previous_route is not None
            else CapabilityExecutor.API
            if attempt_count > 0
            else None
        )
        account_mutation_busy = False
        if policy is not None and policy.operation_class.requires_exclusive_account_coordination:
            account_mutation_busy = (
                await unit_of_work.account_execution_leases.get_active(command.account_id, now)
                is not None
            )
        decision = self._capability_router.decide(
            command.command_id,
            command.account_id,
            command.command_type,
            account.execution_mode,
            CapabilityEvidence(
                api_handler_available=command.command_type in self._handlers,
                worker_assigned=assignment is not None,
                worker_online=(
                    worker is not None
                    and worker.status.value == "ONLINE"
                    and worker.presence_expires_at is not None
                    and worker.presence_expires_at > now
                    and is_worker_protocol_supported(
                        worker.protocol_version, worker.capabilities_schema_version
                    )
                ),
                worker_advertises_capability=advertises_capability,
                account_mutation_busy=account_mutation_busy,
                account_status=account.status,
                attempt_count=attempt_count,
                previous_executor=previous_executor,
                existing_worker_job=existing_job,
            ),
        )
        decision = replace(decision, attempt_count=attempt_count, created_at=now)
        self._logger.info(
            "command_route_selected",
            route_target=decision.target.value,
            executor=decision.executor.value if decision.executor is not None else None,
            capability_name=decision.capability_name,
            status=command.status.value,
            error_code=decision.reason_code,
        )

        if decision.target is RouteTarget.UNSUPPORTED:
            await unit_of_work.command_route_decisions.add(decision)
            command.transition(
                CommandStatus.REJECTED
                if command.status is CommandStatus.VALIDATED
                else CommandStatus.FAILED_FINAL,
                now,
                error_code=decision.reason_code,
            )
            self._clear_execution_lease(command)
            await unit_of_work.commands.update(command)
            await self._enqueue_result(unit_of_work, command, now)
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        if decision.target is RouteTarget.WAITING_INTERVENTION:
            await unit_of_work.command_route_decisions.add(decision)
            command.transition(
                CommandStatus.WAITING_INTERVENTION,
                now,
                error_code=decision.reason_code,
            )
            self._clear_execution_lease(command)
            await unit_of_work.commands.update(command)
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        if decision.target is RouteTarget.WAITING_EXECUTION:
            await unit_of_work.command_route_decisions.add(decision)
            if command.status is not CommandStatus.WAITING_EXECUTION:
                command.transition(
                    CommandStatus.WAITING_EXECUTION,
                    now,
                    error_code=decision.reason_code,
                )
            self._clear_execution_lease(command)
            await unit_of_work.commands.update(command)
            return CommandExecutionResult(command.command_id, command.status, executed=False)

        if decision.target is RouteTarget.WORKER_JOB:
            if self._worker_job_service is None or policy is None:
                decision = replace(
                    decision,
                    target=RouteTarget.WAITING_EXECUTION,
                    executor=None,
                    reason_code="WORKER_JOB_SERVICE_UNAVAILABLE",
                )
                await unit_of_work.command_route_decisions.add(decision)
                if command.status is not CommandStatus.WAITING_EXECUTION:
                    command.transition(
                        CommandStatus.WAITING_EXECUTION,
                        now,
                        error_code=decision.reason_code,
                    )
                self._clear_execution_lease(command)
                await unit_of_work.commands.update(command)
                return CommandExecutionResult(command.command_id, command.status, executed=False)
            await unit_of_work.command_route_decisions.add(decision)
            job, worker_ids = await self._worker_job_service.enqueue_in_transaction(
                unit_of_work,
                policy.worker_capability_name or policy.capability_name,
                policy.worker_capability_version or policy.capability_version,
                now=now,
                command_id=command.command_id,
                account_id=command.account_id,
                assigned_worker_id=assignment.worker_id if assignment is not None else None,
                account_affinity_required=True,
                deadline_at=command.deadline_at,
                retry_safety=(
                    WorkerJobRetrySafety.RECONCILIATION_REQUIRED
                    if decision.operation_class.requires_exclusive_account_coordination
                    else WorkerJobRetrySafety.SAFE_TO_RETRY
                ),
                operation_class=decision.operation_class,
                priority=command.priority,
                preemptible=command.command_type
                in {
                    "threads.browser.feed.browse",
                    "threads.browser.thread.open",
                    "threads.browser.profile.open",
                },
                input_data=(
                    {"max_items": command.payload.get("max_items", 10)}
                    if command.command_type == "threads.browser.feed.browse"
                    else {"thread_ref": command.payload["thread_ref"]}
                    if command.command_type == "threads.browser.thread.open"
                    else {"profile_ref": command.payload["profile_ref"]}
                    if command.command_type == "threads.browser.profile.open"
                    else {"media_ref": command.payload["media_ref"]}
                    if command.command_type == "threads.browser.media.local_upload"
                    else None
                ),
            )
            return _WorkerJobEnqueued(
                CommandExecutionResult(
                    command.command_id,
                    CommandStatus.WAITING_EXECUTION,
                    executed=False,
                ),
                job,
                worker_ids,
                now,
            )

        if decision.target is not RouteTarget.LOCAL_API:
            raise RuntimeError(f"unhandled capability route: {decision.target}")

        await unit_of_work.command_route_decisions.add(decision)
        attempt_number = attempt_count + 1
        attempt = CommandAttempt(command.command_id, attempt_number, started_at=now)
        account_coordination_generation = None
        if decision.operation_class.requires_exclusive_account_coordination:
            lease = await unit_of_work.account_execution_leases.try_acquire(
                command.account_id,
                AccountExecutionOwnerType.COMMAND,
                str(attempt.id),
                decision.operation_class,
                now,
                now + self._execution_lease_duration,
            )
            if lease is None:
                deferred = replace(
                    decision,
                    target=RouteTarget.WAITING_EXECUTION,
                    executor=None,
                    reason_code="ACCOUNT_EXECUTION_ALREADY_OWNED",
                )
                await unit_of_work.command_route_decisions.add(deferred)
                if command.status is not CommandStatus.WAITING_EXECUTION:
                    command.transition(
                        CommandStatus.WAITING_EXECUTION,
                        now,
                        error_code=deferred.reason_code,
                    )
                self._clear_execution_lease(command)
                await unit_of_work.commands.update(command)
                return CommandExecutionResult(command.command_id, command.status, executed=False)
            account_coordination_generation = lease.fencing_generation

        lease_token = uuid4()
        if command.status is CommandStatus.PROCESSING:
            command.reclaim(now)
        else:
            command.transition(CommandStatus.PROCESSING, now)
        command.execution_lease_token = lease_token
        command.execution_lease_expires_at = now + self._execution_lease_duration
        await unit_of_work.attempts.add(attempt)
        await unit_of_work.commands.update(command)
        return _ExecutionClaim(
            command,
            lease_token,
            attempt_number,
            attempt.id,
            account_coordination_generation,
        )

    async def _execute_claim(self, claim: _ExecutionClaim) -> CommandExecutionResult:
        command = claim.command
        handler = self._handlers[command.command_type]

        async def persist_checkpoint(data: dict[str, object]) -> bool:
            now = normalize_utc(self._clock.now())
            async with self._unit_of_work_factory() as unit_of_work:
                if not await self._owns_account_execution(unit_of_work, claim, now):
                    return False
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
            result = await self._current_result(command.command_id)
            self._log_execution_outcome(result)
            return result
        except RetryableCommandError as error:
            result = await self._finish_failure(
                claim, error.code, retryable=True, retry_after=error.retry_after
            )
            self._log_execution_outcome(result, error_code=error.code)
            return result
        except PermanentCommandError as error:
            result = await self._finish_failure(claim, error.code, retryable=False)
            self._log_execution_outcome(result, error_code=error.code)
            return result
        except ThreadsCredentialError as error:
            result = await self._finish_failure(claim, error.code, retryable=error.retryable)
            self._log_execution_outcome(result, error_code=error.code)
            return result
        except Exception:
            error_code = "UNEXPECTED_HANDLER_ERROR"
            result = await self._finish_failure(claim, error_code, retryable=True)
            self._log_execution_outcome(result, error_code=error_code)
            return result
        try:
            result = await self._finish_success(claim, output)
        except _SyncCursorConflict:
            error_code = "SYNC_CURSOR_ADVANCED"
            result = await self._finish_failure(claim, error_code, retryable=True)
            self._log_execution_outcome(result, error_code=error_code)
            return result
        self._log_execution_outcome(result)
        return result

    def _log_execution_outcome(
        self,
        result: CommandExecutionResult,
        *,
        error_code: str | None = None,
    ) -> None:
        self._logger.info(
            "command_execution_outcome",
            status=result.status.value,
            error_code=error_code,
        )

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
                account_renewed = await self._renew_account_execution(unit_of_work, claim, now)
                if not renewed or not account_renewed:
                    raise ExecutionLeaseLost("command or account execution lease was lost")

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
            if not self._owns_claim(
                command, attempt, claim, now
            ) or not await self._owns_account_execution(unit_of_work, claim, now):
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
            await self._release_account_execution(unit_of_work, claim, now)
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
            if not self._owns_claim(
                command, attempt, claim, now
            ) or not await self._owns_account_execution(unit_of_work, claim, now):
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
            await self._release_account_execution(unit_of_work, claim, now)
            return CommandExecutionResult(command.command_id, command.status, executed=True)

    async def _owns_account_execution(
        self, unit_of_work: UnitOfWork, claim: _ExecutionClaim, now: datetime
    ) -> bool:
        if claim.account_coordination_generation is None:
            return True
        return await unit_of_work.account_execution_leases.owns(
            claim.command.account_id,
            AccountExecutionOwnerType.COMMAND,
            str(claim.attempt_id),
            claim.account_coordination_generation,
            now,
        )

    async def _renew_account_execution(
        self, unit_of_work: UnitOfWork, claim: _ExecutionClaim, now: datetime
    ) -> bool:
        if claim.account_coordination_generation is None:
            return True
        return await unit_of_work.account_execution_leases.renew(
            claim.command.account_id,
            AccountExecutionOwnerType.COMMAND,
            str(claim.attempt_id),
            claim.account_coordination_generation,
            now,
            now + self._execution_lease_duration,
        )

    async def _release_account_execution(
        self, unit_of_work: UnitOfWork, claim: _ExecutionClaim, now: datetime
    ) -> None:
        if claim.account_coordination_generation is None:
            return
        released = await unit_of_work.account_execution_leases.release(
            claim.command.account_id,
            AccountExecutionOwnerType.COMMAND,
            str(claim.attempt_id),
            claim.account_coordination_generation,
            now,
        )
        if not released:
            raise ExecutionLeaseLost("account execution fence was lost before finalization")

    async def _current_result(self, command_id: str) -> CommandExecutionResult:
        async with self._unit_of_work_factory() as unit_of_work:
            command = await unit_of_work.commands.get_by_command_id(command_id)
        if command is None:
            raise CommandNotFound(command_id)
        return CommandExecutionResult(command_id, command.status, executed=False)

    @staticmethod
    def _command_log_context(command: Command | CommandEnvelopeHeader) -> dict[str, str]:
        return {
            "command_id": command.command_id,
            "correlation_id": command.correlation_id,
            "account_id": str(command.account_id),
            "command_type": command.command_type,
        }

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
        await enqueue_command_result(
            unit_of_work,
            command,
            now,
            delivery_lifetime=self._delivery_lifetime,
            crm_destination=self._crm_destination,
            event_id_factory=self._event_id_factory,
        )

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
        return TERMINAL_COMMAND_STATUSES
