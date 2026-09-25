"""Add API-first discovery, provenance, resumable runs, and leads.

Revision ID: 20260926_0009
Revises: 20260925_0008
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260926_0009"
down_revision: str | None = "20260925_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.Uuid:
    return sa.Uuid(as_uuid=True)


def _enum() -> sa.String:
    return sa.String(length=40)


def upgrade() -> None:
    op.create_table(
        "discovery_campaigns",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("account_id", _uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", _enum(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_discovery_campaign_account_status",
        "discovery_campaigns",
        ["account_id", "status"],
    )
    op.create_table(
        "discovery_search_queries",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("campaign_id", _uuid(), nullable=False),
        sa.Column("kind", _enum(), nullable=False),
        sa.Column("query_text", sa.String(length=255), nullable=True),
        sa.Column("search_mode", _enum(), nullable=True),
        sa.Column("search_type", _enum(), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("thread_remote_id", sa.String(length=255), nullable=True),
        sa.Column("since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("query_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "since IS NULL OR until IS NULL OR since < until",
            name="ck_discovery_query_time_window",
        ),
        sa.CheckConstraint(
            "(kind <> 'SEARCH' OR (query_text IS NOT NULL AND search_mode IS NOT NULL "
            "AND search_type IS NOT NULL)) AND "
            "(kind <> 'PROFILE' OR username IS NOT NULL) AND "
            "(kind <> 'CONVERSATION' OR thread_remote_id IS NOT NULL)",
            name="ck_discovery_query_kind_fields",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["discovery_campaigns.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "query_fingerprint", name="uq_discovery_query_identity"),
    )
    op.create_index(
        "ix_discovery_queries_campaign_kind",
        "discovery_search_queries",
        ["campaign_id", "kind"],
    )
    op.create_table(
        "discovered_authors",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("remote_author_id", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("biography", sa.String(length=5000), nullable=True),
        sa.Column("profile_picture_url", sa.String(length=2048), nullable=True),
        sa.Column("enrichment_status", _enum(), nullable=False),
        sa.Column("last_enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_author_id", name="uq_discovered_authors_remote_id"),
    )
    op.create_index("ix_discovered_authors_username", "discovered_authors", ["username"])
    op.create_table(
        "discovered_threads",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("remote_thread_id", sa.String(length=255), nullable=False),
        sa.Column("author_id", _uuid(), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("permalink", sa.String(length=2048), nullable=True),
        sa.Column("media_type", sa.String(length=80), nullable=True),
        sa.Column("remote_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_quote_post", sa.Boolean(), nullable=True),
        sa.Column("has_replies", sa.Boolean(), nullable=True),
        sa.Column("enrichment_status", _enum(), nullable=False),
        sa.Column("conversation_status", _enum(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["discovered_authors.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_thread_id", name="uq_discovered_threads_remote_id"),
    )
    op.create_index(
        "ix_discovered_threads_author_created",
        "discovered_threads",
        ["author_id", "remote_created_at"],
    )
    op.create_table(
        "discovery_runs",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("account_id", _uuid(), nullable=False),
        sa.Column("campaign_id", _uuid(), nullable=False),
        sa.Column("query_id", _uuid(), nullable=False),
        sa.Column("command_id", sa.String(length=255), nullable=False),
        sa.Column("status", _enum(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("profile_lookup_complete", sa.Boolean(), nullable=False),
        sa.Column("pages_processed", sa.Integer(), nullable=False),
        sa.Column("items_processed", sa.Integer(), nullable=False),
        sa.Column("items_skipped", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("pages_processed >= 0", name="ck_discovery_run_pages_nonnegative"),
        sa.CheckConstraint("items_processed >= 0", name="ck_discovery_run_items_nonnegative"),
        sa.CheckConstraint("items_skipped >= 0", name="ck_discovery_run_items_skipped_nonnegative"),
        sa.CheckConstraint(
            "cursor IS NULL OR char_length(cursor) <= 4096",
            name="ck_discovery_run_cursor_bounded",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["discovery_campaigns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["query_id"], ["discovery_search_queries.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id", name="uq_discovery_runs_command_id"),
    )
    op.create_index(
        "ix_discovery_runs_campaign_status",
        "discovery_runs",
        ["campaign_id", "status", "updated_at"],
    )
    op.create_table(
        "discovery_run_cursors",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("cursor_digest", sa.String(length=64), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.CheckConstraint("page_number > 0", name="ck_discovery_run_cursor_page_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["discovery_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "cursor_digest", name="uq_discovery_run_cursor_digest"),
        sa.UniqueConstraint("run_id", "page_number", name="uq_discovery_run_cursor_page"),
    )
    op.create_index(
        "ix_discovery_run_cursors_run_page", "discovery_run_cursors", ["run_id", "page_number"]
    )
    op.create_table(
        "discovery_source_evidence",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("thread_id", _uuid(), nullable=True),
        sa.Column("author_id", _uuid(), nullable=True),
        sa.Column("source", _enum(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_class", _enum(), nullable=False),
        sa.CheckConstraint(
            "(thread_id IS NOT NULL) <> (author_id IS NOT NULL)",
            name="ck_discovery_evidence_exactly_one_entity",
        ),
        sa.CheckConstraint("page_number >= 0", name="ck_discovery_evidence_page_nonnegative"),
        sa.ForeignKeyConstraint(["run_id"], ["discovery_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["thread_id"], ["discovered_threads.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["author_id"], ["discovered_authors.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "source", "page_number", "thread_id", name="uq_discovery_evidence_thread"
        ),
        sa.UniqueConstraint(
            "run_id", "source", "page_number", "author_id", name="uq_discovery_evidence_author"
        ),
    )
    op.create_index(
        "ix_discovery_evidence_run_page", "discovery_source_evidence", ["run_id", "page_number"]
    )
    op.create_index(
        "ix_discovery_evidence_thread_source",
        "discovery_source_evidence",
        ["thread_id", "source"],
    )
    op.create_index(
        "ix_discovery_evidence_author_source",
        "discovery_source_evidence",
        ["author_id", "source"],
    )
    op.create_table(
        "lead_candidates",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("account_id", _uuid(), nullable=False),
        sa.Column("author_id", _uuid(), nullable=False),
        sa.Column("status", _enum(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["threads_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["author_id"], ["discovered_authors.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "author_id", name="uq_lead_candidate_account_author"),
    )
    op.create_index(
        "ix_lead_candidates_account_status",
        "lead_candidates",
        ["account_id", "status", "updated_at"],
    )
    op.create_table(
        "lead_candidate_evidence",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("candidate_id", _uuid(), nullable=False),
        sa.Column("evidence_id", _uuid(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["lead_candidates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["discovery_source_evidence.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "evidence_id", name="uq_lead_candidate_evidence"),
    )
    op.create_table(
        "lead_candidate_transitions",
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("candidate_id", _uuid(), nullable=False),
        sa.Column("command_id", sa.String(length=255), nullable=False),
        sa.Column("previous_status", _enum(), nullable=True),
        sa.Column("next_status", _enum(), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["lead_candidates.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id", name="uq_lead_transition_command_id"),
    )
    op.create_index(
        "ix_lead_candidate_transitions_candidate",
        "lead_candidate_transitions",
        ["candidate_id", "occurred_at"],
    )

    op.add_column("replies", sa.Column("discovered_thread_id", _uuid(), nullable=True))
    op.create_foreign_key(
        "fk_replies_discovered_thread_id_discovered_threads",
        "replies",
        "discovered_threads",
        ["discovered_thread_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.alter_column(
        "replies",
        "root_post_id",
        existing_type=_uuid(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_replies_exactly_one_root",
        "replies",
        "(root_post_id IS NOT NULL) <> (discovered_thread_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_replies_exactly_one_root", "replies", type_="check")
    op.execute("UPDATE replies SET parent_reply_id = NULL WHERE discovered_thread_id IS NOT NULL")
    op.execute("DELETE FROM replies WHERE discovered_thread_id IS NOT NULL")
    op.alter_column(
        "replies",
        "root_post_id",
        existing_type=_uuid(),
        nullable=False,
    )
    op.drop_constraint(
        "fk_replies_discovered_thread_id_discovered_threads", "replies", type_="foreignkey"
    )
    op.drop_column("replies", "discovered_thread_id")

    op.drop_index(
        "ix_lead_candidate_transitions_candidate", table_name="lead_candidate_transitions"
    )
    op.drop_table("lead_candidate_transitions")
    op.drop_table("lead_candidate_evidence")
    op.drop_index("ix_lead_candidates_account_status", table_name="lead_candidates")
    op.drop_table("lead_candidates")
    op.drop_index("ix_discovery_evidence_author_source", table_name="discovery_source_evidence")
    op.drop_index("ix_discovery_evidence_thread_source", table_name="discovery_source_evidence")
    op.drop_index("ix_discovery_evidence_run_page", table_name="discovery_source_evidence")
    op.drop_table("discovery_source_evidence")
    op.drop_index("ix_discovery_run_cursors_run_page", table_name="discovery_run_cursors")
    op.drop_table("discovery_run_cursors")
    op.drop_index("ix_discovery_runs_campaign_status", table_name="discovery_runs")
    op.drop_table("discovery_runs")
    op.drop_index("ix_discovered_threads_author_created", table_name="discovered_threads")
    op.drop_table("discovered_threads")
    op.drop_index("ix_discovered_authors_username", table_name="discovered_authors")
    op.drop_table("discovered_authors")
    op.drop_index("ix_discovery_queries_campaign_kind", table_name="discovery_search_queries")
    op.drop_table("discovery_search_queries")
    op.drop_index("ix_discovery_campaign_account_status", table_name="discovery_campaigns")
    op.drop_table("discovery_campaigns")
