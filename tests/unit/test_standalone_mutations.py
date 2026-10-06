from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr

import threads_platform.standalone.mutations as mutation_module
from threads_platform.application.ports.threads import (
    MediaContainer,
    MediaContainerRequest,
    PublishingQuota,
    ThreadsAPI,
    ThreadsAPIError,
    ThreadsContractError,
    ThreadsCredentialError,
    ThreadsCredentialSecretResolver,
    ThreadsTransportError,
)
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.infrastructure.threads_api.environment_credentials import (
    EnvironmentThreadsCredentialSecretResolver,
)
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.mutations import (
    LocalOperation,
    LocalOperationStore,
    LocalThreadsMutationRuntime,
    PublishedMediaResult,
    StandaloneMutationError,
)

_TOKEN = "token-sentinel-never-journal-this"
_CREDENTIAL_REF = "env://THREADS_PLATFORM_THREADS_TOKEN_TEST"
_TEXT = "text-sentinel-never-journal-this"


class _InjectedPublishBaseException(BaseException):
    pass


class _FakeResolver:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.error = error

    async def resolve(self, credential_ref: str) -> SecretStr:
        self.calls.append(credential_ref)
        if self.error is not None:
            raise self.error
        return SecretStr(_TOKEN)


class _FakeAPI:
    def __init__(
        self,
        root: Path,
        operations: LocalOperationStore,
        *,
        quota: PublishingQuota | None = None,
        create_error: BaseException | None = None,
        publish_error: BaseException | None = None,
        container_id: str = "container-123",
        container_statuses: tuple[str | None, ...] | None = None,
        container_status_error: BaseException | None = None,
        container_response_id: str | None = None,
        media_id: str = "media-123",
    ) -> None:
        self.root = root
        self.operations = operations
        self.quota = quota or PublishingQuota(usage=1, total=100)
        self.create_error = create_error
        self.publish_error = publish_error
        self.container_id = container_id
        self.container_statuses = container_statuses
        self.container_status_error = container_status_error
        self.container_response_id = (
            container_id if container_response_id is None else container_response_id
        )
        self.status_check_count = 0
        self.media_id = media_id
        self.calls: list[str] = []
        self.events: list[tuple[str, str | None]] = []
        self.create_request: MediaContainerRequest | None = None

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
        assert token.get_secret_value() == _TOKEN
        self.calls.append("quota")
        has_operations = (self.root / "operations").exists()
        self.events.append(("quota", "operation-directory" if has_operations else None))
        return self.quota

    async def create_container(
        self, token: SecretStr, request: MediaContainerRequest
    ) -> MediaContainer:
        assert token.get_secret_value() == _TOKEN
        self.calls.append("create")
        self.create_request = request
        operation = _only_operation(self.operations, self.root)
        self.events.append(("create", operation.phase))
        if self.create_error is not None:
            raise self.create_error
        return MediaContainer(container_id=self.container_id)

    async def publish_container(self, token: SecretStr, container_id: str) -> str:
        assert token.get_secret_value() == _TOKEN
        assert container_id == self.container_id
        self.calls.append("publish")
        operation = _only_operation(self.operations, self.root)
        assert operation.container_id == container_id
        self.events.append(("publish", operation.phase))
        if self.publish_error is not None:
            raise self.publish_error
        return self.media_id

    async def get_container(self, token: SecretStr, container_id: str) -> MediaContainer:
        assert token.get_secret_value() == _TOKEN
        assert container_id == self.container_id
        self.calls.append("status")
        self.status_check_count += 1
        operation = _only_operation(self.operations, self.root)
        assert operation.container_id == container_id
        self.events.append(("status", operation.phase))
        if self.container_status_error is not None:
            raise self.container_status_error
        if not self.container_statuses:
            raise AssertionError("media status polling was not configured")
        index = min(self.status_check_count - 1, len(self.container_statuses) - 1)
        return MediaContainer(
            container_id=self.container_response_id,
            status=self.container_statuses[index],
        )

    async def get_media(self, *_args: object, **_kwargs: object) -> object:
        self.calls.append("get_media")
        raise AssertionError("media reconciliation is forbidden")


class _CountingAccounts(LocalAccountStore):
    def __init__(self, data_root: Path) -> None:
        super().__init__(data_root)
        self.get_calls = 0

    def get(self, alias: str):
        self.get_calls += 1
        return super().get(alias)


def _setup(
    root: Path,
    *,
    quota: PublishingQuota | None = None,
    resolver: _FakeResolver | None = None,
    create_error: BaseException | None = None,
    publish_error: BaseException | None = None,
    container_id: str = "container-123",
    container_statuses: tuple[str | None, ...] | None = None,
    container_status_error: BaseException | None = None,
    container_response_id: str | None = None,
    media_id: str = "media-123",
) -> tuple[
    LocalAccountStore,
    LocalOperationStore,
    _FakeAPI,
    _FakeResolver,
    LocalThreadsMutationRuntime,
    UUID,
]:
    accounts = LocalAccountStore(root)
    account = accounts.add("alice")
    accounts.set_credential_ref("alice", _CREDENTIAL_REF)
    operations = LocalOperationStore(root)
    secret_resolver = resolver or _FakeResolver()
    api = _FakeAPI(
        root,
        operations,
        quota=quota,
        create_error=create_error,
        publish_error=publish_error,
        container_id=container_id,
        container_statuses=container_statuses,
        container_status_error=container_status_error,
        container_response_id=container_response_id,
        media_id=media_id,
    )
    runtime = LocalThreadsMutationRuntime(
        root,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, secret_resolver),
        operations,
    )
    return accounts, operations, api, secret_resolver, runtime, account.id


def _only_operation(store: LocalOperationStore, root: Path) -> LocalOperation:
    paths = tuple((root / "operations").glob("*.json"))
    assert len(paths) == 1
    return store.get(UUID(paths[0].stem))


async def _publish_media(
    runtime: LocalThreadsMutationRuntime,
    media_type: str,
    media_url: str,
    text: str | None = None,
    alt_text: str | None = None,
) -> PublishedMediaResult:
    if media_type == "IMAGE":
        return await runtime.publish_image("alice", media_url, text, alt_text=alt_text)
    return await runtime.publish_video("alice", media_url, text, alt_text=alt_text)


def _patch_instant_polling(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(mutation_module.asyncio, "sleep", no_sleep)
    return delays


@pytest.mark.parametrize("text", [None, "", "   \t", "x" * 501])
@pytest.mark.asyncio
async def test_invalid_text_is_rejected_before_account_lock_secret_or_api(
    tmp_path: Path, text: object
) -> None:
    accounts = _CountingAccounts(tmp_path)
    operations = LocalOperationStore(tmp_path)
    resolver = _FakeResolver()
    api = _FakeAPI(tmp_path, operations)
    runtime = LocalThreadsMutationRuntime(
        tmp_path,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, resolver),
        operations,
    )

    with pytest.raises(StandaloneMutationError, match="INVALID_POST_TEXT") as caught:
        await runtime.publish_text("missing", cast(str, text))

    assert caught.value.code == "INVALID_POST_TEXT"
    assert accounts.get_calls == 0
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "locks").exists()
    assert not (tmp_path / "operations").exists()


@pytest.mark.parametrize(
    ("thread_id", "text", "parent_reply_id", "expected_code"),
    [
        ("bad/id", _TEXT, None, "INVALID_THREAD_ID"),
        ("https://example.test/thread", _TEXT, None, "INVALID_THREAD_ID"),
        ("thread-1", "", None, "INVALID_REPLY_TEXT"),
        ("thread-1", " \t ", None, "INVALID_REPLY_TEXT"),
        ("thread-1", "x" * 501, None, "INVALID_REPLY_TEXT"),
        ("thread-1", _TEXT, "", "INVALID_REPLY_ID"),
        ("thread-1", _TEXT, "bad/reply", "INVALID_REPLY_ID"),
        ("thread-1", _TEXT, "https://example.test/reply", "INVALID_REPLY_ID"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_reply_inputs_are_rejected_before_account_lock_secret_or_api(
    tmp_path: Path,
    thread_id: str,
    text: str,
    parent_reply_id: str | None,
    expected_code: str,
) -> None:
    accounts = _CountingAccounts(tmp_path)
    operations = LocalOperationStore(tmp_path)
    resolver = _FakeResolver()
    api = _FakeAPI(tmp_path, operations)
    runtime = LocalThreadsMutationRuntime(
        tmp_path,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, resolver),
        operations,
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.create_reply(
            "missing",
            thread_id,
            text,
            parent_reply_id=parent_reply_id,
        )

    assert caught.value.code == expected_code
    assert accounts.get_calls == 0
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "locks").exists()
    assert not (tmp_path / "operations").exists()


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.parametrize(
    "media_url",
    [
        None,
        "",
        " " * 1,
        "x" * 2049,
        "file:///tmp/image.jpg",
        "data:image/png;base64,AA==",
        "/tmp/image.jpg",
        "C:\\tmp\\image.jpg",
        "https:///image.jpg",
        ("https://" + "user" + ":" + "password" + "@media.example/image.jpg"),
        "http://localhost/image.jpg",
        "http://localhost./image.jpg",
        "https://cdn.localhost/image.jpg",
        "http://localhost.localdomain/image.jpg",
        "http://127.0.0.1/image.jpg",
        "http://127.1/image.jpg",
        "http://2130706433/image.jpg",
        "http://0x7f000001/image.jpg",
        "http://[::1]/image.jpg",
        "http://[::ffff:127.0.0.1]/image.jpg",
        "http://0.0.0.0/image.jpg",
        "http://printer.local/image.jpg",
        "https://media.example/image\n.jpg",
        "https://media.example/bad%2.jpg",
        "https://media.example/bad%xx.jpg",
        r"https://media.example\image.jpg",
        "https://[broken/image.jpg",
        "https://bad host/image.jpg",
        "https://media.example:99999/image.jpg",
        "media.example/image.jpg",
    ],
)
@pytest.mark.asyncio
async def test_invalid_media_url_is_rejected_before_account_lock_secret_or_api(
    tmp_path: Path, media_type: str, media_url: object
) -> None:
    accounts = _CountingAccounts(tmp_path)
    operations = LocalOperationStore(tmp_path)
    resolver = _FakeResolver()
    api = _FakeAPI(tmp_path, operations)
    runtime = LocalThreadsMutationRuntime(
        tmp_path,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, resolver),
        operations,
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, cast(str, media_url))

    assert caught.value.code == "INVALID_MEDIA_URL"
    assert accounts.get_calls == 0
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "locks").exists()
    assert not (tmp_path / "operations").exists()


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.parametrize(
    ("text", "alt_text", "expected_code"),
    [
        ("", "description", "INVALID_POST_TEXT"),
        (" \t ", "description", "INVALID_POST_TEXT"),
        ("x" * 501, "description", "INVALID_POST_TEXT"),
        (None, "x" * 1001, "INVALID_ALT_TEXT"),
        (None, 123, "INVALID_ALT_TEXT"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_optional_media_text_is_rejected_before_account_lock_secret_or_api(
    tmp_path: Path,
    media_type: str,
    text: object,
    alt_text: object,
    expected_code: str,
) -> None:
    accounts = _CountingAccounts(tmp_path)
    operations = LocalOperationStore(tmp_path)
    resolver = _FakeResolver()
    api = _FakeAPI(tmp_path, operations)
    runtime = LocalThreadsMutationRuntime(
        tmp_path,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, resolver),
        operations,
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(
            runtime,
            media_type,
            "https://media.example/image.jpg",
            cast(str | None, text),
            cast(str | None, alt_text),
        )

    assert caught.value.code == expected_code
    assert accounts.get_calls == 0
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "locks").exists()
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_missing_credential_and_missing_environment_secret_are_safe(
    tmp_path: Path,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    operations = LocalOperationStore(tmp_path)
    api = _FakeAPI(tmp_path, operations)
    runtime = LocalThreadsMutationRuntime(
        tmp_path,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, _FakeResolver()),
        operations,
    )
    with pytest.raises(ThreadsCredentialError) as missing:
        await runtime.publish_text("alice", _TEXT)
    assert missing.value.code == "THREADS_CREDENTIAL_NOT_CONFIGURED"
    with pytest.raises(ThreadsCredentialError) as missing_reply:
        await runtime.create_reply("alice", "thread-1", _TEXT)
    assert missing_reply.value.code == "THREADS_CREDENTIAL_NOT_CONFIGURED"
    for media_type in ("IMAGE", "VIDEO"):
        with pytest.raises(ThreadsCredentialError) as missing_media:
            await _publish_media(runtime, media_type, "https://media.example/image.jpg")
        assert missing_media.value.code == "THREADS_CREDENTIAL_NOT_CONFIGURED"
    assert api.calls == []

    accounts.set_credential_ref("alice", _CREDENTIAL_REF)
    missing_environment = EnvironmentThreadsCredentialSecretResolver(environment={})
    runtime = LocalThreadsMutationRuntime(
        tmp_path,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, missing_environment),
        operations,
    )
    with pytest.raises(ThreadsCredentialError) as unavailable:
        await runtime.publish_text("alice", _TEXT)
    assert unavailable.value.code == "THREADS_CREDENTIAL_SECRET_UNAVAILABLE"
    with pytest.raises(ThreadsCredentialError) as unavailable_reply:
        await runtime.create_reply("alice", "thread-1", _TEXT)
    assert unavailable_reply.value.code == "THREADS_CREDENTIAL_SECRET_UNAVAILABLE"
    for media_type in ("IMAGE", "VIDEO"):
        with pytest.raises(ThreadsCredentialError) as unavailable_media:
            await _publish_media(runtime, media_type, "https://media.example/image.jpg")
        assert unavailable_media.value.code == "THREADS_CREDENTIAL_SECRET_UNAVAILABLE"
    assert _TOKEN not in str(unavailable.value)
    assert _CREDENTIAL_REF not in str(unavailable.value)
    assert api.calls == []
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_quota_is_called_once_before_journal_and_quota_limit_has_no_mutation(
    tmp_path: Path,
) -> None:
    _, _, api, resolver, runtime, _ = _setup(tmp_path, quota=PublishingQuota(usage=25, total=25))

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_text("alice", _TEXT)

    assert caught.value.code == "THREADS_PUBLISHING_QUOTA_REACHED"
    assert api.calls == ["quota"]
    assert api.events == [("quota", None)]
    assert resolver.calls == [_CREDENTIAL_REF]
    assert not (tmp_path / "operations").exists()


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_media_uses_post_quota_once_and_known_exhaustion_creates_no_journal(
    tmp_path: Path, media_type: str
) -> None:
    _, _, api, resolver, runtime, _ = _setup(
        tmp_path,
        quota=PublishingQuota(usage=25, total=25, reply_usage=0, reply_total=1000),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, "https://media.example/image.jpg")

    assert caught.value.code == "THREADS_PUBLISHING_QUOTA_REACHED"
    assert api.calls == ["quota"]
    assert api.events == [("quota", None)]
    assert resolver.calls == [_CREDENTIAL_REF]
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_media_with_unknown_post_quota_fields_does_not_invent_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        quota=PublishingQuota(usage=None, total=100),
        container_statuses=("FINISHED",),
    )
    _patch_instant_polling(monkeypatch)

    result = await runtime.publish_image("alice", "https://media.example/image.jpg")

    assert api.calls == ["quota", "create", "status", "publish"]
    assert result.media_id == "media-123"
    assert operations.get(result.operation_id).kind == "POST_IMAGE"


@pytest.mark.asyncio
async def test_reply_quota_is_checked_once_and_exhaustion_creates_no_journal_or_container(
    tmp_path: Path,
) -> None:
    _, _, api, resolver, runtime, _ = _setup(
        tmp_path,
        quota=PublishingQuota(
            usage=1,
            total=100,
            reply_usage=25,
            reply_total=25,
        ),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.create_reply("alice", "thread-1", _TEXT)

    assert caught.value.code == "THREADS_REPLY_QUOTA_REACHED"
    assert api.calls == ["quota"]
    assert api.events == [("quota", None)]
    assert resolver.calls == [_CREDENTIAL_REF]
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_unknown_reply_quota_fields_do_not_invent_exhaustion(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        quota=PublishingQuota(
            usage=100,
            total=100,
            reply_usage=None,
            reply_total=25,
        ),
    )

    result = await runtime.create_reply("alice", "thread-1", _TEXT)

    assert api.calls == ["quota", "create", "publish"]
    assert result.reply_id == "media-123"
    assert operations.get(result.operation_id).phase == "PUBLISHED"


@pytest.mark.parametrize(
    ("parent_reply_id", "expected_reply_to"),
    [(None, "thread-123"), ("reply-456", "reply-456")],
)
@pytest.mark.asyncio
async def test_create_reply_uses_exact_text_request_and_journals_safe_phase_order(
    tmp_path: Path,
    parent_reply_id: str | None,
    expected_reply_to: str,
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path)

    result = await runtime.create_reply(
        "alice",
        "thread-123",
        _TEXT,
        parent_reply_id=parent_reply_id,
    )

    assert api.calls == ["quota", "create", "publish"]
    assert api.events == [
        ("quota", None),
        ("create", "RECEIVED"),
        ("publish", "PUBLISH_REQUESTED"),
    ]
    assert api.create_request == MediaContainerRequest(
        media_type="TEXT",
        text=_TEXT,
        reply_to_id=expected_reply_to,
    )
    assert result.reply_id == "media-123"
    operation = operations.get(result.operation_id)
    assert operation.kind == "CREATE_REPLY"
    assert operation.phase == "PUBLISHED"
    assert operation.container_id == "container-123"
    assert operation.media_id == "media-123"
    assert operation.outcome_code is None
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    for forbidden in (_TEXT, _TOKEN, _CREDENTIAL_REF, "thread-123", "reply-456", "Authorization"):
        assert forbidden not in journal
    assert set(json.loads(journal)) == {
        "version",
        "id",
        "account_id",
        "kind",
        "phase",
        "container_id",
        "media_id",
    }


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_media_publish_uses_exact_request_and_safe_journal_phase_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media_type: str
) -> None:
    image_url = "https://image-url-secret-sentinel.media.example/photo.png?key=private"
    video_url = "https://video-url-secret-sentinel.media.example/clip.mp4?key=private"
    media_url = image_url if media_type == "IMAGE" else video_url
    alt_text = "alt-text-secret-sentinel\nordinary description"
    kind = "POST_IMAGE" if media_type == "IMAGE" else "POST_VIDEO"
    _, operations, api, _, runtime, _ = _setup(tmp_path, container_statuses=("FINISHED",))
    _patch_instant_polling(monkeypatch)

    result = await _publish_media(runtime, media_type, media_url, _TEXT, alt_text)

    assert api.calls == ["quota", "create", "status", "publish"]
    assert api.events == [
        ("quota", None),
        ("create", "RECEIVED"),
        ("status", "CONTAINER_CREATED"),
        ("publish", "PUBLISH_REQUESTED"),
    ]
    if media_type == "IMAGE":
        expected_request = MediaContainerRequest(
            media_type="IMAGE",
            image_url=image_url,
            text=_TEXT,
            alt_text=alt_text,
        )
    else:
        expected_request = MediaContainerRequest(
            media_type="VIDEO",
            video_url=video_url,
            text=_TEXT,
            alt_text=alt_text,
        )
    assert api.create_request == expected_request
    assert result.media_id == "media-123"
    operation = operations.get(result.operation_id)
    assert operation.version == 1
    assert operation.kind == kind
    assert operation.phase == "PUBLISHED"
    assert operation.container_id == "container-123"
    assert operation.media_id == "media-123"
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    for forbidden in (
        media_url,
        _TEXT,
        alt_text,
        _TOKEN,
        _CREDENTIAL_REF,
        "Authorization",
    ):
        assert forbidden not in journal
    assert set(json.loads(journal)) == {
        "version",
        "id",
        "account_id",
        "kind",
        "phase",
        "container_id",
        "media_id",
    }


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_media_publish_without_optional_copy_keeps_request_fields_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media_type: str
) -> None:
    _, _, api, _, runtime, _ = _setup(tmp_path, container_statuses=("FINISHED",))
    _patch_instant_polling(monkeypatch)
    media_url = f"https://media.example/{media_type.lower()}.bin"

    await _publish_media(runtime, media_type, media_url)

    if media_type == "IMAGE":
        expected = MediaContainerRequest(media_type="IMAGE", image_url=media_url)
    else:
        expected = MediaContainerRequest(media_type="VIDEO", video_url=media_url)
    assert api.create_request == expected
    assert api.calls.count("publish") == 1


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_invalid_media_container_id_fails_before_status_or_publish(
    tmp_path: Path, media_type: str
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, container_id="../unsafe")

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, "https://media.example/media.bin")

    assert caught.value.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
    assert api.calls == ["quota", "create"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == f"POST_{media_type}"
    assert operation.phase == "FAILED_FINAL"
    assert operation.container_id is None


@pytest.mark.asyncio
async def test_in_progress_container_polls_with_count_and_wall_clock_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = ("IN_PROGRESS",) * 9 + ("FINISHED",)
    _, _, api, _, runtime, _ = _setup(tmp_path, container_statuses=statuses)
    delays = _patch_instant_polling(monkeypatch)
    real_timeout = mutation_module.asyncio.timeout
    timeout_budgets: list[float] = []

    def record_timeout(seconds: float):
        timeout_budgets.append(seconds)
        return real_timeout(seconds)

    monkeypatch.setattr(mutation_module.asyncio, "timeout", record_timeout)

    await runtime.publish_video("alice", "https://media.example/video.mp4")

    assert api.status_check_count == 10
    assert api.calls == ["quota", "create", *("status" for _ in range(10)), "publish"]
    assert delays == [3.0] * 9
    assert sum(delays) <= 30.0
    assert timeout_budgets == [30.0]


@pytest.mark.asyncio
async def test_processing_poll_exhaustion_fails_final_without_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, container_statuses=("IN_PROGRESS",))
    delays = _patch_instant_polling(monkeypatch)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_image("alice", "https://media.example/image.jpg")

    assert caught.value.code == "THREADS_CONTAINER_PROCESSING_TIMEOUT"
    assert api.status_check_count == 10
    assert api.calls == ["quota", "create", *("status" for _ in range(10))]
    assert delays == [3.0] * 9
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == "POST_IMAGE"
    assert operation.phase == "FAILED_FINAL"
    assert operation.outcome_code == "THREADS_CONTAINER_PROCESSING_TIMEOUT"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("ERROR", "THREADS_CONTAINER_ERROR"),
        ("EXPIRED", "THREADS_CONTAINER_EXPIRED"),
        ("PUBLISHED", "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
        ("UNRECOGNIZED", "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
        (None, "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
    ],
)
@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_non_ready_container_status_fails_closed_before_publish(
    tmp_path: Path,
    media_type: str,
    status: str | None,
    expected_code: str,
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, container_statuses=(status,))

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, "https://media.example/media.bin")

    assert caught.value.code == expected_code
    assert api.calls == ["quota", "create", "status"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == f"POST_{media_type}"
    assert operation.phase == "FAILED_FINAL"
    assert operation.outcome_code == expected_code


@pytest.mark.asyncio
async def test_container_status_response_must_match_created_container(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        container_statuses=("FINISHED",),
        container_response_id="different-container",
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_image("alice", "https://media.example/image.jpg")

    assert caught.value.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
    assert api.calls == ["quota", "create", "status"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.phase == "FAILED_FINAL"
    assert operation.container_id == "container-123"


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_container_status_api_failure_is_final_without_publish(
    tmp_path: Path, media_type: str
) -> None:
    error = ThreadsAPIError("THREADS_RATE_LIMITED")
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        container_status_error=error,
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, "https://media.example/media.bin")

    assert caught.value.code == "THREADS_RATE_LIMITED"
    assert api.calls == ["quota", "create", "status"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.phase == "FAILED_FINAL"
    assert operation.outcome_code == "THREADS_RATE_LIMITED"


@pytest.mark.asyncio
async def test_container_status_timeout_is_final_without_publish(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        container_status_error=TimeoutError("private status timeout"),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_image("alice", "https://media.example/image.jpg")

    assert caught.value.code == "THREADS_CONTAINER_PROCESSING_TIMEOUT"
    assert api.calls == ["quota", "create", "status"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.phase == "FAILED_FINAL"
    assert operation.outcome_code == "THREADS_CONTAINER_PROCESSING_TIMEOUT"


@pytest.mark.parametrize("cancel_at", ["status", "sleep"])
@pytest.mark.asyncio
async def test_container_processing_cancellation_is_final_without_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_at: str
) -> None:
    status_error = asyncio.CancelledError("private processing cancellation")
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        container_statuses=("IN_PROGRESS",),
        container_status_error=status_error if cancel_at == "status" else None,
    )
    if cancel_at == "sleep":

        async def cancel_sleep(_delay: float) -> None:
            raise asyncio.CancelledError("private processing cancellation")

        monkeypatch.setattr(mutation_module.asyncio, "sleep", cancel_sleep)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_video("alice", "https://media.example/video.mp4")

    assert caught.value.code == "OPERATION_CANCELLED"
    assert "private processing cancellation" not in str(caught.value)
    assert api.calls == ["quota", "create", "status"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == "POST_VIDEO"
    assert operation.phase == "FAILED_FINAL"
    assert operation.outcome_code == "OPERATION_CANCELLED"


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.parametrize(
    "error",
    [
        ThreadsAPIError("THREADS_RATE_LIMITED"),
        ThreadsAPIError("THREADS_SERVER_ERROR"),
        ThreadsTransportError(),
        ThreadsContractError(),
        RuntimeError("raw media publish response"),
    ],
)
@pytest.mark.asyncio
async def test_media_publish_failure_after_boundary_is_ambiguous_without_retry(
    tmp_path: Path,
    media_type: str,
    error: Exception,
) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        publish_error=error,
        container_statuses=("FINISHED",),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, "https://media.example/media.bin")

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "status", "publish"]
    assert api.calls.count("publish") == 1
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == f"POST_{media_type}"
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == getattr(error, "code", "THREADS_TRANSPORT_FAILURE")
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    assert "raw media publish response" not in journal


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_media_publish_cancellation_after_boundary_is_ambiguous_without_retry(
    tmp_path: Path, media_type: str
) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        publish_error=asyncio.CancelledError("private media publish cancellation"),
        container_statuses=("FINISHED",),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, "https://media.example/media.bin")

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "status", "publish"]
    assert api.calls.count("publish") == 1
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == f"POST_{media_type}"
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert "private media publish cancellation" not in str(caught.value)


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_media_base_exception_after_boundary_still_marks_ambiguous(
    tmp_path: Path, media_type: str
) -> None:
    failure = _InjectedPublishBaseException("private base exception")
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        publish_error=failure,
        container_statuses=("FINISHED",),
    )

    with pytest.raises(_InjectedPublishBaseException):
        await _publish_media(runtime, media_type, "https://media.example/media.bin")

    assert api.calls == ["quota", "create", "status", "publish"]
    assert api.calls.count("publish") == 1
    operation = _only_operation(operations, tmp_path)
    assert operation.kind == f"POST_{media_type}"
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == "PUBLISH_OUTCOME_AMBIGUOUS"
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    assert "private base exception" not in journal


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_invalid_published_media_id_is_ambiguous(tmp_path: Path, media_type: str) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        container_statuses=("FINISHED",),
        media_id="../unsafe",
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, "https://media.example/media.bin")

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "status", "publish"]
    assert api.calls.count("publish") == 1
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_media_publish_requested_is_durable_before_one_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_type: str,
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, container_statuses=("FINISHED",))
    original_update = operations.update

    def fail_publish_requested(operation: LocalOperation) -> LocalOperation:
        if operation.phase == "PUBLISH_REQUESTED":
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_publish_requested)

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, "https://media.example/media.bin")

    assert caught.value.code == "OPERATION_STATE_INVALID"
    assert api.calls == ["quota", "create", "status"]
    operation = _only_operation(operations, tmp_path)
    assert operation.kind == f"POST_{media_type}"
    assert operation.phase == "CONTAINER_CREATED"


@pytest.mark.parametrize("media_type", ["IMAGE", "VIDEO"])
@pytest.mark.asyncio
async def test_media_final_journal_failure_after_publish_is_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_type: str,
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, container_statuses=("FINISHED",))
    original_update = operations.update

    def fail_terminal_updates(operation: LocalOperation) -> LocalOperation:
        if operation.phase in {"PUBLISHED", "AMBIGUOUS"}:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_terminal_updates)

    with pytest.raises(StandaloneMutationError) as caught:
        await _publish_media(runtime, media_type, "https://media.example/media.bin")

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "status", "publish"]
    assert api.calls.count("publish") == 1
    assert caught.value.operation_id is not None
    durable = operations.get(caught.value.operation_id)
    assert durable.kind == f"POST_{media_type}"
    assert durable.phase == "PUBLISH_REQUESTED"


@pytest.mark.asyncio
async def test_reply_container_failure_is_final_and_never_publishes(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        create_error=ThreadsContractError(),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.create_reply("alice", "thread-1", _TEXT)

    assert caught.value.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
    assert api.calls == ["quota", "create"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == "CREATE_REPLY"
    assert operation.phase == "FAILED_FINAL"
    assert operation.outcome_code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"


@pytest.mark.asyncio
async def test_reply_container_cancellation_is_final_before_publish_boundary(
    tmp_path: Path,
) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        create_error=asyncio.CancelledError("pre-publish cancel sentinel"),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.create_reply("alice", "thread-1", _TEXT)

    assert caught.value.code == "OPERATION_CANCELLED"
    assert api.calls == ["quota", "create"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.phase == "FAILED_FINAL"
    assert operation.outcome_code == "OPERATION_CANCELLED"
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    assert "pre-publish cancel sentinel" not in journal


@pytest.mark.asyncio
async def test_success_has_durable_phase_order_exact_text_request_and_one_publish(
    tmp_path: Path,
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path)

    result = await runtime.publish_text("alice", _TEXT)

    assert api.calls == ["quota", "create", "publish"]
    assert api.events == [("quota", None), ("create", "RECEIVED"), ("publish", "PUBLISH_REQUESTED")]
    assert api.create_request == MediaContainerRequest(media_type="TEXT", text=_TEXT)
    assert result.media_id == "media-123"
    operation = operations.get(result.operation_id)
    assert operation.phase == "PUBLISHED"
    assert operation.container_id == "container-123"
    assert operation.media_id == "media-123"
    assert operation.outcome_code is None


@pytest.mark.asyncio
async def test_journal_contains_no_post_text_token_or_credential_reference(
    tmp_path: Path,
) -> None:
    _, _, _, _, runtime, _ = _setup(tmp_path)

    await runtime.publish_text("alice", _TEXT)

    journal = next((tmp_path / "operations").glob("*.json")).read_text(encoding="utf-8")
    for forbidden in (_TEXT, _TOKEN, _CREDENTIAL_REF, "Authorization"):
        assert forbidden not in journal
    assert set(json.loads(journal)) == {
        "version",
        "id",
        "account_id",
        "kind",
        "phase",
        "container_id",
        "media_id",
    }
    assert len(tuple((tmp_path / "operations").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_pre_publish_api_failure_is_final_and_never_publishes(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, create_error=ThreadsContractError())

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_text("alice", _TEXT)

    assert caught.value.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
    assert api.calls == ["quota", "create"]
    operation_id = caught.value.operation_id
    assert operation_id is not None
    operation = operations.get(operation_id)
    assert operation.phase == "FAILED_FINAL"
    assert operation.outcome_code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"


@pytest.mark.asyncio
async def test_invalid_container_id_fails_final_without_publish(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, container_id="../unsafe")

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_text("alice", _TEXT)

    assert caught.value.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
    operation_id = caught.value.operation_id
    assert operation_id is not None
    operation = operations.get(operation_id)
    assert operation.phase == "FAILED_FINAL"
    assert operation.container_id is None
    assert api.calls == ["quota", "create"]


@pytest.mark.parametrize(
    "error",
    [
        ThreadsAPIError("THREADS_RATE_LIMITED"),
        ThreadsTransportError(),
        ThreadsContractError(),
        RuntimeError("raw response sentinel"),
    ],
)
@pytest.mark.asyncio
async def test_publish_failure_is_ambiguous_without_retry_or_reconciliation(
    tmp_path: Path, error: Exception
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, publish_error=error)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_text("alice", _TEXT)

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "publish"]
    operation_id = caught.value.operation_id
    assert operation_id is not None
    operation = operations.get(operation_id)
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == getattr(error, "code", "THREADS_TRANSPORT_FAILURE")
    assert (
        "raw response sentinel"
        not in (tmp_path / "operations" / f"{operation.id}.json").read_text()
    )


@pytest.mark.parametrize(
    "error",
    [
        ThreadsAPIError("THREADS_RATE_LIMITED"),
        ThreadsTransportError(),
        ThreadsContractError(),
        RuntimeError("reply response sentinel"),
    ],
)
@pytest.mark.asyncio
async def test_reply_publish_failure_is_ambiguous_without_retry_or_reconciliation(
    tmp_path: Path,
    error: Exception,
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, publish_error=error)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.create_reply("alice", "thread-1", _TEXT, parent_reply_id="reply-2")

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "publish"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == "CREATE_REPLY"
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == getattr(error, "code", "THREADS_TRANSPORT_FAILURE")
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    assert "reply response sentinel" not in journal


@pytest.mark.asyncio
async def test_publish_cancellation_is_ambiguous_without_retry_or_reconciliation(
    tmp_path: Path,
) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path, publish_error=asyncio.CancelledError("cancel sentinel")
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_text("alice", _TEXT)

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    operation_id = caught.value.operation_id
    assert operation_id is not None
    assert api.calls == ["quota", "create", "publish"]
    assert api.events == [
        ("quota", None),
        ("create", "RECEIVED"),
        ("publish", "PUBLISH_REQUESTED"),
    ]
    operation = operations.get(operation_id)
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == "PUBLISH_OUTCOME_AMBIGUOUS"
    journal = (tmp_path / "operations" / f"{operation_id}.json").read_text(encoding="utf-8")
    assert "cancel sentinel" not in journal


@pytest.mark.asyncio
async def test_reply_publish_cancellation_is_ambiguous_without_retry(
    tmp_path: Path,
) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        publish_error=asyncio.CancelledError("reply cancel sentinel"),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.create_reply("alice", "thread-1", _TEXT)

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "publish"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == "CREATE_REPLY"
    assert operation.phase == "AMBIGUOUS"
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    assert "reply cancel sentinel" not in journal


@pytest.mark.asyncio
async def test_ambiguous_publish_outcome_wins_over_lock_release_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path, publish_error=ThreadsAPIError("THREADS_RATE_LIMITED")
    )
    real_release = FilesystemProcessLock.release

    def release_then_fail(lock: FilesystemProcessLock) -> None:
        real_release(lock)
        raise OSError("cleanup failure")

    monkeypatch.setattr(FilesystemProcessLock, "release", release_then_fail)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_text("alice", _TEXT)

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    operation_id = caught.value.operation_id
    assert operation_id is not None
    assert api.calls == ["quota", "create", "publish"]
    assert operations.get(operation_id).phase == "AMBIGUOUS"


@pytest.mark.asyncio
async def test_durable_published_result_wins_over_lock_release_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path)
    real_release = FilesystemProcessLock.release

    def release_then_fail(lock: FilesystemProcessLock) -> None:
        real_release(lock)
        raise OSError("cleanup failure")

    monkeypatch.setattr(FilesystemProcessLock, "release", release_then_fail)

    result = await runtime.publish_text("alice", _TEXT)

    assert api.calls == ["quota", "create", "publish"]
    assert result.media_id == "media-123"
    operation = operations.get(result.operation_id)
    assert operation.phase == "PUBLISHED"
    assert operation.media_id == "media-123"


@pytest.mark.asyncio
async def test_invalid_media_id_after_publish_is_ambiguous(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, media_id="../unsafe")

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_text("alice", _TEXT)

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "publish"]
    operation_id = caught.value.operation_id
    assert operation_id is not None
    operation = operations.get(operation_id)
    assert operation.phase == "AMBIGUOUS"
    assert operation.media_id is None
    assert operation.outcome_code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"


@pytest.mark.asyncio
async def test_invalid_published_reply_id_is_ambiguous(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path, media_id="../unsafe")

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.create_reply("alice", "thread-1", _TEXT)

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "publish"]
    assert caught.value.operation_id is not None
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == "CREATE_REPLY"
    assert operation.phase == "AMBIGUOUS"
    assert operation.media_id is None
    assert operation.outcome_code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"


def test_required_journal_write_before_publish_prevents_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path)
    original_update = operations.update

    def fail_publish_requested(operation: LocalOperation) -> LocalOperation:
        if operation.phase == "PUBLISH_REQUESTED":
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_publish_requested)

    with pytest.raises(StandaloneMutationError) as caught:
        import asyncio

        asyncio.run(runtime.publish_text("alice", _TEXT))

    assert caught.value.code == "OPERATION_STATE_INVALID"
    assert api.calls == ["quota", "create"]
    operation = _only_operation(operations, tmp_path)
    assert operation.phase == "CONTAINER_CREATED"


@pytest.mark.asyncio
async def test_reply_requires_durable_publish_requested_before_remote_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path)
    original_update = operations.update

    def fail_publish_requested(operation: LocalOperation) -> LocalOperation:
        if operation.phase == "PUBLISH_REQUESTED":
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_publish_requested)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.create_reply("alice", "thread-1", _TEXT)

    assert caught.value.code == "OPERATION_STATE_INVALID"
    assert api.calls == ["quota", "create"]
    operation = _only_operation(operations, tmp_path)
    assert operation.kind == "CREATE_REPLY"
    assert operation.phase == "CONTAINER_CREATED"


@pytest.mark.asyncio
async def test_final_journal_failure_is_ambiguous_and_keeps_last_durable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path)
    original_update = operations.update

    def fail_terminal_updates(operation: LocalOperation) -> LocalOperation:
        if operation.phase in {"PUBLISHED", "AMBIGUOUS"}:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_terminal_updates)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_text("alice", _TEXT)

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "publish"]
    operation_id = caught.value.operation_id
    assert operation_id is not None
    durable = operations.get(operation_id)
    assert durable.phase == "PUBLISH_REQUESTED"
    assert durable.media_id is None


@pytest.mark.asyncio
async def test_final_reply_journal_failure_is_ambiguous_and_keeps_requested_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, operations, api, _, runtime, _ = _setup(tmp_path)
    original_update = operations.update

    def fail_terminal_updates(operation: LocalOperation) -> LocalOperation:
        if operation.phase in {"PUBLISHED", "AMBIGUOUS"}:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_terminal_updates)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.create_reply("alice", "thread-1", _TEXT)

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls == ["quota", "create", "publish"]
    assert caught.value.operation_id is not None
    durable = operations.get(caught.value.operation_id)
    assert durable.kind == "CREATE_REPLY"
    assert durable.phase == "PUBLISH_REQUESTED"
    assert durable.media_id is None


@pytest.mark.parametrize(
    "mutation_case",
    [
        "unknown-key",
        "boolean-version",
        "unsupported-version",
        "malformed-uuid",
        "unsupported-kind",
        "unsupported-phase",
        "invalid-container-id",
        "unsafe-outcome-code",
        "invalid-published-shape",
    ],
)
def test_malformed_or_unknown_journal_state_fails_closed(
    tmp_path: Path, mutation_case: str
) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    path = tmp_path / "operations" / f"{operation.id}.json"
    original = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    malformed = dict(original)
    if mutation_case == "unknown-key":
        malformed["extra"] = "not allowed"
    elif mutation_case == "boolean-version":
        malformed["version"] = True
    elif mutation_case == "unsupported-version":
        malformed["version"] = 2
    elif mutation_case == "malformed-uuid":
        malformed["id"] = "not-a-uuid"
    elif mutation_case == "unsupported-kind":
        malformed["kind"] = "REPLY"
    elif mutation_case == "unsupported-phase":
        malformed["phase"] = "UNKNOWN"
    elif mutation_case == "invalid-container-id":
        malformed["container_id"] = "../unsafe"
    elif mutation_case == "unsafe-outcome-code":
        malformed["outcome_code"] = "bad code"
    else:
        malformed["phase"] = "PUBLISHED"
        malformed["container_id"] = "c"
    path.write_text(json.dumps(malformed), encoding="utf-8")

    with pytest.raises(StandaloneMutationError) as caught:
        store.get(operation.id)

    assert caught.value.code == "OPERATION_STATE_INVALID"


def test_oversized_journal_fails_closed(tmp_path: Path) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    (tmp_path / "operations" / f"{operation.id}.json").write_text(" " * 5000, encoding="utf-8")
    with pytest.raises(StandaloneMutationError) as caught:
        store.get(operation.id)
    assert caught.value.code == "OPERATION_STATE_INVALID"


def test_initial_journal_create_never_overwrites_an_existing_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    monkeypatch.setattr(mutation_module, "uuid4", lambda: operation_id)
    store = LocalOperationStore(tmp_path)
    account_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    first = store.create_received(account_id)
    assert first.id == operation_id
    path = tmp_path / "operations" / f"{operation_id}.json"
    original = path.read_bytes()

    with pytest.raises(StandaloneMutationError) as caught:
        store.create_received(UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))

    assert caught.value.code == "OPERATION_STATE_INVALID"
    assert path.read_bytes() == original


def test_atomic_create_and_update_failures_preserve_valid_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalOperationStore(tmp_path)
    original_link = mutation_module.os.link

    def fail_link(_source: object, _destination: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(mutation_module.os, "link", fail_link)
    with pytest.raises(StandaloneMutationError):
        store.create_received(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert not tuple((tmp_path / "operations").glob("*.json"))
    assert not tuple((tmp_path / "operations").glob("*.tmp"))

    monkeypatch.setattr(mutation_module.os, "link", original_link)
    operation = store.create_received(UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError

    monkeypatch.setattr(mutation_module.os, "replace", fail_replace)
    with pytest.raises(StandaloneMutationError):
        store.update(
            LocalOperation(
                version=1,
                id=operation.id,
                account_id=operation.account_id,
                kind="POST_TEXT",
                phase="FAILED_FINAL",
                outcome_code="THREADS_RATE_LIMITED",
            )
        )
    assert store.get(operation.id).phase == "RECEIVED"
    assert not tuple((tmp_path / "operations").glob("*.tmp"))


@pytest.mark.asyncio
async def test_same_account_lock_conflict_is_bounded_and_lock_releases_after_success(
    tmp_path: Path,
) -> None:
    _, _, api, _, runtime, account_id = _setup(tmp_path)
    held = FilesystemProcessLock(tmp_path / "locks" / f"{account_id}.lock")
    held.acquire()
    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_text("alice", _TEXT)
    assert caught.value.code == "ACCOUNT_BUSY"
    with pytest.raises(StandaloneMutationError) as reply_caught:
        await runtime.create_reply("alice", "thread-1", _TEXT)
    assert reply_caught.value.code == "ACCOUNT_BUSY"
    for media_type in ("IMAGE", "VIDEO"):
        with pytest.raises(StandaloneMutationError) as media_caught:
            await _publish_media(runtime, media_type, "https://media.example/media.bin")
        assert media_caught.value.code == "ACCOUNT_BUSY"
    assert api.calls == []
    held.release()

    await runtime.publish_text("alice", _TEXT)
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account_id}.lock")
    probe.acquire()
    probe.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["credential", "quota", "create", "publish"])
async def test_account_lock_is_released_on_error_paths(tmp_path: Path, failure: str) -> None:
    error = ThreadsAPIError("THREADS_RATE_LIMITED")
    resolver = _FakeResolver(error=error) if failure == "credential" else _FakeResolver()
    _, _, _, _, runtime, account_id = _setup(
        tmp_path,
        resolver=resolver,
        quota=PublishingQuota(usage=1, total=1) if failure == "quota" else None,
        create_error=error if failure == "create" else None,
        publish_error=error if failure == "publish" else None,
    )
    with pytest.raises((StandaloneMutationError, ThreadsAPIError)):
        await runtime.publish_text("alice", _TEXT)
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account_id}.lock")
    probe.acquire()
    probe.release()


def test_operation_get_is_local_bounded_and_rejects_missing_id(tmp_path: Path) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert store.get(operation.id) == operation
    with pytest.raises(StandaloneMutationError) as caught:
        store.get(UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"))
    assert caught.value.code == "OPERATION_NOT_FOUND"


@pytest.mark.parametrize(
    ("current_kind", "replacement_kind"),
    [("POST_TEXT", "CREATE_REPLY"), ("CREATE_REPLY", "POST_TEXT")],
)
def test_operation_update_rejects_kind_changes(
    tmp_path: Path, current_kind: str, replacement_kind: str
) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        kind=current_kind,
    )
    replacement = LocalOperation(
        version=operation.version,
        id=operation.id,
        account_id=operation.account_id,
        kind=replacement_kind,
        phase="FAILED_FINAL",
        outcome_code="TEST_FINAL_FAILURE",
    )

    with pytest.raises(StandaloneMutationError) as caught:
        store.update(replacement)

    assert caught.value.code == "OPERATION_STATE_INVALID"
    assert store.get(operation.id) == operation


@pytest.mark.parametrize("kind", ["POST_TEXT", "CREATE_REPLY"])
def test_operation_update_allows_normal_same_kind_transitions(tmp_path: Path, kind: str) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), kind=kind)
    container_created = LocalOperation(
        version=operation.version,
        id=operation.id,
        account_id=operation.account_id,
        kind=kind,
        phase="CONTAINER_CREATED",
        container_id="container-123",
    )
    publish_requested = LocalOperation(
        version=operation.version,
        id=operation.id,
        account_id=operation.account_id,
        kind=kind,
        phase="PUBLISH_REQUESTED",
        container_id="container-123",
    )
    published = LocalOperation(
        version=operation.version,
        id=operation.id,
        account_id=operation.account_id,
        kind=kind,
        phase="PUBLISHED",
        container_id="container-123",
        media_id="media-123",
    )

    assert store.update(container_created) == container_created
    assert store.update(publish_requested) == publish_requested
    assert store.update(published) == published
    assert store.get(operation.id) == published


def test_post_text_journal_remains_readable_with_shared_reply_journal(tmp_path: Path) -> None:
    store = LocalOperationStore(tmp_path)
    operation_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    account_id = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    operations_path = tmp_path / "operations"
    operations_path.mkdir()
    legacy_path = operations_path / f"{operation_id}.json"
    legacy_reply_id = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    legacy_reply_path = operations_path / f"{legacy_reply_id}.json"
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "id": str(operation_id),
                "account_id": str(account_id),
                "kind": "POST_TEXT",
                "phase": "PUBLISHED",
                "container_id": "container-legacy",
                "media_id": "media-legacy",
            }
        ),
        encoding="utf-8",
    )
    legacy_reply_path.write_text(
        json.dumps(
            {
                "version": 1,
                "id": str(legacy_reply_id),
                "account_id": str(account_id),
                "kind": "CREATE_REPLY",
                "phase": "PUBLISHED",
                "container_id": "container-reply-legacy",
                "media_id": "media-reply-legacy",
            }
        ),
        encoding="utf-8",
    )

    legacy = store.get(operation_id)
    legacy_reply = store.get(legacy_reply_id)
    reply = store.create_received(account_id, kind="CREATE_REPLY")
    image = store.create_received(account_id, kind="POST_IMAGE")
    video = store.create_received(account_id, kind="POST_VIDEO")

    assert legacy.kind == "POST_TEXT"
    assert legacy.phase == "PUBLISHED"
    assert legacy.media_id == "media-legacy"
    assert legacy_reply.kind == "CREATE_REPLY"
    assert legacy_reply.phase == "PUBLISHED"
    assert legacy_reply.media_id == "media-reply-legacy"
    assert reply.version == 1
    assert reply.kind == "CREATE_REPLY"
    assert reply.phase == "RECEIVED"
    for media_operation, kind in ((image, "POST_IMAGE"), (video, "POST_VIDEO")):
        assert media_operation.version == 1
        assert media_operation.kind == kind
        assert media_operation.phase == "RECEIVED"
    for operation in (reply, image, video):
        assert set(json.loads((operations_path / f"{operation.id}.json").read_text())) == {
            "version",
            "id",
            "account_id",
            "kind",
            "phase",
        }
