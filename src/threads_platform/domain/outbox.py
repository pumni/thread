from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED_FINAL = "FAILED_FINAL"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


@dataclass(slots=True)
class OutboxEvent:
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, object]
    id: UUID = field(default_factory=uuid4)
    correlation_id: str | None = None
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = 0
    created_at: datetime = field(default_factory=utc_now)
    available_at: datetime = field(default_factory=utc_now)
    delivered_at: datetime | None = None

    def __post_init__(self) -> None:
        if not all(
            (self.aggregate_type.strip(), self.aggregate_id.strip(), self.event_type.strip())
        ):
            raise ValueError("outbox identity fields must not be empty")
        if self.attempt_count < 0:
            raise ValueError("attempt_count must not be negative")
        self.created_at = normalize_utc(self.created_at)
        self.available_at = normalize_utc(self.available_at)
        self.delivered_at = normalize_utc(self.delivered_at) if self.delivered_at else None


@dataclass(slots=True)
class IntegrationDelivery:
    event_id: UUID
    destination: str
    id: UUID = field(default_factory=uuid4)
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempt_count: int = 0
    next_attempt_at: datetime = field(default_factory=utc_now)
    delivered_at: datetime | None = None
    remote_delivery_id: str | None = None
    error_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.destination.strip() or self.attempt_count < 0:
            raise ValueError("delivery destination and attempt count are invalid")
        self.next_attempt_at = normalize_utc(self.next_attempt_at)
        self.delivered_at = normalize_utc(self.delivered_at) if self.delivered_at else None
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)
