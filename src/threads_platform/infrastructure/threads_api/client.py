from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr, TypeAdapter, ValidationError

from threads_platform.application.ports.threads import (
    DiscoveryPage,
    MediaContainer,
    MediaContainerRequest,
    PublishingQuota,
    RemoteDiscoveryThread,
    RemoteMedia,
    RemotePublicProfile,
    RemoteReply,
    ReplyPage,
    ThreadsAPIError,
    ThreadsContractError,
    ThreadsTransportError,
)
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType

_OBJECT_ADAPTER = TypeAdapter(dict[str, object])
_OBJECT_LIST_ADAPTER = TypeAdapter(list[object])


class HttpThreadsAPI:
    """HTTP adapter for the Meta-owned Threads API contract."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def create_container(
        self, token: SecretStr, request: MediaContainerRequest
    ) -> MediaContainer:
        data: dict[str, str] = {"media_type": request.media_type}
        optional = {
            "text": request.text,
            "image_url": request.image_url,
            "video_url": request.video_url,
            "alt_text": request.alt_text,
            "quote_post_id": request.quote_post_id,
            "reply_to_id": request.reply_to_id,
            "reply_control": request.reply_control,
        }
        data.update({key: value for key, value in optional.items() if value is not None})
        if request.children:
            data["children"] = ",".join(request.children)
        if request.is_carousel_item:
            data["is_carousel_item"] = "true"
        response = await self._request("POST", "me/threads", token, params=data)
        payload = self._object(response)
        container_id = payload.get("id")
        if not isinstance(container_id, str) or not container_id:
            raise ThreadsContractError()
        return MediaContainer(container_id=container_id)

    async def get_container(self, token: SecretStr, container_id: str) -> MediaContainer:
        response = await self._request(
            "GET",
            quote(container_id, safe=""),
            token,
            params={"fields": "id,status,error_message"},
        )
        payload = self._object(response)
        response_id = payload.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise ThreadsContractError()
        status = payload.get("status")
        error_message = payload.get("error_message")
        if status is not None and not isinstance(status, str):
            raise ThreadsContractError()
        if error_message is not None and not isinstance(error_message, str):
            raise ThreadsContractError()
        return MediaContainer(response_id, status, error_message)

    async def publish_container(self, token: SecretStr, container_id: str) -> str:
        response = await self._request(
            "POST", "me/threads_publish", token, params={"creation_id": container_id}
        )
        media_id = self._object(response).get("id")
        if not isinstance(media_id, str) or not media_id:
            raise ThreadsContractError()
        return media_id

    async def get_media(self, token: SecretStr, media_id: str) -> RemoteMedia:
        response = await self._request(
            "GET",
            quote(media_id, safe=""),
            token,
            params={"fields": "id,text,permalink,timestamp"},
        )
        payload = self._object(response)
        remote_id = payload.get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise ThreadsContractError()
        text = self._optional_string(payload.get("text"))
        permalink = self._optional_string(payload.get("permalink"))
        timestamp = self._optional_string(payload.get("timestamp"))
        return RemoteMedia(remote_id, text, permalink, timestamp)

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
        response = await self._request(
            "GET",
            "me/threads_publishing_limit",
            token,
            params={"fields": "quota_usage,config,reply_quota_usage,reply_config"},
        )
        payload = self._object(response)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ThreadsContractError()
        quota_items = self._object_list(cast(object, data))
        if not quota_items:
            raise ThreadsContractError()
        quota = self._mapping(quota_items[0])
        if quota is None:
            raise ThreadsContractError()
        config = self._optional_mapping(quota.get("config"))
        reply_config = self._optional_mapping(quota.get("reply_config"))
        if quota.get("config") is not None and config is None:
            raise ThreadsContractError()
        if quota.get("reply_config") is not None and reply_config is None:
            raise ThreadsContractError()
        return PublishingQuota(
            usage=self._optional_int(quota.get("quota_usage")),
            total=self._optional_int(config.get("quota_total") if config else None),
            duration_seconds=self._optional_int(config.get("quota_duration") if config else None),
            reply_usage=self._optional_int(quota.get("reply_quota_usage")),
            reply_total=self._optional_int(
                reply_config.get("quota_total") if reply_config else None
            ),
            reply_duration_seconds=self._optional_int(
                reply_config.get("quota_duration") if reply_config else None
            ),
        )

    async def get_replies(self, token: SecretStr, thread_id: str, after: str | None) -> ReplyPage:
        return await self._reply_page(token, thread_id, "replies", after)

    async def get_conversation(
        self, token: SecretStr, thread_id: str, after: str | None
    ) -> ReplyPage:
        return await self._reply_page(token, thread_id, "conversation", after)

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
        params = self._discovery_params(after, limit)
        params.update(
            {
                "q": query,
                "search_mode": search_mode.value,
                "search_type": search_type.value,
                "fields": self._discovery_fields(),
            }
        )
        if since is not None:
            params["since"] = since.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        response = await self._request("GET", "keyword_search", token, params=params)
        return self._discovery_page(response)

    async def get_public_profile(self, token: SecretStr, username: str) -> RemotePublicProfile:
        response = await self._request(
            "GET", "profile_lookup", token, params={"username": username}
        )
        payload = self._object(response)
        author_id = payload.get("id")
        response_username = payload.get("username")
        if (
            not isinstance(author_id, str)
            or not author_id
            or not isinstance(response_username, str)
            or not response_username
        ):
            raise ThreadsContractError()
        return RemotePublicProfile(
            remote_author_id=self._bounded_string(author_id, 255),
            username=self._bounded_string(response_username, 255),
            display_name=self._bounded_optional_string(payload.get("name"), 255),
            biography=self._bounded_optional_string(payload.get("threads_biography"), 5000),
            profile_picture_url=self._optional_https_url(
                payload.get("threads_profile_picture_url")
            ),
        )

    async def get_profile_posts(
        self, token: SecretStr, username: str, *, after: str | None, limit: int
    ) -> DiscoveryPage:
        params = self._discovery_params(after, limit)
        params.update(
            {
                "username": username,
                "fields": self._discovery_fields(),
            }
        )
        response = await self._request("GET", "profile_posts", token, params=params)
        return self._discovery_page(response)

    async def get_mentions(
        self,
        token: SecretStr,
        *,
        after: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> DiscoveryPage:
        params = self._discovery_params(after, limit)
        params["fields"] = self._discovery_fields()
        if since is not None:
            params["since"] = since.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        response = await self._request("GET", "me/mentions", token, params=params)
        return self._discovery_page(response)

    async def manage_reply(self, token: SecretStr, reply_id: str, *, hide: bool) -> None:
        response = await self._request(
            "POST",
            f"{quote(reply_id, safe='')}/manage_reply",
            token,
            params={"hide": str(hide).lower()},
        )
        self._check_success(response)

    async def manage_pending_reply(self, token: SecretStr, reply_id: str, *, approve: bool) -> None:
        response = await self._request(
            "POST",
            f"{quote(reply_id, safe='')}/manage_pending_reply",
            token,
            params={"approve": str(approve).lower()},
        )
        self._check_success(response)

    @classmethod
    def _check_success(cls, response: httpx.Response) -> None:
        if cls._object(response).get("success") is not True:
            raise ThreadsContractError()

    async def _reply_page(
        self, token: SecretStr, thread_id: str, edge: str, after: str | None
    ) -> ReplyPage:
        params: dict[str, str] = {
            "fields": "id,text,timestamp,root_post,replied_to",
            "reverse": "false",
        }
        if after is not None:
            params["after"] = after
        response = await self._request(
            "GET", f"{quote(thread_id, safe='')}/{edge}", token, params=params
        )
        payload = self._object(response)
        values = payload.get("data")
        paging = payload.get("paging")
        if not isinstance(values, list):
            raise ThreadsContractError()
        value_items = self._object_list(cast(object, values))
        paging_object = self._optional_mapping(paging)
        if paging is not None and paging_object is None:
            raise ThreadsContractError()
        cursor_value = paging_object.get("cursors") if paging_object is not None else None
        cursors = self._optional_mapping(cursor_value)
        if cursor_value is not None and cursors is None:
            raise ThreadsContractError()
        after_cursor = cursors.get("after") if cursors is not None else None
        if after_cursor is not None and (
            not isinstance(after_cursor, str)
            or not after_cursor.strip()
            or len(after_cursor) > 4096
        ):
            raise ThreadsContractError()
        next_page = paging_object.get("next") if paging_object is not None else None
        if next_page is not None and not isinstance(next_page, str):
            raise ThreadsContractError()
        replies = tuple(self._remote_reply(item) for item in value_items)
        return ReplyPage(
            replies, after_cursor, has_more=bool(next_page) or after_cursor is not None
        )

    @classmethod
    def _discovery_page(cls, response: httpx.Response) -> DiscoveryPage:
        payload = cls._object(response)
        values = payload.get("data")
        paging_value = payload.get("paging")
        paging = cls._optional_mapping(paging_value)
        if not isinstance(values, list) or (paging_value is not None and paging is None):
            raise ThreadsContractError()
        cursors_value = paging.get("cursors") if paging is not None else None
        cursors = cls._optional_mapping(cursors_value)
        if cursors_value is not None and cursors is None:
            raise ThreadsContractError()
        next_cursor = cls._optional_string(cursors.get("after") if cursors else None)
        if next_cursor is not None and (not next_cursor.strip() or len(next_cursor) > 4096):
            raise ThreadsContractError()
        items = cls._object_list(cast(object, values))
        if len(items) > 50:
            raise ThreadsContractError()
        threads = tuple(cls._remote_discovery_thread(item) for item in items)
        return DiscoveryPage(threads, next_cursor, has_more=next_cursor is not None)

    @classmethod
    def _remote_discovery_thread(cls, value: object) -> RemoteDiscoveryThread:
        item = cls._mapping(value)
        if item is None:
            raise ThreadsContractError()
        thread_id = item.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ThreadsContractError()
        thread_id = cls._bounded_string(thread_id, 255)
        author_id = cls._nested_id(item.get("owner"))
        timestamp = cls._optional_timestamp(item.get("timestamp"))
        is_quote_post = item.get("is_quote_post")
        has_replies = item.get("has_replies")
        if is_quote_post is not None and not isinstance(is_quote_post, bool):
            raise ThreadsContractError()
        if has_replies is not None and not isinstance(has_replies, bool):
            raise ThreadsContractError()
        return RemoteDiscoveryThread(
            remote_thread_id=thread_id,
            author_remote_id=author_id,
            username=cls._bounded_optional_string(item.get("username"), 255),
            text=cls._bounded_optional_string(item.get("text"), 10_000, allow_empty=True),
            permalink=cls._optional_https_url(item.get("permalink")),
            media_type=cls._bounded_optional_string(item.get("media_type"), 80),
            timestamp=timestamp,
            is_quote_post=is_quote_post,
            has_replies=has_replies,
        )

    @staticmethod
    def _discovery_fields() -> str:
        return "id,media_type,permalink,username,text,timestamp,is_quote_post,has_replies"

    @staticmethod
    def _discovery_params(after: str | None, limit: int) -> dict[str, str]:
        if not 1 <= limit <= 50:
            raise ValueError("Threads discovery page limit must be between 1 and 50")
        params = {"limit": str(limit)}
        if after is not None:
            if not after.strip():
                raise ValueError("Threads discovery cursor must not be empty")
            params["after"] = after
        return params

    @classmethod
    def _remote_reply(cls, item: object) -> RemoteReply:
        reply = cls._mapping(item)
        if reply is None:
            raise ThreadsContractError()
        reply_id = reply.get("id")
        if not isinstance(reply_id, str) or not reply_id:
            raise ThreadsContractError()
        text = cls._optional_string(reply.get("text"))
        timestamp = cls._optional_string(reply.get("timestamp"))
        return RemoteReply(
            reply_id=reply_id,
            text=text,
            timestamp=timestamp,
            root_post_id=cls._nested_id(reply.get("root_post")),
            replied_to_id=cls._nested_id(reply.get("replied_to")),
        )

    @classmethod
    def _nested_id(cls, value: object) -> str | None:
        if value is None:
            return None
        nested = cls._mapping(value)
        if nested is None:
            raise ThreadsContractError()
        nested_id = nested.get("id")
        if nested_id is not None and not isinstance(nested_id, str):
            raise ThreadsContractError()
        return cls._bounded_string(nested_id, 255) if nested_id is not None else None

    async def _request(
        self,
        method: str,
        path: str,
        token: SecretStr,
        *,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            )
        except httpx.TransportError as error:
            raise ThreadsTransportError() from error
        if response.is_error:
            retry_after = self._retry_after(response.headers.get("Retry-After"))
            status = response.status_code
            if status == 429 or status >= 500:
                raise ThreadsAPIError(
                    "THREADS_RATE_LIMITED" if status == 429 else "THREADS_SERVER_ERROR",
                    retry_after,
                )
            if status == 401:
                raise ThreadsAPIError("THREADS_AUTHENTICATION_FAILED")
            if status == 403:
                raise ThreadsAPIError("THREADS_PERMISSION_DENIED")
            if status == 404:
                raise ThreadsAPIError("THREADS_OBJECT_NOT_FOUND")
            if status == 400 or status == 422:
                raise ThreadsAPIError("THREADS_INVALID_REQUEST")
            raise ThreadsAPIError("THREADS_API_REJECTED_REQUEST")
        return response

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, object]:
        try:
            payload = _OBJECT_ADAPTER.validate_python(response.json())
        except ValidationError, ValueError:
            raise ThreadsContractError() from None
        return payload

    @staticmethod
    def _object_list(value: object) -> list[object]:
        try:
            return _OBJECT_LIST_ADAPTER.validate_python(value)
        except ValidationError:
            raise ThreadsContractError() from None

    @staticmethod
    def _mapping(value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        try:
            return _OBJECT_ADAPTER.validate_python(value)
        except ValidationError:
            raise ThreadsContractError() from None

    @classmethod
    def _optional_mapping(cls, value: object) -> dict[str, object] | None:
        return None if value is None else cls._mapping(value)

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ThreadsContractError()
        return value

    @staticmethod
    def _optional_string(value: object) -> str | None:
        if value is not None and not isinstance(value, str):
            raise ThreadsContractError()
        return value

    @classmethod
    def _bounded_optional_string(
        cls, value: object, maximum: int, *, allow_empty: bool = False
    ) -> str | None:
        result = cls._optional_string(value)
        if result is None:
            return None
        if len(result) > maximum or (not allow_empty and not result.strip()):
            raise ThreadsContractError()
        return result

    @staticmethod
    def _bounded_string(value: str, maximum: int) -> str:
        if not value.strip() or len(value) > maximum:
            raise ThreadsContractError()
        return value

    @classmethod
    def _optional_https_url(cls, value: object) -> str | None:
        result = cls._bounded_optional_string(value, 2048)
        if result is None:
            return None
        try:
            parsed = urlsplit(result)
            hostname = parsed.hostname
        except ValueError:
            raise ThreadsContractError() from None
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ThreadsContractError()
        return result

    @staticmethod
    def _optional_timestamp(value: object) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ThreadsContractError()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ThreadsContractError() from None
        if parsed.tzinfo is None:
            raise ThreadsContractError()
        return parsed.astimezone(UTC)

    @staticmethod
    def _retry_after(value: str | None) -> timedelta | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None
        if seconds < 0:
            return None
        return timedelta(seconds=seconds)
