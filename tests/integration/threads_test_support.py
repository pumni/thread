import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import SecretStr

from threads_platform.application.commands.handlers import (
    CommandExecutionContext,
    CommandExecutionOutput,
)
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.commands.threads_handlers import (
    create_threads_command_handlers,
)
from threads_platform.application.crm_protocol_v1 import CommandEnvelopeV1
from threads_platform.application.ports.threads import (
    DiscoveryPage,
    MediaContainer,
    MediaContainerRequest,
    PublishingQuota,
    RemoteMedia,
    RemotePublicProfile,
    RemoteReply,
    ReplyPage,
    ThreadsAPIError,
)
from threads_platform.application.retry import RetryPolicy
from threads_platform.domain.accounts import ThreadsAccount
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.domain.publishing import ThreadPost
from threads_platform.infrastructure.persistence.uow import SQLAlchemyUnitOfWorkFactory


class FixedClock:
    def __init__(self) -> None:
        self.current_time = datetime.now(UTC)

    def now(self) -> datetime:
        return self.current_time

    def advance(self, duration: timedelta) -> None:
        self.current_time += duration


class TokenProvider:
    async def get_access_token(self, account_id: UUID) -> SecretStr:
        assert account_id.version == 4
        return SecretStr("test-placeholder")


class FakeThreadsAPI:
    def __init__(self) -> None:
        self.created: list[MediaContainerRequest] = []
        self.container_status = "FINISHED"
        self.container_status_responses: list[str] = []
        self.container_status_calls = 0
        self.publish_calls = 0
        self.publish_entered: asyncio.Event | None = None
        self.publish_release: asyncio.Event | None = None
        self.publish_error: ThreadsAPIError | None = None
        self.create_error: ThreadsAPIError | None = None
        self.quota_error: ThreadsAPIError | None = None
        self.quota = PublishingQuota(usage=1, total=250, reply_usage=0, reply_total=1000)
        self.crash_on_status_once = False
        self.crash_on_media_once = False
        self.page_responses: list[ReplyPage] = []
        self.page_after_values: list[str | None] = []
        self.reply_pages: list[ReplyPage] = []
        self.conversation_waiters = 0
        self.conversation_ready = asyncio.Event()
        self.conversation_release: asyncio.Event | None = None
        self.conversation_error: ThreadsAPIError | None = None
        self.moderation_error: ThreadsAPIError | None = None
        self.moderation_calls: list[tuple[str, str, bool]] = []
        self.moderation_attempts = 0
        self.media_id = "published-media-doc-example"
        self.discovery_pages: list[DiscoveryPage] = []
        self.profile_post_pages: list[DiscoveryPage] = []
        self.mention_pages: list[DiscoveryPage] = []
        self.profile = RemotePublicProfile("author-doc-example", "example", "Example", None, None)
        self.discovery_after_values: list[str | None] = []
        self.discovery_error: ThreadsAPIError | None = None
        self.profile_error: ThreadsAPIError | None = None

    async def create_container(
        self, token: SecretStr, request: MediaContainerRequest
    ) -> MediaContainer:
        assert token.get_secret_value() == "test-placeholder"
        if self.create_error is not None:
            raise self.create_error
        self.created.append(request)
        return MediaContainer(f"container-doc-{len(self.created)}")

    async def get_container(self, token: SecretStr, container_id: str) -> MediaContainer:
        assert token.get_secret_value() == "test-placeholder"
        self.container_status_calls += 1
        if self.crash_on_status_once:
            self.crash_on_status_once = False
            raise SimulatedProcessCrash
        status = (
            self.container_status_responses.pop(0)
            if self.container_status_responses
            else self.container_status
        )
        return MediaContainer(container_id, status)

    async def publish_container(self, token: SecretStr, container_id: str) -> str:
        assert token.get_secret_value() == "test-placeholder"
        assert container_id.startswith("container-doc-")
        self.publish_calls += 1
        if self.publish_entered is not None:
            self.publish_entered.set()
        if self.publish_release is not None:
            await self.publish_release.wait()
        if self.publish_error is not None:
            raise self.publish_error
        return self.media_id if self.publish_calls == 1 else f"{self.media_id}-{self.publish_calls}"

    async def get_media(self, token: SecretStr, media_id: str) -> RemoteMedia:
        assert token.get_secret_value() == "test-placeholder"
        if self.crash_on_media_once:
            self.crash_on_media_once = False
            raise SimulatedProcessCrash
        return RemoteMedia(media_id, "Documentation example post", None, None)

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
        assert token.get_secret_value() == "test-placeholder"
        if self.quota_error is not None:
            raise self.quota_error
        return self.quota

    async def get_replies(self, token: SecretStr, thread_id: str, after: str | None) -> ReplyPage:
        assert thread_id.startswith("root-")
        self.page_after_values.append(after)
        return self.reply_pages.pop(0)

    async def get_conversation(
        self, token: SecretStr, thread_id: str, after: str | None
    ) -> ReplyPage:
        assert thread_id.startswith("root-")
        self.page_after_values.append(after)
        if self.conversation_error is not None:
            raise self.conversation_error
        if self.conversation_release is not None:
            self.conversation_waiters += 1
            if self.conversation_waiters == 2:
                self.conversation_ready.set()
            await self.conversation_release.wait()
        if not self.page_responses:
            return ReplyPage((), after, has_more=False)
        return self.page_responses.pop(0)

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
    ) -> DiscoveryPage:
        assert token.get_secret_value() == "test-placeholder"
        self.discovery_after_values.append(after)
        if self.discovery_error is not None:
            raise self.discovery_error
        if not self.discovery_pages:
            return DiscoveryPage((), None, False)
        return self.discovery_pages.pop(0)

    async def get_public_profile(self, token: SecretStr, username: str) -> RemotePublicProfile:
        assert token.get_secret_value() == "test-placeholder"
        if self.profile_error is not None:
            raise self.profile_error
        return self.profile

    async def get_profile_posts(
        self, token: SecretStr, username: str, *, after: str | None, limit: int
    ) -> DiscoveryPage:
        assert token.get_secret_value() == "test-placeholder"
        self.discovery_after_values.append(after)
        if self.discovery_error is not None:
            raise self.discovery_error
        if not self.profile_post_pages:
            return DiscoveryPage((), None, False)
        return self.profile_post_pages.pop(0)

    async def get_mentions(
        self,
        token: SecretStr,
        *,
        after: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> DiscoveryPage:
        assert token.get_secret_value() == "test-placeholder"
        self.discovery_after_values.append(after)
        if self.discovery_error is not None:
            raise self.discovery_error
        if not self.mention_pages:
            return DiscoveryPage((), None, False)
        return self.mention_pages.pop(0)

    async def manage_reply(self, token: SecretStr, reply_id: str, *, hide: bool) -> None:
        self.moderation_attempts += 1
        if self.moderation_error is not None:
            raise self.moderation_error
        self.moderation_calls.append(("manage_reply", reply_id, hide))

    async def manage_pending_reply(self, token: SecretStr, reply_id: str, *, approve: bool) -> None:
        self.moderation_attempts += 1
        if self.moderation_error is not None:
            raise self.moderation_error
        self.moderation_calls.append(("manage_pending_reply", reply_id, approve))


class SimulatedProcessCrash(BaseException):
    pass


class BlockingHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> CommandExecutionOutput:
        assert command.command_id
        assert context.attempt_number > 0
        self.started.set()
        await self.release.wait()
        return CommandExecutionOutput(result={"done": True})


def make_runtime(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
    api: FakeThreadsAPI,
    clock: FixedClock,
    *,
    max_attempts: int = 3,
    lease_duration: timedelta = timedelta(seconds=6),
) -> CommandRuntime:
    return CommandRuntime(
        unit_of_work_factory,
        create_threads_command_handlers(api, TokenProvider(), unit_of_work_factory),
        clock=clock,
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            base_delay=timedelta(seconds=1),
            max_delay=timedelta(seconds=4),
            jitter_ratio=0,
            jitter_source=lambda lower, _: lower,
        ),
        execution_lease_duration=lease_duration,
    )


def command_body(
    account_id: UUID,
    clock: FixedClock,
    command_type: str = "threads.publish_text",
    payload: dict[str, object] | None = None,
    *,
    command_id: str | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "command_id": command_id or f"cmd-{uuid4()}",
        "correlation_id": f"corr-{uuid4()}",
        "account_id": str(account_id),
        "created_at": clock.now().isoformat(),
        "command_type": command_type,
        "payload": payload or {"text": "Documentation example post"},
    }


async def seed_account_and_post(
    unit_of_work_factory: SQLAlchemyUnitOfWorkFactory,
) -> tuple[UUID, ThreadPost]:
    account = ThreadsAccount(threads_user_id=f"synthetic-user-{uuid4()}", username="fixture")
    post = ThreadPost(account_id=account.id, threads_post_id=f"root-{uuid4()}")
    async with unit_of_work_factory() as unit_of_work:
        await unit_of_work.accounts.add(account)
        await unit_of_work.posts.add(post)
    return account.id, post


def remote_reply(
    reply_id: str,
    root_id: str,
    replied_to_id: str,
    text: str = "documentation example reply",
) -> RemoteReply:
    return RemoteReply(reply_id, text, "2026-01-01T00:00:00+00:00", root_id, replied_to_id)
