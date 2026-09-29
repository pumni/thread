"""Add attempt-bound durable WorkerJob cancellation requests."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0013"
down_revision: str | None = "20260929_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_worker_job_attempt_job_id_id",
        "worker_job_attempts",
        ["worker_job_id", "id"],
    )
    op.create_table(
        "worker_job_cancel_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("target_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_attempt_number", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=120), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "ACKNOWLEDGED",
                "SUPERSEDED",
                name="worker_job_cancel_request_status",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_checkpoint", sa.String(length=80), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_reason", sa.String(length=120), nullable=True),
        sa.CheckConstraint("generation > 0", name="ck_worker_job_cancel_request_generation"),
        sa.CheckConstraint(
            "target_attempt_number > 0",
            name="ck_worker_job_cancel_request_attempt_number",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACKNOWLEDGED', 'SUPERSEDED')",
            name="ck_worker_job_cancel_request_status",
        ),
        sa.CheckConstraint(
            "length(reason_code) BETWEEN 1 AND 120 AND reason_code ~ '^[A-Z0-9_]+$'",
            name="ck_worker_job_cancel_request_reason_code",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND acknowledged_at IS NULL AND safe_checkpoint IS NULL "
            "AND superseded_at IS NULL AND superseded_reason IS NULL) OR "
            "(status = 'ACKNOWLEDGED' AND acknowledged_at IS NOT NULL "
            "AND safe_checkpoint IS NOT NULL AND superseded_at IS NULL "
            "AND superseded_reason IS NULL) OR "
            "(status = 'SUPERSEDED' AND acknowledged_at IS NULL AND safe_checkpoint IS NULL "
            "AND superseded_at IS NOT NULL AND superseded_reason IS NOT NULL "
            "AND length(superseded_reason) BETWEEN 1 AND 120 "
            "AND superseded_reason ~ '^[A-Z0-9_]+$')",
            name="ck_worker_job_cancel_request_outcome",
        ),
        sa.CheckConstraint(
            "safe_checkpoint IS NULL OR safe_checkpoint IN ("
            "'BEFORE_NAVIGATION', 'FEED_READY', 'ITEM_BATCH', 'THREAD_READY', "
            "'BEFORE_PROFILE_INSPECTION', 'PROFILE_READY')",
            name="ck_worker_job_cancel_request_safe_checkpoint",
        ),
        sa.ForeignKeyConstraint(
            ["worker_job_id", "target_attempt_id"],
            ["worker_job_attempts.worker_job_id", "worker_job_attempts.id"],
            ondelete="RESTRICT",
            name="fk_worker_job_cancel_request_target_attempt",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "worker_job_id", "generation", name="uq_worker_job_cancel_request_generation"
        ),
    )
    op.create_index(
        "uq_worker_job_cancel_request_pending_attempt",
        "worker_job_cancel_requests",
        ["worker_job_id", "target_attempt_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_worker_job_cancel_request_status",
        "worker_job_cancel_requests",
        ["worker_job_id", "status"],
    )


def downgrade() -> None:
    has_cancellation_history = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM worker_job_cancel_requests) "
            "OR EXISTS (SELECT 1 FROM commands WHERE status = 'CANCELLED') "
            "OR EXISTS (SELECT 1 FROM worker_job_attempts WHERE status = 'CANCELLED')"
        )
    )
    if has_cancellation_history:
        raise RuntimeError(
            "WORKER_JOB_CANCELLATION_DOWNGRADE_BLOCKED: clear cancellation history and "
            "CANCELLED terminal states before downgrade"
        )

    op.drop_index("ix_worker_job_cancel_request_status", table_name="worker_job_cancel_requests")
    op.drop_index(
        "uq_worker_job_cancel_request_pending_attempt",
        table_name="worker_job_cancel_requests",
    )
    op.drop_table("worker_job_cancel_requests")
    op.drop_constraint("uq_worker_job_attempt_job_id_id", "worker_job_attempts", type_="unique")
