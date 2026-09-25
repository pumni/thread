from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from threads_platform.domain.accounts import AccountExecutionMode, AccountStatus, CredentialStatus
from threads_platform.domain.commands import AttemptStatus, CommandStatus
from threads_platform.domain.outbox import DeliveryStatus, OutboxStatus
from threads_platform.domain.publishing import ScheduleStatus
from threads_platform.domain.sync import SyncRunStatus
from threads_platform.domain.workers import (
    NetworkProtocol,
    WorkerStatus,
)

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
JSON_OBJECT_DEFAULT = text("'{}'::jsonb")
JSON_ARRAY_DEFAULT = text("'[]'::jsonb")


def _enum_values[EnumValue: StrEnum](members: Iterable[EnumValue]) -> list[str]:
    return [member.value for member in members]


def enum_type[EnumValue: StrEnum](enum_class: type[EnumValue], name: str) -> Enum:
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        length=40,
        values_callable=_enum_values,
    )


class Base(DeclarativeBase):
    pass


class AccountRecord(Base):
    __tablename__ = "threads_accounts"
    __table_args__ = (UniqueConstraint("threads_user_id", name="uq_threads_accounts_user_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    threads_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[AccountStatus] = mapped_column(
        enum_type(AccountStatus, "account_status"), nullable=False
    )
    execution_mode: Mapped[AccountExecutionMode] = mapped_column(
        enum_type(AccountExecutionMode, "account_execution_mode"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OAuthCredentialRecord(Base):
    __tablename__ = "oauth_credentials"
    __table_args__ = (UniqueConstraint("account_id", name="uq_oauth_credentials_account_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    credential_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    token_type: Mapped[str] = mapped_column(String(40), nullable=False)
    granted_scopes: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list, server_default=JSON_ARRAY_DEFAULT
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[CredentialStatus] = mapped_column(
        enum_type(CredentialStatus, "credential_status"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CommandRecord(Base):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_commands_command_id"),
        Index("ix_commands_status_deadline", "status", "deadline_at"),
        Index("ix_commands_execution_lease", "status", "execution_lease_expires_at"),
        CheckConstraint(
            "(execution_lease_token IS NULL) = (execution_lease_expires_at IS NULL)",
            name="ck_commands_execution_lease_pair",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    command_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    command_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict, server_default=JSON_OBJECT_DEFAULT
    )
    status: Mapped[CommandStatus] = mapped_column(
        enum_type(CommandStatus, "command_status"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    error_code: Mapped[str | None] = mapped_column(String(120))
    execution_lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)


class CommandAttemptRecord(Base):
    __tablename__ = "command_attempts"
    __table_args__ = (
        UniqueConstraint("command_id", "attempt_number", name="uq_command_attempts_number"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    command_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("commands.command_id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AttemptStatus] = mapped_column(
        enum_type(AttemptStatus, "command_attempt_status"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(120))


class PostRecord(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("account_id", "threads_post_id", name="uq_posts_account_threads_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    threads_post_id: Mapped[str] = mapped_column(String(255), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    permalink: Mapped[str | None] = mapped_column(String(2048))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_DOCUMENT, nullable=False, default=dict, server_default=JSON_OBJECT_DEFAULT
    )


class ReplyRecord(Base):
    __tablename__ = "replies"
    __table_args__ = (
        UniqueConstraint("account_id", "threads_reply_id", name="uq_replies_account_threads_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    threads_reply_id: Mapped[str] = mapped_column(String(255), nullable=False)
    root_post_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("posts.id", ondelete="RESTRICT"), nullable=False
    )
    parent_reply_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("replies.id", ondelete="RESTRICT")
    )
    text: Mapped[str | None] = mapped_column(Text)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScheduleRecord(Base):
    __tablename__ = "schedules"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    command_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("commands.command_id", ondelete="RESTRICT")
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ScheduleStatus] = mapped_column(
        enum_type(ScheduleStatus, "schedule_status"), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict, server_default=JSON_OBJECT_DEFAULT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SyncStateRecord(Base):
    __tablename__ = "sync_states"
    __table_args__ = (
        UniqueConstraint("account_id", "sync_type", name="uq_sync_states_account_type"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    sync_type: Mapped[str] = mapped_column(String(120), nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SyncRunRecord(Base):
    __tablename__ = "sync_runs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    sync_type: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[SyncRunStatus] = mapped_column(
        enum_type(SyncRunStatus, "sync_run_status"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_from: Mapped[str | None] = mapped_column(Text)
    cursor_to: Mapped[str | None] = mapped_column(Text)
    records_processed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(String(120))


class InsightSnapshotRecord(Base):
    __tablename__ = "insight_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "entity_type",
            "entity_id",
            "metric",
            "captured_at",
            name="uq_insight_snapshot_identity_time",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxEventRecord(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_events_status_available", "status", "available_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(120), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict, server_default=JSON_OBJECT_DEFAULT
    )
    status: Mapped[OutboxStatus] = mapped_column(
        enum_type(OutboxStatus, "outbox_status"), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationDeliveryRecord(Base):
    __tablename__ = "integration_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "destination", name="uq_delivery_event_destination"),
        Index("ix_deliveries_status_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outbox_events.id", ondelete="RESTRICT"), nullable=False
    )
    destination: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        enum_type(DeliveryStatus, "integration_delivery_status"), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivery_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_delivery_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkerNodeRecord(Base):
    __tablename__ = "worker_nodes"
    __table_args__ = (
        CheckConstraint("max_concurrent_jobs > 0", name="ck_worker_nodes_positive_capacity"),
        CheckConstraint(
            "capabilities_schema_version IS NULL OR capabilities_schema_version > 0",
            name="ck_worker_nodes_capabilities_schema_version",
        ),
        CheckConstraint(
            "public_key IS NULL OR octet_length(public_key) = 32",
            name="ck_worker_nodes_public_key_length",
        ),
        Index("ix_worker_nodes_status_presence", "status", "presence_expires_at"),
    )

    worker_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(80), nullable=False)
    agent_version: Mapped[str | None] = mapped_column(String(80))
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    capabilities_schema_version: Mapped[int | None] = mapped_column(Integer)
    public_key: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    status: Mapped[WorkerStatus] = mapped_column(
        enum_type(WorkerStatus, "worker_status"), nullable=False
    )
    max_concurrent_jobs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    presence_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkerCapabilityRecord(Base):
    __tablename__ = "worker_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "worker_id", "capability_name", "capability_version", name="uq_worker_capability"
        ),
        CheckConstraint("capability_version > 0", name="ck_worker_capability_positive_version"),
        Index("ix_worker_capabilities_name_version", "capability_name", "capability_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    worker_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("worker_nodes.worker_id", ondelete="RESTRICT"),
        nullable=False,
    )
    capability_name: Mapped[str] = mapped_column(String(120), nullable=False)
    capability_version: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_DOCUMENT, nullable=False, default=dict, server_default=JSON_OBJECT_DEFAULT
    )
    advertised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerEnrollmentRecord(Base):
    __tablename__ = "worker_enrollments"
    __table_args__ = (
        Index("ix_worker_enrollments_expiry", "expires_at", "consumed_at"),
        UniqueConstraint("token_digest", name="uq_worker_enrollments_token_digest"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(255))


class WorkerAuthChallengeRecord(Base):
    __tablename__ = "worker_auth_challenges"
    __table_args__ = (Index("ix_worker_auth_challenges_expiry", "expires_at", "used_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    worker_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("worker_nodes.worker_id", ondelete="RESTRICT"),
        nullable=False,
    )
    nonce: Mapped[str] = mapped_column(String(100), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerSessionRecord(Base):
    __tablename__ = "worker_sessions"
    __table_args__ = (
        Index("ix_worker_sessions_worker_expiry", "worker_id", "expires_at"),
        UniqueConstraint("token_digest", name="uq_worker_sessions_token_digest"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    worker_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("worker_nodes.worker_id", ondelete="RESTRICT"),
        nullable=False,
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerAuditEventRecord(Base):
    __tablename__ = "worker_audit_events"
    __table_args__ = (Index("ix_worker_audit_events_created", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    worker_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worker_nodes.worker_id", ondelete="RESTRICT")
    )
    enrollment_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("worker_enrollments.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    detail_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BrowserProfileRecord(Base):
    __tablename__ = "browser_profiles"
    __table_args__ = (
        CheckConstraint(
            "position('/' in profile_ref) = 0 AND position(chr(92) in profile_ref) = 0 "
            "AND position(':' in profile_ref) = 0",
            name="ck_browser_profiles_logical_ref",
        ),
        UniqueConstraint("worker_id", "profile_ref", name="uq_browser_profiles_worker_ref"),
        UniqueConstraint("worker_id", "id", name="uq_browser_profiles_worker_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    worker_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("worker_nodes.worker_id", ondelete="RESTRICT"),
        nullable=False,
    )
    profile_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_DOCUMENT, nullable=False, default=dict, server_default=JSON_OBJECT_DEFAULT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class NetworkProfileRecord(Base):
    __tablename__ = "network_profiles"
    __table_args__ = (
        UniqueConstraint("account_id", "id", name="uq_network_profiles_account_id"),
        CheckConstraint(
            "(protocol = 'DIRECT' AND host IS NULL AND port IS NULL AND credential_ref IS NULL) OR "
            "(protocol <> 'DIRECT' AND host IS NOT NULL AND port BETWEEN 1 AND 65535)",
            name="ck_network_profiles_routing_config",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    protocol: Mapped[NetworkProtocol] = mapped_column(
        enum_type(NetworkProtocol, "network_protocol"), nullable=False
    )
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    credential_ref: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AccountWorkerAssignmentRecord(Base):
    __tablename__ = "account_worker_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["worker_id", "profile_ref"],
            ["browser_profiles.worker_id", "browser_profiles.profile_ref"],
            ondelete="RESTRICT",
            name="fk_assignment_worker_profile",
        ),
        ForeignKeyConstraint(
            ["account_id", "network_profile_id"],
            ["network_profiles.account_id", "network_profiles.id"],
            ondelete="RESTRICT",
            name="fk_assignment_account_network_profile",
        ),
        CheckConstraint(
            "(is_active AND ended_at IS NULL) OR (NOT is_active AND ended_at IS NOT NULL)",
            name="ck_assignment_active_end_time",
        ),
        Index(
            "uq_account_worker_assignments_active_account",
            "account_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("ix_account_worker_assignments_worker_active", "worker_id", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("threads_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    worker_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("worker_nodes.worker_id", ondelete="RESTRICT"),
        nullable=False,
    )
    profile_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    network_profile_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
