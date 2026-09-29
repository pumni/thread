from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from threads_platform.application.browser_capabilities import (
    BROWSER_CAPABILITY_CONTRACTS,
    BrowserMediaStageResultV1,
)
from threads_platform.application.ports.worker_agent import (
    WorkerAccountContext,
    WorkerControlClient,
    WorkerJobControlClient,
    WorkerJobSnapshot,
    WorkerLocalState,
)
from threads_platform.domain.browser_media import BrowserMediaKind
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety
from threads_platform.domain.workers import BrowserSessionState
from threads_platform.infrastructure.worker_agent.local_media import (
    LocalMediaFileError,
    LocalMediaFileResolver,
    WorkerLocalMediaFile,
)
from threads_platform.workers.browser import (
    ActionOutcomeAmbiguous,
    BrowserAdapterError,
    ChallengeDetected,
    PreparedMediaComposer,
    RemoteSessionStateUncertain,
    SessionExpired,
    WorkerBrowserSession,
    WorkerJobExecution,
    WorkerJobLeaseLost,
)
from threads_platform.workers.sessions import BrowserSessionOpenResult as SessionOpenResult

MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME = "threads.browser.media.local_upload"
MEDIA_LOCAL_UPLOAD_CAPABILITY_VERSION = 1
MEDIA_LOCAL_UPLOAD_ALLOWED_FAILURE_CODES = next(
    contract.allowed_failure_codes
    for contract in BROWSER_CAPABILITY_CONTRACTS
    if contract.name == MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME
)
MEDIA_LOCAL_UPLOAD_TIMEOUT_SECONDS = 60
MEDIA_UPLOAD_LEASE_RENEW_INTERVAL_SECONDS = 10
_AMBIGUOUS_UPLOAD_PHASES = frozenset(
    {
        "MUTATION_PENDING",
        "MUTATION_STARTED",
        "MUTATION_CONFIRMED",
        "LOCAL_STAGE_COMPLETE",
    }
)

_MEDIA_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")


class MediaLocalUploadSessionManager(Protocol):
    async def open(self, context: WorkerAccountContext) -> SessionOpenResult: ...

    def browser_session(self, account_id: UUID) -> WorkerBrowserSession | None: ...

    async def close(self, account_id: UUID) -> object: ...


class MediaLocalUploadControlClient(WorkerControlClient, WorkerJobControlClient, Protocol):
    """Control Plane operations used by an account-affine local-upload WorkerJob."""


class BrowserLocalMediaUploadWorker:
    """Stages one approved image in an operator-opened composer under a fenced job."""

    def __init__(
        self,
        worker_id: UUID,
        control_client: MediaLocalUploadControlClient,
        session_manager: MediaLocalUploadSessionManager,
        media_resolver: LocalMediaFileResolver,
        local_state: WorkerLocalState,
    ) -> None:
        self._worker_id = worker_id
        self._control_client = control_client
        self._session_manager = session_manager
        self._media_resolver = media_resolver
        self._local_state = local_state
        self._active_accounts: set[UUID] = set()

    async def __call__(self, job: WorkerJobSnapshot) -> None:
        try:
            execution = WorkerJobExecution(job, self._worker_id, self._control_client)
        except WorkerJobLeaseLost:
            return
        executions = [execution]
        if job.account_id is None or job.assigned_worker_id != self._worker_id:
            await self._fail(execution, "BROWSER_ACCOUNT_AFFINITY_MISMATCH", retryable=False)
            return
        if (
            job.capability_name != MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME
            or job.capability_version != MEDIA_LOCAL_UPLOAD_CAPABILITY_VERSION
        ):
            await self._fail(execution, "UNSUPPORTED_BROWSER_CAPABILITY", retryable=False)
            return
        if job.retry_safety is not WorkerJobRetrySafety.RECONCILIATION_REQUIRED:
            await self._fail(execution, "WORKER_JOB_RETRY_SAFETY_MISMATCH", retryable=False)
            return
        checkpoint_phase = (job.checkpoint or {}).get("phase")
        if isinstance(checkpoint_phase, str) and checkpoint_phase in _AMBIGUOUS_UPLOAD_PHASES:
            await execution.request_intervention(
                "AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION"
            )
            return
        media_ref = parse_media_ref(job.input_data)
        if media_ref is None:
            await self._fail(execution, "WORKER_JOB_INPUT_INVALID", retryable=False)
            return

        try:
            async with asyncio.timeout(MEDIA_LOCAL_UPLOAD_TIMEOUT_SECONDS):
                await self._execute_upload(job, executions, media_ref)
        except ActionOutcomeAmbiguous:
            return
        except WorkerJobLeaseLost:
            execution = executions[0]
            if execution.mutation_may_have_started:
                await execution.report_ambiguous_outcome("MEDIA_UPLOAD_LEASE_LOST")
            return
        except TimeoutError:
            execution = executions[0]
            if execution.mutation_may_have_started:
                await execution.report_ambiguous_outcome("MEDIA_UPLOAD_OUTCOME_UNCERTAIN")
            else:
                await self._fail(execution, "BROWSER_NAVIGATION_TIMEOUT", retryable=True)
        except LocalMediaFileError as error:
            await self._fail(executions[0], error.code, retryable=False)
        except SessionExpired, ChallengeDetected, RemoteSessionStateUncertain:
            return
        except BrowserAdapterError as error:
            execution = executions[0]
            code = error.code
            if code in {
                "LOGIN_REQUIRED",
                "SESSION_EXPIRED",
                "CHALLENGE_REQUIRED",
                "REMOTE_STATE_UNCERTAIN",
            }:
                return
            if code not in MEDIA_LOCAL_UPLOAD_ALLOWED_FAILURE_CODES:
                code = "BROWSER_RUNTIME_UNAVAILABLE"
            await self._fail(
                execution,
                code,
                retryable=code
                in {
                    "BROWSER_NAVIGATION_TIMEOUT",
                    "BROWSER_PROCESS_CRASHED",
                    "BROWSER_RUNTIME_UNAVAILABLE",
                    "BROWSER_SESSION_UNAVAILABLE",
                },
            )

    async def _execute_upload(
        self,
        job: WorkerJobSnapshot,
        executions: list[WorkerJobExecution],
        media_ref: str,
    ) -> None:
        initial_execution = executions[0]
        account_id = job.account_id
        if account_id is None:
            await self._fail(
                initial_execution, "BROWSER_ACCOUNT_AFFINITY_MISMATCH", retryable=False
            )
            return
        context = await self._control_client.account_context(account_id)
        if context.account_id != account_id or context.worker_id != self._worker_id:
            await self._fail(
                initial_execution, "BROWSER_ACCOUNT_AFFINITY_MISMATCH", retryable=False
            )
            return
        if job.assigned_worker_id != self._worker_id:
            await self._fail(
                initial_execution, "BROWSER_ACCOUNT_AFFINITY_MISMATCH", retryable=False
            )
            return

        execution = WorkerJobExecution(
            job,
            self._worker_id,
            self._control_client,
            local_state=self._local_state,
            profile_ref=context.profile_ref,
        )
        executions[0] = execution
        await execution.renew()
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

        media_file = self._media_resolver.resolve(media_ref)
        if media_file.kind is not BrowserMediaKind.IMAGE:
            raise LocalMediaFileError("MEDIA_FILE_REJECTED")

        await execution.checkpoint({"phase": "BEFORE_LOCAL_STAGE"})
        composer = await browser_session.prepare_media_composer()
        if composer is None:
            await execution.request_intervention(
                "OPERATOR_CONFIRMATION_REQUIRED", "COMPOSER_OPEN_REQUIRED"
            )
            return

        operation_started = False

        async def stage_once() -> None:
            nonlocal operation_started
            operation_started = True
            await self._stage_with_lease_renewal(
                execution,
                browser_session,
                composer,
                media_file,
            )

        try:
            await execution.execute_irreversible_boundary(stage_once)
        finally:
            if not operation_started:
                await browser_session.discard_media_composer(composer)

        await execution.checkpoint({"phase": "LOCAL_STAGE_COMPLETE"})
        result = BrowserMediaStageResultV1(
            media_kind=BrowserMediaKind.IMAGE,
            byte_size=media_file.byte_size,
            staged=True,
        )
        await execution.complete(result.model_dump(mode="json"))

    async def _stage_with_lease_renewal(
        self,
        execution: WorkerJobExecution,
        browser_session: WorkerBrowserSession,
        composer: PreparedMediaComposer,
        media_file: WorkerLocalMediaFile,
    ) -> None:
        stage_task = asyncio.create_task(
            browser_session.stage_local_media(composer, media_file.path)
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {stage_task}, timeout=MEDIA_UPLOAD_LEASE_RENEW_INTERVAL_SECONDS
                )
                if stage_task in done:
                    await stage_task
                    return
                await execution.renew()
        except WorkerJobLeaseLost, asyncio.CancelledError:
            if not stage_task.done():
                stage_task.cancel()
            try:
                await stage_task
            except Exception, asyncio.CancelledError:
                pass
            raise

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
            if execution.mutation_may_have_started:
                await execution.report_ambiguous_outcome("MEDIA_UPLOAD_LEASE_LOST")


def parse_media_ref(input_data: Mapping[str, object]) -> str | None:
    if set(input_data) != {"media_ref"}:
        return None
    value = input_data.get("media_ref")
    if not isinstance(value, str) or _MEDIA_REF.fullmatch(value) is None:
        return None
    if value in {".", ".."} or value.split(".", 1)[0].casefold() in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }:
        return None
    return value
