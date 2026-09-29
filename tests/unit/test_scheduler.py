from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import cast

import pytest

from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.scheduler import (
    MAX_SCHEDULER_BATCH_SIZE,
    SchedulerRunner,
    SchedulerRunnerConfig,
    SchedulerTickResult,
    run_scheduler_tick,
)
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.scheduler import install_shutdown_handlers


class FixedClock:
    def __init__(self, current_time: datetime) -> None:
        self.current_time = current_time

    def now(self) -> datetime:
        return self.current_time


def test_scheduler_runner_config_rejects_invalid_limits_and_poll_interval() -> None:
    with pytest.raises(ValueError, match="activity_limit"):
        SchedulerRunnerConfig(activity_limit=0)
    with pytest.raises(ValueError, match="command_limit"):
        SchedulerRunnerConfig(command_limit=MAX_SCHEDULER_BATCH_SIZE + 1)
    with pytest.raises(ValueError, match="poll interval"):
        SchedulerRunnerConfig(poll_interval=timedelta(0))


@pytest.mark.parametrize("limit", [0, -1, MAX_SCHEDULER_BATCH_SIZE + 1, True])
async def test_scheduler_tick_rejects_invalid_batch_limits_before_work(limit: int) -> None:
    with pytest.raises(ValueError, match="activity_limit"):
        await run_scheduler_tick(
            cast(UnitOfWorkFactory, object()),
            cast(CommandRuntime, object()),
            cast(WorkerJobService, object()),
            now=datetime(2026, 9, 29, tzinfo=UTC),
            activity_limit=limit,
            command_limit=1,
            recovery_limit=1,
        )


async def test_runner_executes_tick_then_waits_without_real_sleep() -> None:
    now = datetime(2026, 9, 29, tzinfo=UTC)
    stop_event = asyncio.Event()
    tick_calls: list[dict[str, object]] = []
    wait_calls: list[float] = []

    async def tick(**kwargs: object) -> SchedulerTickResult:
        tick_calls.append(kwargs)
        return SchedulerTickResult(1, 2, 3)

    async def wait_for_stop(event: asyncio.Event, delay: float) -> None:
        wait_calls.append(delay)
        event.set()

    runner = SchedulerRunner(
        tick,
        SchedulerRunnerConfig(
            poll_interval=timedelta(seconds=7),
            activity_limit=2,
            command_limit=3,
            recovery_limit=4,
        ),
        clock=FixedClock(now),
        wait_for_stop=wait_for_stop,
    )

    await runner.run(stop_event)

    assert tick_calls == [
        {
            "now": now,
            "activity_limit": 2,
            "command_limit": 3,
            "recovery_limit": 4,
        }
    ]
    assert wait_calls == [7]


async def test_shutdown_waits_for_the_current_tick_boundary() -> None:
    stop_event = asyncio.Event()
    tick_started = asyncio.Event()
    release_tick = asyncio.Event()
    tick_calls = 0

    async def tick(**_: object) -> SchedulerTickResult:
        nonlocal tick_calls
        tick_calls += 1
        tick_started.set()
        await release_tick.wait()
        return SchedulerTickResult(0, 0, 0)

    async def wait_for_stop(_event: asyncio.Event, _delay: float) -> None:
        pytest.fail("a stopped runner must not begin another poll wait")

    runner = SchedulerRunner(tick, wait_for_stop=wait_for_stop)
    running = asyncio.create_task(runner.run(stop_event))
    await tick_started.wait()

    stop_event.set()
    assert not running.done()
    release_tick.set()
    await running

    assert tick_calls == 1


async def test_runner_uses_bounded_backoff_after_tick_failure() -> None:
    stop_event = asyncio.Event()
    tick_calls = 0
    waits: list[float] = []

    async def tick(**_: object) -> SchedulerTickResult:
        nonlocal tick_calls
        tick_calls += 1
        if tick_calls == 1:
            raise RuntimeError("safe synthetic failure")
        stop_event.set()
        return SchedulerTickResult(0, 0, 0)

    async def wait_for_stop(_event: asyncio.Event, delay: float) -> None:
        waits.append(delay)

    runner = SchedulerRunner(
        tick,
        SchedulerRunnerConfig(poll_interval=timedelta(seconds=2)),
        wait_for_stop=wait_for_stop,
    )

    await runner.run(stop_event)

    assert tick_calls == 2
    assert waits == [4]


async def test_sigint_and_sigterm_handlers_request_graceful_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    registered: dict[signal.Signals, Callable[[int, FrameType | None], None]] = {}

    def unsupported_signal_handler(*_args: object) -> None:
        raise NotImplementedError

    def record_signal_handler(signum: signal.Signals, handler: object) -> signal.Handlers:
        if callable(handler):
            registered[signum] = cast(Callable[[int, FrameType | None], None], handler)
        return signal.SIG_DFL

    def fake_getsignal(_signum: signal.Signals | int) -> signal.Handlers:
        return signal.SIG_DFL

    monkeypatch.setattr(loop, "add_signal_handler", unsupported_signal_handler)
    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", record_signal_handler)
    restore = install_shutdown_handlers(loop, stop_event)

    assert set(registered) == {signal.SIGINT, signal.SIGTERM}
    for signum, handler in registered.items():
        handler(int(signum), None)
    await stop_event.wait()
    restore()

    assert stop_event.is_set()
