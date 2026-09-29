from datetime import UTC, datetime
from typing import cast

import pytest

from threads_platform.application.account_activity_recurrence import (
    generate_due_account_activity_occurrences,
)
from threads_platform.application.ports.repositories import UnitOfWorkFactory


@pytest.mark.parametrize("limit", [0, -1, 101, True])
async def test_generator_rejects_invalid_limits_before_opening_a_unit_of_work(
    limit: int,
) -> None:
    with pytest.raises(ValueError, match="generation limit must be between 1 and 100"):
        await generate_due_account_activity_occurrences(
            cast(UnitOfWorkFactory, object()),
            now=datetime(2026, 10, 1, tzinfo=UTC),
            limit=limit,
        )
