from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest

from threads_platform.application.ports.worker_agent import (
    LocalRecoveryEntry,
    LocalSessionState,
    WorkerAccountContext,
    WorkerControlClientError,
    WorkerJobSnapshot,
)
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety, WorkerJobStatus
from threads_platform.domain.workers import BrowserSessionState, NetworkProfile, NetworkProtocol
from threads_platform.infrastructure.browser.playwright_engine import (
    PlaywrightBrowserEngine,
)
from threads_platform.infrastructure.worker_agent.local_state import (
    LocalDataRoot,
    LocalProfileDirectoryResolver,
    WorkerLocalStateStore,
)
from threads_platform.workers.browser import (
    ActionOutcomeAmbiguous,
    BrowserAccountAffinityMismatch,
    BrowserAdapterError,
    BrowserContractError,
    BrowserLaunchRequest,
    BrowserNavigationPolicy,
    BrowserNetworkRouteUnsupported,
    BrowserProcessCrashed,
    BrowserSurface,
    BrowserSurfaceState,
    ChallengeDetected,
    LocatorNotFound,
    ManagedPlaywrightBrowserSessionManager,
    MediaUploadFailed,
    NavigationTimeout,
    PlaywrightBrowserAdapter,
    SessionExpired,
    UnsupportedUIState,
    WorkerBrowserSession,
    WorkerJobExecution,
    WorkerJobLeaseLost,
    WorkerJobReconnectRecovery,
    classify_browser_surface,
)
from threads_platform.workers.sessions import (
    BrowserSessionOpenResult,
    LocalBrowserSessionManager,
    NetworkRoute,
    ProxyCredentials,
)


@pytest.fixture
def synthetic_origin() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/slow":
                time.sleep(0.4)
            body = _SYNTHETIC_DOCUMENTS.get(
                path,
                _document("AUTHENTICATED"),
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                return

        def log_message(self, format: str, *args: object) -> None:
            _ = (format, args)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_playwright_managed_profile_contract_navigation_and_cleanup(
    tmp_path: Path,
    synthetic_origin: str,
) -> None:
    async def scenario() -> None:
        worker_id = uuid4()
        account_id = uuid4()
        context, manager, store, resolver, opened = await _managed_session(
            tmp_path,
            worker_id,
            account_id,
            "logical-profile",
            open_session=False,
        )
        adapter = PlaywrightBrowserAdapter(
            worker_id,
            resolver,
            PlaywrightBrowserEngine(navigation_timeout_ms=100),
        )
        managed_manager = ManagedPlaywrightBrowserSessionManager(
            manager,
            adapter,
            headless=True,
        )
        opened = await managed_manager.open(context)
        session = managed_manager.browser_session(account_id)
        assert session is not None
        assert opened.state.state is BrowserSessionState.LOGIN_REQUIRED
        profile_directory = resolver.resolve(worker_id, account_id, "logical-profile")
        policy = _local_policy(synthetic_origin)

        await session.navigate(f"{synthetic_origin}/authenticated", policy)
        assert await session.inspect_contract() is BrowserSurfaceState.AUTHENTICATED
        assert profile_directory.is_dir()
        assert list(profile_directory.iterdir())

        await session.navigate(f"{synthetic_origin}/unknown", policy)
        with pytest.raises(UnsupportedUIState) as unknown:
            await session.inspect_contract()
        assert str(unknown.value) == "UNSUPPORTED_UI_STATE"
        current = store.get_session(account_id)
        assert current is not None and current.state is BrowserSessionState.ERROR

        await session.navigate(f"{synthetic_origin}/missing", policy)
        with pytest.raises(LocatorNotFound):
            await session.inspect_contract()

        with pytest.raises(NavigationTimeout):
            await session.navigate(f"{synthetic_origin}/slow", policy)

        await managed_manager.close(account_id)
        assert store.active_session_count() == 0
        current = store.get_session(account_id)
        assert current is not None and current.state is BrowserSessionState.STOPPED

    asyncio.run(scenario())


def test_synthetic_session_states_report_durable_interventions(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_id = uuid4()
        account_id = uuid4()
        context, manager, store, resolver, _ = await _managed_session(
            tmp_path,
            worker_id,
            account_id,
            "session-profile",
            open_session=False,
        )
        engine = _MemoryBrowserEngine()
        adapter = PlaywrightBrowserAdapter(worker_id, resolver, engine)
        policy = _local_policy("http://127.0.0.1:41000")
        outcomes = (
            ("LOGIN_REQUIRED", BrowserSessionState.LOGIN_REQUIRED, None),
            ("SESSION_EXPIRED", BrowserSessionState.SESSION_EXPIRED, SessionExpired),
            ("CHALLENGE_REQUIRED", BrowserSessionState.CHALLENGE_REQUIRED, ChallengeDetected),
        )
        for surface_state, expected_state, expected_error in outcomes:
            opened = await manager.open(context)
            job = _running_job(worker_id, account_id)
            client = _MemoryWorkerJobControl(worker_id, account_id, job)
            execution = WorkerJobExecution(job, worker_id, client)
            engine.surface = _surface(surface_state)
            session = await adapter.open_reserved_session(
                context,
                opened,
                transition=manager.transition,
                close_session=manager.close,
                job_execution=execution,
                headless=True,
            )
            await session.navigate("http://127.0.0.1:41000/test", policy)
            if expected_error is None:
                assert await session.inspect_contract() is BrowserSurfaceState.LOGIN_REQUIRED
            else:
                with pytest.raises(expected_error):
                    await session.inspect_contract()
            state = store.get_session(account_id)
            assert state is not None and state.state is expected_state
            assert client.interventions[-1] == (expected_state.value, expected_state.value)
            await session.close()

    asyncio.run(scenario())


def test_playwright_credentials_stay_in_memory_and_are_account_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_id = uuid4()
        first_account, second_account = uuid4(), uuid4()
        root, store, resolver = _managed_storage(tmp_path, worker_id)
        manager = LocalBrowserSessionManager(worker_id, 2, store, resolver)
        provider = _MemoryProxyCredentials(
            {
                "secret://first-account": ProxyCredentials("first-user", "first-secret"),
                "secret://second-account": ProxyCredentials("second-user", "second-secret"),
            }
        )
        engine = _MemoryBrowserEngine()
        adapter = PlaywrightBrowserAdapter(
            worker_id,
            resolver,
            engine,
            credential_provider=provider,
        )
        sessions: list[WorkerBrowserSession] = []
        for account_id, credential_ref, profile_ref in (
            (first_account, "secret://first-account", "first-profile"),
            (second_account, "secret://second-account", "second-profile"),
        ):
            network = NetworkProfile(
                account_id=account_id,
                name="account network",
                protocol=NetworkProtocol.HTTPS,
                host="proxy.example.test",
                port=8443,
                credential_ref=credential_ref,
            )
            context = WorkerAccountContext(account_id, worker_id, profile_ref, network)
            opened = await manager.open(context)
            sessions.append(
                await adapter.open_reserved_session(
                    context,
                    opened,
                    transition=manager.transition,
                    close_session=manager.close,
                    headless=True,
                )
            )
        assert provider.requested_refs == ["secret://first-account", "secret://second-account"]
        assert [request.account_id for request in engine.requests] == [
            first_account,
            second_account,
        ]
        assert engine.requests[0].proxy_credentials == ProxyCredentials(
            "first-user", "first-secret"
        )
        assert engine.requests[1].proxy_credentials == ProxyCredentials(
            "second-user", "second-secret"
        )
        diagnostic = repr(engine.requests)
        assert "first-secret" not in diagnostic
        assert "second-secret" not in diagnostic
        assert "secret://first-account" not in diagnostic
        assert "secret://second-account" not in diagnostic

        for session in sessions:
            await session.close()
        assert store.active_session_count() == 0
        assert root.path.is_dir()

    asyncio.run(scenario())


def test_credentialed_socks5_is_rejected_without_secret_in_error() -> None:
    account_id = uuid4()
    request = BrowserLaunchRequest(
        uuid4(),
        account_id,
        "profile",
        Path("unused"),
        NetworkRoute(account_id, NetworkProtocol.SOCKS5, "proxy.example.test", 1080),
        proxy_credentials=ProxyCredentials("proxy-user", "proxy-password"),
    )
    with pytest.raises(BrowserNetworkRouteUnsupported) as error:
        from threads_platform.infrastructure.browser.playwright_engine import (
            playwright_proxy_settings,
        )

        playwright_proxy_settings(request)
    assert "proxy-password" not in str(error.value)
    assert "proxy-user" not in repr(request)


def test_browser_adapter_rejects_profile_from_another_account(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_id = uuid4()
        first_account, second_account = uuid4(), uuid4()
        _, store, resolver = _managed_storage(tmp_path, worker_id)
        manager = LocalBrowserSessionManager(worker_id, 2, store, resolver)
        first_context = WorkerAccountContext(first_account, worker_id, "first-profile", None)
        second_context = WorkerAccountContext(second_account, worker_id, "second-profile", None)
        opened = await manager.open(first_context)
        engine = _MemoryBrowserEngine()
        adapter = PlaywrightBrowserAdapter(worker_id, resolver, engine)
        with pytest.raises(BrowserAccountAffinityMismatch):
            await adapter.open_reserved_session(
                second_context,
                opened,
                transition=manager.transition,
                close_session=manager.close,
                headless=True,
            )
        assert engine.requests == []
        assert store.active_session_count() == 0
        closed = store.get_session(first_account)
        assert closed is not None and closed.state is BrowserSessionState.STOPPED

    asyncio.run(scenario())


def test_http_proxy_credentials_are_passed_only_to_engine_in_memory() -> None:
    from threads_platform.infrastructure.browser.playwright_engine import (
        playwright_proxy_settings,
    )

    account_id = uuid4()
    request = BrowserLaunchRequest(
        uuid4(),
        account_id,
        "logical-profile",
        Path("unused"),
        NetworkRoute(account_id, NetworkProtocol.HTTPS, "proxy.example.test", 8443, "vault-ref"),
        proxy_credentials=ProxyCredentials("route-user", "route-password"),
        headless=True,
    )
    settings = playwright_proxy_settings(request)
    assert settings is not None
    assert settings == {
        "server": "https://proxy.example.test:8443",
        "username": "route-user",
        "password": "route-password",
    }
    assert "route-password" not in repr(request)
    assert "vault-ref" not in repr(request)


def test_unknown_ui_contracts_and_missing_markers_fail_closed() -> None:
    assert classify_browser_surface(_surface("AUTHENTICATED")) is BrowserSurfaceState.AUTHENTICATED
    with pytest.raises(UnsupportedUIState):
        classify_browser_surface(_surface("AUTHENTICATED", version="2"))
    with pytest.raises(UnsupportedUIState):
        classify_browser_surface(
            BrowserSurface("unrecognized.contract", "1", "AUTHENTICATED", True)
        )
    with pytest.raises(LocatorNotFound):
        classify_browser_surface(_surface("AUTHENTICATED", include_root=False))
    with pytest.raises(BrowserAdapterError) as missing_contract:
        classify_browser_surface(BrowserSurface(None, None, None, True))
    assert str(missing_contract.value) == "BROWSER_CONTRACT_MISMATCH"


def test_navigation_policy_allows_only_allowlisted_loopback_http() -> None:
    policy = _local_policy("http://127.0.0.1:41000")
    policy.validate("http://127.0.0.1:41000/synthetic")
    with pytest.raises(UnsupportedUIState):
        BrowserNavigationPolicy(frozenset({"https://threads.net"})).validate("https://threads.net")
    with pytest.raises(UnsupportedUIState):
        policy.validate("http://example.test:41000/synthetic")


def test_lease_loss_blocks_mutation_and_ambiguous_outcomes_do_not_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        _, store, _ = _managed_storage(tmp_path, worker_id)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(
            job,
            worker_id,
            client,
            local_state=store,
            profile_ref="safe-profile",
        )
        action_count = 0

        async def increment() -> None:
            nonlocal action_count
            action_count += 1

        client.lose_lease = True
        with pytest.raises(WorkerJobLeaseLost):
            await execution.execute_irreversible_boundary(increment)
        assert action_count == 0
        entry = store.recovery_entries()[0]
        assert entry.phase == "MUTATION_PENDING"

        store.clear_recovery_entry(execution.snapshot.job_id)
        client.lose_lease = False

        async def uncertain_action() -> None:
            nonlocal action_count
            action_count += 1
            raise TimeoutError("synthetic remote timeout")

        with pytest.raises(ActionOutcomeAmbiguous) as ambiguous:
            await execution.execute_irreversible_boundary(uncertain_action)
        assert ambiguous.value.intervention_recorded
        assert action_count == 1
        assert client.interventions == [
            ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")
        ]
        assert store.recovery_entries() == []

    asyncio.run(scenario())


def test_reconnect_reconciliation_turns_local_uncertainty_into_intervention(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        _, store, _ = _managed_storage(tmp_path, worker_id)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        store.save_recovery_entry(
            _recovery_entry(job.job_id, account_id, "recover-profile", "MUTATION_STARTED")
        )
        await WorkerJobReconnectRecovery(worker_id, client, store).reconcile((job,))
        assert client.interventions == [
            ("AMBIGUOUS_OUTCOME", "RESTART_REQUIRES_OUTCOME_RECONCILIATION")
        ]
        assert store.recovery_entries() == []

    asyncio.run(scenario())


def test_browser_crash_retries_only_before_mutation_and_reconciles_after(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        _, store, resolver = _managed_storage(tmp_path, worker_id)
        manager = LocalBrowserSessionManager(worker_id, 1, store, resolver)
        context = WorkerAccountContext(account_id, worker_id, "crash-profile", None)
        opened = await manager.open(context)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(
            job,
            worker_id,
            client,
            local_state=store,
            profile_ref=context.profile_ref,
        )
        session = WorkerBrowserSession(
            account_id,
            opened,
            _MemoryBrowserSession(crash_on_navigation=True),
            manager.transition,
            manager.close,
            execution,
            lambda _: None,
        )
        with pytest.raises(BrowserProcessCrashed):
            await session.navigate(
                "http://127.0.0.1:41000/test", _local_policy("http://127.0.0.1:41000")
            )
        assert client.snapshot.status is WorkerJobStatus.FAILED_RETRYABLE
        current = store.get_session(account_id)
        assert current is not None and current.state is BrowserSessionState.ERROR
        await session.close()

        account_id = uuid4()
        context = WorkerAccountContext(account_id, worker_id, "ambiguous-profile", None)
        opened = await manager.open(context)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(
            job,
            worker_id,
            client,
            local_state=store,
            profile_ref=context.profile_ref,
        )

        async def synthetic_mutation() -> str:
            return "visible"

        await execution.execute_irreversible_boundary(synthetic_mutation)
        session = WorkerBrowserSession(
            account_id,
            opened,
            _MemoryBrowserSession(crash_on_navigation=True),
            manager.transition,
            manager.close,
            execution,
            lambda _: None,
        )
        with pytest.raises(ActionOutcomeAmbiguous) as ambiguous:
            await session.navigate(
                "http://127.0.0.1:41000/test",
                _local_policy("http://127.0.0.1:41000"),
            )
        assert ambiguous.value.intervention_recorded
        assert client.interventions[-1][0] == "AMBIGUOUS_OUTCOME"
        await session.close()

    asyncio.run(scenario())


def test_required_error_messages_are_bounded_codes() -> None:
    errors = (
        BrowserContractError(),
        LocatorNotFound(),
        SessionExpired(),
        ChallengeDetected(),
        NavigationTimeout(),
        ActionOutcomeAmbiguous(intervention_recorded=False),
        MediaUploadFailed(),
        UnsupportedUIState(),
    )
    assert all(error.code.isupper() and str(error) == error.code for error in errors)


async def _managed_session(
    tmp_path: Path,
    worker_id: UUID,
    account_id: UUID,
    profile_ref: str,
    *,
    open_session: bool = True,
) -> tuple[
    WorkerAccountContext,
    LocalBrowserSessionManager,
    WorkerLocalStateStore,
    LocalProfileDirectoryResolver,
    BrowserSessionOpenResult,
]:
    root, store, resolver = _managed_storage(tmp_path, worker_id)
    _ = root
    manager = LocalBrowserSessionManager(worker_id, 2, store, resolver)
    context = WorkerAccountContext(account_id, worker_id, profile_ref, None)
    if open_session:
        opened = await manager.open(context)
    else:
        opened = BrowserSessionOpenResult(
            LocalSessionState(
                account_id,
                profile_ref,
                uuid4(),
                BrowserSessionState.LOGIN_REQUIRED,
                1,
                datetime.now(UTC),
            ),
            NetworkRoute(account_id, NetworkProtocol.DIRECT, None, None),
        )
    return context, manager, store, resolver, opened


def _managed_storage(
    tmp_path: Path,
    worker_id: UUID,
) -> tuple[LocalDataRoot, WorkerLocalStateStore, LocalProfileDirectoryResolver]:
    root = LocalDataRoot(tmp_path / f"worker-{worker_id}")
    root.prepare()
    store = WorkerLocalStateStore(root, worker_id)
    resolver = LocalProfileDirectoryResolver(root, store)
    return root, store, resolver


def _local_policy(origin: str) -> BrowserNavigationPolicy:
    return BrowserNavigationPolicy(frozenset({origin}))


def _document(
    state: str,
    *,
    version: str = "1",
    include_root: bool = True,
) -> bytes:
    root = "<main data-worker-ui-root></main>" if include_root else ""
    return (
        "<!doctype html><html><head>"
        '<meta name="worker-ui-contract" content="worker.synthetic">'
        f'<meta name="worker-ui-version" content="{version}">'
        f'<meta name="worker-session-state" content="{state}">'
        f"</head><body>{root}</body></html>"
    ).encode()


def _surface(
    state: str,
    *,
    version: str = "1",
    include_root: bool = True,
) -> BrowserSurface:
    return BrowserSurface(
        "worker.synthetic",
        version,
        state,
        include_root,
    )


_SYNTHETIC_DOCUMENTS: dict[str, bytes] = {
    "/authenticated": _document("AUTHENTICATED"),
    "/login": _document("LOGIN_REQUIRED"),
    "/expired": _document("SESSION_EXPIRED"),
    "/challenge": _document("CHALLENGE_REQUIRED"),
    "/unknown": _document("AUTHENTICATED", version="17"),
    "/missing": _document("AUTHENTICATED", include_root=False),
    "/no-contract": b"<html><body><main data-worker-ui-root></main></body></html>",
}


def _running_job(worker_id: UUID, account_id: UUID) -> WorkerJobSnapshot:
    return WorkerJobSnapshot(
        job_id=uuid4(),
        capability_name="synthetic.adapter.test",
        capability_version=1,
        status=WorkerJobStatus.RUNNING,
        account_id=account_id,
        assigned_worker_id=worker_id,
        lease_worker_id=worker_id,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        retry_safety=WorkerJobRetrySafety.RECONCILIATION_REQUIRED,
        checkpoint=None,
    )


def _recovery_entry(
    job_id: UUID,
    account_id: UUID,
    profile_ref: str,
    phase: str,
) -> LocalRecoveryEntry:
    return LocalRecoveryEntry(job_id, account_id, profile_ref, phase, datetime.now(UTC))


class _MemoryBrowserSession:
    def __init__(
        self,
        surface: BrowserSurface | None = None,
        *,
        crash_on_navigation: bool = False,
    ) -> None:
        self.surface = surface or _surface("AUTHENTICATED")
        self.crash_on_navigation = crash_on_navigation
        self.closed = False

    async def navigate(self, url: str) -> None:
        _ = url
        if self.crash_on_navigation:
            raise BrowserProcessCrashed()

    async def inspect_surface(self) -> BrowserSurface:
        return self.surface

    async def close(self) -> None:
        self.closed = True


class _MemoryBrowserEngine:
    def __init__(self) -> None:
        self.requests: list[BrowserLaunchRequest] = []
        self.surface = _surface("AUTHENTICATED")

    async def open(self, request: BrowserLaunchRequest) -> _MemoryBrowserSession:
        self.requests.append(request)
        return _MemoryBrowserSession(self.surface)


class _MemoryProxyCredentials:
    def __init__(self, credentials: dict[str, ProxyCredentials]) -> None:
        self._credentials = credentials
        self.requested_refs: list[str] = []

    async def credentials_for(self, credential_ref: str) -> ProxyCredentials:
        self.requested_refs.append(credential_ref)
        return self._credentials[credential_ref]


class _MemoryWorkerJobControl:
    def __init__(
        self,
        worker_id: UUID,
        account_id: UUID,
        snapshot: WorkerJobSnapshot | None = None,
    ) -> None:
        self.snapshot = snapshot or _running_job(worker_id, account_id)
        self.lose_lease = False
        self.interventions: list[tuple[str, str]] = []

    async def renew_job(self, job_id: UUID, lease_token: UUID) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        if self.lose_lease:
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")
        return self.snapshot

    async def checkpoint_job(
        self, job_id: UUID, lease_token: UUID, checkpoint: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.snapshot = replace(self.snapshot, checkpoint=checkpoint)
        return self.snapshot

    async def complete_job(
        self, job_id: UUID, lease_token: UUID, result: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        _ = result
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.SUCCEEDED,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    async def fail_job(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        error_code: str,
        retryable: bool,
        outcome_ambiguous: bool = False,
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        _ = (error_code, outcome_ambiguous)
        self.snapshot = replace(
            self.snapshot,
            status=(
                WorkerJobStatus.FAILED_RETRYABLE if retryable else WorkerJobStatus.FAILED_FINAL
            ),
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    async def request_intervention(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        intervention_type: str,
        detail_code: str,
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.interventions.append((intervention_type, detail_code))
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.WAITING_INTERVENTION,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    def _verify(self, job_id: UUID, lease_token: UUID) -> None:
        if job_id != self.snapshot.job_id or lease_token != self.snapshot.lease_token:
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")
