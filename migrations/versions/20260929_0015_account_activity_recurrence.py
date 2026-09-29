"""Add deterministic recurrence configuration and cursors to account activity."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0015"
down_revision: str | None = "20260929_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "account_activity_template_revisions",
        sa.Column(
            "recurrence_kind",
            sa.String(length=40),
            server_default=sa.text("'NONE'"),
            nullable=False,
        ),
    )
    op.add_column(
        "account_activity_template_revisions",
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "account_activity_template_revisions",
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE account_activity_template_revisions SET recurrence_kind = 'NONE', "
            "anchor_at = NULL, interval_seconds = NULL"
        )
    )
    op.create_check_constraint(
        "ck_account_activity_template_revisions_recurrence_kind",
        "account_activity_template_revisions",
        "recurrence_kind IN ('NONE', 'FIXED_INTERVAL')",
    )
    op.create_check_constraint(
        "ck_account_activity_template_revisions_recurrence_configuration",
        "account_activity_template_revisions",
        "(recurrence_kind = 'NONE' AND anchor_at IS NULL AND interval_seconds IS NULL) OR "
        "(recurrence_kind = 'FIXED_INTERVAL' AND anchor_at IS NOT NULL "
        "AND interval_seconds BETWEEN 900 AND 2592000)",
    )
    op.create_check_constraint(
        "ck_account_activity_template_revisions_recurrence_activity_type",
        "account_activity_template_revisions",
        "recurrence_kind = 'NONE' OR activity_type IN ("
        "'threads.browser.feed.browse', 'threads.browser.thread.open', "
        "'threads.browser.profile.open')",
    )
    op.create_check_constraint(
        "ck_account_activity_template_revisions_recurrence_anchor",
        "account_activity_template_revisions",
        "recurrence_kind = 'NONE' OR anchor_at >= created_at",
    )

    op.create_table(
        "account_activity_recurrence_states",
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_revision", sa.Integer(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_generated_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generated_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "template_revision > 0",
            name="ck_account_activity_recurrence_states_positive_template_revision",
        ),
        sa.CheckConstraint(
            "generated_count >= 0",
            name="ck_account_activity_recurrence_states_nonnegative_generated_count",
        ),
        sa.CheckConstraint(
            "(generated_count = 0 AND last_generated_due_at IS NULL) OR "
            "(generated_count > 0 AND last_generated_due_at IS NOT NULL "
            "AND next_due_at > last_generated_due_at)",
            name="ck_account_activity_recurrence_states_cursor_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "template_revision"],
            [
                "account_activity_template_revisions.template_id",
                "account_activity_template_revisions.revision",
            ],
            name="fk_account_activity_recurrence_states_template_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("template_id", "template_revision"),
    )
    op.create_index(
        "ix_account_activity_recurrence_states_next_due",
        "account_activity_recurrence_states",
        ["next_due_at", "template_id", "template_revision"],
        unique=False,
    )


def downgrade() -> None:
    has_recurrence_data = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM account_activity_template_revisions "
            "WHERE recurrence_kind <> 'NONE' OR anchor_at IS NOT NULL "
            "OR interval_seconds IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM account_activity_recurrence_states)"
        )
    )
    if has_recurrence_data:
        raise RuntimeError(
            "ACCOUNT_ACTIVITY_RECURRENCE_DOWNGRADE_BLOCKED: clear recurrence configuration "
            "and cursor history before downgrade"
        )

    op.drop_index(
        "ix_account_activity_recurrence_states_next_due",
        table_name="account_activity_recurrence_states",
    )
    op.drop_table("account_activity_recurrence_states")
    op.drop_constraint(
        "ck_account_activity_template_revisions_recurrence_anchor",
        "account_activity_template_revisions",
        type_="check",
    )
    op.drop_constraint(
        "ck_account_activity_template_revisions_recurrence_activity_type",
        "account_activity_template_revisions",
        type_="check",
    )
    op.drop_constraint(
        "ck_account_activity_template_revisions_recurrence_configuration",
        "account_activity_template_revisions",
        type_="check",
    )
    op.drop_constraint(
        "ck_account_activity_template_revisions_recurrence_kind",
        "account_activity_template_revisions",
        type_="check",
    )
    op.drop_column("account_activity_template_revisions", "interval_seconds")
    op.drop_column("account_activity_template_revisions", "anchor_at")
    op.drop_column("account_activity_template_revisions", "recurrence_kind")
