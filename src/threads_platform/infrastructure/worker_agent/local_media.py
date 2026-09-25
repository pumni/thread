import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from threads_platform.domain.browser_media import (
    BROWSER_MEDIA_EXTENSIONS,
    MAX_BROWSER_UPLOAD_BYTES,
    BrowserMediaKind,
)
from threads_platform.infrastructure.worker_agent.local_state import LocalDataRoot

_MEDIA_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class LocalMediaFileError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkerLocalMediaFile:
    media_ref: str
    kind: BrowserMediaKind
    byte_size: int
    path: Path = field(repr=False)


class LocalMediaFileResolver:
    """Resolves bounded media references beneath the worker-managed media root."""

    def __init__(
        self,
        data_root: LocalDataRoot,
        *,
        max_file_bytes: int = MAX_BROWSER_UPLOAD_BYTES,
    ) -> None:
        if not 1 <= max_file_bytes <= MAX_BROWSER_UPLOAD_BYTES:
            raise ValueError("maximum local media size exceeds the capability bound")
        self._data_root = data_root
        self._max_file_bytes = max_file_bytes

    def resolve(self, media_ref: str) -> WorkerLocalMediaFile:
        _validate_media_ref(media_ref)
        try:
            media_root = self._data_root.child("media").resolve(strict=True)
        except OSError:
            raise LocalMediaFileError("MEDIA_FILE_UNAVAILABLE") from None
        candidate = media_root / media_ref
        try:
            resolved = candidate.resolve(strict=True)
            file_stat = resolved.stat()
        except OSError:
            raise LocalMediaFileError("MEDIA_FILE_UNAVAILABLE") from None
        if not resolved.is_relative_to(media_root) or not stat.S_ISREG(file_stat.st_mode):
            raise LocalMediaFileError("MEDIA_FILE_REJECTED")

        media_kind = BROWSER_MEDIA_EXTENSIONS.get(resolved.suffix.casefold())
        if media_kind is None or file_stat.st_size < 1:
            raise LocalMediaFileError("MEDIA_FILE_REJECTED")
        if file_stat.st_size > self._max_file_bytes:
            raise LocalMediaFileError("MEDIA_FILE_TOO_LARGE")
        return WorkerLocalMediaFile(media_ref, media_kind, file_stat.st_size, resolved)


def _validate_media_ref(media_ref: str) -> None:
    if (
        _MEDIA_REF.fullmatch(media_ref) is None
        or media_ref in {".", ".."}
        or media_ref.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES
    ):
        raise LocalMediaFileError("MEDIA_FILE_REJECTED")
