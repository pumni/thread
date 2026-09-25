from typing import Annotated, Any, Literal
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

    @field_validator("threads_post_id", "text")
    @classmethod
    def values_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


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


type CommandEnvelopeV1 = Annotated[
    PublishTextPostCommandV1 | CreateReplyCommandV1,
    Field(discriminator="command_type"),
]
COMMAND_ENVELOPE_ADAPTER: TypeAdapter[CommandEnvelopeV1] = TypeAdapter(CommandEnvelopeV1)


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
