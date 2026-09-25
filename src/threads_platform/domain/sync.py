from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now


class SyncRunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


@dataclass(slots=True)
class SyncState:
    account_id: UUID
    sync_type: str
    id: UUID = field(default_factory=uuid4)
    cursor: str | None = None
    last_synced_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.sync_type.strip():
            raise ValueError("sync_type must not be empty")
        self.last_synced_at = (
            normalize_utc(self.last_synced_at) if self.last_synced_at is not None else None
        )
        self.updated_at = normalize_utc(self.updated_at)


@dataclass(slots=True)
class SyncRun:
    account_id: UUID
    sync_type: str
    id: UUID = field(default_factory=uuid4)
    status: SyncRunStatus = SyncRunStatus.RUNNING
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    cursor_from: str | None = None
    cursor_to: str | None = None
    records_processed: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        self.started_at = normalize_utc(self.started_at)
        self.finished_at = normalize_utc(self.finished_at) if self.finished_at else None
        if self.records_processed < 0:
            raise ValueError("records_processed must not be negative")
