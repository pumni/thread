from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from threads_platform.application.browser_capabilities import (
    BROWSER_CAPABILITY_CONTRACTS,
    BrowserFeedItemResultV1,
    BrowserFeedResultV1,
)
from threads_platform.application.browser_read_semantics import (
    BROWSER_FEED_ANCESTOR_BOUND,
    BROWSER_FEED_CANDIDATE_BOUND,
    BROWSER_FEED_ITERATION_BOUND,
    BROWSER_FEED_URL,
    normalize_feed_candidates,
)
from threads_platform.application.ports.browser import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserContractError,
)
from threads_platform.application.ports.worker_agent import (
    LocalSessionState,
    WorkerAccountContext,
    WorkerControlClient,
    WorkerJobControlClient,
    WorkerJobSnapshot,
)
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety
from threads_platform.domain.workers import BrowserSessionState
from threads_platform.workers.browser import (
    BrowserNavigationPolicy,
    WorkerBrowserSession,
    WorkerJobExecution,
    WorkerJobLeaseLost,
)
from threads_platform.workers.sessions import BrowserSessionOpenResult

THREADS_WEB_ORIGIN = BROWSER_FEED_ORIGIN
THREADS_FEED_URL = BROWSER_FEED_URL
FEED_CAPABILITY_NAME = "threads.browser.feed.browse"
FEED_CAPABILITY_VERSION = 1
FEED_ANCESTOR_BOUND = BROWSER_FEED_ANCESTOR_BOUND
FEED_ITERATION_BOUND = BROWSER_FEED_ITERATION_BOUND
FEED_CANDIDATE_BOUND = BROWSER_FEED_CANDIDATE_BOUND
FEED_NAVIGATION_POLICY = BrowserNavigationPolicy(frozenset({THREADS_WEB_ORIGIN}))
FEED_ALLOWED_FAILURE_CODES = next(
    contract.allowed_failure_codes
    for contract in BROWSER_CAPABILITY_CONTRACTS
    if contract.name == FEED_CAPABILITY_NAME
)


class FeedBrowserSessionManager(Protocol):
    async def open(self, context: WorkerAccountContext) -> BrowserSessionOpenResult: ...

    def browser_session(self, account_id: UUID) -> WorkerBrowserSession | None: ...

    async def close(self, account_id: UUID) -> LocalSessionState: ...


class FeedWorkerControlClient(WorkerControlClient, WorkerJobControlClient, Protocol):
    """Control Plane calls required by an account-affine feed WorkerJob."""


class BrowserFeedBrowseWorker:
    """Executes the reviewed permalink-pivot feed contract inside a WorkerJob."""

    def __init__(
        self,
        worker_id: UUID,
        control_client: FeedWorkerControlClient,
        session_manager: FeedBrowserSessionManager,
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
            job.capability_name != FEED_CAPABILITY_NAME
            or job.capability_version != FEED_CAPABILITY_VERSION
        ):
            await self._fail(execution, "UNSUPPORTED_BROWSER_CAPABILITY", retryable=False)
            return
        if job.retry_safety is not WorkerJobRetrySafety.SAFE_TO_RETRY:
            await self._fail(execution, "WORKER_JOB_RETRY_SAFETY_MISMATCH", retryable=False)
            return
        max_items = _max_items(job.input_data)
        if max_items is None:
            await self._fail(execution, "WORKER_JOB_INPUT_INVALID", retryable=False)
            return

        deadline = self._monotonic() + 30
        try:
            async with asyncio.timeout(30):
                await self._execute_feed(job, execution, max_items, deadline)
        except WorkerJobLeaseLost:
            return
        except TimeoutError:
            await self._fail(execution, "BROWSER_NAVIGATION_TIMEOUT", retryable=True)
        except (BrowserContractError, BrowserAdapterError) as error:
            raw_code = getattr(error, "code", "BROWSER_CONTRACT_MISMATCH")
            if raw_code in {
                "SESSION_EXPIRED",
                "CHALLENGE_REQUIRED",
                "REMOTE_STATE_UNCERTAIN",
            }:
                return
            code = (
                raw_code
                if raw_code in FEED_ALLOWED_FAILURE_CODES
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

    async def _execute_feed(
        self,
        job: WorkerJobSnapshot,
        execution: WorkerJobExecution,
        max_items: int,
        deadline: float,
    ) -> None:
        self._check_deadline(deadline)
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

        observations: list[BrowserFeedItemResultV1] = []
        seen_refs: set[str] = set()
        truncated = False
        await execution.checkpoint({"phase": "BEFORE_NAVIGATION"})
        if await execution.acknowledge_cancellation_if_pending():
            return
        await browser_session.navigate(THREADS_FEED_URL, FEED_NAVIGATION_POLICY)

        for iteration in range(FEED_ITERATION_BOUND):
            self._check_deadline(deadline)
            if iteration:
                await browser_session.scroll_feed()
            candidates = await browser_session.collect_feed_candidates(
                ancestor_bound=FEED_ANCESTOR_BOUND
            )
            if not candidates and not observations:
                raise BrowserContractError()
            remaining_items = max_items - len(observations)
            batch = normalize_feed_candidates(
                candidates,
                max_items=remaining_items,
                ancestor_bound=FEED_ANCESTOR_BOUND,
                existing_refs=frozenset(seen_refs),
                position_start=len(observations),
            )
            for item in batch:
                if item.thread_ref is None:
                    raise BrowserContractError()
                seen_refs.add(item.thread_ref)
                observations.append(item)
            self._check_deadline(deadline)
            await execution.checkpoint({"phase": "FEED_READY" if iteration == 0 else "ITEM_BATCH"})
            if await execution.acknowledge_cancellation_if_pending():
                return

            if len(observations) >= max_items:
                truncated = True
                break
            if iteration and not batch:
                truncated = True
                break
            if iteration == FEED_ITERATION_BOUND - 1:
                truncated = True

        result = BrowserFeedResultV1(
            observations=tuple(observations[:max_items]),
            truncated=truncated,
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
        state = session.session_state
        if state is BrowserSessionState.AUTHENTICATED:
            return True
        if state is BrowserSessionState.SESSION_EXPIRED:
            intervention = "SESSION_EXPIRED"
        elif state is BrowserSessionState.CHALLENGE_REQUIRED:
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

    def _check_deadline(self, deadline: float) -> None:
        if self._monotonic() >= deadline:
            raise TimeoutError


def _max_items(input_data: dict[str, object]) -> int | None:
    if set(input_data) - {"max_items"}:
        return None
    value = input_data.get("max_items", 10)
    if type(value) is not int or not 1 <= value <= 20:
        return None
    return value
