from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now


class CommandStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    PROCESSING = "PROCESSING"
    WAITING_EXECUTION = "WAITING_EXECUTION"
    WAITING_INTERVENTION = "WAITING_INTERVENTION"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


class AttemptStatus(StrEnum):
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


_VALID_TRANSITIONS = frozenset(
    {
        (CommandStatus.RECEIVED, CommandStatus.VALIDATED),
        (CommandStatus.RECEIVED, CommandStatus.REJECTED),
        (CommandStatus.RECEIVED, CommandStatus.EXPIRED),
        (CommandStatus.VALIDATED, CommandStatus.PROCESSING),
        (CommandStatus.VALIDATED, CommandStatus.WAITING_EXECUTION),
        (CommandStatus.VALIDATED, CommandStatus.WAITING_INTERVENTION),
        (CommandStatus.VALIDATED, CommandStatus.REJECTED),
        (CommandStatus.VALIDATED, CommandStatus.EXPIRED),
        (CommandStatus.PROCESSING, CommandStatus.SUCCEEDED),
        (CommandStatus.PROCESSING, CommandStatus.FAILED_RETRYABLE),
        (CommandStatus.PROCESSING, CommandStatus.FAILED_FINAL),
        (CommandStatus.PROCESSING, CommandStatus.WAITING_EXECUTION),
        (CommandStatus.PROCESSING, CommandStatus.WAITING_INTERVENTION),
        (CommandStatus.PROCESSING, CommandStatus.EXPIRED),
        (CommandStatus.WAITING_EXECUTION, CommandStatus.WAITING_INTERVENTION),
        (CommandStatus.WAITING_EXECUTION, CommandStatus.PROCESSING),
        (CommandStatus.WAITING_EXECUTION, CommandStatus.SUCCEEDED),
        (CommandStatus.WAITING_EXECUTION, CommandStatus.FAILED_FINAL),
        (CommandStatus.WAITING_EXECUTION, CommandStatus.EXPIRED),
        (CommandStatus.WAITING_INTERVENTION, CommandStatus.WAITING_EXECUTION),
        (CommandStatus.WAITING_INTERVENTION, CommandStatus.FAILED_FINAL),
        (CommandStatus.WAITING_INTERVENTION, CommandStatus.EXPIRED),
        (CommandStatus.FAILED_RETRYABLE, CommandStatus.PROCESSING),
        (CommandStatus.FAILED_RETRYABLE, CommandStatus.WAITING_EXECUTION),
        (CommandStatus.FAILED_RETRYABLE, CommandStatus.WAITING_INTERVENTION),
        (CommandStatus.FAILED_RETRYABLE, CommandStatus.EXPIRED),
        (CommandStatus.FAILED_RETRYABLE, CommandStatus.FAILED_FINAL),
    }
)


@dataclass(slots=True)
class Command:
    command_id: str
    correlation_id: str
    account_id: UUID
    command_type: str
    payload: dict[str, object]
    id: UUID = field(default_factory=uuid4)
    protocol_version: int = 1
    status: CommandStatus = CommandStatus.RECEIVED
    created_at: datetime = field(default_factory=utc_now)
    received_at: datetime = field(default_factory=utc_now)
    deadline_at: datetime | None = None
    next_retry_at: datetime | None = None
    validated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, object] | None = None
    error_code: str | None = None
    execution_lease_token: UUID | None = None
    execution_lease_expires_at: datetime | None = None
    checkpoint: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.command_id.strip() or not self.correlation_id.strip():
            raise ValueError("command_id and correlation_id must not be empty")
        if not self.command_type.strip() or self.protocol_version < 1:
            raise ValueError("command type and protocol version are invalid")
        self.created_at = normalize_utc(self.created_at)
        self.received_at = normalize_utc(self.received_at)
        self.deadline_at = normalize_utc(self.deadline_at) if self.deadline_at is not None else None
        self.next_retry_at = (
            normalize_utc(self.next_retry_at) if self.next_retry_at is not None else None
        )
        self.validated_at = (
            normalize_utc(self.validated_at) if self.validated_at is not None else None
        )
        self.started_at = normalize_utc(self.started_at) if self.started_at is not None else None
        self.completed_at = (
            normalize_utc(self.completed_at) if self.completed_at is not None else None
        )
        self.execution_lease_expires_at = (
            normalize_utc(self.execution_lease_expires_at)
            if self.execution_lease_expires_at is not None
            else None
        )
        if (self.execution_lease_token is None) != (self.execution_lease_expires_at is None):
            raise ValueError("execution lease token and expiry must be set together")

    def transition(
        self,
        target: CommandStatus,
        at: datetime,
        *,
        error_code: str | None = None,
        result: dict[str, object] | None = None,
        next_retry_at: datetime | None = None,
    ) -> None:
        occurred_at = normalize_utc(at)
        if (self.status, target) not in _VALID_TRANSITIONS:
            raise ValueError(f"invalid command transition: {self.status} -> {target}")
        if (
            target == CommandStatus.PROCESSING
            and self.deadline_at
            and occurred_at >= self.deadline_at
        ):
            raise ValueError("expired command cannot enter processing")
        retry_at = normalize_utc(next_retry_at) if next_retry_at is not None else None
        if target == CommandStatus.FAILED_RETRYABLE:
            if retry_at is None or retry_at <= occurred_at:
                raise ValueError("retryable failure requires a future retry time")
            if self.deadline_at is not None and retry_at >= self.deadline_at:
                raise ValueError("retry time must be before the command deadline")

        self.status = target
        self.error_code = error_code
        self.next_retry_at = retry_at
        if target == CommandStatus.VALIDATED:
            self.validated_at = occurred_at
        elif target == CommandStatus.PROCESSING:
            self.started_at = occurred_at
            self.next_retry_at = None
        elif target in {
            CommandStatus.SUCCEEDED,
            CommandStatus.REJECTED,
            CommandStatus.EXPIRED,
            CommandStatus.FAILED_FINAL,
        }:
            self.completed_at = occurred_at
        if target == CommandStatus.SUCCEEDED:
            self.result = result or {}

    def reclaim(self, at: datetime) -> None:
        occurred_at = normalize_utc(at)
        if self.status != CommandStatus.PROCESSING:
            raise ValueError("only an expired processing command can be reclaimed")
        if self.deadline_at is not None and occurred_at >= self.deadline_at:
            raise ValueError("expired command cannot be reclaimed")
        self.started_at = occurred_at
        self.error_code = None
        self.next_retry_at = None


@dataclass(slots=True)
class CommandAttempt:
    command_id: str
    attempt_number: int
    id: UUID = field(default_factory=uuid4)
    status: AttemptStatus = AttemptStatus.PROCESSING
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    error_code: str | None = None

    _allowed_statuses: ClassVar[frozenset[AttemptStatus]] = frozenset(AttemptStatus)

    def __post_init__(self) -> None:
        if not self.command_id.strip() or self.attempt_number < 1:
            raise ValueError("attempt requires a command id and positive attempt number")
        if self.status not in self._allowed_statuses:
            raise ValueError("invalid attempt status")
        self.started_at = normalize_utc(self.started_at)
        self.finished_at = normalize_utc(self.finished_at) if self.finished_at else None
