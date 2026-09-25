import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from threads_platform.domain.commands import CommandStatus

_WINDOWS_DEVICE_NAME_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class CommandEnvelopeHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = Field(ge=1)
    command_id: str = Field(min_length=1, max_length=255)
    correlation_id: str = Field(min_length=1, max_length=255)
    account_id: UUID
    created_at: AwareDatetime
    deadline_at: AwareDatetime | None = None
    command_type: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class PublishTextPostPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    quote_post_id: str | None = Field(default=None, min_length=1, max_length=255)
    reply_control: (
        Literal[
            "everyone",
            "accounts_you_follow",
            "mentioned_only",
            "parent_post_author_only",
            "followers_only",
        ]
        | None
    ) = None

    @field_validator("text")
    @classmethod
    def text_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be empty")
        return value


class CreateReplyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threads_post_id: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=500)
    reply_to_reply_id: str | None = Field(default=None, min_length=1, max_length=255)
    reply_control: (
        Literal[
            "everyone",
            "accounts_you_follow",
            "mentioned_only",
            "parent_post_author_only",
            "followers_only",
        ]
        | None
    ) = None

    @field_validator("threads_post_id", "text")
    @classmethod
    def values_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


class PublishImagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_url: str = Field(min_length=1, max_length=2048)
    text: str | None = Field(default=None, max_length=500)
    alt_text: str | None = Field(default=None, max_length=1000)
    reply_control: (
        Literal[
            "everyone",
            "accounts_you_follow",
            "mentioned_only",
            "parent_post_author_only",
            "followers_only",
        ]
        | None
    ) = None

    @field_validator("image_url")
    @classmethod
    def image_url_must_be_public_http_url(cls, value: str) -> str:
        _validate_media_url(value)
        return value


class PublishVideoPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_url: str = Field(min_length=1, max_length=2048)
    text: str | None = Field(default=None, max_length=500)
    alt_text: str | None = Field(default=None, max_length=1000)
    reply_control: (
        Literal[
            "everyone",
            "accounts_you_follow",
            "mentioned_only",
            "parent_post_author_only",
            "followers_only",
        ]
        | None
    ) = None

    @field_validator("video_url")
    @classmethod
    def video_url_must_be_public_http_url(cls, value: str) -> str:
        _validate_media_url(value)
        return value


class CarouselImageItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: Literal["IMAGE"]
    url: str = Field(min_length=1, max_length=2048)
    alt_text: str | None = Field(default=None, max_length=1000)

    @field_validator("url")
    @classmethod
    def image_url_must_be_public_http_url(cls, value: str) -> str:
        _validate_media_url(value)
        return value


class CarouselVideoItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: Literal["VIDEO"]
    url: str = Field(min_length=1, max_length=2048)
    alt_text: str | None = Field(default=None, max_length=1000)

    @field_validator("url")
    @classmethod
    def video_url_must_be_public_http_url(cls, value: str) -> str:
        _validate_media_url(value)
        return value


CarouselItem = Annotated[
    CarouselImageItem | CarouselVideoItem,
    Field(discriminator="media_type"),
]


class PublishCarouselPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CarouselItem] = Field(min_length=2, max_length=20)
    text: str | None = Field(default=None, max_length=500)
    reply_control: (
        Literal[
            "everyone",
            "accounts_you_follow",
            "mentioned_only",
            "parent_post_author_only",
            "followers_only",
        ]
        | None
    ) = None


class SyncConversationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threads_post_id: str = Field(min_length=1, max_length=255)
    sync_kind: Literal["replies", "conversation"] = "conversation"


class CreateDiscoveryCampaignPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def name_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("campaign name must not be empty")
        return value


class CompleteDiscoveryCampaignPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: UUID


class DiscoverySearchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: UUID
    query: str = Field(min_length=1, max_length=255)
    search_mode: Literal["KEYWORD", "TAG"]
    search_type: Literal["TOP", "RECENT"]
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    max_pages: int = Field(default=3, ge=1, le=5)

    @field_validator("query")
    @classmethod
    def query_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("discovery query must not be empty")
        return value


class DiscoveryProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: UUID
    username: str = Field(min_length=1, max_length=255)
    max_pages: int = Field(default=3, ge=1, le=5)

    @field_validator("username")
    @classmethod
    def username_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip() or value.startswith("@"):
            raise ValueError("username must be an exact handle without @")
        return value


class DiscoveryMentionsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: UUID
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    max_pages: int = Field(default=3, ge=1, le=5)


class DiscoveryConversationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: UUID
    thread_remote_id: str = Field(min_length=1, max_length=255)
    max_pages: int = Field(default=3, ge=1, le=5)


class DiscoveryResumePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    max_pages: int = Field(default=3, ge=1, le=5)


class LeadCandidateStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    target_status: Literal["CANDIDATE", "READY", "DISMISSED"]
    reason_code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$")


class ModerateReplyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threads_reply_id: str = Field(min_length=1, max_length=255)
    action: Literal["hide", "unhide", "approve", "ignore"]


class CRMCommandV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    command_id: str = Field(min_length=1, max_length=255)
    correlation_id: str = Field(min_length=1, max_length=255)
    account_id: UUID
    created_at: AwareDatetime
    deadline_at: AwareDatetime | None = None


class PublishTextPostCommandV1(CRMCommandV1):
    command_type: Literal["threads.publish_text"]
    payload: PublishTextPostPayload


class CreateReplyCommandV1(CRMCommandV1):
    command_type: Literal["threads.create_reply"]
    payload: CreateReplyPayload


class PublishImageCommandV1(CRMCommandV1):
    command_type: Literal["threads.publish_image"]
    payload: PublishImagePayload


class PublishVideoCommandV1(CRMCommandV1):
    command_type: Literal["threads.publish_video"]
    payload: PublishVideoPayload


class PublishCarouselCommandV1(CRMCommandV1):
    command_type: Literal["threads.publish_carousel"]
    payload: PublishCarouselPayload


class SyncConversationCommandV1(CRMCommandV1):
    command_type: Literal["threads.sync_conversation"]
    payload: SyncConversationPayload


class CreateDiscoveryCampaignCommandV1(CRMCommandV1):
    command_type: Literal["threads.discovery.create_campaign"]
    payload: CreateDiscoveryCampaignPayload


class CompleteDiscoveryCampaignCommandV1(CRMCommandV1):
    command_type: Literal["threads.discovery.complete_campaign"]
    payload: CompleteDiscoveryCampaignPayload


class DiscoverySearchCommandV1(CRMCommandV1):
    command_type: Literal["threads.discovery.search"]
    payload: DiscoverySearchPayload


class DiscoveryProfileCommandV1(CRMCommandV1):
    command_type: Literal["threads.discovery.profile"]
    payload: DiscoveryProfilePayload


class DiscoveryMentionsCommandV1(CRMCommandV1):
    command_type: Literal["threads.discovery.mentions"]
    payload: DiscoveryMentionsPayload


class DiscoveryConversationCommandV1(CRMCommandV1):
    command_type: Literal["threads.discovery.conversation"]
    payload: DiscoveryConversationPayload


class DiscoveryResumeCommandV1(CRMCommandV1):
    command_type: Literal["threads.discovery.resume"]
    payload: DiscoveryResumePayload


class LeadCandidateStatusCommandV1(CRMCommandV1):
    command_type: Literal["threads.discovery.lead_status"]
    payload: LeadCandidateStatusPayload


class ModerateReplyCommandV1(CRMCommandV1):
    command_type: Literal["threads.moderate_reply"]
    payload: ModerateReplyPayload


class BrowserFeedBrowsePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_items: int = Field(default=10, ge=1, le=20)


class BrowserThreadOpenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_ref: str = Field(min_length=1, max_length=255)

    @field_validator("thread_ref")
    @classmethod
    def thread_ref_must_be_an_opaque_identifier(cls, value: str) -> str:
        return _validate_browser_identifier(value)


class BrowserProfileOpenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_ref: str = Field(min_length=1, max_length=255)

    @field_validator("profile_ref")
    @classmethod
    def profile_ref_must_be_an_opaque_identifier(cls, value: str) -> str:
        return _validate_browser_identifier(value)


class BrowserLocalUploadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_ref: str = Field(min_length=1, max_length=120)

    @field_validator("media_ref")
    @classmethod
    def media_ref_must_be_a_logical_file_reference(cls, value: str) -> str:
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", value) is None
            or value in {".", ".."}
            or value.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAME_STEMS
        ):
            raise ValueError("media_ref must be a simple worker-local file name")
        return value


class BrowserFeedBrowseCommandV1(CRMCommandV1):
    command_type: Literal["threads.browser.feed.browse"]
    payload: BrowserFeedBrowsePayload


class BrowserThreadOpenCommandV1(CRMCommandV1):
    command_type: Literal["threads.browser.thread.open"]
    payload: BrowserThreadOpenPayload


class BrowserProfileOpenCommandV1(CRMCommandV1):
    command_type: Literal["threads.browser.profile.open"]
    payload: BrowserProfileOpenPayload


class BrowserLocalUploadCommandV1(CRMCommandV1):
    command_type: Literal["threads.browser.media.local_upload"]
    payload: BrowserLocalUploadPayload


type CommandEnvelopeV1 = Annotated[
    PublishTextPostCommandV1
    | CreateReplyCommandV1
    | PublishImageCommandV1
    | PublishVideoCommandV1
    | PublishCarouselCommandV1
    | SyncConversationCommandV1
    | CreateDiscoveryCampaignCommandV1
    | CompleteDiscoveryCampaignCommandV1
    | DiscoverySearchCommandV1
    | DiscoveryProfileCommandV1
    | DiscoveryMentionsCommandV1
    | DiscoveryConversationCommandV1
    | DiscoveryResumeCommandV1
    | LeadCandidateStatusCommandV1
    | ModerateReplyCommandV1
    | BrowserFeedBrowseCommandV1
    | BrowserThreadOpenCommandV1
    | BrowserProfileOpenCommandV1
    | BrowserLocalUploadCommandV1,
    Field(discriminator="command_type"),
]
COMMAND_ENVELOPE_ADAPTER: TypeAdapter[CommandEnvelopeV1] = TypeAdapter(CommandEnvelopeV1)


def _validate_media_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("media URL must be an HTTP(S) URL without embedded credentials")


def _validate_browser_identifier(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", value) is None:
        raise ValueError("browser target must be a bounded opaque identifier")
    return value


class CommandReceiptV1(BaseModel):
    protocol_version: Literal[1] = 1
    command_id: str
    correlation_id: str
    status: CommandStatus
    duplicate: bool = False


class CRMErrorV1(BaseModel):
    code: str
    retryable: bool


class CRMCommandResultV1(BaseModel):
    protocol_version: Literal[1] = 1
    event_id: UUID
    command_id: str
    correlation_id: str
    command_type: str
    status: CommandStatus
    completed_at: AwareDatetime
    result: dict[str, Any] | None = None
    error: CRMErrorV1 | None = None
