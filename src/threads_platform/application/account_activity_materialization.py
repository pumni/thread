from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ValidationError

from threads_platform.application.crm_protocol_v1 import (
    BrowserFeedBrowsePayload,
    BrowserProfileOpenPayload,
    BrowserThreadOpenPayload,
)
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.domain.account_activities import (
    AccountActivityPlan,
    AccountActivityPlanStatus,
    ScheduledActivity,
    configuration_document,
)
from threads_platform.domain.commands import Command
from threads_platform.domain.time import normalize_utc

MAX_DUE_ACTIVITY_MATERIALIZATION_BATCH = 100

_ACTIVITY_PAYLOADS: dict[str, type[BaseModel]] = {
    "threads.browser.feed.browse": BrowserFeedBrowsePayload,
    "threads.browser.thread.open": BrowserThreadOpenPayload,
    "threads.browser.profile.open": BrowserProfileOpenPayload,
}


async def materialize_due_account_activities(
    unit_of_work_factory: UnitOfWorkFactory,
    *,
    now: datetime,
    limit: int,
) -> list[Command]:
    """Atomically turn due, supported activity occurrences into durable Commands."""
    if type(limit) is not int or not 1 <= limit <= MAX_DUE_ACTIVITY_MATERIALIZATION_BATCH:
        raise ValueError(
            "activity materialization limit must be between 1 and "
            f"{MAX_DUE_ACTIVITY_MATERIALIZATION_BATCH}"
        )
    occurred_at = normalize_utc(now)
    materialized: list[Command] = []

    async with unit_of_work_factory() as unit_of_work:
        activities = await unit_of_work.scheduled_activities.list_due_pending_for_materialization(
            occurred_at, limit
        )
        plans: dict[UUID, AccountActivityPlan] = {}
        for plan_id in sorted(
            {activity.plan_id for activity in activities}, key=lambda value: value.int
        ):
            plan = await unit_of_work.activity_plans.get_for_update(plan_id)
            if plan is None:
                raise RuntimeError(f"scheduled activity references missing plan: {plan_id}")
            plans[plan_id] = plan

        for activity in activities:
            plan = plans[activity.plan_id]
            if plan.status is AccountActivityPlanStatus.PAUSED:
                continue
            if plan.status is AccountActivityPlanStatus.DISABLED:
                activity = activity.mark_non_materializable("PLAN_DISABLED", occurred_at)
                await unit_of_work.scheduled_activities.update_materialization(activity)
                continue

            command, reason = _command_for_activity(activity, occurred_at)
            if command is None:
                assert reason is not None
                activity = activity.mark_non_materializable(reason, occurred_at)
                await unit_of_work.scheduled_activities.update_materialization(activity)
                continue

            inserted = await unit_of_work.commands.add_if_absent(command)
            if not inserted:
                existing = await unit_of_work.commands.get_by_command_id(command.command_id)
                if existing is None or not _matches_materialized_command(existing, command):
                    raise RuntimeError(
                        "scheduled activity command identity conflicts with another command"
                    )
                command = existing

            activity = activity.mark_materialized(command.command_id, occurred_at)
            await unit_of_work.scheduled_activities.update_materialization(activity)
            materialized.append(command)

    return materialized


def _command_for_activity(
    activity: ScheduledActivity, now: datetime
) -> tuple[Command | None, str | None]:
    payload_type = _ACTIVITY_PAYLOADS.get(activity.activity_type_snapshot)
    if payload_type is None:
        return None, "UNSUPPORTED_ACTIVITY_TYPE"

    try:
        payload = payload_type.model_validate(
            configuration_document(activity.configuration_snapshot)
        ).model_dump(mode="json")
    except ValidationError:
        return None, "INVALID_ACTIVITY_CONFIGURATION"

    deterministic_id = f"activity:{activity.id}"
    return (
        Command(
            command_id=deterministic_id,
            correlation_id=f"activity-correlation:{activity.id}",
            account_id=activity.account_id,
            command_type=activity.activity_type_snapshot,
            payload=payload,
            priority=activity.priority.worker_job_priority,
            created_at=now,
            received_at=now,
        ),
        None,
    )


def _matches_materialized_command(existing: Command, expected: Command) -> bool:
    return (
        existing.correlation_id == expected.correlation_id
        and existing.account_id == expected.account_id
        and existing.protocol_version == expected.protocol_version
        and existing.command_type == expected.command_type
        and existing.payload == expected.payload
        and existing.priority == expected.priority
    )
