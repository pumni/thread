"""Add Windows Worker Agent capacity and durable account session reports.

Revision ID: 20260925_0008
Revises: 20260925_0007
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0008"
down_revision: str | None = "20260925_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_nodes",
        sa.Column(
            "max_browser_sessions",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column(
        "worker_nodes",
        sa.Column(
            "active_browser_sessions",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_worker_nodes_positive_browser_capacity",
        "worker_nodes",
        "max_browser_sessions > 0",
    )
    op.create_check_constraint(
        "ck_worker_nodes_nonnegative_active_browser_sessions",
        "worker_nodes",
        "active_browser_sessions >= 0",
    )
    op.create_check_constraint(
        "ck_worker_nodes_active_within_browser_capacity",
        "worker_nodes",
        "active_browser_sessions <= max_browser_sessions",
    )
    op.create_index(
        "uq_account_worker_assignments_active_profile",
        "account_worker_assignments",
        ["worker_id", "profile_ref"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "worker_account_sessions",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_ref", sa.String(length=255), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("intervention_required", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_worker_account_sessions_positive_revision"),
        sa.CheckConstraint(
            "state IN ('UNINITIALIZED', 'LOGIN_REQUIRED', 'STARTING', 'AUTHENTICATED', 'BUSY', "
            "'SESSION_EXPIRED', 'CHALLENGE_REQUIRED', 'ERROR', 'STOPPED')",
            name="ck_worker_account_sessions_state",
        ),
        sa.CheckConstraint(
            "intervention_required = (state IN ('LOGIN_REQUIRED', 'SESSION_EXPIRED', "
            "'CHALLENGE_REQUIRED'))",
            name="ck_worker_account_sessions_intervention_state",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["worker_id", "profile_ref"],
            ["browser_profiles.worker_id", "browser_profiles.profile_ref"],
            name="fk_worker_account_session_profile",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_index("ix_worker_account_sessions_worker", "worker_account_sessions", ["worker_id"])


def downgrade() -> None:
    op.drop_index("ix_worker_account_sessions_worker", table_name="worker_account_sessions")
    op.drop_table("worker_account_sessions")
    op.drop_index(
        "uq_account_worker_assignments_active_profile",
        table_name="account_worker_assignments",
    )
    op.drop_constraint(
        "ck_worker_nodes_active_within_browser_capacity", "worker_nodes", type_="check"
    )
    op.drop_constraint(
        "ck_worker_nodes_nonnegative_active_browser_sessions", "worker_nodes", type_="check"
    )
    op.drop_constraint("ck_worker_nodes_positive_browser_capacity", "worker_nodes", type_="check")
    op.drop_column("worker_nodes", "active_browser_sessions")
    op.drop_column("worker_nodes", "max_browser_sessions")
