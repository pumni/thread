from __future__ import annotations

import asyncio
import re
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from httpx2 import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry, generate_latest
from pydantic import ValidationError

from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.operational_metrics import OperationalMetricsSnapshot
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.scheduler import (
    SchedulerRunner,
    SchedulerRunnerConfig,
    SchedulerStage,
    SchedulerTickOutcome,
    SchedulerTickResult,
    run_scheduler_tick,
)
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.config.settings import Settings
from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import WorkerStatus
from threads_platform.observability.metrics import ControlPlaneMetrics, SchedulerMetrics
from threads_platform.observability.metrics_server import start_scheduler_metrics_listener


def _snapshot(
    *,
    workers: Mapping[WorkerStatus, int] | None = None,
    worker_jobs: Mapping[WorkerJobStatus, int] | None = None,
) -> OperationalMetricsSnapshot:
    return OperationalMetricsSnapshot(
        worker_status_counts=workers or {status: 0 for status in WorkerStatus},
        worker_job_status_counts=worker_jobs or {status: 0 for status in WorkerJobStatus},
    )


class StaticMetricsProbe:
    def __init__(self, snapshot: OperationalMetricsSnapshot) -> None:
        self._snapshot = snapshot

    async def snapshot(self) -> OperationalMetricsSnapshot:
        return self._snapshot


class FailingMetricsProbe:
    async def snapshot(self) -> OperationalMetricsSnapshot:
        raise RuntimeError(
            "Bearer SYNTHETIC_METRIC_TOKEN postgresql://user:SYNTHETIC_DB_PASSWORD@db "
            "SYNTHETIC_WORKER_ID"
        )


class ConcurrentMetricsProbe:
    def __init__(self) -> None:
        self._calls = 0
        self._both_started = asyncio.Event()

    async def snapshot(self) -> OperationalMetricsSnapshot:
        self._calls += 1
        current = self._calls
        if self._calls == 2:
            self._both_started.set()
        await self._both_started.wait()
        counts = {status: 0 for status in WorkerStatus}
        counts[WorkerStatus.ONLINE] = current
        return _snapshot(workers=counts)


@pytest.mark.asyncio
async def test_metrics_endpoint_has_bounded_postgres_families_and_safe_failure() -> None:
    import threads_platform.app as app_module

    counts = {status: index + 1 for index, status in enumerate(WorkerStatus)}
    job_counts = {status: index + 2 for index, status in enumerate(WorkerJobStatus)}
    metrics_registry = CollectorRegistry()
    application = app_module.create_app(
        settings=Settings(),
        operational_metrics_probe=StaticMetricsProbe(
            _snapshot(workers=counts, worker_jobs=job_counts)
        ),
        metrics_registry=metrics_registry,
    )
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/metrics",
            headers={"Authorization": "Bearer SYNTHETIC_METRICS_REQUEST_HEADER"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    output = response.text
    assert "threads_platform_database_up 1.0" in output
    assert 'threads_platform_workers{status="DISABLED"} 6.0' in output
    assert 'threads_platform_worker_jobs{status="WAITING_INTERVENTION"} 4.0' in output
    assert "threads_platform_command_execution_duration_seconds_count 0.0" in output
    assert "SYNTHETIC_METRICS_REQUEST_HEADER" not in output
    sample_lines = [line for line in output.splitlines() if line.startswith("threads_platform_")]
    assert sample_lines
    assert all(line.split("{", 1)[0].startswith("threads_platform_") for line in sample_lines)
    for line in sample_lines:
        if line.startswith(("threads_platform_workers{", "threads_platform_worker_jobs{")):
            labels = re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)=", line.split("}", 1)[0])
            assert labels == ["status"]
    assert not any(
        forbidden in output.casefold()
        for forbidden in (
            "worker_id",
            "worker_job_id",
            "account_id",
            "command_id",
            "correlation_id",
            "hostname",
            "credential_ref",
            "authorization",
        )
    )

    down_app = app_module.create_app(
        settings=Settings(),
        operational_metrics_probe=FailingMetricsProbe(),
        metrics_registry=CollectorRegistry(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=down_app),
        base_url="http://test",
    ) as client:
        down_response = await client.get("/metrics")
    assert down_response.status_code == 200
    assert "threads_platform_database_up 0.0" in down_response.text
    assert not re.search(r"^threads_platform_workers\{", down_response.text, re.MULTILINE)
    assert not re.search(r"^threads_platform_worker_jobs\{", down_response.text, re.MULTILINE)
    for sentinel in (
        "SYNTHETIC_METRIC_TOKEN",
        "SYNTHETIC_DB_PASSWORD",
        "SYNTHETIC_WORKER_ID",
        "SYNTHETIC_METRICS_REQUEST_HEADER",
    ):
        assert sentinel not in down_response.text


@pytest.mark.asyncio
async def test_concurrent_metrics_scrapes_keep_snapshot_labels_isolated() -> None:
    import threads_platform.app as app_module

    application = app_module.create_app(
        settings=Settings(),
        operational_metrics_probe=ConcurrentMetricsProbe(),
        metrics_registry=CollectorRegistry(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        responses = await asyncio.gather(client.get("/metrics"), client.get("/metrics"))
    assert all(response.status_code == 200 for response in responses)
    online_values: set[float] = set()
    for response in responses:
        match = re.search(
            r'^threads_platform_workers\{status="ONLINE"\} ([0-9.]+)$',
            response.text,
            re.MULTILINE,
        )
        assert match is not None
        online_values.add(float(match.group(1)))
    assert online_values == {1.0, 2.0}


async def test_scheduler_tick_metrics_use_bounded_process_local_registry() -> None:
    metrics = SchedulerMetrics()
    success_stop = asyncio.Event()

    async def successful_tick(**_kwargs: object) -> SchedulerTickResult:
        success_stop.set()
        return SchedulerTickResult(
            activities_materialized=0,
            commands_processed=0,
            worker_jobs_recovered=3,
            worker_presences_expired=4,
        )

    success_times = iter((10.0, 11.5))
    await SchedulerRunner(
        successful_tick,
        SchedulerRunnerConfig(),
        metrics_observer=metrics,
        monotonic=lambda: next(success_times),
    ).run(success_stop)

    error_stop = asyncio.Event()

    async def failed_tick(**_kwargs: object) -> SchedulerTickResult:
        error_stop.set()
        raise RuntimeError("Bearer SYNTHETIC_SCHEDULER_FAILURE")

    error_times = iter((20.0, 20.25))
    await SchedulerRunner(
        failed_tick,
        SchedulerRunnerConfig(),
        metrics_observer=metrics,
        monotonic=lambda: next(error_times),
    ).run(error_stop)

    output = generate_latest(metrics.registry).decode("utf-8")
    assert 'threads_platform_scheduler_ticks_total{outcome="success"} 1.0' in output
    assert 'threads_platform_scheduler_ticks_total{outcome="error"} 1.0' in output
    assert "threads_platform_scheduler_tick_duration_seconds_count 2.0" in output
    assert "threads_platform_scheduler_tick_duration_seconds_sum 1.75" in output
    assert "threads_platform_scheduler_worker_presences_expired_total 4.0" in output
    assert "threads_platform_scheduler_worker_job_lease_reclaims_total 3.0" in output
    assert "SYNTHETIC_SCHEDULER_FAILURE" not in output
    assert "threads_platform_workers" not in output


@pytest.mark.asyncio
async def test_scheduler_stage_failure_records_partial_recovery_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threads_platform.application.scheduler as scheduler_module

    async def no_rows(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(scheduler_module, "generate_due_account_activity_occurrences", no_rows)
    monkeypatch.setattr(scheduler_module, "dispatch_due_conversation_syncs", no_rows)
    monkeypatch.setattr(scheduler_module, "materialize_due_account_activities", no_rows)

    class PresenceFailure:
        async def expire_presence(self, *, now: datetime, limit: int) -> int:
            raise RuntimeError("Bearer SYNTHETIC_STAGE_FAILURE")

    class Recovery:
        async def recover_expired(self, *, now: datetime, limit: int) -> int:
            return 2

    class Runtime:
        async def process_next(self, **_kwargs: object) -> None:
            return None

    metrics = SchedulerMetrics()
    stop_event = asyncio.Event()

    async def tick(**_kwargs: object) -> SchedulerTickResult:
        stop_event.set()
        return await run_scheduler_tick(
            cast(UnitOfWorkFactory, object()),
            cast(CommandRuntime, Runtime()),
            cast(WorkerJobService, Recovery()),
            worker_control_service=cast(WorkerControlService, PresenceFailure()),
            now=datetime(2026, 9, 30, tzinfo=UTC),
            activity_limit=1,
            command_limit=1,
            recovery_limit=3,
            metrics_observer=metrics,
        )

    tick_times = iter((0.0, 0.5))
    await SchedulerRunner(
        tick,
        SchedulerRunnerConfig(),
        metrics_observer=metrics,
        monotonic=lambda: next(tick_times),
    ).run(stop_event)
    output = generate_latest(metrics.registry).decode("utf-8")
    assert 'threads_platform_scheduler_ticks_total{outcome="error"} 1.0' in output
    assert (
        'threads_platform_scheduler_stage_failures_total{stage="worker_presence_expiry"} 1.0'
        in output
    )
    assert "threads_platform_scheduler_worker_job_lease_reclaims_total 2.0" in output
    assert "SYNTHETIC_STAGE_FAILURE" not in output


def test_http_and_scheduler_metrics_registries_are_independent() -> None:
    http_metrics = ControlPlaneMetrics(CollectorRegistry())
    scheduler_metrics = SchedulerMetrics(CollectorRegistry())
    http_output = http_metrics.render(_snapshot())
    scheduler_metrics.observe_stage_failure(SchedulerStage.WORKER_JOB_RECOVERY)
    scheduler_output = generate_latest(scheduler_metrics.registry)
    assert b"threads_platform_scheduler_stage_failures_total" not in http_output
    assert b"threads_platform_workers" not in scheduler_output
    assert b'threads_platform_scheduler_stage_failures_total{stage="worker_job_recovery"} 1.0' in (
        scheduler_output
    )


@pytest.mark.asyncio
async def test_scheduler_metrics_listener_serves_and_stops_cleanly() -> None:
    metrics = SchedulerMetrics()
    metrics.observe_tick(SchedulerTickOutcome.SUCCESS, 0.25)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    server = await start_scheduler_metrics_listener("127.0.0.1", port, metrics.registry)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        b"GET /metrics HTTP/1.1\r\n"
        b"Host: local\r\n"
        b"Authorization: Bearer SYNTHETIC_LISTENER_TOKEN\r\n\r\n"
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"threads_platform_scheduler_ticks_total" in response
    assert b"SYNTHETIC_LISTENER_TOKEN" not in response

    server.close()
    await server.wait_closed()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


def test_scheduler_metrics_listener_settings_are_bounded() -> None:
    assert Settings().scheduler_metrics_enabled is False
    assert Settings().scheduler_metrics_host == "127.0.0.1"
    assert Settings().scheduler_metrics_port == 9101
    with pytest.raises(ValidationError):
        Settings.model_validate({"scheduler_metrics_host": "metrics.example.test"})
    with pytest.raises(ValidationError):
        Settings(scheduler_metrics_port=65_536)
