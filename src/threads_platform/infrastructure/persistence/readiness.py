import asyncio
from collections.abc import Callable

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.readiness import (
    OperationalReadinessProbe,
    ReadinessSnapshot,
    WorkerFleetReadinessCounts,
    database_unavailable_snapshot,
    readiness_snapshot_for_workers,
)
from threads_platform.domain.workers import WorkerStatus
from threads_platform.infrastructure.persistence.models import WorkerNodeRecord


class PostgresOperationalReadinessProbe(OperationalReadinessProbe):
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("readiness timeout must be positive")
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds
        self._logger = structlog.get_logger(__name__)

    async def snapshot(self) -> ReadinessSnapshot:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    await session.execute(text("SELECT 1"))
                    rows = (
                        await session.execute(
                            select(WorkerNodeRecord.status, func.count())
                            .where(WorkerNodeRecord.status != WorkerStatus.DISABLED)
                            .group_by(WorkerNodeRecord.status)
                        )
                    ).all()
        except Exception as error:
            self._logger.warning(
                "readiness_database_probe_failed",
                error_type=type(error).__name__,
            )
            return database_unavailable_snapshot()

        status_counts = {status: 0 for status in WorkerStatus}
        for status, count in rows:
            status_counts[WorkerStatus(status)] = int(count)
        counts = WorkerFleetReadinessCounts(
            total=sum(
                count
                for status, count in status_counts.items()
                if status is not WorkerStatus.DISABLED
            ),
            online=status_counts[WorkerStatus.ONLINE],
            degraded=status_counts[WorkerStatus.DEGRADED],
            draining=status_counts[WorkerStatus.DRAINING],
            offline=status_counts[WorkerStatus.OFFLINE],
            registering=status_counts[WorkerStatus.REGISTERING],
            upgrade_required=status_counts[WorkerStatus.UPGRADE_REQUIRED],
        )
        return readiness_snapshot_for_workers(counts)
