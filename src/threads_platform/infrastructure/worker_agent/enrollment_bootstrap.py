import os
import re
import stat
from pathlib import Path

from threads_platform.infrastructure.worker_agent.local_state import LocalDataRoot

_MAX_ENROLLMENT_CODE_LENGTH = 256
_ENROLLMENT_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class EnrollmentBootstrapError(ValueError):
    pass


class EnrollmentBootstrapFile:
    """Reads the one-time code from the ACL-managed Worker data root."""

    def __init__(self, data_root: LocalDataRoot) -> None:
        self._data_root = data_root

    @property
    def path(self) -> Path:
        return self._data_root.child("bootstrap", "enrollment-code")

    def read_code(self) -> str | None:
        path = self.path
        parent = path.parent
        try:
            parent_stat = parent.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(parent_stat.st_mode) or _is_reparse_point(parent_stat):
            raise EnrollmentBootstrapError("worker enrollment bootstrap path is invalid")

        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(file_stat.st_mode) or _is_reparse_point(file_stat):
            raise EnrollmentBootstrapError("worker enrollment bootstrap file is invalid")
        if not path.resolve(strict=True).is_relative_to(self._data_root.path):
            raise EnrollmentBootstrapError("worker enrollment bootstrap path is invalid")

        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_ENROLLMENT_CODE_LENGTH + 3)
        except OSError as error:
            raise EnrollmentBootstrapError(
                "worker enrollment bootstrap file is unreadable"
            ) from error
        if len(raw) > _MAX_ENROLLMENT_CODE_LENGTH + 2:
            raise EnrollmentBootstrapError("worker enrollment bootstrap file is invalid")
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        if any(value in raw for value in (b"\r", b"\n", b"\0")):
            raise EnrollmentBootstrapError("worker enrollment bootstrap file is invalid")
        try:
            code = raw.decode("ascii")
        except UnicodeDecodeError as error:
            raise EnrollmentBootstrapError("worker enrollment bootstrap file is invalid") from error
        if len(code) > _MAX_ENROLLMENT_CODE_LENGTH or not _ENROLLMENT_CODE_PATTERN.fullmatch(code):
            raise EnrollmentBootstrapError("worker enrollment bootstrap file is invalid")
        return code

    def remove_after_success(self) -> None:
        try:
            path = self.path
            file_stat = path.lstat()
            if stat.S_ISREG(file_stat.st_mode) and not _is_reparse_point(file_stat):
                path.unlink(missing_ok=True)
        except OSError, ValueError:
            # Removal is best-effort; Windows filesystems do not promise forensic erasure.
            return


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_POINT)
