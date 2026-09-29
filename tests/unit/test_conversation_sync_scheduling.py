from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from threads_platform.domain.conversation_sync import (
    ConversationSyncKind,
    ConversationSyncSchedule,
    ConversationSyncScheduleStatus,
)


def _schedule(
    *,
    interval_seconds: int = 3_600,
    threads_post_id: str = "root-post",
) -> ConversationSyncSchedule:
    anchor = datetime(2026, 9, 29, 10, tzinfo=UTC)
    return ConversationSyncSchedule(
        account_id=uuid4(),
        threads_post_id=threads_post_id,
        sync_kind=ConversationSyncKind.CONVERSATION,
        anchor_at=anchor,
        interval_seconds=interval_seconds,
        next_due_at=anchor + timedelta(seconds=interval_seconds),
        created_at=anchor,
        updated_at=anchor,
    )


@pytest.mark.parametrize("interval_seconds", [900, 2_592_000])
def test_conversation_sync_schedule_accepts_interval_bounds(interval_seconds: int) -> None:
    assert _schedule(interval_seconds=interval_seconds).interval_seconds == interval_seconds


@pytest.mark.parametrize("interval_seconds", [899, 2_592_001, True, 900.0])
def test_conversation_sync_schedule_rejects_invalid_interval(interval_seconds: int) -> None:
    with pytest.raises(ValueError, match="interval"):
        _schedule(interval_seconds=interval_seconds)


@pytest.mark.parametrize("threads_post_id", ["", "   ", "x" * 256])
def test_conversation_sync_schedule_rejects_invalid_root_id(threads_post_id: str) -> None:
    with pytest.raises(ValueError, match="threads_post_id"):
        _schedule(threads_post_id=threads_post_id)


def test_conversation_sync_schedule_rejects_unaligned_due_cursor() -> None:
    anchor = datetime(2026, 9, 29, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="interval slot"):
        ConversationSyncSchedule(
            account_id=uuid4(),
            threads_post_id="root-post",
            sync_kind=ConversationSyncKind.REPLIES,
            anchor_at=anchor,
            interval_seconds=900,
            next_due_at=anchor + timedelta(minutes=16),
            created_at=anchor,
            updated_at=anchor,
        )


def test_pause_resume_changes_control_revision_without_moving_due_cursor() -> None:
    schedule = _schedule()
    due_before_pause = schedule.next_due_at
    at = due_before_pause - timedelta(minutes=1)

    schedule.transition(ConversationSyncScheduleStatus.PAUSED, at, reason="operator pause")
    assert schedule.status is ConversationSyncScheduleStatus.PAUSED
    assert schedule.status_reason == "operator pause"
    assert schedule.revision == 2
    assert schedule.next_due_at == due_before_pause

    schedule.transition(ConversationSyncScheduleStatus.ACTIVE, at, reason="")
    assert schedule.status is ConversationSyncScheduleStatus.ACTIVE
    assert schedule.status_reason is None
    assert schedule.revision == 3
    assert schedule.next_due_at == due_before_pause

    schedule.transition(ConversationSyncScheduleStatus.DISABLED, at, reason="operator disable")
    with pytest.raises(ValueError, match="transition"):
        schedule.transition(ConversationSyncScheduleStatus.ACTIVE, at, reason="")


def test_dispatch_coalesces_outage_to_first_interval_after_now() -> None:
    schedule = _schedule()
    first_due = schedule.next_due_at
    now = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)

    dispatch = schedule.dispatch("conversation-sync-test", now)

    assert dispatch.due_at == first_due
    assert dispatch.schedule_revision == 1
    assert schedule.last_dispatched_due_at == first_due
    assert schedule.last_command_id == "conversation-sync-test"
    assert schedule.next_due_at == datetime(2026, 9, 29, 15, tzinfo=UTC)


def test_dispatch_requires_active_due_schedule() -> None:
    schedule = _schedule()
    due_at = schedule.next_due_at
    schedule.transition(
        ConversationSyncScheduleStatus.PAUSED,
        due_at - timedelta(minutes=1),
        reason="operator pause",
    )

    with pytest.raises(ValueError, match="active"):
        schedule.dispatch("conversation-sync-test", due_at)
