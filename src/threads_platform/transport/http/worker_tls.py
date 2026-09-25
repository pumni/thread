import json

from starlette.types import ASGIApp, Receive, Scope, Send


class WorkerTransportTLSMiddleware:
    def __init__(self, app: ASGIApp, *, required: bool = True) -> None:
        self._app = app
        self._required = required

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        is_worker_path = path == "/v1/workers" or path.startswith("/v1/workers/")
        if not self._required or not is_worker_path:
            await self._app(scope, receive, send)
            return
        secure_scheme = "https" if scope["type"] == "http" else "wss"
        if scope.get("scheme") == secure_scheme:
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        body = json.dumps({"detail": {"code": "HTTPS_REQUIRED"}}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 426,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
