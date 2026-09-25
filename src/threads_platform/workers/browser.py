from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from threads_platform.application.ports.worker_agent import (
    LocalRecoveryEntry,
    LocalSessionState,
    WorkerAccountContext,
    WorkerControlClientError,
    WorkerJobControlClient,
    WorkerJobSnapshot,
    WorkerLocalState,
)
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety, WorkerJobStatus
from threads_platform.domain.workers import BrowserSessionState
from threads_platform.workers.sessions import (
    BrowserSessionManager,
    BrowserSessionOpenResult,
    NetworkRoute,
    ProxyCredentialProvider,
    ProxyCredentials,
)

SUPPORTED_UI_CONTRACT_ID = "worker.synthetic"
SUPPORTED_UI_CONTRACT_VERSION = 1
_SAFE_CHECKPOINT_FIELDS = frozenset({"phase", "contract_id", "contract_version", "reason_code"})
_RECOVERY_AMBIGUOUS_PHASES = frozenset({"MUTATION_STARTED", "MUTATION_CONFIRMED"})
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ResultT = TypeVar("_ResultT")


class BrowserAdapterError(RuntimeError):
    def __init__(self, code: str) -> None:
        if not _CODE.fullmatch(code):
            raise ValueError("browser adapter error code must be bounded and safe")
        super().__init__(code)
        self.code = code


class BrowserContractError(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_CONTRACT_MISMATCH")


class LocatorNotFound(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_REQUIRED_MARKER_NOT_FOUND")


class SessionExpired(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("SESSION_EXPIRED")


class ChallengeDetected(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("CHALLENGE_REQUIRED")


class NavigationTimeout(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_NAVIGATION_TIMEOUT")


class ActionOutcomeAmbiguous(BrowserAdapterError):
    def __init__(self, *, intervention_recorded: bool) -> None:
        super().__init__("ACTION_OUTCOME_AMBIGUOUS")
        self.intervention_recorded = intervention_recorded


class MediaUploadFailed(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("MEDIA_UPLOAD_FAILED")


class UnsupportedUIState(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("UNSUPPORTED_UI_STATE")


class BrowserProcessCrashed(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_PROCESS_CRASHED")


class BrowserRuntimeUnavailable(BrowserAdapterError):
    def __init__(self, code: str = "BROWSER_RUNTIME_UNAVAILABLE") -> None:
        super().__init__(code)


class BrowserNetworkRouteUnsupported(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_NETWORK_ROUTE_UNSUPPORTED")


class BrowserAccountAffinityMismatch(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("BROWSER_ACCOUNT_AFFINITY_MISMATCH")


class WorkerJobLeaseLost(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("WORKER_JOB_LEASE_LOST")


class WorkerJobRetrySafetyViolation(BrowserAdapterError):
    def __init__(self) -> None:
        super().__init__("WORKER_JOB_RETRY_SAFETY_MISMATCH")


class BrowserSurfaceState(StrEnum):
    AUTHENTICATED = "AUTHENTICATED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"


@dataclass(frozen=True, slots=True)
class BrowserSurface:
    contract_id: str | None
    contract_version: str | None
    session_state: str | None
    required_root_present: bool


@dataclass(frozen=True, slots=True)
class BrowserLaunchRequest:
    worker_id: UUID
    account_id: UUID
    profile_ref: str
    profile_directory: Path
    network_route: NetworkRoute
    proxy_credentials: ProxyCredentials | None = field(default=None, repr=False)
    headless: bool = False


class BrowserEngineSession(Protocol):
    async def navigate(self, url: str) -> None: ...

    async def inspect_surface(self) -> BrowserSurface: ...

    async def close(self) -> None: ...


class BrowserEngine(Protocol):
    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession: ...


class ManagedProfilePathResolver(Protocol):
    def resolve(self, worker_id: UUID, account_id: UUID, profile_ref: str) -> Path: ...


SessionTransition = Callable[[UUID, BrowserSessionState], Awaitable[LocalSessionState]]
SessionClose = Callable[[UUID], Awaitable[LocalSessionState]]
IrreversibleOperation = Callable[[], Awaitable[_ResultT]]


@dataclass(frozen=True, slots=True)
class BrowserNavigationPolicy:
    allowed_origins: frozenset[str]

    def validate(self, url: str) -> None:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if (
            parsed.scheme != "http"
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise UnsupportedUIState()
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = hostname.lower() == "localhost"
        if not is_loopback:
            raise UnsupportedUIState()
        try:
            port = parsed.port
        except ValueError as error:
            raise UnsupportedUIState() from error
        host_for_origin = hostname.lower()
        if ":" in host_for_origin:
            host_for_origin = f"[{host_for_origin}]"
        origin = f"{parsed.scheme.lower()}://{host_for_origin}"
        if port is not None:
            origin = f"{origin}:{port}"
        if origin not in self.allowed_origins:
            raise UnsupportedUIState()


def classify_browser_surface(surface: BrowserSurface) -> BrowserSurfaceState:
    if not surface.required_root_present:
        raise LocatorNotFound()
    if surface.contract_id is None or surface.contract_version is None:
        raise BrowserContractError()
    if surface.contract_id != SUPPORTED_UI_CONTRACT_ID or surface.contract_version != str(
        SUPPORTED_UI_CONTRACT_VERSION
    ):
        raise UnsupportedUIState()
    if surface.session_state is None:
        raise LocatorNotFound()
    try:
        state = BrowserSurfaceState(surface.session_state)
    except ValueError as error:
        raise UnsupportedUIState() from error
    if state is BrowserSurfaceState.SESSION_EXPIRED:
        raise SessionExpired()
    if state is BrowserSurfaceState.CHALLENGE_REQUIRED:
        raise ChallengeDetected()
    return state


class WorkerJobExecution:
    """Fenced HTTPS control for one claimed WorkerJob and its mutation boundary."""

    def __init__(
        self,
        snapshot: WorkerJobSnapshot,
        worker_id: UUID,
        control_client: WorkerJobControlClient,
        *,
        local_state: WorkerLocalState | None = None,
        profile_ref: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._control_client = control_client
        self._local_state = local_state
        self._profile_ref = profile_ref
        self._clock = clock or (lambda: datetime.now(UTC))
        self._snapshot = snapshot
        self._validate_snapshot(snapshot)
        if local_state is not None and snapshot.account_id is None:
            raise ValueError("local WorkerJob recovery requires an account-affine job")
        if local_state is not None and (profile_ref is None or not profile_ref.strip()):
            raise ValueError("local WorkerJob recovery requires a logical profile_ref")

    @property
    def snapshot(self) -> WorkerJobSnapshot:
        return self._snapshot

    @property
    def account_id(self) -> UUID | None:
        return self._snapshot.account_id

    @property
    def mutation_may_have_started(self) -> bool:
        entry = self._recovery_entry()
        return entry is not None and entry.phase in _RECOVERY_AMBIGUOUS_PHASES

    async def renew(self) -> WorkerJobSnapshot:
        try:
            snapshot = await self._control_client.renew_job(
                self._snapshot.job_id, self._lease_token()
            )
        except WorkerControlClientError:
            raise WorkerJobLeaseLost() from None
        self._accept_current_snapshot(snapshot)
        return self._snapshot

    async def checkpoint(self, checkpoint: dict[str, object]) -> WorkerJobSnapshot:
        _validate_checkpoint(checkpoint)
        try:
            snapshot = await self._control_client.checkpoint_job(
                self._snapshot.job_id, self._lease_token(), checkpoint
            )
        except WorkerControlClientError as error:
            if error.code == "WORKER_JOB_LEASE_LOST":
                raise WorkerJobLeaseLost() from None
            raise
        self._accept_current_snapshot(snapshot)
        return self._snapshot

    async def complete(self, result: dict[str, object]) -> WorkerJobSnapshot:
        try:
            snapshot = await self._control_client.complete_job(
                self._snapshot.job_id, self._lease_token(), result
            )
        except WorkerControlClientError as error:
            if error.code == "WORKER_JOB_LEASE_LOST":
                raise WorkerJobLeaseLost() from None
            raise
        if (
            snapshot.job_id != self._snapshot.job_id
            or snapshot.status is not WorkerJobStatus.SUCCEEDED
        ):
            raise WorkerJobLeaseLost()
        self._snapshot = snapshot
        self._clear_recovery_entry()
        return snapshot

    async def fail(
        self,
        *,
        error_code: str,
        retryable: bool,
        outcome_ambiguous: bool = False,
    ) -> WorkerJobSnapshot:
        if not _CODE.fullmatch(error_code):
            raise ValueError("WorkerJob error code must be bounded and safe")
        try:
            snapshot = await self._control_client.fail_job(
                self._snapshot.job_id,
                self._lease_token(),
                error_code=error_code,
                retryable=retryable,
                outcome_ambiguous=outcome_ambiguous,
            )
        except WorkerControlClientError as error:
            if error.code == "WORKER_JOB_LEASE_LOST":
                raise WorkerJobLeaseLost() from None
            raise
        if snapshot.job_id != self._snapshot.job_id:
            raise WorkerJobLeaseLost()
        self._snapshot = snapshot
        if snapshot.status is not WorkerJobStatus.RUNNING:
            self._clear_recovery_entry()
        return snapshot

    async def request_intervention(
        self, intervention_type: str, detail_code: str
    ) -> WorkerJobSnapshot:
        allowed_types = {
            "LOGIN_REQUIRED",
            "SESSION_EXPIRED",
            "CHALLENGE_REQUIRED",
            "OPERATOR_CONFIRMATION_REQUIRED",
            "AMBIGUOUS_OUTCOME",
            "REMOTE_STATE_UNCERTAIN",
        }
        if intervention_type not in allowed_types or not _CODE.fullmatch(detail_code):
            raise ValueError("WorkerJob intervention details must use bounded codes")
        try:
            snapshot = await self._control_client.request_intervention(
                self._snapshot.job_id,
                self._lease_token(),
                intervention_type=intervention_type,
                detail_code=detail_code,
            )
        except WorkerControlClientError as error:
            if error.code == "WORKER_JOB_LEASE_LOST":
                raise WorkerJobLeaseLost() from None
            raise
        if snapshot.job_id != self._snapshot.job_id:
            raise WorkerJobLeaseLost()
        self._snapshot = snapshot
        if snapshot.status is WorkerJobStatus.WAITING_INTERVENTION:
            self._clear_recovery_entry()
        return snapshot

    async def execute_irreversible_boundary(
        self, operation: IrreversibleOperation[_ResultT]
    ) -> _ResultT:
        """Fence and journal exactly one irreversible operation; never retry it here."""
        if self.mutation_may_have_started:
            recorded = await self._request_ambiguous_intervention()
            raise ActionOutcomeAmbiguous(intervention_recorded=recorded)
        if self._snapshot.retry_safety is not WorkerJobRetrySafety.RECONCILIATION_REQUIRED:
            raise WorkerJobRetrySafetyViolation()
        if self._local_state is not None:
            account_id = self._snapshot.account_id
            profile_ref = self._profile_ref
            if account_id is None or profile_ref is None:
                raise WorkerJobLeaseLost()
            self._save_recovery_entry(account_id, profile_ref, "MUTATION_PENDING")
        await self.checkpoint({"phase": "MUTATION_PENDING"})
        await self.renew()
        if self._local_state is not None:
            account_id = self._snapshot.account_id
            profile_ref = self._profile_ref
            if account_id is None or profile_ref is None:
                raise WorkerJobLeaseLost()
            self._save_recovery_entry(account_id, profile_ref, "MUTATION_STARTED")
        try:
            result = await operation()
        except Exception:
            recorded = await self._request_ambiguous_intervention()
            raise ActionOutcomeAmbiguous(intervention_recorded=recorded) from None
        if self._local_state is not None:
            account_id = self._snapshot.account_id
            profile_ref = self._profile_ref
            if account_id is None or profile_ref is None:
                raise ActionOutcomeAmbiguous(intervention_recorded=False)
            self._save_recovery_entry(account_id, profile_ref, "MUTATION_CONFIRMED")
        try:
            await self.checkpoint({"phase": "MUTATION_CONFIRMED"})
        except WorkerControlClientError, WorkerJobLeaseLost:
            recorded = await self._request_ambiguous_intervention()
            raise ActionOutcomeAmbiguous(intervention_recorded=recorded) from None
        return result

    async def _request_ambiguous_intervention(self) -> bool:
        try:
            await self.request_intervention(
                "AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION"
            )
        except WorkerControlClientError:
            return False
        except WorkerJobLeaseLost:
            return False
        return self._snapshot.status is WorkerJobStatus.WAITING_INTERVENTION

    async def report_ambiguous_outcome(self, detail_code: str) -> bool:
        if not _CODE.fullmatch(detail_code):
            raise ValueError("reconciliation reason must be a bounded code")
        try:
            await self.request_intervention("AMBIGUOUS_OUTCOME", detail_code)
        except WorkerControlClientError, WorkerJobLeaseLost:
            return False
        return self._snapshot.status is WorkerJobStatus.WAITING_INTERVENTION

    def _validate_snapshot(self, snapshot: WorkerJobSnapshot) -> None:
        if (
            snapshot.status is not WorkerJobStatus.RUNNING
            or snapshot.lease_worker_id != self._worker_id
            or snapshot.lease_token is None
            or snapshot.lease_expires_at is None
            or snapshot.lease_expires_at <= self._clock()
            or snapshot.assigned_worker_id not in {None, self._worker_id}
        ):
            raise WorkerJobLeaseLost()

    def _accept_current_snapshot(self, snapshot: WorkerJobSnapshot) -> None:
        if (
            snapshot.job_id != self._snapshot.job_id
            or snapshot.lease_token != self._snapshot.lease_token
        ):
            raise WorkerJobLeaseLost()
        self._validate_snapshot(snapshot)
        self._snapshot = snapshot

    def _lease_token(self) -> UUID:
        if self._snapshot.lease_token is None:
            raise WorkerJobLeaseLost()
        return self._snapshot.lease_token

    def _recovery_entry(self) -> LocalRecoveryEntry | None:
        if self._local_state is None:
            return None
        for entry in self._local_state.recovery_entries():
            if entry.worker_job_id == self._snapshot.job_id:
                if (
                    entry.account_id != self._snapshot.account_id
                    or entry.profile_ref != self._profile_ref
                ):
                    raise WorkerJobLeaseLost()
                return entry
        return None

    def _save_recovery_entry(self, account_id: UUID, profile_ref: str, phase: str) -> None:
        if self._local_state is None:
            return
        self._local_state.save_recovery_entry(
            LocalRecoveryEntry(
                worker_job_id=self._snapshot.job_id,
                account_id=account_id,
                profile_ref=profile_ref,
                phase=phase,
                updated_at=self._clock(),
            )
        )

    def _clear_recovery_entry(self) -> None:
        if self._local_state is not None:
            self._local_state.clear_recovery_entry(self._snapshot.job_id)


class WorkerJobReconnectRecovery:
    """Moves uncertain locally journaled mutations to durable WorkerJob intervention."""

    def __init__(
        self,
        worker_id: UUID,
        control_client: WorkerJobControlClient,
        local_state: WorkerLocalState,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._control_client = control_client
        self._local_state = local_state
        self._clock = clock or (lambda: datetime.now(UTC))

    async def reconcile(self, jobs: Sequence[WorkerJobSnapshot]) -> None:
        jobs_by_id = {job.job_id: job for job in jobs}
        for entry in self._local_state.recovery_entries():
            job = jobs_by_id.get(entry.worker_job_id)
            if job is None:
                continue
            if job.status is not WorkerJobStatus.RUNNING:
                self._local_state.clear_recovery_entry(job.job_id)
                continue
            if entry.phase not in _RECOVERY_AMBIGUOUS_PHASES:
                continue
            execution = WorkerJobExecution(
                job,
                self._worker_id,
                self._control_client,
                local_state=self._local_state,
                profile_ref=entry.profile_ref,
                clock=self._clock,
            )
            try:
                await execution.request_intervention(
                    "AMBIGUOUS_OUTCOME", "RESTART_REQUIRES_OUTCOME_RECONCILIATION"
                )
            except WorkerControlClientError, WorkerJobLeaseLost:
                # Keep the local journal until the authenticated Control Plane accepts it.
                continue


class WorkerBrowserSession:
    def __init__(
        self,
        account_id: UUID,
        opened: BrowserSessionOpenResult,
        engine_session: BrowserEngineSession,
        transition: SessionTransition,
        close_session: SessionClose,
        job_execution: WorkerJobExecution | None,
        forget: Callable[[UUID], None],
    ) -> None:
        self._account_id = account_id
        self._session_state = opened.state.state
        self._engine_session = engine_session
        self._transition = transition
        self._close_session = close_session
        self._job_execution = job_execution
        self._forget = forget
        self._session_id = opened.state.session_id
        self._closed = False

    @property
    def session_id(self) -> UUID:
        return self._session_id

    async def navigate(self, url: str, policy: BrowserNavigationPolicy) -> None:
        if self._closed:
            raise BrowserProcessCrashed()
        policy.validate(url)
        try:
            await self._engine_session.navigate(url)
        except BrowserProcessCrashed:
            await self._set_state(BrowserSessionState.ERROR)
            if self._job_execution is not None:
                if self._job_execution.mutation_may_have_started:
                    recorded = await self._job_execution.report_ambiguous_outcome(
                        "BROWSER_CRASH_AFTER_MUTATION_BOUNDARY"
                    )
                    raise ActionOutcomeAmbiguous(intervention_recorded=recorded) from None
                await self._job_execution.fail(
                    error_code="BROWSER_PROCESS_CRASHED",
                    retryable=True,
                )
            raise

    async def inspect_contract(self) -> BrowserSurfaceState:
        if self._closed:
            raise BrowserProcessCrashed()
        if self._session_state in {
            BrowserSessionState.LOGIN_REQUIRED,
            BrowserSessionState.SESSION_EXPIRED,
            BrowserSessionState.CHALLENGE_REQUIRED,
            BrowserSessionState.ERROR,
        }:
            await self._set_state(BrowserSessionState.STARTING)
        try:
            state = classify_browser_surface(await self._engine_session.inspect_surface())
        except SessionExpired:
            await self._report_state_and_intervention(
                BrowserSessionState.SESSION_EXPIRED,
                "SESSION_EXPIRED",
                "SESSION_EXPIRED",
            )
            raise
        except ChallengeDetected:
            await self._report_state_and_intervention(
                BrowserSessionState.CHALLENGE_REQUIRED,
                "CHALLENGE_REQUIRED",
                "CHALLENGE_REQUIRED",
            )
            raise
        except BrowserProcessCrashed:
            await self._set_state(BrowserSessionState.ERROR)
            if self._job_execution is not None:
                if self._job_execution.mutation_may_have_started:
                    recorded = await self._job_execution.report_ambiguous_outcome(
                        "BROWSER_CRASH_AFTER_MUTATION_BOUNDARY"
                    )
                    raise ActionOutcomeAmbiguous(intervention_recorded=recorded) from None
                await self._job_execution.fail(
                    error_code="BROWSER_PROCESS_CRASHED",
                    retryable=True,
                )
            raise
        except BrowserAdapterError as error:
            await self._set_state(BrowserSessionState.ERROR)
            await self._request_intervention("REMOTE_STATE_UNCERTAIN", error.code)
            raise
        target = BrowserSessionState(state.value)
        await self._set_state(target)
        if target is BrowserSessionState.LOGIN_REQUIRED:
            await self._request_intervention("LOGIN_REQUIRED", "LOGIN_REQUIRED")
        return state

    async def close(self) -> None:
        try:
            await self.close_engine_only()
        finally:
            await self._close_session(self._account_id)

    async def close_engine_only(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._engine_session.close()
        except BrowserAdapterError:
            try:
                await self._set_state(BrowserSessionState.ERROR)
            finally:
                self._forget(self._account_id)
            raise
        else:
            self._forget(self._account_id)

    def bind_worker_job(self, execution: WorkerJobExecution) -> None:
        if execution.account_id != self._account_id:
            raise BrowserAccountAffinityMismatch()
        self._job_execution = execution

    async def _set_state(self, state: BrowserSessionState) -> None:
        changed = await self._transition(self._account_id, state)
        self._session_state = changed.state

    async def _report_state_and_intervention(
        self,
        state: BrowserSessionState,
        intervention_type: str,
        detail_code: str,
    ) -> None:
        await self._set_state(state)
        await self._request_intervention(intervention_type, detail_code)

    async def _request_intervention(self, intervention_type: str, detail_code: str) -> None:
        if self._job_execution is not None:
            await self._job_execution.request_intervention(intervention_type, detail_code)


class PlaywrightBrowserAdapter:
    """Opens an already-reserved C3-01 profile through the selected engine port."""

    def __init__(
        self,
        worker_id: UUID,
        profile_resolver: ManagedProfilePathResolver,
        engine: BrowserEngine,
        *,
        credential_provider: ProxyCredentialProvider | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._profile_resolver = profile_resolver
        self._engine = engine
        self._credential_provider = credential_provider
        self._sessions: dict[UUID, WorkerBrowserSession] = {}

    async def open_reserved_session(
        self,
        context: WorkerAccountContext,
        opened: BrowserSessionOpenResult,
        *,
        transition: SessionTransition,
        close_session: SessionClose,
        job_execution: WorkerJobExecution | None = None,
        headless: bool = False,
    ) -> WorkerBrowserSession:
        reservation_account_id = opened.state.account_id
        if context.worker_id != self._worker_id:
            await self._release_failed_reservation(
                reservation_account_id, transition, close_session
            )
            raise BrowserAccountAffinityMismatch()
        if (
            context.account_id != reservation_account_id
            or context.profile_ref != opened.state.profile_ref
        ):
            await self._release_failed_reservation(
                reservation_account_id, transition, close_session
            )
            raise BrowserAccountAffinityMismatch()
        if opened.network_route.account_id != context.account_id:
            await self._release_failed_reservation(
                reservation_account_id, transition, close_session
            )
            raise BrowserAccountAffinityMismatch()
        if opened.state.state in {
            BrowserSessionState.ERROR,
            BrowserSessionState.SESSION_EXPIRED,
            BrowserSessionState.STOPPED,
        }:
            await self._release_failed_reservation(
                reservation_account_id, transition, close_session
            )
            raise BrowserAccountAffinityMismatch()
        existing = self._sessions.get(context.account_id)
        if existing is not None:
            if existing.session_id == opened.state.session_id:
                return existing
            await self._release_failed_reservation(
                reservation_account_id, transition, close_session
            )
            raise BrowserAccountAffinityMismatch()
        try:
            profile_directory = self._profile_resolver.resolve(
                context.worker_id, context.account_id, context.profile_ref
            )
            route = opened.network_route
            credentials: ProxyCredentials | None = None
            if route.credential_ref is not None:
                if self._credential_provider is None:
                    raise BrowserNetworkRouteUnsupported()
                credentials = await self._credential_provider.credentials_for(route.credential_ref)
            request = BrowserLaunchRequest(
                worker_id=context.worker_id,
                account_id=context.account_id,
                profile_ref=context.profile_ref,
                profile_directory=profile_directory,
                network_route=route,
                proxy_credentials=credentials,
                headless=headless,
            )
            engine_session = await self._engine.open(request)
        except BrowserAdapterError:
            await self._release_failed_reservation(
                reservation_account_id, transition, close_session
            )
            raise
        except Exception:
            await self._release_failed_reservation(
                reservation_account_id, transition, close_session
            )
            raise BrowserRuntimeUnavailable("BROWSER_START_FAILED") from None
        browser_session = WorkerBrowserSession(
            context.account_id,
            opened,
            engine_session,
            transition,
            close_session,
            job_execution,
            self.forget_closed_session,
        )
        self._sessions[context.account_id] = browser_session
        return browser_session

    def forget_closed_session(self, account_id: UUID) -> None:
        self._sessions.pop(account_id, None)

    @staticmethod
    async def _release_failed_reservation(
        account_id: UUID,
        transition: SessionTransition,
        close_session: SessionClose,
    ) -> None:
        try:
            await transition(account_id, BrowserSessionState.ERROR)
        finally:
            await close_session(account_id)


class ManagedPlaywrightBrowserSessionManager:
    """Composes C3-01 reservations with engine lifetime for WorkerAgent shutdown."""

    def __init__(
        self,
        session_manager: BrowserSessionManager,
        browser_adapter: PlaywrightBrowserAdapter,
        *,
        headless: bool = False,
    ) -> None:
        self._session_manager = session_manager
        self._browser_adapter = browser_adapter
        self._headless = headless
        self._sessions: dict[UUID, WorkerBrowserSession] = {}

    async def open(self, context: WorkerAccountContext) -> BrowserSessionOpenResult:
        opened = await self._session_manager.open(context)
        browser_session = await self._browser_adapter.open_reserved_session(
            context,
            opened,
            transition=self.transition,
            close_session=self.close,
            headless=self._headless,
        )
        self._sessions[context.account_id] = browser_session
        return opened

    async def transition(self, account_id: UUID, state: BrowserSessionState) -> LocalSessionState:
        return await self._session_manager.transition(account_id, state)

    async def close(self, account_id: UUID) -> LocalSessionState:
        browser_session = self._sessions.pop(account_id, None)
        try:
            if browser_session is not None:
                await browser_session.close_engine_only()
        except BrowserAdapterError:
            pass
        return await self._session_manager.close(account_id)

    def browser_session(self, account_id: UUID) -> WorkerBrowserSession | None:
        return self._sessions.get(account_id)


def _validate_checkpoint(checkpoint: dict[str, object]) -> None:
    if not checkpoint or set(checkpoint) - _SAFE_CHECKPOINT_FIELDS:
        raise ValueError("browser WorkerJob checkpoints contain unsupported fields")
    phase = checkpoint.get("phase")
    if not isinstance(phase, str) or not _CODE.fullmatch(phase):
        raise ValueError("browser WorkerJob checkpoints require a bounded phase code")
    for key in ("contract_id", "reason_code"):
        value = checkpoint.get(key)
        if value is not None and (not isinstance(value, str) or not _CODE.fullmatch(value)):
            raise ValueError("browser WorkerJob checkpoint values must be bounded codes")
    version = checkpoint.get("contract_version")
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int) or version < 1
    ):
        raise ValueError("browser WorkerJob contract version must be positive")
    if len(json.dumps(checkpoint, separators=(",", ":")).encode("utf-8")) > 2048:
        raise ValueError("browser WorkerJob checkpoint is too large")
