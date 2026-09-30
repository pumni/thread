import asyncio
import math
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.operational_metrics import (
    OperationalMetricsProbe,
    OperationalMetricsSnapshot,
)
from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import WorkerStatus
from threads_platform.infrastructure.persistence.models import (
    WorkerJobRecord,
    WorkerNodeRecord,
)


class PostgresOperationalMetricsProbe(OperationalMetricsProbe):
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("metrics probe timeout must be finite and positive")
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds

    async def snapshot(self) -> OperationalMetricsSnapshot:
        async with asyncio.timeout(self._timeout_seconds):
            async with self._session_factory() as session:
                worker_rows = (
                    await session.execute(
                        select(WorkerNodeRecord.status, func.count()).group_by(
                            WorkerNodeRecord.status
                        )
                    )
                ).all()
                worker_job_rows = (
                    await session.execute(
                        select(WorkerJobRecord.status, func.count()).group_by(
                            WorkerJobRecord.status
                        )
                    )
                ).all()

        worker_counts = {status: 0 for status in WorkerStatus}
        for status, count in worker_rows:
            worker_counts[WorkerStatus(status)] = int(count)

        worker_job_counts = {status: 0 for status in WorkerJobStatus}
        for status, count in worker_job_rows:
            worker_job_counts[WorkerJobStatus(status)] = int(count)

        return OperationalMetricsSnapshot(
            worker_status_counts=worker_counts,
            worker_job_status_counts=worker_job_counts,
        )
