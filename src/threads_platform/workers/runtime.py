import asyncio
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from threads_platform.application.ports.worker_agent import (
    LocalSessionState,
    WorkerAgentPresence,
    WorkerControlClient,
    WorkerControlClientError,
    WorkerIdentityStore,
    WorkerJobHandler,
    WorkerJobSnapshot,
    WorkerLocalState,
    WorkerProcessLock,
    WorkerReconcileHandler,
)
from threads_platform.domain.workers import BrowserSessionState, WorkerStatus
from threads_platform.workers.key_store import WorkerKeyStore
from threads_platform.workers.sessions import BrowserSessionManager, BrowserSessionOpenResult


@dataclass(frozen=True, slots=True)
class WorkerAgentConfig:
    control_plane_url: str
    display_name: str
    agent_version: str
    enrollment_code: str | None = field(default=None, repr=False)
    hostname: str = field(default_factory=socket.gethostname)
    max_concurrent_jobs: int = 1
    max_browser_sessions: int = 1
    capabilities: tuple[tuple[str, int], ...] = ()
    heartbeat_interval: timedelta = timedelta(seconds=30)
    poll_interval: timedelta = timedelta(seconds=5)
    reconnect_initial_backoff: timedelta = timedelta(seconds=1)
    reconnect_max_backoff: timedelta = timedelta(seconds=60)

    def __post_init__(self) -> None:
        if not self.control_plane_url.startswith("https://"):
            raise ValueError("worker Control Plane URL must use HTTPS")
        if not self.display_name.strip() or not self.agent_version.strip():
            raise ValueError("worker display name and agent version are required")
        if self.max_concurrent_jobs < 1 or self.max_browser_sessions < 1:
            raise ValueError("worker capacities must be positive")
        if self.heartbeat_interval.total_seconds() <= 0 or self.poll_interval.total_seconds() <= 0:
            raise ValueError("worker heartbeat and poll intervals must be positive")
        if (
            self.reconnect_initial_backoff.total_seconds() <= 0
            or self.reconnect_max_backoff < self.reconnect_initial_backoff
        ):
            raise ValueError("worker reconnect backoff bounds are invalid")
        if any(not name.strip() or version < 1 for name, version in self.capabilities):
            raise ValueError("worker capabilities require names and positive versions")


class WorkerAgent:
    """Strict-online process lifecycle and C1 Control Plane reconnect foundation."""

    def __init__(
        self,
        config: WorkerAgentConfig,
        identity_store: WorkerIdentityStore,
        key_store: WorkerKeyStore,
        state_store: WorkerLocalState,
        process_lock: WorkerProcessLock,
        control_client: WorkerControlClient,
        *,
        job_handler: WorkerJobHandler | None = None,
        reconcile_handler: WorkerReconcileHandler | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._identity_store = identity_store
        self._key_store = key_store
        self._state_store = state_store
        self._process_lock = process_lock
        self._control_client = control_client
        self._job_handler = job_handler
        self._reconcile_handler = reconcile_handler
        self._clock = clock or (lambda: datetime.now(UTC))
        self._worker_id: UUID | None = None
        self._identity = None
        self._connected = False
        self._presence: WorkerAgentPresence | None = None
        self._last_heartbeat_at: datetime | None = None
        self._initialized = False
        self._session_managers: dict[UUID, BrowserSessionManager] = {}

    @property
    def worker_id(self) -> UUID | None:
        return self._worker_id

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def status(self) -> WorkerStatus:
        return self._presence.status if self._presence is not None else WorkerStatus.OFFLINE

    @property
    def may_claim(self) -> bool:
        return (
            self._connected
            and self.status is WorkerStatus.ONLINE
            and bool(self._config.capabilities)
            and self._job_handler is not None
        )

    async def connect_once(self) -> WorkerAgentPresence:
        await self._initialize()
        if self._worker_id is None or self._identity is None:
            raise RuntimeError("worker identity was not initialized")
        await self._control_client.authenticate(
            self._worker_id,
            self._identity,
            enrollment_pending=self._identity_store.enrollment_pending(),
            enrollment_code=self._config.enrollment_code,
            display_name=self._config.display_name,
            hostname=self._config.hostname,
            max_concurrent_jobs=self._config.max_concurrent_jobs,
            max_browser_sessions=self._config.max_browser_sessions,
        )
        self._identity_store.mark_enrolled()
        presence = await self._control_client.hello(
            self._worker_id,
            agent_version=self._config.agent_version,
            capabilities=self._config.capabilities,
            max_concurrent_jobs=self._config.max_concurrent_jobs,
            max_browser_sessions=self._config.max_browser_sessions,
            active_browser_sessions=self._state_store.active_session_count(),
        )
        self._presence = presence
        self._connected = True
        self._last_heartbeat_at = self._clock()
        if presence.protocol_compatible:
            jobs = await self._control_client.reconcile()
            if self._reconcile_handler is not None:
                await self._reconcile_handler(jobs)
            await self.flush_session_reports()
        return presence

    async def heartbeat_once(self) -> WorkerAgentPresence:
        if not self._connected:
            raise WorkerControlClientError("WORKER_OFFLINE")
        await self._reauthenticate_if_needed()
        try:
            presence = await self._control_client.heartbeat(
                self._state_store.active_session_count()
            )
        except WorkerControlClientError:
            self._connected = False
            self._presence = None
            raise
        self._presence = presence
        self._last_heartbeat_at = self._clock()
        await self.flush_session_reports()
        return presence

    async def tick(self) -> WorkerJobSnapshot | None:
        if not self._connected:
            return None
        now = self._clock()
        if (
            self._last_heartbeat_at is None
            or now - self._last_heartbeat_at >= self._config.heartbeat_interval
        ):
            presence = await self.heartbeat_once()
            if presence.status is not WorkerStatus.ONLINE:
                return None
        if not self.may_claim:
            return None
        job = await self._control_client.claim_next()
        if job is None:
            return None
        if job.assigned_worker_id not in {None, self._worker_id}:
            raise WorkerControlClientError("CLAIMED_JOB_WORKER_MISMATCH")
        handler = self._job_handler
        if handler is None:
            return None
        await handler(job)
        return job

    async def flush_session_reports(self) -> None:
        if not self._connected:
            return
        for report in self._state_store.pending_session_reports():
            await self._control_client.report_session_state(report)
            self._state_store.mark_session_report_delivered(report)

    async def open_browser_session(
        self, account_id: UUID, session_manager: BrowserSessionManager
    ) -> BrowserSessionOpenResult:
        if not self._connected or self.status is not WorkerStatus.ONLINE:
            raise WorkerControlClientError("WORKER_OFFLINE")
        if self._worker_id is None:
            raise WorkerControlClientError("WORKER_IDENTITY_UNAVAILABLE")
        context = await self._control_client.account_context(account_id)
        if context.worker_id != self._worker_id or context.account_id != account_id:
            raise WorkerControlClientError("ACCOUNT_WORKER_AFFINITY_MISMATCH")
        opened = await session_manager.open(context)
        self._session_managers[account_id] = session_manager
        await self.flush_session_reports()
        return opened

    async def close_browser_session(self, account_id: UUID) -> None:
        manager = self._session_managers.get(account_id)
        if manager is None:
            return
        await manager.close(account_id)
        self._session_managers.pop(account_id, None)
        await self.flush_session_reports()

    async def transition_browser_session(
        self, account_id: UUID, state: BrowserSessionState
    ) -> LocalSessionState:
        manager = self._session_managers.get(account_id)
        if manager is None:
            raise WorkerControlClientError("BROWSER_SESSION_NOT_OPEN")
        session = await manager.transition(account_id, state)
        await self.flush_session_reports()
        return session

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            self._process_lock.acquire()
            await self._initialize()
            backoff = self._config.reconnect_initial_backoff
            while not stop_event.is_set():
                try:
                    if not self._connected:
                        await self.connect_once()
                        backoff = self._config.reconnect_initial_backoff
                    if self.status in {WorkerStatus.DRAINING, WorkerStatus.DISABLED}:
                        break
                    await self.tick()
                    if self.status in {WorkerStatus.DRAINING, WorkerStatus.DISABLED}:
                        break
                    await _wait_or_stop(stop_event, self._config.poll_interval.total_seconds())
                except WorkerControlClientError:
                    self._connected = False
                    self._presence = None
                    await _wait_or_stop(stop_event, backoff.total_seconds())
                    backoff = min(backoff * 2, self._config.reconnect_max_backoff)
        finally:
            try:
                await self._close_sessions()
            finally:
                self._connected = False
                self._process_lock.release()
                await self._control_client.aclose()

    async def close(self) -> None:
        try:
            await self._close_sessions()
        finally:
            self._connected = False
            self._process_lock.release()
            await self._control_client.aclose()

    async def _close_sessions(self) -> None:
        for account_id, manager in tuple(self._session_managers.items()):
            await manager.close(account_id)
            self._session_managers.pop(account_id, None)
            if self._connected:
                try:
                    await self.flush_session_reports()
                except WorkerControlClientError:
                    break

    async def _initialize(self) -> None:
        if self._initialized:
            return
        if not self._process_lock.held:
            self._process_lock.acquire()
        self._worker_id = self._identity_store.load_or_create()
        self._identity = self._key_store.load_or_create(self._worker_id)
        self._state_store.recover_after_restart(updated_at=self._clock())
        self._initialized = True

    async def _reauthenticate_if_needed(self) -> None:
        expires_at = self._control_client.access_token_expires_at
        if expires_at is None or expires_at - self._clock() > timedelta(minutes=1):
            return
        if self._worker_id is None or self._identity is None:
            raise WorkerControlClientError("WORKER_IDENTITY_UNAVAILABLE")
        await self._control_client.authenticate(
            self._worker_id,
            self._identity,
            enrollment_pending=False,
            enrollment_code=None,
            display_name=self._config.display_name,
            hostname=self._config.hostname,
            max_concurrent_jobs=self._config.max_concurrent_jobs,
            max_browser_sessions=self._config.max_browser_sessions,
        )


async def _wait_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        return
