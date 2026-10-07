"""Strict operator-supplied Quote drafts for Standalone Nurture."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

_MAX_QUOTE_DRAFT_BYTES = 8_192
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_QUOTE_DRAFT_FIELDS = frozenset({"version", "action", "target_fingerprint", "text"})


class NurtureQuoteDraftError(Exception):
    """A bounded quote-draft error without path or commentary details."""

    code = "INVALID_NURTURE_QUOTE_DRAFT"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __repr__(self) -> str:
        return "NurtureQuoteDraftError(code=INVALID_NURTURE_QUOTE_DRAFT)"


@dataclass(frozen=True, slots=True, repr=False)
class NurtureQuoteDraft:
    target_fingerprint: str
    text: str = field(repr=False)
    action: Literal["QUOTE"] = "QUOTE"

    def __post_init__(self) -> None:
        if (
            type(self.target_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.target_fingerprint) is None
            or type(self.text) is not str
            or not 1 <= len(self.text) <= 500
            or not self.text.strip()
            or type(self.action) is not str
            or self.action != "QUOTE"
        ):
            raise NurtureQuoteDraftError()

    def __repr__(self) -> str:
        return (
            "NurtureQuoteDraft("
            f"target_fingerprint={self.target_fingerprint}, action=QUOTE, text=<redacted>)"
        )


def load_nurture_quote_draft(path: Path) -> NurtureQuoteDraft:
    """Read one exact, bounded UTF-8 quote draft without exposing its path or text."""

    try:
        before = path.lstat()
        if not _regular_file(before) or before.st_size > _MAX_QUOTE_DRAFT_BYTES:
            raise NurtureQuoteDraftError()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            after = path.lstat()
            if (
                not _regular_file(opened)
                or not _regular_file(after)
                or _file_identity(before) != _file_identity(opened)
                or _file_identity(opened) != _file_identity(after)
                or opened.st_size > _MAX_QUOTE_DRAFT_BYTES
            ):
                raise NurtureQuoteDraftError()
            contents = stream.read(_MAX_QUOTE_DRAFT_BYTES + 1)
    except NurtureQuoteDraftError:
        raise
    except OSError, RuntimeError, ValueError, TypeError:
        raise NurtureQuoteDraftError() from None

    if len(contents) > _MAX_QUOTE_DRAFT_BYTES:
        raise NurtureQuoteDraftError()
    try:
        document = contents.decode("utf-8")
        raw: object = json.loads(
            document,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_json_constant,
        )
    except UnicodeDecodeError, ValueError, RecursionError:
        raise NurtureQuoteDraftError() from None
    if type(raw) is not dict or frozenset(cast(dict[str, object], raw)) != _QUOTE_DRAFT_FIELDS:
        raise NurtureQuoteDraftError()
    values = cast(dict[str, object], raw)
    version = values["version"]
    action = values["action"]
    fingerprint = values["target_fingerprint"]
    text = values["text"]
    if (
        type(version) is not int
        or version != 1
        or type(action) is not str
        or action != "QUOTE"
        or type(fingerprint) is not str
        or type(text) is not str
    ):
        raise NurtureQuoteDraftError()
    return NurtureQuoteDraft(target_fingerprint=fingerprint, text=text)


def _regular_file(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISREG(metadata.st_mode) and not attributes & _REPARSE_POINT


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate quote draft field")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
