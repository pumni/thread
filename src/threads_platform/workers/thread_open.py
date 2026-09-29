from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from threads_platform.application.browser_capabilities import (
    BROWSER_CAPABILITY_CONTRACTS,
    BrowserTargetOpenResultV1,
)
from threads_platform.application.ports.worker_agent import (
    WorkerAccountContext,
    WorkerControlClient,
    WorkerJobControlClient,
    WorkerJobSnapshot,
)
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety
from threads_platform.domain.workers import BrowserSessionState
from threads_platform.workers.browser import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserContractError,
    BrowserNavigationPolicy,
    WorkerBrowserSession,
    WorkerJobExecution,
    WorkerJobLeaseLost,
)
from threads_platform.workers.sessions import BrowserSessionOpenResult

THREAD_OPEN_CAPABILITY_NAME = "threads.browser.thread.open"
THREAD_OPEN_CAPABILITY_VERSION = 1
THREAD_OPEN_ANCESTOR_BOUND = 8
THREAD_OPEN_NAVIGATION_POLICY = BrowserNavigationPolicy(frozenset({BROWSER_FEED_ORIGIN}))
THREAD_OPEN_ALLOWED_FAILURE_CODES = next(
    contract.allowed_failure_codes
    for contract in BROWSER_CAPABILITY_CONTRACTS
    if contract.name == THREAD_OPEN_CAPABILITY_NAME
)

_THREAD_REF = re.compile(r"^/@([A-Za-z0-9._]{1,30})/post/([A-Za-z0-9_-]{1,120})/?$")


class ThreadOpenBrowserSessionManager(Protocol):
    async def open(self, context: WorkerAccountContext) -> BrowserSessionOpenResult: ...

    def browser_session(self, account_id: UUID) -> WorkerBrowserSession | None: ...

    async def close(self, account_id: UUID) -> object: ...


class ThreadOpenWorkerControlClient(WorkerControlClient, WorkerJobControlClient, Protocol):
    """Control Plane operations used by an account-affine thread.open WorkerJob."""


class BrowserThreadOpenWorker:
    """Confirms one evidence-backed Thread target within its WorkerJob lease."""

    def __init__(
        self,
        worker_id: UUID,
        control_client: ThreadOpenWorkerControlClient,
        session_manager: ThreadOpenBrowserSessionManager,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._worker_id = worker_id
        self._control_client = control_client
        self._session_manager = session_manager
        self._monotonic = monotonic
        self._active_accounts: set[UUID] = set()

    async def __call__(self, job: WorkerJobSnapshot) -> None:
        try:
            execution = WorkerJobExecution(job, self._worker_id, self._control_client)
        except WorkerJobLeaseLost:
            return
        if job.account_id is None:
            await self._fail(execution, "BROWSER_ACCOUNT_AFFINITY_MISMATCH", retryable=False)
            return
        if (
            job.capability_name != THREAD_OPEN_CAPABILITY_NAME
            or job.capability_version != THREAD_OPEN_CAPABILITY_VERSION
        ):
            await self._fail(execution, "UNSUPPORTED_BROWSER_CAPABILITY", retryable=False)
            return
        if job.retry_safety is not WorkerJobRetrySafety.SAFE_TO_RETRY:
            await self._fail(execution, "WORKER_JOB_RETRY_SAFETY_MISMATCH", retryable=False)
            return
        target = parse_thread_ref(job.input_data)
        if target is None:
            await self._fail(execution, "WORKER_JOB_INPUT_INVALID", retryable=False)
            return

        try:
            async with asyncio.timeout(30):
                await self._execute_thread(job, execution, *target)
        except WorkerJobLeaseLost:
            return
        except TimeoutError:
            await self._fail(execution, "BROWSER_NAVIGATION_TIMEOUT", retryable=True)
        except (BrowserContractError, BrowserAdapterError) as error:
            raw_code = getattr(error, "code", "BROWSER_CONTRACT_MISMATCH")
            if raw_code in {
                "LOGIN_REQUIRED",
                "SESSION_EXPIRED",
                "CHALLENGE_REQUIRED",
                "REMOTE_STATE_UNCERTAIN",
            }:
                return
            code = (
                raw_code
                if raw_code in THREAD_OPEN_ALLOWED_FAILURE_CODES
                else "BROWSER_RUNTIME_UNAVAILABLE"
            )
            await self._fail(
                execution,
                code,
                retryable=code
                in {
                    "BROWSER_NAVIGATION_TIMEOUT",
                    "BROWSER_PROCESS_CRASHED",
                    "BROWSER_RUNTIME_UNAVAILABLE",
                },
            )

    async def _execute_thread(
        self,
        job: WorkerJobSnapshot,
        execution: WorkerJobExecution,
        target_ref: str,
        author_username: str,
    ) -> None:
        deadline = self._monotonic() + 30
        account_id = job.account_id
        if account_id is None:
            await self._fail(execution, "BROWSER_ACCOUNT_AFFINITY_MISMATCH", retryable=False)
            return
        context = await self._control_client.account_context(account_id)
        if (
            context.account_id != account_id
            or context.worker_id != self._worker_id
            or job.assigned_worker_id != self._worker_id
        ):
            await self._fail(execution, "BROWSER_ACCOUNT_AFFINITY_MISMATCH", retryable=False)
            return

        try:
            opened = await self._session_manager.open(context)
        except ValueError:
            await self._fail(execution, "BROWSER_SESSION_UNAVAILABLE", retryable=True)
            return
        self._active_accounts.add(account_id)
        browser_session = self._session_manager.browser_session(account_id)
        if browser_session is None:
            await self._fail(execution, "BROWSER_SESSION_UNAVAILABLE", retryable=True)
            return
        if (
            opened.state.account_id != context.account_id
            or opened.state.profile_ref != context.profile_ref
        ):
            await self._fail(execution, "BROWSER_ACCOUNT_AFFINITY_MISMATCH", retryable=False)
            return
        browser_session.bind_worker_job(execution)
        if not await self._require_authenticated_session(execution, browser_session):
            return
        if self._monotonic() >= deadline:
            raise TimeoutError

        await execution.checkpoint({"phase": "BEFORE_NAVIGATION"})
        if await execution.acknowledge_cancellation_if_pending():
            return
        await browser_session.navigate(
            f"{BROWSER_FEED_ORIGIN}{target_ref}", THREAD_OPEN_NAVIGATION_POLICY
        )
        if self._monotonic() >= deadline:
            raise TimeoutError
        await browser_session.verify_thread_target(
            target_ref=target_ref,
            author_username=author_username,
            ancestor_bound=THREAD_OPEN_ANCESTOR_BOUND,
        )
        await execution.checkpoint({"phase": "THREAD_READY"})
        if await execution.acknowledge_cancellation_if_pending():
            return
        result = BrowserTargetOpenResultV1(
            target_kind="THREAD",
            target_ref=target_ref,
            recognized=True,
        )
        await execution.complete(result.model_dump(mode="json"))

    async def aclose(self) -> None:
        for account_id in tuple(self._active_accounts):
            try:
                await self._session_manager.close(account_id)
            finally:
                self._active_accounts.discard(account_id)

    async def _require_authenticated_session(
        self, execution: WorkerJobExecution, session: WorkerBrowserSession
    ) -> bool:
        if session.session_state is BrowserSessionState.AUTHENTICATED:
            return True
        if session.session_state is BrowserSessionState.SESSION_EXPIRED:
            intervention = "SESSION_EXPIRED"
        elif session.session_state is BrowserSessionState.CHALLENGE_REQUIRED:
            intervention = "CHALLENGE_REQUIRED"
        else:
            intervention = "LOGIN_REQUIRED"
        try:
            await execution.request_intervention(intervention, "SESSION_REQUIRES_OPERATOR")
        except WorkerJobLeaseLost:
            return False
        return False

    async def _fail(
        self,
        execution: WorkerJobExecution,
        error_code: str,
        *,
        retryable: bool,
    ) -> None:
        try:
            await execution.fail(error_code=error_code, retryable=retryable)
        except WorkerJobLeaseLost:
            return


def parse_thread_ref(input_data: dict[str, object]) -> tuple[str, str] | None:
    if set(input_data) != {"thread_ref"}:
        return None
    value = input_data.get("thread_ref")
    if not isinstance(value, str):
        return None
    match = _THREAD_REF.fullmatch(value)
    if match is None:
        return None
    return value[:-1] if value.endswith("/") else value, match.group(1)
