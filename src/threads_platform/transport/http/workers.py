from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from threads_platform.application.worker_control import (
    WorkerControlError,
    WorkerControlService,
    WorkerPresence,
)
from threads_platform.application.worker_jobs import WorkerJobControlError, WorkerJobService
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.application.worker_sessions import (
    WorkerSessionControlError,
    WorkerSessionService,
)
from threads_platform.domain.worker_jobs import WorkerJob, WorkerJobRetrySafety, WorkerJobStatus
from threads_platform.domain.workers import (
    BrowserSessionState,
    NetworkProtocol,
    WorkerAccountSession,
    WorkerCapability,
)
from threads_platform.transport.http.auth import CommandAuthenticator


class _WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateEnrollmentRequest(_WorkerRequest):
    created_by: str = Field(default="operator", min_length=1, max_length=255)


class CreateEnrollmentResponse(BaseModel):
    enrollment_code: str
    expires_at: AwareDatetime


class EnrollWorkerRequest(_WorkerRequest):
    enrollment_code: str = Field(min_length=1, max_length=256)
    worker_id: UUID
    display_name: str = Field(min_length=1, max_length=255)
    hostname: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=80)
    public_key: str = Field(min_length=40, max_length=64)
    max_concurrent_jobs: int = Field(default=1, ge=1, le=1000)
    max_browser_sessions: int = Field(default=1, ge=1, le=1000)


class EnrollWorkerResponse(BaseModel):
    worker_id: UUID
    status: str = "REGISTERING"


class CreateChallengeRequest(_WorkerRequest):
    worker_id: UUID


class CreateChallengeResponse(BaseModel):
    challenge_id: UUID
    nonce: str
    expires_at: AwareDatetime


class ExchangeChallengeRequest(_WorkerRequest):
    challenge_id: UUID
    signature: str = Field(min_length=80, max_length=100)


class ExchangeChallengeResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_at: AwareDatetime


class CapabilityAdvertisement(_WorkerRequest):
    capability_name: str = Field(min_length=1, max_length=120)
    capability_version: int = Field(ge=1)
    metadata: dict[str, object] = Field(default_factory=dict)


class WorkerHelloRequest(_WorkerRequest):
    protocol_version: int = Field(ge=1)
    agent_version: str = Field(min_length=1, max_length=80)
    capabilities_schema_version: int = Field(ge=1)
    capabilities: list[CapabilityAdvertisement] = Field(max_length=256)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    platform: str | None = Field(default=None, min_length=1, max_length=80)
    max_concurrent_jobs: int | None = Field(default=None, ge=1, le=1000)
    max_browser_sessions: int | None = Field(default=None, ge=1, le=1000)
    active_browser_sessions: int | None = Field(default=None, ge=0, le=1000)
    healthy: bool = True

    @model_validator(mode="after")
    def require_v2_capacity_summary(self) -> WorkerHelloRequest:
        if self.protocol_version == 2 and (
            self.max_browser_sessions is None or self.active_browser_sessions is None
        ):
            raise ValueError("protocol version 2 requires browser session capacity summary")
        if (
            self.max_browser_sessions is not None
            and self.active_browser_sessions is not None
            and self.active_browser_sessions > self.max_browser_sessions
        ):
            raise ValueError("active browser sessions cannot exceed advertised capacity")
        return self


class WorkerHeartbeatRequest(_WorkerRequest):
    healthy: bool = True
    active_browser_sessions: int | None = Field(default=None, ge=0, le=1000)


class WorkerPresenceResponse(BaseModel):
    worker_id: UUID
    status: str
    last_heartbeat_at: AwareDatetime
    presence_expires_at: AwareDatetime
    protocol_compatible: bool
    max_browser_sessions: int | None = None
    active_browser_sessions: int | None = None


class WorkerNetworkProfileResponse(BaseModel):
    id: UUID
    protocol: NetworkProtocol
    host: str | None
    port: int | None
    credential_ref: str | None


class WorkerAccountContextResponse(BaseModel):
    account_id: UUID
    worker_id: UUID
    profile_ref: str
    network_profile: WorkerNetworkProfileResponse | None


class WorkerSessionReportRequest(_WorkerRequest):
    profile_ref: str = Field(min_length=1, max_length=255)
    session_id: UUID
    state: BrowserSessionState
    revision: int = Field(ge=1)


class WorkerSessionReportResponse(BaseModel):
    account_id: UUID
    worker_id: UUID
    profile_ref: str
    session_id: UUID
    state: BrowserSessionState
    revision: int
    requires_intervention: bool
    updated_at: AwareDatetime


class WorkerJobResponse(BaseModel):
    id: UUID
    command_id: str | None
    account_id: UUID | None
    assigned_worker_id: UUID | None
    capability_name: str
    capability_version: int
    status: WorkerJobStatus
    priority: int
    preemptible: bool
    scheduled_at: AwareDatetime
    deadline_at: AwareDatetime | None
    attempt_count: int
    max_attempts: int
    retry_safety: WorkerJobRetrySafety
    lease_worker_id: UUID | None
    lease_token: UUID | None
    lease_expires_at: AwareDatetime | None
    checkpoint: dict[str, object] | None
    result: dict[str, object] | None
    error_code: str | None


class WorkerJobReconcileResponse(BaseModel):
    jobs: list[WorkerJobResponse]


class WorkerJobLeaseRequest(_WorkerRequest):
    lease_token: UUID


class WorkerJobCheckpointRequest(WorkerJobLeaseRequest):
    checkpoint: dict[str, object]


class WorkerJobCompleteRequest(WorkerJobLeaseRequest):
    result: dict[str, object] = Field(default_factory=dict)


class WorkerJobFailRequest(WorkerJobLeaseRequest):
    error_code: str = Field(pattern=r"^[A-Z0-9_]{1,120}$")
    retryable: bool
    outcome_ambiguous: bool = False


class WorkerJobInterventionRequest(WorkerJobLeaseRequest):
    intervention_type: Literal[
        "LOGIN_REQUIRED",
        "SESSION_EXPIRED",
        "CHALLENGE_REQUIRED",
        "OPERATOR_CONFIRMATION_REQUIRED",
        "AMBIGUOUS_OUTCOME",
        "REMOTE_STATE_UNCERTAIN",
    ]
    detail_code: str = Field(pattern=r"^[A-Z0-9_]{1,120}$")


class ResolveInterventionRequest(_WorkerRequest):
    requeue: bool
    confirmed_safe_to_retry: bool = False
    resolved_by: str = Field(default="operator", min_length=1, max_length=255)


def create_worker_router(
    service: WorkerControlService | None,
    admin_authenticator: CommandAuthenticator,
    notifications: WorkerNotificationHub,
    job_service: WorkerJobService | None = None,
    session_service: WorkerSessionService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/workers", tags=["workers"])

    def require_service() -> WorkerControlService:
        if service is None:
            raise HTTPException(status_code=503, detail={"code": "WORKER_CONTROL_UNAVAILABLE"})
        return service

    def require_job_service() -> WorkerJobService:
        if job_service is None:
            raise HTTPException(status_code=503, detail={"code": "WORKER_JOB_SERVICE_UNAVAILABLE"})
        return job_service

    def require_session_service() -> WorkerSessionService:
        if session_service is None:
            raise HTTPException(
                status_code=503, detail={"code": "WORKER_SESSION_SERVICE_UNAVAILABLE"}
            )
        return session_service

    async def authenticated_worker(authorization: str | None) -> UUID:
        control = require_service()
        token = _bearer_token(authorization)
        worker_id = await control.authenticate(token) if token is not None else None
        if worker_id is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "WORKER_UNAUTHORIZED"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return worker_id

    @router.post("/enrollments", response_model=CreateEnrollmentResponse)
    async def create_enrollment(
        request: CreateEnrollmentRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> CreateEnrollmentResponse:
        if not admin_authenticator.is_authorized(authorization):
            raise HTTPException(
                status_code=401,
                detail={"code": "WORKER_ADMIN_UNAUTHORIZED"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        result = await require_service().create_enrollment(request.created_by)
        return CreateEnrollmentResponse(
            enrollment_code=result.code,
            expires_at=result.expires_at,
        )

    @router.post("/enroll", response_model=EnrollWorkerResponse, status_code=201)
    async def enroll_worker(request: EnrollWorkerRequest) -> EnrollWorkerResponse:
        try:
            public_key = _decode_base64(request.public_key)
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code": "INVALID_PUBLIC_KEY"}) from error
        if len(public_key) != 32:
            raise HTTPException(status_code=422, detail={"code": "INVALID_PUBLIC_KEY"})
        try:
            enrolled = await require_service().enroll(
                request.enrollment_code,
                worker_id=request.worker_id,
                display_name=request.display_name,
                hostname=request.hostname,
                platform=request.platform,
                public_key=public_key,
                max_concurrent_jobs=request.max_concurrent_jobs,
                max_browser_sessions=request.max_browser_sessions,
            )
        except WorkerControlError as error:
            raise _http_error(error) from error
        return EnrollWorkerResponse(worker_id=enrolled.worker_id)

    @router.post("/auth/challenges", response_model=CreateChallengeResponse)
    async def create_challenge(request: CreateChallengeRequest) -> CreateChallengeResponse:
        try:
            challenge = await require_service().create_challenge(request.worker_id)
        except WorkerControlError as error:
            raise _http_error(error) from error
        return CreateChallengeResponse(
            challenge_id=challenge.challenge_id,
            nonce=challenge.nonce,
            expires_at=challenge.expires_at,
        )

    @router.post("/auth/sessions", response_model=ExchangeChallengeResponse)
    async def exchange_challenge(request: ExchangeChallengeRequest) -> ExchangeChallengeResponse:
        try:
            signature = _decode_base64(request.signature)
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code": "INVALID_SIGNATURE"}) from error
        try:
            result = await require_service().exchange_challenge(request.challenge_id, signature)
        except WorkerControlError as error:
            raise _http_error(error) from error
        return ExchangeChallengeResponse(
            access_token=result.access_token,
            expires_at=result.expires_at,
        )

    @router.post("/hello", response_model=WorkerPresenceResponse, response_model_exclude_none=True)
    async def worker_hello(
        request: WorkerHelloRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerPresenceResponse:
        access_token = _bearer_token(authorization)
        worker_id = await authenticated_worker(authorization)
        control = require_service()
        try:
            presence = await control.hello(
                worker_id,
                protocol_version=request.protocol_version,
                agent_version=request.agent_version,
                capabilities_schema_version=request.capabilities_schema_version,
                capabilities=[
                    WorkerCapability(
                        worker_id=worker_id,
                        name=item.capability_name,
                        version=item.capability_version,
                        metadata=item.metadata,
                    )
                    for item in request.capabilities
                ],
                display_name=request.display_name,
                hostname=request.hostname,
                platform=request.platform,
                max_concurrent_jobs=request.max_concurrent_jobs,
                max_browser_sessions=request.max_browser_sessions,
                active_browser_sessions=request.active_browser_sessions,
                healthy=request.healthy,
                access_token=access_token,
            )
        except WorkerControlError as error:
            raise _http_error(error) from error
        notifications.publish(
            worker_id,
            {"type": "worker.presence", "status": presence.status.value},
        )
        return _presence_response(presence)

    @router.post(
        "/heartbeat", response_model=WorkerPresenceResponse, response_model_exclude_none=True
    )
    async def worker_heartbeat(
        request: WorkerHeartbeatRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerPresenceResponse:
        access_token = _bearer_token(authorization)
        worker_id = await authenticated_worker(authorization)
        try:
            presence = await require_service().heartbeat(
                worker_id,
                healthy=request.healthy,
                active_browser_sessions=request.active_browser_sessions,
                access_token=access_token,
            )
        except WorkerControlError as error:
            raise _http_error(error) from error
        notifications.publish(
            worker_id,
            {"type": "worker.presence", "status": presence.status.value},
        )
        return _presence_response(presence)

    @router.get("/accounts/{account_id}/context", response_model=WorkerAccountContextResponse)
    async def worker_account_context(
        account_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerAccountContextResponse:
        worker_id = await authenticated_worker(authorization)
        try:
            context = await require_session_service().account_context(worker_id, account_id)
        except WorkerSessionControlError as error:
            raise _worker_session_error(error) from error
        network = context.network_profile
        return WorkerAccountContextResponse(
            account_id=context.account_id,
            worker_id=context.worker_id,
            profile_ref=context.profile_ref,
            network_profile=(
                WorkerNetworkProfileResponse(
                    id=network.id,
                    protocol=network.protocol,
                    host=network.host,
                    port=network.port,
                    credential_ref=network.credential_ref,
                )
                if network is not None
                else None
            ),
        )

    @router.put(
        "/accounts/{account_id}/session",
        response_model=WorkerSessionReportResponse,
    )
    async def report_worker_session(
        account_id: UUID,
        request: WorkerSessionReportRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerSessionReportResponse:
        worker_id = await authenticated_worker(authorization)
        try:
            session = await require_session_service().report_state(
                worker_id,
                account_id=account_id,
                profile_ref=request.profile_ref,
                session_id=request.session_id,
                state=request.state,
                revision=request.revision,
            )
        except WorkerSessionControlError as error:
            raise _worker_session_error(error) from error
        notifications.publish(
            worker_id,
            {"type": "worker.session.state", "account_id": str(account_id), "state": session.state},
        )
        return _worker_session_response(session)

    @router.get("/jobs/reconcile", response_model=WorkerJobReconcileResponse)
    async def reconcile_worker_jobs(
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerJobReconcileResponse:
        worker_id = await authenticated_worker(authorization)
        try:
            reconciliation = await require_job_service().reconcile(worker_id)
        except WorkerJobControlError as error:
            raise _worker_job_error(error) from error
        return WorkerJobReconcileResponse(
            jobs=[_worker_job_response(job) for job in reconciliation.jobs]
        )

    @router.post("/jobs/claim")
    async def claim_worker_job(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        worker_id = await authenticated_worker(authorization)
        try:
            job = await require_job_service().claim_next(worker_id)
        except WorkerJobControlError as error:
            raise _worker_job_error(error) from error
        if job is None:
            return Response(status_code=204)
        return JSONResponse(content=_worker_job_response(job).model_dump(mode="json"))

    @router.post("/jobs/{job_id}/renew", response_model=WorkerJobResponse)
    async def renew_worker_job(
        job_id: UUID,
        request: WorkerJobLeaseRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerJobResponse:
        worker_id = await authenticated_worker(authorization)
        try:
            job = await require_job_service().renew(job_id, worker_id, request.lease_token)
        except WorkerJobControlError as error:
            raise _worker_job_error(error) from error
        return _worker_job_response(job)

    @router.post("/jobs/{job_id}/checkpoint", response_model=WorkerJobResponse)
    async def checkpoint_worker_job(
        job_id: UUID,
        request: WorkerJobCheckpointRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerJobResponse:
        worker_id = await authenticated_worker(authorization)
        try:
            job = await require_job_service().checkpoint(
                job_id,
                worker_id,
                request.lease_token,
                request.checkpoint,
            )
        except (WorkerJobControlError, ValueError) as error:
            raise _worker_job_error(
                error
                if isinstance(error, WorkerJobControlError)
                else WorkerJobControlError("INVALID_CHECKPOINT")
            ) from error
        return _worker_job_response(job)

    @router.post("/jobs/{job_id}/complete", response_model=WorkerJobResponse)
    async def complete_worker_job(
        job_id: UUID,
        request: WorkerJobCompleteRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerJobResponse:
        worker_id = await authenticated_worker(authorization)
        try:
            job = await require_job_service().complete(
                job_id,
                worker_id,
                request.lease_token,
                request.result,
            )
        except (WorkerJobControlError, ValueError) as error:
            raise _worker_job_error(
                error
                if isinstance(error, WorkerJobControlError)
                else WorkerJobControlError("INVALID_RESULT")
            ) from error
        return _worker_job_response(job)

    @router.post("/jobs/{job_id}/fail", response_model=WorkerJobResponse)
    async def fail_worker_job(
        job_id: UUID,
        request: WorkerJobFailRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerJobResponse:
        worker_id = await authenticated_worker(authorization)
        try:
            job = await require_job_service().fail(
                job_id,
                worker_id,
                request.lease_token,
                error_code=request.error_code,
                retryable=request.retryable,
                outcome_ambiguous=request.outcome_ambiguous,
            )
        except WorkerJobControlError as error:
            raise _worker_job_error(error) from error
        return _worker_job_response(job)

    @router.post("/jobs/{job_id}/interventions", response_model=WorkerJobResponse)
    async def request_worker_intervention(
        job_id: UUID,
        request: WorkerJobInterventionRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerJobResponse:
        worker_id = await authenticated_worker(authorization)
        try:
            job = await require_job_service().request_intervention(
                job_id,
                worker_id,
                request.lease_token,
                intervention_type=request.intervention_type,
                detail_code=request.detail_code,
            )
        except WorkerJobControlError as error:
            raise _worker_job_error(error) from error
        return _worker_job_response(job)

    @router.post("/interventions/{intervention_id}/resolve", response_model=WorkerJobResponse)
    async def resolve_worker_intervention(
        intervention_id: UUID,
        request: ResolveInterventionRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerJobResponse:
        if not admin_authenticator.is_authorized(authorization):
            raise HTTPException(
                status_code=401,
                detail={"code": "WORKER_ADMIN_UNAUTHORIZED"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            job = await require_job_service().resolve_intervention(
                intervention_id,
                requeue=request.requeue,
                confirmed_safe_to_retry=request.confirmed_safe_to_retry,
                resolved_by=request.resolved_by,
            )
        except WorkerJobControlError as error:
            raise _worker_job_error(error) from error
        return _worker_job_response(job)

    @router.websocket("/connect")
    async def worker_websocket(websocket: WebSocket) -> None:
        if service is None:
            await websocket.close(code=1013)
            return
        control = service
        token = _bearer_token(websocket.headers.get("authorization"))
        if token is None:
            await websocket.close(code=4401)
            return
        session = await control.authenticate_session(token)
        if session is None:
            await websocket.close(code=4401)
            return
        worker_id = session.worker_id
        async with notifications.subscribe(worker_id) as queue:
            await websocket.accept()

            async def send_notifications() -> None:
                while True:
                    message = await queue.get()
                    if await control.authenticate(token) != worker_id:
                        await websocket.close(code=4401)
                        return
                    await websocket.send_json(message)

            async def receive_messages() -> None:
                while True:
                    raw_message: object = await websocket.receive_json()
                    if await control.authenticate(token) != worker_id:
                        await websocket.close(code=4401)
                        return
                    if not isinstance(raw_message, dict):
                        await websocket.send_json({"type": "error", "code": "INVALID_MESSAGE"})
                        continue
                    message = cast(dict[str, object], raw_message)
                    message_type = message.get("type")
                    body = {key: value for key, value in message.items() if key != "type"}
                    try:
                        if message_type == "worker.hello":
                            request = WorkerHelloRequest.model_validate(body)
                            presence = await control.hello(
                                worker_id,
                                protocol_version=request.protocol_version,
                                agent_version=request.agent_version,
                                capabilities_schema_version=request.capabilities_schema_version,
                                capabilities=[
                                    WorkerCapability(
                                        worker_id=worker_id,
                                        name=item.capability_name,
                                        version=item.capability_version,
                                        metadata=item.metadata,
                                    )
                                    for item in request.capabilities
                                ],
                                display_name=request.display_name,
                                hostname=request.hostname,
                                platform=request.platform,
                                max_concurrent_jobs=request.max_concurrent_jobs,
                                max_browser_sessions=request.max_browser_sessions,
                                active_browser_sessions=request.active_browser_sessions,
                                healthy=request.healthy,
                                access_token=token,
                            )
                            await websocket.send_json(
                                {
                                    "type": "worker.hello.accepted",
                                    "status": presence.status.value,
                                    "protocol_compatible": presence.protocol_compatible,
                                }
                            )
                        elif message_type == "worker.heartbeat":
                            request = WorkerHeartbeatRequest.model_validate(body)
                            presence = await control.heartbeat(
                                worker_id,
                                healthy=request.healthy,
                                active_browser_sessions=request.active_browser_sessions,
                                access_token=token,
                            )
                            await websocket.send_json(
                                {
                                    "type": "worker.heartbeat.accepted",
                                    "status": presence.status.value,
                                }
                            )
                        else:
                            await websocket.send_json(
                                {"type": "error", "code": "UNSUPPORTED_MESSAGE"}
                            )
                    except WorkerControlError as error:
                        if error.code == "WORKER_UNAUTHORIZED":
                            await websocket.close(code=4401)
                            return
                        await websocket.send_json({"type": "error", "code": "INVALID_MESSAGE"})
                    except ValidationError:
                        await websocket.send_json({"type": "error", "code": "INVALID_MESSAGE"})

            send_task = asyncio.create_task(send_notifications())
            receive_task = asyncio.create_task(receive_messages())
            tasks = {send_task, receive_task}
            try:
                await asyncio.wait_for(
                    asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED),
                    timeout=control.session_time_remaining(session),
                )
            except TimeoutError:
                await websocket.close(code=4401)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    return router


def _presence_response(presence: WorkerPresence) -> WorkerPresenceResponse:
    return WorkerPresenceResponse(
        worker_id=presence.worker_id,
        status=presence.status.value,
        last_heartbeat_at=presence.last_heartbeat_at,
        presence_expires_at=presence.presence_expires_at,
        protocol_compatible=presence.protocol_compatible,
        max_browser_sessions=presence.max_browser_sessions,
        active_browser_sessions=presence.active_browser_sessions,
    )


def _worker_session_response(session: WorkerAccountSession) -> WorkerSessionReportResponse:
    return WorkerSessionReportResponse(
        account_id=session.account_id,
        worker_id=session.worker_id,
        profile_ref=session.profile_ref,
        session_id=session.session_id,
        state=session.state,
        revision=session.revision,
        requires_intervention=session.requires_intervention,
        updated_at=session.updated_at,
    )


def _worker_session_error(error: WorkerSessionControlError) -> HTTPException:
    if error.code in {
        "ACCOUNT_WORKER_AFFINITY_MISMATCH",
        "SESSION_ALREADY_OWNED",
        "SESSION_REPORT_STALE",
    }:
        status_code = 409
    elif error.code in {"NETWORK_PROFILE_NOT_FOUND", "ACCOUNT_NOT_FOUND"}:
        status_code = 404
    else:
        status_code = 422
    return HTTPException(status_code=status_code, detail={"code": error.code})


def _worker_job_response(job: WorkerJob) -> WorkerJobResponse:
    return WorkerJobResponse(
        id=job.id,
        command_id=job.command_id,
        account_id=job.account_id,
        assigned_worker_id=job.assigned_worker_id,
        capability_name=job.capability_name,
        capability_version=job.capability_version,
        status=job.status,
        priority=job.priority,
        preemptible=job.preemptible,
        scheduled_at=job.scheduled_at,
        deadline_at=job.deadline_at,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        retry_safety=job.retry_safety,
        lease_worker_id=job.lease_worker_id,
        lease_token=job.lease_token,
        lease_expires_at=job.lease_expires_at,
        checkpoint=job.checkpoint,
        result=job.result,
        error_code=job.error_code,
    )


def _http_error(error: WorkerControlError) -> HTTPException:
    if error.code in {
        "INVALID_PUBLIC_KEY",
        "INVALID_BROWSER_SESSION_CAPACITY",
        "INVALID_ACTIVE_BROWSER_SESSIONS",
        "BROWSER_CAPACITY_SUMMARY_REQUIRED",
        "BROWSER_SESSION_CAPACITY_EXCEEDED",
    }:
        status_code = 422
    elif error.code in {"WORKER_NOT_FOUND", "WORKER_NOT_AUTHENTICATABLE"}:
        status_code = 404
    else:
        status_code = 401
    return HTTPException(status_code=status_code, detail={"code": error.code})


def _worker_job_error(error: WorkerJobControlError) -> HTTPException:
    if error.code in {"WORKER_JOB_NOT_FOUND", "WORKER_NOT_FOUND", "INTERVENTION_NOT_FOUND"}:
        status_code = 404
    elif error.code in {
        "WORKER_NOT_ELIGIBLE",
        "WORKER_JOB_LEASE_LOST",
        "INTERVENTION_ALREADY_RESOLVED",
        "INTERVENTION_REQUEUE_NOT_SAFE",
        "ACCOUNT_WORKER_AFFINITY_MISMATCH",
        "ACTIVE_ACCOUNT_ASSIGNMENT_REQUIRED",
    }:
        status_code = 409
    else:
        status_code = 422
    return HTTPException(status_code=status_code, detail={"code": error.code})


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    return token


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("invalid base64 value") from error
