"""Add Workspace Operators, opaque sessions and security audit records."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261003_0017"
down_revision: str | None = "20260929_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("singleton_key", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("singleton_key = 1", name="ck_workspaces_singleton"),
        sa.PrimaryKeyConstraint("workspace_id"),
        sa.UniqueConstraint("singleton_key", name="uq_workspaces_singleton_key"),
    )
    workspace_id = uuid4()
    op.bulk_insert(
        sa.table(
            "workspaces",
            sa.column("workspace_id", postgresql.UUID(as_uuid=True)),
            sa.column("singleton_key", sa.Integer()),
            sa.column("created_at", sa.DateTime(timezone=True)),
        ),
        [{"workspace_id": workspace_id, "singleton_key": 1, "created_at": datetime.now(UTC)}],
    )

    op.create_table(
        "operator_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "must_change_password", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('OWNER', 'ADMIN', 'OPERATOR', 'VIEWER')", name="ck_operator_users_role"
        ),
        sa.CheckConstraint(
            "length(btrim(username)) > 0", name="ck_operator_users_username_nonblank"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "username", name="uq_operator_users_workspace_username"
        ),
    )
    op.create_index(
        "ix_operator_users_workspace_enabled_role",
        "operator_users",
        ["workspace_id", "enabled", "role"],
    )

    op.create_table(
        "operator_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operator_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["operator_user_id"], ["operator_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_operator_sessions_token_digest"),
    )
    op.create_index(
        "ix_operator_sessions_user_expiry", "operator_sessions", ["operator_user_id", "expires_at"]
    )

    op.create_table(
        "operator_login_throttles",
        sa.Column("operator_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "failed_attempts BETWEEN 1 AND 5",
            name="ck_operator_login_throttles_failed_attempts",
        ),
        sa.ForeignKeyConstraint(["operator_user_id"], ["operator_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("operator_user_id"),
    )

    op.create_table(
        "workspace_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_username", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=80), nullable=True),
        sa.Column("target_id", sa.String(length=255), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["operator_users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workspace_audit_events_workspace_created",
        "workspace_audit_events",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_audit_events_workspace_created", table_name="workspace_audit_events"
    )
    op.drop_table("workspace_audit_events")
    op.drop_table("operator_login_throttles")
    op.drop_index("ix_operator_sessions_user_expiry", table_name="operator_sessions")
    op.drop_table("operator_sessions")
    op.drop_index("ix_operator_users_workspace_enabled_role", table_name="operator_users")
    op.drop_table("operator_users")
    op.drop_table("workspaces")
