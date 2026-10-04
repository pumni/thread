from __future__ import annotations

import uvicorn

from threads_platform.app import app


def main(host: str, port: int, ssl_certfile: str | None, ssl_keyfile: str | None) -> None:
    if not ssl_certfile or not ssl_keyfile:
        raise RuntimeError("controller_https_configuration_required")
    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        access_log=False,
        reload=False,
        proxy_headers=False,
    )
