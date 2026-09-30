from __future__ import annotations

import logging
import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Literal, cast
from urllib.parse import urlsplit

import structlog
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_SERVICE_NAME = "threads-platform-http"
SCHEDULER_SERVICE_NAME = "threads-platform-scheduler"
TracingServiceName = Literal["threads-platform-http", "threads-platform-scheduler"]
_INSTRUMENTATION_SCOPE = "threads_platform"
_EXPORT_TIMEOUT_SECONDS = 2.0
_FLUSH_TIMEOUT_MILLIS = 2_500
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"})
_DIAGNOSTIC_ROUTES = frozenset({"/health", "/ready", "/metrics"})
_TRACE_CONTEXT_PROPAGATOR = TraceContextTextMapPropagator()


class _OpenTelemetryLogFilter(logging.Filter):
    """Hide exporter diagnostics that may include endpoint or response details."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("opentelemetry."):
            record.msg = "opentelemetry_internal_event"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def install_opentelemetry_log_filter() -> None:
    root = logging.getLogger()
    if any(isinstance(item, _OpenTelemetryLogFilter) for item in root.filters):
        return
    for handler in root.handlers:
        if not any(isinstance(item, _OpenTelemetryLogFilter) for item in handler.filters):
            handler.addFilter(_OpenTelemetryLogFilter())


class ProcessTracing:
    """Process-owned tracer provider with a safe, fixed service resource."""

    def __init__(
        self,
        service_name: TracingServiceName,
        provider: TracerProvider | None,
        tracer: Tracer | None,
    ) -> None:
        self.service_name = service_name
        self._provider = provider
        self.tracer = tracer
        self.enabled = tracer is not None
        self._shutdown = False

    def __repr__(self) -> str:
        return f"ProcessTracing(service_name={self.service_name!r}, enabled={self.enabled})"

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self._provider is None:
            return
        logger = structlog.get_logger(__name__)
        try:
            flushed = self._provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MILLIS)
            if not flushed:
                logger.warning("tracing_exporter_flush_failed", service_role=self.service_name)
        except Exception:
            logger.warning("tracing_exporter_flush_failed", service_role=self.service_name)
        try:
            self._provider.shutdown()
        except Exception:
            logger.warning("tracing_exporter_shutdown_failed", service_role=self.service_name)


def create_process_tracing(
    enabled: bool,
    service_name: TracingServiceName,
    *,
    exporter: SpanExporter | None = None,
) -> ProcessTracing:
    if not enabled:
        return ProcessTracing(service_name, None, None)

    install_opentelemetry_log_filter()
    provider = TracerProvider(
        resource=Resource({"service.name": service_name}),
        sampler=ParentBased(ALWAYS_ON),
    )
    resolved_exporter = exporter
    if resolved_exporter is None:
        try:
            endpoint = resolve_exporter_endpoint()
            resolved_exporter = OTLPSpanExporter(
                endpoint=endpoint,
                timeout=_EXPORT_TIMEOUT_SECONDS,
                max_request_size=1_048_576,
            )
        except Exception:
            structlog.get_logger(__name__).warning(
                "tracing_exporter_initialization_failed",
                service_role=service_name,
            )
            return ProcessTracing(
                service_name,
                provider,
                provider.get_tracer(_INSTRUMENTATION_SCOPE),
            )

    provider.add_span_processor(
        BatchSpanProcessor(
            resolved_exporter,
            max_queue_size=64,
            schedule_delay_millis=1_000,
            max_export_batch_size=64,
            export_timeout_millis=2_000,
        )
    )
    return ProcessTracing(
        service_name,
        provider,
        provider.get_tracer(_INSTRUMENTATION_SCOPE),
    )


def resolve_exporter_endpoint() -> str | None:
    traces_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    generic_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if traces_endpoint:
        endpoint = traces_endpoint
    elif generic_endpoint:
        endpoint = f"{generic_endpoint.removesuffix('/')}/v1/traces"
    else:
        return None

    try:
        parsed = urlsplit(endpoint)
        valid = (
            parsed.scheme.casefold() in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and "?" not in endpoint
            and not parsed.fragment
            and "#" not in endpoint
            and not any(character.isspace() or ord(character) < 0x20 for character in endpoint)
        )
        _ = parsed.port  # Validate a configured numeric port without disclosing it.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("OpenTelemetry trace endpoint configuration is invalid")
    return endpoint


@contextmanager
def trace_span(
    tracer: Tracer | None,
    name: str,
    *,
    attributes: Mapping[str, str | int | float | bool] | None = None,
    parent_context: Context | None = None,
) -> Generator[Span | None]:
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(
        name,
        context=parent_context,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except Exception:
            mark_span_failed(span)
            raise


def mark_span_failed(span: Span | None) -> None:
    if span is None:
        return
    try:
        span.set_attribute("error.type", "exception")
        span.set_status(Status(StatusCode.ERROR))
    except Exception:
        pass


def set_span_attribute(
    span: Span | None,
    name: str,
    value: str | int | float | bool,
) -> None:
    if span is None:
        return
    try:
        span.set_attribute(name, value)
    except Exception:
        pass


class HTTPRequestTracingMiddleware:
    """Trace inbound HTTP requests using only the W3C traceparent header."""

    def __init__(self, app: ASGIApp, tracing: ProcessTracing) -> None:
        self.app = app
        self._tracer = tracing.tracer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._tracer is None:
            await self.app(scope, receive, send)
            return
        path = scope.get("path")
        if path in _DIAGNOSTIC_ROUTES:
            await self.app(scope, receive, send)
            return

        parent_context = _extract_trace_context(scope)
        method_value = scope.get("method")
        method = method_value.upper() if isinstance(method_value, str) else ""
        bounded_method = method if method in _HTTP_METHODS else "OTHER"
        status_code: int | None = None

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                candidate = message.get("status")
                if isinstance(candidate, int) and 100 <= candidate <= 599:
                    status_code = candidate
            await send(message)

        with trace_span(
            self._tracer,
            "http.server.request",
            attributes={
                "process.role": "http",
                "http.request.method": bounded_method,
            },
            parent_context=parent_context,
        ) as span:
            try:
                await self.app(scope, receive, send_with_status)
            except Exception:
                mark_span_failed(span)
                raise
            finally:
                set_span_attribute(span, "http.route", _bounded_route_template(scope))
                if status_code is not None:
                    set_span_attribute(span, "http.response.status_code", status_code)


def _extract_trace_context(scope: Scope) -> Context:
    traceparent: str | None = None
    headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
    for key, value in headers:
        if key.lower() == b"traceparent":
            try:
                traceparent = value.decode("ascii")
            except UnicodeDecodeError:
                traceparent = None
            break
    carrier = {"traceparent": traceparent} if traceparent is not None else {}
    return _TRACE_CONTEXT_PROPAGATOR.extract(carrier)


def _bounded_route_template(scope: Scope) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if (
        isinstance(template, str)
        and template.startswith("/")
        and len(template) <= 256
        and "?" not in template
        and "#" not in template
    ):
        return template
    return "unmatched"
