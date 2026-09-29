import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from threads_platform.application.browser_capabilities import (
    BROWSER_CAPABILITY_CONTRACTS,
    BrowserCapabilityStatus,
)
from threads_platform.application.capability_router import CapabilityRouter
from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.commands.results import enqueue_command_result
from threads_platform.application.errors import CommandNotFound
from threads_platform.application.ports.repositories import UnitOfWork, UnitOfWorkFactory
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.application.worker_protocol import is_worker_protocol_supported
from threads_platform.domain.account_activities import ScheduledActivityMaterializationStatus
from threads_platform.domain.account_execution import AccountExecutionOwnerType
from threads_platform.domain.capabilities import (
    CapabilityExecutor,
    OperationClass,
    RouteTarget,
)
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.time import normalize_utc
from threads_platform.domain.worker_jobs import (
    WorkerIntervention,
    WorkerInterventionStatus,
    WorkerJob,
    WorkerJobAttempt,
    WorkerJobAttemptStatus,
    WorkerJobCancelRequest,
    WorkerJobPreemption,
    WorkerJobPreemptionStatus,
    WorkerJobRetrySafety,
    WorkerJobStatus,
)
from threads_platform.domain.workers import WorkerNode, WorkerStatus

_CANCELLATION_SAFE_CHECKPOINTS = {
    "threads.browser.feed.browse": frozenset({"BEFORE_NAVIGATION", "FEED_READY", "ITEM_BATCH"}),
    "threads.browser.thread.open": frozenset({"BEFORE_NAVIGATION", "THREAD_READY"}),
    "threads.browser.profile.open": frozenset(
        {"BEFORE_NAVIGATION", "BEFORE_PROFILE_INSPECTION", "PROFILE_READY"}
    ),
}
_CANCELLABLE_ACTIVITY_CAPABILITIES = {
    contract.name: frozenset(
        _CANCELLATION_SAFE_CHECKPOINTS[contract.name] & frozenset(contract.safe_checkpoints)
    )
    for contract in BROWSER_CAPABILITY_CONTRACTS
    if contract.name in _CANCELLATION_SAFE_CHECKPOINTS
    and contract.version == 1
    and contract.operation_class is OperationClass.READ
    and contract.preemptible
    and contract.status is BrowserCapabilityStatus.AVAILABLE
}
_CANCEL_REASON_CODE = re.compile(r"^[A-Z0-9_]{1,120}$")


class WorkerJobControlError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkerJobReconciliation:
    jobs: tuple[WorkerJob, ...]


class WorkerJobService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        clock: Clock | None = None,
        notifications: WorkerNotificationHub | None = None,
        lease_duration: timedelta = timedelta(minutes=2),
        retry_delay: timedelta = timedelta(seconds=2),
        result_delivery_lifetime: timedelta = timedelta(hours=24),
        crm_destination: str = "crm",
        capability_router: CapabilityRouter | None = None,
    ) -> None:
        if lease_duration <= timedelta(0) or retry_delay < timedelta(0):
            raise ValueError("WorkerJob lease and retry durations are invalid")
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or SystemClock()
        self._notifications = notifications
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._result_delivery_lifetime = result_delivery_lifetime
        self._crm_destination = crm_destination
        self._capability_router = capability_router or CapabilityRouter()

    async def enqueue(
        self,
        capability_name: str,
        capability_version: int,
        *,
        command_id: str | None = None,
        account_id: UUID | None = None,
        assigned_worker_id: UUID | None = None,
        account_affinity_required: bool | None = None,
        priority: int = 0,
        preemptible: bool = False,
        scheduled_at: datetime | None = None,
        deadline_at: datetime | None = None,
        max_attempts: int = 3,
        retry_safety: WorkerJobRetrySafety = WorkerJobRetrySafety.SAFE_TO_RETRY,
        operation_class: OperationClass = OperationClass.READ,
        input_data: dict[str, object] | None = None,
    ) -> WorkerJob:
        self._ensure_capability_not_blocked(capability_name)
        now = normalize_utc(self._clock.now())
        schedule = normalize_utc(scheduled_at) if scheduled_at else now
        async with self._unit_of_work_factory() as unit_of_work:
            job, notification_worker_ids = await self.enqueue_in_transaction(
                unit_of_work,
                capability_name,
                capability_version,
                now=now,
                command_id=command_id,
                account_id=account_id,
                assigned_worker_id=assigned_worker_id,
                account_affinity_required=account_affinity_required,
                priority=priority,
                preemptible=preemptible,
                scheduled_at=schedule,
                deadline_at=deadline_at,
                max_attempts=max_attempts,
                retry_safety=retry_safety,
                operation_class=operation_class,
                input_data=input_data,
            )
        self.publish_available(job, notification_worker_ids, now=now)
        return job

    async def enqueue_in_transaction(
        self,
        unit_of_work: UnitOfWork,
        capability_name: str,
        capability_version: int,
        *,
        now: datetime,
        command_id: str | None = None,
        account_id: UUID | None = None,
        assigned_worker_id: UUID | None = None,
        account_affinity_required: bool | None = None,
        priority: int = 0,
        preemptible: bool = False,
        scheduled_at: datetime | None = None,
        deadline_at: datetime | None = None,
        max_attempts: int = 3,
        retry_safety: WorkerJobRetrySafety = WorkerJobRetrySafety.SAFE_TO_RETRY,
        operation_class: OperationClass = OperationClass.READ,
        input_data: dict[str, object] | None = None,
    ) -> tuple[WorkerJob, tuple[UUID, ...]]:
        self._ensure_capability_not_blocked(capability_name)
        occurred_at = normalize_utc(now)
        schedule = normalize_utc(scheduled_at) if scheduled_at else occurred_at
        command = None
        if command_id is not None:
            command = await unit_of_work.commands.get_by_command_id_for_update(command_id)
            if command is None:
                raise CommandNotFound(command_id)
            if command.status not in {
                CommandStatus.VALIDATED,
                CommandStatus.WAITING_EXECUTION,
            }:
                raise WorkerJobControlError("COMMAND_NOT_READY_FOR_REMOTE_EXECUTION")
            if command.deadline_at is not None and occurred_at >= command.deadline_at:
                raise WorkerJobControlError("COMMAND_DEADLINE_EXPIRED")
            if priority != command.priority:
                raise WorkerJobControlError("COMMAND_WORKER_JOB_PRIORITY_MISMATCH")
            if account_id is not None and account_id != command.account_id:
                raise WorkerJobControlError("COMMAND_ACCOUNT_MISMATCH")
            account_id = command.account_id
            if deadline_at is None:
                deadline_at = command.deadline_at

        assignment = (
            await unit_of_work.assignments.get_active(account_id)
            if account_id is not None
            else None
        )
        affinity_required = (
            account_affinity_required
            if account_affinity_required is not None
            else assignment is not None
        )
        if affinity_required:
            if assignment is None:
                raise WorkerJobControlError("ACTIVE_ACCOUNT_ASSIGNMENT_REQUIRED")
            if assigned_worker_id is not None and assigned_worker_id != assignment.worker_id:
                raise WorkerJobControlError("ACCOUNT_WORKER_AFFINITY_MISMATCH")
            assigned_worker_id = assignment.worker_id
        elif assigned_worker_id is not None and account_id is not None and assignment is not None:
            if assigned_worker_id != assignment.worker_id:
                raise WorkerJobControlError("ACCOUNT_WORKER_AFFINITY_MISMATCH")

        job = WorkerJob(
            command_id=command_id,
            account_id=account_id,
            assigned_worker_id=assigned_worker_id,
            account_affinity_required=affinity_required,
            capability_name=capability_name,
            capability_version=capability_version,
            operation_class=operation_class,
            input_data=input_data or {},
            priority=priority,
            preemptible=preemptible,
            scheduled_at=schedule,
            deadline_at=deadline_at,
            max_attempts=max_attempts,
            retry_safety=retry_safety,
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        if command is not None and command.status is CommandStatus.VALIDATED:
            command.transition(CommandStatus.WAITING_EXECUTION, occurred_at)
            await unit_of_work.commands.update(command)
        await unit_of_work.worker_jobs.add(job)
        if command is not None and command.priority == 100:
            route = await unit_of_work.command_route_decisions.get_latest_execution_for_command(
                command.command_id
            )
            if (
                route is None
                or route.target is not RouteTarget.WORKER_JOB
                or route.executor is not CapabilityExecutor.WORKER
                or route.capability_name != capability_name
                or route.capability_version != capability_version
                or route.account_id != command.account_id
                or route.operation_class is not operation_class
            ):
                raise WorkerJobControlError("HIGH_PRIORITY_ROUTE_NOT_TRUSTED")
            if account_id is None or not affinity_required:
                raise WorkerJobControlError("HIGH_PRIORITY_ACCOUNT_AFFINITY_REQUIRED")
            if await unit_of_work.accounts.get_for_update(account_id) is None:
                raise WorkerJobControlError("ACCOUNT_NOT_FOUND")
            await self._arbitrate_high_priority_job(unit_of_work, job, occurred_at)
        if assigned_worker_id is not None:
            notification_worker_ids = (assigned_worker_id,)
        else:
            notification_worker_ids = tuple(
                await unit_of_work.worker_capabilities.list_worker_ids(
                    capability_name, capability_version
                )
            )
        return job, notification_worker_ids

    def publish_available(
        self, job: WorkerJob, worker_ids: tuple[UUID, ...], *, now: datetime
    ) -> None:
        if self._notifications is None or job.scheduled_at > normalize_utc(now):
            return
        for worker_id in worker_ids:
            self._notifications.publish(
                worker_id,
                {"type": "job.available", "job_id": str(job.id)},
            )

    async def claim_next(self, worker_id: UUID) -> WorkerJob | None:
        now = normalize_utc(self._clock.now())
        token = uuid4()
        lease_expires_at = now + self._lease_duration
        async with self._unit_of_work_factory() as unit_of_work:
            worker = await unit_of_work.workers.get_for_update(worker_id)
            if worker is None:
                raise WorkerJobControlError("WORKER_NOT_FOUND")
            if not self._eligible(worker, now):
                raise WorkerJobControlError("WORKER_NOT_ELIGIBLE")
            candidates = await unit_of_work.worker_jobs.list_claimable(worker, now)
            job = None
            for candidate in candidates:
                if not await self._account_policy_allows_claim(unit_of_work, candidate, worker_id):
                    continue
                if not await self._browser_profile_claim_allows(
                    unit_of_work, candidate, worker, now
                ):
                    continue
                coordination_generation = None
                if (
                    candidate.account_id is not None
                    and candidate.operation_class.requires_exclusive_account_coordination
                ):
                    account_lease = await unit_of_work.account_execution_leases.try_acquire(
                        candidate.account_id,
                        AccountExecutionOwnerType.WORKER_JOB,
                        str(candidate.id),
                        candidate.operation_class,
                        now,
                        lease_expires_at,
                    )
                    if account_lease is None:
                        continue
                    coordination_generation = account_lease.fencing_generation
                if (
                    candidate.status is WorkerJobStatus.RUNNING
                    and candidate.lease_expires_at is not None
                    and candidate.lease_expires_at <= now
                ):
                    await self._supersede_pending_cancel(
                        unit_of_work, candidate.id, now, "TARGET_ATTEMPT_ENDED"
                    )
                job = await unit_of_work.worker_jobs.claim(
                    candidate,
                    worker_id,
                    now,
                    lease_expires_at,
                    token,
                    coordination_generation,
                )
                if job is not None:
                    attempt = WorkerJobAttempt(
                        worker_job_id=job.id,
                        attempt_number=job.attempt_count,
                        worker_id=worker_id,
                        lease_token=token,
                        started_at=now,
                    )
                    await unit_of_work.worker_job_attempts.add(attempt)
                    await self._request_cancel_for_waiting_preemption(unit_of_work, job, now)
                    break
                if coordination_generation is not None and candidate.account_id is not None:
                    await unit_of_work.account_execution_leases.release(
                        candidate.account_id,
                        AccountExecutionOwnerType.WORKER_JOB,
                        str(candidate.id),
                        coordination_generation,
                        now,
                    )
        return job

    async def renew(self, job_id: UUID, worker_id: UUID, lease_token: UUID) -> WorkerJob:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            job = await self._locked_job(unit_of_work, job_id)
            if not job.renew(worker_id, lease_token, now, now + self._lease_duration):
                raise WorkerJobControlError("WORKER_JOB_LEASE_LOST")
            if not await self._renew_account_coordination(unit_of_work, job, now):
                raise WorkerJobControlError("WORKER_JOB_ACCOUNT_COORDINATION_LOST")
            await unit_of_work.worker_jobs.update(job)
            await self._attach_pending_cancel(unit_of_work, job, now)
        return job

    async def checkpoint(
        self,
        job_id: UUID,
        worker_id: UUID,
        lease_token: UUID,
        checkpoint: dict[str, object],
    ) -> WorkerJob:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            job = await self._locked_job(unit_of_work, job_id)
            if not job.save_checkpoint(worker_id, lease_token, now, checkpoint):
                raise WorkerJobControlError("WORKER_JOB_LEASE_LOST")
            if not await self._owns_account_coordination(unit_of_work, job, now):
                raise WorkerJobControlError("WORKER_JOB_ACCOUNT_COORDINATION_LOST")
            await unit_of_work.worker_jobs.update(job)
            await self._attach_pending_cancel(unit_of_work, job, now)
        return job

    async def request_cancel(
        self,
        job_id: UUID,
        *,
        reason_code: str,
        now: datetime,
    ) -> WorkerJobCancelRequest:
        occurred_at = normalize_utc(now)
        if _CANCEL_REASON_CODE.fullmatch(reason_code) is None:
            raise WorkerJobControlError("CANCEL_REASON_INVALID")
        async with self._unit_of_work_factory() as unit_of_work:
            job = await self._locked_job(unit_of_work, job_id)
            request = await self._request_cancel_in_transaction(
                unit_of_work, job, reason_code=reason_code, now=occurred_at
            )
        return request

    async def _request_cancel_in_transaction(
        self,
        unit_of_work: UnitOfWork,
        job: WorkerJob,
        *,
        reason_code: str,
        now: datetime,
    ) -> WorkerJobCancelRequest:
        if _CANCEL_REASON_CODE.fullmatch(reason_code) is None:
            raise WorkerJobControlError("CANCEL_REASON_INVALID")
        attempt = await self._validate_cancel_target(unit_of_work, job, now)
        pending = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(
            job.id, for_update=True
        )
        if pending is not None:
            if pending.target_attempt_id == attempt.id:
                return pending
            pending.supersede(now, "TARGET_ATTEMPT_ENDED")
            await unit_of_work.worker_job_cancel_requests.update(pending)
        request = WorkerJobCancelRequest(
            worker_job_id=job.id,
            generation=(await unit_of_work.worker_job_cancel_requests.latest_generation(job.id))
            + 1,
            target_attempt_id=attempt.id,
            target_attempt_number=attempt.attempt_number,
            reason_code=reason_code,
            requested_at=now,
        )
        await unit_of_work.worker_job_cancel_requests.add(request)
        return request

    async def _arbitrate_high_priority_job(
        self, unit_of_work: UnitOfWork, preemptor: WorkerJob, now: datetime
    ) -> None:
        if preemptor.account_id is None or preemptor.priority != 100:
            return
        running_jobs = await unit_of_work.worker_jobs.list_running_browser_for_account(
            preemptor.account_id
        )
        for candidate in running_jobs:
            if candidate.id == preemptor.id or candidate.priority >= preemptor.priority:
                continue
            victim = await self._locked_job(unit_of_work, candidate.id)
            if (
                victim.status is not WorkerJobStatus.RUNNING
                or victim.account_id != preemptor.account_id
                or not victim.account_affinity_required
                or victim.priority >= preemptor.priority
            ):
                continue
            preemption = await unit_of_work.worker_job_preemptions.add_if_absent(
                WorkerJobPreemption(
                    account_id=preemptor.account_id,
                    preemptor_worker_job_id=preemptor.id,
                    victim_worker_job_id=candidate.id,
                    created_at=now,
                )
            )
            if preemption.status is not WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE:
                continue
            if (
                not victim.preemptible
                or victim.capability_version != 1
                or victim.operation_class is not OperationClass.READ
                or victim.capability_name not in _CANCELLABLE_ACTIVITY_CAPABILITIES
                or victim.lease_expires_at is None
                or victim.lease_expires_at <= now
            ):
                continue
            try:
                request = await self._request_cancel_in_transaction(
                    unit_of_work,
                    victim,
                    reason_code="HIGH_PRIORITY_PREEMPTION",
                    now=now,
                )
            except WorkerJobControlError:
                # Invalid, generic, or no-longer-live victims remain durable blockers,
                # but only the existing #49 primitive may decide cancellation eligibility.
                continue
            if preemption.cancel_request_id != request.id:
                preemption.cancel_request_id = request.id
                await unit_of_work.worker_job_preemptions.update(preemption)

    async def acknowledge_cancel(
        self,
        job_id: UUID,
        worker_id: UUID,
        lease_token: UUID,
        *,
        cancel_request_id: UUID,
        generation: int,
        checkpoint_phase: str,
    ) -> WorkerJob:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            job = await self._locked_job(unit_of_work, job_id)
            if not job.owns_lease(worker_id, lease_token, now):
                raise WorkerJobControlError("WORKER_JOB_LEASE_LOST")
            if not job.preemptible:
                raise WorkerJobControlError("WORKER_JOB_NOT_PREEMPTIBLE")
            allowed_phases = _CANCELLABLE_ACTIVITY_CAPABILITIES.get(job.capability_name)
            if (
                job.capability_version != 1
                or job.operation_class is not OperationClass.READ
                or allowed_phases is None
                or checkpoint_phase not in allowed_phases
            ):
                raise WorkerJobControlError("CANCEL_CHECKPOINT_NOT_SAFE")
            checkpoint = job.checkpoint
            if not isinstance(checkpoint, dict) or checkpoint.get("phase") != checkpoint_phase:
                raise WorkerJobControlError("CANCEL_CHECKPOINT_MISMATCH")
            attempt = await self._running_attempt(unit_of_work, job.id)
            if (
                attempt.attempt_number != job.attempt_count
                or attempt.worker_id != worker_id
                or attempt.lease_token != lease_token
            ):
                raise WorkerJobControlError("WORKER_JOB_ATTEMPT_STALE")
            request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(
                job.id, for_update=True
            )
            if (
                request is None
                or request.id != cancel_request_id
                or request.generation != generation
                or request.target_attempt_id != attempt.id
                or request.target_attempt_number != attempt.attempt_number
            ):
                raise WorkerJobControlError("CANCEL_REQUEST_STALE")
            await self._validate_materialized_activity_link(unit_of_work, job)
            generation_fence = job.account_coordination_generation
            if not job.cancel(worker_id, lease_token, now, request.reason_code):
                raise WorkerJobControlError("WORKER_JOB_LEASE_LOST")
            request.acknowledge(now, checkpoint_phase)
            await unit_of_work.worker_job_cancel_requests.update(request)
            attempt.status = WorkerJobAttemptStatus.CANCELLED
            attempt.finished_at = now
            attempt.error_code = request.reason_code
            await unit_of_work.worker_job_attempts.update(attempt)
            await unit_of_work.worker_jobs.update(job)
            await self._finalize_command(
                unit_of_work,
                job,
                CommandStatus.CANCELLED,
                now,
                error_code=request.reason_code,
            )
            await self._satisfy_preemptions_for_victim(
                unit_of_work, job.id, now, "CANCEL_ACKNOWLEDGED"
            )
            await self._release_account_coordination(unit_of_work, job, generation_fence, now)
        return job

    async def complete(
        self,
        job_id: UUID,
        worker_id: UUID,
        lease_token: UUID,
        result: dict[str, object],
    ) -> WorkerJob:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            job = await self._locked_job(unit_of_work, job_id)
            if not await self._owns_account_coordination(unit_of_work, job, now):
                raise WorkerJobControlError("WORKER_JOB_ACCOUNT_COORDINATION_LOST")
            generation = job.account_coordination_generation
            if not job.complete(worker_id, lease_token, now, result):
                raise WorkerJobControlError("WORKER_JOB_LEASE_LOST")
            await self._supersede_pending_cancel(unit_of_work, job.id, now, "TARGET_ATTEMPT_ENDED")
            await self._satisfy_preemptions_for_victim(unit_of_work, job.id, now, "SUCCEEDED")
            attempt = await self._running_attempt(unit_of_work, job.id)
            attempt.status = WorkerJobAttemptStatus.SUCCEEDED
            attempt.finished_at = now
            await unit_of_work.worker_job_attempts.update(attempt)
            await unit_of_work.worker_jobs.update(job)
            await self._finalize_command(
                unit_of_work,
                job,
                CommandStatus.SUCCEEDED,
                now,
                result=result,
            )
            await self._release_account_coordination(unit_of_work, job, generation, now)
        return job

    async def fail(
        self,
        job_id: UUID,
        worker_id: UUID,
        lease_token: UUID,
        *,
        error_code: str,
        retryable: bool,
        outcome_ambiguous: bool = False,
    ) -> WorkerJob:
        now = normalize_utc(self._clock.now())
        retry_at = now + self._retry_delay
        async with self._unit_of_work_factory() as unit_of_work:
            job = await self._locked_job(unit_of_work, job_id)
            if not await self._owns_account_coordination(unit_of_work, job, now):
                raise WorkerJobControlError("WORKER_JOB_ACCOUNT_COORDINATION_LOST")
            generation = job.account_coordination_generation
            if not job.fail(
                worker_id,
                lease_token,
                now,
                error_code=error_code,
                retryable=retryable,
                outcome_ambiguous=outcome_ambiguous,
                retry_at=retry_at,
            ):
                raise WorkerJobControlError("WORKER_JOB_LEASE_LOST")
            await self._supersede_pending_cancel(unit_of_work, job.id, now, "TARGET_ATTEMPT_ENDED")
            await self._satisfy_preemptions_for_victim(unit_of_work, job.id, now, "FAILED")
            attempt = await self._running_attempt(unit_of_work, job.id)
            attempt.finished_at = now
            attempt.error_code = error_code
            if job.status is WorkerJobStatus.WAITING_INTERVENTION:
                attempt.status = WorkerJobAttemptStatus.WAITING_INTERVENTION
                await self._add_intervention(
                    unit_of_work,
                    job,
                    worker_id,
                    "AMBIGUOUS_OUTCOME" if outcome_ambiguous else "OPERATOR_CONFIRMATION_REQUIRED",
                    error_code,
                    now,
                )
                await self._set_command_waiting_intervention(unit_of_work, job, now)
            elif job.status is WorkerJobStatus.FAILED_RETRYABLE:
                attempt.status = WorkerJobAttemptStatus.FAILED_RETRYABLE
            else:
                attempt.status = WorkerJobAttemptStatus.FAILED_FINAL
            await unit_of_work.worker_job_attempts.update(attempt)
            await unit_of_work.worker_jobs.update(job)
            if job.status is WorkerJobStatus.FAILED_FINAL:
                await self._finalize_command(
                    unit_of_work,
                    job,
                    CommandStatus.FAILED_FINAL,
                    now,
                    error_code=error_code,
                )
            await self._release_account_coordination(unit_of_work, job, generation, now)
        return job

    async def request_intervention(
        self,
        job_id: UUID,
        worker_id: UUID,
        lease_token: UUID,
        *,
        intervention_type: str,
        detail_code: str,
    ) -> WorkerJob:
        allowed_types = {
            "LOGIN_REQUIRED",
            "SESSION_EXPIRED",
            "CHALLENGE_REQUIRED",
            "OPERATOR_CONFIRMATION_REQUIRED",
            "AMBIGUOUS_OUTCOME",
            "REMOTE_STATE_UNCERTAIN",
        }
        if intervention_type not in allowed_types:
            raise WorkerJobControlError("INTERVENTION_TYPE_UNSUPPORTED")
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            job = await self._locked_job(unit_of_work, job_id)
            if not await self._owns_account_coordination(unit_of_work, job, now):
                raise WorkerJobControlError("WORKER_JOB_ACCOUNT_COORDINATION_LOST")
            generation = job.account_coordination_generation
            if not job.require_intervention(worker_id, lease_token, now):
                raise WorkerJobControlError("WORKER_JOB_LEASE_LOST")
            await self._supersede_pending_cancel(unit_of_work, job.id, now, "TARGET_ATTEMPT_ENDED")
            await self._satisfy_preemptions_for_victim(
                unit_of_work, job.id, now, "INTERVENTION_REQUESTED"
            )
            if intervention_type == "AMBIGUOUS_OUTCOME":
                job.retry_safety = WorkerJobRetrySafety.RECONCILIATION_REQUIRED
            attempt = await self._running_attempt(unit_of_work, job.id)
            attempt.status = WorkerJobAttemptStatus.WAITING_INTERVENTION
            attempt.finished_at = now
            attempt.error_code = detail_code
            await unit_of_work.worker_job_attempts.update(attempt)
            await self._add_intervention(
                unit_of_work,
                job,
                worker_id,
                intervention_type,
                detail_code,
                now,
            )
            await unit_of_work.worker_jobs.update(job)
            await self._set_command_waiting_intervention(unit_of_work, job, now)
            await self._release_account_coordination(unit_of_work, job, generation, now)
        return job

    async def resolve_intervention(
        self,
        intervention_id: UUID,
        *,
        requeue: bool,
        confirmed_safe_to_retry: bool = False,
        resolved_by: str = "operator",
    ) -> WorkerJob:
        now = normalize_utc(self._clock.now())
        notification_worker_ids: list[UUID] = []
        async with self._unit_of_work_factory() as unit_of_work:
            intervention = await unit_of_work.worker_interventions.get_for_update(intervention_id)
            if intervention is None:
                raise WorkerJobControlError("INTERVENTION_NOT_FOUND")
            if intervention.status is not WorkerInterventionStatus.OPEN:
                raise WorkerJobControlError("INTERVENTION_ALREADY_RESOLVED")
            job = await self._locked_job(unit_of_work, intervention.worker_job_id)
            try:
                job.resolve_intervention(
                    now,
                    requeue=requeue,
                    confirmed_safe_to_retry=confirmed_safe_to_retry,
                )
            except ValueError as error:
                raise WorkerJobControlError("INTERVENTION_REQUEUE_NOT_SAFE") from error
            intervention.status = WorkerInterventionStatus.RESOLVED
            intervention.resolved_at = now
            intervention.resolved_by = resolved_by
            await unit_of_work.worker_interventions.update(intervention)
            await unit_of_work.worker_jobs.update(job)
            if requeue:
                await self._set_command_waiting_execution(unit_of_work, job, now)
                if job.assigned_worker_id is not None:
                    notification_worker_ids = [job.assigned_worker_id]
                else:
                    notification_worker_ids = (
                        await unit_of_work.worker_capabilities.list_worker_ids(
                            job.capability_name,
                            job.capability_version,
                        )
                    )
            else:
                await self._finalize_command(
                    unit_of_work,
                    job,
                    CommandStatus.FAILED_FINAL,
                    now,
                    error_code=job.error_code or "INTERVENTION_CLOSED",
                )
                await self._supersede_preemptions_for_preemptor(unit_of_work, job.id, now)
        if self._notifications is not None:
            for worker_id in notification_worker_ids:
                self._notifications.publish(
                    worker_id,
                    {"type": "job.available", "job_id": str(job.id)},
                )
        return job

    async def reconcile(self, worker_id: UUID) -> WorkerJobReconciliation:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            worker = await unit_of_work.workers.get(worker_id)
            if worker is None or worker.status is WorkerStatus.DISABLED:
                raise WorkerJobControlError("WORKER_NOT_FOUND")
            jobs = await unit_of_work.worker_jobs.list_for_reconcile(worker_id, now)
            for job in jobs:
                await self._attach_pending_cancel(unit_of_work, job, now)
        return WorkerJobReconciliation(tuple(jobs))

    async def recover_expired(self, limit: int = 50) -> int:
        now = normalize_utc(self._clock.now())
        recovered = 0
        async with self._unit_of_work_factory() as unit_of_work:
            jobs = await unit_of_work.worker_jobs.list_expired_for_update(now, limit)
            for job in jobs:
                generation = job.account_coordination_generation
                attempt = await unit_of_work.worker_job_attempts.get_running_for_update(job.id)
                await self._supersede_pending_cancel(
                    unit_of_work, job.id, now, "TARGET_ATTEMPT_ENDED"
                )
                if job.deadline_at is not None and job.deadline_at <= now:
                    if attempt is not None:
                        attempt.status = WorkerJobAttemptStatus.FAILED_FINAL
                        attempt.finished_at = now
                        attempt.error_code = "JOB_DEADLINE_EXPIRED"
                        await unit_of_work.worker_job_attempts.update(attempt)
                    intervention = await unit_of_work.worker_interventions.get_open_for_job(job.id)
                    if intervention is not None:
                        intervention.status = WorkerInterventionStatus.CANCELLED
                        intervention.resolved_at = now
                        intervention.resolved_by = "system:deadline"
                        await unit_of_work.worker_interventions.update(intervention)
                    job.expire(now)
                    await unit_of_work.worker_jobs.update(job)
                    await self._release_account_coordination(unit_of_work, job, generation, now)
                    await self._finalize_command(
                        unit_of_work,
                        job,
                        CommandStatus.EXPIRED,
                        now,
                        error_code="JOB_DEADLINE_EXPIRED",
                    )
                elif job.attempt_count >= job.max_attempts:
                    if attempt is not None:
                        attempt.status = WorkerJobAttemptStatus.FAILED_FINAL
                        attempt.finished_at = now
                        attempt.error_code = "WORKER_JOB_ATTEMPTS_EXHAUSTED"
                        await unit_of_work.worker_job_attempts.update(attempt)
                    job.finalize_failure(now, "WORKER_JOB_ATTEMPTS_EXHAUSTED")
                    await unit_of_work.worker_jobs.update(job)
                    await self._release_account_coordination(unit_of_work, job, generation, now)
                    await self._finalize_command(
                        unit_of_work,
                        job,
                        CommandStatus.FAILED_FINAL,
                        now,
                        error_code="WORKER_JOB_ATTEMPTS_EXHAUSTED",
                    )
                elif (
                    job.status is WorkerJobStatus.RUNNING
                    and job.retry_safety is WorkerJobRetrySafety.RECONCILIATION_REQUIRED
                ):
                    owner = job.lease_worker_id or job.assigned_worker_id
                    if attempt is not None:
                        attempt.status = WorkerJobAttemptStatus.WAITING_INTERVENTION
                        attempt.finished_at = now
                        attempt.error_code = "AMBIGUOUS_OUTCOME"
                        await unit_of_work.worker_job_attempts.update(attempt)
                    job.suspend_for_intervention(now, "AMBIGUOUS_OUTCOME")
                    await unit_of_work.worker_jobs.update(job)
                    await self._release_account_coordination(unit_of_work, job, generation, now)
                    await self._add_intervention(
                        unit_of_work,
                        job,
                        owner,
                        "AMBIGUOUS_OUTCOME",
                        "LEASE_EXPIRED_WITH_RECONCILIATION_REQUIRED",
                        now,
                    )
                    await self._set_command_waiting_intervention(unit_of_work, job, now)
                if job.status in {
                    WorkerJobStatus.SUCCEEDED,
                    WorkerJobStatus.FAILED_FINAL,
                    WorkerJobStatus.CANCELLED,
                    WorkerJobStatus.EXPIRED,
                }:
                    await self._supersede_preemptions_for_preemptor(unit_of_work, job.id, now)
                recovered += 1
        return recovered

    async def get(self, job_id: UUID) -> WorkerJob:
        async with self._unit_of_work_factory() as unit_of_work:
            job = await unit_of_work.worker_jobs.get(job_id)
        if job is None:
            raise WorkerJobControlError("WORKER_JOB_NOT_FOUND")
        return job

    async def attempts(self, job_id: UUID) -> list[WorkerJobAttempt]:
        async with self._unit_of_work_factory() as unit_of_work:
            return await unit_of_work.worker_job_attempts.list_for_job(job_id)

    async def interventions(self, worker_id: UUID) -> list[WorkerIntervention]:
        async with self._unit_of_work_factory() as unit_of_work:
            return await unit_of_work.worker_interventions.list_for_worker(worker_id)

    @staticmethod
    def _eligible(worker: WorkerNode, now: datetime) -> bool:
        return (
            worker.status is WorkerStatus.ONLINE
            and is_worker_protocol_supported(
                worker.protocol_version, worker.capabilities_schema_version
            )
            and worker.presence_expires_at is not None
            and worker.presence_expires_at > now
        )

    @staticmethod
    async def _locked_job(unit_of_work: UnitOfWork, job_id: UUID) -> WorkerJob:
        job = await unit_of_work.worker_jobs.get_for_update(job_id)
        if job is None:
            raise WorkerJobControlError("WORKER_JOB_NOT_FOUND")
        return job

    async def _owns_account_coordination(
        self, unit_of_work: UnitOfWork, job: WorkerJob, now: datetime
    ) -> bool:
        if (
            job.account_id is None
            or not job.operation_class.requires_exclusive_account_coordination
        ):
            return True
        if job.account_coordination_generation is None:
            return False
        return await unit_of_work.account_execution_leases.owns(
            job.account_id,
            AccountExecutionOwnerType.WORKER_JOB,
            str(job.id),
            job.account_coordination_generation,
            now,
        )

    async def _validate_cancel_target(
        self, unit_of_work: UnitOfWork, job: WorkerJob, now: datetime
    ) -> WorkerJobAttempt:
        if job.status is not WorkerJobStatus.RUNNING:
            raise WorkerJobControlError("WORKER_JOB_NOT_RUNNING")
        if not job.preemptible:
            raise WorkerJobControlError("WORKER_JOB_NOT_PREEMPTIBLE")
        if (
            job.capability_version != 1
            or not _CANCELLABLE_ACTIVITY_CAPABILITIES.get(job.capability_name)
            or job.operation_class is not OperationClass.READ
        ):
            raise WorkerJobControlError("WORKER_JOB_CANCELLATION_UNSUPPORTED")
        if (
            job.lease_worker_id is None
            or job.lease_token is None
            or not job.owns_lease(job.lease_worker_id, job.lease_token, now)
        ):
            raise WorkerJobControlError("WORKER_JOB_LEASE_LOST")
        attempt = await self._running_attempt(unit_of_work, job.id)
        if (
            attempt.attempt_number != job.attempt_count
            or attempt.worker_id != job.lease_worker_id
            or attempt.lease_token != job.lease_token
        ):
            raise WorkerJobControlError("WORKER_JOB_ATTEMPT_STALE")
        await self._validate_materialized_activity_link(unit_of_work, job)
        return attempt

    async def _validate_materialized_activity_link(
        self, unit_of_work: UnitOfWork, job: WorkerJob
    ) -> None:
        if job.command_id is None or not job.command_id.startswith("activity:"):
            raise WorkerJobControlError("WORKER_JOB_NOT_MATERIALIZED_ACTIVITY")
        try:
            activity_id = UUID(job.command_id.removeprefix("activity:"))
        except ValueError as error:
            raise WorkerJobControlError("WORKER_JOB_NOT_MATERIALIZED_ACTIVITY") from error
        if job.command_id != f"activity:{activity_id}":
            raise WorkerJobControlError("WORKER_JOB_NOT_MATERIALIZED_ACTIVITY")
        activity = await unit_of_work.scheduled_activities.get(activity_id)
        command = await unit_of_work.commands.get_by_command_id(job.command_id)
        if (
            activity is None
            or activity.materialization_status
            is not ScheduledActivityMaterializationStatus.MATERIALIZED
            or activity.command_id != job.command_id
            or activity.account_id != job.account_id
            or activity.activity_type_snapshot != job.capability_name
            or activity.priority.worker_job_priority != job.priority
            or command is None
            or command.command_type != job.capability_name
            or command.account_id != job.account_id
            or command.priority != job.priority
            or command.status is not CommandStatus.WAITING_EXECUTION
        ):
            raise WorkerJobControlError("WORKER_JOB_NOT_MATERIALIZED_ACTIVITY")

    async def _attach_pending_cancel(
        self, unit_of_work: UnitOfWork, job: WorkerJob, now: datetime
    ) -> None:
        job.pending_cancel_request = None
        if (
            job.status is not WorkerJobStatus.RUNNING
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            return
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(job.id)
        if request is None:
            return
        attempt = await unit_of_work.worker_job_attempts.get_running_for_update(job.id)
        if (
            attempt is not None
            and request.target_attempt_id == attempt.id
            and request.target_attempt_number == attempt.attempt_number == job.attempt_count
            and attempt.worker_id == job.lease_worker_id
            and attempt.lease_token == job.lease_token
        ):
            job.pending_cancel_request = request

    async def _supersede_pending_cancel(
        self, unit_of_work: UnitOfWork, job_id: UUID, now: datetime, reason: str
    ) -> None:
        request = await unit_of_work.worker_job_cancel_requests.get_pending_for_job(
            job_id, for_update=True
        )
        if request is not None:
            request.supersede(now, reason)
            await unit_of_work.worker_job_cancel_requests.update(request)

    async def _satisfy_preemptions_for_victim(
        self, unit_of_work: UnitOfWork, job_id: UUID, now: datetime, reason: str
    ) -> None:
        preemptions = await unit_of_work.worker_job_preemptions.list_waiting_for_victim(
            job_id, for_update=True
        )
        for preemption in preemptions:
            preemption.satisfy(now, reason)
            await unit_of_work.worker_job_preemptions.update(preemption)

    async def _supersede_preemptions_for_preemptor(
        self, unit_of_work: UnitOfWork, job_id: UUID, now: datetime
    ) -> None:
        preemptions = await unit_of_work.worker_job_preemptions.list_waiting_for_preemptor(
            job_id, for_update=True
        )
        victim_ids: set[UUID] = set()
        for preemption in preemptions:
            preemption.supersede(now, "PREEMPTOR_ENDED")
            victim_ids.add(preemption.victim_worker_job_id)
            await unit_of_work.worker_job_preemptions.update(preemption)
        for victim_id in victim_ids:
            remaining = await unit_of_work.worker_job_preemptions.list_waiting_for_victim(victim_id)
            if not remaining:
                await self._supersede_pending_cancel(
                    unit_of_work, victim_id, now, "PREEMPTION_NO_LONGER_PENDING"
                )

    async def _account_policy_allows_claim(
        self, unit_of_work: UnitOfWork, job: WorkerJob, worker_id: UUID
    ) -> bool:
        policy = self._capability_router.policy_for(job.capability_name)
        if policy is not None and policy.blocked_reason_code is not None:
            return False
        if job.account_id is None:
            return True
        account = await unit_of_work.accounts.get_for_update(job.account_id)
        if account is None:
            return False
        if job.account_affinity_required:
            assignment = await unit_of_work.assignments.get_active(job.account_id)
            if assignment is None or assignment.worker_id != worker_id:
                return False
        if job.command_id is None:
            return True
        command = await unit_of_work.commands.get_by_command_id(job.command_id)
        if (
            command is None
            or command.account_id != job.account_id
            or command.status is not CommandStatus.WAITING_EXECUTION
        ):
            return False
        route = await unit_of_work.command_route_decisions.get_latest_execution_for_command(
            job.command_id
        )
        if route is None:
            return True
        if route.executor is not CapabilityExecutor.WORKER:
            return False
        if account.status.value != "ACTIVE":
            return False
        policy = self._capability_router.policy_for(command.command_type)
        return (
            self._capability_router.worker_execution_allowed(
                command.command_type,
                account.execution_mode,
            )
            and policy is not None
            and policy.worker_capability_name == job.capability_name
            and policy.worker_capability_version == job.capability_version
        )

    async def _browser_profile_claim_allows(
        self,
        unit_of_work: UnitOfWork,
        candidate: WorkerJob,
        worker: WorkerNode,
        now: datetime,
    ) -> bool:
        if candidate.account_id is None:
            return True

        waiting = await unit_of_work.worker_job_preemptions.list_waiting_for_account(
            candidate.account_id
        )
        if candidate.account_affinity_required and any(
            preemption.preemptor_worker_job_id == candidate.id for preemption in waiting
        ):
            return False
        if not candidate.account_affinity_required or not candidate.capability_name.startswith(
            "threads.browser."
        ):
            return True

        is_waiting_victim_reclaim = candidate.status in {
            WorkerJobStatus.RUNNING,
            WorkerJobStatus.QUEUED,
            WorkerJobStatus.FAILED_RETRYABLE,
        } and any(preemption.victim_worker_job_id == candidate.id for preemption in waiting)
        if not is_waiting_victim_reclaim:
            ordered = await unit_of_work.worker_jobs.list_claimable(
                worker,
                now,
                account_id=candidate.account_id,
                browser_profile_only=True,
            )
            if not ordered or ordered[0].id != candidate.id:
                return False

        running = await unit_of_work.worker_jobs.list_running_browser_for_account(
            candidate.account_id
        )
        if any(job.id != candidate.id for job in running):
            return False

        if any(preemption.victim_worker_job_id != candidate.id for preemption in waiting):
            return False
        return True

    async def _request_cancel_for_waiting_preemption(
        self, unit_of_work: UnitOfWork, job: WorkerJob, now: datetime
    ) -> None:
        if job.account_id is None or not job.account_affinity_required:
            return
        waiting = await unit_of_work.worker_job_preemptions.list_waiting_for_victim(
            job.id, for_update=True
        )
        viable: list[WorkerJobPreemption] = []
        for preemption in waiting:
            preemptor = await unit_of_work.worker_jobs.get(preemption.preemptor_worker_job_id)
            if (
                preemptor is None
                or preemptor.status
                not in {WorkerJobStatus.QUEUED, WorkerJobStatus.FAILED_RETRYABLE}
                or preemptor.priority <= job.priority
                or (preemptor.deadline_at is not None and preemptor.deadline_at <= now)
            ):
                preemption.supersede(now, "PREEMPTOR_ENDED")
                await unit_of_work.worker_job_preemptions.update(preemption)
            else:
                viable.append(preemption)
        if not viable or job.priority >= 100:
            return
        try:
            request = await self._request_cancel_in_transaction(
                unit_of_work,
                job,
                reason_code="HIGH_PRIORITY_PREEMPTION",
                now=now,
            )
        except WorkerJobControlError:
            return
        job.pending_cancel_request = request
        for preemption in viable:
            if preemption.cancel_request_id != request.id:
                preemption.cancel_request_id = request.id
                await unit_of_work.worker_job_preemptions.update(preemption)

    def _ensure_capability_not_blocked(self, capability_name: str) -> None:
        policy = self._capability_router.policy_for(capability_name)
        if policy is not None and policy.blocked_reason_code is not None:
            raise WorkerJobControlError(policy.blocked_reason_code)

    async def _renew_account_coordination(
        self, unit_of_work: UnitOfWork, job: WorkerJob, now: datetime
    ) -> bool:
        if (
            job.account_id is None
            or not job.operation_class.requires_exclusive_account_coordination
        ):
            return True
        if job.account_coordination_generation is None:
            return False
        return await unit_of_work.account_execution_leases.renew(
            job.account_id,
            AccountExecutionOwnerType.WORKER_JOB,
            str(job.id),
            job.account_coordination_generation,
            now,
            job.lease_expires_at or now,
        )

    async def _release_account_coordination(
        self,
        unit_of_work: UnitOfWork,
        job: WorkerJob,
        generation: int | None,
        now: datetime,
    ) -> None:
        if job.account_id is None or generation is None:
            return
        await unit_of_work.account_execution_leases.release(
            job.account_id,
            AccountExecutionOwnerType.WORKER_JOB,
            str(job.id),
            generation,
            now,
        )

    @staticmethod
    async def _running_attempt(unit_of_work: UnitOfWork, job_id: UUID) -> WorkerJobAttempt:
        attempt = await unit_of_work.worker_job_attempts.get_running_for_update(job_id)
        if attempt is None:
            raise WorkerJobControlError("WORKER_JOB_ATTEMPT_MISSING")
        return attempt

    async def _add_intervention(
        self,
        unit_of_work: UnitOfWork,
        job: WorkerJob,
        worker_id: UUID | None,
        intervention_type: str,
        detail_code: str,
        now: datetime,
    ) -> None:
        intervention = await unit_of_work.worker_interventions.get_open_for_job(job.id)
        if intervention is not None:
            return
        await unit_of_work.worker_interventions.add(
            WorkerIntervention(
                worker_job_id=job.id,
                account_id=job.account_id,
                worker_id=worker_id,
                intervention_type=intervention_type,
                detail_code=detail_code,
                created_at=now,
            )
        )

    async def _set_command_waiting_intervention(
        self, unit_of_work: UnitOfWork, job: WorkerJob, now: datetime
    ) -> None:
        command = await self._command_for_job(unit_of_work, job)
        if command is None:
            return
        if command.status is CommandStatus.WAITING_EXECUTION:
            command.transition(CommandStatus.WAITING_INTERVENTION, now)
            await unit_of_work.commands.update(command)
        elif command.status is not CommandStatus.WAITING_INTERVENTION:
            raise WorkerJobControlError("COMMAND_STATE_CONFLICT")

    async def _set_command_waiting_execution(
        self, unit_of_work: UnitOfWork, job: WorkerJob, now: datetime
    ) -> None:
        command = await self._command_for_job(unit_of_work, job)
        if command is None:
            return
        if command.status is CommandStatus.WAITING_INTERVENTION:
            command.transition(CommandStatus.WAITING_EXECUTION, now)
            await unit_of_work.commands.update(command)
        elif command.status is not CommandStatus.WAITING_EXECUTION:
            raise WorkerJobControlError("COMMAND_STATE_CONFLICT")

    async def _finalize_command(
        self,
        unit_of_work: UnitOfWork,
        job: WorkerJob,
        target: CommandStatus,
        now: datetime,
        *,
        result: dict[str, object] | None = None,
        error_code: str | None = None,
    ) -> None:
        command = await self._command_for_job(unit_of_work, job)
        if command is None:
            return
        if command.status not in {
            CommandStatus.WAITING_EXECUTION,
            CommandStatus.WAITING_INTERVENTION,
        }:
            raise WorkerJobControlError("COMMAND_STATE_CONFLICT")
        command.transition(target, now, error_code=error_code, result=result)
        await unit_of_work.commands.update(command)
        await enqueue_command_result(
            unit_of_work,
            command,
            now,
            delivery_lifetime=self._result_delivery_lifetime,
            crm_destination=self._crm_destination,
        )

    @staticmethod
    async def _command_for_job(unit_of_work: UnitOfWork, job: WorkerJob) -> Command | None:
        if job.command_id is None:
            return None
        command = await unit_of_work.commands.get_by_command_id_for_update(job.command_id)
        if command is None:
            raise WorkerJobControlError("WORKER_JOB_COMMAND_NOT_FOUND")
        return command
