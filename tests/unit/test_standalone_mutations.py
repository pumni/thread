from __future__ import annotations

import asyncio
import json
from dataclasses import replace
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
    ThreadsCredentialErrorCode,
    ThreadsCredentialSecretResolver,
    ThreadsTransportError,
)
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.infrastructure.threads_api.environment_credentials import (
    EnvironmentThreadsCredentialSecretResolver,
)
from threads_platform.standalone.accounts import LocalAccountStore, StandaloneAccountError
from threads_platform.standalone.mutations import (
    CarouselItem,
    CarouselManifest,
    LocalOperation,
    LocalOperationStore,
    LocalThreadsMutationRuntime,
    PublishedMediaResult,
    StandaloneMutationError,
    load_carousel_manifest,
)

_TOKEN = "token-sentinel-never-journal-this"
_CREDENTIAL_REF = "env://THREADS_PLATFORM_THREADS_TOKEN_TEST"
_TEXT = "text-sentinel-never-journal-this"


class _InjectedPublishBaseException(BaseException):
    pass


class _InjectedModerationBaseException(BaseException):
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


class _ModerationFakeAPI:
    def __init__(
        self,
        root: Path,
        operations: LocalOperationStore,
        *,
        failure: BaseException | None = None,
        response: object = None,
    ) -> None:
        self.root = root
        self.operations = operations
        self.failure = failure
        self.response = response
        self.calls: list[tuple[str, str | None, bool | None]] = []
        self.snapshots: list[LocalOperation] = []

    async def get_publishing_quota(self, _token: SecretStr) -> PublishingQuota:
        self.calls.append(("quota", None, None))
        raise AssertionError("moderation does not use publishing quota")

    async def manage_reply(self, token: SecretStr, reply_id: str, *, hide: bool) -> object:
        self._record(token, reply_id)
        self.calls.append(("manage_reply", reply_id, hide))
        self._raise_failure()
        return self.response

    async def manage_pending_reply(
        self, token: SecretStr, reply_id: str, *, approve: bool
    ) -> object:
        self._record(token, reply_id)
        self.calls.append(("manage_pending_reply", reply_id, approve))
        self._raise_failure()
        return self.response

    def _record(self, token: SecretStr, reply_id: str) -> None:
        assert token.get_secret_value() == _TOKEN
        operation = _only_operation(self.operations, self.root)
        assert operation.kind == "MODERATE_REPLY"
        assert operation.phase == "MUTATION_REQUESTED"
        assert operation.action in {"hide", "unhide", "approve", "ignore"}
        assert reply_id
        self.snapshots.append(operation)

    def _raise_failure(self) -> None:
        if self.failure is not None:
            raise self.failure


class _CarouselFakeAPI:
    def __init__(
        self,
        root: Path,
        operations: LocalOperationStore,
        *,
        quota: PublishingQuota | None = None,
        child_ids: tuple[str, ...] = ("child-image", "child-video"),
        parent_id: str = "carousel-parent",
        statuses: dict[str, tuple[str | None, ...]] | None = None,
        status_errors: dict[str, BaseException] | None = None,
        create_errors: dict[int, BaseException] | None = None,
        parent_create_error: BaseException | None = None,
        response_ids: dict[str, str] | None = None,
        publish_error: BaseException | None = None,
        media_id: str = "carousel-media",
    ) -> None:
        self.root = root
        self.operations = operations
        self.quota = quota or PublishingQuota(usage=1, total=100)
        self.child_ids = child_ids
        self.parent_id = parent_id
        self.statuses = statuses or {}
        self.status_errors = status_errors or {}
        self.create_errors = create_errors or {}
        self.parent_create_error = parent_create_error
        self.response_ids = response_ids or {}
        self.publish_error = publish_error
        self.media_id = media_id
        self.calls: list[str] = []
        self.child_requests: list[MediaContainerRequest] = []
        self.child_create_snapshots: list[LocalOperation] = []
        self.parent_request: MediaContainerRequest | None = None
        self.parent_create_snapshot: LocalOperation | None = None
        self.status_check_counts: dict[str, int] = {}
        self.finished_children: set[str] = set()

    async def get_publishing_quota(self, token: SecretStr) -> PublishingQuota:
        assert token.get_secret_value() == _TOKEN
        self.calls.append("quota")
        return self.quota

    async def create_container(
        self, token: SecretStr, request: MediaContainerRequest
    ) -> MediaContainer:
        assert token.get_secret_value() == _TOKEN
        operation = _only_operation(self.operations, self.root)
        if request.media_type == "CAROUSEL":
            self.calls.append("parent-create")
            self.parent_request = request
            self.parent_create_snapshot = operation
            assert operation.phase == "CHILDREN_CREATING"
            assert operation.child_container_ids == self.child_ids[: len(self.child_requests)]
            assert set(operation.child_container_ids) <= self.finished_children
            if self.parent_create_error is not None:
                raise self.parent_create_error
            return MediaContainer(container_id=self.parent_id)

        index = len(self.child_requests)
        self.calls.append(f"child-create:{index}")
        self.child_requests.append(request)
        self.child_create_snapshots.append(operation)
        assert request.media_type in {"IMAGE", "VIDEO"}
        assert operation.phase == "CHILDREN_CREATING"
        assert operation.child_container_ids == self.child_ids[:index]
        if index in self.create_errors:
            raise self.create_errors[index]
        if index >= len(self.child_ids):
            raise AssertionError("unexpected additional child container request")
        return MediaContainer(container_id=self.child_ids[index])

    async def get_container(self, token: SecretStr, container_id: str) -> MediaContainer:
        assert token.get_secret_value() == _TOKEN
        operation = _only_operation(self.operations, self.root)
        if operation.phase == "CONTAINER_CREATED" and container_id == self.parent_id:
            assert operation.container_id == container_id
            assert operation.phase == "CONTAINER_CREATED"
        else:
            assert container_id in operation.child_container_ids
        self.calls.append(f"status:{container_id}")
        if container_id in self.status_errors:
            raise self.status_errors[container_id]
        check_index = self.status_check_counts.get(container_id, 0)
        self.status_check_counts[container_id] = check_index + 1
        statuses = self.statuses.get(container_id, ("FINISHED",))
        status = statuses[min(check_index, len(statuses) - 1)]
        response_id = self.response_ids.get(container_id, container_id)
        if container_id in self.child_ids and status == "FINISHED":
            self.finished_children.add(container_id)
        return MediaContainer(container_id=response_id, status=status)

    async def publish_container(self, token: SecretStr, container_id: str) -> str:
        assert token.get_secret_value() == _TOKEN
        operation = _only_operation(self.operations, self.root)
        assert container_id == self.parent_id
        assert operation.container_id == container_id
        assert operation.phase == "PUBLISH_REQUESTED"
        self.calls.append("publish")
        if self.publish_error is not None:
            raise self.publish_error
        return self.media_id

    async def get_media(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("media reconciliation is forbidden")


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


def _carousel_manifest(
    items: tuple[CarouselItem, ...] | None = None,
    *,
    text: str | None = "carousel text sentinel",
) -> CarouselManifest:
    return CarouselManifest(
        items=(
            items
            if items is not None
            else (
                CarouselItem("IMAGE", "https://media.example/first.jpg", "first alt"),
                CarouselItem("VIDEO", "https://media.example/second.mp4", "second alt"),
            )
        ),
        text=text,
    )


def _setup_carousel(
    root: Path,
    *,
    quota: PublishingQuota | None = None,
    resolver: _FakeResolver | None = None,
    child_ids: tuple[str, ...] = ("child-image", "child-video"),
    parent_id: str = "carousel-parent",
    statuses: dict[str, tuple[str | None, ...]] | None = None,
    status_errors: dict[str, BaseException] | None = None,
    create_errors: dict[int, BaseException] | None = None,
    parent_create_error: BaseException | None = None,
    response_ids: dict[str, str] | None = None,
    publish_error: BaseException | None = None,
    media_id: str = "carousel-media",
) -> tuple[
    _CountingAccounts,
    LocalOperationStore,
    _CarouselFakeAPI,
    _FakeResolver,
    LocalThreadsMutationRuntime,
    UUID,
]:
    accounts = _CountingAccounts(root)
    account = accounts.add("alice")
    accounts.set_credential_ref("alice", _CREDENTIAL_REF)
    operations = LocalOperationStore(root)
    secret_resolver = resolver or _FakeResolver()
    api = _CarouselFakeAPI(
        root,
        operations,
        quota=quota,
        child_ids=child_ids,
        parent_id=parent_id,
        statuses=statuses,
        status_errors=status_errors,
        create_errors=create_errors,
        parent_create_error=parent_create_error,
        response_ids=response_ids,
        publish_error=publish_error,
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


def _setup_moderation(
    root: Path,
    *,
    resolver: _FakeResolver | None = None,
    credential: bool = True,
    failure: BaseException | None = None,
    response: object = None,
) -> tuple[
    _CountingAccounts,
    LocalOperationStore,
    _ModerationFakeAPI,
    _FakeResolver,
    LocalThreadsMutationRuntime,
    UUID,
]:
    accounts = _CountingAccounts(root)
    account = accounts.add("alice")
    if credential:
        accounts.set_credential_ref("alice", _CREDENTIAL_REF)
    accounts.get_calls = 0
    operations = LocalOperationStore(root)
    secret_resolver = resolver or _FakeResolver()
    api = _ModerationFakeAPI(root, operations, failure=failure, response=response)
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


def _operation_from_error(
    store: LocalOperationStore, error: StandaloneMutationError
) -> LocalOperation:
    assert error.operation_id is not None
    return store.get(error.operation_id)


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


def test_carousel_manifest_load_preserves_order_and_optional_fields(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "text": "caption sentinel",
                "items": [
                    {
                        "media_type": "VIDEO",
                        "url": "https://media.example/first.mp4",
                        "alt_text": "first alt",
                    },
                    {"media_type": "IMAGE", "url": "https://media.example/second.jpg"},
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = load_carousel_manifest(path)

    assert manifest == CarouselManifest(
        items=(
            CarouselItem("VIDEO", "https://media.example/first.mp4", "first alt"),
            CarouselItem("IMAGE", "https://media.example/second.jpg"),
        ),
        text="caption sentinel",
    )


@pytest.mark.parametrize(
    "invalid_file",
    ["missing", "directory", "oversized", "non-utf8", "invalid-json", "deep"],
)
def test_invalid_carousel_manifest_files_fail_closed(tmp_path: Path, invalid_file: str) -> None:
    path = tmp_path / "manifest.json"
    if invalid_file == "directory":
        path.mkdir()
    elif invalid_file == "oversized":
        path.write_bytes(b" " * 65_537)
    elif invalid_file == "non-utf8":
        path.write_bytes(b"\xff")
    elif invalid_file == "invalid-json":
        path.write_bytes(b"{")
    elif invalid_file == "deep":
        path.write_text(
            '{"items":[{"media_type":"IMAGE","url":"https://media.example/a.jpg",'
            '"extra":{"a":{"b":1}}},{"media_type":"VIDEO",'
            '"url":"https://media.example/b.mp4"}]}',
            encoding="utf-8",
        )

    with pytest.raises(StandaloneMutationError) as caught:
        load_carousel_manifest(path)

    assert caught.value.code == "INVALID_CAROUSEL_MANIFEST"
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize(
    "document",
    [
        '{"items":[],"items":[]}',
        '{"items":[{"media_type":"IMAGE","media_type":"VIDEO",'
        '"url":"https://media.example/a.jpg"},'
        '{"media_type":"IMAGE","url":"https://media.example/b.jpg"}]}',
    ],
)
def test_duplicate_carousel_manifest_keys_are_rejected(tmp_path: Path, document: str) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(StandaloneMutationError) as caught:
        load_carousel_manifest(path)

    assert caught.value.code == "INVALID_CAROUSEL_MANIFEST"


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"text": "caption"},
        {"items": [], "unexpected": True},
        {"items": {}},
        {"items": ["first", "second"]},
        {"items": [{"media_type": "IMAGE", "url": "https://media.example/a.jpg"}]},
        {
            "items": [
                {"media_type": "IMAGE", "url": "https://media.example/a.jpg", "extra": 1},
                {"media_type": "IMAGE", "url": "https://media.example/b.jpg"},
            ]
        },
        {
            "items": [
                {"media_type": "IMAGE", "url": "https://media.example/a.jpg"} for _ in range(21)
            ]
        },
    ],
)
def test_carousel_manifest_rejects_wrong_root_shape_and_item_count(
    tmp_path: Path, document: object
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StandaloneMutationError) as caught:
        load_carousel_manifest(path)

    assert caught.value.code == "INVALID_CAROUSEL_MANIFEST"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("media_type", True),
        ("media_type", None),
        ("media_type", {"type": "IMAGE"}),
        ("url", True),
        ("url", None),
        ("url", ["https://media.example/image.jpg"]),
        ("alt_text", None),
        ("alt_text", False),
        ("alt_text", {"text": "alt"}),
        ("text", None),
        ("text", True),
        ("text", ["caption"]),
    ],
)
def test_carousel_manifest_rejects_non_string_fields_without_coercion(
    tmp_path: Path, field: str, value: object
) -> None:
    item: dict[str, object] = {
        "media_type": "IMAGE",
        "url": "https://media.example/image.jpg",
    }
    document: dict[str, object] = {
        "items": [item, {"media_type": "VIDEO", "url": "https://media.example/video.mp4"}]
    }
    if field in {"media_type", "url", "alt_text"}:
        item[field] = value
    else:
        document[field] = value
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StandaloneMutationError) as caught:
        load_carousel_manifest(path)

    assert caught.value.code == "INVALID_CAROUSEL_MANIFEST"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("text", " \t ", "INVALID_POST_TEXT"),
        ("text", "x" * 501, "INVALID_POST_TEXT"),
        ("alt_text", "x" * 1001, "INVALID_ALT_TEXT"),
        ("url", "file:///local/image.jpg", "INVALID_MEDIA_URL"),
        ("url", "https://bad host/image.jpg", "INVALID_MEDIA_URL"),
        ("url", "http://127.1/image.jpg", "INVALID_MEDIA_URL"),
    ],
)
def test_carousel_manifest_reuses_bounded_text_alt_and_media_url_validation(
    tmp_path: Path,
    field: str,
    value: str,
    expected_code: str,
) -> None:
    item: dict[str, object] = {
        "media_type": "IMAGE",
        "url": "https://media.example/image.jpg",
    }
    document: dict[str, object] = {
        "items": [item, {"media_type": "VIDEO", "url": "https://media.example/video.mp4"}]
    }
    if field == "url":
        item[field] = value
    elif field == "text":
        document[field] = value
    else:
        item[field] = value
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StandaloneMutationError) as caught:
        load_carousel_manifest(path)

    assert caught.value.code == expected_code


def test_carousel_manifest_rejects_embedded_url_credentials_without_source_literal(
    tmp_path: Path,
) -> None:
    credentialed_url = "https://" + "user" + ":" + "password" + "@media.example/image.jpg"
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "items": [
                    {"media_type": "IMAGE", "url": credentialed_url},
                    {"media_type": "VIDEO", "url": "https://media.example/video.mp4"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StandaloneMutationError) as caught:
        load_carousel_manifest(path)

    assert caught.value.code == "INVALID_MEDIA_URL"


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


@pytest.mark.parametrize(
    ("quote_post_id", "text", "expected_code"),
    [
        ("invalid/remote/id", _TEXT, "INVALID_THREAD_ID"),
        ("quoted-thread-123", " \t ", "INVALID_POST_TEXT"),
        ("quoted-thread-123", "x" * 501, "INVALID_POST_TEXT"),
    ],
)
@pytest.mark.asyncio
async def test_quote_inputs_fail_before_quota_journal_or_api(
    tmp_path: Path,
    quote_post_id: str,
    text: str,
    expected_code: str,
) -> None:
    _, _, api, _, runtime, _ = _setup(tmp_path)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_quote("alice", quote_post_id, text)

    assert caught.value.code == expected_code
    assert api.calls == []
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_publish_quote_uses_v1_journal_and_exact_text_container_request(
    tmp_path: Path,
) -> None:
    quote_post_id = "quoted-thread-target-sentinel"
    commentary = "operator quote commentary sentinel"
    _, operations, api, _, runtime, _ = _setup(tmp_path)

    result = await runtime.publish_quote("alice", quote_post_id, commentary)

    assert api.calls == ["quota", "create", "publish"]
    assert api.events == [
        ("quota", None),
        ("create", "RECEIVED"),
        ("publish", "PUBLISH_REQUESTED"),
    ]
    assert api.create_request == MediaContainerRequest(
        media_type="TEXT",
        text=commentary,
        quote_post_id=quote_post_id,
    )
    operation = operations.get(result.operation_id)
    assert operation.version == 1
    assert operation.kind == "POST_QUOTE"
    assert operation.phase == "PUBLISHED"
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    assert set(json.loads(journal)) == {
        "version",
        "id",
        "account_id",
        "kind",
        "phase",
        "container_id",
        "media_id",
    }
    for secret in (quote_post_id, commentary, _TOKEN, _CREDENTIAL_REF, "Authorization"):
        assert secret not in journal


@pytest.mark.asyncio
async def test_quote_known_quota_exhaustion_creates_no_journal(tmp_path: Path) -> None:
    _, _, api, _, runtime, _ = _setup(
        tmp_path,
        quota=PublishingQuota(usage=100, total=100),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_quote("alice", "quoted-thread-123", _TEXT)

    assert caught.value.code == "THREADS_PUBLISHING_QUOTA_REACHED"
    assert api.calls == ["quota"]
    assert not (tmp_path / "operations").exists()


@pytest.mark.parametrize(
    "publish_error",
    [ThreadsAPIError("THREADS_RATE_LIMITED"), asyncio.CancelledError()],
    ids=["api-error", "cancellation"],
)
@pytest.mark.asyncio
async def test_quote_publish_boundary_uses_existing_ambiguity_semantics_without_retry(
    tmp_path: Path,
    publish_error: BaseException,
) -> None:
    quote_post_id = "quote-target-not-in-journal"
    commentary = "quote-commentary-not-in-journal"
    _, operations, api, _, runtime, _ = _setup(tmp_path, publish_error=publish_error)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_quote("alice", quote_post_id, commentary)

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert caught.value.operation_id is not None
    assert api.calls == ["quota", "create", "publish"]
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == "POST_QUOTE"
    assert operation.phase == "AMBIGUOUS"
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    for secret in (quote_post_id, commentary, _TOKEN, _CREDENTIAL_REF):
        assert secret not in journal


@pytest.mark.asyncio
async def test_quote_create_container_failure_keeps_existing_failed_final_semantics(
    tmp_path: Path,
) -> None:
    _, operations, api, _, runtime, _ = _setup(
        tmp_path,
        create_error=ThreadsTransportError(),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_quote("alice", "quoted-thread-123", _TEXT)

    assert caught.value.code == "THREADS_TRANSPORT_FAILURE"
    assert caught.value.operation_id is not None
    assert api.calls == ["quota", "create"]
    operation = operations.get(caught.value.operation_id)
    assert operation.kind == "POST_QUOTE"
    assert operation.phase == "FAILED_FINAL"


@pytest.mark.parametrize(
    "kind",
    [
        "POST_TEXT",
        "CREATE_REPLY",
        "POST_IMAGE",
        "POST_VIDEO",
        "POST_CAROUSEL",
        "MODERATE_REPLY",
        "POST_QUOTE",
    ],
)
def test_all_v1_journal_kinds_remain_readable(tmp_path: Path, kind: str) -> None:
    store = LocalOperationStore(tmp_path)
    account_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    operation = store.create_received(
        account_id,
        kind=kind,
        action="hide" if kind == "MODERATE_REPLY" else None,
    )

    decoded = store.get(operation.id)
    assert decoded.version == 1
    assert decoded.kind == kind
    assert decoded.phase == "RECEIVED"


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
    (tmp_path / "operations" / f"{operation.id}.json").write_text(
        " " * 8193,
        encoding="utf-8",
    )
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
    legacy_image_id = UUID("11111111-1111-4111-8111-111111111111")
    legacy_video_id = UUID("22222222-2222-4222-8222-222222222222")
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
    for legacy_media_id, kind in (
        (legacy_image_id, "POST_IMAGE"),
        (legacy_video_id, "POST_VIDEO"),
    ):
        (operations_path / f"{legacy_media_id}.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "id": str(legacy_media_id),
                    "account_id": str(account_id),
                    "kind": kind,
                    "phase": "PUBLISHED",
                    "container_id": f"container-{kind.lower()}",
                    "media_id": f"media-{kind.lower()}",
                }
            ),
            encoding="utf-8",
        )

    legacy = store.get(operation_id)
    legacy_reply = store.get(legacy_reply_id)
    legacy_image = store.get(legacy_image_id)
    legacy_video = store.get(legacy_video_id)
    reply = store.create_received(account_id, kind="CREATE_REPLY")
    image = store.create_received(account_id, kind="POST_IMAGE")
    video = store.create_received(account_id, kind="POST_VIDEO")

    assert legacy.kind == "POST_TEXT"
    assert legacy.phase == "PUBLISHED"
    assert legacy.media_id == "media-legacy"
    assert legacy_reply.kind == "CREATE_REPLY"
    assert legacy_reply.phase == "PUBLISHED"
    assert legacy_reply.media_id == "media-reply-legacy"
    assert legacy_image.kind == "POST_IMAGE"
    assert legacy_image.phase == "PUBLISHED"
    assert legacy_video.kind == "POST_VIDEO"
    assert legacy_video.phase == "PUBLISHED"
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


@pytest.mark.asyncio
async def test_carousel_publishes_children_in_order_then_one_parent_and_one_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(
        tmp_path,
        statuses={
            "child-video": ("IN_PROGRESS", "FINISHED"),
            "carousel-parent": ("IN_PROGRESS", "FINISHED"),
        },
    )
    delays = _patch_instant_polling(monkeypatch)
    manifest = _carousel_manifest()

    result = await runtime.publish_carousel("alice", manifest)

    assert api.calls == [
        "quota",
        "child-create:0",
        "status:child-image",
        "child-create:1",
        "status:child-video",
        "status:child-video",
        "parent-create",
        "status:carousel-parent",
        "status:carousel-parent",
        "publish",
    ]
    assert delays == [3.0, 3.0]
    assert api.child_requests == [
        MediaContainerRequest(
            media_type="IMAGE",
            image_url="https://media.example/first.jpg",
            alt_text="first alt",
            is_carousel_item=True,
        ),
        MediaContainerRequest(
            media_type="VIDEO",
            video_url="https://media.example/second.mp4",
            alt_text="second alt",
            is_carousel_item=True,
        ),
    ]
    assert [operation.child_container_ids for operation in api.child_create_snapshots] == [
        (),
        ("child-image",),
    ]
    assert api.parent_create_snapshot is not None
    assert api.parent_create_snapshot.child_container_ids == ("child-image", "child-video")
    assert api.parent_request == MediaContainerRequest(
        media_type="CAROUSEL",
        text="carousel text sentinel",
        children=("child-image", "child-video"),
    )
    assert result.media_id == "carousel-media"
    operation = operations.get(result.operation_id)
    assert operation.version == 1
    assert operation.kind == "POST_CAROUSEL"
    assert operation.phase == "PUBLISHED"
    assert operation.container_id == "carousel-parent"
    assert operation.child_container_ids == ("child-image", "child-video")
    assert operation.media_id == "carousel-media"
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text(encoding="utf-8")
    assert manifest.items[0].alt_text is not None
    assert manifest.items[1].alt_text is not None
    assert manifest.text is not None
    for forbidden in (
        manifest.items[0].url,
        manifest.items[1].url,
        manifest.items[0].alt_text,
        manifest.items[1].alt_text,
        manifest.text,
        _TOKEN,
        _CREDENTIAL_REF,
        "raw API body sentinel",
        "Authorization",
    ):
        assert forbidden not in journal
    assert api.calls.count("publish") == 1


@pytest.mark.asyncio
async def test_carousel_journal_bound_holds_twenty_maximum_length_child_ids(
    tmp_path: Path,
) -> None:
    child_ids = tuple(f"{index:02d}" + "x" * 253 for index in range(20))
    items = tuple(
        CarouselItem("IMAGE", f"https://media.example/{index}.jpg") for index in range(20)
    )
    _, operations, _, _, runtime, _ = _setup_carousel(
        tmp_path,
        child_ids=child_ids,
        parent_id="p" * 255,
        media_id="m" * 255,
    )

    result = await runtime.publish_carousel("alice", _carousel_manifest(items, text=None))

    operation = operations.get(result.operation_id)
    journal_path = tmp_path / "operations" / f"{operation.id}.json"
    assert operation.child_container_ids == child_ids
    assert journal_path.stat().st_size > 4096
    assert journal_path.stat().st_size <= 8192


@pytest.mark.asyncio
async def test_carousel_validates_manifest_before_account_lock_or_credential(
    tmp_path: Path,
) -> None:
    accounts = _CountingAccounts(tmp_path)
    operations = LocalOperationStore(tmp_path)
    resolver = _FakeResolver()
    api = _CarouselFakeAPI(tmp_path, operations)
    runtime = LocalThreadsMutationRuntime(
        tmp_path,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, resolver),
        operations,
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel(
            "missing",
            _carousel_manifest((CarouselItem("IMAGE", "file:///local.jpg"),)),
        )

    assert caught.value.code == "INVALID_CAROUSEL_MANIFEST"
    assert accounts.get_calls == 0
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "locks").exists()
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_carousel_uses_normal_quota_and_exhaustion_creates_no_journal_or_child(
    tmp_path: Path,
) -> None:
    _, _, api, resolver, runtime, _ = _setup_carousel(
        tmp_path,
        quota=PublishingQuota(usage=20, total=20, reply_usage=0, reply_total=500),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "THREADS_PUBLISHING_QUOTA_REACHED"
    assert api.calls == ["quota"]
    assert resolver.calls == [_CREDENTIAL_REF]
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_carousel_missing_and_unavailable_credentials_stop_before_quota(
    tmp_path: Path,
) -> None:
    accounts = LocalAccountStore(tmp_path)
    accounts.add("alice")
    operations = LocalOperationStore(tmp_path)
    api = _CarouselFakeAPI(tmp_path, operations)
    resolver = _FakeResolver()
    runtime = LocalThreadsMutationRuntime(
        tmp_path,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, resolver),
        operations,
    )

    with pytest.raises(ThreadsCredentialError) as missing:
        await runtime.publish_carousel("alice", _carousel_manifest())
    assert missing.value.code == "THREADS_CREDENTIAL_NOT_CONFIGURED"
    assert api.calls == []

    accounts.set_credential_ref("alice", _CREDENTIAL_REF)
    unavailable = _FakeResolver(
        error=ThreadsCredentialError(ThreadsCredentialErrorCode.SECRET_UNAVAILABLE)
    )
    runtime = LocalThreadsMutationRuntime(
        tmp_path,
        accounts,
        cast(ThreadsAPI, api),
        cast(ThreadsCredentialSecretResolver, unavailable),
        operations,
    )
    with pytest.raises(ThreadsCredentialError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())
    assert caught.value.code == "THREADS_CREDENTIAL_SECRET_UNAVAILABLE"
    assert api.calls == []
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_carousel_unknown_quota_does_not_invent_exhaustion(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(
        tmp_path,
        quota=PublishingQuota(usage=None, total=0),
    )

    result = await runtime.publish_carousel("alice", _carousel_manifest())

    assert api.calls[0] == "quota"
    assert result.media_id == "carousel-media"
    assert operations.get(result.operation_id).phase == "PUBLISHED"


@pytest.mark.asyncio
async def test_carousel_uses_same_account_lock_and_releases_it(tmp_path: Path) -> None:
    _, _, api, _, runtime, account_id = _setup_carousel(tmp_path)
    held = FilesystemProcessLock(tmp_path / "locks" / f"{account_id}.lock")
    held.acquire()

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "ACCOUNT_BUSY"
    assert api.calls == []
    held.release()

    await runtime.publish_carousel("alice", _carousel_manifest())
    probe = FilesystemProcessLock(tmp_path / "locks" / f"{account_id}.lock")
    probe.acquire()
    probe.release()


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("ERROR", "THREADS_CONTAINER_ERROR"),
        ("EXPIRED", "THREADS_CONTAINER_EXPIRED"),
        ("UNKNOWN", "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
        (None, "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
    ],
)
@pytest.mark.asyncio
async def test_carousel_child_status_failure_is_final_before_next_child_or_parent(
    tmp_path: Path,
    status: str | None,
    expected_code: str,
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(
        tmp_path,
        statuses={"child-image": (status,)},
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == expected_code
    assert api.calls == ["quota", "child-create:0", "status:child-image"]
    operation = _operation_from_error(operations, caught.value)
    assert operation.kind == "POST_CAROUSEL"
    assert operation.phase == "FAILED_FINAL"
    assert operation.child_container_ids == ("child-image",)
    assert operation.container_id is None


@pytest.mark.asyncio
async def test_carousel_child_timeout_is_bounded_and_stops_before_next_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(
        tmp_path,
        statuses={"child-image": ("IN_PROGRESS",)},
    )
    delays = _patch_instant_polling(monkeypatch)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "THREADS_CONTAINER_PROCESSING_TIMEOUT"
    assert api.status_check_counts["child-image"] == 10
    assert delays == [3.0] * 9
    assert api.calls == ["quota", "child-create:0", *("status:child-image" for _ in range(10))]
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"
    assert operation.child_container_ids == ("child-image",)


@pytest.mark.parametrize("cancel_at", ["status", "sleep"])
@pytest.mark.asyncio
async def test_carousel_child_cancellation_is_final_without_creating_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_at: str,
) -> None:
    if cancel_at == "status":
        _, operations, api, _, runtime, _ = _setup_carousel(
            tmp_path,
            status_errors={
                "child-image": asyncio.CancelledError("private child status cancellation")
            },
        )
    else:
        _, operations, api, _, runtime, _ = _setup_carousel(
            tmp_path,
            statuses={"child-image": ("IN_PROGRESS",)},
        )

        async def cancel_sleep(_delay: float) -> None:
            raise asyncio.CancelledError("private child poll cancellation")

        monkeypatch.setattr(mutation_module.asyncio, "sleep", cancel_sleep)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "OPERATION_CANCELLED"
    assert "private child" not in str(caught.value)
    assert "parent-create" not in api.calls
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"
    assert operation.outcome_code == "OPERATION_CANCELLED"
    assert operation.child_container_ids == ("child-image",)


@pytest.mark.parametrize(
    ("child_ids", "expected_recorded"),
    [
        (("bad/id", "child-two"), ()),
        (("duplicate-child", "duplicate-child"), ("duplicate-child",)),
    ],
)
@pytest.mark.asyncio
async def test_carousel_rejects_invalid_or_duplicate_child_ids(
    tmp_path: Path,
    child_ids: tuple[str, ...],
    expected_recorded: tuple[str, ...],
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(tmp_path, child_ids=child_ids)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
    assert api.calls.count("publish") == 0
    assert "parent-create" not in api.calls
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"
    assert operation.child_container_ids == expected_recorded


@pytest.mark.asyncio
async def test_carousel_status_response_id_must_match_child(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(
        tmp_path,
        response_ids={"child-image": "different-child"},
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
    assert api.calls == ["quota", "child-create:0", "status:child-image"]
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"
    assert operation.child_container_ids == ("child-image",)


@pytest.mark.asyncio
async def test_carousel_child_status_api_failure_is_final_without_next_child(
    tmp_path: Path,
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(
        tmp_path,
        status_errors={"child-image": ThreadsAPIError("THREADS_RATE_LIMITED")},
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "THREADS_RATE_LIMITED"
    assert api.calls == ["quota", "child-create:0", "status:child-image"]
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"
    assert operation.child_container_ids == ("child-image",)


@pytest.mark.parametrize("failure", ["child-create", "parent-create"])
@pytest.mark.asyncio
async def test_carousel_container_creation_failures_are_final_without_retry(
    tmp_path: Path, failure: str
) -> None:
    error = ThreadsContractError()
    if failure == "child-create":
        _, operations, api, _, runtime, _ = _setup_carousel(
            tmp_path,
            create_errors={0: error},
        )
    else:
        _, operations, api, _, runtime, _ = _setup_carousel(
            tmp_path,
            parent_create_error=error,
        )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
    assert api.calls.count("parent-create") == (1 if failure == "parent-create" else 0)
    assert api.calls.count("publish") == 0
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"
    if failure == "child-create":
        assert operation.child_container_ids == ()
    else:
        assert operation.child_container_ids == ("child-image", "child-video")


@pytest.mark.parametrize("failure", ["child-create", "parent-create"])
@pytest.mark.asyncio
async def test_carousel_container_creation_cancellation_is_final(
    tmp_path: Path, failure: str
) -> None:
    cancellation = asyncio.CancelledError("private container creation cancellation")
    if failure == "child-create":
        _, operations, api, _, runtime, _ = _setup_carousel(
            tmp_path,
            create_errors={0: cancellation},
        )
    else:
        _, operations, api, _, runtime, _ = _setup_carousel(
            tmp_path,
            parent_create_error=cancellation,
        )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "OPERATION_CANCELLED"
    assert "private container creation cancellation" not in str(caught.value)
    assert api.calls.count("publish") == 0
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"


@pytest.mark.parametrize(
    ("parent_id", "expected_code"),
    [
        ("bad/parent", "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
        ("child-image", "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
    ],
)
@pytest.mark.asyncio
async def test_carousel_parent_id_is_valid_and_distinct_from_children(
    tmp_path: Path,
    parent_id: str,
    expected_code: str,
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(tmp_path, parent_id=parent_id)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == expected_code
    assert api.calls.count("parent-create") == 1
    parent_create_index = api.calls.index("parent-create")
    assert not any(call.startswith("status:") for call in api.calls[parent_create_index + 1 :])
    assert api.calls.count("publish") == 0
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"
    assert operation.container_id is None


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("ERROR", "THREADS_CONTAINER_ERROR"),
        ("EXPIRED", "THREADS_CONTAINER_EXPIRED"),
        ("UNKNOWN", "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
        (None, "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
    ],
)
@pytest.mark.asyncio
async def test_carousel_parent_status_failure_is_final_without_publish(
    tmp_path: Path,
    status: str | None,
    expected_code: str,
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(
        tmp_path,
        statuses={"carousel-parent": (status,)},
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == expected_code
    assert api.calls[-1] == "status:carousel-parent"
    assert "publish" not in api.calls
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"
    assert operation.child_container_ids == ("child-image", "child-video")
    assert operation.container_id == "carousel-parent"


@pytest.mark.asyncio
async def test_carousel_parent_timeout_is_bounded_without_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(
        tmp_path,
        statuses={"carousel-parent": ("IN_PROGRESS",)},
    )
    delays = _patch_instant_polling(monkeypatch)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "THREADS_CONTAINER_PROCESSING_TIMEOUT"
    assert api.status_check_counts["carousel-parent"] == 10
    assert delays == [3.0] * 9
    assert "publish" not in api.calls
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"


@pytest.mark.parametrize("cancel_at", ["status", "sleep"])
@pytest.mark.asyncio
async def test_carousel_parent_cancellation_is_final_without_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_at: str,
) -> None:
    if cancel_at == "status":
        _, operations, api, _, runtime, _ = _setup_carousel(
            tmp_path,
            status_errors={
                "carousel-parent": asyncio.CancelledError("private parent cancellation")
            },
        )
    else:
        _, operations, api, _, runtime, _ = _setup_carousel(
            tmp_path,
            statuses={"carousel-parent": ("IN_PROGRESS",)},
        )

        async def cancel_sleep(_delay: float) -> None:
            raise asyncio.CancelledError("private parent poll cancellation")

        monkeypatch.setattr(mutation_module.asyncio, "sleep", cancel_sleep)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "OPERATION_CANCELLED"
    assert "private parent" not in str(caught.value)
    assert api.calls.count("publish") == 0
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "FAILED_FINAL"
    assert operation.container_id == "carousel-parent"


@pytest.mark.asyncio
async def test_carousel_parent_id_is_durable_before_parent_polling(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(tmp_path)

    await runtime.publish_carousel("alice", _carousel_manifest())

    assert api.parent_create_snapshot is not None
    assert api.parent_create_snapshot.container_id is None
    operation = _only_operation(operations, tmp_path)
    assert operation.container_id == "carousel-parent"
    assert operation.child_container_ids == ("child-image", "child-video")
    assert api.calls.index("parent-create") < api.calls.index("status:carousel-parent")


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (ThreadsAPIError("THREADS_RATE_LIMITED"), "THREADS_RATE_LIMITED"),
        (ThreadsAPIError("THREADS_SERVER_ERROR"), "THREADS_SERVER_ERROR"),
        (ThreadsTransportError(), "THREADS_TRANSPORT_FAILURE"),
        (ThreadsContractError(), "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"),
        (RuntimeError("raw carousel publish response"), "THREADS_TRANSPORT_FAILURE"),
    ],
)
@pytest.mark.asyncio
async def test_carousel_publish_failure_after_boundary_is_ambiguous_without_retry(
    tmp_path: Path,
    failure: Exception,
    expected_code: str,
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(tmp_path, publish_error=failure)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls.count("publish") == 1
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == expected_code
    assert operation.child_container_ids == ("child-image", "child-video")


@pytest.mark.asyncio
async def test_carousel_publish_cancellation_after_boundary_is_ambiguous(
    tmp_path: Path,
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(
        tmp_path,
        publish_error=asyncio.CancelledError("private carousel publish cancellation"),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert "private carousel publish cancellation" not in str(caught.value)
    assert api.calls.count("publish") == 1
    assert _operation_from_error(operations, caught.value).phase == "AMBIGUOUS"


@pytest.mark.asyncio
async def test_carousel_base_exception_after_boundary_marks_ambiguous_without_retry(
    tmp_path: Path,
) -> None:
    failure = _InjectedPublishBaseException("private base exception")
    _, operations, api, _, runtime, _ = _setup_carousel(tmp_path, publish_error=failure)

    with pytest.raises(_InjectedPublishBaseException):
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert api.calls.count("publish") == 1
    operation = _only_operation(operations, tmp_path)
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == "PUBLISH_OUTCOME_AMBIGUOUS"


@pytest.mark.asyncio
async def test_carousel_invalid_published_id_is_ambiguous(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(tmp_path, media_id="bad/id")

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls.count("publish") == 1
    operation = _operation_from_error(operations, caught.value)
    assert operation.phase == "AMBIGUOUS"
    assert operation.outcome_code == "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"


@pytest.mark.asyncio
async def test_carousel_publish_requested_must_be_durable_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(tmp_path)
    original_update = operations.update

    def fail_publish_requested(operation: LocalOperation) -> LocalOperation:
        if operation.phase == "PUBLISH_REQUESTED":
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_publish_requested)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "OPERATION_STATE_INVALID"
    assert api.calls.count("publish") == 0
    operation = _only_operation(operations, tmp_path)
    assert operation.phase == "CONTAINER_CREATED"
    assert operation.child_container_ids == ("child-image", "child-video")


@pytest.mark.asyncio
async def test_carousel_final_journal_failure_is_ambiguous_without_republish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, operations, api, _, runtime, _ = _setup_carousel(tmp_path)
    original_update = operations.update

    def fail_terminal_updates(operation: LocalOperation) -> LocalOperation:
        if operation.phase in {"PUBLISHED", "AMBIGUOUS"}:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_terminal_updates)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.publish_carousel("alice", _carousel_manifest())

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert api.calls.count("publish") == 1
    durable = _operation_from_error(operations, caught.value)
    assert durable.phase == "PUBLISH_REQUESTED"


def test_carousel_journal_child_ids_are_append_only_unique_and_ordered(tmp_path: Path) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), kind="POST_CAROUSEL"
    )
    child_phase = store.update(replace(operation, phase="CHILDREN_CREATING"))
    first = store.update(replace(child_phase, child_container_ids=("child-one",)))
    second = store.update(replace(first, child_container_ids=("child-one", "child-two")))

    for invalid_ids in (
        ("child-two", "child-one"),
        ("child-one",),
        ("replacement", "child-two"),
        ("child-one", "child-two", "child-three", "child-four"),
        ("child-one", "child-two", "child-two"),
    ):
        with pytest.raises(StandaloneMutationError) as caught:
            store.update(replace(second, child_container_ids=invalid_ids))
        assert caught.value.code == "OPERATION_STATE_INVALID"
        assert store.get(operation.id) == second

    parent = store.update(replace(second, phase="CONTAINER_CREATED", container_id="parent-one"))
    requested = store.update(replace(parent, phase="PUBLISH_REQUESTED"))
    published = store.update(replace(requested, phase="PUBLISHED", media_id="media-one"))
    assert store.get(operation.id) == published


def test_carousel_journal_can_fail_before_child_creation(tmp_path: Path) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), kind="POST_CAROUSEL"
    )

    failed = store.update(
        replace(operation, phase="FAILED_FINAL", outcome_code="LOCAL_OPERATION_UNAVAILABLE")
    )

    assert store.get(operation.id) == failed
    assert failed.child_container_ids == ()
    assert failed.container_id is None


@pytest.mark.parametrize("kind", ["POST_TEXT", "CREATE_REPLY", "POST_IMAGE", "POST_VIDEO"])
def test_non_carousel_journals_reject_carousel_only_phase_and_metadata(
    tmp_path: Path, kind: str
) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), kind=kind)

    for replacement in (
        replace(operation, phase="CHILDREN_CREATING"),
        replace(operation, child_container_ids=("child-one",)),
    ):
        with pytest.raises(StandaloneMutationError) as caught:
            store.update(replacement)
        assert caught.value.code == "OPERATION_STATE_INVALID"
        assert store.get(operation.id) == operation

    path = tmp_path / "operations" / f"{operation.id}.json"
    data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    data["child_container_ids"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StandaloneMutationError) as caught:
        store.get(operation.id)
    assert caught.value.code == "OPERATION_STATE_INVALID"


def test_carousel_journal_kind_is_immutable_and_v1_child_ids_are_optional(
    tmp_path: Path,
) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), kind="POST_CAROUSEL"
    )
    creating = store.update(replace(operation, phase="CHILDREN_CREATING"))
    first = store.update(replace(creating, child_container_ids=("child-one",)))
    second = store.update(replace(first, child_container_ids=("child-one", "child-two")))
    with pytest.raises(StandaloneMutationError) as caught:
        store.update(
            replace(
                second,
                kind="POST_VIDEO",
                phase="FAILED_FINAL",
                outcome_code="TEST_FAILURE",
            )
        )
    assert caught.value.code == "OPERATION_STATE_INVALID"
    assert store.get(operation.id) == second

    decoded = json.loads((tmp_path / "operations" / f"{operation.id}.json").read_text())
    assert decoded["version"] == 1
    assert decoded["child_container_ids"] == ["child-one", "child-two"]


@pytest.mark.parametrize(
    "child_ids",
    ["not-a-list", [True], ["bad/id"], ["repeated", "repeated"]],
)
def test_malformed_carousel_child_metadata_fails_closed(tmp_path: Path, child_ids: object) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), kind="POST_CAROUSEL"
    )
    path = tmp_path / "operations" / f"{operation.id}.json"
    data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    data["phase"] = "CHILDREN_CREATING"
    data["child_container_ids"] = child_ids
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(StandaloneMutationError) as caught:
        store.get(operation.id)

    assert caught.value.code == "OPERATION_STATE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_call"),
    [
        ("hide", ("manage_reply", "reply-target-sentinel-42", True)),
        ("unhide", ("manage_reply", "reply-target-sentinel-42", False)),
        ("approve", ("manage_pending_reply", "reply-target-sentinel-42", True)),
        ("ignore", ("manage_pending_reply", "reply-target-sentinel-42", False)),
    ],
)
async def test_moderation_action_uses_exact_api_mapping_once_and_confirms(
    tmp_path: Path,
    action: str,
    expected_call: tuple[str, str, bool],
) -> None:
    _, operations, api, resolver, runtime, _ = _setup_moderation(tmp_path)

    result = await runtime.moderate_reply("alice", "reply-target-sentinel-42", action)

    assert api.calls == [expected_call]
    assert len(api.snapshots) == 1
    assert api.snapshots[0].phase == "MUTATION_REQUESTED"
    assert api.snapshots[0].action == action
    assert resolver.calls == [_CREDENTIAL_REF]
    operation = operations.get(result.operation_id)
    assert operation.version == 1
    assert operation.kind == "MODERATE_REPLY"
    assert operation.phase == "CONFIRMED"
    assert operation.action == action
    assert operation.container_id is None
    assert operation.media_id is None
    assert operation.child_container_ids == ()
    journal = (tmp_path / "operations" / f"{result.operation_id}.json").read_text()
    assert "reply-target-sentinel-42" not in journal
    assert _TOKEN not in journal
    assert _CREDENTIAL_REF not in journal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "endpoint", "flag"),
    [
        ("hide", "manage_reply", True),
        ("unhide", "manage_reply", False),
        ("approve", "manage_pending_reply", True),
        ("ignore", "manage_pending_reply", False),
    ],
)
async def test_moderation_never_falls_back_to_other_endpoint(
    tmp_path: Path,
    action: str,
    endpoint: str,
    flag: bool,
) -> None:
    _, _, api, _, runtime, _ = _setup_moderation(
        tmp_path,
        failure=ThreadsAPIError("THREADS_OBJECT_NOT_FOUND"),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.moderate_reply("alice", "reply-target-42", action)

    assert caught.value.code == "MODERATION_OUTCOME_AMBIGUOUS"
    assert api.calls == [(endpoint, "reply-target-42", flag)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply_id",
    [
        "",
        "https://threads.example/replies/42",
        "../reply-42",
        "replies/reply-42",
        ".",
        "..",
        "x" * 256,
    ],
)
async def test_moderation_rejects_invalid_reply_id_before_account_or_side_effects(
    tmp_path: Path,
    reply_id: str,
) -> None:
    accounts, _, api, resolver, runtime, _ = _setup_moderation(tmp_path)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.moderate_reply("alice", reply_id, "hide")

    assert caught.value.code == "INVALID_REPLY_ID"
    assert accounts.get_calls == 0
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["HIDE", "hide ", "approve\n", "delete"])
async def test_moderation_rejects_invalid_action_before_account_or_side_effects(
    tmp_path: Path,
    action: str,
) -> None:
    accounts, _, api, resolver, runtime, _ = _setup_moderation(tmp_path)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.moderate_reply("alice", "reply-target-sentinel-42", action)

    assert caught.value.code == "INVALID_MODERATION_ACTION"
    assert accounts.get_calls == 0
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_moderation_does_not_coerce_action_or_reply_id(tmp_path: Path) -> None:
    accounts, _, api, resolver, runtime, _ = _setup_moderation(tmp_path)

    with pytest.raises(StandaloneMutationError) as bad_action:
        await runtime.moderate_reply("alice", "reply-target-42", cast(str, 1))
    with pytest.raises(StandaloneMutationError) as bad_reply_id:
        await runtime.moderate_reply("alice", cast(str, 42), "hide")

    assert bad_action.value.code == "INVALID_MODERATION_ACTION"
    assert bad_reply_id.value.code == "INVALID_REPLY_ID"
    assert accounts.get_calls == 0
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_moderation_lock_conflict_creates_no_journal_or_api_call(tmp_path: Path) -> None:
    accounts, _, api, resolver, runtime, account_id = _setup_moderation(tmp_path)
    lock_directory = tmp_path / "locks"
    lock_directory.mkdir()
    held_lock = FilesystemProcessLock(lock_directory / f"{account_id}.lock")
    held_lock.acquire()

    try:
        with pytest.raises(StandaloneMutationError) as caught:
            await runtime.moderate_reply("alice", "reply-target-42", "hide")
    finally:
        held_lock.release()

    assert caught.value.code == "ACCOUNT_BUSY"
    assert accounts.get_calls == 1
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", [False, True])
async def test_moderation_credential_failure_creates_no_journal_or_api_call(
    tmp_path: Path,
    credential: bool,
) -> None:
    resolver = _FakeResolver(
        error=ThreadsCredentialError(ThreadsCredentialErrorCode.SECRET_UNAVAILABLE)
    )
    accounts, _, api, _, runtime, _ = _setup_moderation(
        tmp_path,
        resolver=resolver,
        credential=credential,
    )

    with pytest.raises(ThreadsCredentialError):
        await runtime.moderate_reply("alice", "reply-target-42", "hide")

    assert accounts.get_calls == 1
    assert resolver.calls == ([] if not credential else [_CREDENTIAL_REF])
    assert api.calls == []
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_moderation_unexpected_resolver_failure_is_sanitized_before_journal(
    tmp_path: Path,
) -> None:
    raw_credential_error = f"resolver failed for {_CREDENTIAL_REF} {_TOKEN}"
    resolver = _FakeResolver(error=RuntimeError(raw_credential_error))
    _, _, api, _, runtime, _ = _setup_moderation(tmp_path, resolver=resolver)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.moderate_reply("alice", "reply-target-42", "hide")

    assert caught.value.code == "THREADS_CREDENTIAL_SECRET_UNAVAILABLE"
    assert raw_credential_error not in str(caught.value)
    assert api.calls == []
    assert resolver.calls == [_CREDENTIAL_REF]
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
async def test_moderation_missing_account_creates_no_journal_or_api_call(tmp_path: Path) -> None:
    _, _, api, resolver, runtime, _ = _setup_moderation(tmp_path)

    with pytest.raises(StandaloneAccountError) as caught:
        await runtime.moderate_reply("missing", "reply-target-42", "hide")

    assert caught.value.code == "ACCOUNT_NOT_FOUND"
    assert resolver.calls == []
    assert api.calls == []
    assert not (tmp_path / "operations").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ThreadsAPIError("THREADS_RATE_LIMITED"),
        ThreadsAPIError("THREADS_SERVER_ERROR"),
        ThreadsTransportError(),
        ThreadsContractError(),
        RuntimeError("raw moderation response sentinel"),
    ],
)
async def test_moderation_api_uncertainty_is_ambiguous_without_retry(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    _, operations, api, _, runtime, _ = _setup_moderation(tmp_path, failure=failure)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.moderate_reply("alice", "reply-target-sentinel-42", "approve")

    assert caught.value.code == "MODERATION_OUTCOME_AMBIGUOUS"
    assert caught.value.operation_id is not None
    assert len(api.calls) == 1
    assert api.calls[0][0] == "manage_pending_reply"
    operation = operations.get(caught.value.operation_id)
    assert operation.phase == "AMBIGUOUS"
    assert operation.action == "approve"
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text()
    for forbidden in (
        "reply-target-sentinel-42",
        _TOKEN,
        _CREDENTIAL_REF,
        "raw moderation response sentinel",
        "Authorization",
    ):
        assert forbidden not in journal


@pytest.mark.asyncio
async def test_moderation_cancellation_after_boundary_is_ambiguous(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup_moderation(
        tmp_path,
        failure=asyncio.CancelledError("private moderation cancellation"),
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.moderate_reply("alice", "reply-target-42", "hide")

    assert caught.value.code == "MODERATION_OUTCOME_AMBIGUOUS"
    assert caught.value.operation_id is not None
    assert len(api.calls) == 1
    operation = operations.get(caught.value.operation_id)
    assert operation.phase == "AMBIGUOUS"


@pytest.mark.asyncio
async def test_moderation_base_exception_marks_ambiguous_then_reraises(tmp_path: Path) -> None:
    failure = _InjectedModerationBaseException("private moderation interruption")
    _, operations, api, _, runtime, _ = _setup_moderation(tmp_path, failure=failure)

    with pytest.raises(_InjectedModerationBaseException):
        await runtime.moderate_reply("alice", "reply-target-42", "unhide")

    assert len(api.calls) == 1
    operation = _only_operation(operations, tmp_path)
    assert operation.phase == "AMBIGUOUS"
    assert operation.action == "unhide"


@pytest.mark.asyncio
async def test_moderation_invalid_success_result_is_ambiguous(tmp_path: Path) -> None:
    _, operations, api, _, runtime, _ = _setup_moderation(
        tmp_path,
        response={"body": "raw response sentinel", "header": "private-header"},
    )

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.moderate_reply("alice", "reply-target-sentinel-42", "ignore")

    assert caught.value.code == "MODERATION_OUTCOME_AMBIGUOUS"
    assert caught.value.operation_id is not None
    assert len(api.calls) == 1
    operation = operations.get(caught.value.operation_id)
    assert operation.phase == "AMBIGUOUS"
    journal = (tmp_path / "operations" / f"{operation.id}.json").read_text()
    assert "raw response sentinel" not in journal
    assert "private-header" not in journal
    assert "reply-target-sentinel-42" not in journal


@pytest.mark.asyncio
async def test_moderation_requested_is_durable_before_api_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, operations, api, _, runtime, _ = _setup_moderation(tmp_path)
    updates: list[tuple[str, str]] = []
    original_update = operations.update

    def record_update(operation: LocalOperation) -> LocalOperation:
        current = operations.get(operation.id)
        updates.append((current.phase, operation.phase))
        return original_update(operation)

    monkeypatch.setattr(operations, "update", record_update)

    await runtime.moderate_reply("alice", "reply-target-42", "approve")

    assert updates == [("RECEIVED", "MUTATION_REQUESTED"), ("MUTATION_REQUESTED", "CONFIRMED")]
    assert api.snapshots[0].phase == "MUTATION_REQUESTED"


@pytest.mark.asyncio
async def test_moderation_requested_persist_failure_makes_no_remote_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, operations, api, _, runtime, _ = _setup_moderation(tmp_path)
    original_update = operations.update

    def fail_requested(operation: LocalOperation) -> LocalOperation:
        if operation.phase == "MUTATION_REQUESTED":
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_requested)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.moderate_reply("alice", "reply-target-42", "approve")

    assert caught.value.code == "OPERATION_STATE_INVALID"
    assert caught.value.operation_id is not None
    assert api.calls == []
    operation = operations.get(caught.value.operation_id)
    assert operation.phase == "RECEIVED"


@pytest.mark.asyncio
async def test_moderation_confirmation_journal_failure_is_ambiguous_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, operations, api, _, runtime, _ = _setup_moderation(tmp_path)
    original_update = operations.update

    def fail_confirmed(operation: LocalOperation) -> LocalOperation:
        if operation.phase == "CONFIRMED":
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        return original_update(operation)

    monkeypatch.setattr(operations, "update", fail_confirmed)

    with pytest.raises(StandaloneMutationError) as caught:
        await runtime.moderate_reply("alice", "reply-target-42", "ignore")

    assert caught.value.code == "MODERATION_OUTCOME_AMBIGUOUS"
    assert caught.value.operation_id is not None
    assert len(api.calls) == 1
    assert operations.get(caught.value.operation_id).phase == "MUTATION_REQUESTED"


def test_moderation_journal_action_and_kind_are_immutable(tmp_path: Path) -> None:
    store = LocalOperationStore(tmp_path)
    account_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    operation = store.create_received(
        account_id,
        kind="MODERATE_REPLY",
        action="hide",
    )

    with pytest.raises(StandaloneMutationError) as action_error:
        store.update(replace(operation, action="unhide"))
    with pytest.raises(StandaloneMutationError) as kind_error:
        store.update(replace(operation, kind="POST_TEXT", action=None))
    assert action_error.value.code == "OPERATION_STATE_INVALID"
    assert kind_error.value.code == "OPERATION_STATE_INVALID"
    requested = store.update(replace(operation, phase="MUTATION_REQUESTED"))
    confirmed = store.update(replace(requested, phase="CONFIRMED"))
    assert store.get(operation.id) == confirmed


@pytest.mark.parametrize(
    "kind",
    ["POST_TEXT", "CREATE_REPLY", "POST_IMAGE", "POST_VIDEO", "POST_CAROUSEL"],
)
def test_publish_journals_reject_moderation_phase_and_action_metadata(
    tmp_path: Path,
    kind: str,
) -> None:
    store = LocalOperationStore(tmp_path)
    operation = store.create_received(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        kind=kind,
    )

    for replacement in (
        replace(operation, phase="MUTATION_REQUESTED"),
        replace(operation, phase="CONFIRMED"),
        replace(operation, action="hide"),
    ):
        with pytest.raises(StandaloneMutationError) as caught:
            store.update(replacement)
        assert caught.value.code == "OPERATION_STATE_INVALID"
        assert store.get(operation.id) == operation

    journal_path = tmp_path / "operations" / f"{operation.id}.json"
    data = cast(dict[str, object], json.loads(journal_path.read_text(encoding="utf-8")))
    data["action"] = "hide"
    journal_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StandaloneMutationError) as caught:
        store.get(operation.id)
    assert caught.value.code == "OPERATION_STATE_INVALID"


def test_legacy_carousel_journal_without_moderation_action_is_readable(tmp_path: Path) -> None:
    store = LocalOperationStore(tmp_path)
    operation_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    account_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    operations_path = tmp_path / "operations"
    operations_path.mkdir()
    (operations_path / f"{operation_id}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "id": str(operation_id),
                "account_id": str(account_id),
                "kind": "POST_CAROUSEL",
                "phase": "CHILDREN_CREATING",
                "child_container_ids": ["carousel-child-legacy"],
            }
        ),
        encoding="utf-8",
    )

    operation = store.get(operation_id)

    assert operation.version == 1
    assert operation.kind == "POST_CAROUSEL"
    assert operation.phase == "CHILDREN_CREATING"
    assert operation.child_container_ids == ("carousel-child-legacy",)
    assert operation.action is None
