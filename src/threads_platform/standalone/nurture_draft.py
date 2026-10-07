"""Strict operator-supplied reply drafts for Standalone Nurture."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

_MAX_DRAFT_BYTES = 8_192
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DRAFT_FIELDS = frozenset({"version", "action", "target_fingerprint", "text"})


class NurtureDraftError(Exception):
    """A bounded draft validation error without path or text details."""

    code = "INVALID_NURTURE_DRAFT"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __repr__(self) -> str:
        return "NurtureDraftError(code=INVALID_NURTURE_DRAFT)"


@dataclass(frozen=True, slots=True, repr=False)
class NurtureDraft:
    target_fingerprint: str
    text: str = field(repr=False)
    action: Literal["REPLY"] = "REPLY"
    source: Literal["OPERATOR_FILE"] = "OPERATOR_FILE"

    def __post_init__(self) -> None:
        if (
            type(self.target_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.target_fingerprint) is None
            or type(self.text) is not str
            or not 1 <= len(self.text) <= 500
            or not self.text.strip()
            or type(self.action) is not str
            or self.action != "REPLY"
            or type(self.source) is not str
            or self.source != "OPERATOR_FILE"
        ):
            raise NurtureDraftError()

    def __repr__(self) -> str:
        return (
            "NurtureDraft("
            f"target_fingerprint={self.target_fingerprint}, action=REPLY, "
            "source=OPERATOR_FILE, text=<redacted>)"
        )


def load_nurture_draft(path: Path) -> NurtureDraft:
    """Read one exact, bounded UTF-8 draft file and assign its trusted source."""

    try:
        before = path.lstat()
        if not _regular_file(before) or before.st_size > _MAX_DRAFT_BYTES:
            raise NurtureDraftError()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            after = path.lstat()
            if (
                not _regular_file(opened)
                or not _regular_file(after)
                or _file_identity(before) != _file_identity(opened)
                or _file_identity(opened) != _file_identity(after)
                or opened.st_size > _MAX_DRAFT_BYTES
            ):
                raise NurtureDraftError()
            contents = stream.read(_MAX_DRAFT_BYTES + 1)
    except NurtureDraftError:
        raise
    except OSError, RuntimeError, ValueError, TypeError:
        raise NurtureDraftError() from None

    if len(contents) > _MAX_DRAFT_BYTES:
        raise NurtureDraftError()
    try:
        document = contents.decode("utf-8")
        raw: object = json.loads(
            document,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_json_constant,
        )
    except UnicodeDecodeError, ValueError, RecursionError:
        raise NurtureDraftError() from None
    if type(raw) is not dict or frozenset(cast(dict[str, object], raw)) != _DRAFT_FIELDS:
        raise NurtureDraftError()
    values = cast(dict[str, object], raw)
    version = values["version"]
    action = values["action"]
    fingerprint = values["target_fingerprint"]
    text = values["text"]
    if (
        type(version) is not int
        or version != 1
        or type(action) is not str
        or action != "REPLY"
        or type(fingerprint) is not str
        or type(text) is not str
    ):
        raise NurtureDraftError()
    return NurtureDraft(target_fingerprint=fingerprint, text=text)


def _regular_file(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISREG(metadata.st_mode) and not attributes & _REPARSE_POINT


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate draft field")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
