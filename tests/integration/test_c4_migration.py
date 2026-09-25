import pytest
from alembic import command
from alembic.config import Config

from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


def test_c4_migration_upgrade_downgrade_reupgrade(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    del unit_of_work_factory
    config = Config("alembic.ini")
    command.downgrade(config, "20260925_0008")
    command.upgrade(config, "head")
