import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID


class WorkerNotificationHub:
    """Process-local advisory notifications; durable work stays in PostgreSQL."""

    def __init__(self, queue_size: int = 64) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[UUID, set[asyncio.Queue[dict[str, object]]]] = defaultdict(set)

    @asynccontextmanager
    async def subscribe(self, worker_id: UUID) -> AsyncGenerator[asyncio.Queue[dict[str, object]]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers[worker_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[worker_id].discard(queue)
            if not self._subscribers[worker_id]:
                self._subscribers.pop(worker_id, None)

    def publish(self, worker_id: UUID, message: dict[str, object]) -> None:
        for queue in tuple(self._subscribers.get(worker_id, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(dict(message))
