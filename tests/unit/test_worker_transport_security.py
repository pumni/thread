import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from threads_platform.app import create_app
from threads_platform.application.worker_control import (
    AuthenticatedWorkerSession,
    WorkerControlService,
    WorkerPresence,
)
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.config.settings import Settings
from threads_platform.domain.worker_jobs import WorkerJob, WorkerJobStatus
from threads_platform.domain.workers import WorkerStatus


async def test_worker_http_rejects_plain_http_before_authentication() -> None:
    app = create_app(Settings())
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://worker.test") as client:
        response = await client.post(
            "/v1/workers/auth/challenges", json={"worker_id": str(uuid4())}
        )
    assert response.status_code == 426
    assert response.json() == {"detail": {"code": "HTTPS_REQUIRED"}}


async def test_operator_http_rejects_plain_http_and_forwarded_proto_spoofing() -> None:
    app = create_app(Settings())
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://controller.test") as client:
        response = await client.post(
            "/v1/operator/login",
            headers={"X-Forwarded-Proto": "https"},
            json={"username": "owner", "password": "synthetic-test-value"},
        )
    assert response.status_code == 426
    assert response.json() == {"detail": {"code": "HTTPS_REQUIRED"}}


async def test_worker_http_rejects_forwarded_proto_spoofing() -> None:
    app = create_app(Settings())
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://worker.test") as client:
        response = await client.post(
            "/v1/workers/auth/challenges",
            headers={"X-Forwarded-Proto": "https"},
            json={"worker_id": str(uuid4())},
        )
    assert response.status_code == 426
    assert response.json() == {"detail": {"code": "HTTPS_REQUIRED"}}


def test_worker_websocket_rejects_plain_ws() -> None:
    app = create_app(Settings())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as disconnect:
            with client.websocket_connect("ws://worker.test/v1/workers/connect"):
                pass
    assert disconnect.value.code == 4403


def test_authenticated_https_cancel_ack_is_narrow_and_rejects_extra_request_fields() -> None:
    worker_id = uuid4()
    job_id = uuid4()
    lease_token = uuid4()
    request_id = uuid4()
    captured: list[tuple[object, ...]] = []

    class FakeWorkerControl:
        async def authenticate(self, token: str) -> UUID | None:
            return worker_id if token == "worker-session" else None

    class FakeWorkerJobService:
        async def acknowledge_cancel(
            self,
            requested_job_id: UUID,
            requested_worker_id: UUID,
            requested_lease_token: UUID,
            *,
            cancel_request_id: UUID,
            generation: int,
            checkpoint_phase: str,
        ) -> WorkerJob:
            captured.append(
                (
                    requested_job_id,
                    requested_worker_id,
                    requested_lease_token,
                    cancel_request_id,
                    generation,
                    checkpoint_phase,
                )
            )
            return WorkerJob(
                id=requested_job_id,
                capability_name="threads.browser.feed.browse",
                capability_version=1,
                status=WorkerJobStatus.CANCELLED,
                preemptible=True,
            )

    app = create_app(
        Settings(),
        worker_control_service=cast(WorkerControlService, FakeWorkerControl()),
        worker_job_service=cast(WorkerJobService, FakeWorkerJobService()),
    )
    client = TestClient(app, base_url="https://worker.test")
    headers = {"Authorization": "Bearer worker-session"}
    request_body = {
        "lease_token": str(lease_token),
        "cancel_request_id": str(request_id),
        "generation": 3,
        "checkpoint_phase": "BEFORE_NAVIGATION",
    }
    with client:
        response = client.post(
            f"/v1/workers/jobs/{job_id}/cancel", headers=headers, json=request_body
        )
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"
        assert captured == [
            (
                job_id,
                worker_id,
                lease_token,
                request_id,
                3,
                "BEFORE_NAVIGATION",
            )
        ]

        invalid = client.post(
            f"/v1/workers/jobs/{job_id}/cancel",
            headers=headers,
            json={**request_body, "reason_code": "CALLER_CONTROLLED"},
        )
    assert invalid.status_code == 422
    assert len(captured) == 1


def test_wss_hello_and_heartbeat_are_advisory_presence_messages() -> None:
    worker_id = uuid4()
    now = datetime.now(UTC)
    presence = WorkerPresence(
        worker_id,
        WorkerStatus.ONLINE,
        now,
        now + timedelta(seconds=90),
        True,
    )
    session_valid = True

    class FakeWorkerControl:
        async def authenticate_session(self, token: str) -> AuthenticatedWorkerSession | None:
            if token != "session-token":
                return None
            return AuthenticatedWorkerSession(worker_id, now + timedelta(minutes=15))

        async def authenticate(self, token: str) -> object:
            return worker_id if token == "session-token" and session_valid else None

        def session_time_remaining(self, session: AuthenticatedWorkerSession) -> float:
            assert session.worker_id == worker_id
            return 60.0

        async def hello(self, requested_worker_id: object, **_: object) -> WorkerPresence:
            assert requested_worker_id == worker_id
            return presence

        async def heartbeat(
            self, requested_worker_id: object, *, healthy: bool = True, **_: object
        ) -> WorkerPresence:
            assert requested_worker_id == worker_id
            assert healthy
            return presence

        async def expire_presence(self) -> int:
            return 0

    app = create_app(
        Settings(),
        worker_control_service=cast(WorkerControlService, FakeWorkerControl()),
    )
    test_client = TestClient(app, base_url="https://worker.test")
    with test_client:
        with test_client.websocket_connect(
            "wss://worker.test/v1/workers/connect",
            headers={"Authorization": "Bearer session-token"},
        ) as websocket:
            websocket.send_json({"type": "worker.heartbeat", "healthy": True})
            assert websocket.receive_json() == {
                "type": "worker.heartbeat.accepted",
                "status": "ONLINE",
            }
            websocket.send_json(
                {
                    "type": "worker.hello",
                    "protocol_version": 1,
                    "agent_version": "1.0.0",
                    "capabilities_schema_version": 1,
                    "capabilities": [],
                }
            )
            assert websocket.receive_json() == {
                "type": "worker.hello.accepted",
                "status": "ONLINE",
                "protocol_compatible": True,
            }
            # Let the server close the socket before TestClient tears down the task.
            session_valid = False
            websocket.send_json({"type": "worker.heartbeat", "healthy": True})
            with pytest.raises(WebSocketDisconnect) as disconnect:
                websocket.receive_json()
            assert disconnect.value.code == 4401


def test_wss_closes_when_the_authenticated_session_expires() -> None:
    worker_id = uuid4()
    now = datetime.now(UTC)

    class ExpiredWorkerControl:
        async def authenticate_session(self, token: str) -> AuthenticatedWorkerSession | None:
            if token != "session-token":
                return None
            return AuthenticatedWorkerSession(worker_id, now)

        def session_time_remaining(self, session: AuthenticatedWorkerSession) -> float:
            assert session.worker_id == worker_id
            return 0.0

        async def expire_presence(self) -> int:
            return 0

    app = create_app(
        Settings(),
        worker_control_service=cast(WorkerControlService, ExpiredWorkerControl()),
    )
    test_client = TestClient(app, base_url="https://worker.test")
    with test_client:
        with test_client.websocket_connect(
            "wss://worker.test/v1/workers/connect",
            headers={"Authorization": "Bearer session-token"},
        ) as websocket:
            with pytest.raises(WebSocketDisconnect) as disconnect:
                websocket.receive_json()
    assert disconnect.value.code == 4401


async def test_notification_hub_delivers_only_advisory_payloads() -> None:
    hub = WorkerNotificationHub(queue_size=1)
    worker_id = uuid4()
    async with hub.subscribe(worker_id) as queue:
        hub.publish(worker_id, {"type": "job.available", "job_id": "job-1"})
        message = await asyncio.wait_for(queue.get(), timeout=1)
    assert message == {"type": "job.available", "job_id": "job-1"}
