import base64
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from threads_platform.app import create_app
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.config.settings import Settings
from threads_platform.infrastructure.security.worker_auth import challenge_message
from threads_platform.workers.key_store import WorkerDeviceIdentity

pytestmark = pytest.mark.integration


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
