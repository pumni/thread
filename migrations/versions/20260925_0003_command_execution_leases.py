"""Add durable command execution leases and recovery checkpoints.

Revision ID: 20260925_0003
Revises: 20260925_0002
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0003"
down_revision: str | None = "20260925_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("commands", sa.Column("execution_lease_token", postgresql.UUID(as_uuid=True)))
    op.add_column("commands", sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "commands",
        sa.Column("checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_commands_execution_lease",
        "commands",
        ["status", "execution_lease_expires_at"],
    )
    op.create_check_constraint(
        "ck_commands_execution_lease_pair",
        "commands",
        "(execution_lease_token IS NULL) = (execution_lease_expires_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_commands_execution_lease_pair", "commands", type_="check")
    op.drop_index("ix_commands_execution_lease", table_name="commands")
    op.drop_column("commands", "checkpoint")
    op.drop_column("commands", "execution_lease_expires_at")
    op.drop_column("commands", "execution_lease_token")
