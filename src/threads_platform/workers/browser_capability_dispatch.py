from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID

from threads_platform.application.ports.worker_agent import (
    WorkerJobControlClient,
    WorkerJobSnapshot,
)
from threads_platform.workers.browser import WorkerJobExecution, WorkerJobLeaseLost

BrowserCapabilityHandler = Callable[[WorkerJobSnapshot], Awaitable[None]]


class BrowserCapabilityJobDispatcher:
    """Dispatches only the browser capabilities this worker explicitly advertises."""

    def __init__(
        self,
        worker_id: UUID,
        control_client: WorkerJobControlClient,
        handlers: Mapping[str, BrowserCapabilityHandler],
    ) -> None:
        self._worker_id = worker_id
        self._control_client = control_client
        self._handlers = dict(handlers)

    async def __call__(self, job: WorkerJobSnapshot) -> None:
        handler = self._handlers.get(job.capability_name)
        if handler is not None:
            await handler(job)
            return
        try:
            execution = WorkerJobExecution(job, self._worker_id, self._control_client)
        except WorkerJobLeaseLost:
            return
        try:
            await execution.fail(error_code="UNSUPPORTED_BROWSER_CAPABILITY", retryable=False)
        except WorkerJobLeaseLost:
            return

    async def aclose(self) -> None:
        for handler in self._handlers.values():
            close_handler = getattr(handler, "aclose", None)
            if close_handler is not None:
                await close_handler()
