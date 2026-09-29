from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from threads_platform.domain.account_activities import (
    MAX_ACTIVITY_CONFIGURATION_BYTES,
    MAX_ACTIVITY_RECURRENCE_INTERVAL_SECONDS,
    MIN_ACTIVITY_RECURRENCE_INTERVAL_SECONDS,
    AccountActivityPlan,
    AccountActivityPlanStatus,
    AccountActivityRecurrenceState,
    AccountActivityTemplate,
    ActivityPriority,
    ActivityRecurrenceKind,
    ScheduledActivity,
    ScheduledActivityMaterializationStatus,
)
from threads_platform.domain.worker_jobs import WorkerJob


def _plan(account_id: UUID | None = None) -> AccountActivityPlan:
    return AccountActivityPlan(account_id=account_id or uuid4(), name="Daily plan")


def _template(
    plan: AccountActivityPlan,
    *,
    template_id: UUID | None = None,
    revision: int = 1,
    configuration: dict[str, object] | None = None,
    priority: ActivityPriority = ActivityPriority.NORMAL,
) -> AccountActivityTemplate:
    return AccountActivityTemplate(
        id=template_id or uuid4(),
        account_id=plan.account_id,
        plan_id=plan.id,
        revision=revision,
        name="Browse feed",
        activity_type="threads.browser.feed.browse",
        configuration=(
            configuration
            if configuration is not None
            else {"max_items": 5, "include_replies": False}
        ),
        priority=priority,
        change_reason="initial configuration" if revision == 1 else "configuration revision",
    )


def test_activity_plan_validates_state_transitions_and_revisions() -> None:
    plan = _plan()
    start = datetime(2026, 9, 29, 12, tzinfo=UTC)

    plan.transition(AccountActivityPlanStatus.PAUSED, start, reason="operator pause")
    assert plan.status is AccountActivityPlanStatus.PAUSED
    assert plan.revision == 2
    assert plan.status_reason == "operator pause"

    plan.transition(
        AccountActivityPlanStatus.ACTIVE,
        start + timedelta(minutes=1),
        reason="operator resume",
    )
    plan.transition(
        AccountActivityPlanStatus.DISABLED,
        start + timedelta(minutes=2),
        reason="plan retired",
    )
    assert plan.status is AccountActivityPlanStatus.DISABLED
    assert plan.revision == 4

    with pytest.raises(ValueError, match="invalid activity plan transition"):
        plan.transition(
            AccountActivityPlanStatus.ACTIVE,
            start + timedelta(minutes=3),
            reason="cannot reactivate a disabled plan",
        )


def test_activity_plan_rejects_invalid_transition_reason_and_time() -> None:
    start = datetime(2026, 9, 29, 12, tzinfo=UTC)
    plan = AccountActivityPlan(
        account_id=uuid4(),
        name="Daily plan",
        created_at=start,
        updated_at=start,
    )

    with pytest.raises(ValueError, match="invalid activity plan transition"):
        plan.transition(AccountActivityPlanStatus.ACTIVE, start, reason="no change")
    with pytest.raises(ValueError, match="plan status reason"):
        plan.transition(AccountActivityPlanStatus.PAUSED, start, reason=" ")
    with pytest.raises(ValueError, match="move backwards"):
        plan.transition(
            AccountActivityPlanStatus.PAUSED,
            start - timedelta(seconds=1),
            reason="bad timestamp",
        )
    with pytest.raises(ValueError, match="positive"):
        AccountActivityPlan(account_id=uuid4(), name="Plan", revision=0)


def test_activity_template_versions_freeze_and_validate_configuration() -> None:
    plan = _plan()
    template_id = uuid4()
    source_configuration: dict[str, object] = {"options": {"tags": ["one"]}}
    first = _template(plan, template_id=template_id, configuration=source_configuration)
    second = _template(
        plan,
        template_id=template_id,
        revision=2,
        configuration={"options": {"tags": ["two"]}},
    )

    source_configuration["options"] = {"tags": ["changed after construction"]}
    assert first.id == second.id
    assert first.revision == 1
    assert second.revision == 2
    first_options = first.configuration["options"]
    assert first_options == {"tags": ("one",)}
    with pytest.raises(TypeError):
        first.configuration["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        first_options["tags"] = ("changed",)  # type: ignore[index]


def test_fixed_interval_template_accepts_exact_bounds_and_normalizes_anchor() -> None:
    plan = _plan()
    anchor = datetime(2026, 9, 29, 12, tzinfo=UTC)
    template = AccountActivityTemplate(
        account_id=plan.account_id,
        plan_id=plan.id,
        name="Browse feed",
        activity_type="threads.browser.feed.browse",
        configuration={"max_items": 2},
        priority=ActivityPriority.NORMAL,
        change_reason="fixed interval policy",
        created_at=anchor,
        recurrence_kind=ActivityRecurrenceKind.FIXED_INTERVAL,
        anchor_at=anchor,
        interval_seconds=MIN_ACTIVITY_RECURRENCE_INTERVAL_SECONDS,
    )

    assert template.recurrence_kind is ActivityRecurrenceKind.FIXED_INTERVAL
    assert template.anchor_at == anchor
    assert template.interval_seconds == 900

    maximum = AccountActivityTemplate(
        account_id=plan.account_id,
        plan_id=plan.id,
        name="Open profile",
        activity_type="threads.browser.profile.open",
        configuration={"username": "reader"},
        priority=ActivityPriority.NORMAL,
        change_reason="maximum interval policy",
        created_at=anchor,
        recurrence_kind=ActivityRecurrenceKind.FIXED_INTERVAL,
        anchor_at=anchor,
        interval_seconds=MAX_ACTIVITY_RECURRENCE_INTERVAL_SECONDS,
    )
    assert maximum.interval_seconds == 2_592_000


@pytest.mark.parametrize(
    ("activity_type", "interval_seconds", "anchor_delta", "kind", "anchor_set"),
    [
        ("threads.browser.feed.browse", 899, 0, "FIXED_INTERVAL", True),
        ("threads.browser.feed.browse", 2_592_001, 0, "FIXED_INTERVAL", True),
        ("threads.browser.media.local_upload", 900, 0, "FIXED_INTERVAL", True),
        ("threads.publish_text", 900, 0, "FIXED_INTERVAL", True),
        ("unknown.activity", 900, 0, "FIXED_INTERVAL", True),
        ("threads.browser.feed.browse", 900, -1, "FIXED_INTERVAL", True),
        ("threads.browser.feed.browse", None, 0, "FIXED_INTERVAL", True),
        ("threads.browser.feed.browse", 900, 0, "NONE", True),
    ],
)
def test_activity_template_rejects_invalid_recurrence(
    activity_type: str,
    interval_seconds: int | None,
    anchor_delta: int,
    kind: str,
    anchor_set: bool,
) -> None:
    plan = _plan()
    created_at = datetime(2026, 9, 29, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="recurrence|anchor|interval"):
        AccountActivityTemplate(
            account_id=plan.account_id,
            plan_id=plan.id,
            name="Recurring activity",
            activity_type=activity_type,
            configuration={},
            priority=ActivityPriority.NORMAL,
            change_reason="test recurrence",
            created_at=created_at,
            recurrence_kind=ActivityRecurrenceKind(kind),
            anchor_at=(created_at + timedelta(seconds=anchor_delta)) if anchor_set else None,
            interval_seconds=interval_seconds,
        )


def test_recurrence_cursor_uses_exact_interval_arithmetic() -> None:
    start = datetime(2026, 9, 29, 12, tzinfo=UTC)
    state = AccountActivityRecurrenceState(
        template_id=uuid4(),
        template_revision=1,
        next_due_at=start,
        created_at=start,
        updated_at=start,
    )

    first = state.advance(3_600, at=start + timedelta(hours=1, minutes=5))
    second = state.advance(3_600, at=start + timedelta(hours=3, minutes=40))

    assert first == start
    assert second == start + timedelta(hours=1)
    assert state.next_due_at == start + timedelta(hours=2)
    assert state.last_generated_due_at == second
    assert state.generated_count == 2
    assert state.updated_at == start + timedelta(hours=3, minutes=40)


@pytest.mark.parametrize(
    ("priority", "worker_job_value"),
    [
        (ActivityPriority.LOW, -100),
        (ActivityPriority.NORMAL, 0),
        (ActivityPriority.HIGH, 100),
    ],
)
def test_activity_priority_mapping(priority: ActivityPriority, worker_job_value: int) -> None:
    assert priority.worker_job_priority == worker_job_value
    assert ActivityPriority.from_worker_job_priority(worker_job_value) is priority


def test_existing_worker_job_zero_is_normal_priority() -> None:
    job = WorkerJob(capability_name="threads.browser.feed.browse", capability_version=1)

    assert job.priority == 0
    assert ActivityPriority.from_worker_job_priority(job.priority) is ActivityPriority.NORMAL


def test_unknown_worker_job_priority_is_not_assigned_a_semantic_class() -> None:
    with pytest.raises(ValueError, match="unsupported WorkerJob activity priority"):
        ActivityPriority.from_worker_job_priority(10)


def test_scheduled_activity_identity_is_template_revision_and_due_time() -> None:
    plan = _plan()
    template = _template(plan)
    due_at = datetime(2026, 10, 1, 9, tzinfo=UTC)

    first = ScheduledActivity.from_plan_template(plan, template, due_at)
    restarted_tick = ScheduledActivity.from_plan_template(plan, template, due_at)

    assert first.identity == (template.id, template.revision, due_at)
    assert restarted_tick.identity == first.identity
    assert restarted_tick.id != first.id


def test_scheduled_activity_materialization_transitions_are_terminal_and_auditable() -> None:
    plan = _plan()
    template = _template(plan)
    at = datetime(2026, 10, 1, 9, tzinfo=UTC)
    materialized = ScheduledActivity.from_plan_template(plan, template, at)
    materialized = materialized.mark_materialized(f"activity:{materialized.id}", at)

    assert (
        materialized.materialization_status is ScheduledActivityMaterializationStatus.MATERIALIZED
    )
    assert materialized.command_id == f"activity:{materialized.id}"
    assert materialized.materialization_at == at
    assert materialized.materialization_reason is None
    with pytest.raises(ValueError, match="no longer pending"):
        materialized.mark_non_materializable("PLAN_DISABLED", at)

    skipped = ScheduledActivity.from_plan_template(plan, template, at)
    skipped = skipped.mark_non_materializable("UNSUPPORTED_ACTIVITY_TYPE", at)

    assert (
        skipped.materialization_status is ScheduledActivityMaterializationStatus.NON_MATERIALIZABLE
    )
    assert skipped.command_id is None
    assert skipped.materialization_at == at
    assert skipped.materialization_reason == "UNSUPPORTED_ACTIVITY_TYPE"
    with pytest.raises(ValueError, match="no longer pending"):
        skipped.mark_materialized(f"activity:{skipped.id}", at)


def test_occurrence_keeps_immutable_plan_and_template_revision_snapshots() -> None:
    plan = _plan()
    template = _template(
        plan,
        configuration={"max_items": 5, "filters": {"tags": ["science"]}},
    )
    due_at = datetime(2026, 10, 1, 9, tzinfo=UTC)
    activity = ScheduledActivity.from_plan_template(plan, template, due_at)

    plan.transition(
        AccountActivityPlanStatus.PAUSED,
        datetime(2026, 9, 29, 13, tzinfo=UTC),
        reason="operator pause",
    )
    updated_template = _template(
        plan,
        template_id=template.id,
        revision=2,
        configuration={"max_items": 20, "filters": {"tags": ["sports"]}},
    )

    assert activity.plan_revision == 1
    assert activity.plan_status_snapshot is AccountActivityPlanStatus.ACTIVE
    assert activity.template_revision == 1
    assert activity.configuration_snapshot["max_items"] == 5
    assert updated_template.configuration["max_items"] == 20
    with pytest.raises(FrozenInstanceError):
        activity.template_revision = 2  # type: ignore[misc]


@pytest.mark.parametrize("mismatch", ["account", "plan"])
def test_occurrence_rejects_cross_account_or_cross_plan_template(
    mismatch: str,
) -> None:
    plan = _plan()
    if mismatch == "account":
        template = AccountActivityTemplate(
            account_id=uuid4(),
            plan_id=plan.id,
            name="Browse feed",
            activity_type="threads.browser.feed.browse",
            configuration={},
            priority=ActivityPriority.NORMAL,
            change_reason="initial configuration",
        )
    else:
        template = AccountActivityTemplate(
            account_id=plan.account_id,
            plan_id=uuid4(),
            name="Browse feed",
            activity_type="threads.browser.feed.browse",
            configuration={},
            priority=ActivityPriority.NORMAL,
            change_reason="initial configuration",
        )

    with pytest.raises(ValueError, match="belong to the scheduled activity plan account"):
        ScheduledActivity.from_plan_template(plan, template, datetime(2026, 10, 1, 9, tzinfo=UTC))


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ({"nested": {"api_token": "hidden"}}, "secret-bearing fields"),
        ({"endpoint": "https://user@example.test"}, "URLs with credentials"),
        ({"payload": object()}, "non-JSON value"),
        ({"payload": "x" * MAX_ACTIVITY_CONFIGURATION_BYTES}, "exceeds 16 KiB"),
    ],
)
def test_template_rejects_unsafe_or_unbounded_configuration(
    configuration: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AccountActivityTemplate(
            account_id=uuid4(),
            plan_id=uuid4(),
            name="Template",
            activity_type="example.activity",
            configuration=configuration,
            priority=ActivityPriority.NORMAL,
            change_reason="initial configuration",
        )


def test_template_rejects_nonpositive_revision_and_unbounded_reason() -> None:
    with pytest.raises(ValueError, match="revision must be positive"):
        AccountActivityTemplate(
            account_id=uuid4(),
            plan_id=uuid4(),
            name="Template",
            activity_type="example.activity",
            configuration={},
            priority=ActivityPriority.NORMAL,
            change_reason="initial configuration",
            revision=0,
        )
    with pytest.raises(ValueError, match="template change reason"):
        AccountActivityTemplate(
            account_id=uuid4(),
            plan_id=uuid4(),
            name="Template",
            activity_type="example.activity",
            configuration={},
            priority=ActivityPriority.NORMAL,
            change_reason="x" * 241,
        )
