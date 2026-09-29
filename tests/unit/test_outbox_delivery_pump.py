from __future__ import annotations

from collections.abc import Callable
from typing import cast
from uuid import UUID, uuid4

import pytest

from threads_platform.application.outbox_delivery import (
    DeliveryAttemptResult,
    OutboxDeliveryWorker,
    deliver_due_outbox,
)
from threads_platform.domain.outbox import DeliveryStatus


class FakeDeliveryWorker:
    def __init__(self) -> None:
        self.delivery_ids = [uuid4(), uuid4()]
        self.exclusions: list[frozenset[UUID]] = []

    async def deliver_one(
        self,
        *,
        exclude_delivery_ids: frozenset[UUID] = frozenset(),
        on_claimed: Callable[[UUID], None] | None = None,
    ) -> DeliveryAttemptResult:
        self.exclusions.append(exclude_delivery_ids)
        for index, delivery_id in enumerate(self.delivery_ids):
            if delivery_id not in exclude_delivery_ids:
                if on_claimed is not None:
                    on_claimed(delivery_id)
                return DeliveryAttemptResult(
                    delivery_id,
                    uuid4(),
                    DeliveryStatus.FAILED_RETRYABLE if index == 0 else DeliveryStatus.DELIVERED,
                    delivered=index == 1,
                )
        return DeliveryAttemptResult(None, None, None, delivered=False)


@pytest.mark.parametrize("limit", [0, -1, 101, True])
async def test_outbox_pump_rejects_invalid_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="outbox delivery limit"):
        await deliver_due_outbox(cast(OutboxDeliveryWorker, FakeDeliveryWorker()), limit=limit)


async def test_outbox_pump_is_sequential_and_excludes_prior_attempts() -> None:
    worker = FakeDeliveryWorker()

    result = await deliver_due_outbox(cast(OutboxDeliveryWorker, worker), limit=3)

    assert result.attempted == 2
    assert result.succeeded == 1
    assert worker.exclusions == [
        frozenset(),
        frozenset({worker.delivery_ids[0]}),
        frozenset(worker.delivery_ids),
    ]
