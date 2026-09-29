from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from threads_platform.application.account_activity_materialization import (
    materialize_due_account_activities,
)
from threads_platform.domain.account_activities import (
    AccountActivityPlan,
    AccountActivityPlanStatus,
    AccountActivityTemplate,
    ActivityPriority,
    ScheduledActivity,
    ScheduledActivityMaterializationStatus,
)
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import Command
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.repositories import (
    SQLAlchemyScheduledActivityRepository,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


async def _create_activity(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    *,
    now: datetime,
    due_at: datetime,
    priority: ActivityPriority = ActivityPriority.NORMAL,
    activity_type: str = "threads.browser.feed.browse",
    configuration: dict[str, object] | None = None,
    created_at: datetime | None = None,
) -> tuple[AccountActivityPlan, ScheduledActivity]:
    account = ThreadsAccount(
        threads_user_id=f"materialize-{uuid4()}",
        username="materialize_test",
    )
    plan = AccountActivityPlan(account_id=account.id, name="Materialization test plan")
    template = AccountActivityTemplate(
        account_id=account.id,
        plan_id=plan.id,
        name="Materialization test activity",
        activity_type=activity_type,
        configuration=(
            {"max_items": 7}
            if configuration is None and activity_type == "threads.browser.feed.browse"
            else configuration or {}
        ),
        priority=priority,
        change_reason="test template",
    )
    activity = ScheduledActivity.from_plan_template(
        plan,
        template,
        due_at,
        creation_reason="test occurrence",
        created_at=created_at or now - timedelta(minutes=1),
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.activity_plans.add(plan)
        await unit_of_work.activity_templates.add_revision(template)
        await unit_of_work.scheduled_activities.add_if_absent(activity)
    return plan, activity


async def _transition_plan(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    plan_id: UUID,
    target: AccountActivityPlanStatus,
    *,
    at: datetime,
) -> None:
    async with unit_of_work_factory() as unit_of_work:
        plan = await unit_of_work.activity_plans.get_for_update(plan_id)
        assert plan is not None
        plan.transition(target, at, reason=f"test {target.value.lower()}")
        await unit_of_work.activity_plans.update(plan)


async def test_two_materializers_racing_one_occurrence_create_one_deterministic_command(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    _, activity = await _create_activity(
        unit_of_work_factory, now=now, due_at=now - timedelta(seconds=1)
    )
    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    independent_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
    first_selected = asyncio.Event()
    release_first = asyncio.Event()
    original_select = SQLAlchemyScheduledActivityRepository.list_due_pending_for_materialization

    async def hold_first_transaction(
        repository: SQLAlchemyScheduledActivityRepository,
        selected_at: datetime,
        limit: int,
    ) -> list[ScheduledActivity]:
        selected = await original_select(repository, selected_at, limit)
        if selected and not first_selected.is_set():
            first_selected.set()
            await release_first.wait()
        return selected

    monkeypatch.setattr(
        SQLAlchemyScheduledActivityRepository,
        "list_due_pending_for_materialization",
        hold_first_transaction,
    )

    async def race() -> list[Command]:
        expected_identity = f"activity:{activity.id}"
        commands = await materialize_due_account_activities(independent_factory, now=now, limit=10)
        assert all(command.command_id == expected_identity for command in commands)
        return commands

    try:
        first = asyncio.create_task(race())
        await asyncio.wait_for(first_selected.wait(), timeout=10)
        second = asyncio.create_task(race())
        try:
            second_result = await asyncio.wait_for(second, timeout=10)
        finally:
            release_first.set()
        first_result = await first
        assert first_result and second_result == []
        async with independent_factory() as unit_of_work:
            stored = await unit_of_work.scheduled_activities.get(activity.id)
            command = await unit_of_work.commands.get_by_command_id(f"activity:{activity.id}")
        assert stored is not None
        assert stored.materialization_status is ScheduledActivityMaterializationStatus.MATERIALIZED
        assert stored.command_id == f"activity:{activity.id}"
        assert command is not None
        assert command.command_id == stored.command_id
        assert command.correlation_id == f"activity-correlation:{activity.id}"
    finally:
        await engine.dispose()


async def test_distinct_occurrences_materialize_in_priority_and_due_order_without_duplicate_ids(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    high = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now - timedelta(hours=1),
        priority=ActivityPriority.HIGH,
        created_at=now - timedelta(days=1),
    )
    normal_earlier = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now - timedelta(hours=3),
        priority=ActivityPriority.NORMAL,
        created_at=now - timedelta(hours=2),
    )
    normal_later = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now - timedelta(hours=3),
        priority=ActivityPriority.NORMAL,
        created_at=now - timedelta(hours=1),
    )
    same_due_and_creation = [
        await _create_activity(
            unit_of_work_factory,
            now=now,
            due_at=now - timedelta(hours=2),
            priority=ActivityPriority.NORMAL,
            created_at=now - timedelta(minutes=30),
        )
        for _ in range(2)
    ]
    low = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now - timedelta(hours=4),
        priority=ActivityPriority.LOW,
        created_at=now - timedelta(days=2),
    )

    commands = await materialize_due_account_activities(unit_of_work_factory, now=now, limit=10)

    ordered = [
        high[1],
        normal_earlier[1],
        normal_later[1],
        *(
            activity
            for _, activity in sorted(same_due_and_creation, key=lambda pair: pair[1].id.int)
        ),
        low[1],
    ]
    expected_ids = [f"activity:{activity.id}" for activity in ordered]
    assert [command.command_id for command in commands] == expected_ids
    assert len({command.command_id for command in commands}) == len(commands) == 6


async def test_transaction_rollback_leaves_neither_command_nor_half_materialized_activity(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    _, activity = await _create_activity(
        unit_of_work_factory, now=now, due_at=now - timedelta(seconds=1)
    )

    async def crash_after_command_insert(
        _repository: SQLAlchemyScheduledActivityRepository,
        _activity: ScheduledActivity,
    ) -> None:
        raise RuntimeError("simulated process crash before occurrence transition")

    monkeypatch.setattr(
        SQLAlchemyScheduledActivityRepository,
        "update_materialization",
        crash_after_command_insert,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        await materialize_due_account_activities(unit_of_work_factory, now=now, limit=10)

    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.scheduled_activities.get(activity.id)
        command = await unit_of_work.commands.get_by_command_id(f"activity:{activity.id}")
    assert stored is not None
    assert stored.materialization_status is ScheduledActivityMaterializationStatus.PENDING
    assert stored.command_id is None
    assert command is None


async def test_retry_observes_same_materialized_command_without_inserting_another(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    _, activity = await _create_activity(
        unit_of_work_factory, now=now, due_at=now - timedelta(seconds=1)
    )
    first_attempt = await materialize_due_account_activities(
        unit_of_work_factory, now=now, limit=10
    )
    retry = await materialize_due_account_activities(unit_of_work_factory, now=now, limit=10)

    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.scheduled_activities.get(activity.id)
        command = await unit_of_work.commands.get_by_command_id(f"activity:{activity.id}")
    assert len(first_attempt) == 1
    assert retry == []
    assert stored is not None and stored.command_id == first_attempt[0].command_id
    assert command is not None and command.id == first_attempt[0].id
    assert command.command_id == first_attempt[0].command_id


async def test_future_occurrence_is_not_materialized(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    _, activity = await _create_activity(
        unit_of_work_factory, now=now, due_at=now + timedelta(minutes=1)
    )

    assert await materialize_due_account_activities(unit_of_work_factory, now=now, limit=10) == []

    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.scheduled_activities.get(activity.id)
        command = await unit_of_work.commands.get_by_command_id(f"activity:{activity.id}")
    assert stored is not None
    assert stored.materialization_status is ScheduledActivityMaterializationStatus.PENDING
    assert command is None


async def test_paused_plan_keeps_due_occurrence_pending(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    plan, activity = await _create_activity(
        unit_of_work_factory, now=now, due_at=now - timedelta(seconds=1)
    )
    await _transition_plan(
        unit_of_work_factory,
        plan.id,
        AccountActivityPlanStatus.PAUSED,
        at=now + timedelta(seconds=1),
    )

    assert (
        await materialize_due_account_activities(
            unit_of_work_factory, now=now + timedelta(seconds=2), limit=10
        )
        == []
    )

    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.scheduled_activities.get(activity.id)
        command = await unit_of_work.commands.get_by_command_id(f"activity:{activity.id}")
    assert stored is not None
    assert stored.materialization_status is ScheduledActivityMaterializationStatus.PENDING
    assert stored.command_id is None
    assert command is None

    await _transition_plan(
        unit_of_work_factory,
        plan.id,
        AccountActivityPlanStatus.ACTIVE,
        at=now + timedelta(seconds=3),
    )
    resumed = await materialize_due_account_activities(
        unit_of_work_factory, now=now + timedelta(seconds=4), limit=10
    )
    assert [item.command_id for item in resumed] == [f"activity:{activity.id}"]


async def test_disabled_plan_is_terminally_non_materializable_and_not_reselected(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    plan, activity = await _create_activity(
        unit_of_work_factory, now=now, due_at=now - timedelta(seconds=1)
    )
    await _transition_plan(
        unit_of_work_factory,
        plan.id,
        AccountActivityPlanStatus.DISABLED,
        at=now + timedelta(seconds=1),
    )
    materializer_now = now + timedelta(seconds=2)

    assert (
        await materialize_due_account_activities(
            unit_of_work_factory, now=materializer_now, limit=10
        )
        == []
    )
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.scheduled_activities.get(activity.id)
        command = await unit_of_work.commands.get_by_command_id(f"activity:{activity.id}")
    assert stored is not None
    assert (
        stored.materialization_status is ScheduledActivityMaterializationStatus.NON_MATERIALIZABLE
    )
    assert stored.materialization_reason == "PLAN_DISABLED"
    assert stored.materialization_at == materializer_now
    assert command is None
    assert (
        await materialize_due_account_activities(
            unit_of_work_factory, now=materializer_now, limit=10
        )
        == []
    )


@pytest.mark.parametrize(
    ("activity_type", "configuration", "reason"),
    [
        ("example.unsupported", None, "UNSUPPORTED_ACTIVITY_TYPE"),
        ("threads.browser.media.local_upload", None, "UNSUPPORTED_ACTIVITY_TYPE"),
        (
            "threads.browser.feed.browse",
            {"unapproved": True},
            "INVALID_ACTIVITY_CONFIGURATION",
        ),
    ],
)
async def test_unsupported_activity_types_fail_closed_with_auditable_reason(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    activity_type: str,
    configuration: dict[str, object] | None,
    reason: str,
) -> None:
    now = datetime.now(UTC)
    _, activity = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now - timedelta(seconds=1),
        activity_type=activity_type,
        configuration=configuration,
    )

    assert await materialize_due_account_activities(unit_of_work_factory, now=now, limit=10) == []

    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.scheduled_activities.get(activity.id)
        command = await unit_of_work.commands.get_by_command_id(f"activity:{activity.id}")
    assert stored is not None
    assert (
        stored.materialization_status is ScheduledActivityMaterializationStatus.NON_MATERIALIZABLE
    )
    assert stored.materialization_reason == reason
    assert command is None


@pytest.mark.parametrize(
    ("priority", "command_priority"),
    [
        (ActivityPriority.LOW, -100),
        (ActivityPriority.NORMAL, 0),
        (ActivityPriority.HIGH, 100),
    ],
)
async def test_activity_priority_maps_and_round_trips_through_command_persistence(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    priority: ActivityPriority,
    command_priority: int,
) -> None:
    now = datetime.now(UTC)
    _, activity = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now - timedelta(seconds=1),
        priority=priority,
    )

    materialized = await materialize_due_account_activities(unit_of_work_factory, now=now, limit=10)

    assert len(materialized) == 1
    assert materialized[0].priority == command_priority
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.commands.get_by_command_id(f"activity:{activity.id}")
    assert stored is not None
    assert stored.priority == command_priority


async def test_command_priority_and_materialization_history_protect_downgrade(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    _, activity = await _create_activity(
        unit_of_work_factory,
        now=now,
        due_at=now - timedelta(seconds=1),
        priority=ActivityPriority.NORMAL,
    )
    await materialize_due_account_activities(unit_of_work_factory, now=now, limit=10)
    config = Config("alembic.ini")

    with pytest.raises(RuntimeError, match="ACTIVITY_MATERIALIZATION_DOWNGRADE_BLOCKED"):
        await asyncio.to_thread(command.downgrade, config, "20260929_0011")
    async with unit_of_work_factory() as unit_of_work:
        stored = await unit_of_work.scheduled_activities.get(activity.id)
        existing = await unit_of_work.commands.get_by_command_id(f"activity:{activity.id}")
    assert stored is not None
    assert stored.materialization_status is ScheduledActivityMaterializationStatus.MATERIALIZED
    assert existing is not None and existing.priority == 0

    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE scheduled_activities SET materialization_status = 'PENDING', "
                    "command_id = NULL, materialization_at = NULL, materialization_reason = NULL "
                    "WHERE id = :activity_id"
                ),
                {"activity_id": activity.id},
            )
        high_priority_command = Command(
            command_id=f"high-priority-{uuid4()}",
            correlation_id=f"high-priority-correlation-{uuid4()}",
            account_id=activity.account_id,
            command_type="threads.browser.feed.browse",
            payload={"max_items": 4},
            priority=100,
            created_at=now,
            received_at=now,
        )
        async with unit_of_work_factory() as unit_of_work:
            await unit_of_work.commands.add(high_priority_command)
        with pytest.raises(RuntimeError, match="ACTIVITY_MATERIALIZATION_DOWNGRADE_BLOCKED"):
            await asyncio.to_thread(command.downgrade, config, "20260929_0011")
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")


async def test_upgrade_from_pre_0012_preserves_commands_and_defaults_priority_to_normal(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    _, activity = await _create_activity(
        unit_of_work_factory, now=now, due_at=now + timedelta(hours=1)
    )
    existing_command = Command(
        command_id=f"pre-0012-{uuid4()}",
        correlation_id=f"pre-0012-correlation-{uuid4()}",
        account_id=activity.account_id,
        command_type="threads.browser.feed.browse",
        payload={"max_items": 3},
        created_at=now,
        received_at=now,
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.commands.add(existing_command)
    config = Config("alembic.ini")
    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    try:
        await asyncio.to_thread(command.downgrade, config, "20260929_0011")
        async with engine.connect() as connection:
            command_count = await connection.scalar(
                text("SELECT count(*) FROM commands WHERE command_id = :command_id"),
                {"command_id": existing_command.command_id},
            )
            activity_count = await connection.scalar(
                text("SELECT count(*) FROM scheduled_activities WHERE id = :activity_id"),
                {"activity_id": activity.id},
            )
        assert command_count == 1
        assert activity_count == 1
        await asyncio.to_thread(command.upgrade, config, "head")

        async with unit_of_work_factory() as unit_of_work:
            stored_command = await unit_of_work.commands.get_by_command_id(
                existing_command.command_id
            )
            stored_activity = await unit_of_work.scheduled_activities.get(activity.id)
        assert stored_command is not None and stored_command.priority == 0
        assert stored_activity is not None
        assert (
            stored_activity.materialization_status is ScheduledActivityMaterializationStatus.PENDING
        )
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
        await engine.dispose()
