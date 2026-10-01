from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from threads_platform.app import create_app
from threads_platform.application.commands.handlers import (
    CommandExecutionContext,
    CommandExecutionOutput,
)
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.crm_protocol_v1 import CommandEnvelopeV1
from threads_platform.application.operational_metrics import OperationalMetricsSnapshot
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.config.settings import Settings
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import CommandStatus
from threads_platform.domain.worker_jobs import WorkerJobStatus
from threads_platform.domain.workers import WorkerStatus
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory
from threads_platform.observability.tracing import (
    HTTP_SERVICE_NAME,
    create_process_tracing,
)
from threads_platform.transport.http.auth import CommandAuthenticator

pytestmark = pytest.mark.integration

_COMMAND_ID = "SYNTHETIC_COMMAND_ID"
_CORRELATION_ID = "SYNTHETIC_CORRELATION_ID"
_REQUEST_PAYLOAD = "SYNTHETIC_COMMAND_PAYLOAD"
_RESULT_PAYLOAD = "SYNTHETIC_COMMAND_RESULT"
_AUTH_TOKEN = "SYNTHETIC_COMMAND_AUTH_TOKEN"


class _Handler:
    async def execute(
        self,
        command: CommandEnvelopeV1,
        context: CommandExecutionContext,
    ) -> CommandExecutionOutput:
        del command, context
        return CommandExecutionOutput(result={"value": _RESULT_PAYLOAD})


class _Authenticator(CommandAuthenticator):
    def is_authorized(self, authorization: str | None) -> bool:
        return authorization == f"Bearer {_AUTH_TOKEN}"


class _MetricsProbe:
    async def snapshot(self) -> OperationalMetricsSnapshot:
        return OperationalMetricsSnapshot(
            worker_status_counts={status: 0 for status in WorkerStatus},
            worker_job_status_counts={status: 0 for status in WorkerJobStatus},
        )


async def test_http_command_spans_are_bounded_and_metrics_remain_process_local(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    account = ThreadsAccount(
        threads_user_id=f"tracing-user-{uuid4()}",
        username=f"tracing-{uuid4().hex[:12]}",
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)

    runtime = CommandRuntime(
        cast(UnitOfWorkFactory, unit_of_work_factory),
        {"threads.publish_text": _Handler()},
    )
    exporter = InMemorySpanExporter()
    tracing = create_process_tracing(True, HTTP_SERVICE_NAME, exporter=exporter)
    application = create_app(
        Settings(database_url=None, worker_tls_required=False),
        command_runtime=runtime,
        authenticator=_Authenticator(),
        operational_metrics_probe=_MetricsProbe(),
        tracing_runtime=tracing,
    )
    body: Mapping[str, object] = {
        "protocol_version": 1,
        "command_id": _COMMAND_ID,
        "correlation_id": _CORRELATION_ID,
        "account_id": str(account.id),
        "created_at": datetime.now(UTC).isoformat(),
        "command_type": "threads.publish_text",
        "payload": {"text": _REQUEST_PAYLOAD},
    }
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span_id = "00f067aa0ba902b7"
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/commands?customer=SYNTHETIC_QUERY_VALUE",
                json=body,
                headers={
                    "authorization": f"Bearer {_AUTH_TOKEN}",
                    "traceparent": f"00-{trace_id}-{parent_span_id}-01",
                    "cookie": "session=SYNTHETIC_COOKIE_VALUE",
                },
            )
            assert response.status_code == 202
            result = await runtime.process(_COMMAND_ID)
            metrics_response = await client.get("/metrics")

    assert result.status is CommandStatus.SUCCEEDED
    assert "threads_platform_command_execution_duration_seconds_count 1.0" in (
        metrics_response.text
    )
    assert "trace_id" not in metrics_response.text
    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {
        "http.server.request",
        "command.receive",
        "command.process",
    }
    http_span = next(span for span in spans if span.name == "http.server.request")
    receive_span = next(span for span in spans if span.name == "command.receive")
    process_span = next(span for span in spans if span.name == "command.process")
    assert http_span.parent is not None
    assert f"{http_span.parent.trace_id:032x}" == trace_id
    assert receive_span.parent is not None
    http_span_context = http_span.context
    assert http_span_context is not None
    assert receive_span.parent.span_id == http_span_context.span_id
    assert http_span.attributes == {
        "process.role": "http",
        "http.request.method": "POST",
        "http.route": "/v1/commands",
        "http.response.status_code": 202,
    }
    process_attributes = process_span.attributes
    assert process_attributes is not None
    assert process_attributes["command.status"] == CommandStatus.SUCCEEDED.value
    for span in spans:
        attributes = span.attributes or {}
        assert (
            not {
                "command_id",
                "correlation_id",
                "account_id",
                "worker_id",
                "worker_job_id",
                "command.type",
                "http.request.body",
                "http.request.header.authorization",
                "http.url",
                "db.statement",
                "db.url",
            }
            & attributes.keys()
        )
        assert not span.events
    serialized_spans = json.dumps([span.to_json() for span in spans])
    for sentinel in (
        _COMMAND_ID,
        _CORRELATION_ID,
        str(account.id),
        _REQUEST_PAYLOAD,
        _RESULT_PAYLOAD,
        _AUTH_TOKEN,
        "SYNTHETIC_QUERY_VALUE",
        "SYNTHETIC_COOKIE_VALUE",
        "postgresql://",
        "SELECT ",
    ):
        assert sentinel not in serialized_spans

    async with unit_of_work_factory() as unit_of_work:
        persisted = await unit_of_work.commands.get_by_command_id(_COMMAND_ID)
    assert persisted is not None
    assert not hasattr(persisted, "trace_id")
    assert not hasattr(persisted, "span_id")
