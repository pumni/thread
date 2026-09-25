"""Create the initial PostgreSQL persistence schema.

Revision ID: 20260925_0001
Revises:
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "threads_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("threads_user_id", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "REAUTHORIZATION_REQUIRED",
                "DISABLED",
                name="account_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("threads_user_id", name="uq_threads_accounts_user_id"),
    )
    op.create_table(
        "oauth_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_ref", sa.String(length=512), nullable=False),
        sa.Column("token_type", sa.String(length=40), nullable=False),
        sa.Column(
            "granted_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "EXPIRED",
                "REAUTHORIZATION_REQUIRED",
                "REVOKED",
                name="credential_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", name="uq_oauth_credentials_account_id"),
    )
    op.create_table(
        "commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.String(length=255), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_type", sa.String(length=120), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "RECEIVED",
                "VALIDATED",
                "PROCESSING",
                "SUCCEEDED",
                "REJECTED",
                "EXPIRED",
                "FAILED_RETRYABLE",
                "FAILED_FINAL",
                name="command_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id", name="uq_commands_command_id"),
    )
    op.create_index("ix_commands_status_deadline", "commands", ["status", "deadline_at"])
    op.create_table(
        "posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("threads_post_id", sa.String(length=255), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("permalink", sa.String(length=2048), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "threads_post_id", name="uq_posts_account_threads_id"),
    )
    op.create_table(
        "replies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("threads_reply_id", sa.String(length=255), nullable=False),
        sa.Column("root_post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_reply_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_reply_id"], ["replies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["root_post_id"], ["posts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "threads_reply_id", name="uq_replies_account_threads_id"),
    )
    op.create_table(
        "schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", sa.String(length=255), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "CANCELLED",
                "FAILED",
                name="schedule_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_id"], ["commands.command_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "sync_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sync_type", sa.String(length=120), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "sync_type", name="uq_sync_states_account_type"),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sync_type", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING",
                "SUCCEEDED",
                "FAILED_RETRYABLE",
                "FAILED_FINAL",
                name="sync_run_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_from", sa.Text(), nullable=True),
        sa.Column("cursor_to", sa.Text(), nullable=True),
        sa.Column("records_processed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "insight_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=120), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "entity_type",
            "entity_id",
            "metric",
            "captured_at",
            name="uq_insight_snapshot_identity_time",
        ),
    )
    op.create_table(
        "command_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", sa.String(length=255), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PROCESSING",
                "SUCCEEDED",
                "FAILED_RETRYABLE",
                "FAILED_FINAL",
                name="command_attempt_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["command_id"], ["commands.command_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id", "attempt_number", name="uq_command_attempts_number"),
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=120), nullable=False),
        sa.Column("aggregate_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("correlation_id", sa.String(length=255), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "DELIVERED",
                "FAILED_FINAL",
                name="outbox_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_events_status_available", "outbox_events", ["status", "available_at"]
    )
    op.create_table(
        "integration_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "DELIVERED",
                "FAILED_RETRYABLE",
                "FAILED_FINAL",
                name="integration_delivery_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_delivery_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "destination", name="uq_delivery_event_destination"),
    )
    op.create_index(
        "ix_deliveries_status_attempt", "integration_deliveries", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_deliveries_status_attempt", table_name="integration_deliveries")
    op.drop_table("integration_deliveries")
    op.drop_index("ix_outbox_events_status_available", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("command_attempts")
    op.drop_table("insight_snapshots")
    op.drop_table("sync_runs")
    op.drop_table("sync_states")
    op.drop_table("schedules")
    op.drop_table("replies")
    op.drop_table("posts")
    op.drop_index("ix_commands_status_deadline", table_name="commands")
    op.drop_table("commands")
    op.drop_table("oauth_credentials")
    op.drop_table("threads_accounts")
