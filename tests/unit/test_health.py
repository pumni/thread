from httpx import ASGITransport, AsyncClient, Response

from threads_platform.app import create_app
from threads_platform.config.settings import Settings


async def test_health_endpoint_returns_ok() -> None:
    transport = ASGITransport(app=create_app(Settings(log_level="ERROR")))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response: Response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
