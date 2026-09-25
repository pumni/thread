from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now


class DiscoveryCampaignStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETE = "COMPLETE"


class DiscoveryQueryKind(StrEnum):
    SEARCH = "SEARCH"
    PROFILE = "PROFILE"
    MENTIONS = "MENTIONS"
    CONVERSATION = "CONVERSATION"


class DiscoverySearchMode(StrEnum):
    KEYWORD = "KEYWORD"
    TAG = "TAG"


class DiscoverySearchType(StrEnum):
    TOP = "TOP"
    RECENT = "RECENT"


class DiscoveryRunStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


class DiscoveryEnrichmentStatus(StrEnum):
    ENRICHMENT_NEEDED = "ENRICHMENT_NEEDED"
    ENRICHED = "ENRICHED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


class DiscoveryEvidenceSource(StrEnum):
    KEYWORD_SEARCH = "KEYWORD_SEARCH"
    TAG_SEARCH = "TAG_SEARCH"
    PUBLIC_PROFILE_LOOKUP = "PUBLIC_PROFILE_LOOKUP"
    PROFILE_POSTS = "PROFILE_POSTS"
    MENTIONS = "MENTIONS"
    CONVERSATION = "CONVERSATION"


class DiscoveryEvidenceClass(StrEnum):
    DOCUMENTATION_CONTRACT = "DOCUMENTATION_CONTRACT"
    LIVE_SCRUBBED = "LIVE_SCRUBBED"


class LeadCandidateStatus(StrEnum):
    ENRICHMENT_PENDING = "ENRICHMENT_PENDING"
    CANDIDATE = "CANDIDATE"
    READY = "READY"
    DISMISSED = "DISMISSED"


@dataclass(slots=True)
class DiscoveryCampaign:
    account_id: UUID
    name: str
    id: UUID = field(default_factory=uuid4)
    status: DiscoveryCampaignStatus = DiscoveryCampaignStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 120:
            raise ValueError("campaign name must contain 1 to 120 characters")
        self.name = self.name.strip()
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)


@dataclass(slots=True)
class SearchQuery:
    campaign_id: UUID
    kind: DiscoveryQueryKind
    query_text: str | None = None
    search_mode: DiscoverySearchMode | None = None
    search_type: DiscoverySearchType | None = None
    username: str | None = None
    thread_remote_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.query_text is not None:
            self.query_text = self.query_text.strip()
            if not self.query_text or len(self.query_text) > 255:
                raise ValueError("search query must contain 1 to 255 characters")
        if self.username is not None:
            self.username = self.username.strip()
            if not self.username or len(self.username) > 255:
                raise ValueError("username must contain 1 to 255 characters")
        if self.thread_remote_id is not None and (
            not self.thread_remote_id.strip() or len(self.thread_remote_id) > 255
        ):
            raise ValueError("thread remote id must contain 1 to 255 characters")
        if self.since is not None:
            self.since = normalize_utc(self.since)
        if self.until is not None:
            self.until = normalize_utc(self.until)
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("discovery time window must have since before until")
        if self.kind is DiscoveryQueryKind.SEARCH:
            if (
                self.query_text is None
                or self.search_mode is None
                or self.search_type is None
                or self.username is not None
                or self.thread_remote_id is not None
            ):
                raise ValueError("search query requires search text, mode, and type")
        elif self.kind is DiscoveryQueryKind.PROFILE:
            if (
                self.username is None
                or self.query_text is not None
                or self.search_mode is not None
                or self.search_type is not None
                or self.thread_remote_id is not None
            ):
                raise ValueError("profile query requires only a username and time window")
        elif self.kind is DiscoveryQueryKind.MENTIONS:
            if any(
                value is not None
                for value in (
                    self.query_text,
                    self.search_mode,
                    self.search_type,
                    self.username,
                    self.thread_remote_id,
                )
            ):
                raise ValueError("mentions query does not accept a search, username, or thread")
        elif self.kind is DiscoveryQueryKind.CONVERSATION:
            if (
                self.thread_remote_id is None
                or self.query_text is not None
                or self.search_mode is not None
                or self.search_type is not None
                or self.username is not None
            ):
                raise ValueError("conversation query requires only a thread remote id")
        self.created_at = normalize_utc(self.created_at)

    def identity_key(self) -> str:
        since = self.since.isoformat() if self.since is not None else ""
        until = self.until.isoformat() if self.until is not None else ""
        return json.dumps(
            [
                self.kind.value,
                self.query_text or "",
                self.search_mode.value if self.search_mode else "",
                self.search_type.value if self.search_type else "",
                self.username or "",
                self.thread_remote_id or "",
                since,
                until,
            ],
            separators=(",", ":"),
        )


@dataclass(slots=True)
class DiscoveryRun:
    account_id: UUID
    campaign_id: UUID
    query_id: UUID
    command_id: str
    id: UUID = field(default_factory=uuid4)
    status: DiscoveryRunStatus = DiscoveryRunStatus.RUNNING
    cursor: str | None = None
    profile_lookup_complete: bool = False
    pages_processed: int = 0
    items_processed: int = 0
    items_skipped: int = 0
    error_code: str | None = None
    started_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.command_id.strip() or len(self.command_id) > 255:
            raise ValueError("discovery run command id must contain 1 to 255 characters")
        if self.cursor is not None and (not self.cursor or len(self.cursor) > 4096):
            raise ValueError("discovery cursor must contain 1 to 4096 characters")
        if self.pages_processed < 0 or self.items_processed < 0 or self.items_skipped < 0:
            raise ValueError("discovery run counters must not be negative")
        self.started_at = normalize_utc(self.started_at)
        self.updated_at = normalize_utc(self.updated_at)
        if self.finished_at is not None:
            self.finished_at = normalize_utc(self.finished_at)


@dataclass(slots=True)
class DiscoveredAuthor:
    remote_author_id: str
    username: str
    id: UUID = field(default_factory=uuid4)
    display_name: str | None = None
    biography: str | None = None
    profile_picture_url: str | None = None
    enrichment_status: DiscoveryEnrichmentStatus = DiscoveryEnrichmentStatus.ENRICHMENT_NEEDED
    last_enriched_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.remote_author_id = _required_bounded(self.remote_author_id, "remote author id", 255)
        self.username = _required_bounded(self.username, "username", 255)
        self.display_name = _optional_bounded(self.display_name, "display name", 255)
        self.biography = _optional_bounded(self.biography, "biography", 5000)
        if self.profile_picture_url is not None:
            self.profile_picture_url = _validate_https_url(self.profile_picture_url)
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)
        if self.last_enriched_at is not None:
            self.last_enriched_at = normalize_utc(self.last_enriched_at)


@dataclass(slots=True)
class DiscoveredThread:
    remote_thread_id: str
    id: UUID = field(default_factory=uuid4)
    author_id: UUID | None = None
    username: str | None = None
    text: str | None = None
    permalink: str | None = None
    media_type: str | None = None
    remote_created_at: datetime | None = None
    is_quote_post: bool | None = None
    has_replies: bool | None = None
    enrichment_status: DiscoveryEnrichmentStatus = DiscoveryEnrichmentStatus.ENRICHMENT_NEEDED
    conversation_status: DiscoveryEnrichmentStatus = DiscoveryEnrichmentStatus.ENRICHMENT_NEEDED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.remote_thread_id = _required_bounded(self.remote_thread_id, "remote thread id", 255)
        self.username = _optional_bounded(self.username, "username", 255)
        self.text = _optional_bounded(self.text, "thread text", 10_000)
        self.media_type = _optional_bounded(self.media_type, "media type", 80)
        if self.permalink is not None:
            self.permalink = _validate_https_url(self.permalink)
        if self.remote_created_at is not None:
            self.remote_created_at = normalize_utc(self.remote_created_at)
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)


@dataclass(slots=True)
class DiscoverySourceEvidence:
    run_id: UUID
    source: DiscoveryEvidenceSource
    page_number: int
    thread_id: UUID | None = None
    author_id: UUID | None = None
    observed_at: datetime = field(default_factory=utc_now)
    evidence_class: DiscoveryEvidenceClass = DiscoveryEvidenceClass.DOCUMENTATION_CONTRACT
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if (self.thread_id is None) == (self.author_id is None):
            raise ValueError("source evidence must reference exactly one discovered entity")
        if self.page_number < 0:
            raise ValueError("evidence page number must not be negative")
        self.observed_at = normalize_utc(self.observed_at)


@dataclass(slots=True)
class LeadCandidate:
    account_id: UUID
    author_id: UUID
    status: LeadCandidateStatus = LeadCandidateStatus.ENRICHMENT_PENDING
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)

    def transition(
        self,
        target: LeadCandidateStatus,
        *,
        command_id: str,
        reason_code: str,
        occurred_at: datetime,
    ) -> LeadCandidateTransition:
        if not command_id.strip() or len(command_id) > 255:
            raise ValueError("lead transition command id must contain 1 to 255 characters")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", reason_code) is None:
            raise ValueError("lead transition reason must be a bounded code")
        allowed: dict[LeadCandidateStatus, set[LeadCandidateStatus]] = {
            LeadCandidateStatus.ENRICHMENT_PENDING: {
                LeadCandidateStatus.CANDIDATE,
                LeadCandidateStatus.READY,
                LeadCandidateStatus.DISMISSED,
            },
            LeadCandidateStatus.CANDIDATE: {
                LeadCandidateStatus.READY,
                LeadCandidateStatus.DISMISSED,
            },
            LeadCandidateStatus.READY: {LeadCandidateStatus.DISMISSED},
            LeadCandidateStatus.DISMISSED: set(),
        }
        if target is self.status or target not in allowed[self.status]:
            raise ValueError("lead candidate status transition is not allowed")
        changed_at = normalize_utc(occurred_at)
        previous = self.status
        self.status = target
        self.updated_at = changed_at
        return LeadCandidateTransition(
            candidate_id=self.id,
            previous_status=previous,
            next_status=target,
            command_id=command_id,
            reason_code=reason_code,
            occurred_at=changed_at,
        )


@dataclass(frozen=True, slots=True)
class LeadCandidateTransition:
    candidate_id: UUID
    previous_status: LeadCandidateStatus | None
    next_status: LeadCandidateStatus
    command_id: str
    reason_code: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class LeadCandidateEvidence:
    candidate_id: UUID
    evidence_id: UUID
    id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class DiscoveryRunCursor:
    run_id: UUID
    cursor_digest: str
    page_number: int
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if len(self.cursor_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.cursor_digest
        ):
            raise ValueError("cursor digest must be a lowercase SHA-256 hex digest")
        if self.page_number < 1:
            raise ValueError("cursor page number must be positive")


def _required_bounded(value: str, name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return normalized


def _optional_bounded(value: str | None, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    if len(value) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return value


def _validate_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        len(value) > 2048
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("profile URLs must be bounded HTTPS URLs without embedded credentials")
    return value
