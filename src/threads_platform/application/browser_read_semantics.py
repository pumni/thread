"""Pure profile, thread, and feed semantics shared by browser reads."""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from threads_platform.application.browser_capabilities import BrowserFeedItemResultV1
from threads_platform.application.ports.browser import (
    BROWSER_FEED_CANDIDATE_BOUND as _BROWSER_FEED_CANDIDATE_BOUND,
)
from threads_platform.application.ports.browser import BROWSER_FEED_ORIGIN, BrowserContractError

BROWSER_READ_TARGET_ANCESTOR_BOUND = 8
BROWSER_FEED_ITERATION_BOUND = 5
BROWSER_FEED_CANDIDATE_BOUND = _BROWSER_FEED_CANDIDATE_BOUND
BROWSER_FEED_URL = f"{BROWSER_FEED_ORIGIN}/"

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


def normalize_feed_permalinks(
    permalinks: Sequence[str],
    *,
    max_items: int,
    existing_refs: frozenset[str] = frozenset(),
    position_start: int = 0,
) -> tuple[BrowserFeedItemResultV1, ...]:
    if not 1 <= max_items <= 20:
        raise ValueError("feed observation bound is invalid")
    if len(permalinks) > BROWSER_FEED_CANDIDATE_BOUND:
        raise BrowserContractError()

    seen_refs = set(existing_refs)
    normalized: list[BrowserFeedItemResultV1] = []
    for permalink in permalinks:
        reference = _thread_reference(permalink)
        if reference is None:
            continue
        thread_ref, author_username = reference
        if thread_ref in seen_refs:
            continue

        normalized.append(
            BrowserFeedItemResultV1(
                thread_ref=thread_ref,
                author_username=author_username,
                text_excerpt=None,
                position=position_start + len(normalized),
            )
        )
        seen_refs.add(thread_ref)
        if len(normalized) >= max_items:
            break
    return tuple(normalized)


def _thread_reference(href: object) -> tuple[str, str] | None:
    if not isinstance(href, str):
        return None
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
    if parsed.scheme:
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin != BROWSER_FEED_ORIGIN:
            return None
    elif parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    match = _THREAD_REF.fullmatch(parsed.path)
    if match is None:
        return None
    username = match.group(1).casefold()
    post_id = match.group(2)
    return f"{BROWSER_FEED_ORIGIN}/@{username}/post/{post_id}", username
