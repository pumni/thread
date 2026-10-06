"""Secret-safe local receipts and dedupe state for standalone nurture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from threads_platform.application.ports.process_lock import ProcessAlreadyRunning
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.standalone.nurture import NurturePresetV1

NurtureOutcome = Literal["RUNNING", "SUCCESS", "FAILED", "INTERRUPTED", "AMBIGUOUS"]
NurtureActionState = Literal["NONE", "PENDING", "CONFIRMED", "AMBIGUOUS"]

MAX_NURTURE_TARGETS = 2_000
MAX_NURTURE_RUN_RECEIPTS = 2_000
MAX_NURTURE_RUN_BYTES = 8_192
MAX_NURTURE_TARGET_STATE_BYTES = 2_097_152
_MAX_COUNTER = 1_000_000
_MAX_DECISION_CODES = 32
_MAX_OPERATION_IDS = 6
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SAFE_STAGE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_PRESET_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_REMOTE_THREAD_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_OUTCOMES = frozenset({"RUNNING", "SUCCESS", "FAILED", "INTERRUPTED", "AMBIGUOUS"})
_ACTION_STATES = frozenset({"NONE", "PENDING", "CONFIRMED", "AMBIGUOUS"})
_COUNTER_FIELDS = (
    "discovered_count",
    "deduped_count",
    "selected_count",
    "enriched_count",
    "replied_count",
    "published_count",
    "skipped_count",
)
_RUN_KEYS = frozenset(
    {
        "version",
        "id",
        "account_id",
        "preset_id",
        "preset_version",
        "started_at",
        "finished_at",
        "outcome",
        *_COUNTER_FIELDS,
        "decision_codes",
        "operation_ids",
        "error_code",
        "failed_stage",
    }
)
_TARGET_STATE_KEYS = frozenset({"version", "account_id", "preset_id", "targets"})
_TARGET_KEYS = frozenset(
    {
        "fingerprint",
        "first_seen_at",
        "last_seen_at",
        "last_decision_code",
        "last_action_at",
        "action_state",
        "last_run_id",
        "last_operation_id",
    }
)


class NurtureStateError(Exception):
    """A bounded, data-safe error from Nurture's local persistence boundary."""

    code: str

    def __init__(self, code: str) -> None:
        if code not in {
            "NURTURE_STATE_INVALID",
            "NURTURE_VERSION_UNSUPPORTED",
            "NURTURE_RUN_NOT_FOUND",
            "NURTURE_BUSY",
            "NURTURE_TARGET_PENDING",
            "NURTURE_TARGET_AMBIGUOUS",
            "NURTURE_TARGET_RESERVED",
            "NURTURE_STATE_CAP_REACHED",
        }:
            raise ValueError("unsupported Nurture state error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class NurtureRunV1:
    version: int
    id: UUID
    account_id: UUID
    preset_id: str
    preset_version: int
    started_at: datetime
    finished_at: datetime | None
    outcome: NurtureOutcome
    discovered_count: int = 0
    deduped_count: int = 0
    selected_count: int = 0
    enriched_count: int = 0
    replied_count: int = 0
    published_count: int = 0
    skipped_count: int = 0
    decision_codes: tuple[str, ...] = ()
    operation_ids: tuple[UUID, ...] = ()
    error_code: str | None = None
    failed_stage: str | None = None

    def __post_init__(self) -> None:
        _validate_run(self)

    def __repr__(self) -> str:
        return (
            "NurtureRunV1("
            f"id={self.id}, outcome={self.outcome}, preset_id={self.preset_id!r}, "
            f"decision_codes={len(self.decision_codes)}, operation_ids={len(self.operation_ids)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class NurtureTargetV1:
    fingerprint: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_decision_code: str
    last_action_at: datetime | None
    action_state: NurtureActionState
    last_run_id: UUID
    last_operation_id: UUID | None

    def __post_init__(self) -> None:
        _validate_target(self)

    def __repr__(self) -> str:
        return (
            f"NurtureTargetV1(fingerprint={self.fingerprint[:12]}…, "
            f"action_state={self.action_state})"
        )


@dataclass(frozen=True, slots=True)
class NurtureTargetStateV1:
    version: int
    account_id: UUID
    preset_id: str
    targets: tuple[NurtureTargetV1, ...]

    def __post_init__(self) -> None:
        _validate_target_document(self)


class NurtureStore:
    """Strict filesystem receipts and target state under one Standalone root."""

    def __init__(self, data_root: Path) -> None:
        self._configured_root = Path(data_root).absolute()
        try:
            self._root = self._configured_root.resolve(strict=False)
        except OSError, RuntimeError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None

    def acquire_account_lock(self, account_id: UUID) -> NurtureAccountLock:
        _validate_uuid(account_id, version=4)
        return NurtureAccountLock(self, account_id)

    def create_run(
        self,
        owner: NurtureAccountLock,
        preset: NurturePresetV1,
        *,
        now: datetime | None,
    ) -> NurtureRunV1:
        owner.require_held(self)
        preset = _validated_preset(preset)
        started_at = _as_utc(datetime.now(UTC) if now is None else now)
        run = NurtureRunV1(
            version=1,
            id=uuid4(),
            account_id=owner.account_id,
            preset_id=preset.id,
            preset_version=preset.version,
            started_at=started_at,
            finished_at=None,
            outcome="RUNNING",
        )
        directory = self._run_directory(run.account_id, create=True)
        assert directory is not None
        if len(self._run_receipt_paths(directory)) >= MAX_NURTURE_RUN_RECEIPTS:
            raise NurtureStateError("NURTURE_STATE_CAP_REACHED")
        self._write_new_run(directory / f"{run.id}.json", run)
        return run

    def read_run(self, owner: NurtureAccountLock, run_id: UUID) -> NurtureRunV1:
        owner.require_held(self)
        account_id = owner.account_id
        _validate_uuid(account_id, version=4)
        _validate_uuid(run_id, version=4)
        directory = self._run_directory(account_id, create=False)
        if directory is None:
            raise NurtureStateError("NURTURE_RUN_NOT_FOUND")
        path = self._run_path(directory, run_id, must_exist=False)
        if path is None:
            raise NurtureStateError("NURTURE_RUN_NOT_FOUND")
        run = self._read_run(path, run_id)
        if run.account_id != account_id:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        return run

    def update_run(self, owner: NurtureAccountLock, updated: NurtureRunV1) -> NurtureRunV1:
        owner.require_held(self)
        if type(updated) is not NurtureRunV1:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        _validate_run(updated)
        if updated.account_id != owner.account_id:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        current = self.read_run(owner, updated.id)
        if (
            current.version != updated.version
            or current.id != updated.id
            or current.account_id != updated.account_id
            or current.preset_id != updated.preset_id
            or current.preset_version != updated.preset_version
            or current.started_at != updated.started_at
            or current.outcome != "RUNNING"
            or updated.outcome not in _OUTCOMES
            or (updated.outcome == "RUNNING" and updated.finished_at is not None)
            or (updated.outcome != "RUNNING" and updated.finished_at is None)
        ):
            raise NurtureStateError("NURTURE_STATE_INVALID")
        directory = self._run_directory(owner.account_id, create=False)
        assert directory is not None
        path = self._run_path(directory, updated.id, must_exist=True)
        assert path is not None
        self._write_replace(path, _encode_run(updated), MAX_NURTURE_RUN_BYTES)
        return updated

    def load_target_state(
        self,
        owner: NurtureAccountLock,
        preset: NurturePresetV1,
    ) -> NurtureTargetStateV1:
        owner.require_held(self)
        account_id = owner.account_id
        preset = _validated_preset(preset)
        directory = self._state_account_directory(account_id, create=False)
        if directory is None:
            return NurtureTargetStateV1(1, account_id, preset.id, ())
        path = self._target_state_path(directory, preset.id, must_exist=False)
        if path is None:
            return NurtureTargetStateV1(1, account_id, preset.id, ())
        document = self._read_target_document(path, account_id, preset.id)
        return document

    def save_target_state(
        self,
        owner: NurtureAccountLock,
        document: NurtureTargetStateV1,
    ) -> None:
        owner.require_held(self)
        if type(document) is not NurtureTargetStateV1:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        if owner.account_id != document.account_id:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        _validate_target_document(document)
        directory = self._state_account_directory(document.account_id, create=True)
        assert directory is not None
        path = self._target_state_path(directory, document.preset_id, must_exist=False)
        if path is None:
            current = NurtureTargetStateV1(1, owner.account_id, document.preset_id, ())
        else:
            current = self._read_target_document(path, owner.account_id, document.preset_id)
        _validate_target_transition(current, document)
        previous_targets = {target.fingerprint: target for target in current.targets}
        for target in document.targets:
            previous = previous_targets.get(target.fingerprint)
            if previous is None or replace(previous, last_seen_at=target.last_seen_at) != target:
                run = self.read_run(owner, target.last_run_id)
                if run.preset_id != document.preset_id or run.outcome != "RUNNING":
                    raise NurtureStateError("NURTURE_STATE_INVALID")
        encoded = _encode_target_document(document)
        if path is None:
            self._write_new_document(
                directory / f"{document.preset_id}.json", encoded, MAX_NURTURE_TARGET_STATE_BYTES
            )
        else:
            self._write_replace(path, encoded, MAX_NURTURE_TARGET_STATE_BYTES)

    def repair_stale_runs(self, owner: NurtureAccountLock) -> None:
        owner.require_held(self)
        directory = self._run_directory(owner.account_id, create=False)
        if directory is None:
            return
        json_paths = self._run_receipt_paths(directory)

        stale: list[tuple[Path, NurtureRunV1]] = []
        for _path, run_id in json_paths:
            run_path = self._run_path(directory, run_id, must_exist=True)
            assert run_path is not None
            run = self._read_run(run_path, run_id)
            if run.account_id == owner.account_id and run.outcome == "RUNNING":
                stale.append((run_path, run))
        for path, run in stale:
            interrupted = replace(
                run,
                finished_at=run.started_at,
                outcome="INTERRUPTED",
                error_code="INTERRUPTED",
                failed_stage="recovery",
            )
            self._write_replace(path, _encode_run(interrupted), MAX_NURTURE_RUN_BYTES)

    @staticmethod
    def _run_receipt_paths(directory: Path) -> list[tuple[Path, UUID]]:
        try:
            paths = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        json_paths: list[tuple[Path, UUID]] = []
        for path in paths:
            if path.suffix != ".json":
                continue
            try:
                run_id = _parse_uuid(path.stem, version=4)
            except NurtureStateError:
                raise NurtureStateError("NURTURE_STATE_INVALID") from None
            json_paths.append((path, run_id))
        if len(json_paths) > MAX_NURTURE_RUN_RECEIPTS:
            raise NurtureStateError("NURTURE_STATE_CAP_REACHED")
        return json_paths

    def lock_directory(self) -> Path:
        directory = self._subdirectory("locks", create=True)
        assert directory is not None
        return directory

    def validate_lock_file(self, path: Path, parent: Path) -> None:
        self._regular_file_path(path, parent, must_exist=True)

    def _run_directory(self, account_id: UUID, *, create: bool) -> Path | None:
        runs = self._subdirectory("runs", create=create)
        if runs is None:
            return None
        return self._ensure_directory(runs / str(account_id), runs, create=create)

    def _state_account_directory(self, account_id: UUID, *, create: bool) -> Path | None:
        state = self._subdirectory("state", create=create)
        if state is None:
            return None
        return self._ensure_directory(state / str(account_id), state, create=create)

    def _subdirectory(self, name: str, *, create: bool) -> Path | None:
        root = self._validate_root()
        nurture = self._ensure_directory(root / "nurture", root, create=create)
        if nurture is None:
            return None
        return self._ensure_directory(nurture / name, nurture, create=create)

    def _ensure_directory(self, path: Path, parent: Path, *, create: bool) -> Path | None:
        try:
            path.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError:
                raise NurtureStateError("NURTURE_STATE_INVALID") from None
        except OSError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError, RuntimeError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != path
            or path.parent != parent
            or not resolved.is_relative_to(self._root)
        ):
            raise NurtureStateError("NURTURE_STATE_INVALID")
        return path

    def _validate_root(self) -> Path:
        try:
            metadata = self._root.lstat()
            resolved = self._root.resolve(strict=True)
        except OSError, RuntimeError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        if (
            self._configured_root != self._root
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != self._root
        ):
            raise NurtureStateError("NURTURE_STATE_INVALID")
        return self._root

    def _run_path(self, directory: Path, run_id: UUID, *, must_exist: bool) -> Path | None:
        return self._regular_file_path(directory / f"{run_id}.json", directory, must_exist)

    def _target_state_path(
        self,
        directory: Path,
        preset_id: str,
        *,
        must_exist: bool,
    ) -> Path | None:
        if _PRESET_ID.fullmatch(preset_id) is None:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        return self._regular_file_path(directory / f"{preset_id}.json", directory, must_exist)

    def _regular_file_path(
        self,
        path: Path,
        parent: Path,
        must_exist: bool,
    ) -> Path | None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if must_exist:
                raise NurtureStateError("NURTURE_STATE_INVALID") from None
            return None
        except OSError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        try:
            resolved = path.resolve(strict=True)
        except OSError, RuntimeError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or resolved != path
            or path.parent != parent
            or not resolved.is_relative_to(self._root)
        ):
            raise NurtureStateError("NURTURE_STATE_INVALID")
        return path

    def _read_bytes(self, path: Path, parent: Path, maximum: int) -> bytes:
        self._regular_file_path(path, parent, must_exist=True)
        try:
            with path.open("rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                    raise ValueError
                raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError
            return raw
        except OSError, ValueError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None

    def _read_run(self, path: Path, run_id: UUID) -> NurtureRunV1:
        raw = self._read_bytes(path, path.parent, MAX_NURTURE_RUN_BYTES)
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        run = _decode_run(value)
        if run.id != run_id:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        return run

    def _read_target_document(
        self,
        path: Path,
        account_id: UUID,
        preset_id: str,
    ) -> NurtureTargetStateV1:
        raw = self._read_bytes(path, path.parent, MAX_NURTURE_TARGET_STATE_BYTES)
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except UnicodeError, json.JSONDecodeError, RecursionError, ValueError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        document = _decode_target_document(value)
        if document.account_id != account_id or document.preset_id != preset_id:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        return document

    def _write_new_run(self, target: Path, run: NurtureRunV1) -> None:
        self._write_new_document(target, _encode_run(run), MAX_NURTURE_RUN_BYTES)

    def _write_new_document(self, target: Path, document: str, maximum: int) -> None:
        if len(document.encode("utf-8")) > maximum:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            if self._regular_file_path(target, target.parent, must_exist=False) is not None:
                raise NurtureStateError("NURTURE_STATE_INVALID")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary_path = Path(temporary_name)
            stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = None
            with stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                raise NurtureStateError("NURTURE_STATE_INVALID") from None
        except NurtureStateError:
            raise
        except OSError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        finally:
            _cleanup_temporary(temporary_path, descriptor)

    def _write_replace(self, target: Path, document: str, maximum: int) -> None:
        if len(document.encode("utf-8")) > maximum:
            raise NurtureStateError("NURTURE_STATE_INVALID")
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
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            self._regular_file_path(target, target.parent, must_exist=True)
            os.replace(temporary_path, target)
            self._regular_file_path(target, target.parent, must_exist=True)
        except NurtureStateError:
            raise
        except OSError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        finally:
            _cleanup_temporary(temporary_path, descriptor)


class NurtureAccountLock:
    """Separate per-account Nurture owner; never aliases capability locks."""

    def __init__(self, store: NurtureStore, account_id: UUID) -> None:
        _validate_uuid(account_id, version=4)
        self._store = store
        self._account_id = account_id
        self._lock: FilesystemProcessLock | None = None

    @property
    def account_id(self) -> UUID:
        return self._account_id

    @property
    def held(self) -> bool:
        return self._lock is not None and self._lock.held

    def acquire(self) -> NurtureAccountLock:
        if self.held:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        _validate_uuid(self.account_id, version=4)
        directory = self._store.lock_directory()
        path = directory / f"{self.account_id}.lock"
        self._prepare_lock_file(path, directory)
        lock = FilesystemProcessLock(path)
        try:
            lock.acquire()
        except ProcessAlreadyRunning:
            raise NurtureStateError("NURTURE_BUSY") from None
        except OSError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        try:
            self._store.validate_lock_file(path, directory)
            self._lock = lock
            self._store.repair_stale_runs(self)
        except BaseException:
            self._lock = None
            _release_lock(lock)
            raise
        return self

    def release(self) -> None:
        lock = self._lock
        if lock is None:
            return
        self._lock = None
        _release_lock(lock)

    def __enter__(self) -> NurtureAccountLock:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()

    async def __aenter__(self) -> NurtureAccountLock:
        return self.acquire()

    async def __aexit__(self, *_: object) -> None:
        self.release()

    def start_run(
        self,
        preset: NurturePresetV1,
        *,
        now: datetime | None = None,
    ) -> NurtureRunScope:
        self.require_held()
        return NurtureRunScope(self, self._store.create_run(self, preset, now=now))

    def get_run(self, run_id: UUID) -> NurtureRunV1:
        self.require_held()
        return self._store.read_run(self, run_id)

    def update_run(self, updated: NurtureRunV1) -> NurtureRunV1:
        return self._store.update_run(self, updated)

    def observe_target(
        self,
        preset: NurturePresetV1,
        fingerprint: str,
        run_id: UUID,
        decision_code: str,
        *,
        now: datetime | None = None,
    ) -> NurtureTargetV1:
        self.require_held()
        validated_preset = _validated_preset(preset)
        _validate_run_link(self, validated_preset, run_id)
        _validate_fingerprint(fingerprint)
        _validate_code(decision_code)
        observed_at = _as_utc(datetime.now(UTC) if now is None else now)
        document = self._store.load_target_state(self, validated_preset)
        current = _find_target(document.targets, fingerprint)
        if current is None:
            target = NurtureTargetV1(
                fingerprint,
                observed_at,
                observed_at,
                decision_code,
                None,
                "NONE",
                run_id,
                None,
            )
        else:
            if current.action_state in {"PENDING", "AMBIGUOUS"}:
                target = replace(current, last_seen_at=max(current.last_seen_at, observed_at))
            else:
                target = replace(
                    current,
                    last_seen_at=max(current.last_seen_at, observed_at),
                    last_decision_code=decision_code,
                    last_run_id=run_id,
                )
        self._store.save_target_state(
            self,
            _replace_target(document, target, protect=fingerprint),
        )
        return target

    def reserve_target(
        self,
        preset: NurturePresetV1,
        fingerprint: str,
        run_id: UUID,
        decision_code: str,
        *,
        now: datetime | None = None,
    ) -> NurtureTargetV1:
        self.require_held()
        validated_preset = _validated_preset(preset)
        _validate_run_link(self, validated_preset, run_id)
        _validate_fingerprint(fingerprint)
        _validate_code(decision_code)
        action_at = _as_utc(datetime.now(UTC) if now is None else now)
        document = self._store.load_target_state(self, validated_preset)
        current = _find_target(document.targets, fingerprint)
        if current is not None:
            if current.action_state == "PENDING":
                raise NurtureStateError("NURTURE_TARGET_PENDING")
            if current.action_state == "AMBIGUOUS":
                raise NurtureStateError("NURTURE_TARGET_AMBIGUOUS")
            if current.action_state == "CONFIRMED":
                raise NurtureStateError("NURTURE_TARGET_RESERVED")
            target = replace(
                current,
                last_seen_at=max(current.last_seen_at, action_at),
                last_decision_code=decision_code,
                last_action_at=max(current.first_seen_at, action_at),
                action_state="PENDING",
                last_run_id=run_id,
            )
        else:
            target = NurtureTargetV1(
                fingerprint,
                action_at,
                action_at,
                decision_code,
                action_at,
                "PENDING",
                run_id,
                None,
            )
        self._store.save_target_state(
            self,
            _replace_target(document, target, protect=fingerprint),
        )
        return target

    def complete_target_action(
        self,
        preset: NurturePresetV1,
        fingerprint: str,
        run_id: UUID,
        action_state: Literal["CONFIRMED", "AMBIGUOUS"],
        operation_id: UUID | None,
        *,
        now: datetime | None = None,
    ) -> NurtureTargetV1:
        self.require_held()
        validated_preset = _validated_preset(preset)
        _validate_run_link(self, validated_preset, run_id)
        _validate_fingerprint(fingerprint)
        if type(action_state) is not str or action_state not in {"CONFIRMED", "AMBIGUOUS"}:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        if operation_id is not None:
            _validate_uuid(operation_id, version=4)
        if action_state == "CONFIRMED" and operation_id is None:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        action_at = _as_utc(datetime.now(UTC) if now is None else now)
        document = self._store.load_target_state(self, validated_preset)
        current = _find_target(document.targets, fingerprint)
        if current is None or current.action_state != "PENDING" or current.last_run_id != run_id:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        target = replace(
            current,
            last_action_at=max(current.first_seen_at, action_at),
            action_state=action_state,
            last_run_id=run_id,
            last_operation_id=operation_id,
        )
        self._store.save_target_state(self, _replace_target(document, target))
        return target

    def get_targets(self, preset: NurturePresetV1) -> tuple[NurtureTargetV1, ...]:
        self.require_held()
        validated_preset = _validated_preset(preset)
        return self._store.load_target_state(self, validated_preset).targets

    def _prepare_lock_file(self, path: Path, parent: Path) -> None:
        try:
            path.lstat()
        except FileNotFoundError:
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            except OSError:
                raise NurtureStateError("NURTURE_STATE_INVALID") from None
            else:
                try:
                    os.close(descriptor)
                except OSError:
                    raise NurtureStateError("NURTURE_STATE_INVALID") from None
        except OSError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        self._store.validate_lock_file(path, parent)

    def require_held(self, store: NurtureStore | None = None) -> None:
        _validate_uuid(self.account_id, version=4)
        if not self.held or (store is not None and self._store is not store):
            raise NurtureStateError("NURTURE_STATE_INVALID")


class NurtureRunScope:
    """A receipt boundary that persists outcomes and propagates interruptions."""

    def __init__(self, owner: NurtureAccountLock, run: NurtureRunV1) -> None:
        self._owner = owner
        self._run = run

    @property
    def receipt(self) -> NurtureRunV1:
        return self._run

    def update(self, **changes: object) -> NurtureRunV1:
        if self._run.outcome != "RUNNING":
            raise NurtureStateError("NURTURE_STATE_INVALID")
        try:
            updated = replace(self._run, **changes)
        except TypeError, ValueError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        self._run = self._owner.update_run(updated)
        return self._run

    def add_decision(self, code: str) -> NurtureRunV1:
        _validate_code(code)
        if len(self._run.decision_codes) >= _MAX_DECISION_CODES:
            raise NurtureStateError("NURTURE_STATE_CAP_REACHED")
        return self.update(decision_codes=(*self._run.decision_codes, code))

    def link_operation(self, operation_id: UUID) -> NurtureRunV1:
        _validate_uuid(operation_id, version=4)
        if operation_id in self._run.operation_ids:
            return self._run
        if len(self._run.operation_ids) >= _MAX_OPERATION_IDS:
            raise NurtureStateError("NURTURE_STATE_CAP_REACHED")
        return self.update(operation_ids=(*self._run.operation_ids, operation_id))

    def finish(
        self,
        outcome: Literal["SUCCESS", "FAILED", "INTERRUPTED", "AMBIGUOUS"],
        *,
        now: datetime | None = None,
        error_code: str | None = None,
        failed_stage: str | None = None,
    ) -> NurtureRunV1:
        if type(outcome) is not str or outcome not in {
            "SUCCESS",
            "FAILED",
            "INTERRUPTED",
            "AMBIGUOUS",
        }:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        finished_at = max(
            self._run.started_at,
            _as_utc(datetime.now(UTC) if now is None else now),
        )
        self._run = self._owner.update_run(
            replace(
                self._run,
                finished_at=finished_at,
                outcome=outcome,
                error_code=error_code,
                failed_stage=failed_stage,
            )
        )
        return self._run

    def __enter__(self) -> NurtureRunScope:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        self._close_for_exit(exc_type)
        return False

    async def __aenter__(self) -> NurtureRunScope:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        self._close_for_exit(exc_type)
        return False

    def _close_for_exit(self, exc_type: type[BaseException] | None) -> None:
        if self._run.outcome != "RUNNING":
            return
        if exc_type is None:
            self.finish("SUCCESS")
        elif issubclass(exc_type, (KeyboardInterrupt, asyncio.CancelledError)):
            self.finish("INTERRUPTED", error_code="INTERRUPTED", failed_stage="execution")
        elif issubclass(exc_type, Exception):
            self.finish("FAILED", error_code="RUN_FAILED", failed_stage="execution")


def fingerprint_remote_thread(remote_thread_id: str) -> str:
    """Return a stable SHA-256 fingerprint without persisting the remote identity."""

    if type(remote_thread_id) is not str:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    normalized = unicodedata.normalize("NFC", remote_thread_id.strip())
    if _REMOTE_THREAD_ID.fullmatch(normalized) is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    identity = json.dumps(
        {"platform": "threads", "thread_id": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _validate_run(run: NurtureRunV1) -> None:
    if type(run) is not NurtureRunV1:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    if (
        type(run.version) is not int
        or run.version != 1
        or type(run.id) is not UUID
        or run.id.version != 4
        or type(run.account_id) is not UUID
        or run.account_id.version != 4
        or type(run.preset_id) is not str
        or _PRESET_ID.fullmatch(run.preset_id) is None
        or type(run.preset_version) is not int
        or run.preset_version != 1
        or type(run.outcome) is not str
        or run.outcome not in _OUTCOMES
        or type(run.decision_codes) is not tuple
        or len(run.decision_codes) > _MAX_DECISION_CODES
        or type(run.operation_ids) is not tuple
        or len(run.operation_ids) > _MAX_OPERATION_IDS
    ):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    _validate_datetime(run.started_at)
    if run.finished_at is not None:
        _validate_datetime(run.finished_at)
        if _as_utc(run.finished_at) < _as_utc(run.started_at):
            raise NurtureStateError("NURTURE_STATE_INVALID")
    for field_name in _COUNTER_FIELDS:
        count = getattr(run, field_name)
        if type(count) is not int or not 0 <= count <= _MAX_COUNTER:
            raise NurtureStateError("NURTURE_STATE_INVALID")
    for code in run.decision_codes:
        _validate_code(code)
    for operation_id in run.operation_ids:
        _validate_uuid(operation_id, version=4)
    if len(set(run.operation_ids)) != len(run.operation_ids):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    if run.error_code is not None:
        _validate_code(run.error_code)
    if run.failed_stage is not None and (
        type(run.failed_stage) is not str or _SAFE_STAGE.fullmatch(run.failed_stage) is None
    ):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    if run.outcome == "RUNNING":
        if (
            run.finished_at is not None
            or run.error_code is not None
            or run.failed_stage is not None
        ):
            raise NurtureStateError("NURTURE_STATE_INVALID")
    else:
        if run.finished_at is None:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        if run.outcome == "SUCCESS" and (
            run.error_code is not None or run.failed_stage is not None
        ):
            raise NurtureStateError("NURTURE_STATE_INVALID")
        if run.outcome in {"FAILED", "INTERRUPTED", "AMBIGUOUS"} and (
            run.error_code is None or run.failed_stage is None
        ):
            raise NurtureStateError("NURTURE_STATE_INVALID")
        if run.outcome == "INTERRUPTED" and run.error_code != "INTERRUPTED":
            raise NurtureStateError("NURTURE_STATE_INVALID")


def _validate_target(target: NurtureTargetV1) -> None:
    if type(target) is not NurtureTargetV1:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    _validate_fingerprint(target.fingerprint)
    _validate_datetime(target.first_seen_at)
    _validate_datetime(target.last_seen_at)
    if _as_utc(target.last_seen_at) < _as_utc(target.first_seen_at):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    _validate_code(target.last_decision_code)
    if target.last_action_at is not None:
        _validate_datetime(target.last_action_at)
    if (
        type(target.action_state) is not str
        or target.action_state not in _ACTION_STATES
        or type(target.last_run_id) is not UUID
        or target.last_run_id.version != 4
    ):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    if target.last_operation_id is not None:
        _validate_uuid(target.last_operation_id, version=4)
    if target.action_state == "NONE":
        if target.last_action_at is not None or target.last_operation_id is not None:
            raise NurtureStateError("NURTURE_STATE_INVALID")
    elif target.last_action_at is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    if target.action_state == "PENDING" and target.last_operation_id is not None:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    if target.action_state == "CONFIRMED" and target.last_operation_id is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")


def _encode_run(run: NurtureRunV1) -> str:
    _validate_run(run)
    document: dict[str, object] = {
        "version": run.version,
        "id": str(run.id),
        "account_id": str(run.account_id),
        "preset_id": run.preset_id,
        "preset_version": run.preset_version,
        "started_at": _timestamp_text(run.started_at),
        "finished_at": None if run.finished_at is None else _timestamp_text(run.finished_at),
        "outcome": run.outcome,
        **{name: getattr(run, name) for name in _COUNTER_FIELDS},
        "decision_codes": list(run.decision_codes),
        "operation_ids": [str(operation_id) for operation_id in run.operation_ids],
        "error_code": run.error_code,
        "failed_stage": run.failed_stage,
    }
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _decode_run(value: object) -> NurtureRunV1:
    if type(value) is not dict:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    document = cast(dict[str, object], value)
    if frozenset(document) != _RUN_KEYS:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    version = document["version"]
    if type(version) is int and version != 1:
        raise NurtureStateError("NURTURE_VERSION_UNSUPPORTED")
    if type(version) is not int:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    raw_decisions = document["decision_codes"]
    raw_operations = document["operation_ids"]
    if type(raw_decisions) is not list or type(raw_operations) is not list:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    decisions = cast(list[object], raw_decisions)
    operations = cast(list[object], raw_operations)
    if any(type(code) is not str for code in decisions):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    operation_ids = tuple(_parse_uuid(raw, version=4) for raw in operations)
    outcome = document["outcome"]
    if type(outcome) is not str or outcome not in _OUTCOMES:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    return NurtureRunV1(
        version=version,
        id=_parse_uuid(document["id"], version=4),
        account_id=_parse_uuid(document["account_id"], version=4),
        preset_id=_required_string(document["preset_id"]),
        preset_version=_required_int(document["preset_version"]),
        started_at=_parse_timestamp(document["started_at"]),
        finished_at=_nullable_timestamp(document["finished_at"]),
        outcome=cast(NurtureOutcome, outcome),
        **{name: _required_int(document[name]) for name in _COUNTER_FIELDS},
        decision_codes=tuple(cast(list[str], decisions)),
        operation_ids=operation_ids,
        error_code=_nullable_code(document["error_code"]),
        failed_stage=_nullable_stage(document["failed_stage"]),
    )


def _encode_target_document(document: NurtureTargetStateV1) -> str:
    _validate_target_document(document)
    value = {
        "version": document.version,
        "account_id": str(document.account_id),
        "preset_id": document.preset_id,
        "targets": [
            {
                "fingerprint": target.fingerprint,
                "first_seen_at": _timestamp_text(target.first_seen_at),
                "last_seen_at": _timestamp_text(target.last_seen_at),
                "last_decision_code": target.last_decision_code,
                "last_action_at": (
                    None
                    if target.last_action_at is None
                    else _timestamp_text(target.last_action_at)
                ),
                "action_state": target.action_state,
                "last_run_id": str(target.last_run_id),
                "last_operation_id": (
                    None if target.last_operation_id is None else str(target.last_operation_id)
                ),
            }
            for target in sorted(document.targets, key=lambda item: item.fingerprint)
        ],
    }
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _decode_target_document(value: object) -> NurtureTargetStateV1:
    if type(value) is not dict:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    document = cast(dict[str, object], value)
    if frozenset(document) != _TARGET_STATE_KEYS:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    version = document["version"]
    if type(version) is int and version != 1:
        raise NurtureStateError("NURTURE_VERSION_UNSUPPORTED")
    if type(version) is not int:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    raw_targets = document["targets"]
    if type(raw_targets) is not list:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    targets: list[NurtureTargetV1] = []
    for raw_target in cast(list[object], raw_targets):
        if type(raw_target) is not dict:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        target_data = cast(dict[str, object], raw_target)
        if frozenset(target_data) != _TARGET_KEYS:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        state = target_data["action_state"]
        if type(state) is not str or state not in _ACTION_STATES:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        targets.append(
            NurtureTargetV1(
                fingerprint=_required_string(target_data["fingerprint"]),
                first_seen_at=_parse_timestamp(target_data["first_seen_at"]),
                last_seen_at=_parse_timestamp(target_data["last_seen_at"]),
                last_decision_code=_required_code(target_data["last_decision_code"]),
                last_action_at=_nullable_timestamp(target_data["last_action_at"]),
                action_state=cast(NurtureActionState, state),
                last_run_id=_parse_uuid(target_data["last_run_id"], version=4),
                last_operation_id=_nullable_uuid(target_data["last_operation_id"], version=4),
            )
        )
    result = NurtureTargetStateV1(
        version,
        _parse_uuid(document["account_id"], version=4),
        _required_string(document["preset_id"]),
        tuple(targets),
    )
    _validate_target_document(result)
    return result


def _validate_target_document(document: NurtureTargetStateV1) -> None:
    if type(document) is not NurtureTargetStateV1:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    if (
        type(document.version) is not int
        or document.version != 1
        or type(document.account_id) is not UUID
        or document.account_id.version != 4
        or type(document.preset_id) is not str
        or _PRESET_ID.fullmatch(document.preset_id) is None
        or type(document.targets) is not tuple
        or len(document.targets) > MAX_NURTURE_TARGETS
    ):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    fingerprints: set[str] = set()
    for target in document.targets:
        _validate_target(target)
        if target.fingerprint in fingerprints:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        fingerprints.add(target.fingerprint)


def _validate_target_transition(
    current: NurtureTargetStateV1,
    updated: NurtureTargetStateV1,
) -> None:
    if (
        current.account_id != updated.account_id
        or current.preset_id != updated.preset_id
        or updated.version != 1
    ):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    previous = {target.fingerprint: target for target in current.targets}
    next_targets = {target.fingerprint: target for target in updated.targets}
    for fingerprint, old in previous.items():
        new = next_targets.get(fingerprint)
        if old.action_state in {"PENDING", "AMBIGUOUS"}:
            if new is None:
                raise NurtureStateError("NURTURE_STATE_INVALID")
            if old.action_state == "AMBIGUOUS":
                if (
                    new.action_state != "AMBIGUOUS"
                    or new.first_seen_at != old.first_seen_at
                    or new.last_decision_code != old.last_decision_code
                    or new.last_action_at != old.last_action_at
                    or new.last_run_id != old.last_run_id
                    or new.last_operation_id != old.last_operation_id
                ):
                    raise NurtureStateError("NURTURE_STATE_INVALID")
            elif new.action_state == "PENDING":
                if (
                    new.first_seen_at != old.first_seen_at
                    or new.last_decision_code != old.last_decision_code
                    or new.last_action_at != old.last_action_at
                    or new.last_run_id != old.last_run_id
                    or new.last_operation_id != old.last_operation_id
                ):
                    raise NurtureStateError("NURTURE_STATE_INVALID")
            elif new.action_state in {"CONFIRMED", "AMBIGUOUS"}:
                if (
                    new.first_seen_at != old.first_seen_at
                    or new.last_decision_code != old.last_decision_code
                    or new.last_run_id != old.last_run_id
                ):
                    raise NurtureStateError("NURTURE_STATE_INVALID")
            else:
                raise NurtureStateError("NURTURE_STATE_INVALID")
        elif old.action_state == "NONE":
            if new is not None and new.action_state not in {"NONE", "PENDING"}:
                raise NurtureStateError("NURTURE_STATE_INVALID")
            if new is not None and new.first_seen_at != old.first_seen_at:
                raise NurtureStateError("NURTURE_STATE_INVALID")
        elif new is not None:
            if (
                new.action_state != "CONFIRMED"
                or new.first_seen_at != old.first_seen_at
                or new.last_action_at != old.last_action_at
                or new.last_operation_id != old.last_operation_id
            ):
                raise NurtureStateError("NURTURE_STATE_INVALID")

    for fingerprint, new in next_targets.items():
        if fingerprint not in previous and new.action_state not in {"NONE", "PENDING"}:
            raise NurtureStateError("NURTURE_STATE_INVALID")


def _replace_target(
    document: NurtureTargetStateV1,
    target: NurtureTargetV1,
    *,
    protect: str | None = None,
) -> NurtureTargetStateV1:
    targets = {item.fingerprint: item for item in document.targets}
    targets[target.fingerprint] = target
    if len(targets) > MAX_NURTURE_TARGETS:
        removable = sorted(
            (
                item
                for item in targets.values()
                if item.action_state in {"NONE", "CONFIRMED"} and item.fingerprint != protect
            ),
            key=lambda item: (_as_utc(item.last_seen_at), item.fingerprint),
        )
        excess = len(targets) - MAX_NURTURE_TARGETS
        if len(removable) < excess:
            raise NurtureStateError("NURTURE_STATE_CAP_REACHED")
        for item in removable[:excess]:
            del targets[item.fingerprint]
    ordered = tuple(sorted(targets.values(), key=lambda item: item.fingerprint))
    return NurtureTargetStateV1(document.version, document.account_id, document.preset_id, ordered)


def _find_target(targets: tuple[NurtureTargetV1, ...], fingerprint: str) -> NurtureTargetV1 | None:
    for target in targets:
        if target.fingerprint == fingerprint:
            return target
    return None


def _validate_run_link(
    owner: NurtureAccountLock,
    preset: NurturePresetV1,
    run_id: UUID,
) -> None:
    run = owner.get_run(run_id)
    if run.preset_id != preset.id or run.outcome != "RUNNING":
        raise NurtureStateError("NURTURE_STATE_INVALID")


def _validated_preset(preset: NurturePresetV1) -> NurturePresetV1:
    if type(preset) is not NurturePresetV1:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    try:
        return replace(preset)
    except AttributeError, TypeError, ValueError:
        raise NurtureStateError("NURTURE_STATE_INVALID") from None


def _parse_uuid(value: object, *, version: int) -> UUID:
    if type(value) is not str:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    try:
        result = UUID(value)
    except ValueError, AttributeError:
        raise NurtureStateError("NURTURE_STATE_INVALID") from None
    if str(result) != value or result.version != version:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    return result


def _validate_uuid(value: object, *, version: int) -> None:
    if type(value) is not UUID or value.version != version:
        raise NurtureStateError("NURTURE_STATE_INVALID")


def _required_string(value: object) -> str:
    if type(value) is not str:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    return value


def _required_int(value: object) -> int:
    if type(value) is not int:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    return value


def _required_code(value: object) -> str:
    code = _required_string(value)
    _validate_code(code)
    return code


def _nullable_code(value: object) -> str | None:
    return None if value is None else _required_code(value)


def _nullable_stage(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _SAFE_STAGE.fullmatch(value) is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    return value


def _nullable_uuid(value: object, *, version: int) -> UUID | None:
    return None if value is None else _parse_uuid(value, version=version)


def _nullable_timestamp(value: object) -> datetime | None:
    return None if value is None else _parse_timestamp(value)


def _validate_code(value: object) -> None:
    if type(value) is not str or _SAFE_CODE.fullmatch(value) is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")


def _validate_fingerprint(value: object) -> None:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")


def _validate_datetime(value: object) -> None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")


def _as_utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    return value.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise NurtureStateError("NURTURE_STATE_INVALID") from None
    if _timestamp_text(result) != value:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-JSON constant")


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


def _release_lock(lock: FilesystemProcessLock) -> None:
    try:
        lock.release()
    except OSError:
        pass
