import asyncio
import base64
import binascii
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, WebSocket
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from threads_platform.application.worker_control import (
    WorkerControlError,
    WorkerControlService,
    WorkerPresence,
)
from threads_platform.application.worker_notifications import WorkerNotificationHub
from threads_platform.domain.workers import WorkerCapability
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
    healthy: bool = True


class WorkerHeartbeatRequest(_WorkerRequest):
    healthy: bool = True


class WorkerPresenceResponse(BaseModel):
    worker_id: UUID
    status: str
    last_heartbeat_at: AwareDatetime
    presence_expires_at: AwareDatetime
    protocol_compatible: bool


def create_worker_router(
    service: WorkerControlService | None,
    admin_authenticator: CommandAuthenticator,
    notifications: WorkerNotificationHub,
) -> APIRouter:
    router = APIRouter(prefix="/v1/workers", tags=["workers"])

    def require_service() -> WorkerControlService:
        if service is None:
            raise HTTPException(status_code=503, detail={"code": "WORKER_CONTROL_UNAVAILABLE"})
        return service

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
        try:
            enrolled = await require_service().enroll(
                request.enrollment_code,
                worker_id=request.worker_id,
                display_name=request.display_name,
                hostname=request.hostname,
                platform=request.platform,
                public_key=public_key,
                max_concurrent_jobs=request.max_concurrent_jobs,
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

    @router.post("/hello", response_model=WorkerPresenceResponse)
    async def worker_hello(
        request: WorkerHelloRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerPresenceResponse:
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
                healthy=request.healthy,
            )
        except WorkerControlError as error:
            raise _http_error(error) from error
        notifications.publish(
            worker_id,
            {"type": "worker.presence", "status": presence.status.value},
        )
        return _presence_response(presence)

    @router.post("/heartbeat", response_model=WorkerPresenceResponse)
    async def worker_heartbeat(
        request: WorkerHeartbeatRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerPresenceResponse:
        worker_id = await authenticated_worker(authorization)
        try:
            presence = await require_service().heartbeat(worker_id, healthy=request.healthy)
        except WorkerControlError as error:
            raise _http_error(error) from error
        notifications.publish(
            worker_id,
            {"type": "worker.presence", "status": presence.status.value},
        )
        return _presence_response(presence)

    @router.websocket("/connect")
    async def worker_websocket(websocket: WebSocket) -> None:
        if service is None:
            await websocket.close(code=1013)
            return
        control = service
        token = _bearer_token(websocket.headers.get("authorization"))
        worker_id = await control.authenticate(token) if token is not None else None
        if worker_id is None:
            await websocket.close(code=4401)
            return
        async with notifications.subscribe(worker_id) as queue:
            await websocket.accept()

            async def send_notifications() -> None:
                while True:
                    await websocket.send_json(await queue.get())

            async def receive_messages() -> None:
                while True:
                    raw_message: object = await websocket.receive_json()
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
                                healthy=request.healthy,
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
                            presence = await control.heartbeat(worker_id, healthy=request.healthy)
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
                    except ValidationError, WorkerControlError:
                        await websocket.send_json({"type": "error", "code": "INVALID_MESSAGE"})

            send_task = asyncio.create_task(send_notifications())
            receive_task = asyncio.create_task(receive_messages())
            done, pending = await asyncio.wait(
                {send_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)

    return router


def _presence_response(presence: WorkerPresence) -> WorkerPresenceResponse:
    return WorkerPresenceResponse(
        worker_id=presence.worker_id,
        status=presence.status.value,
        last_heartbeat_at=presence.last_heartbeat_at,
        presence_expires_at=presence.presence_expires_at,
        protocol_compatible=presence.protocol_compatible,
    )


def _http_error(error: WorkerControlError) -> HTTPException:
    status_code = 404 if error.code in {"WORKER_NOT_FOUND", "WORKER_NOT_AUTHENTICATABLE"} else 401
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
