"""Add deterministic capability routing and account mutation fencing.

Revision ID: 20260925_0007
Revises: 20260925_0006
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0007"
down_revision: str | None = "20260925_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_jobs",
        sa.Column(
            "operation_class",
            sa.Enum(
                "READ",
                "MUTATION",
                "SESSION",
                "BACKGROUND",
                name="worker_job_operation_class",
                native_enum=False,
                length=40,
            ),
            server_default=sa.text("'READ'"),
            nullable=False,
        ),
    )
    op.add_column(
        "worker_jobs",
        sa.Column("account_coordination_generation", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_worker_jobs_account_coordination_fence",
        "worker_jobs",
        "(account_coordination_generation IS NULL OR "
        "(status = 'RUNNING' AND account_id IS NOT NULL AND operation_class <> 'READ')) "
        "AND (status <> 'RUNNING' OR account_id IS NULL OR operation_class = 'READ' "
        "OR account_coordination_generation IS NOT NULL)",
    )

    op.create_table(
        "account_execution_leases",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "owner_type",
            sa.Enum(
                "COMMAND",
                "WORKER_JOB",
                name="account_execution_owner_type",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column(
            "operation_class",
            sa.Enum(
                "READ",
                "MUTATION",
                "SESSION",
                "BACKGROUND",
                name="account_execution_operation_class",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("fencing_generation", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("fencing_generation >= 0", name="ck_account_execution_lease_generation"),
        sa.CheckConstraint(
            "operation_class <> 'READ'",
            name="ck_account_execution_lease_exclusive_operation",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_index(
        "ix_account_execution_lease_expiry",
        "account_execution_leases",
        ["lease_expires_at"],
    )

    op.create_table(
        "command_route_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", sa.String(length=255), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability_name", sa.String(length=120), nullable=False),
        sa.Column("capability_version", sa.Integer(), nullable=False),
        sa.Column(
            "execution_class",
            sa.Enum(
                "NATIVE_API",
                "HYBRID",
                "BROWSER_ASSISTED",
                "HUMAN_ASSISTED",
                "UNSUPPORTED",
                name="capability_execution_class",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "operation_class",
            sa.Enum(
                "READ",
                "MUTATION",
                "SESSION",
                "BACKGROUND",
                name="capability_operation_class",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "account_mode",
            sa.Enum(
                "API_ONLY",
                "BROWSER_ONLY",
                "HYBRID",
                "MANUAL",
                name="route_account_execution_mode",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "target",
            sa.Enum(
                "LOCAL_API",
                "WORKER_JOB",
                "WAITING_EXECUTION",
                "WAITING_INTERVENTION",
                "UNSUPPORTED",
                name="capability_route_target",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "executor",
            sa.Enum(
                "API",
                "WORKER",
                name="capability_executor",
                native_enum=False,
                length=40,
            ),
            nullable=True,
        ),
        sa.Column("reason_code", sa.String(length=120), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("capability_version > 0", name="ck_command_route_capability_version"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_command_route_attempt_count"),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_id"], ["commands.command_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_command_route_decisions_command",
        "command_route_decisions",
        ["command_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_command_route_decisions_command", table_name="command_route_decisions")
    op.drop_table("command_route_decisions")
    op.drop_index("ix_account_execution_lease_expiry", table_name="account_execution_leases")
    op.drop_table("account_execution_leases")
    op.drop_constraint("ck_worker_jobs_account_coordination_fence", "worker_jobs", type_="check")
    op.drop_column("worker_jobs", "account_coordination_generation")
    op.drop_column("worker_jobs", "operation_class")
