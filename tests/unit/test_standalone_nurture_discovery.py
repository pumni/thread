from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

import threads_platform.standalone.nurture_discovery as nurture_discovery_module
from threads_platform.application.ports.threads import DiscoveryPage, RemoteDiscoveryThread
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.nurture import NurturePresetV1, get_nurture_preset
from threads_platform.standalone.nurture_discovery import (
    NurtureDiscoveryError,
    NurtureDiscoveryObservation,
    NurtureDiscoverySource,
    discover_nurture_candidates,
    rank_nurture_discovery,
)
from threads_platform.standalone.nurture_store import (
    NurtureActionState,
    NurtureStore,
    NurtureTargetStateV1,
    NurtureTargetV1,
    fingerprint_remote_thread,
)

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _preset(
    *,
    keyword_queries: tuple[str, ...] = ("job search",),
    tag_queries: tuple[str, ...] = (),
    watched_public_usernames: tuple[str, ...] = (),
    include_terms: tuple[str, ...] = ("career",),
    exclude_terms: tuple[str, ...] = (),
    per_source_page_limit: int = 25,
    max_total_discovery_candidates: int = 50,
    max_selected_candidates: int | None = None,
    seen_cooldown_seconds: int = 86_400,
) -> NurturePresetV1:
    selected = (
        min(10, max_total_discovery_candidates)
        if max_selected_candidates is None
        else max_selected_candidates
    )
    return replace(
        get_nurture_preset("recruitment"),
        keyword_queries=keyword_queries,
        tag_queries=tag_queries,
        watched_public_usernames=watched_public_usernames,
        include_terms=include_terms,
        exclude_terms=exclude_terms,
        per_source_page_limit=per_source_page_limit,
        max_total_discovery_candidates=max_total_discovery_candidates,
        max_selected_candidates=selected,
        max_browser_enrichments=min(5, selected),
        seen_cooldown_seconds=seen_cooldown_seconds,
    )


def _snapshot(
    preset: NurturePresetV1,
    targets: tuple[NurtureTargetV1, ...] = (),
) -> NurtureTargetStateV1:
    return NurtureTargetStateV1(1, uuid4(), preset.id, targets)


def _thread(
    remote_thread_id: str,
    *,
    username: str | None = None,
    text: str | None = "ordinary conversation",
    permalink: str | None = None,
    timestamp: datetime | None = None,
    has_replies: bool | None = None,
    is_quote_post: bool | None = None,
) -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(
        remote_thread_id,
        None,
        username,
        text,
        permalink,
        None,
        timestamp,
        is_quote_post,
        has_replies,
    )


def _page(*threads: RemoteDiscoveryThread) -> DiscoveryPage:
    return DiscoveryPage(tuple(threads), "opaque-next-cursor", True)


def _observation(
    thread: RemoteDiscoveryThread,
    source_class: NurtureDiscoverySource = NurtureDiscoverySource.MENTIONS,
    *,
    source_index: int = 0,
    source_order: int = 0,
) -> NurtureDiscoveryObservation:
    return NurtureDiscoveryObservation(thread, source_class, source_index, source_order)


def _target(
    thread_id: str,
    action_state: str,
    *,
    last_seen_at: datetime | None = None,
    last_action_at: datetime | None = None,
) -> NurtureTargetV1:
    seen_at = last_seen_at or _NOW - timedelta(days=365)
    action_at = None if action_state == "NONE" else last_action_at or seen_at
    return NurtureTargetV1(
        fingerprint_remote_thread(thread_id),
        _NOW - timedelta(days=365),
        seen_at,
        "PREVIOUSLY_OBSERVED",
        action_at,
        cast(NurtureActionState, action_state),
        uuid4(),
        uuid4() if action_state in {"CONFIRMED", "AMBIGUOUS"} else None,
    )


class _FakeApiRuntime:
    def __init__(self, outcomes: tuple[DiscoveryPage | Exception, ...]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[object, ...]] = []

    def _next_page(self) -> DiscoveryPage:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def mentions(self, account_alias: str, *, after: str | None, limit: int) -> DiscoveryPage:
        self.calls.append(("mentions", account_alias, None, None, None, after, limit))
        return self._next_page()

    async def search(
        self,
        account_alias: str,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self.calls.append(("search", account_alias, query, search_mode, search_type, after, limit))
        return self._next_page()

    async def profile_posts(
        self,
        account_alias: str,
        username: str,
        *,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        self.calls.append(("profile_posts", account_alias, username, None, None, after, limit))
        return self._next_page()


async def _discover(
    api: _FakeApiRuntime,
    preset: NurturePresetV1,
    target_state: NurtureTargetStateV1 | None = None,
    *,
    now: datetime = _NOW,
) -> nurture_discovery_module.NurtureDiscoveryResult:
    return await discover_nurture_candidates(
        cast(LocalThreadsApiRuntime, api),
        "alice",
        preset,
        _snapshot(preset) if target_state is None else target_state,
        now,
    )


def _rank(
    observations: tuple[NurtureDiscoveryObservation, ...],
    preset: NurturePresetV1 | None = None,
    target_state: NurtureTargetStateV1 | None = None,
    *,
    now: datetime = _NOW,
) -> nurture_discovery_module.NurtureDiscoveryResult:
    validated_preset = _preset() if preset is None else preset
    return rank_nurture_discovery(
        observations,
        validated_preset,
        _snapshot(validated_preset) if target_state is None else target_state,
        now,
    )


@pytest.mark.asyncio
async def test_source_order_mode_limits_and_one_page_per_source() -> None:
    preset = _preset(
        keyword_queries=("keyword-first", "keyword-second"),
        tag_queries=("tag-first", "tag-second"),
        watched_public_usernames=("watchfirst", "watchsecond"),
        per_source_page_limit=7,
    )
    api = _FakeApiRuntime(tuple(_page() for _ in range(7)))

    result = await _discover(api, preset)

    assert api.calls == [
        ("mentions", "alice", None, None, None, None, 7),
        (
            "search",
            "alice",
            "keyword-first",
            DiscoverySearchMode.KEYWORD,
            DiscoverySearchType.RECENT,
            None,
            7,
        ),
        (
            "search",
            "alice",
            "keyword-second",
            DiscoverySearchMode.KEYWORD,
            DiscoverySearchType.RECENT,
            None,
            7,
        ),
        (
            "search",
            "alice",
            "tag-first",
            DiscoverySearchMode.TAG,
            DiscoverySearchType.RECENT,
            None,
            7,
        ),
        (
            "search",
            "alice",
            "tag-second",
            DiscoverySearchMode.TAG,
            DiscoverySearchType.RECENT,
            None,
            7,
        ),
        ("profile_posts", "alice", "watchfirst", None, None, None, 7),
        ("profile_posts", "alice", "watchsecond", None, None, None, 7),
    ]
    assert result.source_call_counts.mentions == 1
    assert result.source_call_counts.keyword_searches == 2
    assert result.source_call_counts.tag_searches == 2
    assert result.source_call_counts.profile_posts == 2
    assert result.discovered_count == result.deduped_count == result.rejected_count == 0
    assert result.selected_candidates == ()


@pytest.mark.asyncio
async def test_remaining_capacity_limits_page_and_stops_later_sources() -> None:
    preset = _preset(
        keyword_queries=("keyword-first", "keyword-second"),
        tag_queries=("tag-later",),
        watched_public_usernames=("watchlater",),
        per_source_page_limit=50,
        max_total_discovery_candidates=3,
    )
    api = _FakeApiRuntime(
        (
            _page(_thread("mention-a"), _thread("mention-b")),
            _page(_thread("keyword-prefix"), _thread("discard-b"), _thread("discard-c")),
        )
    )

    result = await _discover(api, preset)

    assert [call[0] for call in api.calls] == ["mentions", "search"]
    assert [call[-1] for call in api.calls] == [3, 1]
    assert api.calls[1][2] == "keyword-first"
    assert api.calls[1][5] is None
    assert result.discovered_count == 3
    assert result.deduped_count == 3
    assert {candidate.remote_thread_id for candidate in result.selected_candidates} == {
        "mention-a",
        "mention-b",
        "keyword-prefix",
    }
    assert result.source_call_counts.mentions == 1
    assert result.source_call_counts.keyword_searches == 1
    assert result.source_call_counts.tag_searches == 0
    assert result.source_call_counts.profile_posts == 0


@pytest.mark.asyncio
async def test_duplicate_identity_keeps_earliest_provenance_without_source_boost() -> None:
    preset = _preset(keyword_queries=("job search",), tag_queries=("career",))
    duplicate_id = " thread:duplicate "
    api = _FakeApiRuntime(
        (
            _page(_thread(duplicate_id, text="career")),
            _page(_thread("thread:duplicate", text="career career career")),
            _page(_thread("thread:duplicate", text="career")),
        )
    )

    result = await _discover(api, preset)
    single = _rank((_observation(_thread(duplicate_id, text="career")),), preset)

    assert result.discovered_count == 3
    assert result.deduped_count == 1
    assert result.rejected_count == 0
    assert result.selected_count == 1
    candidate = result.selected_candidates[0]
    assert candidate.remote_thread_id == "thread:duplicate"
    assert candidate.source_class is NurtureDiscoverySource.MENTIONS
    assert candidate.source_order == 0
    assert candidate.score == single.selected_candidates[0].score


def test_pending_and_ambiguous_are_suppressed_and_cooldowns_are_explicit() -> None:
    preset = _preset(seen_cooldown_seconds=100)
    targets = (
        _target("pending", "PENDING"),
        _target("ambiguous", "AMBIGUOUS"),
        _target("seen-recent", "NONE", last_seen_at=_NOW - timedelta(seconds=99)),
        _target(
            "confirmed-recent",
            "CONFIRMED",
            last_action_at=_NOW - timedelta(seconds=99),
        ),
        _target("seen-expired", "NONE", last_seen_at=_NOW - timedelta(seconds=100)),
        _target(
            "confirmed-expired",
            "CONFIRMED",
            last_action_at=_NOW - timedelta(seconds=100),
        ),
    )
    snapshot = _snapshot(preset, targets)
    before = snapshot
    observations = tuple(
        _observation(_thread(thread_id))
        for thread_id in (
            "pending",
            "ambiguous",
            "seen-recent",
            "confirmed-recent",
            "seen-expired",
            "confirmed-expired",
        )
    )

    result = _rank(observations, preset, snapshot)

    assert {candidate.remote_thread_id for candidate in result.selected_candidates} == {
        "seen-expired",
        "confirmed-expired",
    }
    assert result.deduped_count == 6
    assert result.rejected_count == 4
    assert {"LOCAL_PENDING", "LOCAL_AMBIGUOUS", "LOCAL_SEEN_COOLDOWN"} <= set(result.reason_codes)
    assert "LOCAL_ACTION_COOLDOWN" in result.reason_codes
    assert snapshot == before


def test_future_confirmed_action_time_is_suppressed_conservatively() -> None:
    preset = _preset(seen_cooldown_seconds=0)
    future_action = _target(
        "future-confirmed",
        "CONFIRMED",
        last_action_at=_NOW + timedelta(days=1),
    )
    result = _rank(
        (_observation(_thread("future-confirmed")),),
        preset,
        _snapshot(preset, (future_action,)),
    )

    assert result.rejected_count == 1
    assert result.selected_candidates == ()
    assert "LOCAL_ACTION_COOLDOWN" in result.reason_codes


def test_exclude_term_rejects_and_reason_does_not_echo_matched_text() -> None:
    preset = _preset(exclude_terms=("crypto",))
    result = _rank((_observation(_thread("excluded", text="CRYPTO opportunity")),), preset)

    assert result.discovered_count == result.deduped_count == 1
    assert result.rejected_count == 1
    assert result.selected_candidates == ()
    assert result.reason_codes == ("SOURCE_MENTIONS", "EXCLUDED_TERM")
    assert "CRYPTO" not in repr(result)


def test_include_matching_is_normalized_distinct_and_bounded() -> None:
    preset = _preset(include_terms=("career", "CAFE", "job", "career"))
    text = "ＣＡＦＥ job career career career " * 100
    result = _rank((_observation(_thread("include", text=text)),), preset)

    candidate = result.selected_candidates[0]
    assert candidate.include_term_match is True
    assert candidate.score == 46
    assert candidate.reason_codes == ("SOURCE_MENTIONS", "INCLUDE_TERM_MATCH")
    assert "career" not in repr(candidate)
    assert "ＣＡＦＥ" not in repr(candidate)


def test_question_and_reply_presence_are_signals_not_facts() -> None:
    preset = _preset(include_terms=())
    observations = (
        _observation(_thread("question", text="Tuyển dụng？", has_replies=True)),
        _observation(_thread("no-reply", text="ordinary", has_replies=False)),
        _observation(_thread("unknown-replies", text="ordinary", has_replies=None)),
        _observation(_thread("missing-text", text=None, username=None, timestamp=None)),
    )

    result = _rank(observations, preset)
    candidates = {candidate.remote_thread_id: candidate for candidate in result.selected_candidates}

    assert candidates["question"].question_signal is True
    assert "QUESTION_MARK" in candidates["question"].reason_codes
    assert candidates["no-reply"].has_replies is False
    assert "NO_REPLY_SIGNAL" in candidates["no-reply"].reason_codes
    assert candidates["unknown-replies"].has_replies is None
    assert "NO_REPLY_SIGNAL" not in candidates["unknown-replies"].reason_codes
    missing = candidates["missing-text"]
    assert missing.text is None
    assert missing.username is None
    assert missing.timestamp is None
    assert missing.include_term_match is False
    assert missing.question_signal is False
    assert missing.recency_bucket is None
    assert missing.score == 40
    assert "UNANSWERED_QUESTION" not in result.reason_codes


def test_recency_buckets_are_bounded_and_future_timestamps_are_neutral() -> None:
    preset = _preset(include_terms=())
    timestamps = (
        ("recent", _NOW - timedelta(hours=12), 3, 44),
        ("week", _NOW - timedelta(days=2), 2, 42),
        ("month", _NOW - timedelta(days=10), 1, 41),
        ("old", _NOW - timedelta(days=40), 0, 40),
        ("null", None, None, 40),
        ("future", _NOW + timedelta(days=3650), None, 40),
    )
    result = _rank(
        tuple(
            _observation(_thread(thread_id, timestamp=timestamp))
            for thread_id, timestamp, _, _ in timestamps
        ),
        preset,
    )
    candidates = {candidate.remote_thread_id: candidate for candidate in result.selected_candidates}

    for thread_id, _, bucket, score in timestamps:
        assert candidates[thread_id].recency_bucket == bucket
        assert candidates[thread_id].score == score
    assert candidates["future"].recency_bucket == candidates["null"].recency_bucket is None


def test_equal_scores_have_repeatable_source_time_and_fingerprint_order() -> None:
    preset = _preset(include_terms=(), max_selected_candidates=2)
    timestamp = _NOW - timedelta(days=10)
    observations = tuple(
        _observation(_thread(thread_id, timestamp=timestamp))
        for thread_id in ("tie-c", "tie-a", "tie-b")
    )

    results = tuple(_rank(observations, preset) for _ in range(10))
    ordered = tuple(candidate.fingerprint for candidate in results[0].selected_candidates)
    expected = tuple(
        sorted(fingerprint_remote_thread(thread_id) for thread_id in ("tie-c", "tie-a", "tie-b"))[
            :2
        ]
    )

    assert ordered == expected
    assert results[0].selected_count == 2
    assert all(
        tuple(candidate.fingerprint for candidate in result.selected_candidates) == ordered
        for result in results
    )
    assert all(result == results[0] for result in results[1:])


@pytest.mark.asyncio
async def test_source_failure_stops_and_error_metadata_is_bounded() -> None:
    query_sentinel = "private-query-sentinel"
    username_sentinel = "privatewatcher"
    preset = _preset(
        keyword_queries=(query_sentinel,),
        tag_queries=("later-tag",),
        watched_public_usernames=(username_sentinel,),
    )

    class _Failure(Exception):
        code = "THREADS_TRANSPORT_FAILURE"

    api = _FakeApiRuntime(
        (
            _page(),
            _Failure("token-sentinel credential-sentinel response-sentinel"),
        )
    )

    with pytest.raises(NurtureDiscoveryError) as caught:
        await _discover(api, preset)

    error = caught.value
    assert error.code == "THREADS_TRANSPORT_FAILURE"
    assert error.stage == "source_call"
    assert error.source_class is NurtureDiscoverySource.KEYWORD
    assert error.source_index == 0
    assert error.__context__ is None
    assert [call[0] for call in api.calls] == ["mentions", "search"]
    rendered = f"{error!s} {error!r} {vars(error)!r}"
    for sentinel in (
        query_sentinel,
        username_sentinel,
        "token-sentinel",
        "credential-sentinel",
        "response-sentinel",
    ):
        assert sentinel not in rendered


@pytest.mark.asyncio
async def test_unrecognized_error_code_is_replaced_with_safe_code() -> None:
    secret_code_sentinel = "THREADS_ACCESS_TOKEN_SENTINEL"

    class _Failure(Exception):
        code = secret_code_sentinel

    preset = _preset()
    api = _FakeApiRuntime((_Failure(secret_code_sentinel),))

    with pytest.raises(NurtureDiscoveryError) as caught:
        await _discover(api, preset)

    assert caught.value.code == "DISCOVERY_SOURCE_FAILED"
    assert secret_code_sentinel not in str(caught.value)
    assert secret_code_sentinel not in repr(caught.value)


@pytest.mark.asyncio
async def test_candidate_repr_and_durable_receipt_never_contain_remote_text(
    tmp_path: Path,
) -> None:
    raw_text_sentinel = "raw-remote-post-text-sentinel"
    preset = _preset(keyword_queries=("job search",), max_total_discovery_candidates=3)
    store = NurtureStore(tmp_path)
    account_id = uuid4()
    api = _FakeApiRuntime(
        (
            _page(_thread("text-sentinel-target", text=raw_text_sentinel)),
            _page(),
        )
    )

    with store.acquire_account_lock(account_id) as owner:
        run = owner.start_run(preset, now=_NOW)
        target_state = store.load_target_state(owner, preset)
        result = await _discover(api, preset, target_state)
        run.finish("SUCCESS", now=_NOW + timedelta(seconds=1))

    candidate = result.selected_candidates[0]
    assert candidate.text == raw_text_sentinel
    assert raw_text_sentinel not in repr(candidate)
    assert raw_text_sentinel not in repr(result)
    persisted_json = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json")
    )
    assert raw_text_sentinel not in persisted_json


def test_policy_module_has_no_randomness_sleep_or_clock_reads() -> None:
    module_path = Path(inspect.getfile(nurture_discovery_module))
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    called_attributes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module.split(".", maxsplit=1)[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                called_attributes.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                called_attributes.add(node.func.id)

    assert "random" not in imported_modules
    assert "time" not in imported_modules
    assert "sleep" not in called_attributes
    assert "now" not in called_attributes
