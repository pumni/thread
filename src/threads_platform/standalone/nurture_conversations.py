"""Bounded read-only collection of replies on explicitly configured own Threads."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

from threads_platform.application.ports.threads import (
    DiscoveryPage,
    RemoteDiscoveryThread,
    RemoteReply,
    ReplyPage,
)
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.nurture import NurturePresetV1
from threads_platform.standalone.nurture_store import (
    NurtureStateError,
    fingerprint_remote_reply,
    fingerprint_remote_thread,
)

_SAFE_SOURCE_CODES = frozenset(
    {
        "DISCOVERY_CONTRACT_MISMATCH",
        "DISCOVERY_SOURCE_FAILED",
        "INVALID_CURSOR",
        "INVALID_LIMIT",
        "INVALID_THREAD_ID",
        "INVALID_USERNAME",
        "THREADS_API_REJECTED_REQUEST",
        "THREADS_AUTHENTICATION_FAILED",
        "THREADS_CREDENTIAL_EXPIRED",
        "THREADS_CREDENTIAL_INVALID",
        "THREADS_CREDENTIAL_NOT_CONFIGURED",
        "THREADS_CREDENTIAL_SECRET_UNAVAILABLE",
        "THREADS_DOCUMENTATION_CONTRACT_MISMATCH",
        "THREADS_INVALID_REQUEST",
        "THREADS_OBJECT_NOT_FOUND",
        "THREADS_PERMISSION_DENIED",
        "THREADS_RATE_LIMITED",
        "THREADS_REAUTHORIZATION_REQUIRED",
        "THREADS_SERVER_ERROR",
        "THREADS_TRANSPORT_FAILURE",
    }
)
_PROFILE_STAGE = "profile_posts"
_CONVERSATION_STAGE = "conversation"


@dataclass(frozen=True, slots=True, repr=False)
class NurtureInboundCandidate:
    """An ephemeral proven reply with repr-hidden IDs for explicit apply only."""

    fingerprint: str
    timestamp: datetime | None
    root_thread_id: str = field(repr=False)
    reply_id: str = field(repr=False)
    replied_to_id: str | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            "NurtureInboundCandidate("
            f"fingerprint={self.fingerprint[:12]}…, timestamp_known={self.timestamp is not None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class NurtureInboundResult:
    """Bounded reply counts and ordered proven candidates from one own-thread scan."""

    discovered_count: int
    deduped_count: int
    candidates: tuple[NurtureInboundCandidate, ...]

    def __repr__(self) -> str:
        return (
            "NurtureInboundResult("
            f"discovered_count={self.discovered_count}, deduped_count={self.deduped_count}, "
            f"candidate_count={len(self.candidates)})"
        )


class NurtureConversationError(Exception):
    """A bounded source failure with no owner name, reply ID, or exception message."""

    def __init__(self, code: str, stage: str) -> None:
        self.code = (
            code if type(code) is str and code in _SAFE_SOURCE_CODES else "DISCOVERY_SOURCE_FAILED"
        )
        self.stage = (
            stage
            if type(stage) is str and stage in {_PROFILE_STAGE, _CONVERSATION_STAGE}
            else _PROFILE_STAGE
        )
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"NurtureConversationError(code={self.code}, stage={self.stage})"


async def collect_nurture_inbound(
    api: LocalThreadsApiRuntime,
    account_alias: str,
    preset: NurturePresetV1,
) -> NurtureInboundResult:
    """Read one owner-profile page and one conversation page per selected own Thread."""

    owner_username = preset.owner_public_username
    maximum_threads = preset.max_owned_threads_inspected_per_explicit_run
    if owner_username is None or maximum_threads == 0:
        return NurtureInboundResult(0, 0, ())

    profile_limit = min(preset.per_source_page_limit, maximum_threads)
    try:
        profile_page = await api.profile_posts(
            account_alias,
            owner_username,
            after=None,
            limit=profile_limit,
        )
    except Exception as error:
        raise _source_error(error, _PROFILE_STAGE) from None
    if type(profile_page) is not DiscoveryPage or type(profile_page.threads) is not tuple:
        raise NurtureConversationError("DISCOVERY_CONTRACT_MISMATCH", _PROFILE_STAGE)

    own_thread_ids = _unique_thread_prefix(profile_page, maximum_threads)
    reply_occurrences = 0
    reply_fingerprints: set[str] = set()
    proven_by_fingerprint: dict[str, NurtureInboundCandidate] = {}

    for own_thread_id in own_thread_ids:
        try:
            page = await api.conversation(account_alias, own_thread_id, after=None)
        except Exception as error:
            raise _source_error(error, _CONVERSATION_STAGE) from None
        if type(page) is not ReplyPage or type(page.replies) is not tuple:
            raise NurtureConversationError("DISCOVERY_CONTRACT_MISMATCH", _CONVERSATION_STAGE)

        # Conversation has no caller-supplied page limit in the accepted shared API.
        # Inspect only this deterministic prefix to keep receipt and candidate work bounded.
        for reply in page.replies[: preset.per_source_page_limit]:
            if type(reply) is not RemoteReply:
                raise NurtureConversationError("DISCOVERY_CONTRACT_MISMATCH", _CONVERSATION_STAGE)
            reply_occurrences += 1
            if reply.is_reply_owned_by_me is not None and (
                type(reply.is_reply_owned_by_me) is not bool
            ):
                raise NurtureConversationError("DISCOVERY_CONTRACT_MISMATCH", _CONVERSATION_STAGE)
            try:
                fingerprint = fingerprint_remote_reply(reply.reply_id)
            except NurtureStateError:
                continue
            reply_fingerprints.add(fingerprint)

            if reply.is_reply_owned_by_me is not False or reply.root_post_id != own_thread_id:
                continue

            candidate = NurtureInboundCandidate(
                fingerprint=fingerprint,
                timestamp=_parse_reply_timestamp(reply.timestamp),
                root_thread_id=own_thread_id,
                reply_id=reply.reply_id,
                replied_to_id=reply.replied_to_id,
            )
            # The first proven occurrence keeps its timestamp; repeated hits never
            # improve a candidate's relative priority.
            if fingerprint not in proven_by_fingerprint:
                proven_by_fingerprint[fingerprint] = candidate

    candidates = tuple(sorted(proven_by_fingerprint.values(), key=_inbound_sort_key))
    return NurtureInboundResult(
        discovered_count=reply_occurrences,
        deduped_count=len(reply_fingerprints),
        candidates=candidates,
    )


def _unique_thread_prefix(page: DiscoveryPage, maximum_threads: int) -> tuple[str, ...]:
    selected: list[str] = []
    seen_fingerprints: set[str] = set()
    for thread in page.threads:
        if type(thread) is not RemoteDiscoveryThread:
            raise NurtureConversationError("DISCOVERY_CONTRACT_MISMATCH", _PROFILE_STAGE)
        if type(thread.remote_thread_id) is not str:
            continue
        normalized = unicodedata.normalize("NFC", thread.remote_thread_id.strip())
        try:
            fingerprint = fingerprint_remote_thread(normalized)
        except NurtureStateError:
            continue
        if fingerprint in seen_fingerprints:
            continue
        selected.append(normalized)
        seen_fingerprints.add(fingerprint)
        if len(selected) >= maximum_threads:
            break
    return tuple(selected)


def _parse_reply_timestamp(value: str | None) -> datetime | None:
    if type(value) is not str or not value or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except OverflowError, TypeError, ValueError:
        return None


def _inbound_sort_key(
    candidate: NurtureInboundCandidate,
) -> tuple[int, int, str]:
    timestamp = candidate.timestamp
    if timestamp is None:
        return (1, 0, candidate.fingerprint)
    epoch_microseconds = (
        (timestamp.toordinal() - 1) * 86_400_000_000
        + timestamp.hour * 3_600_000_000
        + timestamp.minute * 60_000_000
        + timestamp.second * 1_000_000
        + timestamp.microsecond
    )
    return (0, -epoch_microseconds, candidate.fingerprint)


def _source_error(error: Exception, stage: str) -> NurtureConversationError:
    try:
        error_code = getattr(error, "code", None)
    except Exception:
        error_code = None
    if type(error_code) is not str or error_code not in _SAFE_SOURCE_CODES:
        error_code = "DISCOVERY_SOURCE_FAILED"
    return NurtureConversationError(error_code, stage)
