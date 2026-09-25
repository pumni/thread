"""Add command timing and CRM delivery lease fields.

Revision ID: 20260925_0002
Revises: 20260925_0001
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0002"
down_revision: str | None = "20260925_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "commands",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.alter_column("commands", "created_at", server_default=None)
    op.add_column("commands", sa.Column("next_retry_at", sa.DateTime(timezone=True)))
    op.add_column(
        "integration_deliveries", sa.Column("delivery_deadline_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "integration_deliveries",
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "integration_deliveries",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("integration_deliveries", "lease_expires_at")
    op.drop_column("integration_deliveries", "lease_token")
    op.drop_column("integration_deliveries", "delivery_deadline_at")
    op.drop_column("commands", "next_retry_at")
    op.drop_column("commands", "created_at")
