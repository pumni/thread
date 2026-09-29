"""Add durable WorkerJob preemption relationships."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0014"
down_revision: str | None = "20260929_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_worker_job_cancel_request_job_id_id",
        "worker_job_cancel_requests",
        ["worker_job_id", "id"],
    )
    op.create_table(
        "worker_job_preemptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("preemptor_worker_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("victim_worker_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "WAITING_FOR_QUIESCENCE",
                "SATISFIED",
                "SUPERSEDED",
                name="worker_job_preemption_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_reason", sa.String(length=120), nullable=True),
        sa.Column("cancel_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "preemptor_worker_job_id <> victim_worker_job_id",
            name="ck_worker_job_preemption_distinct_jobs",
        ),
        sa.CheckConstraint(
            "status IN ('WAITING_FOR_QUIESCENCE', 'SATISFIED', 'SUPERSEDED')",
            name="ck_worker_job_preemption_status",
        ),
        sa.CheckConstraint(
            "(status = 'WAITING_FOR_QUIESCENCE' AND resolved_at IS NULL "
            "AND resolution_reason IS NULL) OR "
            "(status IN ('SATISFIED', 'SUPERSEDED') AND resolved_at IS NOT NULL "
            "AND resolution_reason IS NOT NULL "
            "AND length(resolution_reason) BETWEEN 1 AND 120 "
            "AND resolution_reason ~ '^[A-Z0-9_]+$')",
            name="ck_worker_job_preemption_resolution",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["preemptor_worker_job_id"], ["worker_jobs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["victim_worker_job_id"], ["worker_jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["victim_worker_job_id", "cancel_request_id"],
            ["worker_job_cancel_requests.worker_job_id", "worker_job_cancel_requests.id"],
            ondelete="RESTRICT",
            name="fk_worker_job_preemption_cancel_request",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "preemptor_worker_job_id",
            "victim_worker_job_id",
            name="uq_worker_job_preemption_pair",
        ),
    )
    op.create_index(
        "ix_worker_job_preemption_account_status",
        "worker_job_preemptions",
        ["account_id", "status"],
    )
    op.create_index(
        "ix_worker_job_preemption_victim_status",
        "worker_job_preemptions",
        ["victim_worker_job_id", "status"],
    )


def downgrade() -> None:
    has_preemption_history = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM worker_job_preemptions)")
    )
    if has_preemption_history:
        raise RuntimeError(
            "WORKER_JOB_PREEMPTION_DOWNGRADE_BLOCKED: clear preemption history before downgrade"
        )

    op.drop_index("ix_worker_job_preemption_victim_status", table_name="worker_job_preemptions")
    op.drop_index("ix_worker_job_preemption_account_status", table_name="worker_job_preemptions")
    op.drop_table("worker_job_preemptions")
    op.drop_constraint(
        "uq_worker_job_cancel_request_job_id_id",
        "worker_job_cancel_requests",
        type_="unique",
    )
