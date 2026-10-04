import json

from starlette.types import ASGIApp, Receive, Scope, Send


class TransportSecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        protected_path = any(
            path == prefix or path.startswith(f"{prefix}/")
            for prefix in ("/v1/operator", "/v1/workers")
        )
        if not protected_path:
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
