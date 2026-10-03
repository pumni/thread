from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.crm_protocol_v1 import CommandReceiptV1
from threads_platform.application.errors import CommandInputError, IdempotencyConflict
from threads_platform.application.operator_access import OperatorAction, role_allows
from threads_platform.domain.operators import OperatorPrincipal, OperatorRole, OperatorUser
from threads_platform.infrastructure.security.operator_auth import (
    CreatedOperatorUser,
    OperatorAuthError,
    OperatorAuthService,
)


class _OperatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperatorLoginRequest(_OperatorRequest):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class OperatorUserCreateRequest(_OperatorRequest):
    username: str = Field(min_length=1, max_length=255)
    role: OperatorRole


class OperatorUserUpdateRequest(_OperatorRequest):
    role: OperatorRole | None = None
    enabled: bool | None = None


class OperatorPasswordChangeRequest(_OperatorRequest):
    new_password: str = Field(min_length=1, max_length=1024, repr=False)


class OperatorUserResponse(BaseModel):
    id: UUID
    username: str
    role: OperatorRole
    enabled: bool
    must_change_password: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user: OperatorUser) -> OperatorUserResponse:
        return cls(
            id=user.id,
            username=user.username,
            role=user.role,
            enabled=user.enabled,
            must_change_password=user.must_change_password,
            created_at=user.created_at,
        )


class OperatorPrincipalResponse(BaseModel):
    id: UUID
    username: str
    role: OperatorRole
    must_change_password: bool
    expires_at: datetime

    @classmethod
    def from_principal(cls, principal: OperatorPrincipal) -> OperatorPrincipalResponse:
        return cls(
            id=principal.user_id,
            username=principal.username,
            role=principal.role,
            must_change_password=principal.must_change_password,
            expires_at=principal.expires_at,
        )


class OperatorLoginResponse(BaseModel):
    access_token: str = Field(repr=False)
    token_type: str = "Bearer"
    expires_at: datetime
    operator: OperatorPrincipalResponse


class OperatorUserCreateResponse(BaseModel):
    user: OperatorUserResponse
    temporary_password: str = Field(repr=False)

    @classmethod
    def from_created(cls, created: CreatedOperatorUser) -> OperatorUserCreateResponse:
        return cls(
            user=OperatorUserResponse.from_user(created.user),
            temporary_password=created.temporary_password,
        )


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token or " " in token:
        return None
    return token


def _raise_operator_error(error: OperatorAuthError) -> HTTPException:
    if error.code == "OPERATOR_SESSION_INVALID":
        return HTTPException(
            status_code=401,
            detail={"code": error.code},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if error.code == "OPERATOR_FORBIDDEN" or error.code == "OPERATOR_PASSWORD_CHANGE_REQUIRED":
        return HTTPException(status_code=403, detail={"code": error.code})
    if error.code == "OPERATOR_USER_NOT_FOUND":
        return HTTPException(status_code=404, detail={"code": error.code})
    if error.code == "LAST_ENABLED_OWNER_REQUIRED":
        return HTTPException(status_code=409, detail={"code": error.code})
    if error.code == "OPERATOR_BOOTSTRAP_ALREADY_COMPLETED":
        return HTTPException(status_code=409, detail={"code": error.code})
    if error.code in {
        "OPERATOR_PASSWORD_INVALID",
        "OPERATOR_USERNAME_INVALID",
        "OPERATOR_USER_CHANGE_REQUIRED",
    }:
        return HTTPException(status_code=422, detail={"code": error.code})
    if error.code == "WORKSPACE_NOT_INITIALIZED":
        return HTTPException(status_code=503, detail={"code": error.code})
    return HTTPException(status_code=400, detail={"code": error.code})


def create_operator_router(
    service: OperatorAuthService | None,
    command_runtime: CommandRuntime | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/operator", tags=["operator"])

    def require_service() -> OperatorAuthService:
        if service is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "OPERATOR_AUTH_UNAVAILABLE"},
            )
        return service

    async def require_principal(
        authorization: str | None,
        *,
        allow_password_change: bool = False,
        action: OperatorAction | None = None,
    ) -> tuple[str, OperatorPrincipal]:
        token = _bearer_token(authorization)
        if token is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "OPERATOR_UNAUTHORIZED"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        principal = await require_service().authenticate(token)
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "OPERATOR_SESSION_INVALID"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if principal.must_change_password and not allow_password_change:
            raise HTTPException(
                status_code=403,
                detail={"code": "OPERATOR_PASSWORD_CHANGE_REQUIRED"},
            )
        if action is not None and not role_allows(principal.role, action):
            raise HTTPException(status_code=403, detail={"code": "OPERATOR_FORBIDDEN"})
        return token, principal

    @router.post("/login", response_model=OperatorLoginResponse)
    async def login(request: OperatorLoginRequest, response: Response) -> OperatorLoginResponse:
        issued = await require_service().login(request.username, request.password)
        if issued is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "OPERATOR_LOGIN_FAILED"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return OperatorLoginResponse(
            access_token=issued.access_token,
            expires_at=issued.expires_at,
            operator=OperatorPrincipalResponse.from_principal(issued.operator),
        )

    @router.get("/me", response_model=OperatorPrincipalResponse)
    async def get_me(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> OperatorPrincipalResponse:
        _, principal = await require_principal(authorization, allow_password_change=True)
        response.headers["Cache-Control"] = "no-store"
        return OperatorPrincipalResponse.from_principal(principal)

    @router.post("/logout", status_code=204)
    async def logout(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        response.headers["Cache-Control"] = "no-store"
        await require_service().logout(_bearer_token(authorization))
        response.status_code = 204
        return response

    @router.post("/me/password", response_model=OperatorPrincipalResponse)
    async def change_password(
        request: OperatorPasswordChangeRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> OperatorPrincipalResponse:
        token, _ = await require_principal(authorization, allow_password_change=True)
        try:
            principal = await require_service().change_password(token, request.new_password)
        except OperatorAuthError as error:
            raise _raise_operator_error(error) from error
        response.headers["Cache-Control"] = "no-store"
        return OperatorPrincipalResponse.from_principal(principal)

    @router.get("/users", response_model=list[OperatorUserResponse])
    async def list_users(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> list[OperatorUserResponse]:
        token, _ = await require_principal(authorization)
        try:
            users = await require_service().list_users(token)
        except OperatorAuthError as error:
            raise _raise_operator_error(error) from error
        response.headers["Cache-Control"] = "no-store"
        return [OperatorUserResponse.from_user(user) for user in users]

    @router.post("/users", response_model=OperatorUserCreateResponse, status_code=201)
    async def create_user(
        request: OperatorUserCreateRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> OperatorUserCreateResponse:
        token, _ = await require_principal(authorization)
        try:
            created = await require_service().create_user(token, request.username, request.role)
        except OperatorAuthError as error:
            raise _raise_operator_error(error) from error
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return OperatorUserCreateResponse.from_created(created)

    @router.patch("/users/{user_id}", response_model=OperatorUserResponse)
    async def update_user(
        user_id: UUID,
        request: OperatorUserUpdateRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> OperatorUserResponse:
        token, _ = await require_principal(authorization)
        try:
            user = await require_service().update_user(
                token,
                user_id,
                role=request.role,
                enabled=request.enabled,
            )
        except OperatorAuthError as error:
            raise _raise_operator_error(error) from error
        response.headers["Cache-Control"] = "no-store"
        return OperatorUserResponse.from_user(user)

    @router.post("/commands", response_model=CommandReceiptV1)
    async def submit_command(
        command: dict[str, object],
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        token, _ = await require_principal(
            authorization,
            action=OperatorAction.SUBMIT_COMMAND,
        )
        if command_runtime is None:
            raise HTTPException(status_code=503, detail={"code": "COMMAND_RUNTIME_UNAVAILABLE"})
        try:
            receipt = await command_runtime.receive(command)
        except CommandInputError as error:
            raise HTTPException(status_code=422, detail={"code": error.code}) from error
        except IdempotencyConflict as error:
            raise HTTPException(status_code=409, detail={"code": "COMMAND_ID_CONFLICT"}) from error
        try:
            await require_service().record_action(
                token,
                "operator.command_submitted",
                "command",
                receipt.command_id,
                {},
            )
        except OperatorAuthError as error:
            raise _raise_operator_error(error) from error
        return JSONResponse(
            status_code=200 if receipt.duplicate else 202,
            content=receipt.model_dump(mode="json"),
        )

    return router
