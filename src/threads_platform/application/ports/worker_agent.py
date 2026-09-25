from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import (
    BrowserSessionState,
    NetworkProfile,
    WorkerStatus,
)
from threads_platform.workers.key_store import WorkerDeviceIdentity


class WorkerControlClientError(RuntimeError):
    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class WorkerAgentPresence:
    worker_id: UUID
    status: WorkerStatus
    protocol_compatible: bool
    max_browser_sessions: int
    active_browser_sessions: int


@dataclass(frozen=True, slots=True)
class WorkerJobSnapshot:
    job_id: UUID
    capability_name: str
    capability_version: int
    status: WorkerJobStatus
    account_id: UUID | None
    assigned_worker_id: UUID | None
    lease_token: UUID | None
    lease_expires_at: datetime | None
    checkpoint: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class WorkerAccountContext:
    account_id: UUID
    worker_id: UUID
    profile_ref: str
    network_profile: NetworkProfile | None


@dataclass(frozen=True, slots=True)
class LocalSessionState:
    account_id: UUID
    profile_ref: str
    session_id: UUID
    state: BrowserSessionState
    revision: int
    updated_at: datetime

    @property
    def requires_intervention(self) -> bool:
        return self.state.requires_intervention


@dataclass(frozen=True, slots=True)
class LocalRecoveryEntry:
    worker_job_id: UUID
    account_id: UUID
    profile_ref: str
    phase: str
    updated_at: datetime


class WorkerLocalState(Protocol):
    def active_session_count(self) -> int: ...

    def get_session(self, account_id: UUID) -> LocalSessionState | None: ...

    def reserve_session(
        self,
        *,
        worker_id: UUID,
        account_id: UUID,
        profile_ref: str,
        session_id: UUID,
        maximum: int,
    ) -> None: ...

    def release_session(self, account_id: UUID, session_id: UUID) -> None: ...

    def record_session_state(
        self,
        *,
        worker_id: UUID,
        account_id: UUID,
        profile_ref: str,
        session_id: UUID,
        state: BrowserSessionState,
        updated_at: datetime,
    ) -> LocalSessionState: ...

    def recover_after_restart(self, *, updated_at: datetime) -> list[LocalSessionState]: ...

    def pending_session_reports(self) -> list[LocalSessionState]: ...

    def mark_session_report_delivered(self, report: LocalSessionState) -> None: ...

    def save_recovery_entry(self, entry: LocalRecoveryEntry) -> None: ...

    def recovery_entries(self) -> list[LocalRecoveryEntry]: ...


class ManagedProfileDirectory(Protocol):
    def ensure_profile(self, worker_id: UUID, account_id: UUID, profile_ref: str) -> None: ...


class WorkerIdentityStore(Protocol):
    def load_or_create(self) -> UUID: ...

    def enrollment_pending(self) -> bool: ...

    def mark_enrolled(self) -> None: ...


class WorkerProcessLock(Protocol):
    @property
    def held(self) -> bool: ...

    def acquire(self) -> None: ...

    def release(self) -> None: ...


class WorkerControlClient(Protocol):
    @property
    def access_token_expires_at(self) -> datetime | None: ...

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
    ) -> None: ...

    async def hello(
        self,
        worker_id: UUID,
        *,
        agent_version: str,
        capabilities: Sequence[tuple[str, int]],
        max_concurrent_jobs: int,
        max_browser_sessions: int,
        active_browser_sessions: int,
    ) -> WorkerAgentPresence: ...

    async def heartbeat(self, active_browser_sessions: int) -> WorkerAgentPresence: ...

    async def reconcile(self) -> tuple[WorkerJobSnapshot, ...]: ...

    async def claim_next(self) -> WorkerJobSnapshot | None: ...

    async def account_context(self, account_id: UUID) -> WorkerAccountContext: ...

    async def report_session_state(self, session: LocalSessionState) -> None: ...

    async def aclose(self) -> None: ...


WorkerJobHandler = Callable[[WorkerJobSnapshot], Awaitable[None]]
WorkerReconcileHandler = Callable[[Sequence[WorkerJobSnapshot]], Awaitable[None]]
