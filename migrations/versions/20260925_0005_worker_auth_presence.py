"""Add worker enrollment, device authentication, and presence storage.

Revision ID: 20260925_0005
Revises: 20260925_0004
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0005"
down_revision: str | None = "20260925_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("worker_nodes", sa.Column("capabilities_schema_version", sa.Integer()))
    op.add_column("worker_nodes", sa.Column("public_key", postgresql.BYTEA(), nullable=True))
    op.create_check_constraint(
        "ck_worker_nodes_capabilities_schema_version",
        "worker_nodes",
        "capabilities_schema_version IS NULL OR capabilities_schema_version > 0",
    )
    op.create_check_constraint(
        "ck_worker_nodes_public_key_length",
        "worker_nodes",
        "public_key IS NULL OR octet_length(public_key) = 32",
    )

    op.create_table(
        "worker_enrollments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_worker_enrollments_token_digest"),
    )
    op.create_index(
        "ix_worker_enrollments_expiry", "worker_enrollments", ["expires_at", "consumed_at"]
    )

    op.create_table(
        "worker_auth_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("nonce", sa.String(length=100), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_worker_auth_challenges_expiry",
        "worker_auth_challenges",
        ["expires_at", "used_at"],
    )

    op.create_table(
        "worker_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_worker_sessions_token_digest"),
    )
    op.create_index(
        "ix_worker_sessions_worker_expiry", "worker_sessions", ["worker_id", "expires_at"]
    )

    op.create_table(
        "worker_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("detail_code", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.worker_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["enrollment_id"], ["worker_enrollments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_worker_audit_events_created", "worker_audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_audit_events_created", table_name="worker_audit_events")
    op.drop_table("worker_audit_events")
    op.drop_index("ix_worker_sessions_worker_expiry", table_name="worker_sessions")
    op.drop_table("worker_sessions")
    op.drop_index("ix_worker_auth_challenges_expiry", table_name="worker_auth_challenges")
    op.drop_table("worker_auth_challenges")
    op.drop_index("ix_worker_enrollments_expiry", table_name="worker_enrollments")
    op.drop_table("worker_enrollments")
    op.drop_constraint("ck_worker_nodes_public_key_length", "worker_nodes", type_="check")
    op.drop_constraint("ck_worker_nodes_capabilities_schema_version", "worker_nodes", type_="check")
    op.drop_column("worker_nodes", "public_key")
    op.drop_column("worker_nodes", "capabilities_schema_version")
