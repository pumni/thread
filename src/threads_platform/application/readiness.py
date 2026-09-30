from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class OverallReadiness(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"


class DatabaseReadiness(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class FleetReadiness(StrEnum):
    EMPTY = "EMPTY"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, slots=True)
class WorkerFleetReadinessCounts:
    total: int = 0
    online: int = 0
    degraded: int = 0
    draining: int = 0
    offline: int = 0
    registering: int = 0
    upgrade_required: int = 0


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    overall: OverallReadiness
    database: DatabaseReadiness
    fleet: FleetReadiness
    workers_available: bool
    workers: WorkerFleetReadinessCounts


class OperationalReadinessProbe(Protocol):
    async def snapshot(self) -> ReadinessSnapshot: ...


def readiness_snapshot_for_workers(
    counts: WorkerFleetReadinessCounts,
) -> ReadinessSnapshot:
    if counts.total == 0:
        fleet = FleetReadiness.EMPTY
        overall = OverallReadiness.READY
    elif counts.online == counts.total:
        fleet = FleetReadiness.HEALTHY
        overall = OverallReadiness.READY
    else:
        fleet = FleetReadiness.DEGRADED
        overall = OverallReadiness.DEGRADED
    return ReadinessSnapshot(
        overall=overall,
        database=DatabaseReadiness.UP,
        fleet=fleet,
        workers_available=True,
        workers=counts,
    )


def database_unavailable_snapshot() -> ReadinessSnapshot:
    return ReadinessSnapshot(
        overall=OverallReadiness.NOT_READY,
        database=DatabaseReadiness.DOWN,
        fleet=FleetReadiness.DEGRADED,
        workers_available=False,
        workers=WorkerFleetReadinessCounts(),
    )
