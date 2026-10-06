"""Pure profile, thread, and feed semantics shared by browser reads."""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from threads_platform.application.browser_capabilities import BrowserFeedItemResultV1
from threads_platform.application.ports.browser import (
    BROWSER_FEED_CANDIDATE_BOUND as _BROWSER_FEED_CANDIDATE_BOUND,
)
from threads_platform.application.ports.browser import (
    BROWSER_FEED_ORIGIN,
    BrowserContractError,
    FeedCandidateObservation,
)

BROWSER_READ_TARGET_ANCESTOR_BOUND = 8
BROWSER_FEED_ANCESTOR_BOUND = 8
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


def normalize_feed_candidates(
    candidates: Sequence[FeedCandidateObservation],
    *,
    max_items: int,
    ancestor_bound: int = BROWSER_FEED_ANCESTOR_BOUND,
    existing_refs: frozenset[str] = frozenset(),
    position_start: int = 0,
) -> tuple[BrowserFeedItemResultV1, ...]:
    if not 1 <= max_items <= 20 or ancestor_bound < 1:
        raise ValueError("feed observation bounds are invalid")
    if len(candidates) > BROWSER_FEED_CANDIDATE_BOUND:
        raise BrowserContractError()

    seen_refs = set(existing_refs)
    normalized: list[BrowserFeedItemResultV1] = []
    for candidate in candidates:
        pivot = _thread_reference(candidate.permalink_href)
        if pivot is None:
            raise BrowserContractError()
        thread_ref, pivot_username = pivot
        if thread_ref in seen_refs:
            continue

        matched: tuple[str, str] | None = None
        for ancestor in candidate.ancestors[:ancestor_bound]:
            if ancestor.links_truncated or ancestor.text_regions_truncated:
                raise BrowserContractError()

            thread_refs_list: list[str] = []
            for href in ancestor.hrefs:
                reference = _thread_reference(href)
                if reference is not None:
                    thread_refs_list.append(reference[0])
            thread_refs = tuple(thread_refs_list)
            matching_pivots = sum(ref == thread_ref for ref in thread_refs)
            if any(ref != thread_ref for ref in thread_refs):
                raise BrowserContractError()
            if matching_pivots > 1:
                raise BrowserContractError()
            if matching_pivots != 1:
                continue

            compatible_authors = tuple(
                username
                for href in ancestor.hrefs
                if (username := _profile_username(href)) is not None
                and username.casefold() == pivot_username.casefold()
            )
            if len(compatible_authors) > 1:
                raise BrowserContractError()
            if not compatible_authors:
                continue

            excerpts = tuple(
                " ".join(region.split())
                for region in ancestor.text_regions
                if " ".join(region.split())
            )
            if not excerpts:
                continue
            excerpt = max(excerpts, key=len)[:500]
            matched = (compatible_authors[0].casefold(), excerpt)
            break

        if matched is None:
            raise BrowserContractError()

        normalized.append(
            BrowserFeedItemResultV1(
                thread_ref=thread_ref,
                author_username=matched[0],
                text_excerpt=matched[1],
                position=position_start + len(normalized),
            )
        )
        seen_refs.add(thread_ref)
        if len(normalized) >= max_items:
            break
    return tuple(normalized)


def _thread_reference(href: str) -> tuple[str, str] | None:
    parsed = urlsplit(href)
    if parsed.scheme:
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin != BROWSER_FEED_ORIGIN:
            return None
    elif parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    if parsed.query or parsed.fragment:
        return None
    match = _THREAD_REF.fullmatch(parsed.path)
    if match is None:
        return None
    username = match.group(1).casefold()
    post_id = match.group(2)
    return f"{BROWSER_FEED_ORIGIN}/@{username}/post/{post_id}", username


def _profile_username(href: str) -> str | None:
    parsed = urlsplit(href)
    if parsed.scheme:
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin != BROWSER_FEED_ORIGIN:
            return None
    elif parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    if parsed.query or parsed.fragment:
        return None
    match = _PROFILE_REF.fullmatch(parsed.path)
    return match.group(1) if match is not None else None
