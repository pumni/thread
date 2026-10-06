from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx2
import pytest
from pydantic import AnyHttpUrl, SecretStr

import threads_platform.standalone.api as api_module
from threads_platform.application.ports.threads import (
    DiscoveryPage,
    PublishingQuota,
    RemoteMedia,
    RemotePublicProfile,
    ReplyPage,
    ThreadsAPI,
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
)
from threads_platform.config.settings import Settings
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.infrastructure.threads_api.environment_credentials import (
    EnvironmentThreadsCredentialSecretResolver,
)
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import (
    LocalThreadsApiRuntime,
    StandaloneApiError,
    bind_env_credential,
    build_threads_http_client,
)

_ENV_NAME = "THREADS_PLATFORM_THREADS_TOKEN_ACCOUNT_A_V1"
_CREDENTIAL_REF = f"env://{_ENV_NAME}"
_TOKEN_SENTINEL = "local-api-token-sentinel-do-not-log"


class _FakeResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.token = SecretStr(_TOKEN_SENTINEL)

    async def resolve(self, credential_ref: str) -> SecretStr:
        self.calls.append(credential_ref)
        return self.token


class _FakeApi:
    def __init__(self) -> None:
        self.quota_calls: list[SecretStr] = []
        self.media_calls: list[tuple[SecretStr, str]] = []
        self.replies_calls: list[tuple[SecretStr, str, str | None]] = []
        self.conversation_calls: list[tuple[SecretStr, str, str | None]] = []
        self.public_profile_calls: list[tuple[SecretStr, str]] = []
        self.profile_posts_calls: list[tuple[SecretStr, str, str | None, int]] = []
        self.search_calls: list[
            tuple[
                SecretStr,
                str,
                DiscoverySearchMode,
                DiscoverySearchType,
                str | None,
                object,
                object,
                int,
            ]
        ] = []
        self.mentions_calls: list[tuple[SecretStr, str | None, object, object, int]] = []
        self.quota_result = PublishingQuota(usage=3, total=250, reply_usage=1, reply_total=100)
        self.media_result = RemoteMedia("media-1", "text", "https://example.test/p/1", "now")
        self.page_result = ReplyPage((), "next", True)
        self.discovery_page_result = DiscoveryPage((), "next", True)
        self.public_profile_result = RemotePublicProfile("author-1", "alice", None, None, None)
        self.failure: Exception | None = None

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
        self.quota_calls.append(token)
        if self.failure is not None:
            raise self.failure
        return self.quota_result

    async def get_media(self, token: SecretStr, media_id: str) -> RemoteMedia:
        self.media_calls.append((token, media_id))
        if self.failure is not None:
            raise self.failure
        return self.media_result

    async def get_replies(self, token: SecretStr, thread_id: str, after: str | None) -> ReplyPage:
        self.replies_calls.append((token, thread_id, after))
        if self.failure is not None:
            raise self.failure
        return self.page_result

    async def get_conversation(
        self, token: SecretStr, thread_id: str, after: str | None
    ) -> ReplyPage:
        self.conversation_calls.append((token, thread_id, after))
        if self.failure is not None:
            raise self.failure
        return self.page_result

    async def get_public_profile(self, token: SecretStr, username: str) -> RemotePublicProfile:
        self.public_profile_calls.append((token, username))
        if self.failure is not None:
            raise self.failure
        return self.public_profile_result

    async def get_profile_posts(
        self, token: SecretStr, username: str, *, after: str | None, limit: int
    ) -> DiscoveryPage:
        self.profile_posts_calls.append((token, username, after, limit))
        if self.failure is not None:
            raise self.failure
        return self.discovery_page_result

    async def search_threads(
        self,
        token: SecretStr,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        since: object,
        until: object,
        limit: int,
    ) -> DiscoveryPage:
        self.search_calls.append(
            (token, query, search_mode, search_type, after, since, until, limit)
        )
        if self.failure is not None:
            raise self.failure
        return self.discovery_page_result

    async def get_mentions(
        self,
        token: SecretStr,
        *,
        after: str | None,
        since: object,
        until: object,
        limit: int,
    ) -> DiscoveryPage:
        self.mentions_calls.append((token, after, since, until, limit))
        if self.failure is not None:
            raise self.failure
        return self.discovery_page_result


def _configured_runtime(
    tmp_path: Path,
) -> tuple[LocalAccountStore, _FakeApi, _FakeResolver, LocalThreadsApiRuntime]:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    accounts.set_credential_ref("alice", _CREDENTIAL_REF)
    api = _FakeApi()
    resolver = _FakeResolver()
    runtime = LocalThreadsApiRuntime(accounts, cast(ThreadsAPI, api), resolver)
    return accounts, api, resolver, runtime


@pytest.mark.parametrize("reference", [_ENV_NAME, _CREDENTIAL_REF])
def test_bind_accepts_environment_name_or_exact_reference(tmp_path: Path, reference: str) -> None:
    accounts = LocalAccountStore(tmp_path)
    original = accounts.add("alice")

    result = bind_env_credential(accounts, "alice", reference)

    assert result.id == original.id
    assert result.alias == original.alias
    assert result.credential_ref == _CREDENTIAL_REF
    assert LocalAccountStore(tmp_path).get("alice").credential_ref == _CREDENTIAL_REF


@pytest.mark.parametrize(
    "invalid_value",
    [
        "THREADS_PLATFORM_DATABASE_URL",
        "env://THREADS_PLATFORM_DATABASE_URL",
        _TOKEN_SENTINEL,
    ],
)
def test_bind_rejects_arbitrary_reference_without_changing_account(
    tmp_path: Path, invalid_value: str
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    path = tmp_path / "accounts" / "alice.json"
    original_document = path.read_bytes()

    with pytest.raises(ThreadsCredentialError) as error:
        bind_env_credential(accounts, "alice", invalid_value)

    assert error.value.code == ThreadsCredentialErrorCode.INVALID.value
    assert path.read_bytes() == original_document
    assert _TOKEN_SENTINEL not in str(error.value)
    assert _TOKEN_SENTINEL not in repr(error.value)


@pytest.mark.asyncio
async def test_missing_credential_is_reported_before_resolver_or_api(
    tmp_path: Path,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    api = _FakeApi()
    resolver = _FakeResolver()
    runtime = LocalThreadsApiRuntime(accounts, cast(ThreadsAPI, api), resolver)

    with pytest.raises(ThreadsCredentialError) as error:
        await runtime.quota("alice")

    assert error.value.code == ThreadsCredentialErrorCode.NOT_CONFIGURED.value
    assert resolver.calls == []
    assert api.quota_calls == []


@pytest.mark.asyncio
async def test_missing_environment_secret_uses_safe_credential_error(
    tmp_path: Path,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    accounts.set_credential_ref("alice", _CREDENTIAL_REF)
    api = _FakeApi()
    resolver = EnvironmentThreadsCredentialSecretResolver({})
    runtime = LocalThreadsApiRuntime(accounts, cast(ThreadsAPI, api), resolver)

    with pytest.raises(ThreadsCredentialError) as error:
        await runtime.quota("alice")

    assert error.value.code == ThreadsCredentialErrorCode.SECRET_UNAVAILABLE.value
    assert api.quota_calls == []
    assert _TOKEN_SENTINEL not in str(error.value)
    assert _TOKEN_SENTINEL not in repr(error.value)


@pytest.mark.asyncio
async def test_invalid_ids_and_cursors_are_rejected_before_account_lookup() -> None:
    class _ForbiddenAccountLookup:
        def get(self, alias: str) -> object:
            raise AssertionError("account lookup must not occur for invalid input")

    api = _FakeApi()
    resolver = _FakeResolver()
    runtime = LocalThreadsApiRuntime(
        cast(LocalAccountStore, _ForbiddenAccountLookup()),
        cast(ThreadsAPI, api),
        resolver,
    )
    operations: tuple[tuple[Callable[[], object], str], ...] = (
        (lambda: runtime.media("alice", "bad/id"), "INVALID_MEDIA_ID"),
        (lambda: runtime.media("alice", "media?query"), "INVALID_MEDIA_ID"),
        (lambda: runtime.replies("alice", "bad/id"), "INVALID_THREAD_ID"),
        (lambda: runtime.conversation("alice", "bad id"), "INVALID_THREAD_ID"),
        (lambda: runtime.replies("alice", "thread-1", after=""), "INVALID_CURSOR"),
        (lambda: runtime.replies("alice", "thread-1", after="  \t"), "INVALID_CURSOR"),
        (lambda: runtime.conversation("alice", "thread-1", after="x" * 4097), "INVALID_CURSOR"),
        (lambda: runtime.conversation("alice", "thread-1", after="x\ry"), "INVALID_CURSOR"),
        (lambda: runtime.replies("alice", "thread-1", after="x\ny"), "INVALID_CURSOR"),
    )

    for operation, expected_code in operations:
        with pytest.raises(StandaloneApiError) as error:
            await operation()  # type: ignore[misc]
        assert error.value.code == expected_code

    assert resolver.calls == []
    assert api.media_calls == []
    assert api.replies_calls == []
    assert api.conversation_calls == []


@pytest.mark.asyncio
async def test_invalid_discovery_inputs_are_rejected_before_account_lookup() -> None:
    class _ForbiddenAccountLookup:
        def get(self, alias: str) -> object:
            raise AssertionError("account lookup must not occur for invalid input")

    api = _FakeApi()
    resolver = _FakeResolver()
    runtime = LocalThreadsApiRuntime(
        cast(LocalAccountStore, _ForbiddenAccountLookup()),
        cast(ThreadsAPI, api),
        resolver,
    )
    operations: tuple[tuple[Callable[[], object], str], ...] = (
        (lambda: runtime.public_profile("alice", ""), "INVALID_USERNAME"),
        (lambda: runtime.public_profile("alice", "   "), "INVALID_USERNAME"),
        (lambda: runtime.public_profile("alice", "x" * 256), "INVALID_USERNAME"),
        (lambda: runtime.public_profile("alice", "user\nname"), "INVALID_USERNAME"),
        (
            lambda: runtime.public_profile("alice", "https://example.test/profile"),
            "INVALID_USERNAME",
        ),
        (lambda: runtime.profile_posts("alice", "alice", after=""), "INVALID_CURSOR"),
        (lambda: runtime.profile_posts("alice", "alice", limit=0), "INVALID_LIMIT"),
        (lambda: runtime.profile_posts("alice", "alice", limit=51), "INVALID_LIMIT"),
        (lambda: runtime.profile_posts("alice", "alice", limit=True), "INVALID_LIMIT"),
        (
            lambda: runtime.search(
                "alice",
                "",
                search_mode=DiscoverySearchMode.KEYWORD,
                search_type=DiscoverySearchType.TOP,
            ),
            "INVALID_QUERY",
        ),
        (
            lambda: runtime.search(
                "alice",
                "https://example.test/search",
                search_mode=DiscoverySearchMode.KEYWORD,
                search_type=DiscoverySearchType.TOP,
            ),
            "INVALID_QUERY",
        ),
        (
            lambda: runtime.search(
                "alice",
                "query",
                search_mode=cast(DiscoverySearchMode, "KEYWORD"),
                search_type=DiscoverySearchType.TOP,
            ),
            "INVALID_SEARCH_MODE",
        ),
        (
            lambda: runtime.search(
                "alice",
                "query",
                search_mode=DiscoverySearchMode.KEYWORD,
                search_type=cast(DiscoverySearchType, "top"),
            ),
            "INVALID_SEARCH_TYPE",
        ),
        (
            lambda: runtime.search(
                "alice",
                "query",
                search_mode=DiscoverySearchMode.KEYWORD,
                search_type=DiscoverySearchType.TOP,
                after="x\ry",
            ),
            "INVALID_CURSOR",
        ),
        (
            lambda: runtime.search(
                "alice",
                "query",
                search_mode=DiscoverySearchMode.KEYWORD,
                search_type=DiscoverySearchType.TOP,
                limit=cast(int, 1.5),
            ),
            "INVALID_LIMIT",
        ),
        (lambda: runtime.mentions("alice", after="x" * 4097), "INVALID_CURSOR"),
        (lambda: runtime.mentions("alice", limit=False), "INVALID_LIMIT"),
    )

    for operation, expected_code in operations:
        with pytest.raises(StandaloneApiError) as error:
            await operation()  # type: ignore[misc]
        assert error.value.code == expected_code

    assert resolver.calls == []
    assert api.public_profile_calls == []
    assert api.profile_posts_calls == []
    assert api.search_calls == []
    assert api.mentions_calls == []


@pytest.mark.asyncio
async def test_discovery_methods_resolve_and_delegate_once_with_bounded_inputs(
    tmp_path: Path,
) -> None:
    _, api, resolver, runtime = _configured_runtime(tmp_path)

    profile = await runtime.public_profile("alice", "public_user")
    posts = await runtime.profile_posts("alice", "post_user", after="posts-cursor", limit=12)
    search = await runtime.search(
        "alice",
        "topic",
        search_mode=DiscoverySearchMode.TAG,
        search_type=DiscoverySearchType.RECENT,
        after="search-cursor",
        limit=8,
    )
    mentions = await runtime.mentions("alice", after="mentions-cursor", limit=3)

    assert profile == api.public_profile_result
    assert posts == search == mentions == api.discovery_page_result
    assert api.public_profile_calls == [(resolver.token, "public_user")]
    assert api.profile_posts_calls == [(resolver.token, "post_user", "posts-cursor", 12)]
    assert api.search_calls == [
        (
            resolver.token,
            "topic",
            DiscoverySearchMode.TAG,
            DiscoverySearchType.RECENT,
            "search-cursor",
            None,
            None,
            8,
        )
    ]
    assert api.mentions_calls == [(resolver.token, "mentions-cursor", None, None, 3)]
    assert resolver.calls == [_CREDENTIAL_REF] * 4


@pytest.mark.asyncio
async def test_quota_resolves_secret_and_calls_api_once(tmp_path: Path) -> None:
    _, api, resolver, runtime = _configured_runtime(tmp_path)

    result = await runtime.quota("alice")

    assert result == api.quota_result
    assert resolver.calls == [_CREDENTIAL_REF]
    assert api.quota_calls == [resolver.token]
    assert api.quota_calls[0].get_secret_value() == _TOKEN_SENTINEL


@pytest.mark.asyncio
async def test_media_resolves_secret_and_calls_api_once(tmp_path: Path) -> None:
    _, api, resolver, runtime = _configured_runtime(tmp_path)

    result = await runtime.media("alice", "media:1")

    assert result == api.media_result
    assert resolver.calls == [_CREDENTIAL_REF]
    assert api.media_calls == [(resolver.token, "media:1")]


@pytest.mark.parametrize("method_name", ["replies", "conversation"])
@pytest.mark.asyncio
async def test_page_methods_forward_id_and_cursor_once_without_mutation(
    tmp_path: Path, method_name: str
) -> None:
    _, api, resolver, runtime = _configured_runtime(tmp_path)
    cursor = "opaque.cursor:with spaces"

    result = await getattr(runtime, method_name)("alice", "thread:1", after=cursor)

    assert result == api.page_result
    assert resolver.calls == [_CREDENTIAL_REF]
    calls = api.replies_calls if method_name == "replies" else api.conversation_calls
    assert calls == [(resolver.token, "thread:1", cursor)]


@pytest.mark.asyncio
async def test_credential_is_resolved_again_for_each_api_invocation(tmp_path: Path) -> None:
    _, api, resolver, runtime = _configured_runtime(tmp_path)

    await runtime.quota("alice")
    await runtime.quota("alice")

    assert resolver.calls == [_CREDENTIAL_REF, _CREDENTIAL_REF]
    assert len(api.quota_calls) == 2


@pytest.mark.asyncio
async def test_api_errors_do_not_expose_resolved_token(tmp_path: Path) -> None:
    _, api, resolver, runtime = _configured_runtime(tmp_path)
    from threads_platform.application.ports.threads import ThreadsAPIError

    api.failure = ThreadsAPIError("THREADS_TRANSPORT_FAILURE")

    with pytest.raises(ThreadsAPIError) as error:
        await runtime.media("alice", "media-1")

    assert resolver.calls == [_CREDENTIAL_REF]
    assert _TOKEN_SENTINEL not in str(error.value)
    assert _TOKEN_SENTINEL not in repr(error.value)


def test_http_client_builder_uses_accepted_transport_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(threads_api_base_url=AnyHttpUrl("https://example.test/v1/"))
    captured: dict[str, object] = {}
    marker = object()

    def fake_client(**kwargs: object) -> object:
        captured.update(kwargs)
        return marker

    monkeypatch.setattr(api_module.httpx2, "AsyncClient", fake_client)

    client = build_threads_http_client(settings)

    assert client is marker
    assert captured == {
        "base_url": "https://example.test/v1/",
        "timeout": httpx2.Timeout(15.0),
        "follow_redirects": False,
        "verify": True,
        "trust_env": True,
    }
