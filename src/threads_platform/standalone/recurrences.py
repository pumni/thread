"""Local durable state and foreground runner for READ-only workflows."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from threads_platform.application.ports.process_lock import ProcessAlreadyRunning
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.workflows import (
    MAX_WORKFLOW_BYTES,
    StandaloneWorkflowError,
    WorkflowPlan,
    is_read_only_workflow,
    load_workflow,
)

_VERSION = 1
_MAX_RECORD_BYTES = 8_192
_INTERVAL_MIN_SECONDS = 300
_INTERVAL_MAX_SECONDS = 2_592_000
_WORKFLOW_FILE = "workflow.json"
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_RECORD_KEYS = frozenset(
    {
        "version",
        "id",
        "workflow_file",
        "account",
        "interval_seconds",
        "status",
        "created_at",
        "updated_at",
        "anchor_at",
        "next_due_at",
        "last_started_at",
        "last_finished_at",
        "last_outcome",
        "last_error_code",
        "last_step_index",
    }
)
_OUTCOMES = frozenset({"RUNNING", "SUCCESS", "FAILED", "SKIPPED", "INTERRUPTED"})


class StandaloneRecurrenceError(Exception):
    """A bounded recurrence state or runner error."""

    code: str

    def __init__(self, code: str) -> None:
        if _SAFE_ERROR_CODE.fullmatch(code) is None:
            raise ValueError("recurrence error code must be bounded and safe")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RecurrenceRecord:
    version: int
    id: str
    workflow_file: str
    account: str
    interval_seconds: int
    status: Literal["ENABLED", "DISABLED"]
    created_at: datetime
    updated_at: datetime
    anchor_at: datetime
    next_due_at: datetime
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_outcome: Literal["RUNNING", "SUCCESS", "FAILED", "SKIPPED", "INTERRUPTED"] | None
    last_error_code: str | None
    last_step_index: int | None


class RecurrenceTiming(Protocol):
    def utc_now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class _SystemRecurrenceTiming:
    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class LocalRecurrenceStore:
    """Strict local filesystem store for recurrence records and snapshots."""

    def __init__(self, data_root: Path, accounts: LocalAccountStore) -> None:
        self._configured_root = Path(data_root).absolute()
        self._accounts = accounts
        try:
            self._root = self._configured_root.resolve(strict=False)
        except OSError, RuntimeError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None

    def create(
        self,
        workflow_path: Path,
        interval_seconds: int,
        *,
        now: datetime | None = None,
    ) -> RecurrenceRecord:
        _validate_interval(interval_seconds)
        workflow_bytes = _read_source_workflow(workflow_path)
        recurrences = self._recurrences_directory(create=True)
        assert recurrences is not None
        recurrence_id = str(uuid4())
        stage = recurrences / f".staging-{uuid4()}"
        final_directory = recurrences / recurrence_id
        try:
            stage.mkdir(mode=0o700)
            self._validate_directory(stage, recurrences)
            snapshot_path = stage / _WORKFLOW_FILE
            self._write_new_file(snapshot_path, workflow_bytes)
            try:
                plan = load_workflow(snapshot_path)
            except StandaloneWorkflowError:
                raise
            if not is_read_only_workflow(plan):
                raise StandaloneRecurrenceError("RECURRENCE_WORKFLOW_NOT_READ_ONLY")
            self._accounts.get(plan.account)

            now = _utc_now(datetime.now(UTC) if now is None else now)
            record = RecurrenceRecord(
                version=_VERSION,
                id=recurrence_id,
                workflow_file=_WORKFLOW_FILE,
                account=plan.account,
                interval_seconds=interval_seconds,
                status="ENABLED",
                created_at=now,
                updated_at=now,
                anchor_at=now,
                next_due_at=now + timedelta(seconds=interval_seconds),
                last_started_at=None,
                last_finished_at=None,
                last_outcome=None,
                last_error_code=None,
                last_step_index=None,
            )
            self._write_replace(stage / "record.json", record)
            self._validate_absent_directory(final_directory, recurrences)
            os.rename(stage, final_directory)
            return record
        except StandaloneWorkflowError:
            raise
        except StandaloneRecurrenceError:
            raise
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        finally:
            self._remove_staging_directory(stage, recurrences)

    def list(self) -> tuple[RecurrenceRecord, ...]:
        recurrences = self._recurrences_directory(create=False)
        if recurrences is None:
            return ()
        records: list[RecurrenceRecord] = []
        try:
            entries = sorted(recurrences.iterdir(), key=lambda item: item.name)
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        for entry in entries:
            try:
                metadata = entry.lstat()
            except OSError:
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
            if entry.name.startswith(".staging-"):
                self._validate_directory(entry, recurrences)
                continue
            try:
                recurrence_id = _parse_recurrence_id(entry.name)
            except StandaloneRecurrenceError:
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
            self._validate_directory(entry, recurrences)
            records.append(self._read_record(recurrence_id))
        return tuple(records)

    def get(self, recurrence_id: str) -> RecurrenceRecord:
        canonical_id = _parse_recurrence_id(recurrence_id)
        directory = self._recurrence_directory(canonical_id, must_exist=True)
        assert directory is not None
        record_path = directory / "record.json"
        self._validate_regular_file(record_path, directory)
        try:
            with record_path.open("rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError
                raw = stream.read(_MAX_RECORD_BYTES + 1)
            if len(raw) > _MAX_RECORD_BYTES:
                raise ValueError
            data: object = json.loads(raw, object_pairs_hook=_unique_object)
        except OSError, ValueError, UnicodeError, json.JSONDecodeError, RecursionError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        return _decode_record(data, canonical_id)

    def load_workflow(self, record: RecurrenceRecord) -> WorkflowPlan:
        directory = self._recurrence_directory(record.id, must_exist=True)
        assert directory is not None
        snapshot = directory / _WORKFLOW_FILE
        self._validate_regular_file(snapshot, directory)
        try:
            plan = load_workflow(snapshot)
        except StandaloneWorkflowError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        if plan.account != record.account:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
        if not is_read_only_workflow(plan):
            raise StandaloneRecurrenceError("RECURRENCE_WORKFLOW_NOT_READ_ONLY")
        return plan

    def update(self, record: RecurrenceRecord) -> RecurrenceRecord:
        _validate_record(record, record.id)
        current = self.get(record.id)
        if (
            current.account != record.account
            or current.created_at != record.created_at
            or current.anchor_at != record.anchor_at
            or current.interval_seconds != record.interval_seconds
            or current.workflow_file != record.workflow_file
            or (current.status == "DISABLED" and record.status != "DISABLED")
            or (current.status != record.status and record.status != "DISABLED")
        ):
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
        directory = self._recurrence_directory(record.id, must_exist=True)
        assert directory is not None
        self._validate_regular_file(directory / "record.json", directory)
        self._write_replace(directory / "record.json", record)
        return record

    def disable(self, recurrence_id: str) -> RecurrenceRecord:
        lock = self.acquire_runner_lock(recurrence_id)
        try:
            current = self.get(recurrence_id)
            if current.status == "DISABLED":
                return current
            now = _utc_now(datetime.now(UTC))
            disabled = replace(
                current,
                status="DISABLED",
                updated_at=max(current.updated_at, now),
            )
            return self.update(disabled)
        finally:
            _release_lock(lock)

    def acquire_runner_lock(self, recurrence_id: str) -> FilesystemProcessLock:
        canonical_id = _parse_recurrence_id(recurrence_id)
        directory = self._recurrence_directory(canonical_id, must_exist=True)
        assert directory is not None
        lock_path = directory / "runner.lock"
        self._validate_optional_regular_file(lock_path, directory)
        lock = FilesystemProcessLock(lock_path)
        try:
            lock.acquire()
        except ProcessAlreadyRunning:
            raise StandaloneRecurrenceError("RECURRENCE_BUSY") from None
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        try:
            self._validate_regular_file(lock_path, directory)
        except StandaloneRecurrenceError:
            _release_lock(lock)
            raise
        return lock

    def _read_record(self, recurrence_id: str) -> RecurrenceRecord:
        return self.get(recurrence_id)

    def _recurrences_directory(self, *, create: bool) -> Path | None:
        try:
            root_metadata = self._root.lstat()
        except FileNotFoundError:
            if not create:
                return None
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        if (
            self._configured_root != self._root
            or stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
        ):
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
        try:
            resolved_root = self._root.resolve(strict=True)
        except OSError, RuntimeError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        if resolved_root != self._root:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")

        directory = self._root / "recurrences"
        try:
            directory.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError:
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        self._validate_directory(directory, self._root)
        return directory

    def _recurrence_directory(
        self,
        recurrence_id: str,
        *,
        must_exist: bool,
    ) -> Path | None:
        recurrences = self._recurrences_directory(create=False)
        if recurrences is None:
            if must_exist:
                raise StandaloneRecurrenceError("RECURRENCE_NOT_FOUND")
            return None
        directory = recurrences / recurrence_id
        try:
            directory.lstat()
        except FileNotFoundError:
            if must_exist:
                raise StandaloneRecurrenceError("RECURRENCE_NOT_FOUND") from None
            return None
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        self._validate_directory(directory, recurrences)
        return directory

    def _validate_directory(self, path: Path, parent: Path) -> None:
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError, RuntimeError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != path
            or path.parent != parent
            or not resolved.is_relative_to(self._root)
        ):
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")

    def _validate_absent_directory(self, path: Path, parent: Path) -> None:
        self._validate_directory(parent, self._root)
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")

    def _validate_regular_file(self, path: Path, parent: Path) -> None:
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError, RuntimeError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or resolved != path
            or path.parent != parent
            or not resolved.is_relative_to(self._root)
        ):
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")

    def _validate_optional_regular_file(self, path: Path, parent: Path) -> None:
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        self._validate_regular_file(path, parent)

    def _write_new_file(self, target: Path, contents: bytes) -> None:
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary_path = Path(temporary_name)
            stream = os.fdopen(descriptor, "wb")
            descriptor = None
            with stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        except StandaloneRecurrenceError:
            raise
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        finally:
            _cleanup_temporary(temporary_path, descriptor)

    def _write_replace(self, target: Path, record: RecurrenceRecord) -> None:
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
                stream.write(_encode_record(record))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
        except OSError:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
        finally:
            _cleanup_temporary(temporary_path, descriptor)

    def _remove_staging_directory(self, stage: Path, recurrences: Path) -> None:
        try:
            if stage.exists() and stage.parent == recurrences:
                self._validate_directory(stage, recurrences)
                shutil.rmtree(stage)
        except OSError, StandaloneRecurrenceError:
            return


class LocalRecurrenceRunner:
    """Run each anchored workflow occurrence once in the foreground."""

    def __init__(
        self,
        store: LocalRecurrenceStore,
        timing: RecurrenceTiming | None = None,
    ) -> None:
        self._store = store
        self._timing = timing if timing is not None else _SystemRecurrenceTiming()

    async def run(
        self,
        recurrence_id: str,
        execute_once: Callable[[WorkflowPlan], Awaitable[object]],
    ) -> None:
        lock = self._store.acquire_runner_lock(recurrence_id)
        try:
            record = self._store.get(recurrence_id)
            if record.status != "ENABLED":
                raise StandaloneRecurrenceError("RECURRENCE_DISABLED")
            plan = self._store.load_workflow(record)
            now = _utc_now(self._timing.utc_now())
            repaired = record
            if record.last_outcome == "RUNNING":
                finished = max(now, record.last_started_at or now)
                repaired = replace(
                    repaired,
                    updated_at=max(repaired.updated_at, finished),
                    last_finished_at=finished,
                    last_outcome="INTERRUPTED",
                    last_error_code="INTERRUPTED",
                    last_step_index=None,
                )
            if repaired.next_due_at <= now:
                repaired = replace(
                    repaired,
                    next_due_at=_next_anchored_boundary(
                        repaired.anchor_at, now, repaired.interval_seconds
                    ),
                    updated_at=max(repaired.updated_at, now),
                )
            if repaired != record:
                record = self._store.update(repaired)

            while True:
                await self._wait_until(record.next_due_at)
                now = _utc_now(self._timing.utc_now())
                if now < record.next_due_at:
                    continue
                next_due = _next_anchored_boundary(record.anchor_at, now, record.interval_seconds)
                started = replace(
                    record,
                    updated_at=max(record.updated_at, now),
                    next_due_at=next_due,
                    last_started_at=now,
                    last_finished_at=None,
                    last_outcome="RUNNING",
                    last_error_code=None,
                    last_step_index=None,
                )
                record = self._store.update(started)
                try:
                    await execute_once(plan)
                except StandaloneWorkflowError as error:
                    finished = max(_utc_now(self._timing.utc_now()), record.last_started_at or now)
                    outcome: Literal["FAILED", "SKIPPED"] = (
                        "SKIPPED" if error.code == "ACCOUNT_BUSY" else "FAILED"
                    )
                    record = self._store.update(
                        replace(
                            record,
                            updated_at=max(record.updated_at, finished),
                            next_due_at=_next_anchored_boundary(
                                record.anchor_at, finished, record.interval_seconds
                            ),
                            last_finished_at=finished,
                            last_outcome=outcome,
                            last_error_code=error.code,
                            last_step_index=error.step_index,
                        )
                    )
                except KeyboardInterrupt, asyncio.CancelledError:
                    finished = max(_utc_now(self._timing.utc_now()), record.last_started_at or now)
                    record = self._store.update(
                        replace(
                            record,
                            updated_at=max(record.updated_at, finished),
                            next_due_at=_next_anchored_boundary(
                                record.anchor_at, finished, record.interval_seconds
                            ),
                            last_finished_at=finished,
                            last_outcome="INTERRUPTED",
                            last_error_code="INTERRUPTED",
                            last_step_index=None,
                        )
                    )
                    raise
                else:
                    finished = max(_utc_now(self._timing.utc_now()), record.last_started_at or now)
                    record = self._store.update(
                        replace(
                            record,
                            updated_at=max(record.updated_at, finished),
                            next_due_at=_next_anchored_boundary(
                                record.anchor_at, finished, record.interval_seconds
                            ),
                            last_finished_at=finished,
                            last_outcome="SUCCESS",
                            last_error_code=None,
                            last_step_index=None,
                        )
                    )
        finally:
            _release_lock(lock)

    async def _wait_until(self, due_at: datetime) -> None:
        delay = max(0.0, (due_at - _utc_now(self._timing.utc_now())).total_seconds())
        deadline = self._timing.monotonic() + delay
        while True:
            remaining = deadline - self._timing.monotonic()
            if remaining <= 0:
                return
            await self._timing.sleep(remaining)


def next_anchored_boundary(
    anchor_at: datetime,
    now: datetime,
    interval_seconds: int,
) -> datetime:
    """Return the first immutable-anchor boundary strictly after ``now``."""

    _validate_interval(interval_seconds)
    anchor = _utc_now(anchor_at)
    current = _utc_now(now)
    interval = timedelta(seconds=interval_seconds)
    elapsed = current - anchor
    periods = elapsed // interval + 1 if elapsed >= timedelta(0) else 0
    return anchor + periods * interval


def _next_anchored_boundary(anchor_at: datetime, now: datetime, interval_seconds: int) -> datetime:
    return next_anchored_boundary(anchor_at, now, interval_seconds)


def _validate_interval(value: object) -> int:
    if type(value) is not int or not _INTERVAL_MIN_SECONDS <= value <= _INTERVAL_MAX_SECONDS:
        raise StandaloneRecurrenceError("RECURRENCE_INTERVAL_INVALID")
    return value


def _parse_recurrence_id(value: object) -> str:
    if not isinstance(value, str):
        raise StandaloneRecurrenceError("RECURRENCE_NOT_FOUND")
    try:
        parsed = UUID(value)
    except ValueError, AttributeError:
        raise StandaloneRecurrenceError("RECURRENCE_NOT_FOUND") from None
    if str(parsed) != value or parsed.version != 4:
        raise StandaloneRecurrenceError("RECURRENCE_NOT_FOUND")
    return value


def _read_source_workflow(path: Path) -> bytes:
    try:
        with Path(path).open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError
            contents = stream.read(MAX_WORKFLOW_BYTES + 1)
    except OSError:
        raise StandaloneWorkflowError("WORKFLOW_UNAVAILABLE") from None
    if len(contents) > MAX_WORKFLOW_BYTES:
        raise StandaloneWorkflowError("INVALID_WORKFLOW") from None
    return contents


def _validate_record(record: RecurrenceRecord, expected_id: str) -> None:
    if (
        type(record.version) is not int
        or record.version != _VERSION
        or record.id != expected_id
        or record.workflow_file != _WORKFLOW_FILE
        or _ALIAS.fullmatch(record.account) is None
        or type(record.interval_seconds) is not int
        or not _INTERVAL_MIN_SECONDS <= record.interval_seconds <= _INTERVAL_MAX_SECONDS
        or record.status not in ("ENABLED", "DISABLED")
    ):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    timestamps = (
        record.created_at,
        record.updated_at,
        record.anchor_at,
        record.next_due_at,
    )
    for timestamp in timestamps:
        _utc_now(timestamp)
    if record.updated_at < record.created_at or record.anchor_at != record.created_at:
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    interval = timedelta(seconds=record.interval_seconds)
    offset = record.next_due_at - record.anchor_at
    if offset <= timedelta(0) or offset % interval != timedelta(0):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    if record.last_outcome is not None and record.last_outcome not in _OUTCOMES:
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    if record.status == "DISABLED" and record.last_outcome == "RUNNING":
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    if record.last_outcome is None:
        if any(
            item is not None
            for item in (
                record.last_started_at,
                record.last_finished_at,
                record.last_error_code,
                record.last_step_index,
            )
        ):
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    else:
        if record.last_started_at is None:
            raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
        _utc_now(record.last_started_at)
        if record.last_outcome == "RUNNING":
            if (
                record.last_finished_at is not None
                or record.last_error_code is not None
                or record.last_step_index is not None
                or record.next_due_at <= record.last_started_at
            ):
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
        else:
            if record.last_finished_at is None:
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
            _utc_now(record.last_finished_at)
            if record.last_finished_at < record.last_started_at:
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
            if record.next_due_at <= record.last_finished_at:
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
            if record.last_outcome == "SUCCESS":
                if record.last_error_code is not None or record.last_step_index is not None:
                    raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
            elif (
                not isinstance(record.last_error_code, str)
                or _SAFE_ERROR_CODE.fullmatch(record.last_error_code) is None
                or (record.last_outcome == "SKIPPED" and record.last_error_code != "ACCOUNT_BUSY")
                or (
                    record.last_outcome == "INTERRUPTED" and record.last_error_code != "INTERRUPTED"
                )
                or (record.last_outcome == "FAILED" and record.last_error_code == "ACCOUNT_BUSY")
                or (record.last_outcome == "INTERRUPTED" and record.last_step_index is not None)
                or (record.last_outcome in ("FAILED", "SKIPPED") and record.last_step_index is None)
            ):
                raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    if record.last_step_index is not None and (
        type(record.last_step_index) is not int or not 1 <= record.last_step_index <= 32
    ):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    event_times = [record.created_at]
    if record.last_started_at is not None:
        event_times.append(record.last_started_at)
    if record.last_finished_at is not None:
        event_times.append(record.last_finished_at)
    if record.updated_at < max(event_times):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")


def _decode_record(value: object, expected_id: str) -> RecurrenceRecord:
    if not isinstance(value, dict):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    data = cast(dict[str, object], value)
    version = data.get("version")
    if type(version) is int and version != _VERSION:
        raise StandaloneRecurrenceError("RECURRENCE_VERSION_UNSUPPORTED")
    if type(version) is not int:
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    if len(data) != len(_RECORD_KEYS) or any(key not in _RECORD_KEYS for key in data):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    interval_seconds = data["interval_seconds"]
    if (
        type(interval_seconds) is not int
        or not _INTERVAL_MIN_SECONDS <= interval_seconds <= _INTERVAL_MAX_SECONDS
    ):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    try:
        record = RecurrenceRecord(
            version=version,
            id=_required_string(data["id"]),
            workflow_file=_required_string(data["workflow_file"]),
            account=_required_string(data["account"]),
            interval_seconds=interval_seconds,
            status=_required_status(data["status"]),
            created_at=_parse_utc(data["created_at"]),
            updated_at=_parse_utc(data["updated_at"]),
            anchor_at=_parse_utc(data["anchor_at"]),
            next_due_at=_parse_utc(data["next_due_at"]),
            last_started_at=_parse_nullable_utc(data["last_started_at"]),
            last_finished_at=_parse_nullable_utc(data["last_finished_at"]),
            last_outcome=_required_outcome(data["last_outcome"]),
            last_error_code=_required_nullable_error(data["last_error_code"]),
            last_step_index=_required_nullable_step_index(data["last_step_index"]),
        )
    except StandaloneRecurrenceError:
        raise
    except TypeError, ValueError, OverflowError:
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
    _validate_record(record, expected_id)
    return record


def _required_string(value: object) -> str:
    if not isinstance(value, str):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    return value


def _required_status(value: object) -> Literal["ENABLED", "DISABLED"]:
    if not isinstance(value, str) or value not in ("ENABLED", "DISABLED"):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    return value


def _required_outcome(
    value: object,
) -> Literal["RUNNING", "SUCCESS", "FAILED", "SKIPPED", "INTERRUPTED"] | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _OUTCOMES:
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    return cast(Literal["RUNNING", "SUCCESS", "FAILED", "SKIPPED", "INTERRUPTED"], value)


def _required_nullable_error(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SAFE_ERROR_CODE.fullmatch(value) is None:
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    return value


def _required_nullable_step_index(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 32:
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    return value


def _parse_nullable_utc(value: object) -> datetime | None:
    if value is None:
        return None
    return _parse_utc(value)


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
    if _format_utc(_utc_now(parsed)) != value:
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    return parsed.astimezone(UTC)


def _utc_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID")
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return _utc_now(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _encode_record(record: RecurrenceRecord) -> str:
    _validate_record(record, record.id)
    document = {
        "version": record.version,
        "id": record.id,
        "workflow_file": record.workflow_file,
        "account": record.account,
        "interval_seconds": record.interval_seconds,
        "status": record.status,
        "created_at": _format_utc(record.created_at),
        "updated_at": _format_utc(record.updated_at),
        "anchor_at": _format_utc(record.anchor_at),
        "next_due_at": _format_utc(record.next_due_at),
        "last_started_at": (
            None if record.last_started_at is None else _format_utc(record.last_started_at)
        ),
        "last_finished_at": (
            None if record.last_finished_at is None else _format_utc(record.last_finished_at)
        ),
        "last_outcome": record.last_outcome,
        "last_error_code": record.last_error_code,
        "last_step_index": record.last_step_index,
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


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
        raise StandaloneRecurrenceError("RECURRENCE_STATE_INVALID") from None
