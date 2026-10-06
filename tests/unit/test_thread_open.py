from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from threads_platform.application.ports.browser import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserNetworkRouteUnsupported,
    BrowserSurface,
    ChallengeDetected,
    RemoteSessionStateUncertain,
    SessionExpired,
)
from threads_platform.application.ports.worker_agent import (
    LocalSessionState,
    WorkerAccountContext,
    WorkerControlClientError,
    WorkerJobCancelSnapshot,
    WorkerJobSnapshot,
)
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety, WorkerJobStatus
from threads_platform.domain.workers import BrowserSessionState
from threads_platform.workers.browser import WorkerBrowserSession
from threads_platform.workers.browser_capability_dispatch import BrowserCapabilityJobDispatcher
from threads_platform.workers.sessions import BrowserSessionOpenResult, WorkerNetworkRoute
from threads_platform.workers.thread_open import (
    THREAD_OPEN_ALLOWED_FAILURE_CODES,
    THREAD_OPEN_CAPABILITY_NAME,
    THREAD_OPEN_CAPABILITY_VERSION,
    BrowserThreadOpenWorker,
    ThreadOpenBrowserSessionManager,
    ThreadOpenWorkerControlClient,
    parse_thread_ref,
)


def test_thread_ref_is_relative_bounded_and_normalized() -> None:
    assert parse_thread_ref({"thread_ref": "/@alice.post/post-id_1/"}) is None
    assert parse_thread_ref({"thread_ref": "/@alice/post/post-id_1/"}) == (
        "/@alice/post/post-id_1",
        "alice",
    )
    for invalid in (
        "https://www.threads.com/@alice/post/post-1",
        "//www.threads.com/@alice/post/post-1",
        "/@alice/post/post-1?x=1",
        "/@alice/post/post-1#fragment",
        "/@alice/post/../post-1",
        "/@alice/post/post-1/extra",
    ):
        assert parse_thread_ref({"thread_ref": invalid}) is None
    assert parse_thread_ref({"thread_ref": "/@alice/post/post-1", "url": "https://x"}) is None


@pytest.mark.asyncio
async def test_thread_worker_completes_with_only_normalized_recognition_result() -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id)
    worker = BrowserThreadOpenWorker(
        worker_id,
        cast(ThreadOpenWorkerControlClient, client),
        cast(ThreadOpenBrowserSessionManager, manager),
    )

    await worker(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.SUCCEEDED
    assert manager.engine.navigation == [f"{BROWSER_FEED_ORIGIN}/@alice/post/post-1"]
    assert manager.engine.verifications == [("/@alice/post/post-1", "alice")]
    assert client.completed == {
        "result_version": 1,
        "target_kind": "THREAD",
        "target_ref": "/@alice/post/post-1",
        "recognized": True,
    }
    assert client.snapshot.checkpoint == {"phase": "THREAD_READY"}


@pytest.mark.parametrize("phase", ("BEFORE_NAVIGATION", "THREAD_READY"))
@pytest.mark.asyncio
async def test_thread_worker_acknowledges_cancel_without_later_browser_work(phase: str) -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    client.cancel_after_checkpoint_phase = phase
    lease_token = client.snapshot.lease_token
    assert lease_token is not None
    manager = _MemorySessionManager(worker_id, account_id)
    worker = BrowserThreadOpenWorker(
        worker_id,
        cast(ThreadOpenWorkerControlClient, client),
        cast(ThreadOpenBrowserSessionManager, manager),
    )

    await worker(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.CANCELLED
    assert client.completed is None
    assert client.failures == []
    assert client.cancellation_acks == [
        (
            client.snapshot.job_id,
            lease_token,
            client.cancel_request_id,
            client.cancel_generation,
            phase,
        )
    ]
    if phase == "BEFORE_NAVIGATION":
        assert manager.engine.navigation == []
        assert manager.engine.verifications == []
    else:
        assert len(manager.engine.navigation) == 1
        assert len(manager.engine.verifications) == 1


@pytest.mark.parametrize(
    ("session_state", "expected_intervention"),
    [
        (BrowserSessionState.LOGIN_REQUIRED, "LOGIN_REQUIRED"),
        (BrowserSessionState.SESSION_EXPIRED, "SESSION_EXPIRED"),
        (BrowserSessionState.CHALLENGE_REQUIRED, "CHALLENGE_REQUIRED"),
    ],
)
@pytest.mark.asyncio
async def test_thread_worker_intervenes_for_known_session_states(
    session_state: BrowserSessionState,
    expected_intervention: str,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id, session_state=session_state)
    worker = BrowserThreadOpenWorker(
        worker_id,
        cast(ThreadOpenWorkerControlClient, client),
        cast(ThreadOpenBrowserSessionManager, manager),
    )

    await worker(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.WAITING_INTERVENTION
    assert client.interventions == [(expected_intervention, "SESSION_REQUIRES_OPERATOR")]
    assert manager.engine.navigation == []
    assert manager.engine.verifications == []


@pytest.mark.parametrize(
    ("operation", "error", "intervention"),
    [
        ("navigate", RemoteSessionStateUncertain, "REMOTE_STATE_UNCERTAIN"),
        ("verify", RemoteSessionStateUncertain, "REMOTE_STATE_UNCERTAIN"),
        ("verify", SessionExpired, "SESSION_EXPIRED"),
        ("verify", ChallengeDetected, "CHALLENGE_REQUIRED"),
    ],
)
@pytest.mark.asyncio
async def test_thread_worker_maps_post_navigation_transitions_to_intervention(
    operation: str,
    error: Callable[[], BrowserAdapterError],
    intervention: str,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id)
    if operation == "navigate":
        manager.engine.navigation_error = error()
    else:
        manager.engine.verify_error = error()
    worker = BrowserThreadOpenWorker(
        worker_id,
        cast(ThreadOpenWorkerControlClient, client),
        cast(ThreadOpenBrowserSessionManager, manager),
    )

    await worker(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.WAITING_INTERVENTION
    assert client.interventions == [(intervention, intervention)]
    assert client.completed is None
    if operation == "navigate":
        assert manager.engine.verifications == []


@pytest.mark.parametrize("lost_at", ("BEFORE_NAVIGATION", "VERIFY_THREAD"))
@pytest.mark.asyncio
async def test_thread_worker_lease_loss_blocks_later_browser_operations(lost_at: str) -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    if lost_at == "BEFORE_NAVIGATION":
        client.lose_lease_on_checkpoint_phase = "BEFORE_NAVIGATION"
    else:
        client.lose_lease_on_renew_call = 2
    manager = _MemorySessionManager(worker_id, account_id)
    worker = BrowserThreadOpenWorker(
        worker_id,
        cast(ThreadOpenWorkerControlClient, client),
        cast(ThreadOpenBrowserSessionManager, manager),
    )

    await worker(client.snapshot)

    if lost_at == "BEFORE_NAVIGATION":
        assert manager.engine.navigation == []
    else:
        assert len(manager.engine.navigation) == 1
    assert manager.engine.verifications == []
    assert client.completed is None


@pytest.mark.asyncio
async def test_thread_worker_rejects_wrong_worker_affinity_before_open() -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    client.context = replace(client.context, worker_id=uuid4())
    manager = _MemorySessionManager(worker_id, account_id)
    worker = BrowserThreadOpenWorker(
        worker_id,
        cast(ThreadOpenWorkerControlClient, client),
        cast(ThreadOpenBrowserSessionManager, manager),
    )

    await worker(client.snapshot)

    assert client.snapshot.status is WorkerJobStatus.FAILED_FINAL
    assert client.failures == [("BROWSER_ACCOUNT_AFFINITY_MISMATCH", False)]
    assert manager.open_count == 0


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("unsupported_capability", "UNSUPPORTED_BROWSER_CAPABILITY"),
        ("retry_safety", "WORKER_JOB_RETRY_SAFETY_MISMATCH"),
        ("invalid_input", "WORKER_JOB_INPUT_INVALID"),
        ("session_unavailable", "BROWSER_SESSION_UNAVAILABLE"),
        ("unsupported_network_route", "BROWSER_NETWORK_ROUTE_UNSUPPORTED"),
    ],
)
@pytest.mark.asyncio
async def test_thread_worker_direct_failure_codes_are_declared(
    failure: str,
    expected_code: str,
) -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    manager = _MemorySessionManager(worker_id, account_id)
    if failure == "unsupported_capability":
        client.snapshot = replace(
            client.snapshot,
            capability_version=THREAD_OPEN_CAPABILITY_VERSION + 1,
        )
    elif failure == "retry_safety":
        client.snapshot = replace(
            client.snapshot,
            retry_safety=WorkerJobRetrySafety.RECONCILIATION_REQUIRED,
        )
    elif failure == "invalid_input":
        client.snapshot = replace(client.snapshot, input_data={"unexpected": "value"})
    elif failure == "session_unavailable":
        manager.open_error = ValueError("session unavailable")
    else:
        manager.open_error = BrowserNetworkRouteUnsupported()
    worker = BrowserThreadOpenWorker(
        worker_id,
        cast(ThreadOpenWorkerControlClient, client),
        cast(ThreadOpenBrowserSessionManager, manager),
    )

    await worker(client.snapshot)

    assert expected_code in THREAD_OPEN_ALLOWED_FAILURE_CODES
    assert client.failures == [(expected_code, expected_code == "BROWSER_SESSION_UNAVAILABLE")]


@pytest.mark.asyncio
async def test_browser_capability_dispatcher_uses_only_registered_handlers() -> None:
    worker_id, account_id = uuid4(), uuid4()
    client = _MemoryControl(worker_id, account_id)
    calls: list[str] = []

    async def thread_open(_: WorkerJobSnapshot) -> None:
        calls.append("thread.open")

    dispatcher = BrowserCapabilityJobDispatcher(
        worker_id,
        client,
        {THREAD_OPEN_CAPABILITY_NAME: thread_open},
    )
    await dispatcher(client.snapshot)
    assert calls == ["thread.open"]

    await dispatcher(replace(client.snapshot, capability_name="unadvertised.capability"))
    assert client.snapshot.status is WorkerJobStatus.FAILED_FINAL
    assert client.failures == [("UNSUPPORTED_BROWSER_CAPABILITY", False)]


class _MemoryControl:
    def __init__(self, worker_id: UUID, account_id: UUID) -> None:
        self.worker_id = worker_id
        self.account_id = account_id
        self.snapshot = WorkerJobSnapshot(
            job_id=uuid4(),
            capability_name=THREAD_OPEN_CAPABILITY_NAME,
            capability_version=THREAD_OPEN_CAPABILITY_VERSION,
            status=WorkerJobStatus.RUNNING,
            account_id=account_id,
            assigned_worker_id=worker_id,
            lease_worker_id=worker_id,
            lease_token=uuid4(),
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            retry_safety=WorkerJobRetrySafety.SAFE_TO_RETRY,
            checkpoint=None,
            input_data={"thread_ref": "/@alice/post/post-1/"},
        )
        self.context = WorkerAccountContext(account_id, worker_id, "profile-main", None)
        self.interventions: list[tuple[str, str]] = []
        self.failures: list[tuple[str, bool]] = []
        self.completed: dict[str, object] | None = None
        self.renew_calls = 0
        self.lose_lease_on_renew_call: int | None = None
        self.lose_lease_on_checkpoint_phase: str | None = None
        self.cancel_after_checkpoint_phase: str | None = None
        self.cancel_request_id = uuid4()
        self.cancel_generation = 7
        self.cancellation_acks: list[tuple[UUID, UUID, UUID, int, str]] = []

    async def account_context(self, account_id: UUID) -> WorkerAccountContext:
        assert account_id == self.account_id
        return self.context

    async def renew_job(self, job_id: UUID, lease_token: UUID) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.renew_calls += 1
        if self.renew_calls == self.lose_lease_on_renew_call:
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")
        return self.snapshot

    async def checkpoint_job(
        self, job_id: UUID, lease_token: UUID, checkpoint: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        if checkpoint.get("phase") == self.lose_lease_on_checkpoint_phase:
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")
        self.snapshot = replace(self.snapshot, checkpoint=checkpoint)
        if checkpoint.get("phase") == self.cancel_after_checkpoint_phase:
            self.snapshot = replace(
                self.snapshot,
                pending_cancel=WorkerJobCancelSnapshot(
                    self.cancel_request_id,
                    self.cancel_generation,
                    "OPERATOR_REQUESTED",
                    datetime.now(UTC),
                ),
            )
        return self.snapshot

    async def cancel_job(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        cancel_request_id: UUID,
        generation: int,
        checkpoint_phase: str,
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.cancellation_acks.append(
            (job_id, lease_token, cancel_request_id, generation, checkpoint_phase)
        )
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.CANCELLED,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
            pending_cancel=None,
        )
        return self.snapshot

    async def complete_job(
        self, job_id: UUID, lease_token: UUID, result: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.completed = result
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
        _ = outcome_ambiguous
        self.failures.append((error_code, retryable))
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


class _MemorySessionManager:
    def __init__(
        self,
        worker_id: UUID,
        account_id: UUID,
        *,
        session_state: BrowserSessionState = BrowserSessionState.AUTHENTICATED,
        open_error: Exception | None = None,
    ) -> None:
        now = datetime.now(UTC)
        self.context = WorkerAccountContext(account_id, worker_id, "profile-main", None)
        self.opened = BrowserSessionOpenResult(
            LocalSessionState(
                account_id,
                "profile-main",
                uuid4(),
                session_state,
                1,
                now,
            ),
            WorkerNetworkRoute(
                account_id,
                BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
            ),
        )
        self.engine = _MemoryEngineSession()
        self.session = WorkerBrowserSession(
            account_id,
            self.opened,
            self.engine,
            self.transition,
            self.close,
            None,
            lambda _: None,
        )
        self.open_count = 0
        self.closed_accounts: list[UUID] = []
        self.open_error = open_error

    async def open(self, context: WorkerAccountContext) -> BrowserSessionOpenResult:
        self.open_count += 1
        self.context = context
        if self.open_error is not None:
            raise self.open_error
        return self.opened

    def browser_session(self, account_id: UUID) -> WorkerBrowserSession | None:
        return self.session if account_id == self.context.account_id else None

    async def transition(self, account_id: UUID, state: BrowserSessionState) -> LocalSessionState:
        assert account_id == self.context.account_id
        self.opened = replace(self.opened, state=replace(self.opened.state, state=state))
        return self.opened.state

    async def close(self, account_id: UUID) -> LocalSessionState:
        self.closed_accounts.append(account_id)
        return await self.transition(account_id, BrowserSessionState.STOPPED)


class _MemoryEngineSession:
    def __init__(self) -> None:
        self.navigation: list[str] = []
        self.verifications: list[tuple[str, str]] = []
        self.navigation_error: BrowserAdapterError | None = None
        self.verify_error: BrowserAdapterError | None = None

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        assert allowed_origins == frozenset({BROWSER_FEED_ORIGIN})
        if self.navigation_error is not None:
            raise self.navigation_error
        self.navigation.append(url)

    async def inspect_surface(self) -> BrowserSurface:
        return BrowserSurface("worker.synthetic", "1", "AUTHENTICATED", True)

    async def verify_thread_target(self, *, target_ref: str, author_username: str) -> None:
        if self.verify_error is not None:
            raise self.verify_error
        self.verifications.append((target_ref, author_username))

    async def close(self) -> None:
        return None
