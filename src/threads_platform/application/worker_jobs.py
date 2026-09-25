from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from threads_platform.application.capability_router import CapabilityRouter
from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.commands.results import enqueue_command_result
from threads_platform.application.errors import CommandNotFound
from threads_platform.application.ports.repositories import UnitOfWork, UnitOfWorkFactory
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.application.worker_protocol import is_worker_protocol_supported
from threads_platform.domain.account_execution import AccountExecutionOwnerType
from threads_platform.domain.capabilities import CapabilityExecutor, OperationClass
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.time import normalize_utc
from threads_platform.domain.worker_jobs import (
    WorkerIntervention,
    WorkerInterventionStatus,
    WorkerJob,
    WorkerJobAttempt,
    WorkerJobAttemptStatus,
    WorkerJobRetrySafety,
    WorkerJobStatus,
)
from threads_platform.domain.workers import WorkerNode, WorkerStatus


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
                job = await unit_of_work.worker_jobs.claim(
                    candidate,
                    worker_id,
                    now,
                    lease_expires_at,
                    token,
                    coordination_generation,
                )
                if job is not None:
                    await unit_of_work.worker_job_attempts.add(
                        WorkerJobAttempt(
                            worker_job_id=job.id,
                            attempt_number=job.attempt_count,
                            worker_id=worker_id,
                            lease_token=token,
                            started_at=now,
                        )
                    )
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
        return WorkerJobReconciliation(tuple(jobs))

    async def recover_expired(self, limit: int = 50) -> int:
        now = normalize_utc(self._clock.now())
        recovered = 0
        async with self._unit_of_work_factory() as unit_of_work:
            jobs = await unit_of_work.worker_jobs.list_expired_for_update(now, limit)
            for job in jobs:
                generation = job.account_coordination_generation
                attempt = await unit_of_work.worker_job_attempts.get_running_for_update(job.id)
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
