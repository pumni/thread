from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.crm_protocol_v1 import CommandReceiptV1
from threads_platform.application.errors import CommandInputError, IdempotencyConflict
from threads_platform.transport.http.auth import CommandAuthenticator


def create_command_router(
    runtime: CommandRuntime | None,
    authenticator: CommandAuthenticator,
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/commands", response_model=CommandReceiptV1)
    async def receive_command(
        command: dict[str, object],
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if not authenticator.is_authorized(authorization):
            raise HTTPException(
                status_code=401,
                detail={"code": "UNAUTHORIZED"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if runtime is None:
            raise HTTPException(status_code=503, detail={"code": "COMMAND_RUNTIME_UNAVAILABLE"})
        try:
            receipt = await runtime.receive(command)
        except CommandInputError as error:
            raise HTTPException(status_code=422, detail={"code": error.code}) from error
        except IdempotencyConflict as error:
            raise HTTPException(status_code=409, detail={"code": "COMMAND_ID_CONFLICT"}) from error
        return JSONResponse(
            status_code=200 if receipt.duplicate else 202,
            content=receipt.model_dump(mode="json"),
        )

    return router
