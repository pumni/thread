from typing import Protocol
from uuid import UUID

from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.commands import Command
from threads_platform.domain.publishing import ThreadPost, ThreadReply


class AccountRepository(Protocol):
    async def add(self, account: ThreadsAccount) -> None: ...

    async def get(self, account_id: UUID) -> ThreadsAccount | None: ...

    async def update(self, account: ThreadsAccount) -> None: ...


class CommandRepository(Protocol):
    async def add(self, command: Command) -> None: ...

    async def get_by_command_id(self, command_id: str) -> Command | None: ...

    async def update(self, command: Command) -> None: ...


class PostRepository(Protocol):
    async def add(self, post: ThreadPost) -> None: ...

    async def get_by_external_id(
        self, account_id: UUID, threads_post_id: str
    ) -> ThreadPost | None: ...


class ReplyRepository(Protocol):
    async def add(self, reply: ThreadReply) -> None: ...

    async def get_by_external_id(
        self, account_id: UUID, threads_reply_id: str
    ) -> ThreadReply | None: ...
