from threading import Lock

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from threads_platform.application.operational_metrics import OperationalMetricsSnapshot
from threads_platform.application.scheduler import (
    SchedulerStage,
    SchedulerTickOutcome,
    SchedulerTickResult,
)
from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import WorkerStatus


class ControlPlaneMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self._render_lock = Lock()
        self._database_up = Gauge(
            "threads_platform_database_up",
            "Whether the latest PostgreSQL metrics snapshot succeeded.",
            registry=self.registry,
        )
        self._workers = Gauge(
            "threads_platform_workers",
            "Persisted Worker count by bounded status.",
            labelnames=("status",),
            registry=self.registry,
        )
        self._worker_jobs = Gauge(
            "threads_platform_worker_jobs",
            "Persisted WorkerJob count by bounded status.",
            labelnames=("status",),
            registry=self.registry,
        )
        self._command_execution_duration = Histogram(
            "threads_platform_command_execution_duration_seconds",
            "Duration of an existing CommandRuntime processing boundary in this process.",
            registry=self.registry,
        )

    def observe_command_execution_duration(self, duration_seconds: float) -> None:
        self._command_execution_duration.observe(max(0.0, duration_seconds))

    def render(self, snapshot: OperationalMetricsSnapshot | None) -> bytes:
        with self._render_lock:
            self._workers.clear()
            self._worker_jobs.clear()
            if snapshot is None:
                self._database_up.set(0)
            else:
                self._database_up.set(1)
                for status in WorkerStatus:
                    self._workers.labels(status=status.value).set(
                        max(0, int(snapshot.worker_status_counts.get(status, 0)))
                    )
                for status in WorkerJobStatus:
                    self._worker_jobs.labels(status=status.value).set(
                        max(0, int(snapshot.worker_job_status_counts.get(status, 0)))
                    )
            return generate_latest(self.registry)


class SchedulerMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self._ticks = Counter(
            "threads_platform_scheduler_ticks",
            "Scheduler tick count by bounded outcome.",
            labelnames=("outcome",),
            registry=self.registry,
        )
        self._tick_duration = Histogram(
            "threads_platform_scheduler_tick_duration_seconds",
            "Duration of a standalone scheduler tick.",
            registry=self.registry,
        )
        self._worker_presences_expired = Counter(
            "threads_platform_scheduler_worker_presences_expired",
            "Worker presence rows expired by scheduler ticks.",
            registry=self.registry,
        )
        self._worker_job_lease_reclaims = Counter(
            "threads_platform_scheduler_worker_job_lease_reclaims",
            "WorkerJob leases recovered by scheduler ticks.",
            registry=self.registry,
        )
        self._stage_failures = Counter(
            "threads_platform_scheduler_stage_failures",
            "Scheduler stage failures by fixed stage name.",
            labelnames=("stage",),
            registry=self.registry,
        )
        self._command_execution_duration = Histogram(
            "threads_platform_command_execution_duration_seconds",
            "Duration of an existing CommandRuntime processing boundary in this process.",
            registry=self.registry,
        )

    def observe_tick(self, outcome: SchedulerTickOutcome, duration_seconds: float) -> None:
        self._ticks.labels(outcome=outcome.value).inc()
        self._tick_duration.observe(max(0.0, duration_seconds))

    def observe_tick_result(self, result: SchedulerTickResult) -> None:
        if result.worker_presences_expired > 0:
            self._worker_presences_expired.inc(result.worker_presences_expired)
        if result.worker_jobs_recovered > 0:
            self._worker_job_lease_reclaims.inc(result.worker_jobs_recovered)

    def observe_stage_failure(self, stage: SchedulerStage) -> None:
        self._stage_failures.labels(stage=stage.value).inc()

    def observe_command_execution_duration(self, duration_seconds: float) -> None:
        self._command_execution_duration.observe(max(0.0, duration_seconds))
