from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID, uuid4

from pydantic import SecretStr

from threads_platform.application.commands.handlers import (
    CommandExecutionContext,
    CommandExecutionOutput,
    SyncStateUpdate,
)
from threads_platform.application.crm_protocol_v1 import CommandEnvelopeV1
from threads_platform.application.errors import PermanentCommandError, RetryableCommandError
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.ports.threads import (
    RemoteReply,
    ThreadsAccessTokenProvider,
    ThreadsAPI,
    ThreadsAPIError,
)
from threads_platform.domain.publishing import ThreadPost, ThreadReply
from threads_platform.domain.sync import SyncState

_RETRYABLE_API_CODES = frozenset(
    {"THREADS_TRANSPORT_FAILURE", "THREADS_RATE_LIMITED", "THREADS_SERVER_ERROR"}
)
_MAX_SYNC_PAGES = 500


class ThreadsConversationHandler:
    def __init__(
        self,
        api: ThreadsAPI,
        token_provider: ThreadsAccessTokenProvider,
        unit_of_work_factory: UnitOfWorkFactory,
    ) -> None:
        self._api = api
        self._token_provider = token_provider
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self, command: CommandEnvelopeV1, context: CommandExecutionContext
    ) -> CommandExecutionOutput:
        if command.command_type not in {
            "threads.sync_conversation",
            "threads.moderate_reply",
        }:
            raise PermanentCommandError("UNKNOWN_COMMAND")
        token = await self._token_provider.get_access_token(command.account_id)
        if command.command_type == "threads.sync_conversation":
            return await self._sync_conversation(command, token)
        return await self._moderate_reply(command, context, token)

    async def _sync_conversation(
        self, command: CommandEnvelopeV1, token: SecretStr
    ) -> CommandExecutionOutput:
        payload = command.payload.model_dump(mode="python")
        threads_post_id = str(payload["threads_post_id"])
        sync_kind = str(payload.get("sync_kind", "conversation"))
        sync_type = f"{sync_kind}:{threads_post_id}"
        async with self._unit_of_work_factory() as unit_of_work:
            root_post = await unit_of_work.posts.get_by_external_id(
                command.account_id, threads_post_id
            )
            if root_post is None:
                raise PermanentCommandError("LOCAL_ROOT_POST_NOT_FOUND")
            state = await unit_of_work.sync_states.get(command.account_id, sync_type)
            known_replies = await unit_of_work.replies.list_for_root(
                command.account_id, root_post.id
            )
        cursor_from = state.cursor if state else None
        cursor = cursor_from
        remote_replies: dict[str, RemoteReply] = {}
        page_count = 0
        while True:
            if page_count >= _MAX_SYNC_PAGES:
                raise RetryableCommandError("THREADS_CONVERSATION_PAGE_LIMIT")
            try:
                get_page = (
                    self._api.get_replies if sync_kind == "replies" else self._api.get_conversation
                )
                page = await get_page(token, threads_post_id, cursor)
            except ThreadsAPIError as error:
                if error.code == "THREADS_OBJECT_NOT_FOUND":
                    return CommandExecutionOutput(
                        result={
                            "status": "remote_root_missing",
                            "replies_synced": 0,
                            "cursor": cursor_from,
                        }
                    )
                self._raise_api_error(error)
            page_count += 1
            for reply in page.replies:
                remote_replies.setdefault(reply.reply_id, reply)
            if page.next_cursor is not None:
                if page.has_more and page.next_cursor == cursor:
                    raise RetryableCommandError("THREADS_CONVERSATION_CURSOR_STALLED")
                cursor = page.next_cursor
            if not page.has_more:
                break
            if page.next_cursor is None:
                raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")

        existing_by_external_id = {reply.threads_reply_id: reply for reply in known_replies}
        new_replies, skipped_missing_parent = self.map_replies(
            command.account_id,
            root_post,
            remote_replies,
            existing_by_external_id,
        )
        completed_at = datetime.now(UTC)
        new_state = SyncState(
            account_id=command.account_id,
            sync_type=sync_type,
            id=state.id if state else uuid4(),
            cursor=cursor,
            last_synced_at=completed_at,
            updated_at=completed_at,
        )
        return CommandExecutionOutput(
            result={
                "replies_synced": len(new_replies),
                "duplicates_ignored": (
                    len(remote_replies) - len(new_replies) - skipped_missing_parent
                ),
                "missing_parent_skipped": skipped_missing_parent,
                "pages_read": page_count,
                "cursor": cursor,
            },
            replies=tuple(new_replies),
            sync_states=(SyncStateUpdate(new_state, cursor_from),),
        )

    async def _moderate_reply(
        self,
        command: CommandEnvelopeV1,
        context: CommandExecutionContext,
        token: SecretStr,
    ) -> CommandExecutionOutput:
        payload = command.payload.model_dump(mode="python")
        reply_id = str(payload["threads_reply_id"])
        action = str(payload["action"])
        if action in {"hide", "unhide"}:
            async with self._unit_of_work_factory() as unit_of_work:
                reply = await unit_of_work.replies.get_by_external_id(command.account_id, reply_id)
                if reply is None:
                    raise PermanentCommandError("LOCAL_REPLY_NOT_FOUND")
                if reply.parent_reply_id is not None:
                    raise PermanentCommandError("NESTED_REPLY_MODERATION_UNSUPPORTED")
        if context.checkpoint.data.get("phase") == "MODERATION_REQUESTED":
            raise RetryableCommandError("THREADS_MODERATION_OUTCOME_AMBIGUOUS")
        await context.checkpoint.save({"phase": "MODERATION_REQUESTED", "action": action})
        try:
            if action in {"hide", "unhide"}:
                await self._api.manage_reply(token, reply_id, hide=action == "hide")
            else:
                await self._api.manage_pending_reply(token, reply_id, approve=action == "approve")
        except ThreadsAPIError as error:
            self._raise_api_error(error)
        return CommandExecutionOutput(
            result={"reply_id": reply_id, "action": action, "status": "confirmed"}
        )

    @staticmethod
    def map_replies(
        account_id: UUID,
        root_post: ThreadPost | None,
        remote_replies: dict[str, RemoteReply],
        existing: dict[str, ThreadReply],
        *,
        root_remote_id: str | None = None,
        discovered_thread_id: UUID | None = None,
    ) -> tuple[list[ThreadReply], int]:
        root_id = root_post.threads_post_id if root_post is not None else root_remote_id
        if root_id is None or (root_post is None) != (discovered_thread_id is not None):
            raise ValueError("conversation mapping requires exactly one local root")
        internal_by_external_id = {key: value.id for key, value in existing.items()}
        pending = dict(remote_replies)
        new_replies: list[ThreadReply] = []
        skipped_missing_parent = 0
        while pending:
            progressed = False
            for external_id, remote in tuple(pending.items()):
                if remote.root_post_id != root_id or remote.replied_to_id is None:
                    pending.pop(external_id)
                    skipped_missing_parent += 1
                    progressed = True
                    continue
                if remote.replied_to_id == root_id:
                    parent_id = None
                else:
                    parent_id = internal_by_external_id.get(remote.replied_to_id)
                    if parent_id is None:
                        continue
                if external_id not in internal_by_external_id:
                    reply = ThreadReply(
                        account_id=account_id,
                        threads_reply_id=external_id,
                        root_post_id=root_post.id if root_post is not None else None,
                        discovered_thread_id=discovered_thread_id,
                        parent_reply_id=parent_id,
                        text=remote.text,
                        replied_at=ThreadsConversationHandler._parse_timestamp(remote.timestamp),
                    )
                    new_replies.append(reply)
                    internal_by_external_id[external_id] = reply.id
                pending.pop(external_id)
                progressed = True
            if not progressed:
                skipped_missing_parent += len(pending)
                break
        return new_replies, skipped_missing_parent

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH") from error
        if parsed.tzinfo is None:
            raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
        return parsed

    @staticmethod
    def _raise_api_error(error: ThreadsAPIError) -> NoReturn:
        if error.code in _RETRYABLE_API_CODES:
            raise RetryableCommandError(error.code, error.retry_after) from error
        raise PermanentCommandError(error.code) from error
