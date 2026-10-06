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
    StandaloneMutationError,
)

_TOKEN = "token-sentinel-never-journal-this"
_CREDENTIAL_REF = "env://THREADS_PLATFORM_THREADS_TOKEN_TEST"
_TEXT = "text-sentinel-never-journal-this"


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
        media_id: str = "media-123",
    ) -> None:
        self.root = root
        self.operations = operations
        self.quota = quota or PublishingQuota(usage=1, total=100)
        self.create_error = create_error
        self.publish_error = publish_error
        self.container_id = container_id
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

    async def get_container(self, *_args: object, **_kwargs: object) -> MediaContainer:
        self.calls.append("get_container")
        raise AssertionError("container reconciliation is forbidden")

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


def test_post_text_journal_remains_readable_with_shared_reply_journal(tmp_path: Path) -> None:
    store = LocalOperationStore(tmp_path)
    operation_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    account_id = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    operations_path = tmp_path / "operations"
    operations_path.mkdir()
    legacy_path = operations_path / f"{operation_id}.json"
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

    legacy = store.get(operation_id)
    reply = store.create_received(account_id, kind="CREATE_REPLY")

    assert legacy.kind == "POST_TEXT"
    assert legacy.phase == "PUBLISHED"
    assert legacy.media_id == "media-legacy"
    assert reply.version == 1
    assert reply.kind == "CREATE_REPLY"
    assert reply.phase == "RECEIVED"
    assert set(json.loads((operations_path / f"{reply.id}.json").read_text())) == {
        "version",
        "id",
        "account_id",
        "kind",
        "phase",
    }
