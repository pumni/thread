from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.infrastructure.persistence.database import create_database_engine
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


async def test_worker_job_input_migration_refuses_loss_and_roundtrips_empty_data(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    service = WorkerJobService(unit_of_work_factory)
    job = await service.enqueue(
        "threads.browser.feed.browse",
        1,
        input_data={"max_items": 6},
    )
    config = Config("alembic.ini")
    database_url = os.environ["THREADS_PLATFORM_DATABASE_URL"]
    engine = create_database_engine(database_url)
    try:
        with pytest.raises(RuntimeError, match="WORKER_JOB_INPUT_DATA_DOWNGRADE_BLOCKED"):
            await asyncio.to_thread(command.downgrade, config, "20260926_0009")

        async with unit_of_work_factory() as unit_of_work:
            stored = await unit_of_work.worker_jobs.get(job.id)
        assert stored is not None
        assert stored.input_data == {"max_items": 6}

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE worker_jobs SET input_data = '{}'::jsonb WHERE id = :job_id"),
                {"job_id": job.id},
            )

        await asyncio.to_thread(command.downgrade, config, "20260926_0009")
        try:
            async with engine.connect() as connection:
                column_exists = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'worker_jobs' "
                        "AND column_name = 'input_data')"
                    )
                )
                assert column_exists is False
        finally:
            await asyncio.to_thread(command.upgrade, config, "head")

        async with unit_of_work_factory() as unit_of_work:
            upgraded = await unit_of_work.worker_jobs.get(job.id)
        assert upgraded is not None
        assert upgraded.input_data == {}
    finally:
        await engine.dispose()
