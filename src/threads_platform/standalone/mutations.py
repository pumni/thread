"""Local, journaled Threads API mutations for the standalone command line."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, NoReturn, TypeGuard, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import SecretStr

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
_NUMERIC_HOST = re.compile(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*\Z")
_JOURNAL_VERSION = 1
_JOURNAL_KIND = "POST_TEXT"
_CAROUSEL_KIND = "POST_CAROUSEL"
_MODERATION_KIND = "MODERATE_REPLY"
_MODERATION_ACTIONS = frozenset({"hide", "unhide", "approve", "ignore"})
_MODERATION_PHASES = frozenset({"RECEIVED", "MUTATION_REQUESTED", "CONFIRMED", "AMBIGUOUS"})
_MODERATION_ONLY_PHASES = frozenset({"MUTATION_REQUESTED", "CONFIRMED"})
_JOURNAL_KINDS = frozenset(
    {
        _JOURNAL_KIND,
        "CREATE_REPLY",
        "POST_IMAGE",
        "POST_VIDEO",
        _CAROUSEL_KIND,
        _MODERATION_KIND,
    }
)
_MAX_CAROUSEL_MANIFEST_BYTES = 65_536
_MAX_CAROUSEL_JSON_DEPTH = 3
_MIN_CAROUSEL_ITEMS = 2
_MAX_CAROUSEL_ITEMS = 20
_CONTAINER_PROCESSING_TIMEOUT_SECONDS = 30.0
_CONTAINER_PROCESSING_MAX_CHECKS = 10
_CONTAINER_PROCESSING_POLL_INTERVAL_SECONDS = 3.0
_JOURNAL_PHASES = frozenset(
    {
        "RECEIVED",
        "CHILDREN_CREATING",
        "MUTATION_REQUESTED",
        "CONTAINER_CREATED",
        "PUBLISH_REQUESTED",
        "PUBLISHED",
        "CONFIRMED",
        "AMBIGUOUS",
        "FAILED_FINAL",
    }
)
_JOURNAL_KEYS = frozenset(
    {
        "version",
        "id",
        "account_id",
        "kind",
        "phase",
        "container_id",
        "media_id",
        "outcome_code",
        "child_container_ids",
        "action",
    }
)
_REQUIRED_JOURNAL_KEYS = frozenset({"version", "id", "account_id", "kind", "phase"})
# Required for 20 bounded child IDs plus the parent and published media IDs in one v1 record.
_MAX_JOURNAL_BYTES = 8192
_TRANSITIONS = {
    "RECEIVED": frozenset({"CHILDREN_CREATING", "CONTAINER_CREATED", "FAILED_FINAL"}),
    "CHILDREN_CREATING": frozenset({"CHILDREN_CREATING", "CONTAINER_CREATED", "FAILED_FINAL"}),
    "CONTAINER_CREATED": frozenset({"PUBLISH_REQUESTED", "FAILED_FINAL"}),
    "PUBLISH_REQUESTED": frozenset({"PUBLISHED", "AMBIGUOUS"}),
}
_MODERATION_TRANSITIONS = {
    "RECEIVED": frozenset({"MUTATION_REQUESTED"}),
    "MUTATION_REQUESTED": frozenset({"CONFIRMED", "AMBIGUOUS"}),
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
    child_container_ids: tuple[str, ...] = ()
    action: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedTextResult:
    operation_id: UUID
    media_id: str


@dataclass(frozen=True, slots=True)
class CreatedReplyResult:
    operation_id: UUID
    reply_id: str


@dataclass(frozen=True, slots=True)
class PublishedMediaResult:
    operation_id: UUID
    media_id: str


@dataclass(frozen=True, slots=True)
class CarouselItem:
    media_type: Literal["IMAGE", "VIDEO"]
    url: str = field(repr=False)
    alt_text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CarouselManifest:
    items: tuple[CarouselItem, ...]
    text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class PublishedCarouselResult:
    operation_id: UUID
    media_id: str


@dataclass(frozen=True, slots=True)
class ModeratedReplyResult:
    operation_id: UUID
    action: str


def load_carousel_manifest(path: Path) -> CarouselManifest:
    """Read and fully validate one bounded local carousel manifest."""

    try:
        path_metadata = path.stat()
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_size > _MAX_CAROUSEL_MANIFEST_BYTES
        ):
            raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > _MAX_CAROUSEL_MANIFEST_BYTES
            ):
                raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
            contents = stream.read(_MAX_CAROUSEL_MANIFEST_BYTES + 1)
    except OSError, RuntimeError, ValueError:
        raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST") from None

    if len(contents) > _MAX_CAROUSEL_MANIFEST_BYTES:
        raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
    try:
        document = contents.decode("utf-8")
        _validate_carousel_json_depth(document)
        raw_manifest: object = json.loads(
            document,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_json_constant,
        )
    except UnicodeDecodeError, ValueError, RecursionError:
        raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST") from None
    return _parse_carousel_manifest(raw_manifest)


def _validate_carousel_json_depth(document: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in document:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_CAROUSEL_JSON_DEPTH:
                raise ValueError
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise ValueError
    if depth != 0 or in_string or escaped:
        raise ValueError


def _parse_carousel_manifest(value: object) -> CarouselManifest:
    if not isinstance(value, dict):
        raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
    document = cast(dict[str, object], value)
    keys = set(document)
    if "items" not in keys or not keys <= {"text", "items"}:
        raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")

    text: str | None = None
    if "text" in document:
        raw_text = document["text"]
        if type(raw_text) is not str:
            raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
        _validate_optional_post_text(raw_text)
        text = raw_text

    raw_items = document["items"]
    if not isinstance(raw_items, list):
        raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
    item_values = cast(list[object], raw_items)
    if not _MIN_CAROUSEL_ITEMS <= len(item_values) <= _MAX_CAROUSEL_ITEMS:
        raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")

    items: list[CarouselItem] = []
    for raw_item in item_values:
        if not isinstance(raw_item, dict):
            raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
        item = cast(dict[str, object], raw_item)
        item_keys = set(item)
        if item_keys not in (
            {"media_type", "url"},
            {"media_type", "url", "alt_text"},
        ):
            raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
        media_type = item["media_type"]
        url = item["url"]
        if type(media_type) is not str or media_type not in {"IMAGE", "VIDEO"}:
            raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
        if type(url) is not str:
            raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
        alt_text: str | None = None
        if "alt_text" in item:
            raw_alt_text = item["alt_text"]
            if type(raw_alt_text) is not str:
                raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
            alt_text = raw_alt_text
        validate_media_post_inputs(url, None, alt_text)
        items.append(CarouselItem(cast(Literal["IMAGE", "VIDEO"], media_type), url, alt_text))
    return CarouselManifest(items=tuple(items), text=text)


def _validate_carousel_manifest(value: object) -> CarouselManifest:
    if not isinstance(value, CarouselManifest) or type(value.items) is not tuple:
        raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
    if not _MIN_CAROUSEL_ITEMS <= len(value.items) <= _MAX_CAROUSEL_ITEMS:
        raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
    if value.text is not None:
        if type(value.text) is not str:
            raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
        _validate_optional_post_text(value.text)

    items: list[CarouselItem] = []
    for raw_item in cast(tuple[object, ...], value.items):
        if not isinstance(raw_item, CarouselItem) or raw_item.media_type not in {
            "IMAGE",
            "VIDEO",
        }:
            raise StandaloneMutationError("INVALID_CAROUSEL_MANIFEST")
        item = raw_item
        validate_media_post_inputs(item.url, None, item.alt_text)
        items.append(item)
    return CarouselManifest(items=tuple(items), text=value.text)


class LocalOperationStore:
    """Strict, atomic JSON journal for standalone API mutations."""

    def __init__(self, data_root: Path) -> None:
        self._configured_root = Path(data_root).absolute()
        try:
            self._root = self._configured_root.resolve(strict=False)
        except OSError, RuntimeError:
            raise StandaloneMutationError("OPERATION_STATE_INVALID") from None

    def create_received(
        self,
        account_id: UUID,
        *,
        kind: str = _JOURNAL_KIND,
        action: str | None = None,
    ) -> LocalOperation:
        if kind not in _JOURNAL_KINDS:
            raise StandaloneMutationError("OPERATION_STATE_INVALID")
        operation = LocalOperation(
            version=_JOURNAL_VERSION,
            id=uuid4(),
            account_id=account_id,
            kind=kind,
            phase="RECEIVED",
            action=action,
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
        allowed_phases = (
            _MODERATION_TRANSITIONS.get(current.phase, frozenset())
            if current.kind == _MODERATION_KIND
            else _TRANSITIONS.get(current.phase, frozenset())
        )
        if (
            current.account_id != operation.account_id
            or current.kind != operation.kind
            or current.action != operation.action
            or operation.phase not in allowed_phases
            or (
                current.kind == _CAROUSEL_KIND
                and not _valid_carousel_child_transition(current, operation)
            )
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
        operation_id, media_id = await self._publish_container(
            alias,
            MediaContainerRequest(media_type="TEXT", text=text),
            kind="POST_TEXT",
            reply_quota=False,
        )
        return PublishedTextResult(operation_id=operation_id, media_id=media_id)

    async def publish_image(
        self,
        alias: str,
        image_url: str,
        text: str | None = None,
        *,
        alt_text: str | None = None,
    ) -> PublishedMediaResult:
        validate_media_post_inputs(image_url, text, alt_text)
        operation_id, media_id = await self._publish_container(
            alias,
            MediaContainerRequest(
                media_type="IMAGE",
                image_url=image_url,
                text=text,
                alt_text=alt_text,
            ),
            kind="POST_IMAGE",
            reply_quota=False,
            wait_for_processing=True,
        )
        return PublishedMediaResult(operation_id=operation_id, media_id=media_id)

    async def publish_video(
        self,
        alias: str,
        video_url: str,
        text: str | None = None,
        *,
        alt_text: str | None = None,
    ) -> PublishedMediaResult:
        validate_media_post_inputs(video_url, text, alt_text)
        operation_id, media_id = await self._publish_container(
            alias,
            MediaContainerRequest(
                media_type="VIDEO",
                video_url=video_url,
                text=text,
                alt_text=alt_text,
            ),
            kind="POST_VIDEO",
            reply_quota=False,
            wait_for_processing=True,
        )
        return PublishedMediaResult(operation_id=operation_id, media_id=media_id)

    async def publish_carousel(
        self,
        alias: str,
        manifest: CarouselManifest,
    ) -> PublishedCarouselResult:
        validated_manifest = _validate_carousel_manifest(manifest)
        operation_id, media_id = await self._publish_carousel(alias, validated_manifest)
        return PublishedCarouselResult(operation_id=operation_id, media_id=media_id)

    async def create_reply(
        self,
        alias: str,
        thread_id: str,
        text: str,
        *,
        parent_reply_id: str | None = None,
    ) -> CreatedReplyResult:
        if not _valid_remote_id(thread_id):
            raise StandaloneMutationError("INVALID_THREAD_ID")
        if parent_reply_id is not None and not _valid_remote_id(parent_reply_id):
            raise StandaloneMutationError("INVALID_REPLY_ID")
        _validate_mutation_text(text, "INVALID_REPLY_TEXT")
        reply_to_id = parent_reply_id if parent_reply_id is not None else thread_id
        operation_id, reply_id = await self._publish_container(
            alias,
            MediaContainerRequest(media_type="TEXT", text=text, reply_to_id=reply_to_id),
            kind="CREATE_REPLY",
            reply_quota=True,
        )
        return CreatedReplyResult(operation_id=operation_id, reply_id=reply_id)

    async def moderate_reply(
        self,
        alias: str,
        reply_id: str,
        action: str,
    ) -> ModeratedReplyResult:
        validated_reply_id, validated_action = validate_reply_moderation_inputs(reply_id, action)
        operation_id = await self._moderate_reply(alias, validated_reply_id, validated_action)
        return ModeratedReplyResult(operation_id=operation_id, action=validated_action)

    async def _moderate_reply(
        self,
        alias: str,
        reply_id: str,
        action: str,
    ) -> UUID:
        account = self._accounts.get(alias)
        lock = FilesystemProcessLock(_account_lock_path(self._root, account.id))
        try:
            lock.acquire()
        except ProcessAlreadyRunning:
            raise StandaloneMutationError("ACCOUNT_BUSY") from None
        except OSError:
            raise StandaloneMutationError("LOCAL_OPERATION_UNAVAILABLE") from None

        try:
            if account.credential_ref is None:
                raise ThreadsCredentialError(ThreadsCredentialErrorCode.NOT_CONFIGURED)
            try:
                token = await self._secret_resolver.resolve(account.credential_ref)
            except ThreadsCredentialError:
                raise
            except Exception:
                raise StandaloneMutationError("THREADS_CREDENTIAL_SECRET_UNAVAILABLE") from None
            operation = self._operations.create_received(
                account.id,
                kind=_MODERATION_KIND,
                action=action,
            )
            try:
                requested = self._operations.update(replace(operation, phase="MUTATION_REQUESTED"))
            except Exception:
                raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id) from None

            try:
                if action in {"hide", "unhide"}:
                    response: object = await self._api.manage_reply(
                        token, reply_id, hide=action == "hide"
                    )
                else:
                    response = await self._api.manage_pending_reply(
                        token, reply_id, approve=action == "approve"
                    )
            except asyncio.CancelledError:
                self._raise_moderation_ambiguous(requested)
            except Exception:
                self._raise_moderation_ambiguous(requested)
            except BaseException:
                self._best_effort_moderation_ambiguous(requested)
                raise

            if response is not None:
                self._raise_moderation_ambiguous(requested)

            try:
                self._operations.update(replace(requested, phase="CONFIRMED"))
            except Exception:
                # The durable MUTATION_REQUESTED record prevents any blind retry.
                raise StandaloneMutationError(
                    "MODERATION_OUTCOME_AMBIGUOUS", requested.id
                ) from None
            except BaseException:
                self._best_effort_moderation_ambiguous(requested)
                raise
            return requested.id
        finally:
            try:
                lock.release()
            except OSError:
                pass

    async def _publish_container(
        self,
        alias: str,
        request: MediaContainerRequest,
        *,
        kind: str,
        reply_quota: bool,
        wait_for_processing: bool = False,
    ) -> tuple[UUID, str]:
        account = self._accounts.get(alias)
        lock = FilesystemProcessLock(_account_lock_path(self._root, account.id))
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
            usage, total = (
                (quota.reply_usage, quota.reply_total)
                if reply_quota
                else (quota.usage, quota.total)
            )
            if usage is not None and total is not None and usage >= total:
                quota_code = (
                    "THREADS_REPLY_QUOTA_REACHED"
                    if reply_quota
                    else "THREADS_PUBLISHING_QUOTA_REACHED"
                )
                raise StandaloneMutationError(quota_code)

            operation = self._operations.create_received(account.id, kind=kind)
            try:
                container = await self._api.create_container(token, request)
            except asyncio.CancelledError:
                self._best_effort_failed_final(operation, "OPERATION_CANCELLED")
                raise StandaloneMutationError("OPERATION_CANCELLED", operation.id) from None
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
            if wait_for_processing:
                try:
                    await self._wait_for_container_ready(token, container_id)
                except asyncio.CancelledError:
                    self._best_effort_failed_final(operation, "OPERATION_CANCELLED")
                    raise StandaloneMutationError("OPERATION_CANCELLED", operation.id) from None
                except StandaloneMutationError as error:
                    self._best_effort_failed_final(operation, error.code)
                    raise StandaloneMutationError(error.code, operation.id) from None
                except Exception as error:
                    code = _safe_exception_code(error)
                    self._best_effort_failed_final(operation, code)
                    raise StandaloneMutationError(code, operation.id) from None

            return await self._publish_ready_container(token, operation)
        finally:
            try:
                lock.release()
            except OSError:
                # The lock implementation closes its stream even when unlock fails.
                # Cleanup must not replace the primary publish outcome.
                pass

    async def _publish_carousel(
        self,
        alias: str,
        manifest: CarouselManifest,
    ) -> tuple[UUID, str]:
        account = self._accounts.get(alias)
        lock = FilesystemProcessLock(_account_lock_path(self._root, account.id))
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

            operation = self._operations.create_received(account.id, kind=_CAROUSEL_KIND)
            try:
                operation = self._persist_required(replace(operation, phase="CHILDREN_CREATING"))
            except StandaloneMutationError as error:
                self._best_effort_failed_final(operation, error.code)
                raise StandaloneMutationError(error.code, operation.id) from None

            for item in manifest.items:
                request = MediaContainerRequest(
                    media_type=item.media_type,
                    image_url=item.url if item.media_type == "IMAGE" else None,
                    video_url=item.url if item.media_type == "VIDEO" else None,
                    alt_text=item.alt_text,
                    is_carousel_item=True,
                )
                try:
                    child = await self._api.create_container(token, request)
                except asyncio.CancelledError:
                    self._best_effort_failed_final(operation, "OPERATION_CANCELLED")
                    raise StandaloneMutationError("OPERATION_CANCELLED", operation.id) from None
                except Exception as error:
                    code = _safe_exception_code(error)
                    self._best_effort_failed_final(operation, code)
                    raise StandaloneMutationError(code, operation.id) from None

                child_id = getattr(child, "container_id", None)
                if not _valid_remote_id(child_id) or child_id in operation.child_container_ids:
                    code = "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
                    self._best_effort_failed_final(operation, code)
                    raise StandaloneMutationError(code, operation.id)

                try:
                    operation = self._persist_required(
                        replace(
                            operation,
                            child_container_ids=(*operation.child_container_ids, child_id),
                        )
                    )
                except StandaloneMutationError as error:
                    self._best_effort_failed_final(operation, error.code)
                    raise StandaloneMutationError(error.code, operation.id) from None

                try:
                    await self._wait_for_container_ready(token, child_id)
                except asyncio.CancelledError:
                    self._best_effort_failed_final(operation, "OPERATION_CANCELLED")
                    raise StandaloneMutationError("OPERATION_CANCELLED", operation.id) from None
                except StandaloneMutationError as error:
                    self._best_effort_failed_final(operation, error.code)
                    raise StandaloneMutationError(error.code, operation.id) from None
                except Exception as error:
                    code = _safe_exception_code(error)
                    self._best_effort_failed_final(operation, code)
                    raise StandaloneMutationError(code, operation.id) from None

            parent_request = MediaContainerRequest(
                media_type="CAROUSEL",
                text=manifest.text,
                children=operation.child_container_ids,
            )
            try:
                parent = await self._api.create_container(token, parent_request)
            except asyncio.CancelledError:
                self._best_effort_failed_final(operation, "OPERATION_CANCELLED")
                raise StandaloneMutationError("OPERATION_CANCELLED", operation.id) from None
            except Exception as error:
                code = _safe_exception_code(error)
                self._best_effort_failed_final(operation, code)
                raise StandaloneMutationError(code, operation.id) from None

            parent_id = getattr(parent, "container_id", None)
            if not _valid_remote_id(parent_id) or parent_id in operation.child_container_ids:
                code = "THREADS_DOCUMENTATION_CONTRACT_MISMATCH"
                self._best_effort_failed_final(operation, code)
                raise StandaloneMutationError(code, operation.id)
            try:
                operation = self._persist_required(
                    replace(operation, phase="CONTAINER_CREATED", container_id=parent_id)
                )
            except StandaloneMutationError as error:
                self._best_effort_failed_final(operation, error.code)
                raise StandaloneMutationError(error.code, operation.id) from None

            try:
                await self._wait_for_container_ready(token, parent_id)
            except asyncio.CancelledError:
                self._best_effort_failed_final(operation, "OPERATION_CANCELLED")
                raise StandaloneMutationError("OPERATION_CANCELLED", operation.id) from None
            except StandaloneMutationError as error:
                self._best_effort_failed_final(operation, error.code)
                raise StandaloneMutationError(error.code, operation.id) from None
            except Exception as error:
                code = _safe_exception_code(error)
                self._best_effort_failed_final(operation, code)
                raise StandaloneMutationError(code, operation.id) from None

            return await self._publish_ready_container(token, operation)
        finally:
            try:
                lock.release()
            except OSError:
                pass

    async def _publish_ready_container(
        self,
        token: SecretStr,
        operation: LocalOperation,
    ) -> tuple[UUID, str]:
        container_id = operation.container_id
        if container_id is None:
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        publish_requested = self._persist_required(replace(operation, phase="PUBLISH_REQUESTED"))
        try:
            media_id = await self._api.publish_container(token, container_id)
        except asyncio.CancelledError:
            self._raise_ambiguous(publish_requested, "PUBLISH_OUTCOME_AMBIGUOUS")
        except Exception as error:
            self._raise_ambiguous(publish_requested, _safe_exception_code(error))
        except BaseException:
            self._best_effort_ambiguous(publish_requested, "PUBLISH_OUTCOME_AMBIGUOUS")
            raise
        if not _valid_remote_id(media_id):
            self._raise_ambiguous(publish_requested, "THREADS_DOCUMENTATION_CONTRACT_MISMATCH")

        try:
            published = self._operations.update(
                replace(publish_requested, phase="PUBLISHED", media_id=media_id)
            )
        except BaseException:
            self._raise_ambiguous(publish_requested, "OPERATION_STATE_INVALID")
        return published.id, media_id

    async def _wait_for_container_ready(self, token: SecretStr, container_id: str) -> None:
        try:
            async with asyncio.timeout(_CONTAINER_PROCESSING_TIMEOUT_SECONDS):
                for check_index in range(_CONTAINER_PROCESSING_MAX_CHECKS):
                    container = await self._api.get_container(token, container_id)
                    if getattr(container, "container_id", None) != container_id:
                        raise StandaloneMutationError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
                    status = getattr(container, "status", None)
                    if status == "FINISHED":
                        return
                    if status == "ERROR":
                        raise StandaloneMutationError("THREADS_CONTAINER_ERROR")
                    if status == "EXPIRED":
                        raise StandaloneMutationError("THREADS_CONTAINER_EXPIRED")
                    if status != "IN_PROGRESS":
                        raise StandaloneMutationError("THREADS_DOCUMENTATION_CONTRACT_MISMATCH")
                    if check_index == _CONTAINER_PROCESSING_MAX_CHECKS - 1:
                        raise StandaloneMutationError("THREADS_CONTAINER_PROCESSING_TIMEOUT")
                    await asyncio.sleep(_CONTAINER_PROCESSING_POLL_INTERVAL_SECONDS)
        except TimeoutError:
            raise StandaloneMutationError("THREADS_CONTAINER_PROCESSING_TIMEOUT") from None

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
        self._best_effort_ambiguous(operation, code)
        raise StandaloneMutationError("PUBLISH_OUTCOME_AMBIGUOUS", operation.id) from None

    def _best_effort_ambiguous(self, operation: LocalOperation, code: str) -> None:
        try:
            self._operations.update(replace(operation, phase="AMBIGUOUS", outcome_code=code))
        except StandaloneMutationError:
            pass

    def _raise_moderation_ambiguous(self, operation: LocalOperation) -> NoReturn:
        self._best_effort_moderation_ambiguous(operation)
        raise StandaloneMutationError("MODERATION_OUTCOME_AMBIGUOUS", operation.id) from None

    def _best_effort_moderation_ambiguous(self, operation: LocalOperation) -> None:
        try:
            self._operations.update(
                replace(
                    operation,
                    phase="AMBIGUOUS",
                    outcome_code="MODERATION_OUTCOME_AMBIGUOUS",
                )
            )
        except BaseException:
            pass


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
    _validate_mutation_text(text, "INVALID_POST_TEXT")


def _validate_optional_post_text(text: object) -> None:
    if text is not None:
        _validate_mutation_text(text, "INVALID_POST_TEXT")


def validate_media_post_inputs(media_url: object, text: object, alt_text: object) -> None:
    if not _valid_media_url(media_url):
        raise StandaloneMutationError("INVALID_MEDIA_URL")
    _validate_optional_post_text(text)
    _validate_alt_text(alt_text)


def _validate_mutation_text(text: object, code: str) -> None:
    if not isinstance(text, str) or not 1 <= len(text) <= 500 or not text.strip():
        raise StandaloneMutationError(code)


def _validate_alt_text(alt_text: object) -> None:
    if alt_text is not None and (not isinstance(alt_text, str) or len(alt_text) > 1000):
        raise StandaloneMutationError("INVALID_ALT_TEXT")


def _valid_media_url(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    if (
        "\\" in value
        or re.search(r"%(?![0-9a-fA-F]{2})", value) is not None
        or any(
            character.isspace() or unicodedata.category(character) == "Cc" for character in value
        )
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "%" in hostname
    ):
        return False
    if port is not None and not 0 <= port <= 65535:
        return False

    normalized_host = hostname[:-1] if hostname.endswith(".") else hostname
    if not normalized_host or normalized_host.endswith("."):
        return False
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if _NUMERIC_HOST.fullmatch(normalized_host) is not None:
            return False
        try:
            ascii_host = normalized_host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return False
        if (
            ascii_host == "localhost"
            or ascii_host.endswith(".localhost")
            or ascii_host == "localhost.localdomain"
            or ascii_host.endswith(".local")
        ):
            return False
        labels = ascii_host.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
            for label in labels
        ):
            return False
    else:
        if address.is_loopback or address.is_unspecified:
            return False
        if isinstance(address, ipaddress.IPv6Address):
            mapped_address = address.ipv4_mapped
            if mapped_address is not None and (
                mapped_address.is_loopback or mapped_address.is_unspecified
            ):
                return False
    return True


def _valid_remote_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _REMOTE_ID.fullmatch(value) is not None


def validate_reply_moderation_inputs(reply_id: object, action: object) -> tuple[str, str]:
    if type(reply_id) is not str or reply_id in {".", ".."} or not _valid_remote_id(reply_id):
        raise StandaloneMutationError("INVALID_REPLY_ID")
    if type(action) is not str or action not in _MODERATION_ACTIONS:
        raise StandaloneMutationError("INVALID_MODERATION_ACTION")
    return reply_id, action


def _safe_exception_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code) is not None:
        return code
    return "THREADS_TRANSPORT_FAILURE"


def _validate_operation(operation: LocalOperation) -> None:
    if (
        type(operation.version) is not int
        or operation.version != _JOURNAL_VERSION
        or operation.kind not in _JOURNAL_KINDS
        or operation.phase not in _JOURNAL_PHASES
        or type(operation.child_container_ids) is not tuple
        or len(operation.child_container_ids) > _MAX_CAROUSEL_ITEMS
        or any(not _valid_remote_id(child_id) for child_id in operation.child_container_ids)
        or len(set(operation.child_container_ids)) != len(operation.child_container_ids)
    ):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
    if operation.container_id is not None and not _valid_remote_id(operation.container_id):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
    if operation.media_id is not None and not _valid_remote_id(operation.media_id):
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
    if operation.outcome_code is not None and _SAFE_CODE.fullmatch(operation.outcome_code) is None:
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
    if operation.kind == _MODERATION_KIND:
        if (
            type(operation.action) is not str
            or operation.action not in _MODERATION_ACTIONS
            or operation.phase not in _MODERATION_PHASES
            or operation.container_id is not None
            or operation.media_id is not None
            or operation.child_container_ids
        ):
            raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
        valid_shape = {
            "RECEIVED": operation.outcome_code is None,
            "MUTATION_REQUESTED": operation.outcome_code is None,
            "CONFIRMED": operation.outcome_code is None,
            "AMBIGUOUS": operation.outcome_code is not None,
        }
    elif operation.action is not None or operation.phase in _MODERATION_ONLY_PHASES:
        raise StandaloneMutationError("OPERATION_STATE_INVALID", operation.id)
    elif operation.kind == _CAROUSEL_KIND:
        has_ready_children = _MIN_CAROUSEL_ITEMS <= len(operation.child_container_ids)
        parent_is_distinct = (
            operation.container_id is None
            or operation.container_id not in operation.child_container_ids
        )
        valid_shape = {
            "RECEIVED": operation.container_id is None
            and operation.media_id is None
            and operation.outcome_code is None
            and not operation.child_container_ids,
            "CHILDREN_CREATING": operation.container_id is None
            and operation.media_id is None
            and operation.outcome_code is None,
            "CONTAINER_CREATED": operation.container_id is not None
            and operation.media_id is None
            and operation.outcome_code is None
            and has_ready_children
            and parent_is_distinct,
            "PUBLISH_REQUESTED": operation.container_id is not None
            and operation.media_id is None
            and operation.outcome_code is None
            and has_ready_children
            and parent_is_distinct,
            "PUBLISHED": operation.container_id is not None
            and operation.media_id is not None
            and operation.outcome_code is None
            and has_ready_children
            and parent_is_distinct,
            "AMBIGUOUS": operation.container_id is not None
            and operation.media_id is None
            and operation.outcome_code is not None
            and has_ready_children
            and parent_is_distinct,
            "FAILED_FINAL": operation.media_id is None
            and operation.outcome_code is not None
            and parent_is_distinct,
        }
    else:
        if operation.child_container_ids or operation.phase == "CHILDREN_CREATING":
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


def _valid_carousel_child_transition(
    current: LocalOperation,
    operation: LocalOperation,
) -> bool:
    current_children = current.child_container_ids
    next_children = operation.child_container_ids
    if len(next_children) < len(current_children) or next_children[: len(current_children)] != (
        current_children
    ):
        return False
    if current.phase == "RECEIVED":
        return (
            operation.phase in {"CHILDREN_CREATING", "FAILED_FINAL"}
            and not current_children
            and not next_children
        )
    if current.phase == "CHILDREN_CREATING" and operation.phase == "CHILDREN_CREATING":
        return len(next_children) == len(current_children) + 1
    return next_children == current_children


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
    if operation.child_container_ids:
        data["child_container_ids"] = list(operation.child_container_ids)
    if operation.action is not None:
        data["action"] = operation.action
    return json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> NoReturn:
    raise ValueError(value)


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
    raw_child_ids = journal_data.get("child_container_ids", [])
    raw_action = journal_data.get("action")
    if (
        (container_id is not None and not isinstance(container_id, str))
        or (media_id is not None and not isinstance(media_id, str))
        or (outcome_code is not None and not isinstance(outcome_code, str))
        or not isinstance(raw_child_ids, list)
        or any(type(child_id) is not str for child_id in cast(list[object], raw_child_ids))
        or (kind != _CAROUSEL_KIND and "child_container_ids" in journal_data)
        or (raw_action is not None and type(raw_action) is not str)
        or (kind != _MODERATION_KIND and "action" in journal_data)
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
        child_container_ids=tuple(cast(list[str], raw_child_ids)),
        action=raw_action,
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
