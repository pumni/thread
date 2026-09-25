import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)


def _test_database_url() -> str:
    value = os.environ.get("THREADS_PLATFORM_TEST_DATABASE_URL")
    if not value:
        pytest.skip("set THREADS_PLATFORM_TEST_DATABASE_URL to run PostgreSQL integration tests")
    database_name = make_url(value).database
    if database_name is None or not database_name.endswith("_test"):
        raise RuntimeError("integration tests require a database whose name ends with _test")
    return value


@pytest.fixture(scope="session", autouse=True)
def apply_migrations_to_test_database() -> Iterator[None]:
    test_url = _test_database_url()
    original_url = os.environ.get("THREADS_PLATFORM_DATABASE_URL")
    os.environ["THREADS_PLATFORM_DATABASE_URL"] = test_url
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield
    finally:
        if original_url is None:
            os.environ.pop("THREADS_PLATFORM_DATABASE_URL", None)
        else:
            os.environ["THREADS_PLATFORM_DATABASE_URL"] = original_url


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_database_engine(_test_database_url())
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        transaction = await session.begin()
        try:
            yield session
        finally:
            await transaction.rollback()
    await engine.dispose()
