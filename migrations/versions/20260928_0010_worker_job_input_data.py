"""Add durable WorkerJob input data."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0010"
down_revision: str | None = "20260926_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_jobs",
        sa.Column("input_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute("UPDATE worker_jobs SET input_data = '{}'::jsonb WHERE input_data IS NULL")
    op.alter_column("worker_jobs", "input_data", nullable=False)


def downgrade() -> None:
    has_payloads = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM worker_jobs "
            "WHERE input_data IS DISTINCT FROM '{}'::jsonb)"
        )
    )
    if has_payloads:
        raise RuntimeError(
            "WORKER_JOB_INPUT_DATA_DOWNGRADE_BLOCKED: clear WorkerJob input data before downgrade"
        )
    op.drop_column("worker_jobs", "input_data")
