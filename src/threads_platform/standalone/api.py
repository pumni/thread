"""Secret-safe standalone access to the supported Threads API reads."""

from __future__ import annotations

import re

import httpx2
from pydantic import SecretStr

from threads_platform.application.ports.threads import (
    PublishingQuota,
    RemoteMedia,
    ReplyPage,
    ThreadsAPI,
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
    ThreadsCredentialSecretResolver,
)
from threads_platform.config.settings import Settings
from threads_platform.infrastructure.threads_api.environment_credentials import (
    normalize_threads_credential_ref,
)
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore

_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}")
_MAX_CURSOR_LENGTH = 4096


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
        self._validate_cursor(after)
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
        self._validate_cursor(after)
        token = await self._resolve_token(alias)
        return await self._api.get_conversation(token, thread_id, after)

    async def _resolve_token(self, alias: str) -> SecretStr:
        account = self._accounts.get(alias)
        if account.credential_ref is None:
            raise ThreadsCredentialError(ThreadsCredentialErrorCode.NOT_CONFIGURED)
        return await self._secret_resolver.resolve(account.credential_ref)

    @staticmethod
    def _validate_cursor(after: str | None) -> None:
        if after is not None and (
            not after.strip() or len(after) > _MAX_CURSOR_LENGTH or "\r" in after or "\n" in after
        ):
            raise StandaloneApiError("INVALID_CURSOR")
