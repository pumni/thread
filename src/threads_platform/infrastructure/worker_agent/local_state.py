import hashlib
import os
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from threads_platform.application.ports.worker_agent import LocalRecoveryEntry, LocalSessionState
from threads_platform.domain.workers import BrowserSessionState


class LocalWorkerStateError(ValueError):
    pass


class ProfileOwnershipError(LocalWorkerStateError):
    pass


class SessionCapacityError(LocalWorkerStateError):
    pass


class LocalDataRoot:
    _directories = ("worker", "profiles", "journal", "cache", "logs", "updates")

    def __init__(self, root: Path) -> None:
        self._configured_root = root.expanduser()

    @classmethod
    def from_environment(cls, root: Path | None = None) -> LocalDataRoot:
        if root is not None:
            return cls(root)
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise LocalWorkerStateError("LOCALAPPDATA is required for the default worker data root")
        return cls(Path(local_app_data) / "ThreadsOperations")

    @property
    def path(self) -> Path:
        root = self._configured_root.resolve()
        _reject_git_worktree_path(root)
        return root

    def prepare(self) -> None:
        _reject_git_worktree_path(self._configured_root.resolve())
        self._configured_root.mkdir(parents=True, exist_ok=True)
        root = self._configured_root.resolve(strict=True)
        for directory in self._directories:
            child = self._configured_root / directory
            child.mkdir(parents=True, exist_ok=True)
            resolved = child.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise LocalWorkerStateError("worker data directory escapes the managed root")

    def child(self, *parts: str) -> Path:
        root = self.path
        candidate = self._configured_root.joinpath(*parts)
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise LocalWorkerStateError("worker path escapes the managed root")
        return candidate

    @property
    def journal_path(self) -> Path:
        return self.child("journal", "worker-state.sqlite3")


class LocalProfileDirectoryResolver:
    def __init__(self, data_root: LocalDataRoot, store: WorkerLocalStateStore) -> None:
        self._data_root = data_root
        self._store = store

    def resolve(self, worker_id: UUID, account_id: UUID, profile_ref: str) -> Path:
        _validate_profile_ref(profile_ref)
        self._store.bind_profile_owner(worker_id, account_id, profile_ref)
        profile_key = hashlib.sha256(profile_ref.encode("utf-8")).hexdigest()
        directory = self._data_root.child("profiles", str(worker_id), profile_key)
        directory.mkdir(parents=True, exist_ok=True)
        resolved = directory.resolve(strict=True)
        if not resolved.is_relative_to(self._data_root.path):
            raise LocalWorkerStateError("profile directory escapes the managed root")
        return resolved

    def ensure_profile(self, worker_id: UUID, account_id: UUID, profile_ref: str) -> None:
        self.resolve(worker_id, account_id, profile_ref)


class WorkerLocalStateStore:
    def __init__(self, data_root: LocalDataRoot, worker_id: UUID) -> None:
        self._data_root = data_root
        self._worker_id = worker_id
        data_root.prepare()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._data_root.journal_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile_owners (
                    worker_id TEXT NOT NULL,
                    profile_ref TEXT NOT NULL,
                    profile_hash TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    PRIMARY KEY (worker_id, profile_hash),
                    UNIQUE (worker_id, profile_ref)
                );
                CREATE TABLE IF NOT EXISTS local_browser_sessions (
                    account_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    profile_ref TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_reservations (
                    account_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    profile_ref TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL UNIQUE,
                    reserved_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_session_reports (
                    account_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    worker_id TEXT NOT NULL,
                    profile_ref TEXT NOT NULL,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, session_id, revision)
                );
                CREATE TABLE IF NOT EXISTS worker_recovery_journal (
                    worker_job_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    profile_ref TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def bind_profile_owner(self, worker_id: UUID, account_id: UUID, profile_ref: str) -> None:
        _validate_profile_ref(profile_ref)
        profile_hash = hashlib.sha256(profile_ref.encode("utf-8")).hexdigest()
        if worker_id != self._worker_id:
            raise ProfileOwnershipError("profile belongs to a different worker identity")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO profile_owners "
                "(worker_id, profile_ref, profile_hash, account_id) VALUES (?, ?, ?, ?)",
                (str(worker_id), profile_ref, profile_hash, str(account_id)),
            )
            owner = connection.execute(
                "SELECT account_id FROM profile_owners WHERE worker_id = ? AND profile_hash = ?",
                (str(worker_id), profile_hash),
            ).fetchone()
            if owner is None or owner["account_id"] != str(account_id):
                raise ProfileOwnershipError("profile_ref belongs to another account")

    def reserve_session(
        self,
        *,
        worker_id: UUID,
        account_id: UUID,
        profile_ref: str,
        session_id: UUID,
        maximum: int,
    ) -> None:
        if maximum < 1:
            raise ValueError("maximum browser sessions must be positive")
        if worker_id != self._worker_id:
            raise LocalWorkerStateError(
                "session reservation belongs to a different worker identity"
            )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT account_id FROM profile_owners WHERE worker_id=? AND profile_ref=?",
                (str(worker_id), profile_ref),
            ).fetchone()
            if owner is None or owner["account_id"] != str(account_id):
                raise ProfileOwnershipError("profile_ref is not assigned to this account")
            current = connection.execute(
                "SELECT profile_ref, session_id FROM session_reservations WHERE account_id = ?",
                (str(account_id),),
            ).fetchone()
            if current is not None:
                if current["profile_ref"] != profile_ref:
                    raise LocalWorkerStateError(
                        "account already has a reservation for another profile"
                    )
                if current["session_id"] != str(session_id):
                    raise LocalWorkerStateError(
                        "account already has a different session reservation"
                    )
                return
            active_count = connection.execute(
                "SELECT count(*) FROM session_reservations WHERE worker_id = ?",
                (str(worker_id),),
            ).fetchone()[0]
            if active_count >= maximum:
                raise SessionCapacityError("maximum browser sessions reached")
            connection.execute(
                "INSERT INTO session_reservations "
                "(account_id, worker_id, profile_ref, session_id, reserved_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(account_id),
                    str(worker_id),
                    profile_ref,
                    str(session_id),
                    _timestamp(datetime.now(UTC)),
                ),
            )

    def release_session(self, account_id: UUID, session_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM session_reservations WHERE account_id = ? AND session_id = ?",
                (str(account_id), str(session_id)),
            )

    def active_session_count(self) -> int:
        with self._connection() as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM session_reservations WHERE worker_id = ?",
                    (str(self._worker_id),),
                ).fetchone()[0]
            )

    def get_session(self, account_id: UUID) -> LocalSessionState | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM local_browser_sessions WHERE account_id = ?",
                (str(account_id),),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def record_session_state(
        self,
        *,
        worker_id: UUID,
        account_id: UUID,
        profile_ref: str,
        session_id: UUID,
        state: BrowserSessionState,
        updated_at: datetime,
    ) -> LocalSessionState:
        _validate_profile_ref(profile_ref)
        timestamp = _timestamp(updated_at)
        if worker_id != self._worker_id:
            raise LocalWorkerStateError("session report belongs to a different worker identity")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT account_id FROM profile_owners WHERE worker_id=? AND profile_ref=?",
                (str(worker_id), profile_ref),
            ).fetchone()
            if owner is None or owner["account_id"] != str(account_id):
                raise ProfileOwnershipError("profile_ref is not assigned to this account")
            previous = connection.execute(
                "SELECT * FROM local_browser_sessions WHERE account_id = ?",
                (str(account_id),),
            ).fetchone()
            if (
                previous is not None
                and previous["session_id"] == str(session_id)
                and previous["profile_ref"] != profile_ref
            ):
                raise ProfileOwnershipError("a local session cannot change its profile_ref")
            if previous is not None and previous["session_id"] != str(session_id):
                if BrowserSessionState(previous["state"]) not in {
                    BrowserSessionState.ERROR,
                    BrowserSessionState.SESSION_EXPIRED,
                    BrowserSessionState.STOPPED,
                }:
                    raise LocalWorkerStateError("account has an active local session")
            revision = int(previous["revision"]) + 1 if previous is not None else 1
            connection.execute(
                "INSERT INTO local_browser_sessions "
                "(account_id, worker_id, profile_ref, session_id, state, revision, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET worker_id=excluded.worker_id, "
                "profile_ref=excluded.profile_ref, session_id=excluded.session_id, "
                "state=excluded.state, revision=excluded.revision, updated_at=excluded.updated_at",
                (
                    str(account_id),
                    str(worker_id),
                    profile_ref,
                    str(session_id),
                    state.value,
                    revision,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO pending_session_reports "
                "(account_id, session_id, revision, worker_id, profile_ref, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(account_id),
                    str(session_id),
                    revision,
                    str(worker_id),
                    profile_ref,
                    state.value,
                    timestamp,
                ),
            )
        return LocalSessionState(account_id, profile_ref, session_id, state, revision, updated_at)

    def recover_after_restart(self, *, updated_at: datetime) -> list[LocalSessionState]:
        recovered: list[LocalSessionState] = []
        timestamp = _timestamp(updated_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reservations = connection.execute("SELECT * FROM session_reservations").fetchall()
            for reservation in reservations:
                previous = connection.execute(
                    "SELECT * FROM local_browser_sessions WHERE account_id = ?",
                    (reservation["account_id"],),
                ).fetchone()
                if previous is None:
                    continue
                revision = int(previous["revision"]) + 1
                connection.execute(
                    "UPDATE local_browser_sessions SET state=?, revision=?, updated_at=? "
                    "WHERE account_id=?",
                    (
                        BrowserSessionState.SESSION_EXPIRED.value,
                        revision,
                        timestamp,
                        reservation["account_id"],
                    ),
                )
                connection.execute(
                    "INSERT INTO pending_session_reports "
                    "(account_id, session_id, revision, worker_id, profile_ref, state, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        reservation["account_id"],
                        reservation["session_id"],
                        revision,
                        reservation["worker_id"],
                        reservation["profile_ref"],
                        BrowserSessionState.SESSION_EXPIRED.value,
                        timestamp,
                    ),
                )
                recovered.append(
                    LocalSessionState(
                        UUID(reservation["account_id"]),
                        reservation["profile_ref"],
                        UUID(reservation["session_id"]),
                        BrowserSessionState.SESSION_EXPIRED,
                        revision,
                        updated_at,
                    )
                )
            connection.execute("DELETE FROM session_reservations")
        return recovered

    def pending_session_reports(self) -> list[LocalSessionState]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_session_reports ORDER BY updated_at, revision, account_id"
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    def mark_session_report_delivered(self, report: LocalSessionState) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM pending_session_reports WHERE account_id=? AND session_id=? "
                "AND revision=?",
                (str(report.account_id), str(report.session_id), report.revision),
            )

    def save_recovery_entry(self, entry: LocalRecoveryEntry) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", entry.phase):
            raise ValueError("recovery phase must be a bounded code")
        _validate_profile_ref(entry.profile_ref)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO worker_recovery_journal "
                "(worker_job_id, account_id, profile_ref, phase, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(worker_job_id) DO UPDATE SET account_id=excluded.account_id, "
                "profile_ref=excluded.profile_ref, phase=excluded.phase, "
                "updated_at=excluded.updated_at",
                (
                    str(entry.worker_job_id),
                    str(entry.account_id),
                    entry.profile_ref,
                    entry.phase,
                    _timestamp(entry.updated_at),
                ),
            )

    def recovery_entries(self) -> list[LocalRecoveryEntry]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM worker_recovery_journal ORDER BY updated_at, worker_job_id"
            ).fetchall()
        return [
            LocalRecoveryEntry(
                worker_job_id=UUID(row["worker_job_id"]),
                account_id=UUID(row["account_id"]),
                profile_ref=row["profile_ref"],
                phase=row["phase"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def clear_recovery_entry(self, worker_job_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM worker_recovery_journal WHERE worker_job_id = ?",
                (str(worker_job_id),),
            )


def _validate_profile_ref(profile_ref: str) -> None:
    if (
        not profile_ref.strip()
        or profile_ref in {".", ".."}
        or any(character in profile_ref for character in ("/", "\\", ":", "\0"))
    ):
        raise LocalWorkerStateError("profile_ref must be a logical identifier")


def _reject_git_worktree_path(path: Path) -> None:
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise LocalWorkerStateError("worker data root must be outside Git worktrees")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("local timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _session_from_row(row: sqlite3.Row) -> LocalSessionState:
    return LocalSessionState(
        account_id=UUID(row["account_id"]),
        profile_ref=row["profile_ref"],
        session_id=UUID(row["session_id"]),
        state=BrowserSessionState(row["state"]),
        revision=int(row["revision"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
