from datetime import datetime
from typing import cast

from httpx import ASGITransport, AsyncClient, Response

from threads_platform.app import create_app
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.config.settings import Settings


async def test_health_endpoint_returns_ok() -> None:
    transport = ASGITransport(app=create_app(Settings(log_level="ERROR")))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response: Response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


async def test_fastapi_lifespan_does_not_run_worker_maintenance() -> None:
    class PresenceService:
        calls = 0

        async def expire_presence(self, *, now: datetime, limit: int) -> int:
            self.calls += 1
            return 0

    class RecoveryService:
        calls = 0

        async def recover_expired(self, limit: int = 50, *, now: datetime | None = None) -> int:
            self.calls += 1
            return 0

    presence = PresenceService()
    recovery = RecoveryService()
    app = create_app(
        Settings(log_level="ERROR"),
        worker_control_service=cast(WorkerControlService, presence),
        worker_job_service=cast(WorkerJobService, recovery),
    )

    async with app.router.lifespan_context(app):
        pass

    assert presence.calls == 0
    assert recovery.calls == 0
