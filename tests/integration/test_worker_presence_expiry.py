from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from threads_platform.application.clock import Clock
from threads_platform.application.commands.composition import compose_command_runtime
from threads_platform.application.scheduler import SchedulerTickResult, run_scheduler_tick
from threads_platform.application.worker_control import WorkerControlService, WorkerPresence
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.domain.workers import WorkerAuditEvent, WorkerNode, WorkerStatus
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.repositories import SQLAlchemyWorkerRepository
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


class FixedClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


async def _add_worker(
    factory: SQLAlchemyUnitOfWorkFactory,
    *,
    now: datetime,
    status: WorkerStatus,
    presence_expires_at: datetime | None,
) -> UUID:
    worker_id = uuid4()
    worker = WorkerNode(
        worker_id=worker_id,
        display_name=f"Presence test {worker_id}",
        hostname=f"presence-{worker_id}",
        platform="windows",
        agent_version="1.0.0",
        protocol_version=1,
        capabilities_schema_version=1,
        status=status,
        max_concurrent_jobs=2,
        last_heartbeat_at=now,
        presence_expires_at=presence_expires_at,
        created_at=now,
        updated_at=now,
    )
    async with factory() as unit_of_work:
        await unit_of_work.workers.add(worker)
    return worker_id


async def _independent_factory() -> tuple[SQLAlchemyUnitOfWorkFactory, AsyncEngine]:
    database_url = os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"]
    engine = create_database_engine(database_url)
    return SQLAlchemyUnitOfWorkFactory(create_session_factory(engine)), engine


async def _worker_and_events(
    factory: SQLAlchemyUnitOfWorkFactory, worker_id: UUID
) -> tuple[WorkerNode, list[WorkerAuditEvent]]:
    async with factory() as unit_of_work:
        worker = await unit_of_work.workers.get(worker_id)
        events = await unit_of_work.worker_security.list_audit_events(worker_id)
    assert worker is not None
    return worker, events


async def test_presence_expiry_is_bounded_and_only_mutates_expired_eligible_statuses(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    non_expirable = [
        status
        for status in WorkerStatus
        if status not in {WorkerStatus.ONLINE, WorkerStatus.DEGRADED}
    ]
    untouched = [
        (
            status,
            await _add_worker(
                unit_of_work_factory,
                now=now,
                status=status,
                presence_expires_at=now - timedelta(days=1),
            ),
        )
        for status in non_expirable
    ]
    degraded_id = await _add_worker(
        unit_of_work_factory,
        now=now,
        status=WorkerStatus.DEGRADED,
        presence_expires_at=now - timedelta(seconds=2),
    )
    boundary_id = await _add_worker(
        unit_of_work_factory,
        now=now,
        status=WorkerStatus.ONLINE,
        presence_expires_at=now,
    )
    future_id = await _add_worker(
        unit_of_work_factory,
        now=now,
        status=WorkerStatus.ONLINE,
        presence_expires_at=now + timedelta(seconds=1),
    )

    first = await WorkerControlService(unit_of_work_factory).expire_presence(now=now, limit=1)
    after_first, first_events = await _worker_and_events(unit_of_work_factory, degraded_id)
    boundary_before = (await _worker_and_events(unit_of_work_factory, boundary_id))[0]
    assert first == 1
    assert after_first.status is WorkerStatus.OFFLINE
    assert after_first.updated_at == now
    assert sum(event.event_type == "worker.presence.expired" for event in first_events) == 1
    assert boundary_before.status is WorkerStatus.ONLINE

    second = await WorkerControlService(unit_of_work_factory).expire_presence(now=now, limit=1)
    third = await WorkerControlService(unit_of_work_factory).expire_presence(now=now, limit=1)
    assert second == 1
    assert third == 0
    boundary_after, boundary_events = await _worker_and_events(unit_of_work_factory, boundary_id)
    assert boundary_after.status is WorkerStatus.OFFLINE
    assert sum(event.event_type == "worker.presence.expired" for event in boundary_events) == 1
    future, future_events = await _worker_and_events(unit_of_work_factory, future_id)
    assert future.status is WorkerStatus.ONLINE
    assert not any(event.event_type == "worker.presence.expired" for event in future_events)
    for status, worker_id in untouched:
        worker, events = await _worker_and_events(unit_of_work_factory, worker_id)
        assert worker.status is status
        assert not any(event.event_type == "worker.presence.expired" for event in events)


async def test_two_schedulers_skip_the_same_locked_worker_and_emit_one_audit(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    worker_id = await _add_worker(
        unit_of_work_factory,
        now=now,
        status=WorkerStatus.ONLINE,
        presence_expires_at=now,
    )
    second_factory, second_engine = await _independent_factory()
    first_selected = asyncio.Event()
    release_first = asyncio.Event()
    original_select = SQLAlchemyWorkerRepository.list_expired_presence_for_update
    held_selection = False

    async def hold_first_selection(
        repository: SQLAlchemyWorkerRepository, selected_at: datetime, limit: int
    ) -> list[WorkerNode]:
        nonlocal held_selection
        selected = await original_select(repository, selected_at, limit)
        if selected and not held_selection:
            held_selection = True
            first_selected.set()
            await release_first.wait()
        return selected

    monkeypatch.setattr(
        SQLAlchemyWorkerRepository,
        "list_expired_presence_for_update",
        hold_first_selection,
    )
    first_task = asyncio.create_task(
        WorkerControlService(unit_of_work_factory).expire_presence(now=now, limit=1)
    )
    try:
        await asyncio.wait_for(first_selected.wait(), timeout=10)
        second_result = await asyncio.wait_for(
            WorkerControlService(second_factory).expire_presence(now=now, limit=1),
            timeout=10,
        )
        release_first.set()
        first_result = await asyncio.wait_for(first_task, timeout=10)
    finally:
        release_first.set()
        if not first_task.done():
            await asyncio.gather(first_task, return_exceptions=True)
        await second_engine.dispose()

    worker, events = await _worker_and_events(unit_of_work_factory, worker_id)
    assert (first_result, second_result) == (1, 0)
    assert worker.status is WorkerStatus.OFFLINE
    assert sum(event.event_type == "worker.presence.expired" for event in events) == 1


async def test_distinct_stale_workers_can_be_split_across_scheduler_instances(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    first_id = await _add_worker(
        unit_of_work_factory,
        now=now,
        status=WorkerStatus.ONLINE,
        presence_expires_at=now - timedelta(seconds=2),
    )
    second_id = await _add_worker(
        unit_of_work_factory,
        now=now,
        status=WorkerStatus.DEGRADED,
        presence_expires_at=now - timedelta(seconds=1),
    )
    second_factory, second_engine = await _independent_factory()
    first_selected = asyncio.Event()
    release_first = asyncio.Event()
    selections: list[tuple[UUID, ...]] = []
    original_select = SQLAlchemyWorkerRepository.list_expired_presence_for_update

    async def hold_first_selection(
        repository: SQLAlchemyWorkerRepository, selected_at: datetime, limit: int
    ) -> list[WorkerNode]:
        selected = await original_select(repository, selected_at, limit)
        selections.append(tuple(worker.worker_id for worker in selected))
        if selected and not first_selected.is_set():
            first_selected.set()
            await release_first.wait()
        return selected

    monkeypatch.setattr(
        SQLAlchemyWorkerRepository,
        "list_expired_presence_for_update",
        hold_first_selection,
    )
    first_task = asyncio.create_task(
        WorkerControlService(unit_of_work_factory).expire_presence(now=now, limit=1)
    )
    try:
        await asyncio.wait_for(first_selected.wait(), timeout=10)
        second_result = await asyncio.wait_for(
            WorkerControlService(second_factory).expire_presence(now=now, limit=1),
            timeout=10,
        )
        release_first.set()
        first_result = await asyncio.wait_for(first_task, timeout=10)
    finally:
        release_first.set()
        if not first_task.done():
            await asyncio.gather(first_task, return_exceptions=True)
        await second_engine.dispose()

    assert selections == [(first_id,), (second_id,)]
    assert first_result == second_result == 1
    for worker_id in (first_id, second_id):
        worker, events = await _worker_and_events(unit_of_work_factory, worker_id)
        assert worker.status is WorkerStatus.OFFLINE
        assert sum(event.event_type == "worker.presence.expired" for event in events) == 1


async def test_heartbeat_committed_first_prevents_stale_expiry_audit(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    worker_id = await _add_worker(
        unit_of_work_factory,
        now=now,
        status=WorkerStatus.ONLINE,
        presence_expires_at=now,
    )
    selection_started = asyncio.Event()
    continue_selection = asyncio.Event()
    original_select = SQLAlchemyWorkerRepository.list_expired_presence_for_update

    async def wait_before_select(
        repository: SQLAlchemyWorkerRepository, selected_at: datetime, limit: int
    ) -> list[WorkerNode]:
        selection_started.set()
        await continue_selection.wait()
        return await original_select(repository, selected_at, limit)

    monkeypatch.setattr(
        SQLAlchemyWorkerRepository, "list_expired_presence_for_update", wait_before_select
    )
    expiry_task = asyncio.create_task(
        WorkerControlService(unit_of_work_factory).expire_presence(now=now, limit=1)
    )
    try:
        await asyncio.wait_for(selection_started.wait(), timeout=10)
        heartbeat = await WorkerControlService(
            unit_of_work_factory, clock=FixedClock(now)
        ).heartbeat(worker_id)
        continue_selection.set()
        expired = await asyncio.wait_for(expiry_task, timeout=10)
    finally:
        continue_selection.set()
        if not expiry_task.done():
            await asyncio.gather(expiry_task, return_exceptions=True)

    worker, events = await _worker_and_events(unit_of_work_factory, worker_id)
    assert expired == 0
    assert heartbeat.status is WorkerStatus.ONLINE
    assert worker.status is WorkerStatus.ONLINE
    assert worker.presence_expires_at is not None and worker.presence_expires_at > now
    assert not any(event.event_type == "worker.presence.expired" for event in events)


async def test_expiry_committed_first_then_heartbeat_restores_presence(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    worker_id = await _add_worker(
        unit_of_work_factory,
        now=now,
        status=WorkerStatus.DEGRADED,
        presence_expires_at=now,
    )
    selection_locked = asyncio.Event()
    release_selection = asyncio.Event()
    heartbeat_waiting = asyncio.Event()
    original_select = SQLAlchemyWorkerRepository.list_expired_presence_for_update
    original_get_for_update = SQLAlchemyWorkerRepository.get_for_update

    async def hold_locked_selection(
        repository: SQLAlchemyWorkerRepository, selected_at: datetime, limit: int
    ) -> list[WorkerNode]:
        selected = await original_select(repository, selected_at, limit)
        if selected:
            selection_locked.set()
            await release_selection.wait()
        return selected

    async def observe_heartbeat_lock_attempt(
        repository: SQLAlchemyWorkerRepository, selected_worker_id: UUID
    ) -> WorkerNode | None:
        if selected_worker_id == worker_id:
            heartbeat_waiting.set()
        return await original_get_for_update(repository, selected_worker_id)

    monkeypatch.setattr(
        SQLAlchemyWorkerRepository,
        "list_expired_presence_for_update",
        hold_locked_selection,
    )
    monkeypatch.setattr(
        SQLAlchemyWorkerRepository, "get_for_update", observe_heartbeat_lock_attempt
    )
    expiry_task = asyncio.create_task(
        WorkerControlService(unit_of_work_factory).expire_presence(now=now, limit=1)
    )
    heartbeat_task: asyncio.Task[WorkerPresence] | None = None
    try:
        await asyncio.wait_for(selection_locked.wait(), timeout=10)
        heartbeat_task = asyncio.create_task(
            WorkerControlService(
                unit_of_work_factory, clock=FixedClock(now + timedelta(seconds=1))
            ).heartbeat(worker_id)
        )
        await asyncio.wait_for(heartbeat_waiting.wait(), timeout=10)
        release_selection.set()
        expired = await asyncio.wait_for(expiry_task, timeout=10)
        heartbeat = await asyncio.wait_for(heartbeat_task, timeout=10)
    finally:
        release_selection.set()
        if not expiry_task.done():
            await asyncio.gather(expiry_task, return_exceptions=True)
        if heartbeat_task is not None and not heartbeat_task.done():
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    worker, events = await _worker_and_events(unit_of_work_factory, worker_id)
    assert expired == 1
    assert heartbeat.status is WorkerStatus.ONLINE
    assert worker.status is WorkerStatus.ONLINE
    assert worker.presence_expires_at is not None and worker.presence_expires_at > now
    assert sum(event.event_type == "worker.presence.expired" for event in events) == 1


async def test_scheduler_reconstruction_rediscovers_presence_from_postgres(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    for seconds in (2, 1):
        await _add_worker(
            unit_of_work_factory,
            now=now,
            status=WorkerStatus.ONLINE,
            presence_expires_at=now - timedelta(seconds=seconds),
        )

    async def fresh_tick() -> SchedulerTickResult:
        clock: Clock = FixedClock(now)
        worker_jobs = WorkerJobService(unit_of_work_factory, clock=clock)
        runtime = compose_command_runtime(
            unit_of_work_factory,
            worker_jobs,
            threads_api_gateway=None,
            threads_access_token_provider=None,
            clock=clock,
        ).command_runtime
        return await run_scheduler_tick(
            unit_of_work_factory,
            runtime,
            worker_jobs,
            worker_control_service=WorkerControlService(unit_of_work_factory, clock=clock),
            now=now,
            presence_expiry_limit=1,
            generation_limit=1,
            conversation_sync_limit=1,
            activity_limit=1,
            command_limit=1,
            recovery_limit=1,
        )

    first = await fresh_tick()
    second = await fresh_tick()
    assert first.worker_presences_expired == 1
    assert second.worker_presences_expired == 1
