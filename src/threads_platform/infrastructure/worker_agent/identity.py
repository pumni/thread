import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from threads_platform.infrastructure.worker_agent.local_state import LocalDataRoot


class WorkerIdentityStoreError(ValueError):
    pass


class WorkerIdentityFileStore:
    def __init__(self, data_root: LocalDataRoot) -> None:
        self._path = data_root.child("worker", "worker_id")

    def load_or_create(self) -> UUID:
        try:
            return self._load()
        except FileNotFoundError:
            worker_id = uuid4()
            try:
                create_file_if_absent(self._path, f"{worker_id}\nPENDING\n".encode("ascii"))
            except FileExistsError:
                pass
            return self._load()
        except (OSError, ValueError) as error:
            raise WorkerIdentityStoreError("persisted worker identity is unreadable") from error

    def _load(self) -> UUID:
        raw = self._path.read_text(encoding="ascii").splitlines()
        try:
            return UUID(raw[0])
        except (IndexError, ValueError) as error:
            raise WorkerIdentityStoreError("persisted worker identity is corrupt") from error

    def enrollment_pending(self) -> bool:
        try:
            lines = self._path.read_text(encoding="ascii").splitlines()
        except OSError as error:
            raise WorkerIdentityStoreError("persisted worker identity is unreadable") from error
        if not lines:
            raise WorkerIdentityStoreError("persisted worker identity is corrupt")
        if len(lines) > 1 and lines[1] not in {"PENDING", "ENROLLED"}:
            raise WorkerIdentityStoreError("persisted worker enrollment state is corrupt")
        return len(lines) > 1 and lines[1] == "PENDING"

    def mark_enrolled(self) -> None:
        worker_id = self._load()
        try:
            _replace_file(self._path, f"{worker_id}\nENROLLED\n".encode("ascii"))
        except OSError as error:
            raise WorkerIdentityStoreError(
                "worker enrollment state could not be persisted"
            ) from error


def create_file_if_absent(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".worker-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_file(path: Path, contents: bytes) -> None:
    handle, temporary_name = tempfile.mkstemp(prefix=".worker-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
