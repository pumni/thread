import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from threads_platform.app import create_app
from threads_platform.application.worker_control import WorkerControlService, WorkerPresence
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.config.settings import Settings
from threads_platform.domain.workers import WorkerStatus


async def test_worker_http_rejects_plain_http_before_authentication() -> None:
    app = create_app(Settings(worker_tls_required=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://worker.test") as client:
        response = await client.post(
            "/v1/workers/auth/challenges", json={"worker_id": str(uuid4())}
        )
    assert response.status_code == 426
    assert response.json() == {"detail": {"code": "HTTPS_REQUIRED"}}


def test_worker_websocket_rejects_plain_ws() -> None:
    app = create_app(Settings(worker_tls_required=True))
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as disconnect:
            with client.websocket_connect("ws://worker.test/v1/workers/connect"):
                pass
    assert disconnect.value.code == 4403


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

    class FakeWorkerControl:
        async def authenticate(self, token: str) -> object:
            return worker_id if token == "session-token" else None

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
        Settings(worker_tls_required=True),
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


async def test_notification_hub_delivers_only_advisory_payloads() -> None:
    hub = WorkerNotificationHub(queue_size=1)
    worker_id = uuid4()
    async with hub.subscribe(worker_id) as queue:
        hub.publish(worker_id, {"type": "job.available", "job_id": "job-1"})
        message = await asyncio.wait_for(queue.get(), timeout=1)
    assert message == {"type": "job.available", "job_id": "job-1"}
