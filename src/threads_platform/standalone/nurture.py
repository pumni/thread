"""Strict, bounded policy presets for standalone nurture checkpoints."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

_PRESET_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_PUBLIC_USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}\Z")
_MAX_COOLDOWN_SECONDS = 31_536_000


class NurturePresetError(ValueError):
    """A bounded, data-safe preset validation error."""

    code: str

    def __init__(self, code: str = "INVALID_NURTURE_PRESET") -> None:
        if code not in {"INVALID_NURTURE_PRESET", "PRESET_NOT_FOUND"}:
            raise ValueError("invalid nurture preset error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class NurturePresetV1:
    version: int
    id: str
    keyword_queries: tuple[str, ...]
    tag_queries: tuple[str, ...]
    include_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    watched_public_usernames: tuple[str, ...]
    per_source_page_limit: int
    max_total_discovery_candidates: int
    max_selected_candidates: int
    max_browser_enrichments: int
    seen_cooldown_seconds: int
    max_replies_per_explicit_run: int
    max_own_post_mutations_per_explicit_run: int
    engagement_enabled: bool
    owner_public_username: str | None
    max_owned_threads_inspected_per_explicit_run: int
    own_content_cooldown_seconds: int
    own_content_due_interval_seconds: int

    def __post_init__(self) -> None:
        _validate_fields(self)

    def __repr__(self) -> str:
        return (
            "NurturePresetV1("
            f"id={self.id!r}, version={self.version}, "
            f"keyword_queries={len(self.keyword_queries)}, "
            f"tag_queries={len(self.tag_queries)}, "
            f"include_terms={len(self.include_terms)}, "
            f"exclude_terms={len(self.exclude_terms)}, "
            f"watched_public_usernames={len(self.watched_public_usernames)}, "
            f"max_selected_candidates={self.max_selected_candidates})"
        )


_PRESET_FIELDS = frozenset(
    {
        "version",
        "id",
        "keyword_queries",
        "tag_queries",
        "include_terms",
        "exclude_terms",
        "watched_public_usernames",
        "per_source_page_limit",
        "max_total_discovery_candidates",
        "max_selected_candidates",
        "max_browser_enrichments",
        "seen_cooldown_seconds",
        "max_replies_per_explicit_run",
        "max_own_post_mutations_per_explicit_run",
        "engagement_enabled",
        "owner_public_username",
        "max_owned_threads_inspected_per_explicit_run",
        "own_content_cooldown_seconds",
        "own_content_due_interval_seconds",
    }
)


def validate_nurture_preset_v1(value: object) -> NurturePresetV1:
    """Validate one exact v1 document without echoing supplied data in errors."""

    if type(value) is not dict:
        raise NurturePresetError()
    document = cast(dict[str, object], value)
    if frozenset(document) != _PRESET_FIELDS:
        raise NurturePresetError()
    return NurturePresetV1(
        version=cast(int, document["version"]),
        id=cast(str, document["id"]),
        keyword_queries=_parse_string_list(document["keyword_queries"]),
        tag_queries=_parse_string_list(document["tag_queries"]),
        include_terms=_parse_string_list(document["include_terms"]),
        exclude_terms=_parse_string_list(document["exclude_terms"]),
        watched_public_usernames=_parse_string_list(document["watched_public_usernames"]),
        per_source_page_limit=cast(int, document["per_source_page_limit"]),
        max_total_discovery_candidates=cast(int, document["max_total_discovery_candidates"]),
        max_selected_candidates=cast(int, document["max_selected_candidates"]),
        max_browser_enrichments=cast(int, document["max_browser_enrichments"]),
        seen_cooldown_seconds=cast(int, document["seen_cooldown_seconds"]),
        max_replies_per_explicit_run=cast(int, document["max_replies_per_explicit_run"]),
        max_own_post_mutations_per_explicit_run=cast(
            int, document["max_own_post_mutations_per_explicit_run"]
        ),
        engagement_enabled=cast(bool, document["engagement_enabled"]),
        owner_public_username=cast(str | None, document["owner_public_username"]),
        max_owned_threads_inspected_per_explicit_run=cast(
            int, document["max_owned_threads_inspected_per_explicit_run"]
        ),
        own_content_cooldown_seconds=cast(int, document["own_content_cooldown_seconds"]),
        own_content_due_interval_seconds=cast(int, document["own_content_due_interval_seconds"]),
    )


def get_nurture_preset(preset_id: object) -> NurturePresetV1:
    """Return a fresh validated built-in preset by its bounded stable id."""

    if type(preset_id) is not str:
        raise NurturePresetError("PRESET_NOT_FOUND")
    if preset_id != "recruitment":
        raise NurturePresetError("PRESET_NOT_FOUND")
    return validate_nurture_preset_v1(
        {
            "version": 1,
            "id": "recruitment",
            "keyword_queries": [
                "tìm việc",
                "việc làm",
                "tuyển dụng",
                "CV xin việc",
                "phỏng vấn xin việc",
                "career advice",
                "recruiter",
            ],
            "tag_queries": ["timviec", "tuyendung", "career", "hr"],
            "include_terms": [
                "tìm việc",
                "việc làm",
                "tuyển dụng",
                "ứng tuyển",
                "CV",
                "phỏng vấn",
                "career",
                "HR",
                "recruiter",
                "sales",
                "marketing",
                "IT",
                "intern",
                "sinh viên",
                "mới ra trường",
            ],
            "exclude_terms": ["đa cấp", "crypto", "việc nhẹ lương cao"],
            "watched_public_usernames": [],
            "per_source_page_limit": 25,
            "max_total_discovery_candidates": 50,
            "max_selected_candidates": 10,
            "max_browser_enrichments": 5,
            "seen_cooldown_seconds": 604_800,
            "max_replies_per_explicit_run": 1,
            "max_own_post_mutations_per_explicit_run": 1,
            "engagement_enabled": True,
            "owner_public_username": None,
            "max_owned_threads_inspected_per_explicit_run": 0,
            "own_content_cooldown_seconds": 86_400,
            "own_content_due_interval_seconds": 86_400,
        }
    )


def _parse_string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise NurturePresetError()
    items = cast(list[object], value)
    if any(type(item) is not str for item in items):
        raise NurturePresetError()
    return tuple(cast(list[str], items))


def _validate_fields(preset: NurturePresetV1) -> None:
    if type(preset.version) is not int or preset.version != 1:
        raise NurturePresetError()
    if type(preset.id) is not str or len(preset.id) > 48 or _PRESET_ID.fullmatch(preset.id) is None:
        raise NurturePresetError()

    _validate_string_tuple(preset.keyword_queries, 16, 120, allow_empty=False)
    _validate_string_tuple(preset.tag_queries, 16, 120, allow_empty=True)
    if len(preset.keyword_queries) + len(preset.tag_queries) > 16:
        raise NurturePresetError()
    _validate_string_tuple(preset.include_terms, 32, 80, allow_empty=True)
    _validate_string_tuple(preset.exclude_terms, 32, 80, allow_empty=True)
    _validate_username_tuple(preset.watched_public_usernames)

    _validate_integer(preset.per_source_page_limit, 1, 50)
    _validate_integer(preset.max_total_discovery_candidates, 1, 100)
    _validate_integer(preset.max_selected_candidates, 1, 10)
    _validate_integer(preset.max_browser_enrichments, 0, 5)
    _validate_integer(preset.seen_cooldown_seconds, 0, _MAX_COOLDOWN_SECONDS)
    _validate_integer(preset.max_replies_per_explicit_run, 0, 3)
    _validate_integer(preset.max_own_post_mutations_per_explicit_run, 0, 3)
    if type(preset.engagement_enabled) is not bool:
        raise NurturePresetError()
    _validate_integer(preset.max_owned_threads_inspected_per_explicit_run, 0, 10)
    _validate_integer(preset.own_content_cooldown_seconds, 0, _MAX_COOLDOWN_SECONDS)
    _validate_integer(preset.own_content_due_interval_seconds, 1, _MAX_COOLDOWN_SECONDS)

    if preset.max_selected_candidates > preset.max_total_discovery_candidates:
        raise NurturePresetError()
    if preset.max_browser_enrichments > preset.max_selected_candidates:
        raise NurturePresetError()
    if preset.owner_public_username is None:
        if preset.max_owned_threads_inspected_per_explicit_run != 0:
            raise NurturePresetError()
    elif (
        type(preset.owner_public_username) is not str
        or _PUBLIC_USERNAME.fullmatch(preset.owner_public_username) is None
    ):
        raise NurturePresetError()


def _validate_string_tuple(
    value: object,
    maximum_count: int,
    maximum_length: int,
    *,
    allow_empty: bool,
) -> None:
    if type(value) is not tuple:
        raise NurturePresetError()
    items = cast(tuple[object, ...], value)
    if len(items) > maximum_count or (not allow_empty and not items):
        raise NurturePresetError()
    for item in items:
        if type(item) is not str or not _valid_bounded_text(item, maximum_length):
            raise NurturePresetError()


def _validate_username_tuple(value: object) -> None:
    if type(value) is not tuple:
        raise NurturePresetError()
    usernames = cast(tuple[object, ...], value)
    if len(usernames) > 16:
        raise NurturePresetError()
    for username in usernames:
        if type(username) is not str or _PUBLIC_USERNAME.fullmatch(username) is None:
            raise NurturePresetError()


def _valid_bounded_text(value: str, maximum_length: int) -> bool:
    return (
        bool(value)
        and len(value) <= maximum_length
        and value == value.strip()
        and all(character.isprintable() for character in value)
    )


def _validate_integer(value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise NurturePresetError()
