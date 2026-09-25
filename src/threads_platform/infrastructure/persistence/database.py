from collections.abc import Callable

from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_database_engine(database_url: SecretStr | str) -> AsyncEngine:
    raw_url = (
        database_url.get_secret_value() if isinstance(database_url, SecretStr) else database_url
    )
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("PostgreSQL is the only supported persistent database")
    if url.get_driver_name() == "psycopg2" or url.get_driver_name() == "psycopg":
        raise ValueError("use the asyncpg PostgreSQL driver")
    if url.get_driver_name() == "postgresql":
        url = url.set(drivername="postgresql+asyncpg")
    return create_async_engine(url, pool_pre_ping=True)


def create_session_factory(
    engine: AsyncEngine,
) -> Callable[[], AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
