from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from threads_platform.domain.commands import CommandStatus


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


class ModerateReplyCommandV1(CRMCommandV1):
    command_type: Literal["threads.moderate_reply"]
    payload: ModerateReplyPayload


type CommandEnvelopeV1 = Annotated[
    PublishTextPostCommandV1
    | CreateReplyCommandV1
    | PublishImageCommandV1
    | PublishVideoCommandV1
    | PublishCarouselCommandV1
    | SyncConversationCommandV1
    | ModerateReplyCommandV1,
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
