from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry

from threads_platform.application.commands.handlers import (
    CommandExecutionContext,
    CommandExecutionOutput,
)
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.crm_protocol_v1 import CommandEnvelopeV1
from threads_platform.config.settings import Settings
from threads_platform.domain.accounts import AccountExecutionMode, ThreadsAccount
from threads_platform.domain.worker_jobs import (
    WorkerJob,
    WorkerJobRetrySafety,
    WorkerJobStatus,
)
from threads_platform.domain.workers import WorkerNode, WorkerStatus
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.operational_metrics import (
    PostgresOperationalMetricsProbe,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


class MetricsCommandHandler:
    async def execute(
        self,
        command: CommandEnvelopeV1,
        context: CommandExecutionContext,
    ) -> CommandExecutionOutput:
        return CommandExecutionOutput(result={"completed": True})


@pytest.mark.asyncio
async def test_postgres_metrics_match_every_persisted_worker_and_job_status(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    factory = unit_of_work_factory
    now = datetime.now(UTC)
    worker_ids: dict[WorkerStatus, UUID] = {}
    async with factory() as unit_of_work:
        for status in WorkerStatus:
            worker = WorkerNode(
                worker_id=uuid4(),
                display_name=f"Synthetic metrics Worker {status.value}",
                hostname=f"metrics-{status.value}-{uuid4().hex}",
                platform="synthetic-test",
                status=status,
            )
            await unit_of_work.workers.add(worker)
            worker_ids[status] = worker.worker_id

    async with factory() as unit_of_work:
        for status in WorkerJobStatus:
            lease_worker_id = (
                worker_ids[WorkerStatus.DISABLED] if status is WorkerJobStatus.RUNNING else None
            )
            job = WorkerJob(
                capability_name="synthetic.metrics",
                capability_version=1,
                status=status,
                scheduled_at=now,
                created_at=now,
                updated_at=now,
                lease_worker_id=lease_worker_id,
                lease_token=uuid4() if status is WorkerJobStatus.RUNNING else None,
                lease_expires_at=(
                    now + timedelta(minutes=1) if status is WorkerJobStatus.RUNNING else None
                ),
                retry_safety=WorkerJobRetrySafety.SAFE_TO_RETRY,
            )
            await unit_of_work.worker_jobs.add(job)

    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    probe = PostgresOperationalMetricsProbe(create_session_factory(engine))
    try:
        first = await probe.snapshot()
        second = await probe.snapshot()
    finally:
        await engine.dispose()

    assert first == second
    assert first.worker_status_counts == dict.fromkeys(WorkerStatus, 1)
    assert first.worker_job_status_counts == dict.fromkeys(WorkerJobStatus, 1)


@pytest.mark.asyncio
async def test_command_runtime_observes_monotonic_processing_duration_on_http_metrics(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    import threads_platform.app as app_module

    factory = unit_of_work_factory
    account = ThreadsAccount(
        threads_user_id=f"metrics-user-{uuid4()}",
        username=f"metrics-{uuid4().hex[:12]}",
        execution_mode=AccountExecutionMode.API_ONLY,
    )
    async with factory() as unit_of_work:
        await unit_of_work.accounts.add(account)

    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    session_factory = create_session_factory(engine)
    probe = PostgresOperationalMetricsProbe(session_factory)
    monotonic_values = iter((100.0, 100.625))
    runtime = CommandRuntime(
        factory,
        {"threads.publish_text": MetricsCommandHandler()},
        monotonic=lambda: next(monotonic_values),
    )
    registry = CollectorRegistry()
    application = app_module.create_app(
        settings=Settings(database_url=None),
        command_runtime=runtime,
        operational_metrics_probe=probe,
        metrics_registry=registry,
    )
    command_id = f"metrics-command-{uuid4()}"
    await runtime.receive(
        {
            "protocol_version": 1,
            "command_id": command_id,
            "correlation_id": f"metrics-correlation-{uuid4()}",
            "account_id": str(account.id),
            "created_at": datetime.now(UTC).isoformat(),
            "command_type": "threads.publish_text",
            "payload": {"text": "SYNTHETIC_COMMAND_METRIC_PAYLOAD"},
        }
    )
    await runtime.process(command_id)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get("/metrics")
        assert response.status_code == 200
        assert (
            registry.get_sample_value("threads_platform_command_execution_duration_seconds_count")
            == 1.0
        )
        assert (
            registry.get_sample_value("threads_platform_command_execution_duration_seconds_sum")
            == 0.625
        )
        assert "SYNTHETIC_COMMAND_METRIC_PAYLOAD" not in response.text
        assert command_id not in response.text
    finally:
        await engine.dispose()
