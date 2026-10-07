"""Strict operator-supplied Threads-native own-content packets for Nurture."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from threads_platform.standalone.mutations import StandaloneMutationError, validate_publish_text

ContentCategory = Literal[
    "CAREER_TIP",
    "CV_GUIDANCE",
    "INTERVIEW_PREP",
    "RECRUITMENT_MARKET",
    "EMPLOYER_GUIDANCE",
    "AUTHORIZED_CANDIDATE_EXAMPLE",
]

_MAX_CONTENT_BYTES = 16_384
_MAX_SOURCE_ID_LENGTH = 256
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_CONTENT_CATEGORIES = frozenset(
    {
        "CAREER_TIP",
        "CV_GUIDANCE",
        "INTERVIEW_PREP",
        "RECRUITMENT_MARKET",
        "EMPLOYER_GUIDANCE",
        "AUTHORIZED_CANDIDATE_EXAMPLE",
    }
)
_CONTENT_FIELDS = frozenset(
    {
        "version",
        "action",
        "candidate_id",
        "account",
        "preset",
        "source_kind",
        "source_id",
        "category",
        "text",
    }
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class NurtureContentError(Exception):
    """A bounded packet validation error without source, path, or text details."""

    code = "INVALID_NURTURE_CONTENT"

    def __init__(self, code: str = "INVALID_NURTURE_CONTENT") -> None:
        if code not in {"INVALID_NURTURE_CONTENT", "NURTURE_CONTENT_ACCOUNT_MISMATCH"}:
            code = "INVALID_NURTURE_CONTENT"
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"NurtureContentError(code={self.code})"


@dataclass(frozen=True, slots=True, repr=False)
class ContentCandidateV1:
    """Ephemeral validated packet; raw source id and draft text stay in memory only."""

    candidate_id: UUID
    account_alias: str
    preset_id: str
    source_id: str = field(repr=False)
    category: ContentCategory
    text: str = field(repr=False)
    source_kind: Literal["OPERATOR_SOURCE_ID"] = "OPERATOR_SOURCE_ID"

    def __post_init__(self) -> None:
        if (
            type(self.candidate_id) is not UUID
            or self.candidate_id.version != 4
            or type(self.account_alias) is not str
            or not self.account_alias
            or type(self.preset_id) is not str
            or not self.preset_id
            or type(self.source_kind) is not str
            or self.source_kind != "OPERATOR_SOURCE_ID"
            or type(self.category) is not str
            or self.category not in _CONTENT_CATEGORIES
            or type(self.source_id) is not str
            or not _valid_source_id(self.source_id)
            or type(self.text) is not str
        ):
            raise NurtureContentError()
        try:
            validate_publish_text(self.text)
            source_fingerprint(self.source_kind, self.source_id)
            draft_fingerprint(self.text)
        except StandaloneMutationError, UnicodeError, ValueError:
            raise NurtureContentError() from None

    @property
    def source_fingerprint(self) -> str:
        return source_fingerprint(self.source_kind, self.source_id)

    @property
    def draft_fingerprint(self) -> str:
        return draft_fingerprint(self.text)

    def __repr__(self) -> str:
        return (
            "ContentCandidateV1("
            f"candidate_id={self.candidate_id}, account_alias={self.account_alias!r}, "
            f"preset_id={self.preset_id!r}, category={self.category}, "
            f"source_fingerprint={self.source_fingerprint}, "
            f"draft_fingerprint={self.draft_fingerprint}, "
            "source_id=<redacted>, text=<redacted>)"
        )


def load_content_candidate(
    path: Path,
    *,
    account_alias: str,
    preset_id: str,
) -> ContentCandidateV1:
    """Read one exact, bounded UTF-8 packet and bind it to the invocation."""

    try:
        before = path.lstat()
        if not _regular_file(before) or before.st_size > _MAX_CONTENT_BYTES:
            raise NurtureContentError()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            after = path.lstat()
            if (
                not _regular_file(opened)
                or not _regular_file(after)
                or _file_identity(before) != _file_identity(opened)
                or _file_identity(opened) != _file_identity(after)
                or opened.st_size > _MAX_CONTENT_BYTES
            ):
                raise NurtureContentError()
            contents = stream.read(_MAX_CONTENT_BYTES + 1)
    except NurtureContentError:
        raise
    except OSError, RuntimeError, ValueError, TypeError:
        raise NurtureContentError() from None

    if len(contents) > _MAX_CONTENT_BYTES:
        raise NurtureContentError()
    try:
        raw: object = json.loads(
            contents.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_json_constant,
        )
    except UnicodeDecodeError, ValueError, RecursionError:
        raise NurtureContentError() from None
    if type(raw) is not dict or frozenset(cast(dict[str, object], raw)) != _CONTENT_FIELDS:
        raise NurtureContentError()

    values = cast(dict[str, object], raw)
    version = values["version"]
    action = values["action"]
    candidate_id_value = values["candidate_id"]
    packet_account = values["account"]
    packet_preset = values["preset"]
    source_kind = values["source_kind"]
    source_id = values["source_id"]
    category = values["category"]
    text = values["text"]
    if (
        type(version) is not int
        or version != 1
        or type(action) is not str
        or action != "POST_TEXT"
        or type(candidate_id_value) is not str
        or type(packet_account) is not str
        or type(packet_preset) is not str
        or type(source_kind) is not str
        or source_kind != "OPERATOR_SOURCE_ID"
        or type(source_id) is not str
        or type(category) is not str
        or category not in _CONTENT_CATEGORIES
        or type(text) is not str
    ):
        raise NurtureContentError()
    if packet_account != account_alias or packet_preset != preset_id:
        raise NurtureContentError("NURTURE_CONTENT_ACCOUNT_MISMATCH")
    candidate_id = _parse_uuid4(candidate_id_value)
    if not _valid_source_id(source_id):
        raise NurtureContentError()
    try:
        return ContentCandidateV1(
            candidate_id=candidate_id,
            account_alias=account_alias,
            preset_id=preset_id,
            source_id=source_id,
            category=cast(ContentCategory, category),
            text=text,
            source_kind="OPERATOR_SOURCE_ID",
        )
    except NurtureContentError:
        raise


def source_fingerprint(source_kind: str, source_id: str) -> str:
    if (
        type(source_kind) is not str
        or source_kind != "OPERATOR_SOURCE_ID"
        or type(source_id) is not str
        or not _valid_source_id(source_id)
    ):
        raise NurtureContentError()
    identity = json.dumps(
        {
            "source_id": unicodedata.normalize("NFC", source_id),
            "source_kind": source_kind,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def draft_fingerprint(text: str) -> str:
    if type(text) is not str:
        raise NurtureContentError()
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    try:
        return hashlib.sha256(
            b"threads-native-post:v1\x00" + normalized.encode("utf-8")
        ).hexdigest()
    except UnicodeError:
        raise NurtureContentError() from None


def content_fingerprint(source_fp: str, draft_fp: str) -> str:
    """Return a namespaced identity for one source and one exact Threads draft."""
    if (
        type(source_fp) is not str
        or _FINGERPRINT.fullmatch(source_fp) is None
        or type(draft_fp) is not str
        or _FINGERPRINT.fullmatch(draft_fp) is None
    ):
        raise NurtureContentError()
    identity = json.dumps(
        {"draft_fingerprint": draft_fp, "source_fingerprint": source_fp, "version": 1},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(b"threads-nurture-content:v1\x00" + identity).hexdigest()


def _valid_source_id(value: str) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= _MAX_SOURCE_ID_LENGTH
        and value == value.strip()
        and "\r" not in value
        and "\n" not in value
        and all(character.isprintable() for character in value)
    )


def _regular_file(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISREG(metadata.st_mode) and not attributes & _REPARSE_POINT


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _parse_uuid4(value: str) -> UUID:
    try:
        result = UUID(value)
    except ValueError, AttributeError:
        raise NurtureContentError() from None
    if str(result) != value or result.version != 4:
        raise NurtureContentError()
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate content field")
        result[key] = value
    return result


def _reject_non_json_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")
