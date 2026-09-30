from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import WorkerStatus


@dataclass(frozen=True, slots=True)
class OperationalMetricsSnapshot:
    worker_status_counts: Mapping[WorkerStatus, int]
    worker_job_status_counts: Mapping[WorkerJobStatus, int]


class OperationalMetricsProbe(Protocol):
    async def snapshot(self) -> OperationalMetricsSnapshot: ...
