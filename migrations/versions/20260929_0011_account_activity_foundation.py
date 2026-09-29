"""Add durable account activity plans, template revisions, and occurrences."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0011"
down_revision: str | None = "20260928_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_activity_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status_reason", sa.String(length=240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision = 1 OR status_reason IS NOT NULL",
            name="ck_account_activity_plans_revision_reason",
        ),
        sa.CheckConstraint("revision > 0", name="ck_account_activity_plans_positive_revision"),
        sa.CheckConstraint(
            "status = 'ACTIVE' OR (status_reason IS NOT NULL AND length(status_reason) > 0)",
            name="ck_account_activity_plans_nonactive_reason",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'PAUSED', 'DISABLED')",
            name="ck_account_activity_plans_status",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "id", name="uq_account_activity_plans_account_id"),
    )
    op.create_index(
        "ix_account_activity_plans_account_status",
        "account_activity_plans",
        ["account_id", "status"],
        unique=False,
    )

    op.create_table(
        "account_activity_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "plan_id"],
            ["account_activity_plans.account_id", "account_activity_plans.id"],
            name="fk_account_activity_templates_plan_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "plan_id", "id", name="uq_account_activity_templates_account_plan_id"
        ),
    )
    op.create_index(
        "ix_account_activity_templates_plan",
        "account_activity_templates",
        ["plan_id"],
        unique=False,
    )

    # Domain validation caps compact JSON at 16 KiB; jsonb::text may expand exponent-form
    # numbers into decimal digits, so the database check is a separate 1 MiB storage envelope.
    op.create_table(
        "account_activity_template_revisions",
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("activity_type", sa.String(length=120), nullable=False),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("priority", sa.String(length=40), nullable=False),
        sa.Column("change_reason", sa.String(length=240), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(configuration) = 'object' "
            "AND octet_length(configuration::text) <= 1048576",
            name="ck_account_activity_template_revisions_configuration_bound",
        ),
        sa.CheckConstraint(
            "priority IN ('LOW', 'NORMAL', 'HIGH')",
            name="ck_account_activity_template_revisions_priority",
        ),
        sa.CheckConstraint("revision > 0", name="ck_account_activity_template_revisions_positive"),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["account_activity_templates.id"],
            name="fk_account_activity_template_revisions_template",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("template_id", "revision"),
    )

    # Keep the occurrence snapshot's JSONB storage envelope consistent with template revisions.
    op.create_table(
        "scheduled_activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("plan_name_snapshot", sa.String(length=120), nullable=False),
        sa.Column("plan_status_snapshot", sa.String(length=40), nullable=False),
        sa.Column("plan_status_reason_snapshot", sa.String(length=240), nullable=True),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_revision", sa.Integer(), nullable=False),
        sa.Column("template_name_snapshot", sa.String(length=120), nullable=False),
        sa.Column("activity_type_snapshot", sa.String(length=120), nullable=False),
        sa.Column(
            "configuration_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("priority", sa.String(length=40), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("creation_reason", sa.String(length=240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(configuration_snapshot) = 'object' "
            "AND octet_length(configuration_snapshot::text) <= 1048576",
            name="ck_scheduled_activities_configuration_bound",
        ),
        sa.CheckConstraint(
            "plan_revision > 0", name="ck_scheduled_activities_positive_plan_revision"
        ),
        sa.CheckConstraint(
            "plan_status_snapshot IN ('ACTIVE', 'PAUSED', 'DISABLED')",
            name="ck_scheduled_activities_plan_status_snapshot",
        ),
        sa.CheckConstraint(
            "priority IN ('LOW', 'NORMAL', 'HIGH')",
            name="ck_scheduled_activities_priority",
        ),
        sa.CheckConstraint(
            "template_revision > 0",
            name="ck_scheduled_activities_positive_template_revision",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "plan_id"],
            ["account_activity_plans.account_id", "account_activity_plans.id"],
            name="fk_scheduled_activities_plan_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "plan_id", "template_id"],
            [
                "account_activity_templates.account_id",
                "account_activity_templates.plan_id",
                "account_activity_templates.id",
            ],
            name="fk_scheduled_activities_template_account_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "template_revision"],
            [
                "account_activity_template_revisions.template_id",
                "account_activity_template_revisions.revision",
            ],
            name="fk_scheduled_activities_template_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_id",
            "template_revision",
            "due_at",
            name="uq_scheduled_activities_template_revision_due_at",
        ),
    )
    op.create_index(
        "ix_scheduled_activities_account_due",
        "scheduled_activities",
        ["account_id", "due_at"],
        unique=False,
    )


def downgrade() -> None:
    has_activity_data = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM account_activity_plans) "
            "OR EXISTS (SELECT 1 FROM account_activity_templates) "
            "OR EXISTS (SELECT 1 FROM account_activity_template_revisions) "
            "OR EXISTS (SELECT 1 FROM scheduled_activities)"
        )
    )
    if has_activity_data:
        raise RuntimeError(
            "ACCOUNT_ACTIVITY_DOWNGRADE_BLOCKED: clear durable account activity data "
            "before downgrade"
        )

    op.drop_index("ix_scheduled_activities_account_due", table_name="scheduled_activities")
    op.drop_table("scheduled_activities")
    op.drop_table("account_activity_template_revisions")
    op.drop_index("ix_account_activity_templates_plan", table_name="account_activity_templates")
    op.drop_table("account_activity_templates")
    op.drop_index("ix_account_activity_plans_account_status", table_name="account_activity_plans")
    op.drop_table("account_activity_plans")
