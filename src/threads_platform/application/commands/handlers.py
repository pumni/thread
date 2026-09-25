from dataclasses import dataclass
from typing import Protocol

from threads_platform.application.crm_protocol_v1 import CommandEnvelopeV1
from threads_platform.application.errors import ExecutionLeaseLost
from threads_platform.domain.publishing import ThreadPost, ThreadReply


class CheckpointWriter(Protocol):
    async def __call__(self, data: dict[str, object]) -> bool: ...


@dataclass(slots=True)
class CommandCheckpoint:
    data: dict[str, object]
    _write: CheckpointWriter

    async def save(self, data: dict[str, object]) -> None:
        if not await self._write(data):
            raise ExecutionLeaseLost("command execution lease is no longer active")
        self.data = data


@dataclass(frozen=True, slots=True)
class CommandExecutionContext:
    attempt_number: int
    checkpoint: CommandCheckpoint


@dataclass(frozen=True, slots=True)
class CommandExecutionOutput:
    result: dict[str, object]
    posts: tuple[ThreadPost, ...] = ()
    replies: tuple[ThreadReply, ...] = ()


class CommandHandler(Protocol):
    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> CommandExecutionOutput: ...
