"""Pure target parsing shared by local and distributed browser reads."""

from __future__ import annotations

import re

BROWSER_READ_TARGET_ANCESTOR_BOUND = 8

_PROFILE_REF = re.compile(r"/@([A-Za-z0-9._]{1,30})/?")
_PROFILE_USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
_THREAD_REF = re.compile(r"/@([A-Za-z0-9._]{1,30})/post/([A-Za-z0-9_-]{1,120})/?")


def normalize_profile_username(username: object) -> str | None:
    if not isinstance(username, str) or _PROFILE_USERNAME.fullmatch(username) is None:
        return None
    return f"/@{username}"


def normalize_profile_ref(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _PROFILE_REF.fullmatch(value)
    if match is None:
        return None
    return f"/@{match.group(1)}"


def parse_thread_ref(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = _THREAD_REF.fullmatch(value)
    if match is None:
        return None
    return f"/@{match.group(1)}/post/{match.group(2)}", match.group(1)
