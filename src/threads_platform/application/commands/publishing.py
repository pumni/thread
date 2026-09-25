from datetime import datetime
from typing import Any, NoReturn, cast

from pydantic import SecretStr

from threads_platform.application.commands.handlers import (
    CommandExecutionContext,
    CommandExecutionOutput,
)
from threads_platform.application.crm_protocol_v1 import CommandEnvelopeV1
from threads_platform.application.errors import PermanentCommandError, RetryableCommandError
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.ports.threads import (
    MediaContainer,
    MediaContainerRequest,
    MediaType,
    PublishingQuota,
    RemoteMedia,
    ThreadsAccessTokenProvider,
    ThreadsAPI,
    ThreadsAPIError,
)
from threads_platform.domain.publishing import ThreadPost, ThreadReply

_RETRYABLE_API_CODES = frozenset(
    {"THREADS_TRANSPORT_FAILURE", "THREADS_RATE_LIMITED", "THREADS_SERVER_ERROR"}
)
_PUBLISH_CHECKPOINT_VERSION = 1


class ThreadsPublishingHandler:
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
            "threads.publish_text",
            "threads.publish_image",
            "threads.publish_video",
            "threads.publish_carousel",
            "threads.create_reply",
        }:
            raise PermanentCommandError("UNKNOWN_COMMAND")
        token = await self._token_provider.get_access_token(command.account_id)
        return await self._publish(command, context, token)

    async def _publish(
        self,
        command: CommandEnvelopeV1,
        context: CommandExecutionContext,
        token: SecretStr,
    ) -> CommandExecutionOutput:
        payload = command.payload.model_dump(mode="python")
        reply_to_id: str | None = None
        parent_reply: ThreadReply | None = None
        root_post: ThreadPost | None = None
        if command.command_type == "threads.create_reply":
            root_post_id = payload["threads_post_id"]
            async with self._unit_of_work_factory() as unit_of_work:
                root_post = await unit_of_work.posts.get_by_external_id(
                    command.account_id, str(root_post_id)
                )
                if root_post is None:
                    raise PermanentCommandError("LOCAL_ROOT_POST_NOT_FOUND")
                reply_to_id = payload.get("reply_to_reply_id") or str(root_post_id)
                if reply_to_id != root_post_id:
                    parent_reply = await unit_of_work.replies.get_by_external_id(
                        command.account_id, str(reply_to_id)
                    )
                    if parent_reply is None:
                        raise PermanentCommandError("LOCAL_PARENT_REPLY_NOT_FOUND")

        media_type, text, containers = self._media_requests(command, payload, reply_to_id)
        recovery = self._read_recovery(context)
        is_reply = command.command_type == "threads.create_reply"
        quota_verified = False
        if not isinstance(recovery.get("container_id"), str) and not isinstance(
            recovery.get("published_media_id"), str
        ):
            quota = await self._get_quota(token, is_reply=is_reply)
            await self._check_quota(quota, is_reply=is_reply)
            quota_verified = quota.usage is not None and quota.total is not None

        child_ids = list(recovery.get("child_container_ids", []))
        if media_type == "CAROUSEL":
            if len(child_ids) > len(containers):
                raise PermanentCommandError("INVALID_PUBLISH_CHECKPOINT")
            for index, request in enumerate(containers):
                if index < len(child_ids):
                    continue
                container = await self._create_container(token, request)
                child_ids.append(container.container_id)
                recovery["child_container_ids"] = child_ids
                recovery["phase"] = "CHILD_CONTAINERS_CREATED"
                await self._save_recovery(context, recovery)
            container_id = recovery.get("container_id")
            if not isinstance(container_id, str):
                parent_request = MediaContainerRequest(
                    media_type="CAROUSEL",
                    text=text,
                    children=tuple(child_ids),
                    reply_control=payload.get("reply_control"),
                )
                parent = await self._create_container(token, parent_request)
                recovery["container_id"] = parent.container_id
                recovery["phase"] = "CONTAINER_CREATED"
                await self._save_recovery(context, recovery)
                container_id = parent.container_id
        else:
            container_id = recovery.get("container_id")
            if not isinstance(container_id, str):
                container = await self._create_container(token, containers[0])
                recovery["container_id"] = container.container_id
                recovery["phase"] = "CONTAINER_CREATED"
                await self._save_recovery(context, recovery)
                container_id = container.container_id

        remote_media = await self._publish_or_reconcile(
            token, container_id, recovery, context, is_reply=is_reply
        )
        published_id = recovery.get("published_media_id")
        if not isinstance(published_id, str):
            raise RetryableCommandError("THREADS_PUBLISH_OUTCOME_AMBIGUOUS")

        published_at = self._parse_timestamp(remote_media.published_at)
        if command.command_type == "threads.create_reply":
            if root_post is None:
                raise PermanentCommandError("LOCAL_ROOT_POST_NOT_FOUND")
            reply = ThreadReply(
                account_id=command.account_id,
                threads_reply_id=remote_media.media_id,
                root_post_id=root_post.id,
                parent_reply_id=parent_reply.id if parent_reply is not None else None,
                text=remote_media.text or text,
                replied_at=published_at,
            )
            return CommandExecutionOutput(
                result={
                    "reply_id": remote_media.media_id,
                    "root_post_id": root_post.threads_post_id,
                    "parent_reply_id": parent_reply.threads_reply_id if parent_reply else None,
                    "container_id": container_id,
                },
                replies=(reply,),
            )

        post = ThreadPost(
            account_id=command.account_id,
            threads_post_id=remote_media.media_id,
            text=remote_media.text if remote_media.text is not None else text,
            permalink=remote_media.permalink,
            published_at=published_at,
            metadata={"media_type": media_type},
        )
        return CommandExecutionOutput(
            result={
                "media_id": remote_media.media_id,
                "container_id": container_id,
                "quota_verified": quota_verified,
            },
            posts=(post,),
        )

    async def _publish_or_reconcile(
        self,
        token: SecretStr,
        container_id: str,
        recovery: dict[str, Any],
        context: CommandExecutionContext,
        *,
        is_reply: bool,
    ) -> RemoteMedia:
        published_media_id = recovery.get("published_media_id")
        if isinstance(published_media_id, str):
            return await self._get_media(token, published_media_id)

        status = await self._get_container(token, container_id)
        state = status.status
        if state == "PUBLISHED":
            # The documentation status response exposes the container id, not the published
            # media id. Never infer equality or repeat publish when the outcome is ambiguous.
            raise RetryableCommandError("THREADS_PUBLISH_OUTCOME_AMBIGUOUS")
        if state == "IN_PROGRESS":
            raise RetryableCommandError("THREADS_CONTAINER_PROCESSING")
        if state == "ERROR":
            raise PermanentCommandError("THREADS_CONTAINER_ERROR")
        if state == "EXPIRED":
            raise PermanentCommandError("THREADS_CONTAINER_EXPIRED")
        if state != "FINISHED":
            raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")

        quota = await self._get_quota(token, is_reply=is_reply)
        await self._check_quota(quota, is_reply=is_reply)

        recovery["phase"] = "PUBLISH_REQUESTED"
        await self._save_recovery(context, recovery)
        try:
            media_id = await self._api.publish_container(token, container_id)
        except ThreadsAPIError as error:
            return await self._reconcile_publish_error(token, container_id, error)

        recovery["published_media_id"] = media_id
        recovery["phase"] = "PUBLISHED"
        await self._save_recovery(context, recovery)
        return await self._get_media(token, media_id)

    async def _reconcile_publish_error(
        self,
        token: SecretStr,
        container_id: str,
        publish_error: ThreadsAPIError,
    ) -> RemoteMedia:
        try:
            status = await self._get_container(token, container_id)
        except ThreadsAPIError as status_error:
            if publish_error.code in {
                "THREADS_AUTHENTICATION_FAILED",
                "THREADS_PERMISSION_DENIED",
                "THREADS_INVALID_REQUEST",
            }:
                self._raise_api_error(publish_error)
            self._raise_api_error(status_error)
        if status.status == "PUBLISHED":
            raise RetryableCommandError("THREADS_PUBLISH_OUTCOME_AMBIGUOUS")
        if status.status == "IN_PROGRESS":
            raise RetryableCommandError("THREADS_CONTAINER_PROCESSING")
        if status.status == "ERROR":
            raise PermanentCommandError("THREADS_CONTAINER_ERROR")
        if status.status == "EXPIRED":
            raise PermanentCommandError("THREADS_CONTAINER_EXPIRED")
        if status.status == "FINISHED":
            self._raise_api_error(publish_error)
        raise PermanentCommandError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")

    def _media_requests(
        self,
        command: CommandEnvelopeV1,
        payload: dict[str, Any],
        reply_to_id: str | None,
    ) -> tuple[str, str | None, tuple[MediaContainerRequest, ...]]:
        control = payload.get("reply_control")
        if command.command_type in {"threads.publish_text", "threads.create_reply"}:
            text = str(payload["text"])
            return (
                "TEXT",
                text,
                (
                    MediaContainerRequest(
                        media_type="TEXT",
                        text=text,
                        quote_post_id=payload.get("quote_post_id"),
                        reply_to_id=reply_to_id,
                        reply_control=control,
                    ),
                ),
            )
        if command.command_type == "threads.publish_image":
            return (
                "IMAGE",
                payload.get("text"),
                (
                    MediaContainerRequest(
                        media_type="IMAGE",
                        image_url=str(payload["image_url"]),
                        text=payload.get("text"),
                        alt_text=payload.get("alt_text"),
                        reply_control=control,
                    ),
                ),
            )
        if command.command_type == "threads.publish_video":
            return (
                "VIDEO",
                payload.get("text"),
                (
                    MediaContainerRequest(
                        media_type="VIDEO",
                        video_url=str(payload["video_url"]),
                        text=payload.get("text"),
                        alt_text=payload.get("alt_text"),
                        reply_control=control,
                    ),
                ),
            )
        if command.command_type == "threads.publish_carousel":
            items = cast(list[dict[str, object]], payload["items"])
            requests_list: list[MediaContainerRequest] = []
            for item in items:
                item_type = item.get("media_type")
                item_url = item.get("url")
                alt_text = item.get("alt_text")
                if (
                    item_type not in {"IMAGE", "VIDEO"}
                    or not isinstance(item_url, str)
                    or (alt_text is not None and not isinstance(alt_text, str))
                ):
                    raise PermanentCommandError("INVALID_CAROUSEL_ITEM")
                media_type = cast(MediaType, item_type)
                requests_list.append(
                    MediaContainerRequest(
                        media_type=media_type,
                        image_url=item_url if media_type == "IMAGE" else None,
                        video_url=item_url if media_type == "VIDEO" else None,
                        alt_text=alt_text,
                        is_carousel_item=True,
                    )
                )
            requests = tuple(requests_list)
            return "CAROUSEL", payload.get("text"), requests
        raise PermanentCommandError("UNKNOWN_COMMAND")

    async def _get_quota(self, token: SecretStr, *, is_reply: bool) -> PublishingQuota:
        try:
            return await self._api.get_publishing_quota(token)
        except ThreadsAPIError as error:
            if error.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH":
                # Quota's exact live response remains a TP-002 verification item.
                return PublishingQuota()
            self._raise_api_error(error)

    @staticmethod
    async def _check_quota(quota: PublishingQuota, *, is_reply: bool) -> None:
        usage = quota.reply_usage if is_reply else quota.usage
        total = quota.reply_total if is_reply else quota.total
        if usage is not None and total is not None and usage >= total:
            raise PermanentCommandError("THREADS_PUBLISHING_QUOTA_REACHED")

    async def _create_container(
        self, token: SecretStr, request: MediaContainerRequest
    ) -> MediaContainer:
        try:
            return await self._api.create_container(token, request)
        except ThreadsAPIError as error:
            self._raise_api_error(error)

    async def _get_container(self, token: SecretStr, container_id: str) -> MediaContainer:
        try:
            return await self._api.get_container(token, container_id)
        except ThreadsAPIError as error:
            self._raise_api_error(error)

    async def _get_media(self, token: SecretStr, media_id: str) -> RemoteMedia:
        try:
            return await self._api.get_media(token, media_id)
        except ThreadsAPIError as error:
            self._raise_api_error(error)

    async def _save_recovery(
        self, context: CommandExecutionContext, recovery: dict[str, Any]
    ) -> None:
        recovery["version"] = _PUBLISH_CHECKPOINT_VERSION
        await context.checkpoint.save(dict(recovery))

    @staticmethod
    def _read_recovery(context: CommandExecutionContext) -> dict[str, Any]:
        recovery = dict(context.checkpoint.data)
        if not recovery:
            return {"version": _PUBLISH_CHECKPOINT_VERSION, "phase": "RECEIVED"}
        if recovery.get("version") != _PUBLISH_CHECKPOINT_VERSION:
            raise PermanentCommandError("INVALID_PUBLISH_CHECKPOINT")
        children = recovery.get("child_container_ids", [])
        if not isinstance(children, list) or any(
            not isinstance(item, str) for item in cast(list[object], children)
        ):
            raise PermanentCommandError("INVALID_PUBLISH_CHECKPOINT")
        for key in ("container_id", "published_media_id"):
            value = recovery.get(key)
            if value is not None and not isinstance(value, str):
                raise PermanentCommandError("INVALID_PUBLISH_CHECKPOINT")
        return recovery

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
