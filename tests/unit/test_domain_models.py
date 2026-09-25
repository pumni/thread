import ast
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import Command, CommandStatus


def test_domain_normalizes_aware_timestamps_to_utc() -> None:
    local_time = datetime(2026, 9, 25, 12, tzinfo=timezone(timedelta(hours=7)))

    account = ThreadsAccount(threads_user_id="user-1", username="example", created_at=local_time)

    assert account.created_at == datetime(2026, 9, 25, 5, tzinfo=UTC)
    assert account.created_at.tzinfo == UTC


def test_domain_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ThreadsAccount(
            threads_user_id="user-1",
            username="example",
            created_at=datetime(2026, 9, 25),
        )


def test_expired_command_cannot_enter_processing() -> None:
    deadline = datetime(2026, 9, 25, 10, tzinfo=UTC)
    command = Command(
        command_id="cmd-1",
        correlation_id="corr-1",
        account_id=uuid4(),
        command_type="threads.publish_post",
        payload={"text": "hello"},
        deadline_at=deadline,
    )
    command.transition(CommandStatus.VALIDATED, deadline - timedelta(seconds=1))

    with pytest.raises(ValueError, match="expired command"):
        command.transition(CommandStatus.PROCESSING, deadline)


def test_domain_modules_do_not_import_infrastructure_or_transport_libraries() -> None:
    domain_root = Path(__file__).parents[2] / "src" / "threads_platform" / "domain"
    forbidden = {"fastapi", "httpx", "sqlalchemy", "websockets", "starlette"}

    for source_file in domain_root.glob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        imported_modules = {
            node.module.split(".", maxsplit=1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_modules.update(
            alias.name.split(".", maxsplit=1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert forbidden.isdisjoint(imported_modules)
