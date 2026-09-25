"""Add durable WorkerJob leases, attempts, and interventions.

Revision ID: 20260925_0006
Revises: 20260925_0005
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0006"
down_revision: str | None = "20260925_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", sa.String(length=255), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "account_affinity_required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("capability_name", sa.String(length=120), nullable=False),
        sa.Column("capability_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "RUNNING",
                "WAITING_INTERVENTION",
                "SUCCEEDED",
                "FAILED_RETRYABLE",
                "FAILED_FINAL",
                "CANCELLED",
                "EXPIRED",
                name="worker_job_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("preemptible", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column(
            "retry_safety",
            sa.Enum(
                "SAFE_TO_RETRY",
                "RECONCILIATION_REQUIRED",
                name="worker_job_retry_safety",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "retry_authorized_by_operator",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("lease_worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "max_attempts > 0 AND attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_worker_jobs_attempt_bound",
        ),
        sa.CheckConstraint(
            "capability_version > 0", name="ck_worker_jobs_positive_capability_version"
        ),
        sa.CheckConstraint(
            "deadline_at IS NULL OR scheduled_at < deadline_at",
            name="ck_worker_jobs_deadline_after_schedule",
        ),
        sa.CheckConstraint(
            "(lease_worker_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_worker_jobs_lease_pair",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND lease_worker_id IS NOT NULL) OR "
            "(status <> 'RUNNING' AND lease_worker_id IS NULL)",
            name="ck_worker_jobs_running_lease",
        ),
        sa.CheckConstraint(
            "NOT account_affinity_required OR "
            "(account_id IS NOT NULL AND assigned_worker_id IS NOT NULL)",
            name="ck_worker_jobs_affinity_assignment",
        ),
        sa.ForeignKeyConstraint(["command_id"], ["commands.command_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["assigned_worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["lease_worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id", name="uq_worker_jobs_command_id"),
    )
    op.create_index("ix_worker_jobs_claim", "worker_jobs", ["status", "scheduled_at", "priority"])
    op.create_index("ix_worker_jobs_lease_expiry", "worker_jobs", ["status", "lease_expires_at"])
    op.create_index(
        "ix_worker_jobs_assigned_worker", "worker_jobs", ["assigned_worker_id", "status"]
    )
    op.create_index("ix_worker_jobs_lease_worker", "worker_jobs", ["lease_worker_id", "status"])

    op.create_table(
        "worker_job_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING",
                "SUCCEEDED",
                "FAILED_RETRYABLE",
                "FAILED_FINAL",
                "ABANDONED",
                "WAITING_INTERVENTION",
                name="worker_job_attempt_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["worker_job_id"], ["worker_jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_job_id", "attempt_number", name="uq_worker_job_attempt_number"),
        sa.UniqueConstraint("lease_token", name="uq_worker_job_attempt_lease_token"),
    )
    op.create_index(
        "ix_worker_job_attempts_job_status", "worker_job_attempts", ["worker_job_id", "status"]
    )

    op.create_table(
        "worker_interventions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("intervention_type", sa.String(length=80), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "OPEN",
                "RESOLVED",
                "CANCELLED",
                name="worker_intervention_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("detail_code", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["worker_job_id"], ["worker_jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_worker_interventions_open_job",
        "worker_interventions",
        ["worker_job_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_index(
        "ix_worker_interventions_worker_status",
        "worker_interventions",
        ["worker_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_interventions_worker_status", table_name="worker_interventions")
    op.drop_index("uq_worker_interventions_open_job", table_name="worker_interventions")
    op.drop_table("worker_interventions")
    op.drop_index("ix_worker_job_attempts_job_status", table_name="worker_job_attempts")
    op.drop_table("worker_job_attempts")
    op.drop_index("ix_worker_jobs_lease_worker", table_name="worker_jobs")
    op.drop_index("ix_worker_jobs_assigned_worker", table_name="worker_jobs")
    op.drop_index("ix_worker_jobs_lease_expiry", table_name="worker_jobs")
    op.drop_index("ix_worker_jobs_claim", table_name="worker_jobs")
    op.drop_table("worker_jobs")
