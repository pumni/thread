"""Secret-safe standalone access to the supported Threads API reads."""

from __future__ import annotations

import re

import httpx2
from pydantic import SecretStr

from threads_platform.application.ports.threads import (
    DiscoveryPage,
    PublishingQuota,
    RemoteMedia,
    RemotePublicProfile,
    ReplyPage,
    ThreadsAPI,
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
    ThreadsCredentialSecretResolver,
)
from threads_platform.config.settings import Settings
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.infrastructure.threads_api.environment_credentials import (
    normalize_threads_credential_ref,
)
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore

_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}")
_URL_INPUT = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|//|www\.)", re.IGNORECASE)
_MAX_CURSOR_LENGTH = 4096
_MAX_PAGE_LIMIT = 50
_DEFAULT_PAGE_LIMIT = 25


class StandaloneApiError(Exception):
    """A sanitized local API command error."""

    code: str

    def __init__(self, code: str) -> None:
        if _SAFE_ERROR_CODE.fullmatch(code) is None:
            raise ValueError("standalone API error code must be bounded and safe")
        self.code = code
        super().__init__(code)


def bind_env_credential(
    accounts: LocalAccountStore,
    alias: str,
    variable_name: str,
) -> LocalAccount:
    credential_ref = normalize_threads_credential_ref(variable_name)
    return accounts.set_credential_ref(alias, credential_ref)


def build_threads_http_client(settings: Settings) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=str(settings.threads_api_base_url),
        timeout=httpx2.Timeout(15.0),
        follow_redirects=False,
        verify=True,
        trust_env=True,
    )


class LocalThreadsApiRuntime:
    def __init__(
        self,
        accounts: LocalAccountStore,
        api: ThreadsAPI,
        secret_resolver: ThreadsCredentialSecretResolver,
    ) -> None:
        self._accounts = accounts
        self._api = api
        self._secret_resolver = secret_resolver

    async def quota(self, alias: str) -> PublishingQuota:
        token = await self._resolve_token(alias)
        return await self._api.get_publishing_quota(token)

    async def media(self, alias: str, media_id: str) -> RemoteMedia:
        if _OPAQUE_ID.fullmatch(media_id) is None:
            raise StandaloneApiError("INVALID_MEDIA_ID")
        token = await self._resolve_token(alias)
        return await self._api.get_media(token, media_id)

    async def replies(
        self,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        if _OPAQUE_ID.fullmatch(thread_id) is None:
            raise StandaloneApiError("INVALID_THREAD_ID")
        self.validate_cursor(after)
        token = await self._resolve_token(alias)
        return await self._api.get_replies(token, thread_id, after)

    async def conversation(
        self,
        alias: str,
        thread_id: str,
        *,
        after: str | None = None,
    ) -> ReplyPage:
        if _OPAQUE_ID.fullmatch(thread_id) is None:
            raise StandaloneApiError("INVALID_THREAD_ID")
        self.validate_cursor(after)
        token = await self._resolve_token(alias)
        return await self._api.get_conversation(token, thread_id, after)

    async def public_profile(self, alias: str, username: str) -> RemotePublicProfile:
        normalized_username = self.validate_lookup_text(username, "INVALID_USERNAME")
        token = await self._resolve_token(alias)
        return await self._api.get_public_profile(token, normalized_username)

    async def profile_posts(
        self,
        alias: str,
        username: str,
        *,
        after: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> DiscoveryPage:
        normalized_username = self.validate_lookup_text(username, "INVALID_USERNAME")
        self.validate_cursor(after)
        self.validate_limit(limit)
        token = await self._resolve_token(alias)
        return await self._api.get_profile_posts(
            token, normalized_username, after=after, limit=limit
        )

    async def search(
        self,
        alias: str,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> DiscoveryPage:
        normalized_query = self.validate_lookup_text(query, "INVALID_QUERY")
        self.validate_cursor(after)
        self.validate_limit(limit)
        search_mode = self._validate_search_mode(search_mode)
        search_type = self._validate_search_type(search_type)
        token = await self._resolve_token(alias)
        return await self._api.search_threads(
            token,
            normalized_query,
            search_mode=search_mode,
            search_type=search_type,
            after=after,
            since=None,
            until=None,
            limit=limit,
        )

    async def mentions(
        self,
        alias: str,
        *,
        after: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> DiscoveryPage:
        self.validate_cursor(after)
        self.validate_limit(limit)
        token = await self._resolve_token(alias)
        return await self._api.get_mentions(
            token,
            after=after,
            since=None,
            until=None,
            limit=limit,
        )

    async def _resolve_token(self, alias: str) -> SecretStr:
        account = self._accounts.get(alias)
        if account.credential_ref is None:
            raise ThreadsCredentialError(ThreadsCredentialErrorCode.NOT_CONFIGURED)
        return await self._secret_resolver.resolve(account.credential_ref)

    @staticmethod
    def validate_cursor(after: object) -> None:
        if after is not None and (
            not isinstance(after, str)
            or not after.strip()
            or len(after) > _MAX_CURSOR_LENGTH
            or "\r" in after
            or "\n" in after
        ):
            raise StandaloneApiError("INVALID_CURSOR")

    @staticmethod
    def validate_lookup_text(value: object, code: str) -> str:
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise StandaloneApiError(code)
        normalized = value.strip()
        if not normalized or len(normalized) > 255 or _URL_INPUT.match(normalized) is not None:
            raise StandaloneApiError(code)
        return normalized

    @staticmethod
    def validate_limit(limit: object) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_PAGE_LIMIT
        ):
            raise StandaloneApiError("INVALID_LIMIT")

    @staticmethod
    def _validate_search_mode(value: object) -> DiscoverySearchMode:
        if not isinstance(value, DiscoverySearchMode):
            raise StandaloneApiError("INVALID_SEARCH_MODE")
        return value

    @staticmethod
    def _validate_search_type(value: object) -> DiscoverySearchType:
        if not isinstance(value, DiscoverySearchType):
            raise StandaloneApiError("INVALID_SEARCH_TYPE")
        return value
