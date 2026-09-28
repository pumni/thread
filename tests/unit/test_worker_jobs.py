from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.worker_jobs import WorkerJob


def test_command_waiting_hooks_remain_separate_from_worker_job_status() -> None:
    now = datetime(2026, 9, 25, 12, tzinfo=UTC)
    command = Command(
        command_id="remote-command",
        correlation_id="remote-correlation",
        account_id=uuid4(),
        command_type="threads.publish_text",
        payload={},
    )
    command.transition(CommandStatus.VALIDATED, now)
    command.transition(CommandStatus.WAITING_EXECUTION, now)
    command.transition(CommandStatus.WAITING_INTERVENTION, now)
    command.transition(CommandStatus.WAITING_EXECUTION, now + timedelta(seconds=1))
    command.transition(CommandStatus.SUCCEEDED, now + timedelta(seconds=2), result={"ok": True})

    assert command.status is CommandStatus.SUCCEEDED
    assert command.result == {"ok": True}


def test_worker_job_documents_reject_secret_fields_and_proxy_credentials() -> None:
    with pytest.raises(ValueError, match="secret-bearing fields"):
        WorkerJob(
            capability_name="synthetic.echo",
            capability_version=1,
            checkpoint={"access_token": "never-store-this"},
        )

    credentialed_url = "".join(("https://user", ":pass", "word@proxy.example.test:8443"))
    with pytest.raises(ValueError, match="URLs with credentials"):
        WorkerJob(
            capability_name="synthetic.echo",
            capability_version=1,
            result={"remote": credentialed_url},
        )


def test_worker_job_claim_and_fencing_reject_old_lease() -> None:
    now = datetime(2026, 9, 25, 12, tzinfo=UTC)
    first_worker = uuid4()
    second_worker = uuid4()
    first_token = uuid4()
    next_token = uuid4()
    job = WorkerJob("synthetic.echo", 1, scheduled_at=now)

    job.claim(first_worker, now, now + timedelta(seconds=5), first_token)
    assert job.owns_lease(first_worker, first_token, now + timedelta(seconds=1))
    job.claim(second_worker, now + timedelta(seconds=6), now + timedelta(seconds=11), next_token)

    assert not job.owns_lease(first_worker, first_token, now + timedelta(seconds=6))
    assert job.owns_lease(second_worker, next_token, now + timedelta(seconds=6))
