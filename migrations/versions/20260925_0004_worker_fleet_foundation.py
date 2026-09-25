"""Add worker fleet registry and account affinity foundations.

Revision ID: 20260925_0004
Revises: 20260925_0003
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0004"
down_revision: str | None = "20260925_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "threads_accounts",
        sa.Column(
            "execution_mode",
            sa.Enum(
                "API_ONLY",
                "BROWSER_ONLY",
                "HYBRID",
                "MANUAL",
                name="account_execution_mode",
                native_enum=False,
                length=40,
            ),
            server_default="API_ONLY",
            nullable=False,
        ),
    )
    op.alter_column("threads_accounts", "execution_mode", server_default=None)

    op.create_table(
        "worker_nodes",
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("platform", sa.String(length=80), nullable=False),
        sa.Column("agent_version", sa.String(length=80), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "REGISTERING",
                "ONLINE",
                "DEGRADED",
                "DRAINING",
                "OFFLINE",
                "DISABLED",
                "UPGRADE_REQUIRED",
                name="worker_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("max_concurrent_jobs", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("presence_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("max_concurrent_jobs > 0", name="ck_worker_nodes_positive_capacity"),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_worker_nodes_status_presence", "worker_nodes", ["status", "presence_expires_at"]
    )

    op.create_table(
        "worker_capabilities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability_name", sa.String(length=120), nullable=False),
        sa.Column("capability_version", sa.Integer(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("advertised_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("capability_version > 0", name="ck_worker_capability_positive_version"),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "worker_id", "capability_name", "capability_version", name="uq_worker_capability"
        ),
    )
    op.create_index(
        "ix_worker_capabilities_name_version",
        "worker_capabilities",
        ["capability_name", "capability_version"],
    )

    op.create_table(
        "browser_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_ref", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "position('/' in profile_ref) = 0 AND position(chr(92) in profile_ref) = 0 "
            "AND position(':' in profile_ref) = 0",
            name="ck_browser_profiles_logical_ref",
        ),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_id", "profile_ref", name="uq_browser_profiles_worker_ref"),
        sa.UniqueConstraint("worker_id", "id", name="uq_browser_profiles_worker_id"),
    )

    op.create_table(
        "network_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "protocol",
            sa.Enum(
                "DIRECT",
                "HTTP",
                "HTTPS",
                "SOCKS5",
                name="network_protocol",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("credential_ref", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(protocol = 'DIRECT' AND host IS NULL AND port IS NULL AND credential_ref IS NULL) OR "
            "(protocol <> 'DIRECT' AND host IS NOT NULL AND port BETWEEN 1 AND 65535)",
            name="ck_network_profiles_routing_config",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "id", name="uq_network_profiles_account_id"),
    )

    op.create_table(
        "account_worker_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_ref", sa.String(length=255), nullable=False),
        sa.Column("network_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(is_active AND ended_at IS NULL) OR (NOT is_active AND ended_at IS NOT NULL)",
            name="ck_assignment_active_end_time",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["worker_id", "profile_ref"],
            ["browser_profiles.worker_id", "browser_profiles.profile_ref"],
            ondelete="RESTRICT",
            name="fk_assignment_worker_profile",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "network_profile_id"],
            ["network_profiles.account_id", "network_profiles.id"],
            ondelete="RESTRICT",
            name="fk_assignment_account_network_profile",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_account_worker_assignments_active_account",
        "account_worker_assignments",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_account_worker_assignments_worker_active",
        "account_worker_assignments",
        ["worker_id", "is_active"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_worker_assignments_worker_active", table_name="account_worker_assignments"
    )
    op.drop_index(
        "uq_account_worker_assignments_active_account", table_name="account_worker_assignments"
    )
    op.drop_table("account_worker_assignments")
    op.drop_table("network_profiles")
    op.drop_table("browser_profiles")
    op.drop_index("ix_worker_capabilities_name_version", table_name="worker_capabilities")
    op.drop_table("worker_capabilities")
    op.drop_index("ix_worker_nodes_status_presence", table_name="worker_nodes")
    op.drop_table("worker_nodes")
    op.drop_column("threads_accounts", "execution_mode")
