import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from threads_platform.domain.capabilities import OperationClass
from threads_platform.domain.time import normalize_utc, utc_now

MAX_WORKER_JOB_DOCUMENT_BYTES = 64 * 1024


class WorkerJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_INTERVENTION = "WAITING_INTERVENTION"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class WorkerJobRetrySafety(StrEnum):
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class WorkerJobAttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    ABANDONED = "ABANDONED"
    WAITING_INTERVENTION = "WAITING_INTERVENTION"
    CANCELLED = "CANCELLED"


class WorkerJobCancelRequestStatus(StrEnum):
    PENDING = "PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SUPERSEDED = "SUPERSEDED"


class WorkerJobPreemptionStatus(StrEnum):
    WAITING_FOR_QUIESCENCE = "WAITING_FOR_QUIESCENCE"
    SATISFIED = "SATISFIED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(slots=True)
class WorkerJobPreemption:
    account_id: UUID
    preemptor_worker_job_id: UUID
    victim_worker_job_id: UUID
    created_at: datetime
    id: UUID = field(default_factory=uuid4)
    status: WorkerJobPreemptionStatus = WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE
    resolved_at: datetime | None = None
    resolution_reason: str | None = None
    cancel_request_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.preemptor_worker_job_id == self.victim_worker_job_id:
            raise ValueError("WorkerJob cannot preempt itself")
        self.status = WorkerJobPreemptionStatus(self.status)
        self.created_at = normalize_utc(self.created_at)
        self.resolved_at = normalize_utc(self.resolved_at) if self.resolved_at else None
        if self.status is WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE:
            if self.resolved_at is not None or self.resolution_reason is not None:
                raise ValueError("waiting preemption cannot have a resolution")
        elif (
            self.resolved_at is None
            or self.resolution_reason is None
            or re.fullmatch(r"[A-Z0-9_]{1,120}", self.resolution_reason) is None
        ):
            raise ValueError("resolved preemption requires a bounded reason and timestamp")

    def satisfy(self, at: datetime, reason: str) -> None:
        self._resolve(WorkerJobPreemptionStatus.SATISFIED, at, reason)

    def supersede(self, at: datetime, reason: str) -> None:
        self._resolve(WorkerJobPreemptionStatus.SUPERSEDED, at, reason)

    def _resolve(self, status: WorkerJobPreemptionStatus, at: datetime, reason: str) -> None:
        if self.status is not WorkerJobPreemptionStatus.WAITING_FOR_QUIESCENCE:
            raise ValueError("only a waiting preemption can be resolved")
        if re.fullmatch(r"[A-Z0-9_]{1,120}", reason) is None:
            raise ValueError("preemption resolution reason must be a bounded code")
        self.status = status
        self.resolved_at = normalize_utc(at)
        self.resolution_reason = reason


@dataclass(slots=True)
class WorkerJobCancelRequest:
    worker_job_id: UUID
    generation: int
    target_attempt_id: UUID
    target_attempt_number: int
    reason_code: str
    requested_at: datetime
    id: UUID = field(default_factory=uuid4)
    status: WorkerJobCancelRequestStatus = WorkerJobCancelRequestStatus.PENDING
    acknowledged_at: datetime | None = None
    safe_checkpoint: str | None = None
    superseded_at: datetime | None = None
    superseded_reason: str | None = None

    def __post_init__(self) -> None:
        if self.generation < 1 or self.target_attempt_number < 1:
            raise ValueError("cancel request generation and target attempt must be positive")
        if re.fullmatch(r"[A-Z0-9_]{1,120}", self.reason_code) is None:
            raise ValueError("cancel request reason must be a bounded code")
        self.requested_at = normalize_utc(self.requested_at)
        self.acknowledged_at = normalize_utc(self.acknowledged_at) if self.acknowledged_at else None
        self.superseded_at = normalize_utc(self.superseded_at) if self.superseded_at else None
        self.status = WorkerJobCancelRequestStatus(self.status)
        if self.status is WorkerJobCancelRequestStatus.PENDING:
            if any(
                value is not None
                for value in (
                    self.acknowledged_at,
                    self.safe_checkpoint,
                    self.superseded_at,
                    self.superseded_reason,
                )
            ):
                raise ValueError("pending cancel request cannot have a terminal outcome")
        elif self.status is WorkerJobCancelRequestStatus.ACKNOWLEDGED:
            if (
                self.acknowledged_at is None
                or self.safe_checkpoint is None
                or self.superseded_at is not None
                or self.superseded_reason is not None
            ):
                raise ValueError("acknowledged cancel request requires checkpoint and timestamp")
        elif (
            self.acknowledged_at is not None
            or self.safe_checkpoint is not None
            or self.superseded_at is None
            or self.superseded_reason is None
            or re.fullmatch(r"[A-Z0-9_]{1,120}", self.superseded_reason) is None
        ):
            raise ValueError("superseded cancel request requires a bounded reason and timestamp")

    def acknowledge(self, at: datetime, safe_checkpoint: str) -> None:
        if self.status is not WorkerJobCancelRequestStatus.PENDING:
            raise ValueError("only a pending cancel request can be acknowledged")
        if not safe_checkpoint or len(safe_checkpoint) > 80:
            raise ValueError("safe checkpoint must be bounded")
        self.status = WorkerJobCancelRequestStatus.ACKNOWLEDGED
        self.acknowledged_at = normalize_utc(at)
        self.safe_checkpoint = safe_checkpoint

    def supersede(self, at: datetime, reason: str) -> None:
        if self.status is not WorkerJobCancelRequestStatus.PENDING:
            raise ValueError("only a pending cancel request can be superseded")
        if re.fullmatch(r"[A-Z0-9_]{1,120}", reason) is None:
            raise ValueError("superseded reason must be a bounded code")
        self.status = WorkerJobCancelRequestStatus.SUPERSEDED
        self.superseded_at = normalize_utc(at)
        self.superseded_reason = reason


class WorkerInterventionStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class WorkerJob:
    capability_name: str
    capability_version: int
    operation_class: OperationClass = OperationClass.READ
    input_data: dict[str, object] = field(default_factory=lambda: dict[str, object](), repr=False)
    id: UUID = field(default_factory=uuid4)
    command_id: str | None = None
    account_id: UUID | None = None
    assigned_worker_id: UUID | None = None
    account_affinity_required: bool = False
    status: WorkerJobStatus = WorkerJobStatus.QUEUED
    priority: int = 0
    preemptible: bool = False
    scheduled_at: datetime = field(default_factory=utc_now)
    deadline_at: datetime | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    retry_safety: WorkerJobRetrySafety = WorkerJobRetrySafety.SAFE_TO_RETRY
    retry_authorized_by_operator: bool = False
    lease_worker_id: UUID | None = None
    lease_token: UUID | None = field(default=None, repr=False)
    lease_expires_at: datetime | None = None
    account_coordination_generation: int | None = None
    checkpoint: dict[str, object] | None = field(default=None, repr=False)
    result: dict[str, object] | None = field(default=None, repr=False)
    error_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    pending_cancel_request: WorkerJobCancelRequest | None = None

    def __post_init__(self) -> None:
        if not self.capability_name.strip() or self.capability_version < 1:
            raise ValueError("WorkerJob requires a capability and positive version")
        if self.command_id is not None and not self.command_id.strip():
            raise ValueError("command_id must not be empty")
        if self.account_affinity_required and (
            self.account_id is None or self.assigned_worker_id is None
        ):
            raise ValueError("account-affine WorkerJob requires an account and assigned worker")
        if self.max_attempts < 1 or not 0 <= self.attempt_count <= self.max_attempts:
            raise ValueError("WorkerJob attempt count is outside its retry bound")
        self.scheduled_at = normalize_utc(self.scheduled_at)
        self.deadline_at = normalize_utc(self.deadline_at) if self.deadline_at else None
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)
        self.completed_at = normalize_utc(self.completed_at) if self.completed_at else None
        self.lease_expires_at = (
            normalize_utc(self.lease_expires_at) if self.lease_expires_at else None
        )
        if self.deadline_at is not None and self.scheduled_at >= self.deadline_at:
            raise ValueError("WorkerJob deadline must be after scheduled_at")
        lease_values = (self.lease_worker_id, self.lease_token, self.lease_expires_at)
        if any(value is None for value in lease_values) and not all(
            value is None for value in lease_values
        ):
            raise ValueError("WorkerJob lease owner, token, and expiry must be set together")
        if self.status is WorkerJobStatus.RUNNING and self.lease_token is None:
            raise ValueError("RUNNING WorkerJob requires a lease")
        requires_coordination = (
            self.account_id is not None
            and self.operation_class.requires_exclusive_account_coordination
        )
        if (
            self.status is WorkerJobStatus.RUNNING
            and requires_coordination
            and self.account_coordination_generation is None
        ):
            raise ValueError("running exclusive account job requires an account coordination fence")
        if self.account_coordination_generation is not None and (
            self.status is not WorkerJobStatus.RUNNING
            or not requires_coordination
            or self.account_id is None
            or self.account_coordination_generation < 1
        ):
            raise ValueError("account coordination fence requires a running exclusive job")
        _validate_document(self.input_data)
        _validate_document(self.checkpoint)
        _validate_document(self.result)

    def claim(
        self,
        worker_id: UUID,
        now: datetime,
        lease_expires_at: datetime,
        lease_token: UUID,
        *,
        account_coordination_generation: int | None = None,
    ) -> None:
        occurred_at = normalize_utc(now)
        expires_at = normalize_utc(lease_expires_at)
        reclaiming = self.status is WorkerJobStatus.RUNNING
        if self.status not in {
            WorkerJobStatus.QUEUED,
            WorkerJobStatus.FAILED_RETRYABLE,
            WorkerJobStatus.RUNNING,
        }:
            raise ValueError("WorkerJob is not claimable")
        if self.scheduled_at > occurred_at:
            raise ValueError("WorkerJob is not scheduled yet")
        if self.deadline_at is not None and occurred_at >= self.deadline_at:
            raise ValueError("WorkerJob deadline has expired")
        if self.attempt_count >= self.max_attempts:
            raise ValueError("WorkerJob attempt limit has been reached")
        if self.assigned_worker_id is not None and self.assigned_worker_id != worker_id:
            raise ValueError("WorkerJob is assigned to a different worker")
        if reclaiming:
            if self.lease_expires_at is None or self.lease_expires_at > occurred_at:
                raise ValueError("WorkerJob lease has not expired")
            if (
                self.retry_safety is WorkerJobRetrySafety.RECONCILIATION_REQUIRED
                and not self.retry_authorized_by_operator
            ):
                raise ValueError("WorkerJob requires side-effect reconciliation")
        if expires_at <= occurred_at:
            raise ValueError("WorkerJob lease expiry must be in the future")
        if (
            self.operation_class.requires_exclusive_account_coordination
            and self.account_id is not None
        ):
            if account_coordination_generation is None or account_coordination_generation < 1:
                raise ValueError("exclusive account job requires an account coordination fence")
        elif account_coordination_generation is not None:
            raise ValueError("account coordination fence is not valid for this job")
        self.status = WorkerJobStatus.RUNNING
        self.lease_worker_id = worker_id
        self.lease_token = lease_token
        self.lease_expires_at = expires_at
        self.account_coordination_generation = account_coordination_generation
        self.attempt_count += 1
        self.retry_authorized_by_operator = False
        self.error_code = None
        self.updated_at = occurred_at

    def owns_lease(self, worker_id: UUID, lease_token: UUID, now: datetime) -> bool:
        return (
            self.status is WorkerJobStatus.RUNNING
            and self.lease_worker_id == worker_id
            and self.lease_token == lease_token
            and self.lease_expires_at is not None
            and self.lease_expires_at > normalize_utc(now)
        )

    def renew(
        self, worker_id: UUID, lease_token: UUID, now: datetime, expires_at: datetime
    ) -> bool:
        occurred_at = normalize_utc(now)
        if not self.owns_lease(worker_id, lease_token, occurred_at):
            return False
        new_expiry = normalize_utc(expires_at)
        if new_expiry <= occurred_at:
            raise ValueError("renewed lease expiry must be in the future")
        self.lease_expires_at = new_expiry
        self.updated_at = occurred_at
        return True

    def save_checkpoint(
        self,
        worker_id: UUID,
        lease_token: UUID,
        now: datetime,
        checkpoint: dict[str, object],
    ) -> bool:
        if not self.owns_lease(worker_id, lease_token, now):
            return False
        _validate_document(checkpoint)
        self.checkpoint = dict(checkpoint)
        self.updated_at = normalize_utc(now)
        return True

    def complete(
        self, worker_id: UUID, lease_token: UUID, now: datetime, result: dict[str, object]
    ) -> bool:
        occurred_at = normalize_utc(now)
        if not self.owns_lease(worker_id, lease_token, occurred_at):
            return False
        _validate_document(result)
        self.status = WorkerJobStatus.SUCCEEDED
        self.result = dict(result)
        self.completed_at = occurred_at
        self.updated_at = occurred_at
        self._clear_lease()
        return True

    def cancel(self, worker_id: UUID, lease_token: UUID, now: datetime, reason_code: str) -> bool:
        occurred_at = normalize_utc(now)
        if not self.preemptible or not self.owns_lease(worker_id, lease_token, occurred_at):
            return False
        if re.fullmatch(r"[A-Z0-9_]{1,120}", reason_code) is None:
            raise ValueError("WorkerJob cancellation requires a bounded reason code")
        self.status = WorkerJobStatus.CANCELLED
        self.error_code = reason_code
        self.completed_at = occurred_at
        self.updated_at = occurred_at
        self._clear_lease()
        self.pending_cancel_request = None
        return True

    def fail(
        self,
        worker_id: UUID,
        lease_token: UUID,
        now: datetime,
        *,
        error_code: str,
        retryable: bool,
        outcome_ambiguous: bool = False,
        retry_at: datetime | None = None,
    ) -> bool:
        occurred_at = normalize_utc(now)
        if not self.owns_lease(worker_id, lease_token, occurred_at):
            return False
        if not error_code.strip():
            raise ValueError("WorkerJob failure requires an error code")
        self.error_code = error_code
        self.updated_at = occurred_at
        if outcome_ambiguous or (
            self.retry_safety is WorkerJobRetrySafety.RECONCILIATION_REQUIRED and retryable
        ):
            if outcome_ambiguous:
                self.retry_safety = WorkerJobRetrySafety.RECONCILIATION_REQUIRED
            self.status = WorkerJobStatus.WAITING_INTERVENTION
        elif retryable and self.attempt_count < self.max_attempts:
            self.status = WorkerJobStatus.FAILED_RETRYABLE
            self.scheduled_at = normalize_utc(retry_at) if retry_at is not None else occurred_at
        else:
            self.status = WorkerJobStatus.FAILED_FINAL
            self.completed_at = occurred_at
        self._clear_lease()
        return True

    def require_intervention(self, worker_id: UUID, lease_token: UUID, now: datetime) -> bool:
        occurred_at = normalize_utc(now)
        if not self.owns_lease(worker_id, lease_token, occurred_at):
            return False
        self.status = WorkerJobStatus.WAITING_INTERVENTION
        self.updated_at = occurred_at
        self._clear_lease()
        return True

    def suspend_for_intervention(self, now: datetime, error_code: str) -> None:
        occurred_at = normalize_utc(now)
        if self.status is not WorkerJobStatus.RUNNING:
            raise ValueError("only a running WorkerJob can be suspended")
        self.status = WorkerJobStatus.WAITING_INTERVENTION
        self.error_code = error_code
        self.updated_at = occurred_at
        self._clear_lease()

    def resolve_intervention(
        self,
        now: datetime,
        *,
        requeue: bool,
        confirmed_safe_to_retry: bool,
    ) -> None:
        occurred_at = normalize_utc(now)
        if self.status is not WorkerJobStatus.WAITING_INTERVENTION:
            raise ValueError("WorkerJob is not waiting for intervention")
        if not requeue:
            self.status = WorkerJobStatus.FAILED_FINAL
            self.error_code = self.error_code or "INTERVENTION_CLOSED"
            self.completed_at = occurred_at
        else:
            if self.deadline_at is not None and occurred_at >= self.deadline_at:
                raise ValueError("expired WorkerJob cannot be requeued")
            if self.attempt_count >= self.max_attempts:
                raise ValueError("WorkerJob attempt limit has been reached")
            if (
                self.retry_safety is WorkerJobRetrySafety.RECONCILIATION_REQUIRED
                and not confirmed_safe_to_retry
            ):
                raise ValueError("operator must confirm safe retry after reconciliation")
            self.retry_authorized_by_operator = (
                self.retry_safety is WorkerJobRetrySafety.RECONCILIATION_REQUIRED
            )
            self.status = WorkerJobStatus.QUEUED
            self.scheduled_at = occurred_at
            self.error_code = None
        self.updated_at = occurred_at

    def expire(self, now: datetime, error_code: str = "JOB_DEADLINE_EXPIRED") -> None:
        occurred_at = normalize_utc(now)
        self.status = WorkerJobStatus.EXPIRED
        self.error_code = error_code
        self.completed_at = occurred_at
        self.updated_at = occurred_at
        self._clear_lease()

    def finalize_failure(self, now: datetime, error_code: str) -> None:
        occurred_at = normalize_utc(now)
        self.status = WorkerJobStatus.FAILED_FINAL
        self.error_code = error_code
        self.completed_at = occurred_at
        self.updated_at = occurred_at
        self._clear_lease()

    def _clear_lease(self) -> None:
        self.lease_worker_id = None
        self.lease_token = None
        self.lease_expires_at = None
        self.account_coordination_generation = None


@dataclass(slots=True)
class WorkerJobAttempt:
    worker_job_id: UUID
    attempt_number: int
    worker_id: UUID
    lease_token: UUID = field(repr=False)
    id: UUID = field(default_factory=uuid4)
    status: WorkerJobAttemptStatus = WorkerJobAttemptStatus.RUNNING
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.attempt_number < 1:
            raise ValueError("WorkerJobAttempt requires a positive attempt number")
        self.started_at = normalize_utc(self.started_at)
        self.finished_at = normalize_utc(self.finished_at) if self.finished_at else None


@dataclass(slots=True)
class WorkerIntervention:
    worker_job_id: UUID
    account_id: UUID | None
    intervention_type: str
    id: UUID = field(default_factory=uuid4)
    worker_id: UUID | None = None
    status: WorkerInterventionStatus = WorkerInterventionStatus.OPEN
    detail_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    def __post_init__(self) -> None:
        if not self.intervention_type.strip():
            raise ValueError("intervention_type must not be empty")
        self.created_at = normalize_utc(self.created_at)
        self.resolved_at = normalize_utc(self.resolved_at) if self.resolved_at else None


def _validate_document(document: dict[str, object] | None) -> None:
    if document is None:
        return
    try:
        encoded = json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("WorkerJob document must be JSON serializable") from error
    if len(encoded) > MAX_WORKER_JOB_DOCUMENT_BYTES:
        raise ValueError("WorkerJob document exceeds 64 KiB")
    _reject_secret_fields(document)


def _reject_secret_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError("WorkerJob document keys must be strings")
            normalized_key = re.sub(r"[^a-z]", "", key.lower())
            if any(
                term in normalized_key
                for term in (
                    "password",
                    "token",
                    "secret",
                    "credential",
                    "authorization",
                    "privatekey",
                    "proxy",
                )
            ):
                raise ValueError("WorkerJob document cannot contain secret-bearing fields")
            _reject_secret_fields(item)
    elif isinstance(value, list):
        for item in cast(list[object], value):
            _reject_secret_fields(item)
    elif isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https", "socks5", "socks5h"} and (
            parsed.username is not None or parsed.password is not None
        ):
            raise ValueError("WorkerJob document cannot contain URLs with credentials")
