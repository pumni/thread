from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from threads_platform.app import create_app
from threads_platform.application.worker_control import WorkerControlError, WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobControlError, WorkerJobService
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.config.settings import Settings
from threads_platform.domain.worker_jobs import WorkerJob, WorkerJobAttemptStatus, WorkerJobStatus
from threads_platform.domain.workers import WorkerCapability, WorkerNode, WorkerStatus
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.infrastructure.security.worker_auth import challenge_message
from threads_platform.workers.key_store import WorkerDeviceIdentity

pytestmark = pytest.mark.integration


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 30, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, duration: timedelta) -> None:
        self.current += duration


async def _add_worker(
    factory: SQLAlchemyUnitOfWorkFactory,
    *,
    now: datetime,
    status: WorkerStatus = WorkerStatus.ONLINE,
    active_browser_sessions: int = 0,
) -> UUID:
    worker_id = uuid4()
    worker = WorkerNode(
        worker_id=worker_id,
        display_name=f"Drain test {worker_id}",
        hostname=f"drain-{worker_id}",
        platform="windows",
        agent_version="1.0.0",
        protocol_version=1,
        capabilities_schema_version=1,
        status=status,
        max_concurrent_jobs=2,
        active_browser_sessions=active_browser_sessions,
        last_heartbeat_at=now,
        presence_expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    async with factory() as unit_of_work:
        await unit_of_work.workers.add(worker)
        await unit_of_work.worker_capabilities.replace_for_worker(
            worker_id,
            [WorkerCapability(worker_id, "synthetic.echo", 1, advertised_at=now)],
        )
    return worker_id


async def _authenticated_worker(
    control: WorkerControlService,
) -> tuple[UUID, str]:
    worker_id = uuid4()
    identity = WorkerDeviceIdentity.generate()
    enrollment = await control.create_enrollment()
    await control.enroll(
        enrollment.code,
        worker_id=worker_id,
        display_name="Drain transport worker",
        hostname="SYNTHETIC-HOST",
        platform="windows",
        public_key=identity.public_key_bytes,
    )
    challenge = await control.create_challenge(worker_id)
    signature = identity.sign(challenge_message(challenge.challenge_id, challenge.nonce))
    session = await control.exchange_challenge(challenge.challenge_id, signature)
    await control.hello(
        worker_id,
        protocol_version=1,
        agent_version="1.0.0",
        capabilities_schema_version=1,
        capabilities=[],
        access_token=session.access_token,
    )
    return worker_id, session.access_token


async def test_drain_transitions_are_locked_idempotent_and_audited(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    control = WorkerControlService(unit_of_work_factory, clock=MutableClock())
    for initial in (WorkerStatus.ONLINE, WorkerStatus.DEGRADED):
        worker_id = await _add_worker(unit_of_work_factory, now=now, status=initial)
        assert await control.request_drain(worker_id, "UPDATE_REQUESTED")
        assert not await control.request_drain(worker_id, "UPDATE_REQUESTED")
        status = await control.drain_status(worker_id)
        assert status.status is WorkerStatus.DRAINING
        events = await control.audit_events(worker_id)
        requested = [event for event in events if event.event_type == "worker.drain.requested"]
        assert len(requested) == 1
        assert requested[0].detail_code == "UPDATE_REQUESTED"

    for initial in (
        WorkerStatus.OFFLINE,
        WorkerStatus.REGISTERING,
        WorkerStatus.UPGRADE_REQUIRED,
        WorkerStatus.DISABLED,
    ):
        worker_id = await _add_worker(unit_of_work_factory, now=now, status=initial)
        with pytest.raises(WorkerControlError, match="WORKER_DRAIN_NOT_ALLOWED"):
            await control.request_drain(worker_id, "UPDATE_REQUESTED")
        assert (await control.drain_status(worker_id)).status is initial

    draining_id = await _add_worker(unit_of_work_factory, now=now, status=WorkerStatus.DRAINING)
    assert not await control.request_drain(draining_id, "UPDATE_REQUESTED")
    assert not any(
        event.event_type == "worker.drain.requested"
        for event in await control.audit_events(draining_id)
    )

    online_id = await _add_worker(unit_of_work_factory, now=now)
    await control.request_drain(online_id, "UPDATE_REQUESTED")
    hello = await control.hello(
        online_id,
        protocol_version=1,
        agent_version="1.0.0",
        capabilities_schema_version=1,
        capabilities=[],
    )
    heartbeat = await control.heartbeat(online_id)
    assert hello.status is WorkerStatus.DRAINING
    assert heartbeat.status is WorkerStatus.DRAINING


async def test_running_job_remains_valid_through_drain_and_blocks_completion(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    worker_id = await _add_worker(unit_of_work_factory, now=clock.now())
    jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    control = WorkerControlService(unit_of_work_factory, clock=clock)
    queued = await jobs.enqueue("synthetic.echo", 1)
    claimed = await jobs.claim_next(worker_id)
    assert claimed is not None and claimed.lease_token is not None
    original_lease_token = claimed.lease_token

    assert await control.request_drain(worker_id, "UPDATE_REQUESTED")
    status = await control.drain_status(worker_id)
    assert status.running_worker_jobs == 1
    assert not status.quiescent
    with pytest.raises(WorkerControlError, match="WORKER_DRAIN_NOT_QUIESCENT"):
        await control.complete_drain(worker_id)
    async with unit_of_work_factory() as unit_of_work:
        assert await unit_of_work.worker_job_cancel_requests.latest_generation(queued.id) == 0
        assert await unit_of_work.worker_job_cancel_requests.get_pending_for_job(queued.id) is None
        assert await unit_of_work.worker_job_preemptions.list_waiting_for_preemptor(queued.id) == []
        assert await unit_of_work.worker_job_preemptions.list_waiting_for_victim(queued.id) == []
        assert await unit_of_work.worker_interventions.get_open_for_job(queued.id) is None

    renewed = await jobs.renew(queued.id, worker_id, original_lease_token)
    assert renewed.status is WorkerJobStatus.RUNNING
    with pytest.raises(WorkerControlError, match="WORKER_DRAIN_NOT_QUIESCENT"):
        await control.complete_drain(worker_id)
    running_after_rejected_completion = await jobs.get(queued.id)
    attempts_after_rejected_completion = await jobs.attempts(queued.id)
    assert running_after_rejected_completion is not None
    assert running_after_rejected_completion.status is WorkerJobStatus.RUNNING
    assert running_after_rejected_completion.lease_token == original_lease_token
    assert len(attempts_after_rejected_completion) == 1
    assert attempts_after_rejected_completion[0].status is WorkerJobAttemptStatus.RUNNING
    checkpointed = await jobs.checkpoint(
        queued.id, worker_id, original_lease_token, {"phase": "synthetic-safe-point"}
    )
    assert checkpointed.checkpoint == {"phase": "synthetic-safe-point"}
    completed = await jobs.complete(queued.id, worker_id, original_lease_token, {"ok": True})
    assert completed.status is WorkerJobStatus.SUCCEEDED
    assert (await control.drain_status(worker_id)).quiescent
    job_before_drain_completion = await jobs.get(queued.id)
    attempts_before_drain_completion = await jobs.attempts(queued.id)
    await control.complete_drain(worker_id)
    with pytest.raises(WorkerControlError, match="WORKER_NOT_DRAINING"):
        await control.complete_drain(worker_id)

    async with unit_of_work_factory() as unit_of_work:
        worker = await unit_of_work.workers.get(worker_id)
        stored_job = await unit_of_work.worker_jobs.get(queued.id)
        attempts = await unit_of_work.worker_job_attempts.list_for_job(queued.id)
    assert worker is not None and worker.status is WorkerStatus.OFFLINE
    assert stored_job is not None and stored_job.status is WorkerJobStatus.SUCCEEDED
    assert len(attempts) == 1
    assert attempts[0].status is WorkerJobAttemptStatus.SUCCEEDED
    assert stored_job == job_before_drain_completion
    assert attempts == attempts_before_drain_completion
    assert [event.event_type for event in await control.audit_events(worker_id)].count(
        "worker.drain.completed"
    ) == 1
    hello = await control.hello(
        worker_id,
        protocol_version=1,
        agent_version="1.0.0",
        capabilities_schema_version=1,
        capabilities=[],
    )
    assert hello.status is WorkerStatus.ONLINE


async def test_active_browser_sessions_prevent_quiescence(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    worker_id = await _add_worker(
        unit_of_work_factory,
        now=clock.now(),
        active_browser_sessions=1,
    )
    control = WorkerControlService(unit_of_work_factory, clock=clock)
    await control.request_drain(worker_id, "UPDATE_REQUESTED")
    assert not (await control.drain_status(worker_id)).quiescent
    with pytest.raises(WorkerControlError, match="WORKER_DRAIN_NOT_QUIESCENT"):
        await control.complete_drain(worker_id)
    active_status = await control.drain_status(worker_id)
    assert active_status.status is WorkerStatus.DRAINING
    assert active_status.active_browser_sessions == 1
    assert not any(
        event.event_type == "worker.drain.completed"
        for event in await control.audit_events(worker_id)
    )
    await control.heartbeat(worker_id, active_browser_sessions=0)
    assert (await control.drain_status(worker_id)).quiescent
    await control.complete_drain(worker_id)
    assert (await control.drain_status(worker_id)).status is WorkerStatus.OFFLINE


async def test_expired_running_lease_still_blocks_quiescence(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    worker_id = await _add_worker(unit_of_work_factory, now=clock.now())
    control = WorkerControlService(unit_of_work_factory, clock=clock)
    jobs = WorkerJobService(unit_of_work_factory, clock=clock, lease_duration=timedelta(seconds=5))
    queued = await jobs.enqueue("synthetic.echo", 1)
    claimed = await jobs.claim_next(worker_id)
    assert claimed is not None
    await control.request_drain(worker_id, "UPDATE_REQUESTED")
    clock.advance(timedelta(seconds=6))
    expired_but_running = await control.drain_status(worker_id)
    assert expired_but_running.running_worker_jobs == 1
    assert not expired_but_running.quiescent
    with pytest.raises(WorkerControlError, match="WORKER_DRAIN_NOT_QUIESCENT"):
        await control.complete_drain(worker_id)
    async with unit_of_work_factory() as unit_of_work:
        stored_job = await unit_of_work.worker_jobs.get(queued.id)
    assert stored_job is not None and stored_job.status is WorkerJobStatus.RUNNING


async def test_claim_and_drain_race_is_serialized_by_worker_row_lock(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    worker_id = await _add_worker(unit_of_work_factory, now=clock.now())
    jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    control = WorkerControlService(unit_of_work_factory, clock=clock)
    queued = await jobs.enqueue("synthetic.echo", 1)
    barrier = asyncio.Barrier(2)

    async def claim() -> WorkerJob | str | None:
        await barrier.wait()
        try:
            return await jobs.claim_next(worker_id)
        except WorkerJobControlError as error:
            return error.code

    async def drain() -> bool:
        await barrier.wait()
        return await control.request_drain(worker_id, "UPDATE_REQUESTED")

    claimed, transitioned = await asyncio.gather(claim(), drain())
    assert transitioned
    status = await control.drain_status(worker_id)
    assert status.status is WorkerStatus.DRAINING
    if claimed is None:
        assert status.running_worker_jobs == 0
        assert await jobs.get(queued.id) is not None
    elif isinstance(claimed, str):
        assert claimed == "WORKER_NOT_ELIGIBLE"
        assert status.running_worker_jobs == 0
    else:
        assert status.running_worker_jobs == 1
        assert claimed.id == queued.id
        assert not status.quiescent


async def test_each_commit_order_preserves_claim_and_drain_invariants(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    control = WorkerControlService(unit_of_work_factory, clock=clock)
    jobs = WorkerJobService(unit_of_work_factory, clock=clock)

    drain_first_worker = await _add_worker(unit_of_work_factory, now=clock.now())
    drain_first_job = await jobs.enqueue("synthetic.echo", 1, assigned_worker_id=drain_first_worker)
    await control.request_drain(drain_first_worker, "UPDATE_REQUESTED")
    with pytest.raises(WorkerJobControlError, match="WORKER_NOT_ELIGIBLE"):
        await jobs.claim_next(drain_first_worker)
    assert (await control.drain_status(drain_first_worker)).running_worker_jobs == 0
    drain_first_stored = await jobs.get(drain_first_job.id)
    assert drain_first_stored is not None
    assert drain_first_stored.status is WorkerJobStatus.QUEUED

    claim_first_worker = await _add_worker(unit_of_work_factory, now=clock.now())
    claim_first_job = await jobs.enqueue("synthetic.echo", 1, assigned_worker_id=claim_first_worker)
    claimed = await jobs.claim_next(claim_first_worker)
    assert claimed is not None and claimed.id == claim_first_job.id
    await control.request_drain(claim_first_worker, "UPDATE_REQUESTED")
    claim_first_status = await control.drain_status(claim_first_worker)
    assert claim_first_status.running_worker_jobs == 1
    assert not claim_first_status.quiescent


async def test_abort_is_offline_recovery_and_worker_can_only_complete_itself(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    control = WorkerControlService(unit_of_work_factory, clock=clock)
    worker_id = await _add_worker(unit_of_work_factory, now=clock.now())
    jobs = WorkerJobService(unit_of_work_factory, clock=clock)
    queued = await jobs.enqueue("synthetic.echo", 1)
    claimed = await jobs.claim_next(worker_id)
    assert claimed is not None
    await control.request_drain(worker_id, "UPDATE_REQUESTED")
    job_before_abort = await jobs.get(queued.id)
    attempts_before_abort = await jobs.attempts(queued.id)
    await control.abort_drain(worker_id)
    status_after_abort = await control.drain_status(worker_id)
    assert status_after_abort.status is WorkerStatus.OFFLINE
    assert status_after_abort.running_worker_jobs == 1
    assert not status_after_abort.quiescent
    assert await jobs.get(queued.id) == job_before_abort
    assert await jobs.attempts(queued.id) == attempts_before_abort
    assert any(
        event.event_type == "worker.drain.aborted"
        for event in await control.audit_events(worker_id)
    )
    assert not any(
        event.event_type == "worker.drain.completed"
        for event in await control.audit_events(worker_id)
    )
    assert (await control.heartbeat(worker_id)).status is WorkerStatus.ONLINE


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows intentionally disables the legacy static-admin-token path",
)
async def test_drain_http_auth_boundaries_status_and_advisory(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    admin_token = "SYNTHETIC_WORKER_ADMIN_TOKEN"
    control = WorkerControlService(unit_of_work_factory)
    worker_id, worker_token = await _authenticated_worker(control)
    other_worker_id, other_worker_token = await _authenticated_worker(control)
    notifications = WorkerNotificationHub()
    app = create_app(
        Settings(
            database_url=None,
            worker_admin_token=SecretStr(admin_token),
            worker_admin_auth_profile="legacy_linux_it",
        ),
        worker_control_service=control,
        worker_notifications=notifications,
    )
    transport = httpx2.ASGITransport(app=app)

    async with notifications.subscribe(worker_id) as events:
        async with httpx2.AsyncClient(
            transport=transport, base_url="https://worker.test"
        ) as client:
            denied = await client.post(
                f"/v1/workers/{worker_id}/drain",
                headers={"Authorization": f"Bearer {worker_token}"},
                json={"reason_code": "UPDATE_REQUESTED"},
            )
            assert denied.status_code == 401
            assert worker_token not in denied.text
            assert "SYNTHETIC_WORKER_ADMIN_TOKEN" not in denied.text

            unauthenticated_status = await client.get(f"/v1/workers/{worker_id}/drain")
            assert unauthenticated_status.status_code == 401

            with capture_logs() as lifecycle_logs:
                drained = await client.post(
                    f"/v1/workers/{worker_id}/drain",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"reason_code": "UPDATE_REQUESTED"},
                )
            assert drained.status_code == 200
            assert drained.json()["status"] == "DRAINING"
            assert len(lifecycle_logs) == 1
            assert {
                "event",
                "worker_id",
                "status",
                "reason_code",
                "active_browser_sessions",
                "running_job_count",
            } <= set(lifecycle_logs[0])
            assert not {
                "hostname",
                "account_id",
                "profile_ref",
                "job_id",
                "lease_token",
                "payload",
            } & set(lifecycle_logs[0])
            assert lifecycle_logs[0]["worker_id"] == str(worker_id)
            assert lifecycle_logs[0]["status"] == "DRAINING"
            assert lifecycle_logs[0]["reason_code"] == "UPDATE_REQUESTED"
            assert worker_token not in repr(lifecycle_logs)
            assert await asyncio.wait_for(events.get(), timeout=1) == {
                "type": "worker.drain",
                "reason_code": "UPDATE_REQUESTED",
            }

            repeated = await client.post(
                f"/v1/workers/{worker_id}/drain",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"reason_code": "UPDATE_REQUESTED"},
            )
            assert repeated.status_code == 200
            assert events.empty()

            status = await client.get(
                f"/v1/workers/{worker_id}/drain",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert status.status_code == 200
            assert set(status.json()) == {
                "worker_id",
                "status",
                "active_browser_sessions",
                "running_worker_jobs",
                "quiescent",
            }
            assert status.json()["quiescent"] is True

            admin_cannot_complete = await client.post(
                "/v1/workers/drain/complete",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={},
            )
            assert admin_cannot_complete.status_code == 401

            other_worker_cannot_complete = await client.post(
                "/v1/workers/drain/complete",
                headers={"Authorization": f"Bearer {other_worker_token}"},
                json={},
            )
            assert other_worker_cannot_complete.status_code == 409
            assert other_worker_id != worker_id
            assert (await control.drain_status(worker_id)).status is WorkerStatus.DRAINING

            requested_other_drain = await client.post(
                f"/v1/workers/{other_worker_id}/drain",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"reason_code": "UPDATE_REQUESTED"},
            )
            assert requested_other_drain.status_code == 200
            worker_cannot_abort = await client.post(
                f"/v1/workers/{other_worker_id}/drain/abort",
                headers={"Authorization": f"Bearer {worker_token}"},
            )
            assert worker_cannot_abort.status_code == 401
            unauthenticated_abort = await client.post(
                f"/v1/workers/{other_worker_id}/drain/abort",
            )
            assert unauthenticated_abort.status_code == 401
            aborted_other_drain = await client.post(
                f"/v1/workers/{other_worker_id}/drain/abort",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert aborted_other_drain.status_code == 200
            assert aborted_other_drain.json()["status"] == "OFFLINE"

            completed = await client.post(
                "/v1/workers/drain/complete",
                headers={"Authorization": f"Bearer {worker_token}"},
                json={},
            )
            assert completed.status_code == 200
            assert completed.json() == {"worker_id": str(worker_id), "status": "OFFLINE"}
            repeated_completion = await client.post(
                "/v1/workers/drain/complete",
                headers={"Authorization": f"Bearer {worker_token}"},
                json={},
            )
            assert repeated_completion.status_code == 409
            assert repeated_completion.json() == {"detail": {"code": "WORKER_NOT_DRAINING"}}
