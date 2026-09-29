from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now

MIN_CONVERSATION_SYNC_INTERVAL_SECONDS = 900
MAX_CONVERSATION_SYNC_INTERVAL_SECONDS = 2_592_000
MAX_CONVERSATION_SYNC_ROOT_ID_LENGTH = 255


class ConversationSyncKind(StrEnum):
    CONVERSATION = "conversation"
    REPLIES = "replies"


class ConversationSyncScheduleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


@dataclass(slots=True)
class ConversationSyncSchedule:
    account_id: UUID
    threads_post_id: str
    sync_kind: ConversationSyncKind
    anchor_at: datetime
    interval_seconds: int
    next_due_at: datetime
    created_at: datetime = field(default_factory=utc_now)
    id: UUID = field(default_factory=uuid4)
    status: ConversationSyncScheduleStatus = ConversationSyncScheduleStatus.ACTIVE
    status_reason: str | None = None
    revision: int = 1
    last_dispatched_due_at: datetime | None = None
    last_command_id: str | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.threads_post_id.strip():
            raise ValueError("threads_post_id must not be empty")
        if len(self.threads_post_id) > MAX_CONVERSATION_SYNC_ROOT_ID_LENGTH:
            raise ValueError("threads_post_id exceeds the supported bound")
        self.sync_kind = ConversationSyncKind(self.sync_kind)
        self.status = ConversationSyncScheduleStatus(self.status)
        if (
            type(self.interval_seconds) is not int
            or not MIN_CONVERSATION_SYNC_INTERVAL_SECONDS
            <= self.interval_seconds
            <= MAX_CONVERSATION_SYNC_INTERVAL_SECONDS
        ):
            raise ValueError(
                "conversation sync interval must be between "
                f"{MIN_CONVERSATION_SYNC_INTERVAL_SECONDS} and "
                f"{MAX_CONVERSATION_SYNC_INTERVAL_SECONDS} seconds"
            )
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("conversation sync schedule revision must be positive")

        self.anchor_at = normalize_utc(self.anchor_at)
        self.next_due_at = normalize_utc(self.next_due_at)
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)
        self.last_dispatched_due_at = (
            normalize_utc(self.last_dispatched_due_at)
            if self.last_dispatched_due_at is not None
            else None
        )

        interval = timedelta(seconds=self.interval_seconds)
        if self.next_due_at <= self.anchor_at or (self.next_due_at - self.anchor_at) % interval:
            raise ValueError("next_due_at must be an interval slot after anchor_at")
        if self.updated_at < self.created_at:
            raise ValueError("conversation sync updated_at cannot precede created_at")
        if self.status is ConversationSyncScheduleStatus.ACTIVE:
            if self.status_reason is not None:
                raise ValueError("active conversation sync schedule cannot have a status reason")
        elif self.status_reason is None or not self.status_reason.strip():
            raise ValueError("paused or disabled conversation sync schedule requires a reason")
        elif len(self.status_reason) > 240:
            raise ValueError("conversation sync status reason exceeds the supported bound")
        if (self.last_dispatched_due_at is None) != (self.last_command_id is None):
            raise ValueError("last dispatched due time and command id must be set together")
        if self.last_command_id is not None and (
            not self.last_command_id.strip() or len(self.last_command_id) > 255
        ):
            raise ValueError("last conversation sync command id is invalid")
        if self.last_dispatched_due_at is not None:
            if self.last_dispatched_due_at >= self.next_due_at:
                raise ValueError("next_due_at must follow the last dispatched due time")
            if (self.last_dispatched_due_at - self.anchor_at) % interval:
                raise ValueError("last_dispatched_due_at must be an interval slot")

    def transition(
        self,
        target: ConversationSyncScheduleStatus,
        at: datetime,
        *,
        reason: str,
    ) -> None:
        target = ConversationSyncScheduleStatus(target)
        allowed = {
            (ConversationSyncScheduleStatus.ACTIVE, ConversationSyncScheduleStatus.PAUSED),
            (ConversationSyncScheduleStatus.ACTIVE, ConversationSyncScheduleStatus.DISABLED),
            (ConversationSyncScheduleStatus.PAUSED, ConversationSyncScheduleStatus.ACTIVE),
            (ConversationSyncScheduleStatus.PAUSED, ConversationSyncScheduleStatus.DISABLED),
        }
        if (self.status, target) not in allowed:
            raise ValueError(
                f"invalid conversation sync schedule transition: {self.status} -> {target}"
            )
        occurred_at = normalize_utc(at)
        if occurred_at < self.updated_at:
            raise ValueError("conversation sync schedule transition time cannot move backwards")
        if target is ConversationSyncScheduleStatus.ACTIVE:
            self.status_reason = None
        else:
            bounded_reason = reason.strip()
            if not bounded_reason or len(bounded_reason) > 240:
                raise ValueError("non-active conversation sync schedule requires a bounded reason")
            self.status_reason = bounded_reason
        self.status = target
        self.revision += 1
        self.updated_at = occurred_at

    def dispatch(self, command_id: str, now: datetime) -> ConversationSyncDispatch:
        occurred_at = normalize_utc(now)
        due_at = self.next_due_at
        if self.status is not ConversationSyncScheduleStatus.ACTIVE:
            raise ValueError("only active conversation sync schedules can dispatch")
        if due_at > occurred_at:
            raise ValueError("conversation sync schedule is not due")
        if not command_id.strip() or len(command_id) > 255:
            raise ValueError("conversation sync command id is invalid")
        if self.updated_at > occurred_at:
            raise ValueError("conversation sync dispatch time cannot move backwards")

        dispatch = ConversationSyncDispatch(
            schedule_id=self.id,
            schedule_revision=self.revision,
            due_at=due_at,
            command_id=command_id,
            created_at=occurred_at,
        )
        elapsed_intervals = int((occurred_at - due_at) // timedelta(seconds=self.interval_seconds))
        self.next_due_at = due_at + timedelta(
            seconds=(elapsed_intervals + 1) * self.interval_seconds
        )
        self.last_dispatched_due_at = due_at
        self.last_command_id = command_id
        self.updated_at = occurred_at
        return dispatch


@dataclass(frozen=True, slots=True)
class ConversationSyncDispatch:
    schedule_id: UUID
    schedule_revision: int
    due_at: datetime
    command_id: str
    created_at: datetime
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.schedule_revision < 1:
            raise ValueError("conversation sync dispatch schedule revision must be positive")
        if not self.command_id.strip() or len(self.command_id) > 255:
            raise ValueError("conversation sync dispatch command id is invalid")
        object.__setattr__(self, "due_at", normalize_utc(self.due_at))
        object.__setattr__(self, "created_at", normalize_utc(self.created_at))
