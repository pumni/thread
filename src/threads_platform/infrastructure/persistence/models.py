from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
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

from threads_platform.domain.accounts import AccountStatus, CredentialStatus
from threads_platform.domain.commands import AttemptStatus, CommandStatus
from threads_platform.domain.outbox import DeliveryStatus, OutboxStatus
from threads_platform.domain.publishing import ScheduleStatus
from threads_platform.domain.sync import SyncRunStatus

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
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    error_code: Mapped[str | None] = mapped_column(String(120))


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
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_delivery_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
