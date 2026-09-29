from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from threads_platform.domain.account_activities import (
    AccountActivityPlan,
    AccountActivityPlanStatus,
    AccountActivityTemplate,
    ActivityPriority,
    ScheduledActivity,
)
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration


async def _create_activity(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    *,
    due_at: datetime | None = None,
) -> tuple[AccountActivityPlan, AccountActivityTemplate, ScheduledActivity]:
    account = ThreadsAccount(threads_user_id=f"activity-{uuid4()}", username="activity_test")
    plan = AccountActivityPlan(account_id=account.id, name="Daily account activity")
    template = AccountActivityTemplate(
        account_id=account.id,
        plan_id=plan.id,
        name="Browse feed",
        activity_type="threads.browser.feed.browse",
        configuration={"max_items": 4, "include_replies": False},
        priority=ActivityPriority.NORMAL,
        change_reason="initial configuration",
    )
    activity = ScheduledActivity.from_plan_template(
        plan,
        template,
        due_at or datetime(2026, 10, 1, 9, tzinfo=UTC),
        creation_reason="test occurrence",
    )
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.activity_plans.add(plan)
        await unit_of_work.activity_templates.add_revision(template)
        await unit_of_work.scheduled_activities.add_if_absent(activity)
    return plan, template, activity


async def test_activity_plan_template_and_occurrence_persist_with_snapshots(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    plan, template, activity = await _create_activity(unit_of_work_factory)

    async with unit_of_work_factory() as unit_of_work:
        stored_plan = await unit_of_work.activity_plans.get(plan.id)
        stored_template = await unit_of_work.activity_templates.get_revision(template.id, 1)
        stored_activity = await unit_of_work.scheduled_activities.get(activity.id)

    assert stored_plan is not None
    assert stored_plan.status is AccountActivityPlanStatus.ACTIVE
    assert stored_plan.revision == 1
    assert stored_template is not None
    assert stored_template.configuration == template.configuration
    assert stored_activity is not None
    assert stored_activity.identity == activity.identity
    assert stored_activity.plan_revision == 1
    assert stored_activity.template_revision == 1
    assert stored_activity.configuration_snapshot == template.configuration
    assert stored_activity.priority is ActivityPriority.NORMAL

    async with unit_of_work_factory() as unit_of_work:
        current_plan = await unit_of_work.activity_plans.get_for_update(plan.id)
        assert current_plan is not None
        current_plan.transition(
            AccountActivityPlanStatus.PAUSED,
            datetime(2026, 9, 29, 13, tzinfo=UTC),
            reason="operator pause",
        )
        await unit_of_work.activity_plans.update(current_plan)
        current_plan.transition(
            AccountActivityPlanStatus.DISABLED,
            datetime(2026, 9, 29, 14, tzinfo=UTC),
            reason="plan retired",
        )
        await unit_of_work.activity_plans.update(current_plan)

    async with unit_of_work_factory() as unit_of_work:
        disabled_plan = await unit_of_work.activity_plans.get(plan.id)
        retained_activity = await unit_of_work.scheduled_activities.get(activity.id)
    assert disabled_plan is not None
    assert disabled_plan.status is AccountActivityPlanStatus.DISABLED
    assert disabled_plan.revision == 3
    assert retained_activity is not None
    assert retained_activity.plan_status_snapshot is AccountActivityPlanStatus.ACTIVE
    assert retained_activity.plan_revision == 1


async def test_activity_template_revisions_are_append_only_and_occurrence_dedupes(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    plan, template, first_occurrence = await _create_activity(unit_of_work_factory)
    second_template = AccountActivityTemplate(
        id=template.id,
        revision=2,
        account_id=plan.account_id,
        plan_id=plan.id,
        name="Browse fewer items",
        activity_type=template.activity_type,
        configuration={"max_items": 2, "include_replies": False},
        priority=ActivityPriority.LOW,
        change_reason="reduce daily volume",
    )
    due_at = first_occurrence.due_at + timedelta(days=1)
    second_occurrence = ScheduledActivity.from_plan_template(plan, second_template, due_at)

    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.activity_templates.add_revision(second_template)
        await unit_of_work.scheduled_activities.add_if_absent(second_occurrence)
        first_version = await unit_of_work.activity_templates.get_revision(template.id, 1)
        second_version = await unit_of_work.activity_templates.get_revision(template.id, 2)

    assert first_version is not None
    assert first_version.configuration["max_items"] == 4
    assert first_version.priority is ActivityPriority.NORMAL
    assert second_version is not None
    assert second_version.configuration["max_items"] == 2
    assert second_version.priority is ActivityPriority.LOW

    async with unit_of_work_factory() as unit_of_work:
        duplicate_request = ScheduledActivity.from_plan_template(
            plan, template, first_occurrence.due_at, creation_reason="restarted tick"
        )
        duplicate = await unit_of_work.scheduled_activities.add_if_absent(duplicate_request)
        same_id_retry = await unit_of_work.scheduled_activities.add_if_absent(first_occurrence)
        listed = await unit_of_work.scheduled_activities.list_for_account(plan.account_id)
    assert duplicate.id == first_occurrence.id
    assert same_id_retry.id == first_occurrence.id
    assert len(listed) == 2


async def test_scheduled_activity_composite_foreign_keys_reject_cross_account_links(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    plan, template, activity = await _create_activity(unit_of_work_factory)
    other_account = ThreadsAccount(
        threads_user_id=f"other-activity-{uuid4()}", username="other_activity_test"
    )

    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(other_account)

    invalid_activity = ScheduledActivity(
        id=uuid4(),
        account_id=other_account.id,
        plan_id=plan.id,
        plan_revision=activity.plan_revision,
        plan_name_snapshot=activity.plan_name_snapshot,
        plan_status_snapshot=activity.plan_status_snapshot,
        plan_status_reason_snapshot=activity.plan_status_reason_snapshot,
        template_id=template.id,
        template_revision=template.revision,
        template_name_snapshot=activity.template_name_snapshot,
        activity_type_snapshot=activity.activity_type_snapshot,
        configuration_snapshot=activity.configuration_snapshot,
        priority=activity.priority,
        due_at=activity.due_at + timedelta(days=1),
        created_at=activity.created_at,
    )
    with pytest.raises(IntegrityError):
        async with unit_of_work_factory() as unit_of_work:
            await unit_of_work.scheduled_activities.add_if_absent(invalid_activity)


async def test_duplicate_occurrence_creation_is_safe_across_concurrent_transactions(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    plan, template, activity = await _create_activity(unit_of_work_factory)
    duplicate_request = ScheduledActivity.from_plan_template(
        plan, template, activity.due_at, creation_reason="concurrent restarted tick"
    )
    engine = create_database_engine(os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"])
    independent_factory = SQLAlchemyUnitOfWorkFactory(create_session_factory(engine))
    barrier = asyncio.Barrier(2)

    async def create_in_own_transaction(candidate: ScheduledActivity) -> ScheduledActivity:
        async with independent_factory() as unit_of_work:
            await barrier.wait()
            return await unit_of_work.scheduled_activities.add_if_absent(candidate)

    try:
        stored = await asyncio.gather(
            create_in_own_transaction(activity), create_in_own_transaction(duplicate_request)
        )
        assert stored[0].id == stored[1].id
        assert stored[0].id in {activity.id, duplicate_request.id}
        async with independent_factory() as unit_of_work:
            listed = await unit_of_work.scheduled_activities.list_for_account(activity.account_id)
        assert len(listed) == 1
    finally:
        await engine.dispose()


async def test_activity_migration_from_current_schema_and_data_aware_downgrade(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> None:
    config = Config("alembic.ini")
    database_url = os.environ["THREADS_PLATFORM_TEST_DATABASE_URL"]
    engine = create_database_engine(database_url)
    table_names = (
        "account_activity_plans",
        "account_activity_templates",
        "account_activity_template_revisions",
        "scheduled_activities",
    )
    try:
        await asyncio.to_thread(command.downgrade, config, "20260928_0010")
        async with engine.connect() as connection:
            for table_name in table_names:
                assert (
                    await connection.scalar(
                        text("SELECT to_regclass(:table_name) IS NULL"),
                        {"table_name": f"public.{table_name}"},
                    )
                    is True
                )

        await asyncio.to_thread(command.upgrade, config, "head")
        account = ThreadsAccount(threads_user_id=f"migration-{uuid4()}", username="migration")
        plan = AccountActivityPlan(account_id=account.id, name="Migration plan")
        template = AccountActivityTemplate(
            account_id=account.id,
            plan_id=plan.id,
            name="Migration template",
            activity_type="example.activity",
            configuration={"limit": 1},
            priority=ActivityPriority.NORMAL,
            change_reason="migration fixture",
        )
        activity = ScheduledActivity.from_plan_template(
            plan, template, datetime(2026, 10, 2, 9, tzinfo=UTC)
        )
        async with unit_of_work_factory() as unit_of_work:
            await unit_of_work.accounts.add(account)
            await unit_of_work.activity_plans.add(plan)
            await unit_of_work.activity_templates.add_revision(template)
            await unit_of_work.scheduled_activities.add_if_absent(activity)

        with pytest.raises(RuntimeError, match="ACCOUNT_ACTIVITY_DOWNGRADE_BLOCKED"):
            await asyncio.to_thread(command.downgrade, config, "20260928_0010")
        async with unit_of_work_factory() as unit_of_work:
            assert await unit_of_work.scheduled_activities.get(activity.id) is not None

        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM scheduled_activities"))
            await connection.execute(text("DELETE FROM account_activity_template_revisions"))
            await connection.execute(text("DELETE FROM account_activity_templates"))
            await connection.execute(text("DELETE FROM account_activity_plans"))

        await asyncio.to_thread(command.downgrade, config, "20260928_0010")
        async with engine.connect() as connection:
            for table_name in table_names:
                assert (
                    await connection.scalar(
                        text("SELECT to_regclass(:table_name) IS NULL"),
                        {"table_name": f"public.{table_name}"},
                    )
                    is True
                )
        await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as connection:
            for table_name in table_names:
                assert (
                    await connection.scalar(
                        text("SELECT to_regclass(:table_name) IS NOT NULL"),
                        {"table_name": f"public.{table_name}"},
                    )
                    is True
                )
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
        await engine.dispose()
