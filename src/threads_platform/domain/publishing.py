from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now


def _empty_metadata() -> dict[str, object]:
    return {}


class ScheduleStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass(slots=True)
class ThreadPost:
    account_id: UUID
    threads_post_id: str
    id: UUID = field(default_factory=uuid4)
    text: str | None = None
    permalink: str | None = None
    published_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, object] = field(default_factory=_empty_metadata)

    def __post_init__(self) -> None:
        if not self.threads_post_id.strip():
            raise ValueError("threads_post_id must not be empty")
        self.published_at = normalize_utc(self.published_at) if self.published_at else None
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)


@dataclass(slots=True)
class ThreadReply:
    account_id: UUID
    threads_reply_id: str
    root_post_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    parent_reply_id: UUID | None = None
    discovered_thread_id: UUID | None = None
    text: str | None = None
    replied_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.threads_reply_id.strip():
            raise ValueError("threads_reply_id must not be empty")
        if (self.root_post_id is None) == (self.discovered_thread_id is None):
            raise ValueError("reply must reference exactly one post or discovered-thread root")
        self.replied_at = normalize_utc(self.replied_at) if self.replied_at else None
        self.created_at = normalize_utc(self.created_at)


@dataclass(slots=True)
class Schedule:
    account_id: UUID
    scheduled_at: datetime
    payload: dict[str, object]
    id: UUID = field(default_factory=uuid4)
    command_id: str | None = None
    status: ScheduleStatus = ScheduleStatus.PENDING
    deadline_at: datetime | None = None
    attempts: int = 0
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.scheduled_at = normalize_utc(self.scheduled_at)
        self.deadline_at = normalize_utc(self.deadline_at) if self.deadline_at else None
        self.created_at = normalize_utc(self.created_at)
        if self.attempts < 0:
            raise ValueError("attempts must not be negative")
