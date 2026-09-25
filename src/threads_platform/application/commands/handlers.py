from dataclasses import dataclass
from typing import Protocol

from threads_platform.application.crm_protocol_v1 import CommandEnvelopeV1
from threads_platform.application.ports.repositories import PostRepository, ReplyRepository


@dataclass(frozen=True, slots=True)
class CommandExecutionContext:
    posts: PostRepository
    replies: ReplyRepository


class CommandHandler(Protocol):
    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> dict[str, object]: ...
