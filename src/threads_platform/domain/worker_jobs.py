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


class WorkerInterventionStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class WorkerJob:
    capability_name: str
    capability_version: int
    operation_class: OperationClass = OperationClass.READ
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
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None
    account_coordination_generation: int | None = None
    checkpoint: dict[str, object] | None = None
    result: dict[str, object] | None = None
    error_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None

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
    lease_token: UUID
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
