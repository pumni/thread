import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import WorkerStatus
from threads_platform.workers.control_client import (
    HttpWorkerControlClient,
    WorkerControlClientError,
)
from threads_platform.workers.key_store import WorkerDeviceIdentity


async def _authenticate_client(
    client: HttpWorkerControlClient,
    worker_id: UUID,
) -> None:
    await client.authenticate(
        worker_id,
        WorkerDeviceIdentity.generate(),
        enrollment_pending=False,
        enrollment_code=None,
        display_name="Cancellation test worker",
        hostname="CANCEL-TEST",
        max_concurrent_jobs=1,
        max_browser_sessions=1,
    )


@pytest.mark.asyncio
async def test_http_client_parses_pending_snapshot_and_sends_fenced_ack() -> None:
    worker_id = uuid4()
    job_id = uuid4()
    lease_token = uuid4()
    request_id = uuid4()
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/challenges"):
            return httpx.Response(
                200,
                json={"challenge_id": str(uuid4()), "nonce": "test-nonce"},
            )
        if request.url.path.endswith("/auth/sessions"):
            return httpx.Response(
                200,
                json={
                    "access_token": "worker-test-token",
                    "expires_at": (now + timedelta(minutes=10)).isoformat(),
                },
            )
        if request.url.path.endswith("/renew"):
            return httpx.Response(
                200,
                json={
                    "id": str(job_id),
                    "capability_name": "threads.browser.feed.browse",
                    "capability_version": 1,
                    "status": "RUNNING",
                    "account_id": str(uuid4()),
                    "assigned_worker_id": str(worker_id),
                    "lease_worker_id": str(worker_id),
                    "lease_token": str(lease_token),
                    "lease_expires_at": (now + timedelta(minutes=1)).isoformat(),
                    "retry_safety": "SAFE_TO_RETRY",
                    "checkpoint": {"phase": "FEED_READY"},
                    "input_data": {"max_items": 5},
                    "pending_cancel": {
                        "request_id": str(request_id),
                        "generation": 4,
                        "reason_code": "OPERATOR_REQUESTED",
                        "requested_at": now.isoformat(),
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": str(job_id),
                "capability_name": "threads.browser.feed.browse",
                "capability_version": 1,
                "status": "CANCELLED",
                "account_id": str(uuid4()),
                "assigned_worker_id": str(worker_id),
                "lease_worker_id": None,
                "lease_token": None,
                "lease_expires_at": None,
                "retry_safety": "SAFE_TO_RETRY",
                "checkpoint": {"phase": "FEED_READY"},
                "input_data": {"max_items": 5},
                "pending_cancel": None,
            },
        )

    client = HttpWorkerControlClient("https://control.test", transport=httpx.MockTransport(handle))
    try:
        await _authenticate_client(client, worker_id)
        renewed = await client.renew_job(job_id, lease_token)
        assert renewed.pending_cancel is not None
        assert renewed.pending_cancel.request_id == request_id
        assert renewed.pending_cancel.generation == 4
        assert renewed.pending_cancel.reason_code == "OPERATOR_REQUESTED"
        assert renewed.pending_cancel.requested_at == now

        cancelled = await client.cancel_job(
            job_id,
            lease_token,
            cancel_request_id=request_id,
            generation=4,
            checkpoint_phase="FEED_READY",
        )
    finally:
        await client.aclose()

    assert cancelled.status is WorkerJobStatus.CANCELLED
    assert cancelled.pending_cancel is None
    cancel_request = requests[-1]
    assert cancel_request.url.path == f"/v1/workers/jobs/{job_id}/cancel"
    assert cancel_request.headers["Authorization"] == "Bearer worker-test-token"
    assert json.loads(cancel_request.read()) == {
        "lease_token": str(lease_token),
        "cancel_request_id": str(request_id),
        "generation": 4,
        "checkpoint_phase": "FEED_READY",
    }


@pytest.mark.asyncio
async def test_http_client_rejects_extra_pending_cancellation_metadata() -> None:
    job_id = uuid4()
    worker_id = uuid4()

    async def handle(_: httpx.Request) -> httpx.Response:
        if _.url.path.endswith("/auth/challenges"):
            return httpx.Response(
                200,
                json={"challenge_id": str(uuid4()), "nonce": "test-nonce"},
            )
        if _.url.path.endswith("/auth/sessions"):
            return httpx.Response(
                200,
                json={
                    "access_token": "worker-test-token",
                    "expires_at": datetime.now(UTC).isoformat(),
                },
            )
        return httpx.Response(
            200,
            json={
                "id": str(job_id),
                "capability_name": "threads.browser.feed.browse",
                "capability_version": 1,
                "status": "RUNNING",
                "account_id": None,
                "assigned_worker_id": str(worker_id),
                "lease_worker_id": str(worker_id),
                "lease_token": str(uuid4()),
                "lease_expires_at": datetime.now(UTC).isoformat(),
                "retry_safety": "SAFE_TO_RETRY",
                "checkpoint": {"phase": "BEFORE_NAVIGATION"},
                "input_data": {},
                "pending_cancel": {
                    "request_id": str(uuid4()),
                    "generation": 1,
                    "reason_code": "OPERATOR_REQUESTED",
                    "requested_at": datetime.now(UTC).isoformat(),
                    "caller_controlled": True,
                },
            },
        )

    client = HttpWorkerControlClient("https://control.test", transport=httpx.MockTransport(handle))
    try:
        await _authenticate_client(client, worker_id)
        with pytest.raises(WorkerControlClientError, match="WORKER_PROTOCOL_INVALID_RESPONSE"):
            await client.renew_job(job_id, uuid4())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_client_completes_drain_with_worker_authentication() -> None:
    worker_id = uuid4()
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/challenges"):
            return httpx.Response(
                200,
                json={"challenge_id": str(uuid4()), "nonce": "test-nonce"},
            )
        if request.url.path.endswith("/auth/sessions"):
            return httpx.Response(
                200,
                json={
                    "access_token": "worker-test-token",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                },
            )
        if request.url.path == "/v1/workers/drain/complete":
            if request.headers.get("Authorization") != "Bearer worker-test-token":
                return httpx.Response(401, json={"detail": {"code": "WORKER_UNAUTHORIZED"}})
            return httpx.Response(
                200,
                json={"worker_id": str(worker_id), "status": "OFFLINE"},
            )
        return httpx.Response(404, json={"detail": {"code": "NOT_FOUND"}})

    client = HttpWorkerControlClient("https://control.test", transport=httpx.MockTransport(handle))
    try:
        await _authenticate_client(client, worker_id)
        status = await client.complete_drain()
    finally:
        await client.aclose()

    assert status is WorkerStatus.OFFLINE
    completion_request = requests[-1]
    assert completion_request.url.path == "/v1/workers/drain/complete"
    assert completion_request.method == "POST"
    assert completion_request.headers["Authorization"] == "Bearer worker-test-token"
    assert json.loads(completion_request.read()) == {}
