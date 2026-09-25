from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from threads_platform.app import create_app
from threads_platform.application.ports.worker_agent import LocalSessionState
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.application.worker_sessions import WorkerSessionService
from threads_platform.config.settings import Settings
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.workers import (
    AccountWorkerAssignment,
    BrowserProfile,
    BrowserSessionState,
    NetworkProfile,
    NetworkProtocol,
    WorkerNode,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.workers.control_client import (
    HttpWorkerControlClient,
    WorkerControlClientError,
)
from threads_platform.workers.key_store import WorkerDeviceIdentity

pytestmark = pytest.mark.integration


async def test_protocol_v2_http_session_and_capacity_foundation(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    worker_id = uuid4()
    other_worker_id = uuid4()
    account = ThreadsAccount(threads_user_id=f"agent-{uuid4()}", username="agent_account")
    other_account = ThreadsAccount(
        threads_user_id=f"agent-other-{uuid4()}", username="other_agent_account"
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.accounts.add(other_account)

    admin_token = "agent-transport-admin-token"
    control = WorkerControlService(unit_of_work_factory)
    jobs = WorkerJobService(unit_of_work_factory)
    sessions = WorkerSessionService(unit_of_work_factory)
    app = create_app(
        Settings(worker_admin_token=SecretStr(admin_token), worker_tls_required=True),
        worker_control_service=control,
        worker_job_service=jobs,
        worker_session_service=sessions,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://control.test") as admin:
        enrollment = await admin.post(
            "/v1/workers/enrollments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"created_by": "integration"},
        )
    assert enrollment.status_code == 200

    identity = WorkerDeviceIdentity.generate()
    client = HttpWorkerControlClient("https://control.test", transport=transport)
    await client.authenticate(
        worker_id,
        identity,
        enrollment_pending=True,
        enrollment_code=enrollment.json()["enrollment_code"],
        display_name="Worker Agent Test",
        hostname="AGENT-TEST",
        max_concurrent_jobs=2,
        max_browser_sessions=3,
    )
    profile = BrowserProfile(worker_id, f"profile-{uuid4()}")
    other_profile = BrowserProfile(other_worker_id, f"profile-{uuid4()}")
    network = NetworkProfile(
        account_id=account.id,
        name="account route",
        protocol=NetworkProtocol.SOCKS5,
        host="proxy.example.test",
        port=1080,
        credential_ref="secret-store://account/proxy",
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.workers.add(WorkerNode(other_worker_id, "other", "other-host"))
        await unit_of_work.browser_profiles.add(profile)
        await unit_of_work.browser_profiles.add(other_profile)
        await unit_of_work.network_profiles.add(network)
        await unit_of_work.assignments.add(
            AccountWorkerAssignment(
                account.id,
                worker_id,
                profile.profile_ref,
                network_profile_id=network.id,
            )
        )
        await unit_of_work.assignments.add(
            AccountWorkerAssignment(other_account.id, other_worker_id, other_profile.profile_ref)
        )
    presence = await client.hello(
        worker_id,
        agent_version="0.1.0",
        capabilities=(),
        max_concurrent_jobs=2,
        max_browser_sessions=3,
        active_browser_sessions=0,
    )
    assert presence.protocol_compatible
    assert presence.max_browser_sessions == 3
    assert presence.active_browser_sessions == 0

    context = await client.account_context(account.id)
    assert context.worker_id == worker_id
    assert context.profile_ref == profile.profile_ref
    assert context.network_profile is not None
    assert context.network_profile.protocol is NetworkProtocol.SOCKS5
    assert context.network_profile.credential_ref == "secret-store://account/proxy"
    with pytest.raises(WorkerControlClientError) as mismatch:
        await client.account_context(other_account.id)
    assert mismatch.value.code == "ACCOUNT_WORKER_AFFINITY_MISMATCH"
    with pytest.raises(WorkerControlClientError) as over_capacity:
        await client.heartbeat(active_browser_sessions=4)
    assert over_capacity.value.code == "BROWSER_SESSION_CAPACITY_EXCEEDED"

    local_state = LocalSessionState(
        account_id=account.id,
        profile_ref=profile.profile_ref,
        session_id=uuid4(),
        state=BrowserSessionState.LOGIN_REQUIRED,
        revision=1,
        updated_at=datetime.now(UTC),
    )
    await client.report_session_state(local_state)
    await client.report_session_state(local_state)
    heartbeat = await client.heartbeat(active_browser_sessions=1)
    assert heartbeat.status == presence.status
    assert heartbeat.active_browser_sessions == 1
    conflicting_state = LocalSessionState(
        account_id=local_state.account_id,
        profile_ref=local_state.profile_ref,
        session_id=local_state.session_id,
        state=BrowserSessionState.SESSION_EXPIRED,
        revision=local_state.revision,
        updated_at=local_state.updated_at,
    )
    with pytest.raises(WorkerControlClientError) as stale:
        await client.report_session_state(conflicting_state)
    assert stale.value.code == "SESSION_REPORT_STALE"

    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.worker_account_sessions.get(account.id)
    assert stored is not None
    assert stored.state is BrowserSessionState.LOGIN_REQUIRED
    assert stored.requires_intervention
    await client.aclose()
