from datetime import datetime
from uuid import UUID

from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.domain.account_activities import (
    AccountActivityPlanStatus,
    ActivityRecurrenceKind,
    ScheduledActivity,
)
from threads_platform.domain.time import normalize_utc

MAX_DUE_ACTIVITY_GENERATION_BATCH = 100


async def generate_due_account_activity_occurrences(
    unit_of_work_factory: UnitOfWorkFactory,
    *,
    now: datetime,
    limit: int,
) -> list[ScheduledActivity]:
    """Generate a bounded, deterministic set of due fixed-interval occurrences."""
    if type(limit) is not int or not 1 <= limit <= MAX_DUE_ACTIVITY_GENERATION_BATCH:
        raise ValueError(
            "activity occurrence generation limit must be between 1 and "
            f"{MAX_DUE_ACTIVITY_GENERATION_BATCH}"
        )
    occurred_at = normalize_utc(now)
    generated: list[ScheduledActivity] = []
    skipped_states: set[tuple[UUID, int]] = set()
    cursor_advancements = 0

    async with unit_of_work_factory() as unit_of_work:
        while cursor_advancements < limit:
            state = await unit_of_work.activity_recurrence_states.get_next_due_for_update(
                occurred_at,
                exclude=frozenset(skipped_states),
            )
            if state is None:
                break

            template = await unit_of_work.activity_templates.get_latest_revision_for_update(
                state.template_id
            )
            if (
                template is None
                or template.revision != state.template_revision
                or template.recurrence_kind is not ActivityRecurrenceKind.FIXED_INTERVAL
                or template.interval_seconds is None
            ):
                skipped_states.add((state.template_id, state.template_revision))
                continue

            plan = await unit_of_work.activity_plans.get_for_update(template.plan_id)
            if plan is None:
                raise RuntimeError(
                    f"recurring activity template references missing plan: {template.plan_id}"
                )
            if plan.status is AccountActivityPlanStatus.DISABLED:
                skipped_states.add((state.template_id, state.template_revision))
                continue

            due_at = state.next_due_at
            existing = await unit_of_work.scheduled_activities.get_by_identity(
                state.template_id,
                state.template_revision,
                due_at,
            )
            if existing is None:
                candidate = ScheduledActivity.from_plan_template(
                    plan,
                    template,
                    due_at,
                    created_at=occurred_at,
                )
                stored = await unit_of_work.scheduled_activities.add_if_absent(candidate)
                generated.append(stored)

            state.advance(template.interval_seconds, at=occurred_at)
            await unit_of_work.activity_recurrence_states.update(state)
            cursor_advancements += 1

    return generated
