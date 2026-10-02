from __future__ import annotations

import uvicorn

from threads_platform.app import app


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    uvicorn.run(app, host=host, port=port, access_log=False, reload=False)
