import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from starlette.types import Message, Scope

from threads_platform.app import create_app
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.config.settings import Settings
from threads_platform.infrastructure.security.worker_auth import challenge_message
from threads_platform.workers.key_store import WorkerDeviceIdentity

pytestmark = pytest.mark.integration


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 25, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, duration: timedelta) -> None:
        self.current += duration


class WebSocketHarness:
    def __init__(self, app: object, token: str) -> None:
        self._app = app
        self._incoming: asyncio.Queue[Message] = asyncio.Queue()
        self.outgoing: asyncio.Queue[Message] = asyncio.Queue()
        self.scope: Scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "scheme": "wss",
            "server": ("worker.test", 443),
            "client": ("test", 1234),
            "root_path": "",
            "path": "/v1/workers/connect",
            "raw_path": b"/v1/workers/connect",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {token}".encode("ascii")),
                (b"host", b"worker.test"),
            ],
            "subprotocols": [],
            "state": {},
        }
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await self._app(self.scope, self._receive, self._send)  # type: ignore[operator]

    async def _receive(self) -> Message:
        return await self._incoming.get()

    async def _send(self, message: Message) -> None:
        await self.outgoing.put(message)

    async def connect(self) -> None:
        await self._incoming.put({"type": "websocket.connect"})
        accepted = await asyncio.wait_for(self.outgoing.get(), timeout=2)
        assert accepted["type"] == "websocket.accept"

    async def send_json(self, message: dict[str, object]) -> None:
        await self._incoming.put({"type": "websocket.receive", "text": json.dumps(message)})

    async def receive_close(self) -> Message:
        while True:
            message = await asyncio.wait_for(self.outgoing.get(), timeout=2)
            if message["type"] == "websocket.close":
                await asyncio.wait_for(self.task, timeout=2)
                return message


async def _authenticated_worker(
    control: WorkerControlService,
    identity: WorkerDeviceIdentity,
) -> tuple[UUID, str]:
    worker_id = uuid4()
    enrollment = await control.create_enrollment()
    await control.enroll(
        enrollment.code,
        worker_id=worker_id,
        display_name="WSS expiry test worker",
        hostname="TEST-HOST",
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


async def test_worker_enrollment_auth_and_hello_use_authenticated_tls_routes(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    admin_token = "c1-test-worker-admin-token"
    service = WorkerControlService(unit_of_work_factory)
    app = create_app(
        Settings(worker_admin_token=SecretStr(admin_token), worker_tls_required=True),
        worker_control_service=service,
    )
    transport = httpx.ASGITransport(app=app)
    worker_id = uuid4()
    identity = WorkerDeviceIdentity.generate()

    async with httpx.AsyncClient(transport=transport, base_url="https://worker.test") as client:
        denied_admin = await client.post("/v1/workers/enrollments", json={})
        assert denied_admin.status_code == 401
        created = await client.post(
            "/v1/workers/enrollments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"created_by": "test-operator"},
        )
        assert created.status_code == 200
        enrollment_code = created.json()["enrollment_code"]

        enrollment_body = {
            "enrollment_code": enrollment_code,
            "worker_id": str(worker_id),
            "display_name": "Transport test worker",
            "hostname": "HOST-TEST",
            "platform": "windows",
            "public_key": base64.b64encode(identity.public_key_bytes).decode("ascii"),
            "max_concurrent_jobs": 2,
        }
        invalid_key_body = {
            **enrollment_body,
            "public_key": base64.b64encode(b"x" * 31).decode("ascii"),
        }
        invalid_key = await client.post("/v1/workers/enroll", json=invalid_key_body)
        assert invalid_key.status_code == 422
        assert invalid_key.json() == {"detail": {"code": "INVALID_PUBLIC_KEY"}}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://worker.test"
        ) as plain_http:
            rejected_transport = await plain_http.post("/v1/workers/enroll", json=enrollment_body)
        assert rejected_transport.status_code == 426

        enrolled = await client.post("/v1/workers/enroll", json=enrollment_body)
        assert enrolled.status_code == 201
        assert enrolled.json()["worker_id"] == str(worker_id)

        challenge_response = await client.post(
            "/v1/workers/auth/challenges", json={"worker_id": str(worker_id)}
        )
        assert challenge_response.status_code == 200
        challenge = challenge_response.json()
        signature = identity.sign(challenge_message(challenge["challenge_id"], challenge["nonce"]))
        session_response = await client.post(
            "/v1/workers/auth/sessions",
            json={
                "challenge_id": challenge["challenge_id"],
                "signature": base64.b64encode(signature).decode("ascii"),
            },
        )
        assert session_response.status_code == 200
        access_token = session_response.json()["access_token"]

        hello_body = {
            "protocol_version": 1,
            "agent_version": "1.0.0",
            "capabilities_schema_version": 1,
            "capabilities": [{"capability_name": "synthetic.echo", "capability_version": 1}],
        }
        denied_hello = await client.post("/v1/workers/hello", json=hello_body)
        assert denied_hello.status_code == 401
        hello = await client.post(
            "/v1/workers/hello",
            headers={"Authorization": f"Bearer {access_token}"},
            json=hello_body,
        )
        assert hello.status_code == 200
        assert hello.json()["worker_id"] == str(worker_id)
        assert hello.json()["status"] == "ONLINE"
        assert set(hello.json()) == {
            "worker_id",
            "status",
            "last_heartbeat_at",
            "presence_expires_at",
            "protocol_compatible",
        }
        heartbeat = await client.post(
            "/v1/workers/heartbeat",
            headers={"Authorization": f"Bearer {access_token}"},
            json={},
        )
        assert heartbeat.status_code == 200
        assert set(heartbeat.json()) == set(hello.json())


async def test_durable_https_pull_recovers_job_without_wss_notification(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    admin_token = "c1-test-worker-admin-token"
    control = WorkerControlService(unit_of_work_factory)
    notifications = WorkerNotificationHub()
    jobs = WorkerJobService(unit_of_work_factory, notifications=notifications)
    app = create_app(
        Settings(worker_admin_token=SecretStr(admin_token), worker_tls_required=True),
        worker_control_service=control,
        worker_job_service=jobs,
        worker_notifications=notifications,
    )
    transport = httpx.ASGITransport(app=app)
    worker_id = uuid4()
    identity = WorkerDeviceIdentity.generate()

    async with httpx.AsyncClient(transport=transport, base_url="https://worker.test") as client:
        created = await client.post(
            "/v1/workers/enrollments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"created_by": "test-operator"},
        )
        enrollment_code = created.json()["enrollment_code"]
        enrolled = await client.post(
            "/v1/workers/enroll",
            json={
                "enrollment_code": enrollment_code,
                "worker_id": str(worker_id),
                "display_name": "Durable pull worker",
                "hostname": "HOST-PULL",
                "platform": "windows",
                "public_key": base64.b64encode(identity.public_key_bytes).decode("ascii"),
            },
        )
        assert enrolled.status_code == 201
        challenge_response = await client.post(
            "/v1/workers/auth/challenges", json={"worker_id": str(worker_id)}
        )
        challenge = challenge_response.json()
        signature = identity.sign(challenge_message(challenge["challenge_id"], challenge["nonce"]))
        session_response = await client.post(
            "/v1/workers/auth/sessions",
            json={
                "challenge_id": challenge["challenge_id"],
                "signature": base64.b64encode(signature).decode("ascii"),
            },
        )
        access_token = session_response.json()["access_token"]
        worker_headers = {"Authorization": f"Bearer {access_token}"}
        hello = await client.post(
            "/v1/workers/hello",
            headers=worker_headers,
            json={
                "protocol_version": 1,
                "agent_version": "1.0.0",
                "capabilities_schema_version": 1,
                "capabilities": [{"capability_name": "synthetic.echo", "capability_version": 1}],
            },
        )
        assert hello.status_code == 200

        # The job is persisted while no WSS subscriber is connected; workers recover it by pull.
        queued = await jobs.enqueue("synthetic.echo", 1)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://worker.test"
        ) as insecure_client:
            rejected = await insecure_client.post("/v1/workers/jobs/claim", headers=worker_headers)
        assert rejected.status_code == 426

        claimed = await client.post("/v1/workers/jobs/claim", headers=worker_headers)
        assert claimed.status_code == 200
        assert claimed.json()["id"] == str(queued.id)
        lease_token = claimed.json()["lease_token"]
        checkpoint = await client.post(
            f"/v1/workers/jobs/{queued.id}/checkpoint",
            headers=worker_headers,
            json={"lease_token": lease_token, "checkpoint": {"phase": "prepared"}},
        )
        assert checkpoint.status_code == 200
        renewed = await client.post(
            f"/v1/workers/jobs/{queued.id}/renew",
            headers=worker_headers,
            json={"lease_token": lease_token},
        )
        assert renewed.status_code == 200
        reconciled = await client.get("/v1/workers/jobs/reconcile", headers=worker_headers)
        assert [item["id"] for item in reconciled.json()["jobs"]] == [str(queued.id)]
        completed = await client.post(
            f"/v1/workers/jobs/{queued.id}/complete",
            headers=worker_headers,
            json={"lease_token": lease_token, "result": {"synthetic": "done"}},
        )
        assert completed.status_code == 200
        stale = await client.post(
            f"/v1/workers/jobs/{queued.id}/complete",
            headers=worker_headers,
            json={"lease_token": lease_token, "result": {"synthetic": "stale"}},
        )
        assert stale.status_code == 409


async def test_established_wss_sessions_expire_for_presence_and_notifications(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    clock = MutableClock()
    control = WorkerControlService(
        unit_of_work_factory,
        clock=clock,
        session_ttl=timedelta(seconds=30),
    )
    notifications = WorkerNotificationHub()
    app = create_app(
        Settings(worker_tls_required=True),
        worker_control_service=control,
        worker_notifications=notifications,
    )
    heartbeat_worker, heartbeat_token = await _authenticated_worker(
        control, WorkerDeviceIdentity.generate()
    )
    notification_worker, notification_token = await _authenticated_worker(
        control, WorkerDeviceIdentity.generate()
    )
    hello_worker, hello_token = await _authenticated_worker(
        control, WorkerDeviceIdentity.generate()
    )
    heartbeat_socket = WebSocketHarness(app, heartbeat_token)
    notification_socket = WebSocketHarness(app, notification_token)
    hello_socket = WebSocketHarness(app, hello_token)
    await heartbeat_socket.connect()
    await notification_socket.connect()
    await hello_socket.connect()

    clock.advance(timedelta(seconds=31))
    await heartbeat_socket.send_json({"type": "worker.heartbeat", "healthy": True})
    heartbeat_close = await heartbeat_socket.receive_close()
    assert heartbeat_close.get("code") == 4401

    await hello_socket.send_json(
        {
            "type": "worker.hello",
            "protocol_version": 1,
            "agent_version": "1.0.0",
            "capabilities_schema_version": 1,
            "capabilities": [],
        }
    )
    hello_close = await hello_socket.receive_close()
    assert hello_close.get("code") == 4401

    notifications.publish(
        notification_worker,
        {"type": "job.available", "job_id": str(uuid4())},
    )
    notification_close = await notification_socket.receive_close()
    assert notification_close.get("code") == 4401

    async with unit_of_work_factory() as unit_of_work:
        stored_heartbeat_worker = await unit_of_work.workers.get(heartbeat_worker)
        stored_hello_worker = await unit_of_work.workers.get(hello_worker)
    initial_presence_time = clock.current - timedelta(seconds=31)
    assert stored_heartbeat_worker is not None
    assert stored_heartbeat_worker.last_heartbeat_at == initial_presence_time
    assert stored_hello_worker is not None
    assert stored_hello_worker.last_heartbeat_at == initial_presence_time
