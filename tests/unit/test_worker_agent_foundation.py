import asyncio
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from threads_platform.application.ports.worker_agent import (
    LocalRecoveryEntry,
    LocalSessionState,
    WorkerAccountContext,
    WorkerAgentPresence,
    WorkerControlClientError,
    WorkerJobSnapshot,
)
from threads_platform.application.worker_protocol import is_worker_protocol_supported
from threads_platform.domain.workers import (
    BrowserSessionState,
    NetworkProfile,
    NetworkProtocol,
    WorkerNode,
    WorkerStatus,
)
from threads_platform.infrastructure.worker_agent.identity import (
    WorkerIdentityFileStore,
    WorkerIdentityStoreError,
)
from threads_platform.infrastructure.worker_agent.local_state import (
    LocalDataRoot,
    LocalProfileDirectoryResolver,
    LocalWorkerStateError,
    ProfileOwnershipError,
    SessionCapacityError,
    WorkerLocalStateStore,
)
from threads_platform.infrastructure.worker_agent.process_lock import (
    WorkerProcessAlreadyRunning,
    WorkerProcessLock,
)
from threads_platform.infrastructure.worker_agent.windows_keys import (
    DPAPIWorkerKeyStore,
    WindowsDPAPIDataProtector,
    WorkerDataProtector,
    WorkerKeyStoreError,
)
from threads_platform.workers.key_store import WorkerDeviceIdentity
from threads_platform.workers.runtime import WorkerAgent, WorkerAgentConfig
from threads_platform.workers.sessions import (
    BrowserSessionOpenResult,
    InvalidSessionTransition,
    LocalBrowserSessionManager,
    NetworkProfileApplication,
    ProxyCredentials,
)


class FakeProtector(WorkerDataProtector):
    def protect(self, plaintext: bytes) -> bytes:
        return b"test-protector-v1\0" + bytes(byte ^ 0xA5 for byte in plaintext)

    def unprotect(self, ciphertext: bytes) -> bytes:
        marker = b"test-protector-v1\0"
        if not ciphertext.startswith(marker):
            raise ValueError("invalid test protected data")
        return bytes(byte ^ 0xA5 for byte in ciphertext[len(marker) :])


def test_local_data_root_uses_local_app_data_and_rejects_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    root = LocalDataRoot.from_environment()
    assert root.path == (tmp_path / "Local" / "ThreadsOperations")
    root.prepare()
    assert root.child("worker", "worker_id").is_relative_to(root.path)
    with pytest.raises(LocalWorkerStateError, match="escapes"):
        root.child("..", "outside")
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    with pytest.raises(LocalWorkerStateError, match="outside Git worktrees"):
        LocalDataRoot(repository / "worker-data").prepare()


def test_identity_is_stable_and_corruption_fails_closed(tmp_path: Path) -> None:
    root = LocalDataRoot(tmp_path / "worker-data")
    root.prepare()
    store = WorkerIdentityFileStore(root)
    first = store.load_or_create()
    assert store.enrollment_pending()
    store.mark_enrolled()

    restarted = WorkerIdentityFileStore(root)
    assert restarted.load_or_create() == first
    assert not restarted.enrollment_pending()

    identity_file = root.child("worker", "worker_id")
    identity_file.write_text("corrupt identity", encoding="ascii")
    with pytest.raises(WorkerIdentityStoreError):
        restarted.load_or_create()
    assert identity_file.read_text(encoding="ascii") == "corrupt identity"


def test_persistent_key_store_protector_contract_and_fail_closed_behavior(
    tmp_path: Path,
) -> None:
    root = LocalDataRoot(tmp_path / "worker-data")
    root.prepare()
    worker_id = uuid4()
    key_store = DPAPIWorkerKeyStore(root, protector_factory=lambda _: FakeProtector())

    first = key_store.load_or_create(worker_id)
    protected_path = root.child("worker", f"{worker_id}.device-key.dpapi")
    protected_file = protected_path.read_bytes()
    assert b"test-protector-v1" in protected_file
    assert first.public_key_bytes == key_store.load_or_create(worker_id).public_key_bytes

    protected_path.write_bytes(b"corrupt")
    with pytest.raises(WorkerKeyStoreError, match="corrupt"):
        key_store.load_or_create(worker_id)

    if os.name != "nt":
        with pytest.raises(WorkerKeyStoreError, match="only on Windows"):
            WindowsDPAPIDataProtector(worker_id)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows DPAPI")
def test_windows_dpapi_persists_and_reloads_the_same_device_identity(tmp_path: Path) -> None:
    root = LocalDataRoot(tmp_path / "worker-data")
    root.prepare()
    worker_id = uuid4()
    key_store = DPAPIWorkerKeyStore(root)

    first = key_store.load_or_create(worker_id)
    protected_file = root.child("worker", f"{worker_id}.device-key.dpapi").read_bytes()

    assert protected_file.startswith(b"TPW-DPAPI-ED25519-1\0")
    assert key_store.load_or_create(worker_id).public_key_bytes == first.public_key_bytes


def test_profile_ownership_capacity_and_restart_recovery(tmp_path: Path) -> None:
    root = LocalDataRoot(tmp_path / "worker-data")
    root.prepare()
    worker_id = uuid4()
    first_account = uuid4()
    second_account = uuid4()
    store = WorkerLocalStateStore(root, worker_id)
    profiles = LocalProfileDirectoryResolver(root, store)
    first_path = profiles.resolve(worker_id, first_account, "profile-a")
    assert first_path.is_relative_to(root.path)
    assert "profile-a" not in str(first_path)
    with pytest.raises(ProfileOwnershipError):
        profiles.resolve(worker_id, second_account, "profile-a")

    profiles.ensure_profile(worker_id, first_account, "profile-a")
    profiles.ensure_profile(worker_id, second_account, "profile-b")
    session_id = uuid4()
    store.reserve_session(
        worker_id=worker_id,
        account_id=first_account,
        profile_ref="profile-a",
        session_id=session_id,
        maximum=1,
    )
    with pytest.raises(SessionCapacityError):
        store.reserve_session(
            worker_id=worker_id,
            account_id=second_account,
            profile_ref="profile-b",
            session_id=uuid4(),
            maximum=1,
        )
    store.record_session_state(
        worker_id=worker_id,
        account_id=first_account,
        profile_ref="profile-a",
        session_id=session_id,
        state=BrowserSessionState.LOGIN_REQUIRED,
        updated_at=datetime.now(UTC),
    )

    after_restart = WorkerLocalStateStore(root, worker_id)
    recovered = after_restart.recover_after_restart(updated_at=datetime.now(UTC))
    assert len(recovered) == 1
    assert recovered[0].state is BrowserSessionState.SESSION_EXPIRED
    assert recovered[0].revision == 2
    assert after_restart.active_session_count() == 0
    assert after_restart.pending_session_reports()[0].requires_intervention


def test_local_recovery_journal_has_only_approved_fields(tmp_path: Path) -> None:
    root = LocalDataRoot(tmp_path / "worker-data")
    root.prepare()
    worker_id = uuid4()
    store = WorkerLocalStateStore(root, worker_id)
    entry = LocalRecoveryEntry(
        worker_job_id=uuid4(),
        account_id=uuid4(),
        profile_ref="profile-safe",
        phase="RECONCILE_REQUIRED",
        updated_at=datetime.now(UTC),
    )
    store.save_recovery_entry(entry)

    assert store.recovery_entries() == [entry]
    assert {item.name for item in fields(LocalRecoveryEntry)} == {
        "worker_job_id",
        "account_id",
        "profile_ref",
        "phase",
        "updated_at",
    }
    database = root.journal_path.read_bytes()
    assert b"password" not in database
    assert b"access_token" not in database


def test_profile_session_transitions_capacity_and_network_boundary(tmp_path: Path) -> None:
    root = LocalDataRoot(tmp_path / "worker-data")
    root.prepare()
    worker_id = uuid4()
    account_id = uuid4()
    other_account_id = uuid4()
    third_account_id = uuid4()
    store = WorkerLocalStateStore(root, worker_id)
    profiles = LocalProfileDirectoryResolver(root, store)
    manager = LocalBrowserSessionManager(worker_id, 2, store, profiles)
    network = NetworkProfile(
        account_id=account_id,
        name="account proxy",
        protocol=NetworkProtocol.HTTPS,
        host="proxy.example.test",
        port=8443,
        credential_ref="secret-store://account/proxy",
    )
    context = WorkerAccountContext(account_id, worker_id, "profile-main", network)

    opened = asyncio.run(manager.open(context))
    assert isinstance(opened, BrowserSessionOpenResult)
    assert opened.state.state is BrowserSessionState.LOGIN_REQUIRED
    assert opened.state.state.requires_intervention
    assert opened.network_route.protocol is NetworkProtocol.HTTPS
    assert opened.network_route.credential_ref == "secret-store://account/proxy"
    assert "secret-store://account/proxy" not in repr(opened.network_route)
    with pytest.raises(ValueError, match="cannot be moved"):
        asyncio.run(
            manager.open(WorkerAccountContext(account_id, worker_id, "profile-reassigned", None))
        )

    other_context = WorkerAccountContext(other_account_id, worker_id, "profile-other", None)
    other_opened = asyncio.run(manager.open(other_context))
    assert other_opened.state.state is BrowserSessionState.LOGIN_REQUIRED
    assert other_opened.network_route.protocol is NetworkProtocol.DIRECT
    assert store.active_session_count() == 2
    third_context = WorkerAccountContext(third_account_id, worker_id, "profile-third", None)
    with pytest.raises(SessionCapacityError):
        asyncio.run(manager.open(third_context))
    with pytest.raises(ValueError, match="another worker"):
        asyncio.run(
            manager.open(WorkerAccountContext(account_id, uuid4(), "profile-main", network))
        )
    with pytest.raises(ValueError, match="different account"):
        NetworkProfileApplication().resolve(other_account_id, network)

    with pytest.raises(InvalidSessionTransition):
        asyncio.run(manager.transition(account_id, BrowserSessionState.AUTHENTICATED))
    asyncio.run(manager.transition(account_id, BrowserSessionState.STARTING))
    asyncio.run(manager.transition(account_id, BrowserSessionState.CHALLENGE_REQUIRED))
    asyncio.run(manager.transition(account_id, BrowserSessionState.LOGIN_REQUIRED))
    stopped = asyncio.run(manager.close(account_id))
    assert stopped.state is BrowserSessionState.STOPPED
    asyncio.run(manager.close(other_account_id))
    assert store.active_session_count() == 0


def test_concurrent_session_opens_cannot_exceed_local_capacity(tmp_path: Path) -> None:
    root = LocalDataRoot(tmp_path / "worker-data")
    root.prepare()
    worker_id = uuid4()
    store = WorkerLocalStateStore(root, worker_id)
    manager = LocalBrowserSessionManager(
        worker_id, 1, store, LocalProfileDirectoryResolver(root, store)
    )
    contexts = (
        WorkerAccountContext(uuid4(), worker_id, "profile-a", None),
        WorkerAccountContext(uuid4(), worker_id, "profile-b", None),
    )

    def try_open(context: WorkerAccountContext) -> bool:
        try:
            asyncio.run(manager.open(context))
        except SessionCapacityError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(try_open, contexts))

    assert results.count(True) == 1
    assert results.count(False) == 1
    assert store.active_session_count() == 1


def test_process_lock_prevents_a_second_local_agent(tmp_path: Path) -> None:
    lock_path = tmp_path / "worker" / "agent.lock"
    first = WorkerProcessLock(lock_path)
    second = WorkerProcessLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(WorkerProcessAlreadyRunning):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_worker_protocol_v1_and_v2_compatibility() -> None:
    assert is_worker_protocol_supported(1, 1)
    assert is_worker_protocol_supported(2, 1)
    assert not is_worker_protocol_supported(3, 1)
    assert not is_worker_protocol_supported(2, 2)


def test_worker_capacity_summary_cannot_exceed_advertised_browser_capacity() -> None:
    with pytest.raises(ValueError, match="cannot exceed browser session capacity"):
        WorkerNode(
            worker_id=uuid4(),
            display_name="capacity-test",
            hostname="host",
            max_browser_sessions=1,
            active_browser_sessions=2,
        )


class _FakeIdentityStore:
    def __init__(self) -> None:
        self.worker_id = uuid4()
        self.pending = True

    def load_or_create(self) -> UUID:
        return self.worker_id

    def enrollment_pending(self) -> bool:
        return self.pending

    def mark_enrolled(self) -> None:
        self.pending = False


class _FakeKeyStore:
    def __init__(self) -> None:
        self.identity = WorkerDeviceIdentity.generate()

    def load_or_create(self, worker_id: UUID) -> WorkerDeviceIdentity:
        _ = worker_id
        return self.identity


class _FakeControlClient:
    def __init__(self, worker_id: UUID) -> None:
        self.worker_id = worker_id
        self.authentication_count = 0
        self.reconcile_count = 0
        self.claim_count = 0
        self.closed = False
        self.fail_heartbeat = False
        self.account_context_value: WorkerAccountContext | None = None
        self.reported_sessions: list[LocalSessionState] = []
        self.expires_at = datetime(2026, 9, 25, 13, tzinfo=UTC)
        self.presence = WorkerAgentPresence(worker_id, WorkerStatus.ONLINE, True, 2, 0)

    @property
    def access_token_expires_at(self) -> datetime | None:
        return self.expires_at

    async def authenticate(
        self,
        worker_id: UUID,
        identity: WorkerDeviceIdentity,
        *,
        enrollment_pending: bool,
        enrollment_code: str | None,
        display_name: str,
        hostname: str,
        max_concurrent_jobs: int,
        max_browser_sessions: int,
    ) -> None:
        _ = (
            worker_id,
            identity,
            enrollment_pending,
            enrollment_code,
            display_name,
            hostname,
            max_concurrent_jobs,
            max_browser_sessions,
        )
        self.authentication_count += 1

    async def hello(
        self,
        worker_id: UUID,
        *,
        agent_version: str,
        capabilities: Sequence[tuple[str, int]],
        max_concurrent_jobs: int,
        max_browser_sessions: int,
        active_browser_sessions: int,
    ) -> WorkerAgentPresence:
        _ = (
            worker_id,
            agent_version,
            capabilities,
            max_concurrent_jobs,
            max_browser_sessions,
            active_browser_sessions,
        )
        return self.presence

    async def heartbeat(self, active_browser_sessions: int) -> WorkerAgentPresence:
        _ = active_browser_sessions
        if self.fail_heartbeat:
            raise WorkerControlClientError("CONTROL_PLANE_UNAVAILABLE")
        return self.presence

    async def reconcile(self) -> tuple[WorkerJobSnapshot, ...]:
        self.reconcile_count += 1
        return ()

    async def claim_next(self) -> WorkerJobSnapshot | None:
        self.claim_count += 1
        return None

    async def account_context(self, account_id: UUID) -> WorkerAccountContext:
        if self.account_context_value is None:
            raise AssertionError("no account context configured for this test")
        assert self.account_context_value.account_id == account_id
        return self.account_context_value

    async def report_session_state(self, session: LocalSessionState) -> None:
        self.reported_sessions.append(session)

    async def aclose(self) -> None:
        self.closed = True


def test_worker_runtime_reauthenticates_reconciles_and_never_claims_offline(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = LocalDataRoot(tmp_path / "worker-data")
        root.prepare()
        identity_store = _FakeIdentityStore()
        state_store = WorkerLocalStateStore(root, identity_store.worker_id)
        client = _FakeControlClient(identity_store.worker_id)
        now = datetime(2026, 9, 25, 12, tzinfo=UTC)
        processed: list[WorkerJobSnapshot] = []

        async def handle(job: WorkerJobSnapshot) -> None:
            processed.append(job)

        agent = WorkerAgent(
            WorkerAgentConfig(
                control_plane_url="https://control.test",
                display_name="runtime-test",
                agent_version="0.1.0",
                capabilities=(("synthetic.echo", 1),),
                heartbeat_interval=timedelta(seconds=1),
                poll_interval=timedelta(milliseconds=10),
            ),
            identity_store,
            _FakeKeyStore(),
            state_store,
            WorkerProcessLock(root.child("worker", "agent.lock")),
            client,
            job_handler=handle,
            clock=lambda: now,
        )
        await agent.connect_once()
        assert agent.connected
        assert agent.may_claim
        assert identity_store.pending is False
        assert client.reconcile_count == 1

        for status in (WorkerStatus.DRAINING, WorkerStatus.UPGRADE_REQUIRED):
            now = now + timedelta(seconds=2)
            client.presence = WorkerAgentPresence(
                identity_store.worker_id,
                status,
                status is WorkerStatus.DRAINING,
                2,
                0,
            )
            assert await agent.tick() is None
            assert not agent.may_claim
            assert client.claim_count == 0

        client.presence = WorkerAgentPresence(
            identity_store.worker_id, WorkerStatus.ONLINE, True, 2, 0
        )
        now = now + timedelta(seconds=2)
        client.fail_heartbeat = True
        with pytest.raises(WorkerControlClientError):
            await agent.tick()
        assert not agent.connected
        assert await agent.tick() is None
        assert client.claim_count == 0

        client.fail_heartbeat = False
        await agent.connect_once()
        assert client.authentication_count == 2
        assert client.reconcile_count == 2

        now = now + timedelta(seconds=2)
        client.expires_at = now + timedelta(seconds=30)
        await agent.tick()
        assert client.authentication_count == 3
        assert client.claim_count == 1
        assert processed == []
        await agent.close()
        assert client.closed

    asyncio.run(scenario())


def test_worker_runtime_reports_session_transitions_and_shutdown(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = LocalDataRoot(tmp_path / "worker-data")
        root.prepare()
        identity_store = _FakeIdentityStore()
        worker_id = identity_store.worker_id
        account_id = uuid4()
        context = WorkerAccountContext(account_id, worker_id, "profile-runtime", None)
        state_store = WorkerLocalStateStore(root, worker_id)
        client = _FakeControlClient(worker_id)
        client.account_context_value = context
        agent = WorkerAgent(
            WorkerAgentConfig(
                control_plane_url="https://control.test",
                display_name="session-test",
                agent_version="0.1.0",
            ),
            identity_store,
            _FakeKeyStore(),
            state_store,
            WorkerProcessLock(root.child("worker", "agent.lock")),
            client,
        )
        manager = LocalBrowserSessionManager(
            worker_id, 1, state_store, LocalProfileDirectoryResolver(root, state_store)
        )

        await agent.connect_once()
        opened = await agent.open_browser_session(account_id, manager)
        assert opened.state.state is BrowserSessionState.LOGIN_REQUIRED
        assert [report.state for report in client.reported_sessions] == [
            BrowserSessionState.STARTING,
            BrowserSessionState.LOGIN_REQUIRED,
        ]
        await agent.transition_browser_session(account_id, BrowserSessionState.STARTING)
        await agent.transition_browser_session(account_id, BrowserSessionState.AUTHENTICATED)
        challenge = await agent.transition_browser_session(
            account_id, BrowserSessionState.CHALLENGE_REQUIRED
        )
        assert challenge.requires_intervention
        await agent.close_browser_session(account_id)
        assert client.reported_sessions[-1].state is BrowserSessionState.STOPPED
        assert state_store.active_session_count() == 0
        await agent.close()

    asyncio.run(scenario())


def test_secret_diagnostic_representations_are_redacted() -> None:
    credentials = ProxyCredentials(username="proxy-user", password="proxy-pass")
    error = WorkerControlClientError("SESSION_REPORT_STALE", status_code=409)
    assert "proxy-user" not in repr(credentials)
    assert "proxy-pass" not in repr(credentials)
    assert "SESSION_REPORT_STALE" in repr(error)
    assert "proxy" not in repr(error)
