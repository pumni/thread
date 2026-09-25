from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from pydantic import SecretStr

from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType

MediaType = Literal["TEXT", "IMAGE", "VIDEO", "CAROUSEL"]


@dataclass(frozen=True, slots=True)
class MediaContainerRequest:
    media_type: MediaType
    text: str | None = None
    image_url: str | None = None
    video_url: str | None = None
    alt_text: str | None = None
    children: tuple[str, ...] = ()
    is_carousel_item: bool = False
    quote_post_id: str | None = None
    reply_to_id: str | None = None
    reply_control: str | None = None


@dataclass(frozen=True, slots=True)
class MediaContainer:
    container_id: str
    status: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteMedia:
    media_id: str
    text: str | None = None
    permalink: str | None = None
    published_at: str | None = None


@dataclass(frozen=True, slots=True)
class PublishingQuota:
    usage: int | None = None
    total: int | None = None
    reply_usage: int | None = None
    reply_total: int | None = None
    duration_seconds: int | None = None
    reply_duration_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class RemoteReply:
    reply_id: str
    text: str | None
    timestamp: str | None
    root_post_id: str | None
    replied_to_id: str | None


@dataclass(frozen=True, slots=True)
class ReplyPage:
    replies: tuple[RemoteReply, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class RemoteDiscoveryThread:
    remote_thread_id: str
    author_remote_id: str | None
    username: str | None
    text: str | None
    permalink: str | None
    media_type: str | None
    timestamp: datetime | None
    is_quote_post: bool | None
    has_replies: bool | None


@dataclass(frozen=True, slots=True)
class RemotePublicProfile:
    remote_author_id: str
    username: str
    display_name: str | None
    biography: str | None
    profile_picture_url: str | None


@dataclass(frozen=True, slots=True)
class DiscoveryPage:
    threads: tuple[RemoteDiscoveryThread, ...]
    next_cursor: str | None
    has_more: bool


class ThreadsAPIError(Exception):
    """A sanitized Threads API failure. Response bodies are never attached."""

    def __init__(self, code: str, retry_after: timedelta | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class ThreadsTransportError(ThreadsAPIError):
    def __init__(self) -> None:
        super().__init__("THREADS_TRANSPORT_FAILURE")


class ThreadsContractError(ThreadsAPIError):
    def __init__(self) -> None:
        super().__init__("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")


class ThreadsAPI(Protocol):
    async def create_container(
        self, token: SecretStr, request: MediaContainerRequest
    ) -> MediaContainer: ...

    async def get_container(self, token: SecretStr, container_id: str) -> MediaContainer: ...

    async def publish_container(self, token: SecretStr, container_id: str) -> str: ...

    async def get_media(self, token: SecretStr, media_id: str) -> RemoteMedia: ...

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota: ...

    async def get_replies(
        self, token: SecretStr, thread_id: str, after: str | None
    ) -> ReplyPage: ...

    async def get_conversation(
        self, token: SecretStr, thread_id: str, after: str | None
    ) -> ReplyPage: ...

    async def search_threads(
        self,
        token: SecretStr,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> DiscoveryPage: ...

    async def get_public_profile(self, token: SecretStr, username: str) -> RemotePublicProfile: ...

    async def get_profile_posts(
        self, token: SecretStr, username: str, *, after: str | None, limit: int
    ) -> DiscoveryPage: ...

    async def get_mentions(
        self,
        token: SecretStr,
        *,
        after: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> DiscoveryPage: ...

    async def manage_reply(self, token: SecretStr, reply_id: str, *, hide: bool) -> None: ...

    async def manage_pending_reply(
        self, token: SecretStr, reply_id: str, *, approve: bool
    ) -> None: ...


class ThreadsAccessTokenProvider(Protocol):
    async def get_access_token(self, account_id: UUID) -> SecretStr: ...
