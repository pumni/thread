from uuid import UUID

from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.ports.worker_agent import WorkerAccountContext
from threads_platform.domain.time import normalize_utc
from threads_platform.domain.workers import (
    BrowserSessionState,
    WorkerAccountSession,
    WorkerAuditEvent,
)


class WorkerSessionControlError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WorkerSessionService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or SystemClock()

    async def account_context(self, worker_id: UUID, account_id: UUID) -> WorkerAccountContext:
        async with self._unit_of_work_factory() as unit_of_work:
            if await unit_of_work.accounts.get_for_update(account_id) is None:
                raise WorkerSessionControlError("ACCOUNT_NOT_FOUND")
            assignment = await unit_of_work.assignments.get_active(account_id)
            if assignment is None or assignment.worker_id != worker_id:
                raise WorkerSessionControlError("ACCOUNT_WORKER_AFFINITY_MISMATCH")
            network_profile = (
                await unit_of_work.network_profiles.get(account_id, assignment.network_profile_id)
                if assignment.network_profile_id is not None
                else None
            )
            if assignment.network_profile_id is not None and network_profile is None:
                raise WorkerSessionControlError("NETWORK_PROFILE_NOT_FOUND")
        return WorkerAccountContext(
            account_id=account_id,
            worker_id=worker_id,
            profile_ref=assignment.profile_ref,
            network_profile=network_profile,
        )

    async def report_state(
        self,
        worker_id: UUID,
        *,
        account_id: UUID,
        profile_ref: str,
        session_id: UUID,
        state: BrowserSessionState,
        revision: int,
    ) -> WorkerAccountSession:
        now = normalize_utc(self._clock.now())
        if revision < 1:
            raise WorkerSessionControlError("SESSION_REVISION_INVALID")
        async with self._unit_of_work_factory() as unit_of_work:
            if await unit_of_work.accounts.get_for_update(account_id) is None:
                raise WorkerSessionControlError("ACCOUNT_NOT_FOUND")
            assignment = await unit_of_work.assignments.get_active(account_id)
            if (
                assignment is None
                or assignment.worker_id != worker_id
                or assignment.profile_ref != profile_ref
            ):
                raise WorkerSessionControlError("ACCOUNT_WORKER_AFFINITY_MISMATCH")
            current = await unit_of_work.worker_account_sessions.get_for_update(account_id)
            if current is None:
                session = WorkerAccountSession(
                    account_id,
                    worker_id,
                    profile_ref,
                    session_id,
                    state,
                    revision,
                    now,
                )
                await unit_of_work.worker_account_sessions.add(session)
            else:
                same_assignment = (
                    current.worker_id == worker_id and current.profile_ref == profile_ref
                )
                if not same_assignment and current.state not in {
                    BrowserSessionState.ERROR,
                    BrowserSessionState.SESSION_EXPIRED,
                    BrowserSessionState.STOPPED,
                }:
                    raise WorkerSessionControlError("SESSION_ALREADY_OWNED")
                if same_assignment and current.session_id == session_id:
                    if revision < current.revision:
                        raise WorkerSessionControlError("SESSION_REPORT_STALE")
                    if revision == current.revision:
                        if current.state is state:
                            return current
                        raise WorkerSessionControlError("SESSION_REPORT_STALE")
                elif current.state not in {
                    BrowserSessionState.ERROR,
                    BrowserSessionState.SESSION_EXPIRED,
                    BrowserSessionState.STOPPED,
                }:
                    raise WorkerSessionControlError("SESSION_ALREADY_OWNED")
                current.worker_id = worker_id
                current.profile_ref = profile_ref
                current.session_id = session_id
                current.state = state
                current.revision = revision
                current.updated_at = now
                session = current
                await unit_of_work.worker_account_sessions.update(session)
            await unit_of_work.worker_security.add_audit_event(
                WorkerAuditEvent(
                    event_type="worker.session.state_reported",
                    worker_id=worker_id,
                    detail_code=state.value,
                    created_at=now,
                )
            )
        return session
