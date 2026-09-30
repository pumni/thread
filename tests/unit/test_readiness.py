from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient

from threads_platform.app import create_app
from threads_platform.application.readiness import (
    DatabaseReadiness,
    FleetReadiness,
    OverallReadiness,
    ReadinessSnapshot,
    WorkerFleetReadinessCounts,
    database_unavailable_snapshot,
    readiness_snapshot_for_workers,
)
from threads_platform.config.settings import Settings


class StaticReadinessProbe:
    def __init__(self, result: ReadinessSnapshot) -> None:
        self.result = result

    async def snapshot(self) -> ReadinessSnapshot:
        return self.result


class FailingReadinessProbe:
    async def snapshot(self) -> ReadinessSnapshot:
        raise RuntimeError(
            "postgresql+asyncpg://synthetic:dsn-secret@db/application "
            "Bearer SYNTHETIC_EXCEPTION_SENTINEL"
        )


async def _get_ready(snapshot: ReadinessSnapshot | None = None) -> tuple[int, dict[str, object]]:
    probe = StaticReadinessProbe(snapshot) if snapshot is not None else None
    app = create_app(Settings(database_url=None, log_level="ERROR"), readiness_probe=probe)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/ready")
    return response.status_code, response.json()


async def test_ready_without_database_is_not_ready_and_contains_only_aggregate_state() -> None:
    status_code, body = await _get_ready()

    assert status_code == 503
    assert body["overall"] == "NOT_READY"
    assert body["database"] == "DOWN"
    assert body["workers_available"] is False
    assert body["workers"] == {
        "total": 0,
        "online": 0,
        "degraded": 0,
        "draining": 0,
        "offline": 0,
        "registering": 0,
        "upgrade_required": 0,
    }
    assert not {"worker_id", "hostname", "account_id", "profile_ref"} & body.keys()


@pytest.mark.parametrize(
    ("counts", "overall", "fleet", "http_status"),
    [
        (
            WorkerFleetReadinessCounts(),
            OverallReadiness.READY,
            FleetReadiness.EMPTY,
            200,
        ),
        (
            WorkerFleetReadinessCounts(total=2, online=2),
            OverallReadiness.READY,
            FleetReadiness.HEALTHY,
            200,
        ),
        (
            WorkerFleetReadinessCounts(total=1, degraded=1),
            OverallReadiness.DEGRADED,
            FleetReadiness.DEGRADED,
            200,
        ),
        (
            WorkerFleetReadinessCounts(total=1, draining=1),
            OverallReadiness.DEGRADED,
            FleetReadiness.DEGRADED,
            200,
        ),
        (
            WorkerFleetReadinessCounts(total=1, offline=1),
            OverallReadiness.DEGRADED,
            FleetReadiness.DEGRADED,
            200,
        ),
        (
            WorkerFleetReadinessCounts(total=1, registering=1),
            OverallReadiness.DEGRADED,
            FleetReadiness.DEGRADED,
            200,
        ),
        (
            WorkerFleetReadinessCounts(total=1, upgrade_required=1),
            OverallReadiness.DEGRADED,
            FleetReadiness.DEGRADED,
            200,
        ),
    ],
)
async def test_ready_fleet_semantics(
    counts: WorkerFleetReadinessCounts,
    overall: OverallReadiness,
    fleet: FleetReadiness,
    http_status: int,
) -> None:
    snapshot = readiness_snapshot_for_workers(counts)
    status_code, body = await _get_ready(snapshot)

    assert status_code == http_status
    assert body["overall"] == overall.value
    assert body["database"] == DatabaseReadiness.UP.value
    assert body["fleet"] == fleet.value
    workers = cast(dict[str, int], body["workers"])
    assert workers["total"] == counts.total


async def test_ready_database_failure_response_and_log_are_sanitized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(
        Settings(database_url=None, log_level="WARNING"),
        readiness_probe=FailingReadinessProbe(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/ready")

    rendered_output = capsys.readouterr().err
    assert response.status_code == 503
    assert response.json()["overall"] == "NOT_READY"
    assert "synthetic:dsn-secret" not in response.text
    assert "SYNTHETIC_EXCEPTION_SENTINEL" not in response.text
    assert "synthetic:dsn-secret" not in rendered_output
    assert "SYNTHETIC_EXCEPTION_SENTINEL" not in rendered_output
    assert "RuntimeError" in rendered_output


async def test_health_endpoint_stays_independent_of_readiness_probe() -> None:
    app = create_app(
        Settings(database_url=None, log_level="ERROR"),
        readiness_probe=StaticReadinessProbe(database_unavailable_snapshot()),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
