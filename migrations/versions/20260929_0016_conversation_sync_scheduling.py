"""Add durable periodic conversation sync schedules and dispatch audit."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0016"
down_revision: str | None = "20260929_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_sync_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("threads_post_id", sa.String(length=255), nullable=False),
        sa.Column("sync_kind", sa.String(length=40), nullable=False),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("status_reason", sa.String(length=240), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_dispatched_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_command_id", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(btrim(threads_post_id)) > 0",
            name="ck_conversation_sync_schedules_root_nonblank",
        ),
        sa.CheckConstraint(
            "sync_kind IN ('conversation', 'replies')",
            name="ck_conversation_sync_schedules_sync_kind",
        ),
        sa.CheckConstraint(
            "interval_seconds BETWEEN 900 AND 2592000",
            name="ck_conversation_sync_schedules_interval_bounds",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'PAUSED', 'DISABLED')",
            name="ck_conversation_sync_schedules_status",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_conversation_sync_schedules_positive_revision",
        ),
        sa.CheckConstraint(
            "next_due_at > anchor_at",
            name="ck_conversation_sync_schedules_next_due_after_anchor",
        ),
        sa.CheckConstraint(
            "mod(extract(epoch from (next_due_at - anchor_at)), interval_seconds) = 0",
            name="ck_conversation_sync_schedules_next_due_slot",
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND status_reason IS NULL) OR "
            "(status <> 'ACTIVE' AND status_reason IS NOT NULL "
            "AND length(btrim(status_reason)) > 0)",
            name="ck_conversation_sync_schedules_status_reason",
        ),
        sa.CheckConstraint(
            "(last_dispatched_due_at IS NULL AND last_command_id IS NULL) OR "
            "(last_dispatched_due_at IS NOT NULL AND last_command_id IS NOT NULL "
            "AND next_due_at > last_dispatched_due_at "
            "AND mod(extract(epoch from (last_dispatched_due_at - anchor_at)), "
            "interval_seconds) = 0)",
            name="ck_conversation_sync_schedules_last_dispatch_pair",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["threads_accounts.id"],
            name="fk_conversation_sync_schedules_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_command_id"],
            ["commands.command_id"],
            name="fk_conversation_sync_schedules_last_command",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_conversation_sync_schedules_active_target",
        "conversation_sync_schedules",
        ["account_id", "threads_post_id", "sync_kind"],
        unique=True,
        postgresql_where=sa.text("status <> 'DISABLED'"),
    )
    op.create_index(
        "ix_conversation_sync_schedules_due",
        "conversation_sync_schedules",
        ["next_due_at", "id"],
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "conversation_sync_dispatches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schedule_revision", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("command_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schedule_revision > 0",
            name="ck_conversation_sync_dispatches_positive_revision",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["conversation_sync_schedules.id"],
            name="fk_conversation_sync_dispatches_schedule",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["commands.command_id"],
            name="fk_conversation_sync_dispatches_command",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "due_at", name="uq_conversation_sync_dispatches_schedule_due"
        ),
        sa.UniqueConstraint("command_id", name="uq_conversation_sync_dispatches_command"),
    )
    op.create_index(
        "ix_conversation_sync_dispatches_schedule_due",
        "conversation_sync_dispatches",
        ["schedule_id", "due_at"],
        unique=False,
    )


def downgrade() -> None:
    has_history = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM conversation_sync_schedules) "
            "OR EXISTS (SELECT 1 FROM conversation_sync_dispatches)"
        )
    )
    if has_history:
        raise RuntimeError(
            "CONVERSATION_SYNC_SCHEDULING_DOWNGRADE_BLOCKED: clear schedule and dispatch "
            "history before downgrade"
        )

    op.drop_index(
        "ix_conversation_sync_dispatches_schedule_due",
        table_name="conversation_sync_dispatches",
    )
    op.drop_table("conversation_sync_dispatches")
    op.drop_index(
        "ix_conversation_sync_schedules_due",
        table_name="conversation_sync_schedules",
    )
    op.drop_index(
        "uq_conversation_sync_schedules_active_target",
        table_name="conversation_sync_schedules",
    )
    op.drop_table("conversation_sync_schedules")
