from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.worker_jobs import (
    WorkerJob,
    WorkerJobAttemptStatus,
    WorkerJobCancelRequest,
    WorkerJobCancelRequestStatus,
    WorkerJobStatus,
)


def test_cancelled_command_is_an_explicit_terminal_outcome() -> None:
    now = datetime(2026, 9, 29, tzinfo=UTC)
    account = ThreadsAccount(threads_user_id="cancel-command-account", username="cancel")
    command = Command(
        command_id="activity:test",
        correlation_id="activity-correlation:test",
        account_id=account.id,
        command_type="threads.browser.feed.browse",
        payload={},
        created_at=now,
        received_at=now,
    )
    command.transition(CommandStatus.VALIDATED, now)
    command.transition(CommandStatus.WAITING_EXECUTION, now)
    command.transition(CommandStatus.CANCELLED, now, error_code="OPERATOR_REQUESTED")

    assert command.status is CommandStatus.CANCELLED
    assert command.completed_at == now
    assert command.error_code == "OPERATOR_REQUESTED"
    with pytest.raises(ValueError, match="invalid command transition"):
        command.transition(CommandStatus.SUCCEEDED, now)


def test_worker_job_cancel_requires_preemptible_current_live_lease() -> None:
    now = datetime(2026, 9, 29, tzinfo=UTC)
    worker_id = uuid4()
    token = uuid4()

    nonpreemptible = WorkerJob(
        capability_name="threads.browser.feed.browse",
        capability_version=1,
        preemptible=False,
        scheduled_at=now,
        created_at=now,
        updated_at=now,
    )
    nonpreemptible.claim(worker_id, now, now + timedelta(seconds=5), token)
    assert not nonpreemptible.cancel(worker_id, token, now, "OPERATOR_REQUESTED")
    assert nonpreemptible.status is WorkerJobStatus.RUNNING
    assert nonpreemptible.lease_token == token

    preemptible = WorkerJob(
        capability_name="threads.browser.feed.browse",
        capability_version=1,
        preemptible=True,
        scheduled_at=now,
        created_at=now,
        updated_at=now,
    )
    preemptible.claim(worker_id, now, now + timedelta(seconds=5), token)
    assert not preemptible.cancel(
        worker_id, token, now + timedelta(seconds=5), "OPERATOR_REQUESTED"
    )
    assert preemptible.cancel(worker_id, token, now, "OPERATOR_REQUESTED")
    assert preemptible.status is WorkerJobStatus.CANCELLED
    assert preemptible.lease_token is None
    assert preemptible.completed_at == now
    assert WorkerJobAttemptStatus.CANCELLED.value == "CANCELLED"


def test_cancel_request_acknowledgement_and_supersession_are_mutually_exclusive() -> None:
    now = datetime(2026, 9, 29, tzinfo=UTC)
    acknowledged = WorkerJobCancelRequest(
        worker_job_id=uuid4(),
        generation=1,
        target_attempt_id=uuid4(),
        target_attempt_number=1,
        reason_code="OPERATOR_REQUESTED",
        requested_at=now,
    )
    acknowledged.acknowledge(now, "BEFORE_NAVIGATION")
    assert acknowledged.status is WorkerJobCancelRequestStatus.ACKNOWLEDGED
    assert acknowledged.safe_checkpoint == "BEFORE_NAVIGATION"
    with pytest.raises(ValueError, match="only a pending cancel request"):
        acknowledged.supersede(now, "TARGET_ATTEMPT_ENDED")

    superseded = WorkerJobCancelRequest(
        worker_job_id=uuid4(),
        generation=1,
        target_attempt_id=uuid4(),
        target_attempt_number=1,
        reason_code="OPERATOR_REQUESTED",
        requested_at=now,
    )
    superseded.supersede(now, "TARGET_ATTEMPT_ENDED")
    assert superseded.status is WorkerJobCancelRequestStatus.SUPERSEDED
    assert superseded.superseded_reason == "TARGET_ATTEMPT_ENDED"
    with pytest.raises(ValueError, match="only a pending cancel request"):
        superseded.acknowledge(now, "BEFORE_NAVIGATION")
