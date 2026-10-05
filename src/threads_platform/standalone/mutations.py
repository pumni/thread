"""Local, journaled text publishing for the standalone command line."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn, TypeGuard, cast
from uuid import UUID, uuid4

from threads_platform.application.ports.process_lock import ProcessAlreadyRunning
from threads_platform.application.ports.threads import (
    MediaContainerRequest,
    ThreadsAPI,
    ThreadsCredentialError,
    ThreadsCredentialErrorCode,
    ThreadsCredentialSecretResolver,
)
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.standalone.accounts import LocalAccountStore

_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_REMOTE_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_JOURNAL_VERSION = 1
_JOURNAL_KIND = "POST_TEXT"
_JOURNAL_PHASES = frozenset(
    {
        "RECEIVED",
        "CONTAINER_CREATED",
        "PUBLISH_REQUESTED",
        "PUBLISHED",
        "AMBIGUOUS",
        "FAILED_FINAL",
    }
)
_JOURNAL_KEYS = frozenset(
    {"version", "id", "account_id", "kind", "phase", "container_id", "media_id", "outcome_code"}
)
_REQUIRED_JOURNAL_KEYS = frozenset({"version", "id", "account_id", "kind", "phase"})
_MAX_JOURNAL_BYTES = 4096
_TRANSITIONS = {
    "RECEIVED": frozenset({"CONTAINER_CREATED", "FAILED_FINAL"}),
    "CONTAINER_CREATED": frozenset({"PUBLISH_REQUESTED", "FAILED_FINAL"}),
    "PUBLISH_REQUESTED": frozenset({"PUBLISHED", "AMBIGUOUS"}),
}


class StandaloneMutationError(Exception):
    """A bounded, output-safe error from a standalone mutation."""

    code: str
    operation_id: UUID | None

    def __init__(self, code: str, operation_id: UUID | None = None) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("standalone mutation error code must be bounded and safe")
        self.code = code
        self.operation_id = operation_id
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LocalOperation:
    version: int
    id: UUID
    account_id: UUID
    kind: str
    phase: str
    container_id: str | None = None
    media_id: str | None = None
    outcome_code: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedTextResult:
    operation_id: UUID
    media_id: str


class LocalOperationStore:
    """Strict, atomic JSON journal for standalone post operations."""

    def __init__(self, data_root: Path) -> None:
        self._configured_root = Path(data_root).absolute()
        try:
            self._root = self._configured_root.resolve(strict=False)
        except OSError, RuntimeError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID") from None

    def create_received(self, account_id: UUID) -> LocalOperation:
        operation = LocalOperation(
            version=_JOURNAL_VERSION,
            id=uuid4(),
            account_id=account_id,
            kind=_JOURNAL_KIND,
            phase="RECEIVED",
        )
        operations = self._operations_directory(create=True)
        if operations is None:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        target = operations / f"{operation.id}.json"
        self._write_initial(target, operation)
        return operation

    def update(self, operation: LocalOperation) -> LocalOperation:
        _validate_operation(operation)
        try:
            current = self.get(operation.id)
        except StandaloneMutationError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id) from None
        if current.account_id != operation.account_id or operation.phase not in _TRANSITIONS.get(
            current.phase, frozenset()
        ):
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        operations = self._operations_directory(create=False)
        if operations is None:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        target = self._operation_path(operations, operation.id, must_exist=True)
        if target is None:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        self._write_replace(target, operation)
        return operation

    def get(self, operation_id: UUID) -> LocalOperation:
        if type(operation_id) is not UUID:
            raise StandaloneMutationError("OPERATION_NOT_FOUND")
        operations = self._operations_directory(create=False)
        if operations is None:
            raise StandaloneMutationError("OPERATION_NOT_FOUND", operation_id)
        target = self._operation_path(operations, operation_id, must_exist=False)
        if target is None:
            raise StandaloneMutationError("OPERATION_NOT_FOUND", operation_id)
        try:
            size = target.stat().st_size
            if size > _MAX_JOURNAL_BYTES:
                raise ValueError
            raw = target.read_bytes()
            if len(raw) > _MAX_JOURNAL_BYTES:
                raise ValueError
            data = json.loads(raw, object_pairs_hook=_unique_object)
        except OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation_id) from None
        operation = _decode_operation(data, operation_id)
        return operation

    def _operations_directory(self, *, create: bool) -> Path | None:
        try:
            self._root.lstat()
        except FileNotFoundError:
            if not create:
                return None
            raise StandaloneMutationError("OPERATION_STATE_INVALID") from None
        except OSError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID") from None
        root = self._validate_root()
        operations = root / "operations"
        try:
            operations.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                operations.mkdir()
            except FileExistsError:
                pass
            except OSError:
                raise StandaloneMutationError("OPERATION_STATE_INVALID") from None
        except OSError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID") from None
        try:
            metadata = operations.lstat()
            resolved = operations.resolve(strict=True)
        except OSError, RuntimeError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != operations
            or resolved.parent != root
        ):
            raise StandaloneMutationError("OPERATION_STATE_INVALID")
        return operations

    def _validate_root(self) -> Path:
        try:
            metadata = self._root.lstat()
            resolved = self._root.resolve(strict=True)
        except OSError, RuntimeError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID") from None
        if (
            self._configured_root != self._root
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != self._root
        ):
            raise StandaloneMutationError("OPERATION_STATE_INVALID")
        return self._root

    def _operation_path(
        self,
        operations: Path,
        operation_id: UUID,
        *,
        must_exist: bool,
    ) -> Path | None:
        path = operations / f"{operation_id}.json"
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if must_exist:
                raise StandaloneMutationError("OPERATION_STATE_INVALID", operation_id) from None
            return None
        except OSError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation_id) from None
        try:
            resolved = path.resolve(strict=True)
        except OSError, RuntimeError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation_id) from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or resolved != path
            or resolved.parent != operations
            or not resolved.is_relative_to(self._root)
        ):
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation_id)
        return path

    def _write_initial(self, target: Path, operation: LocalOperation) -> None:
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            if self._operation_path(target.parent, operation.id, must_exist=False) is not None:
                raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary_path = Path(temporary_name)
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = None
            with stream:
                stream.write(_encode_operation(operation))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id) from None
        except StandaloneMutationError:
            raise
        except OSError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id) from None
        finally:
            _cleanup_temporary(temporary_path, descriptor)

    def _write_replace(self, target: Path, operation: LocalOperation) -> None:
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary_path = Path(temporary_name)
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = None
            with stream:
                stream.write(_encode_operation(operation))
                stream.flush()
                os.fsync(stream.fileno())
            if self._operation_path(target.parent, operation.id, must_exist=True) is None:
                raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
            os.replace(temporary_path, target)
        except StandaloneMutationError:
            raise
        except OSError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id) from None
        finally:
            _cleanup_temporary(temporary_path, descriptor)


class LocalThreadsMutationRuntime:
    def __init__(
        self,
        data_root: Path,
        accounts: LocalAccountStore,
        api: ThreadsAPI,
        secret_resolver: ThreadsCredentialSecretResolver,
        operations: LocalOperationStore,
    ) -> None:
        self._root = Path(data_root).resolve(strict=False)
        self._accounts = accounts
        self._api = api
        self._secret_resolver = secret_resolver
        self._operations = operations

    async def publish_text(self, alias: str, text: str) -> PublishedTextResult:
        _validate_post_text(text)
        account = self._accounts.get(alias)
        lock = FilesystemProcessLock(_account_lock_path(self._root, account.id))
        operation_id: UUID | None = None
        try:
            lock.acquire()
        except ProcessAlreadyRunning:
            raise StandaloneMutationError("ACCOUNT_BUSY") from None
        except OSError:
            raise StandaloneMutationError("LOCAL_OPERATION_UNAVAILABLE") from None

        try:
            if account.credential_ref is None:
                raise ThreadsCredentialError(ThreadsCredentialErrorCode.NOT_CONFIGURED)
            token = await self._secret_resolver.resolve(account.credential_ref)
            quota = await self._api.get_publishing_quota(token)
            if quota.usage is not None and quota.total is not None and quota.usage >= quota.total:
                raise StandaloneMutationError("THREADS_PUBLISHING_QUOTA_REACHED")

            operation = self._operations.create_received(account.id)
            operation_id = operation.id
            try:
                container = await self._api.create_container(
                    token,
                    MediaContainerRequest(media_type="TEXT", text=text),
                )
            except Exception as error:
                code = _safe_exception_code(error)
                self._best_effort_failed_final(operation, code)
                raise StandaloneMutationError(code, operation.id) from None

            container_id = getattr(container, "container_id", None)
            if not _valid_remote_id(container_id):
                code = "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
                self._best_effort_failed_final(operation, code)
                raise StandaloneMutationError(code, operation.id)

            operation = self._persist_required(
                replace(operation, phase="CONTAINER_CREATED", container_id=container_id)
            )
            publish_requested = self._persist_required(
                replace(operation, phase="PUBLISH_REQUESTED")
            )
            try:
                media_id = await self._api.publish_container(token, container_id)
            except Exception as error:
                self._raise_ambiguous(publish_requested, _safe_exception_code(error))
            if not _valid_remote_id(media_id):
                self._raise_ambiguous(publish_requested, "THREADS_DOCUMENTATION_CONTRACT_MISMATCH")

            try:
                published = self._operations.update(
                    replace(publish_requested, phase="PUBLISHED", media_id=media_id)
                )
            except StandaloneMutationError:
                self._raise_ambiguous(publish_requested, "OPERATION_STATE_INVALID")
            return PublishedTextResult(operation_id=published.id, media_id=media_id)
        finally:
            try:
                lock.release()
            except OSError:
                raise StandaloneMutationError("LOCAL_OPERATION_UNAVAILABLE", operation_id) from None

    def _persist_required(self, operation: LocalOperation) -> LocalOperation:
        try:
            return self._operations.update(operation)
        except StandaloneMutationError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id) from None

    def _best_effort_failed_final(self, operation: LocalOperation, code: str) -> None:
        try:
            self._operations.update(replace(operation, phase="FAILED_FINAL", outcome_code=code))
        except StandaloneMutationError:
            pass

    def _raise_ambiguous(self, operation: LocalOperation, code: str) -> NoReturn:
        try:
            self._operations.update(replace(operation, phase="AMBIGUOUS", outcome_code=code))
        except StandaloneMutationError:
            pass
        raise StandaloneMutationError("PUBLISH_OUTCOME_AMBIGUOUS", operation.id) from None


def _account_lock_path(root: Path, account_id: UUID) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        root_metadata = root.lstat()
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise OSError
        if resolved_root != root:
            raise OSError
        locks = root / "locks"
        try:
            locks.lstat()
        except FileNotFoundError:
            try:
                locks.mkdir()
            except FileExistsError:
                pass
        metadata = locks.lstat()
        resolved_locks = locks.resolve(strict=True)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved_locks != locks
            or resolved_locks.parent != root
        ):
            raise OSError
        target = locks / f"{account_id}.lock"
        try:
            target_metadata = target.lstat()
        except FileNotFoundError:
            return target
        resolved_target = target.resolve(strict=True)
        if (
            stat.S_ISLNK(target_metadata.st_mode)
            or not stat.S_ISREG(target_metadata.st_mode)
            or resolved_target != target
            or resolved_target.parent != locks
            or not resolved_target.is_relative_to(root)
        ):
            raise OSError
        return target
    except OSError, RuntimeError, ValueError:
        raise StandaloneMutationError("LOCAL_OPERATION_UNAVAILABLE") from None


def _validate_post_text(text: object) -> None:
    if not isinstance(text, str) or not 1 <= len(text) <= 500 or not text.strip():
        raise StandaloneMutationError("INVALID_POST_TEXT")


def _valid_remote_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _REMOTE_ID.fullmatch(value) is not None


def _safe_exception_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code) is not None:
        return code
    return "THREADS_TRANSPORT_FAILURE"


def _validate_operation(operation: LocalOperation) -> None:
    if (
        type(operation.version) is not int
        or operation.version != _JOURNAL_VERSION
        or operation.kind != _JOURNAL_KIND
        or operation.phase not in _JOURNAL_PHASES
    ):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
    if operation.container_id is not None and not _valid_remote_id(operation.container_id):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
    if operation.media_id is not None and not _valid_remote_id(operation.media_id):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
    if operation.outcome_code is not None and _SAFE_CODE.fullmatch(operation.outcome_code) is None:
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
    valid_shape = {
        "RECEIVED": operation.container_id is None
        and operation.media_id is None
        and operation.outcome_code is None,
        "CONTAINER_CREATED": operation.container_id is not None
        and operation.media_id is None
        and operation.outcome_code is None,
        "PUBLISH_REQUESTED": operation.container_id is not None
        and operation.media_id is None
        and operation.outcome_code is None,
        "PUBLISHED": operation.container_id is not None
        and operation.media_id is not None
        and operation.outcome_code is None,
        "AMBIGUOUS": operation.container_id is not None
        and operation.media_id is None
        and operation.outcome_code is not None,
        "FAILED_FINAL": operation.media_id is None and operation.outcome_code is not None,
    }
    if not valid_shape[operation.phase]:
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)


def _encode_operation(operation: LocalOperation) -> str:
    _validate_operation(operation)
    data: dict[str, object] = {
        "version": operation.version,
        "id": str(operation.id),
        "account_id": str(operation.account_id),
        "kind": operation.kind,
        "phase": operation.phase,
    }
    if operation.container_id is not None:
        data["container_id"] = operation.container_id
    if operation.media_id is not None:
        data["media_id"] = operation.media_id
    if operation.outcome_code is not None:
        data["outcome_code"] = operation.outcome_code
    return json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode_operation(data: object, requested_id: UUID) -> LocalOperation:
    if not isinstance(data, dict):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", requested_id)
    journal_data = cast(dict[str, object], data)
    keys: frozenset[str] = frozenset(journal_data)
    if not _REQUIRED_JOURNAL_KEYS <= keys or not keys <= _JOURNAL_KEYS:
        raise StandaloneMutationError("OPERATION_STATE_INVALID", requested_id)
    version = journal_data.get("version")
    raw_id = journal_data.get("id")
    raw_account_id = journal_data.get("account_id")
    kind = journal_data.get("kind")
    phase = journal_data.get("phase")
    if (
        type(version) is not int
        or not isinstance(raw_id, str)
        or not isinstance(raw_account_id, str)
        or not isinstance(kind, str)
        or not isinstance(phase, str)
    ):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", requested_id)
    try:
        operation_id = UUID(raw_id)
        account_id = UUID(raw_account_id)
    except ValueError, AttributeError:
        raise StandaloneMutationError("OPERATION_STATE_INVALID", requested_id) from None
    if (
        str(operation_id) != raw_id
        or str(account_id) != raw_account_id
        or operation_id != requested_id
    ):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", requested_id)
    container_id = journal_data.get("container_id")
    media_id = journal_data.get("media_id")
    outcome_code = journal_data.get("outcome_code")
    if (
        (container_id is not None and not isinstance(container_id, str))
        or (media_id is not None and not isinstance(media_id, str))
        or (outcome_code is not None and not isinstance(outcome_code, str))
    ):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", requested_id)
    operation = LocalOperation(
        version=version,
        id=operation_id,
        account_id=account_id,
        kind=kind,
        phase=phase,
        container_id=container_id,
        media_id=media_id,
        outcome_code=outcome_code,
    )
    try:
        _validate_operation(operation)
    except StandaloneMutationError:
        raise StandaloneMutationError("OPERATION_STATE_INVALID", requested_id) from None
    return operation


def _cleanup_temporary(path: Path | None, descriptor: int | None) -> None:
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
