from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from threading import Event
from typing import cast

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from prometheus_client import generate_latest

import threads_platform.observability.tracing as tracing_module
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.operational_metrics import OperationalMetricsSnapshot
from threads_platform.application.outbox_delivery import (
    OutboxDeliveryBatchResult,
    OutboxDeliveryWorker,
)
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.scheduler import (
    SchedulerRunner,
    SchedulerRunnerConfig,
    SchedulerTickResult,
    run_scheduler_tick,
)
from threads_platform.application.worker_control import WorkerControlService
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.config.settings import Settings
from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import WorkerStatus
from threads_platform.observability.logging import configure_logging
from threads_platform.observability.metrics import SchedulerMetrics
from threads_platform.observability.tracing import (
    HTTP_SERVICE_NAME,
    SCHEDULER_SERVICE_NAME,
    HTTPRequestTracingMiddleware,
    create_process_tracing,
    trace_span,
)


def test_tracing_is_disabled_by_default_without_constructing_an_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_exporter(**_kwargs: object) -> SpanExporter:
        raise AssertionError("the disabled tracing path must not construct an exporter")

    monkeypatch.setattr(tracing_module, "OTLPSpanExporter", unexpected_exporter)
    assert Settings().tracing_enabled is False
    tracing = create_process_tracing(Settings().tracing_enabled, HTTP_SERVICE_NAME)
    assert not tracing.enabled
    assert tracing.tracer is None
    assert "OTLP" not in repr(tracing)
    tracing.shutdown()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://" + "SYNTHETIC_USER:SYNTHETIC_PASSWORD" + "@collector.invalid/v1/traces",
        "https://collector.invalid/v1/traces?token=SYNTHETIC_QUERY_SECRET",
        "https://collector.invalid/v1/traces#SYNTHETIC_FRAGMENT_SECRET",
        "file://collector.invalid/v1/traces",
    ],
)
def test_exporter_endpoint_rejects_credentials_queries_fragments_and_schemes(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    with pytest.raises(ValueError, match="configuration is invalid") as caught:
        tracing_module.resolve_exporter_endpoint()
    assert endpoint.split("@")[0] not in str(caught.value)
    assert "SYNTHETIC_" not in str(caught.value)


def test_http_and_scheduler_have_independent_process_providers() -> None:
    http_exporter = InMemorySpanExporter()
    scheduler_exporter = InMemorySpanExporter()
    http_tracing = create_process_tracing(True, HTTP_SERVICE_NAME, exporter=http_exporter)
    scheduler_tracing = create_process_tracing(
        True,
        SCHEDULER_SERVICE_NAME,
        exporter=scheduler_exporter,
    )
    assert http_tracing.tracer is not None
    assert scheduler_tracing.tracer is not None

    with trace_span(http_tracing.tracer, "http.test"):
        pass
    with trace_span(scheduler_tracing.tracer, "scheduler.test"):
        pass

    http_tracing.shutdown()
    scheduler_tracing.shutdown()
    http_spans = http_exporter.get_finished_spans()
    scheduler_spans = scheduler_exporter.get_finished_spans()
    assert [span.name for span in http_spans] == ["http.test"]
    assert [span.name for span in scheduler_spans] == ["scheduler.test"]
    assert http_spans[0].resource.attributes == {"service.name": HTTP_SERVICE_NAME}
    assert scheduler_spans[0].resource.attributes == {"service.name": SCHEDULER_SERVICE_NAME}


@pytest.mark.asyncio
async def test_http_traceparent_uses_only_bounded_route_method_and_status() -> None:
    exporter = InMemorySpanExporter()
    tracing = create_process_tracing(True, HTTP_SERVICE_NAME, exporter=exporter)
    app = FastAPI()
    app.add_middleware(HTTPRequestTracingMiddleware, tracing=tracing)

    @app.post("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span_id = "00f067aa0ba902b7"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/items/SYNTHETIC_RAW_PATH?account_id=SYNTHETIC_QUERY",
            content="SYNTHETIC_REQUEST_PAYLOAD",
            headers={
                "traceparent": f"00-{trace_id}-{parent_span_id}-01",
                "tracestate": "vendor=SYNTHETIC_TRACESTATE",
                "baggage": "account_id=SYNTHETIC_BAGGAGE",
                "authorization": "Bearer SYNTHETIC_HTTP_TOKEN",
                "cookie": "session=SYNTHETIC_COOKIE",
            },
        )
        assert response.status_code == 200
        assert (await client.get("/health")).status_code == 200

    tracing.shutdown()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "http.server.request"
    assert span.parent is not None
    assert f"{span.parent.trace_id:032x}" == trace_id
    assert f"{span.parent.span_id:016x}" == parent_span_id
    assert span.attributes == {
        "process.role": "http",
        "http.request.method": "POST",
        "http.route": "/items/{item_id}",
        "http.response.status_code": 200,
    }
    assert not span.events
    exported = json.dumps(span.to_json())
    for sentinel in (
        "SYNTHETIC_RAW_PATH",
        "SYNTHETIC_QUERY",
        "SYNTHETIC_REQUEST_PAYLOAD",
        "SYNTHETIC_TRACESTATE",
        "SYNTHETIC_BAGGAGE",
        "SYNTHETIC_HTTP_TOKEN",
        "SYNTHETIC_COOKIE",
    ):
        assert sentinel not in exported


def test_structured_logs_include_only_active_recording_trace_ids() -> None:
    configure_logging("INFO", tracing_enabled=True)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    exporter = InMemorySpanExporter()
    tracing = create_process_tracing(True, HTTP_SERVICE_NAME, exporter=exporter)
    assert tracing.tracer is not None
    logger = structlog.get_logger("test.tracing.correlation")
    try:
        with trace_span(tracing.tracer, "log.test") as span:
            assert span is not None
            logger.info("active_trace_log", authorization="Bearer SYNTHETIC_LOG_TOKEN")
        logger.info("no_trace_log", trace_id="caller-supplied", span_id="caller-supplied")
    finally:
        root_logger.removeHandler(handler)
        tracing.shutdown()

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    active, inactive = events
    assert len(active["trace_id"]) == 32
    assert len(active["span_id"]) == 16
    assert active["trace_id"] == active["trace_id"].lower()
    assert active["span_id"] == active["span_id"].lower()
    assert active["authorization"] == "[REDACTED]"
    assert "SYNTHETIC_LOG_TOKEN" not in output.getvalue()
    assert "trace_id" not in inactive
    assert "span_id" not in inactive


class _EmptyMetricsProbe:
    async def snapshot(self) -> OperationalMetricsSnapshot:
        return OperationalMetricsSnapshot(
            worker_status_counts={status: 0 for status in WorkerStatus},
            worker_job_status_counts={status: 0 for status in WorkerJobStatus},
        )


@pytest.mark.asyncio
async def test_tracing_leaves_health_readiness_and_metrics_contract_unchanged() -> None:
    import threads_platform.app as app_module

    async def fetch_metrics(*, tracing: object | None) -> tuple[str, int, int]:
        app = app_module.create_app(
            Settings(worker_tls_required=False, database_url=None),
            operational_metrics_probe=_EmptyMetricsProbe(),
            tracing_runtime=cast(tracing_module.ProcessTracing | None, tracing),
        )
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                health_response = await client.get("/health")
                ready_response = await client.get("/ready")
                metrics_response = await client.get("/metrics")
        return metrics_response.text, health_response.status_code, ready_response.status_code

    plain = await fetch_metrics(tracing=None)
    exporter = InMemorySpanExporter()
    tracing = create_process_tracing(True, HTTP_SERVICE_NAME, exporter=exporter)
    traced = await fetch_metrics(tracing=tracing)

    def stable_metrics_contract(output: str) -> str:
        return "\n".join(
            line
            for line in output.splitlines()
            if not line.startswith("threads_platform_command_execution_duration_seconds_created ")
        )

    assert stable_metrics_contract(plain[0]) == stable_metrics_contract(traced[0])
    assert plain[1:] == (200, 503)
    assert exporter.get_finished_spans() == ()


class _WorkerControl:
    async def expire_presence(self, *, now: datetime, limit: int) -> int:
        del now, limit
        return 2


class _WorkerJobs:
    async def recover_expired(self, *, now: datetime, limit: int) -> int:
        del now, limit
        return 3


class _CommandRuntime:
    async def process_next(self, **_kwargs: object) -> None:
        return None


@pytest.mark.asyncio
async def test_scheduler_parent_fixed_stage_spans_and_failure_privacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threads_platform.application.scheduler as scheduler_module

    async def no_rows(*_args: object, **_kwargs: object) -> list[object]:
        return []

    async def fail_with_secret(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError(
            "Bearer SYNTHETIC_SCHEDULER_TOKEN "
            "postgresql://user:SYNTHETIC_DB_PASSWORD@host/db "
            "credential_ref=env://THREADS_PLATFORM_THREADS_TOKEN_SYNTHETIC_V1"
        )

    monkeypatch.setattr(scheduler_module, "dispatch_due_conversation_syncs", no_rows)
    monkeypatch.setattr(scheduler_module, "materialize_due_account_activities", no_rows)

    exporter = InMemorySpanExporter()
    tracing = create_process_tracing(True, SCHEDULER_SERVICE_NAME, exporter=exporter)
    metrics = SchedulerMetrics()
    now = datetime(2026, 9, 30, tzinfo=UTC)
    stop_event = asyncio.Event()
    successful_stop_event = asyncio.Event()

    async def successful_tick(**_kwargs: object) -> SchedulerTickResult:
        successful_stop_event.set()
        return SchedulerTickResult(0, 0, 0)

    await SchedulerRunner(
        successful_tick,
        SchedulerRunnerConfig(),
        metrics_observer=metrics,
        tracer=tracing.tracer,
    ).run(successful_stop_event)

    async def tick(**_kwargs: object) -> SchedulerTickResult:
        try:
            return await run_scheduler_tick(
                cast(UnitOfWorkFactory, object()),
                cast(CommandRuntime, _CommandRuntime()),
                cast(WorkerJobService, _WorkerJobs()),
                worker_control_service=cast(WorkerControlService, _WorkerControl()),
                now=now,
                activity_limit=1,
                command_limit=1,
                recovery_limit=1,
                metrics_observer=metrics,
                tracer=tracing.tracer,
            )
        finally:
            stop_event.set()

    monkeypatch.setattr(
        scheduler_module,
        "generate_due_account_activity_occurrences",
        fail_with_secret,
    )
    await SchedulerRunner(
        tick,
        SchedulerRunnerConfig(),
        metrics_observer=metrics,
        tracer=tracing.tracer,
    ).run(stop_event)

    monkeypatch.setattr(scheduler_module, "generate_due_account_activity_occurrences", no_rows)

    async def delivery_succeeds(*_args: object, **_kwargs: object) -> OutboxDeliveryBatchResult:
        return OutboxDeliveryBatchResult(attempted=1, succeeded=1)

    monkeypatch.setattr(scheduler_module, "deliver_due_outbox", delivery_succeeds)
    with trace_span(tracing.tracer, "scheduler.tick"):
        await run_scheduler_tick(
            cast(UnitOfWorkFactory, object()),
            cast(CommandRuntime, _CommandRuntime()),
            cast(WorkerJobService, _WorkerJobs()),
            worker_control_service=cast(WorkerControlService, _WorkerControl()),
            outbox_delivery_worker=cast(OutboxDeliveryWorker, object()),
            now=now,
            activity_limit=1,
            command_limit=1,
            recovery_limit=1,
            tracer=tracing.tracer,
        )
    tracing.shutdown()

    spans = exporter.get_finished_spans()
    names = {span.name for span in spans}
    assert "scheduler.tick" in names
    tick_spans = [span for span in spans if span.name == "scheduler.tick"]
    assert len(tick_spans) == 3
    assert any((span.attributes or {}).get("scheduler.outcome") == "success" for span in tick_spans)
    assert any(span.status.status_code is StatusCode.ERROR for span in tick_spans)
    assert {
        "scheduler.stage.worker_presence_expiry",
        "scheduler.stage.activity_recurrence_generation",
        "scheduler.stage.conversation_sync_dispatch",
        "scheduler.stage.activity_materialization",
        "scheduler.stage.command_processing",
        "scheduler.stage.worker_job_recovery",
    } <= names
    outbox_span = next(span for span in spans if span.name == "scheduler.stage.outbox_delivery")
    assert outbox_span.parent is not None
    recurrence = next(
        span for span in spans if span.name == "scheduler.stage.activity_recurrence_generation"
    )
    assert recurrence.status.status_code is StatusCode.ERROR
    recurrence_attributes = recurrence.attributes
    assert recurrence_attributes is not None
    assert recurrence_attributes["scheduler.stage"] == "activity_recurrence_generation"
    assert recurrence_attributes["error.type"] == "exception"
    assert not recurrence.events
    assert all(
        span.parent is not None for span in spans if span.name.startswith("scheduler.stage.")
    )
    serialized_spans = json.dumps([span.to_json() for span in spans])
    for sentinel in (
        "SYNTHETIC_SCHEDULER_TOKEN",
        "SYNTHETIC_DB_PASSWORD",
        "THREADS_PLATFORM_THREADS_TOKEN_SYNTHETIC_V1",
        "credential_ref",
        "postgresql://",
    ):
        assert sentinel not in serialized_spans
    metric_text = generate_latest(metrics.registry).decode("utf-8")
    assert 'threads_platform_scheduler_ticks_total{outcome="success"} 1.0' in metric_text
    assert 'threads_platform_scheduler_ticks_total{outcome="error"} 1.0' in metric_text
    assert (
        'threads_platform_scheduler_stage_failures_total{stage="activity_recurrence_generation"} '
        "1.0"
    ) in metric_text
    assert "threads_platform_scheduler_worker_job_lease_reclaims_total 3.0" in metric_text


class _FailingExporter(SpanExporter):
    def __init__(self) -> None:
        self.calls = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        del spans
        self.calls += 1
        raise RuntimeError("Bearer SYNTHETIC_EXPORTER_TOKEN")

    def shutdown(self) -> None:
        raise RuntimeError("SYNTHETIC_EXPORTER_SHUTDOWN_FAILURE")


class _BlockingShutdownExporter(SpanExporter):
    def __init__(self) -> None:
        self.shutdown_started = Event()
        self.allow_shutdown = Event()
        self.shutdown_finished = Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        del spans
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        try:
            self.shutdown_started.set()
            self.allow_shutdown.wait()
        finally:
            self.shutdown_finished.set()


class _RetryLoggingExporter(SpanExporter):
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.calls = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        del spans
        self.calls += 1
        logging.getLogger("urllib3.connectionpool").warning(
            "Retrying after an OTLP transport failure for %s",
            self.endpoint,
        )
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None


def test_tracing_shutdown_has_a_hard_timeout_for_blocked_exporter(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(tracing_module, "_SHUTDOWN_HARD_TIMEOUT_SECONDS", 0.05)
    exporter = _BlockingShutdownExporter()
    tracing = create_process_tracing(True, SCHEDULER_SERVICE_NAME, exporter=exporter)

    started_at = time.perf_counter()
    try:
        with caplog.at_level(logging.WARNING):
            tracing.shutdown()
        duration = time.perf_counter() - started_at

        assert exporter.shutdown_started.wait(timeout=1)
        assert duration < 0.5
        assert "tracing_shutdown_timed_out" in caplog.text
    finally:
        exporter.allow_shutdown.set()
        assert exporter.shutdown_finished.wait(timeout=1)


def test_transport_retry_logs_do_not_disclose_otlp_endpoint_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint = "http://collector.invalid/tenant/SYNTHETIC_TENANT_PATH_SECRET/v1/traces"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    resolved_endpoint = tracing_module.resolve_exporter_endpoint()
    assert resolved_endpoint == endpoint

    exporter = _RetryLoggingExporter(endpoint)
    transport_logger = logging.getLogger("urllib3.connectionpool")
    transport_output = io.StringIO()
    transport_handler = logging.StreamHandler(transport_output)
    transport_logger.addHandler(transport_handler)
    try:
        tracing = create_process_tracing(True, SCHEDULER_SERVICE_NAME, exporter=exporter)
        with caplog.at_level(logging.WARNING, logger="urllib3.connectionpool"):
            with trace_span(tracing.tracer, "transport.retry_test"):
                pass
            tracing.shutdown()

        assert exporter.calls >= 1
        assert endpoint not in caplog.text
        assert "SYNTHETIC_TENANT_PATH_SECRET" not in caplog.text
        rendered_transport_log = transport_output.getvalue()
        assert "opentelemetry_internal_event" in rendered_transport_log
        assert endpoint not in rendered_transport_log
        assert "SYNTHETIC_TENANT_PATH_SECRET" not in rendered_transport_log
    finally:
        transport_logger.removeHandler(transport_handler)
        transport_handler.close()


@pytest.mark.asyncio
async def test_exporter_failure_does_not_change_scheduler_result_or_leak_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = _FailingExporter()
    tracing = create_process_tracing(True, SCHEDULER_SERVICE_NAME, exporter=exporter)
    stop_event = asyncio.Event()
    result = SchedulerTickResult(0, 0, 0)

    async def tick(**_kwargs: object) -> SchedulerTickResult:
        stop_event.set()
        return result

    await SchedulerRunner(
        tick,
        SchedulerRunnerConfig(),
        tracer=tracing.tracer,
    ).run(stop_event)
    shutdown_started = time.perf_counter()
    tracing.shutdown()

    assert time.perf_counter() - shutdown_started < 5
    assert exporter.calls >= 1
    assert "SYNTHETIC_EXPORTER_TOKEN" not in caplog.text
    assert "SYNTHETIC_EXPORTER_SHUTDOWN_FAILURE" not in caplog.text
