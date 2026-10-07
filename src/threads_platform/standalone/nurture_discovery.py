"""Bounded, read-only discovery and deterministic ranking for Standalone Nurture."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from threads_platform.application.ports.threads import DiscoveryPage, RemoteDiscoveryThread
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.nurture import NurturePresetV1
from threads_platform.standalone.nurture_store import (
    NurtureStateError,
    NurtureTargetStateV1,
    NurtureTargetV1,
    fingerprint_remote_thread,
)

_MAX_REMOTE_TEXT_CHARS = 10_000
_MAX_SOURCE_ORDER = 32
_MAX_SOURCE_CALLS = 33
_REMOTE_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_SAFE_ERROR_CODES = frozenset(
    {
        "DISCOVERY_CONTRACT_MISMATCH",
        "DISCOVERY_SOURCE_FAILED",
        "INVALID_CURSOR",
        "INVALID_LIMIT",
        "INVALID_MEDIA_ID",
        "INVALID_QUERY",
        "INVALID_SEARCH_MODE",
        "INVALID_SEARCH_TYPE",
        "INVALID_THREAD_ID",
        "INVALID_USERNAME",
        "NURTURE_STATE_INVALID",
        "NURTURE_VERSION_UNSUPPORTED",
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

# Source priority is intentionally fixed for the recruitment policy. Other signals
# are bounded separately so repeated terms, long text, or duplicate sources cannot
# outweigh the source class without limit.
_MENTIONS_SOURCE_SCORE = 40
_KEYWORD_SOURCE_SCORE = 30
_TAG_SOURCE_SCORE = 20
_WATCHED_PROFILE_SOURCE_SCORE = 10
_INCLUDE_TERM_SCORE = 2
_MAX_INCLUDE_TERM_SCORE = 6
_QUESTION_MARK_SCORE = 3
_RECENCY_24_HOURS_SCORE = 4
_RECENCY_7_DAYS_SCORE = 2
_RECENCY_30_DAYS_SCORE = 1
_NO_REPLY_SIGNAL_SCORE = 2

_RECENCY_24_HOURS = timedelta(hours=24)
_RECENCY_7_DAYS = timedelta(days=7)
_RECENCY_30_DAYS = timedelta(days=30)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# A distinct include term contributes 2 points, capped at 6 total. A question
# mark contributes 3, recency contributes at most 4, and has_replies=False adds 2.
# Null and future timestamps contribute no recency points.


class NurtureDiscoverySource(StrEnum):
    MENTIONS = "MENTIONS"
    KEYWORD = "KEYWORD"
    TAG = "TAG"
    WATCHED_PROFILE = "WATCHED_PROFILE"


_SOURCE_SCORE = {
    NurtureDiscoverySource.MENTIONS: _MENTIONS_SOURCE_SCORE,
    NurtureDiscoverySource.KEYWORD: _KEYWORD_SOURCE_SCORE,
    NurtureDiscoverySource.TAG: _TAG_SOURCE_SCORE,
    NurtureDiscoverySource.WATCHED_PROFILE: _WATCHED_PROFILE_SOURCE_SCORE,
}
_SOURCE_REASON = {
    NurtureDiscoverySource.MENTIONS: "SOURCE_MENTIONS",
    NurtureDiscoverySource.KEYWORD: "SOURCE_KEYWORD",
    NurtureDiscoverySource.TAG: "SOURCE_TAG",
    NurtureDiscoverySource.WATCHED_PROFILE: "SOURCE_WATCHED_PROFILE",
}
_REASON_ORDER = (
    "SOURCE_MENTIONS",
    "SOURCE_KEYWORD",
    "SOURCE_TAG",
    "SOURCE_WATCHED_PROFILE",
    "INCLUDE_TERM_MATCH",
    "QUESTION_MARK",
    "RECENCY_24_HOURS",
    "RECENCY_7_DAYS",
    "RECENCY_30_DAYS",
    "NO_REPLY_SIGNAL",
    "COOLDOWN_ELAPSED",
    "EXCLUDED_TERM",
    "LOCAL_PENDING",
    "LOCAL_AMBIGUOUS",
    "LOCAL_SEEN_COOLDOWN",
    "LOCAL_ACTION_COOLDOWN",
    "LOCAL_ACTION_TIME_UNAVAILABLE",
)


@dataclass(frozen=True, slots=True, repr=False)
class NurtureDiscoveryObservation:
    """One bounded source occurrence used by the pure ranking function."""

    thread: RemoteDiscoveryThread
    source_class: NurtureDiscoverySource
    source_index: int
    source_order: int

    def __repr__(self) -> str:
        return (
            "NurtureDiscoveryObservation("
            f"source_class={self.source_class.value}, source_index={self.source_index}, "
            f"source_order={self.source_order})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class NurtureCandidate:
    """Ephemeral candidate; its representation deliberately omits remote content."""

    remote_thread_id: str
    source_class: NurtureDiscoverySource
    source_index: int
    source_order: int
    username: str | None
    text: str | None
    permalink: str | None
    timestamp: datetime | None
    has_replies: bool | None
    is_quote_post: bool | None
    fingerprint: str
    include_term_match: bool
    question_signal: bool
    recency_bucket: int | None
    score: int
    reason_codes: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "NurtureCandidate("
            f"source_class={self.source_class.value}, source_order={self.source_order}, "
            f"fingerprint={self.fingerprint[:12]}…, score={self.score}, "
            f"reason_codes={self.reason_codes!r})"
        )


@dataclass(frozen=True, slots=True)
class NurtureSourceCallCounts:
    mentions: int = 0
    keyword_searches: int = 0
    tag_searches: int = 0
    profile_posts: int = 0

    def __post_init__(self) -> None:
        values = (self.mentions, self.keyword_searches, self.tag_searches, self.profile_posts)
        if any(type(value) is not int or not 0 <= value <= _MAX_SOURCE_CALLS for value in values):
            raise NurtureStateError("NURTURE_STATE_INVALID")
        if sum(values) > _MAX_SOURCE_CALLS:
            raise NurtureStateError("NURTURE_STATE_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class NurtureDiscoveryResult:
    """Counts mean raw bounded occurrences, unique identities, policy rejects, then selected."""

    source_call_counts: NurtureSourceCallCounts
    discovered_count: int
    deduped_count: int
    rejected_count: int
    selected_candidates: tuple[NurtureCandidate, ...]
    reason_codes: tuple[str, ...]

    @property
    def selected_count(self) -> int:
        return len(self.selected_candidates)

    def __repr__(self) -> str:
        return (
            "NurtureDiscoveryResult("
            f"source_call_counts={self.source_call_counts!r}, "
            f"discovered_count={self.discovered_count}, deduped_count={self.deduped_count}, "
            f"rejected_count={self.rejected_count}, selected_count={self.selected_count}, "
            f"reason_codes={self.reason_codes!r})"
        )


class NurtureDiscoveryError(Exception):
    """Safe source failure metadata without query, username, response, or exception text."""

    def __init__(
        self,
        *,
        code: str,
        stage: str,
        source_class: NurtureDiscoverySource,
        source_index: int,
    ) -> None:
        self.code = (
            code
            if type(code) is str and len(code) <= 64 and code in _SAFE_ERROR_CODES
            else "DISCOVERY_SOURCE_FAILED"
        )
        self.stage = stage
        self.source_class = source_class
        self.source_index = source_index
        super().__init__("NURTURE_DISCOVERY_FAILED")

    def __repr__(self) -> str:
        return (
            "NurtureDiscoveryError("
            f"code={self.code}, stage={self.stage}, source_class={self.source_class.value}, "
            f"source_index={self.source_index})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _SourceCall:
    source_class: NurtureDiscoverySource
    source_index: int
    source_order: int
    value: str | None = None

    def __repr__(self) -> str:
        return (
            f"_SourceCall(source_class={self.source_class.value}, "
            f"source_index={self.source_index}, source_order={self.source_order})"
        )


async def discover_nurture_candidates(
    api: LocalThreadsApiRuntime,
    account_alias: str,
    preset: NurturePresetV1,
    target_state: NurtureTargetStateV1,
    now: datetime,
) -> NurtureDiscoveryResult:
    """Collect one bounded page per planned source, then apply the pure policy."""

    validated_preset = _validated_preset(preset)
    _validated_target_snapshot(target_state, validated_preset)
    captured_now = _as_utc(now)
    source_plan = _source_plan(validated_preset)
    observations: list[NurtureDiscoveryObservation] = []
    calls = {source: 0 for source in NurtureDiscoverySource}

    for source in source_plan:
        remaining_capacity = validated_preset.max_total_discovery_candidates - len(observations)
        if remaining_capacity <= 0:
            break
        limit = min(validated_preset.per_source_page_limit, remaining_capacity)
        page: DiscoveryPage | None = None
        source_error: NurtureDiscoveryError | None = None
        try:
            page = await _call_source(api, account_alias, source, limit)
        except Exception as error:
            source_error = _source_error("source_call", source, error)
        if source_error is not None:
            raise source_error
        if page is None:
            raise _source_error("normalize", source, _SafeContractFailure())
        calls[source.source_class] += 1

        source_observations: list[NurtureDiscoveryObservation] = []
        source_error = None
        try:
            if type(page) is not DiscoveryPage or type(page.threads) is not tuple:
                raise _SafeContractFailure()
            page_prefix = page.threads[:remaining_capacity]
            for thread in page_prefix:
                observation = NurtureDiscoveryObservation(
                    thread=thread,
                    source_class=source.source_class,
                    source_index=source.source_index,
                    source_order=source.source_order,
                )
                _canonical_identity(observation)
                source_observations.append(observation)
        except Exception as error:
            source_error = _source_error("normalize", source, error)
        if source_error is not None:
            raise source_error
        observations.extend(source_observations)
        if len(observations) >= validated_preset.max_total_discovery_candidates:
            break

    call_counts = NurtureSourceCallCounts(
        mentions=calls[NurtureDiscoverySource.MENTIONS],
        keyword_searches=calls[NurtureDiscoverySource.KEYWORD],
        tag_searches=calls[NurtureDiscoverySource.TAG],
        profile_posts=calls[NurtureDiscoverySource.WATCHED_PROFILE],
    )
    return rank_nurture_discovery(
        tuple(observations),
        validated_preset,
        target_state,
        captured_now,
        source_call_counts=call_counts,
    )


def rank_nurture_discovery(
    observations: tuple[NurtureDiscoveryObservation, ...],
    preset: NurturePresetV1,
    target_state: NurtureTargetStateV1,
    now: datetime,
    *,
    source_call_counts: NurtureSourceCallCounts | None = None,
) -> NurtureDiscoveryResult:
    """Pure dedupe/ranking; the first occurrence owns provenance and candidate fields."""

    validated_preset = _validated_preset(preset)
    targets = _validated_target_snapshot(target_state, validated_preset)
    captured_now = _as_utc(now)
    if type(observations) is not tuple or len(observations) > (
        validated_preset.max_total_discovery_candidates
    ):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    if source_call_counts is None:
        call_counts = NurtureSourceCallCounts()
    elif type(source_call_counts) is NurtureSourceCallCounts:
        call_counts = source_call_counts
    else:
        raise NurtureStateError("NURTURE_STATE_INVALID")

    unique: list[tuple[str, NurtureDiscoveryObservation]] = []
    fingerprints: set[str] = set()
    for observation in observations:
        canonical_id, fingerprint = _canonical_identity(observation)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        normalized_thread = replace(observation.thread, remote_thread_id=canonical_id)
        unique.append(
            (
                fingerprint,
                replace(observation, thread=normalized_thread),
            )
        )

    targets_by_fingerprint = {target.fingerprint: target for target in targets}
    rejected_count = 0
    eligible: list[NurtureCandidate] = []
    all_reasons: set[str] = set()

    for fingerprint, observation in unique:
        thread = observation.thread
        source_class = observation.source_class
        source_reason = _SOURCE_REASON[source_class]
        reasons = [source_reason]
        all_reasons.add(source_reason)

        normalized_text = _normalize_text(thread.text)
        excluded = normalized_text is not None and _matches_any(
            normalized_text, validated_preset.exclude_terms
        )
        if excluded:
            rejected_count += 1
            all_reasons.add("EXCLUDED_TERM")
            continue

        target = targets_by_fingerprint.get(fingerprint)
        eligibility_reason = _target_suppression_reason(
            target,
            validated_preset.seen_cooldown_seconds,
            captured_now,
        )
        if eligibility_reason is not None and eligibility_reason != "COOLDOWN_ELAPSED":
            rejected_count += 1
            all_reasons.add(eligibility_reason)
            continue
        if eligibility_reason is not None:
            reasons.append(eligibility_reason)
            all_reasons.add(eligibility_reason)

        include_match = normalized_text is not None and _matches_any(
            normalized_text, validated_preset.include_terms
        )
        include_points = (
            _include_score(normalized_text, validated_preset.include_terms)
            if normalized_text is not None
            else 0
        )
        if include_match:
            reasons.append("INCLUDE_TERM_MATCH")
            all_reasons.add("INCLUDE_TERM_MATCH")

        question_signal = normalized_text is not None and "?" in normalized_text
        if question_signal:
            reasons.append("QUESTION_MARK")
            all_reasons.add("QUESTION_MARK")

        recency_bucket = _recency_bucket(thread.timestamp, captured_now)
        recency_score, recency_reason = _recency_signal(recency_bucket)
        if recency_reason is not None:
            reasons.append(recency_reason)
            all_reasons.add(recency_reason)

        no_reply_signal = thread.has_replies is False
        if no_reply_signal:
            reasons.append("NO_REPLY_SIGNAL")
            all_reasons.add("NO_REPLY_SIGNAL")

        score = (
            _SOURCE_SCORE[source_class]
            + include_points
            + (_QUESTION_MARK_SCORE if question_signal else 0)
            + recency_score
            + (_NO_REPLY_SIGNAL_SCORE if no_reply_signal else 0)
        )
        eligible.append(
            NurtureCandidate(
                remote_thread_id=thread.remote_thread_id,
                source_class=source_class,
                source_index=observation.source_index,
                source_order=observation.source_order,
                username=thread.username,
                text=thread.text,
                permalink=thread.permalink,
                timestamp=(None if thread.timestamp is None else thread.timestamp.astimezone(UTC)),
                has_replies=thread.has_replies,
                is_quote_post=thread.is_quote_post,
                fingerprint=fingerprint,
                include_term_match=include_match,
                question_signal=question_signal,
                recency_bucket=recency_bucket,
                score=score,
                reason_codes=tuple(reasons),
            )
        )

    eligible.sort(key=lambda candidate: _candidate_sort_key(candidate, captured_now))
    selected = tuple(eligible[: validated_preset.max_selected_candidates])
    bounded_reasons = tuple(code for code in _REASON_ORDER if code in all_reasons)
    return NurtureDiscoveryResult(
        source_call_counts=call_counts,
        discovered_count=len(observations),
        deduped_count=len(unique),
        rejected_count=rejected_count,
        selected_candidates=selected,
        reason_codes=bounded_reasons,
    )


async def _call_source(
    api: LocalThreadsApiRuntime,
    account_alias: str,
    source: _SourceCall,
    limit: int,
) -> DiscoveryPage:
    if source.source_class is NurtureDiscoverySource.MENTIONS:
        return await api.mentions(account_alias, after=None, limit=limit)
    if source.source_class is NurtureDiscoverySource.KEYWORD:
        assert source.value is not None
        return await api.search(
            account_alias,
            source.value,
            search_mode=DiscoverySearchMode.KEYWORD,
            search_type=DiscoverySearchType.RECENT,
            after=None,
            limit=limit,
        )
    if source.source_class is NurtureDiscoverySource.TAG:
        assert source.value is not None
        return await api.search(
            account_alias,
            source.value,
            search_mode=DiscoverySearchMode.TAG,
            search_type=DiscoverySearchType.RECENT,
            after=None,
            limit=limit,
        )
    assert source.source_class is NurtureDiscoverySource.WATCHED_PROFILE
    assert source.value is not None
    return await api.profile_posts(account_alias, source.value, after=None, limit=limit)


def _source_plan(preset: NurturePresetV1) -> tuple[_SourceCall, ...]:
    plan: list[_SourceCall] = []

    def append(source_class: NurtureDiscoverySource, source_index: int, value: str | None) -> None:
        plan.append(_SourceCall(source_class, source_index, len(plan), value))

    append(NurtureDiscoverySource.MENTIONS, 0, None)
    for index, query in enumerate(preset.keyword_queries):
        append(NurtureDiscoverySource.KEYWORD, index, query)
    for index, query in enumerate(preset.tag_queries):
        append(NurtureDiscoverySource.TAG, index, query)
    for index, username in enumerate(preset.watched_public_usernames):
        append(NurtureDiscoverySource.WATCHED_PROFILE, index, username)
    return tuple(plan)


def _canonical_identity(
    observation: NurtureDiscoveryObservation,
) -> tuple[str, str]:
    if (
        type(observation) is not NurtureDiscoveryObservation
        or type(observation.source_class) is not NurtureDiscoverySource
        or type(observation.source_index) is not int
        or not 0 <= observation.source_index < 16
        or type(observation.source_order) is not int
        or not 0 <= observation.source_order <= _MAX_SOURCE_ORDER
        or type(observation.thread) is not RemoteDiscoveryThread
    ):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    thread = observation.thread
    if (
        type(thread.remote_thread_id) is not str
        or len(thread.remote_thread_id) > 255
        or type(thread.username) not in {str, type(None)}
        or (thread.username is not None and len(thread.username) > 255)
        or type(thread.text) not in {str, type(None)}
        or (thread.text is not None and len(thread.text) > _MAX_REMOTE_TEXT_CHARS)
        or type(thread.permalink) not in {str, type(None)}
        or (thread.permalink is not None and len(thread.permalink) > 2048)
        or type(thread.timestamp) not in {datetime, type(None)}
        or type(thread.has_replies) not in {bool, type(None)}
        or type(thread.is_quote_post) not in {bool, type(None)}
    ):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    if thread.timestamp is not None:
        _as_utc(thread.timestamp)
    canonical_id = unicodedata.normalize("NFC", thread.remote_thread_id.strip())
    if _REMOTE_ID.fullmatch(canonical_id) is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    return canonical_id, fingerprint_remote_thread(canonical_id)


def _validated_preset(preset: NurturePresetV1) -> NurturePresetV1:
    if type(preset) is not NurturePresetV1:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    try:
        return replace(preset)
    except AttributeError, TypeError, ValueError:
        raise NurtureStateError("NURTURE_STATE_INVALID") from None


def _validated_target_snapshot(
    target_state: NurtureTargetStateV1,
    preset: NurturePresetV1,
) -> tuple[NurtureTargetV1, ...]:
    if (
        type(target_state) is not NurtureTargetStateV1
        or type(target_state.version) is not int
        or target_state.version != 1
        or type(target_state.account_id) is not UUID
        or target_state.account_id.version != 4
        or type(target_state.preset_id) is not str
        or target_state.preset_id != preset.id
        or type(target_state.targets) is not tuple
    ):
        raise NurtureStateError("NURTURE_STATE_INVALID")
    targets: list[NurtureTargetV1] = []
    fingerprints: set[str] = set()
    for target in target_state.targets:
        if type(target) is not NurtureTargetV1:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        try:
            validated = replace(target)
        except AttributeError, TypeError, ValueError:
            raise NurtureStateError("NURTURE_STATE_INVALID") from None
        if validated.fingerprint in fingerprints:
            raise NurtureStateError("NURTURE_STATE_INVALID")
        fingerprints.add(validated.fingerprint)
        targets.append(validated)
    return tuple(targets)


def _target_suppression_reason(
    target: NurtureTargetV1 | None,
    cooldown_seconds: int,
    now: datetime,
) -> str | None:
    """PENDING/AMBIGUOUS never re-enter; otherwise elapsed time must be >= cooldown."""
    if target is None:
        return None
    if target.action_state == "PENDING":
        return "LOCAL_PENDING"
    if target.action_state == "AMBIGUOUS":
        return "LOCAL_AMBIGUOUS"
    if target.action_state == "NONE":
        event_time = target.last_seen_at
        active_reason = "LOCAL_SEEN_COOLDOWN"
    elif target.action_state == "CONFIRMED":
        event_time = target.last_action_at
        active_reason = "LOCAL_ACTION_COOLDOWN"
        if event_time is None:
            return "LOCAL_ACTION_TIME_UNAVAILABLE"
    else:
        return "LOCAL_ACTION_TIME_UNAVAILABLE"
    try:
        elapsed = now - _as_utc(event_time)
    except AttributeError, TypeError, ValueError, OverflowError:
        return "LOCAL_ACTION_TIME_UNAVAILABLE"
    if elapsed < timedelta(seconds=cooldown_seconds):
        return active_reason
    return "COOLDOWN_ELAPSED"


def _normalize_text(text: str | None) -> str | None:
    if text is None:
        return None
    bounded_text = text[:_MAX_REMOTE_TEXT_CHARS]
    try:
        return unicodedata.normalize("NFKC", bounded_text).casefold()
    except UnicodeError:
        return None


def _matches_any(normalized_text: str, terms: tuple[str, ...]) -> bool:
    return any(
        normalized_term in normalized_text
        for term in terms
        if (normalized_term := _normalize_text(term)) is not None and normalized_term
    )


def _include_score(normalized_text: str, terms: tuple[str, ...]) -> int:
    matched_terms: set[str] = set()
    for term in terms:
        normalized_term = _normalize_text(term)
        if normalized_term and normalized_term not in matched_terms:
            if normalized_term in normalized_text:
                matched_terms.add(normalized_term)
    return min(len(matched_terms) * _INCLUDE_TERM_SCORE, _MAX_INCLUDE_TERM_SCORE)


def _recency_bucket(timestamp: datetime | None, now: datetime) -> int | None:
    """Return 3 <=24h, 2 <=7d, 1 <=30d, 0 older; null/future returns None."""
    if timestamp is None:
        return None
    captured_timestamp = _as_utc(timestamp)
    if captured_timestamp > now:
        return None
    age = now - captured_timestamp
    if age <= _RECENCY_24_HOURS:
        return 3
    if age <= _RECENCY_7_DAYS:
        return 2
    if age <= _RECENCY_30_DAYS:
        return 1
    return 0


def _recency_signal(bucket: int | None) -> tuple[int, str | None]:
    if bucket == 3:
        return _RECENCY_24_HOURS_SCORE, "RECENCY_24_HOURS"
    if bucket == 2:
        return _RECENCY_7_DAYS_SCORE, "RECENCY_7_DAYS"
    if bucket == 1:
        return _RECENCY_30_DAYS_SCORE, "RECENCY_30_DAYS"
    return 0, None


def _candidate_sort_key(
    candidate: NurtureCandidate,
    now: datetime,
) -> tuple[int, int, int, int, str]:
    """Sort score desc, source order asc, known-newest time (null/future last), fingerprint asc."""
    timestamp = candidate.timestamp
    if timestamp is not None:
        timestamp = _as_utc(timestamp)
    usable_timestamp = timestamp if timestamp is not None and timestamp <= now else None
    if usable_timestamp is None:
        timestamp_null_order = 1
        timestamp_order = 0
    else:
        timestamp_null_order = 0
        delta = usable_timestamp - _EPOCH
        timestamp_order = -(
            delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        )
    return (
        -candidate.score,
        candidate.source_order,
        timestamp_null_order,
        timestamp_order,
        candidate.fingerprint,
    )


def _source_error(
    stage: str,
    source: _SourceCall,
    error: Exception,
) -> NurtureDiscoveryError:
    try:
        error_code = getattr(error, "code", None)
    except Exception:
        error_code = None
    if type(error_code) is not str or len(error_code) > 64 or error_code not in _SAFE_ERROR_CODES:
        error_code = "DISCOVERY_SOURCE_FAILED"
    return NurtureDiscoveryError(
        code=error_code,
        stage=stage,
        source_class=source.source_class,
        source_index=source.source_index,
    )


class _SafeContractFailure(Exception):
    code = "DISCOVERY_CONTRACT_MISMATCH"


def _as_utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise NurtureStateError("NURTURE_STATE_INVALID")
    return value.astimezone(UTC)
