"""Add activity materialization audit and durable Command priority."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0012"
down_revision: str | None = "20260929_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "commands",
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint("ck_commands_priority", "commands", "priority IN (-100, 0, 100)")

    op.add_column(
        "scheduled_activities",
        sa.Column(
            "materialization_status",
            sa.String(length=40),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
    )
    op.add_column(
        "scheduled_activities",
        sa.Column("command_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "scheduled_activities",
        sa.Column("materialization_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "scheduled_activities",
        sa.Column("materialization_reason", sa.String(length=240), nullable=True),
    )
    op.create_check_constraint(
        "ck_scheduled_activities_materialization_status",
        "scheduled_activities",
        "materialization_status IN ('PENDING', 'MATERIALIZED', 'NON_MATERIALIZABLE')",
    )
    op.create_check_constraint(
        "ck_scheduled_activities_materialization_state",
        "scheduled_activities",
        "(materialization_status = 'PENDING' AND command_id IS NULL "
        "AND materialization_at IS NULL AND materialization_reason IS NULL) OR "
        "(materialization_status = 'MATERIALIZED' AND command_id IS NOT NULL "
        "AND materialization_at IS NOT NULL AND materialization_reason IS NULL) OR "
        "(materialization_status = 'NON_MATERIALIZABLE' AND command_id IS NULL "
        "AND materialization_at IS NOT NULL AND materialization_reason IS NOT NULL "
        "AND length(materialization_reason) > 0)",
    )
    op.create_foreign_key(
        "fk_scheduled_activities_command",
        "scheduled_activities",
        "commands",
        ["command_id"],
        ["command_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_scheduled_activities_command_id", "scheduled_activities", ["command_id"]
    )
    op.create_index(
        "ix_scheduled_activities_materialization_due",
        "scheduled_activities",
        ["materialization_status", "due_at"],
        unique=False,
    )


def downgrade() -> None:
    has_durable_priority_or_materialization = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM commands WHERE priority <> 0) "
            "OR EXISTS (SELECT 1 FROM scheduled_activities "
            "WHERE materialization_status <> 'PENDING' OR command_id IS NOT NULL "
            "OR materialization_at IS NOT NULL OR materialization_reason IS NOT NULL)"
        )
    )
    if has_durable_priority_or_materialization:
        raise RuntimeError(
            "ACTIVITY_MATERIALIZATION_DOWNGRADE_BLOCKED: clear non-default Command priority "
            "and ScheduledActivity materialization history before downgrade"
        )

    op.drop_index("ix_scheduled_activities_materialization_due", table_name="scheduled_activities")
    op.drop_constraint("uq_scheduled_activities_command_id", "scheduled_activities", type_="unique")
    op.drop_constraint(
        "fk_scheduled_activities_command", "scheduled_activities", type_="foreignkey"
    )
    op.drop_constraint(
        "ck_scheduled_activities_materialization_state", "scheduled_activities", type_="check"
    )
    op.drop_constraint(
        "ck_scheduled_activities_materialization_status", "scheduled_activities", type_="check"
    )
    op.drop_column("scheduled_activities", "materialization_reason")
    op.drop_column("scheduled_activities", "materialization_at")
    op.drop_column("scheduled_activities", "command_id")
    op.drop_column("scheduled_activities", "materialization_status")
    op.drop_constraint("ck_commands_priority", "commands", type_="check")
    op.drop_column("commands", "priority")
