from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from threads_platform.application.browser_capabilities import (
    BrowserFeedItemResultV1,
    BrowserFeedResultV1,
    BrowserTargetOpenResultV1,
)
from threads_platform.application.ports.threads import (
    DiscoveryPage,
    PublishingQuota,
    RemoteMedia,
    RemotePublicProfile,
    RemoteReply,
    ReplyPage,
    ThreadsAPIError,
)
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.mutations import (
    LocalThreadsMutationRuntime,
    PublishedTextResult,
    StandaloneMutationError,
)
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError
from threads_platform.standalone.workflows import (
    LocalWorkflowRuntime,
    PostTextStep,
    QuotaStep,
    RepliesStep,
    StandaloneWorkflowError,
    load_workflow,
)


def _write_document(path: Path, steps: object, *, account: object = "alice") -> Path:
    path.write_text(
        json.dumps({"version": 1, "account": account, "steps": steps}),
        encoding="utf-8",
    )
    return path


def _document(steps: list[object], *, account: object = "alice") -> dict[str, object]:
    return {"version": 1, "account": account, "steps": steps}


def _load_document(tmp_path: Path, document: object) -> None:
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    load_workflow(path)


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_missing_or_non_file_workflow_is_unavailable_without_path_echo(
    tmp_path: Path, path_kind: str
) -> None:
    path = tmp_path / "private-workflow-name.json"
    if path_kind == "directory":
        path.mkdir()

    with pytest.raises(StandaloneWorkflowError) as caught:
        load_workflow(path)

    assert caught.value.code == "WORKFLOW_UNAVAILABLE"
    assert str(caught.value) == "WORKFLOW_UNAVAILABLE"
    assert str(path) not in str(caught.value)


def test_workflow_size_limit_is_inclusive_at_65536_bytes(tmp_path: Path) -> None:
    path = _write_document(tmp_path / "workflow.json", [{"action": "quota"}])
    contents = path.read_bytes()
    path.write_bytes(contents + b" " * (65_536 - len(contents)))

    plan = load_workflow(path)

    assert len(path.read_bytes()) == 65_536
    assert plan.steps == (QuotaStep(),)

    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(StandaloneWorkflowError, match="^INVALID_WORKFLOW$"):
        load_workflow(path)


@pytest.mark.parametrize(
    "contents",
    [b"\xff", b"{not-json}", b'{"version":1,"account":"alice","steps":[]'],
)
def test_invalid_utf8_or_json_is_rejected(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / "workflow.json"
    path.write_bytes(contents)

    with pytest.raises(StandaloneWorkflowError, match="^INVALID_WORKFLOW$"):
        load_workflow(path)


@pytest.mark.parametrize(
    "contents",
    [
        b'{"version":1,"version":1,"account":"alice","steps":[{"action":"quota"}]}',
        b'{"version":1,"account":"alice","steps":[{"action":"quota","action":"quota"}]}',
    ],
)
def test_duplicate_json_keys_are_rejected_at_every_object_level(
    tmp_path: Path, contents: bytes
) -> None:
    path = tmp_path / "workflow.json"
    path.write_bytes(contents)

    with pytest.raises(StandaloneWorkflowError, match="^INVALID_WORKFLOW$"):
        load_workflow(path)


@pytest.mark.parametrize(
    "document",
    [
        {"version": 1, "account": "alice", "steps": [{"action": "quota"}], "extra": 1},
        {"version": 1, "steps": [{"action": "quota"}]},
        {"version": True, "account": "alice", "steps": [{"action": "quota"}]},
        {"version": 1.0, "account": "alice", "steps": [{"action": "quota"}]},
        {"version": 2, "account": "alice", "steps": [{"action": "quota"}]},
        [1, "alice", [{"action": "quota"}]],
        {"version": 1, "account": "alice", "steps": []},
        {"version": 1, "account": "alice", "steps": [{"action": "quota"}] * 33},
        {"version": 1, "account": "../alice", "steps": [{"action": "quota"}]},
        {"version": 1, "account": "", "steps": [{"action": "quota"}]},
    ],
)
def test_top_level_schema_version_step_count_and_alias_are_strict(
    tmp_path: Path, document: object
) -> None:
    with pytest.raises(StandaloneWorkflowError, match="^INVALID_WORKFLOW$"):
        _load_document(tmp_path, document)


@pytest.mark.parametrize(
    "step",
    [
        {"action": "unknown"},
        {"action": "quota", "extra": 1},
        {"action": "media"},
        {"action": "media", "media_id": "media-1", "extra": 1},
        {"action": "replies"},
        {"action": "conversation", "thread_id": "thread-1", "extra": 1},
        {"action": "post_text"},
        {"action": "post_text", "text": "chosen", "extra": 1},
        {"action": "feed"},
        {"action": "feed", "limit": 3, "extra": 1},
        {"action": "feed", "limit": True},
        {"action": "feed", "limit": 0},
        {"action": "feed", "limit": 21},
        {"action": "profile"},
        {"action": "profile", "username": "alice", "extra": 1},
        {"action": "profile", "username": "bad/name"},
        {"action": "profile", "username": True},
        {"action": "thread"},
        {"action": "thread", "thread_ref": "/@alice/post/post-1", "extra": 1},
        {"action": "thread", "thread_ref": "https://www.threads.com/@alice/post/post-1"},
        {"action": "thread", "thread_ref": "/@alice/post/post-1?query=1"},
        {"action": "thread", "thread_ref": "/@alice/post/post-1#fragment"},
        {"action": "public_profile"},
        {"action": "public_profile", "username": "alice", "extra": 1},
        {"action": "public_profile", "username": "https://example.test/alice"},
        {"action": "public_profile", "username": ""},
        {"action": "public_profile", "username": "x" * 256},
        {"action": "public_profile", "username": "alice\nbob"},
        {"action": "profile_posts"},
        {"action": "profile_posts", "username": "alice", "limit": True},
        {"action": "profile_posts", "username": "alice", "limit": 0},
        {"action": "profile_posts", "username": "alice", "limit": 51},
        {"action": "profile_posts", "username": "alice", "limit": 25, "extra": 1},
        {"action": "profile_posts", "username": "alice", "limit": 25, "after": None},
        {"action": "profile_posts", "username": "https://example.test/alice", "limit": 25},
        {"action": "search"},
        {
            "action": "search",
            "query": "query",
            "mode": "keyword",
            "type": "TOP",
            "limit": 25,
        },
        {
            "action": "search",
            "query": "query",
            "mode": "KEYWORD",
            "type": "top",
            "limit": 25,
        },
        {
            "action": "search",
            "query": "www.example.test",
            "mode": "KEYWORD",
            "type": "TOP",
            "limit": 25,
        },
        {"action": "search", "query": "", "mode": "KEYWORD", "type": "TOP", "limit": 25},
        {
            "action": "search",
            "query": "x" * 256,
            "mode": "KEYWORD",
            "type": "TOP",
            "limit": 25,
        },
        {
            "action": "search",
            "query": "line\nbreak",
            "mode": "KEYWORD",
            "type": "TOP",
            "limit": 25,
        },
        {
            "action": "search",
            "query": "query",
            "mode": "KEYWORD",
            "type": "TOP",
            "limit": True,
        },
        {
            "action": "search",
            "query": "query",
            "mode": "KEYWORD",
            "type": "TOP",
            "limit": 25,
            "extra": 1,
        },
        {
            "action": "search",
            "query": "query",
            "mode": "KEYWORD",
            "type": "TOP",
            "limit": 25,
            "after": "line\nbreak",
        },
        {"action": "mentions"},
        {"action": "mentions", "limit": True},
        {"action": "mentions", "limit": 25, "extra": 1},
        {"action": "mentions", "limit": 25, "after": None},
        {"action": "mentions", "limit": 0},
        {"action": "mentions", "limit": 51},
        "quota",
    ],
)
def test_action_schemas_reject_unknown_missing_and_wrong_types(
    tmp_path: Path, step: object
) -> None:
    with pytest.raises(StandaloneWorkflowError, match="^INVALID_WORKFLOW$"):
        _load_document(tmp_path, _document([step]))


@pytest.mark.parametrize(
    "step",
    [
        {"action": "media", "media_id": ""},
        {"action": "media", "media_id": "x" * 256},
        {"action": "media", "media_id": "media/id"},
        {"action": "replies", "thread_id": ""},
        {"action": "conversation", "thread_id": "x" * 256},
        {"action": "replies", "thread_id": "thread id"},
    ],
)
def test_media_and_thread_ids_use_bounded_opaque_id_contract(tmp_path: Path, step: object) -> None:
    with pytest.raises(StandaloneWorkflowError, match="^INVALID_WORKFLOW$"):
        _load_document(tmp_path, _document([step]))


@pytest.mark.parametrize(
    "after",
    [None, 1, "", " \t", "x" * 4097, "line\nbreak", "line\rbreak"],
)
def test_cursor_contract_rejects_wrong_type_blank_crlf_and_overlong_values(
    tmp_path: Path, after: object
) -> None:
    for step in (
        {"action": "replies", "thread_id": "thread-1", "after": after},
        {"action": "profile_posts", "username": "alice", "limit": 25, "after": after},
        {
            "action": "search",
            "query": "example",
            "mode": "KEYWORD",
            "type": "TOP",
            "limit": 25,
            "after": after,
        },
        {"action": "mentions", "limit": 25, "after": after},
    ):
        with pytest.raises(StandaloneWorkflowError, match="^INVALID_WORKFLOW$"):
            _load_document(tmp_path, _document([step]))


def test_valid_cursor_and_text_are_preserved_without_normalization(tmp_path: Path) -> None:
    cursor = " opaque cursor "
    text = "  chosen\n text  "
    path = _write_document(
        tmp_path / "workflow.json",
        [
            {"action": "replies", "thread_id": "thread-1", "after": cursor},
            {"action": "post_text", "text": text},
        ],
    )

    plan = load_workflow(path)

    assert plan.account == "alice"
    assert plan.steps == (RepliesStep("thread-1", cursor), PostTextStep(text))
    assert text not in repr(plan)


@pytest.mark.parametrize("text", [None, 0, "", " \t", "x" * 501])
def test_post_text_validation_is_bounded_and_does_not_echo_text(
    tmp_path: Path, text: object
) -> None:
    with pytest.raises(StandaloneWorkflowError) as caught:
        _load_document(tmp_path, _document([{"action": "post_text", "text": text}]))

    assert caught.value.code == "INVALID_WORKFLOW"
    assert str(caught.value) == "INVALID_WORKFLOW"
    if isinstance(text, str) and text:
        assert text not in str(caught.value)


@pytest.mark.parametrize(
    "steps",
    [
        [{"action": "post_text", "text": "first"}, {"action": "quota"}],
        [
            {"action": "post_text", "text": "first"},
            {"action": "post_text", "text": "second"},
        ],
    ],
)
def test_post_text_must_be_the_only_final_mutation(tmp_path: Path, steps: list[object]) -> None:
    with pytest.raises(StandaloneWorkflowError, match="^INVALID_WORKFLOW$"):
        _load_document(tmp_path, _document(steps))


class _FakeApiRuntime:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events
        self.failure_action: str | None = None
        self.failure: BaseException | None = None

    def _record(self, action: str, *arguments: object) -> None:
        self.events.append((action, *arguments))
        if action == self.failure_action and self.failure is not None:
            raise self.failure

    async def quota(self, alias: str) -> PublishingQuota:
        self._record("quota", alias)
        return PublishingQuota(usage=2, total=100, reply_usage=1, reply_total=20)

    async def media(self, alias: str, media_id: str) -> RemoteMedia:
        self._record("media", alias, media_id)
        return RemoteMedia(media_id, "media text", "https://www.threads.com/t/1", "timestamp")

    async def replies(self, alias: str, thread_id: str, *, after: str | None = None) -> ReplyPage:
        self._record("replies", alias, thread_id, after)
        return ReplyPage((RemoteReply("reply-1", "reply", None, "root-1", None),), None, False)

    async def conversation(
        self, alias: str, thread_id: str, *, after: str | None = None
    ) -> ReplyPage:
        self._record("conversation", alias, thread_id, after)
        return ReplyPage((), "cursor-2", True)

    async def public_profile(self, alias: str, username: str) -> RemotePublicProfile:
        self._record("public_profile", alias, username)
        return RemotePublicProfile("author-1", username, "Alice", None, None)

    async def profile_posts(
        self, alias: str, username: str, *, after: str | None = None, limit: int = 25
    ) -> DiscoveryPage:
        self._record("profile_posts", alias, username, after, limit)
        return DiscoveryPage((), None, False)

    async def search(
        self,
        alias: str,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None = None,
        limit: int = 25,
    ) -> DiscoveryPage:
        self._record("search", alias, query, search_mode, search_type, after, limit)
        return DiscoveryPage((), "search-cursor", True)

    async def mentions(
        self, alias: str, *, after: str | None = None, limit: int = 25
    ) -> DiscoveryPage:
        self._record("mentions", alias, after, limit)
        return DiscoveryPage((), None, False)


class _FakeMutationRuntime:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events
        self.failure: BaseException | None = None

    async def publish_text(self, alias: str, text: str) -> PublishedTextResult:
        self.events.append(("post_text", alias, text))
        if self.failure is not None:
            raise self.failure
        return PublishedTextResult(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), "media-published")


class _FakeBrowserRuntime:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events
        self.failure_action: str | None = None
        self.failure: BaseException | None = None

    def _record(self, action: str, *arguments: object) -> None:
        self.events.append((action, *arguments))
        if action == self.failure_action and self.failure is not None:
            raise self.failure

    async def browse_feed(self, alias: str, limit: int) -> BrowserFeedResultV1:
        self._record("feed", alias, limit)
        return BrowserFeedResultV1(
            observations=(
                BrowserFeedItemResultV1(
                    thread_ref="https://www.threads.com/@alice/post/post-1",
                    author_username="alice",
                    position=0,
                ),
            ),
            truncated=True,
        )

    async def open_profile(self, alias: str, username: str) -> BrowserTargetOpenResultV1:
        self._record("profile", alias, username)
        return BrowserTargetOpenResultV1(target_kind="PROFILE", target_ref=f"/@{username}")

    async def open_thread(self, alias: str, thread_ref: str) -> BrowserTargetOpenResultV1:
        self._record("thread", alias, thread_ref)
        return BrowserTargetOpenResultV1(target_kind="THREAD", target_ref=thread_ref)


def _runtime(
    api: _FakeApiRuntime,
    mutation: _FakeMutationRuntime,
    browser: _FakeBrowserRuntime | None = None,
) -> LocalWorkflowRuntime:
    return LocalWorkflowRuntime(
        cast(LocalThreadsApiRuntime, api),
        cast(LocalThreadsMutationRuntime, mutation),
        cast(LocalRuntime, browser or _FakeBrowserRuntime(api.events)),
    )


@pytest.mark.asyncio
async def test_invalid_later_step_prevents_all_api_browser_and_mutation_calls(
    tmp_path: Path,
) -> None:
    path = _write_document(
        tmp_path / "workflow.json",
        [
            {"action": "public_profile", "username": "alice"},
            {"action": "feed", "limit": 3},
            {"action": "thread", "thread_ref": "INVALID"},
        ],
    )
    events: list[tuple[object, ...]] = []
    runtime = _runtime(_FakeApiRuntime(events), _FakeMutationRuntime(events))

    with pytest.raises(StandaloneWorkflowError, match="^INVALID_WORKFLOW$"):
        plan = load_workflow(path)
        await runtime.run(plan)

    assert events == []


@pytest.mark.asyncio
async def test_browser_and_api_reads_dispatch_in_document_order_for_one_account(
    tmp_path: Path,
) -> None:
    path = _write_document(
        tmp_path / "workflow.json",
        [
            {"action": "public_profile", "username": " alice "},
            {
                "action": "search",
                "query": " example ",
                "mode": "KEYWORD",
                "type": "TOP",
                "limit": 25,
                "after": "search-cursor-in",
            },
            {"action": "feed", "limit": 3},
            {"action": "profile", "username": "alice"},
            {"action": "thread", "thread_ref": "/@alice/post/post-1/"},
            {"action": "profile_posts", "username": "alice", "limit": 50},
            {"action": "mentions", "limit": 10, "after": "mentions-cursor-in"},
        ],
        account="local-alice",
    )
    events: list[tuple[object, ...]] = []
    api = _FakeApiRuntime(events)
    browser = _FakeBrowserRuntime(events)

    results = await _runtime(api, _FakeMutationRuntime(events), browser).run(load_workflow(path))

    assert events == [
        ("public_profile", "local-alice", "alice"),
        (
            "search",
            "local-alice",
            "example",
            DiscoverySearchMode.KEYWORD,
            DiscoverySearchType.TOP,
            "search-cursor-in",
            25,
        ),
        ("feed", "local-alice", 3),
        ("profile", "local-alice", "alice"),
        ("thread", "local-alice", "/@alice/post/post-1"),
        ("profile_posts", "local-alice", "alice", None, 50),
        ("mentions", "local-alice", "mentions-cursor-in", 10),
    ]
    assert [result.index for result in results] == list(range(1, 8))
    assert [result.action for result in results] == [
        "public_profile",
        "search",
        "feed",
        "profile",
        "thread",
        "profile_posts",
        "mentions",
    ]


@pytest.mark.asyncio
async def test_browser_failure_stops_later_steps_without_retry_and_preserves_index(
    tmp_path: Path,
) -> None:
    path = _write_document(
        tmp_path / "workflow.json",
        [
            {"action": "feed", "limit": 3},
            {"action": "profile", "username": "alice"},
        ],
    )
    events: list[tuple[object, ...]] = []
    browser = _FakeBrowserRuntime(events)
    browser.failure_action = "feed"
    browser.failure = StandaloneRuntimeError("REMOTE_STATE_UNCERTAIN")

    with pytest.raises(StandaloneWorkflowError) as caught:
        await _runtime(_FakeApiRuntime(events), _FakeMutationRuntime(events), browser).run(
            load_workflow(path)
        )

    assert caught.value.code == "REMOTE_STATE_UNCERTAIN"
    assert caught.value.step_index == 1
    assert caught.value.operation_id is None
    assert events == [("feed", "alice", 3)]


@pytest.mark.asyncio
async def test_api_failure_stops_before_later_browser_step(tmp_path: Path) -> None:
    path = _write_document(
        tmp_path / "workflow.json",
        [
            {"action": "profile", "username": "alice"},
            {"action": "public_profile", "username": "alice"},
            {"action": "feed", "limit": 3},
        ],
    )
    events: list[tuple[object, ...]] = []
    api = _FakeApiRuntime(events)
    api.failure_action = "public_profile"
    api.failure = ThreadsAPIError("THREADS_TRANSPORT_FAILURE")

    with pytest.raises(StandaloneWorkflowError) as caught:
        await _runtime(api, _FakeMutationRuntime(events)).run(load_workflow(path))

    assert caught.value.code == "THREADS_TRANSPORT_FAILURE"
    assert caught.value.step_index == 2
    assert events == [
        ("profile", "alice", "alice"),
        ("public_profile", "alice", "alice"),
    ]


@pytest.mark.asyncio
async def test_all_allowed_steps_dispatch_once_in_order_with_exact_arguments(
    tmp_path: Path,
) -> None:
    text = "chosen post text"
    path = _write_document(
        tmp_path / "workflow.json",
        [
            {"action": "quota"},
            {"action": "media", "media_id": "media-1"},
            {"action": "replies", "thread_id": "thread-1", "after": " cursor "},
            {"action": "conversation", "thread_id": "thread-2"},
            {"action": "post_text", "text": text},
        ],
    )
    events: list[tuple[object, ...]] = []
    api = _FakeApiRuntime(events)
    mutation = _FakeMutationRuntime(events)

    results = await _runtime(api, mutation).run(load_workflow(path))

    assert events == [
        ("quota", "alice"),
        ("media", "alice", "media-1"),
        ("replies", "alice", "thread-1", " cursor "),
        ("conversation", "alice", "thread-2", None),
        ("post_text", "alice", text),
    ]
    assert [result.index for result in results] == [1, 2, 3, 4, 5]
    assert [result.action for result in results] == [
        "quota",
        "media",
        "replies",
        "conversation",
        "post_text",
    ]
    assert isinstance(results[-1].value, PublishedTextResult)
    assert results[-1].value.media_id == "media-published"


@pytest.mark.asyncio
async def test_first_step_failure_stops_without_retry_and_preserves_code_and_index(
    tmp_path: Path,
) -> None:
    path = _write_document(
        tmp_path / "workflow.json",
        [
            {"action": "quota"},
            {"action": "media", "media_id": "media-1"},
            {"action": "conversation", "thread_id": "thread-2"},
        ],
    )
    events: list[tuple[object, ...]] = []
    api = _FakeApiRuntime(events)
    api.failure_action = "media"
    api.failure = ThreadsAPIError("THREADS_TRANSPORT_FAILURE")

    with pytest.raises(StandaloneWorkflowError) as caught:
        await _runtime(api, _FakeMutationRuntime(events)).run(load_workflow(path))

    assert caught.value.code == "THREADS_TRANSPORT_FAILURE"
    assert caught.value.step_index == 2
    assert caught.value.operation_id is None
    assert events == [("quota", "alice"), ("media", "alice", "media-1")]


@pytest.mark.asyncio
async def test_publish_ambiguity_preserves_step_index_and_operation_uuid(tmp_path: Path) -> None:
    path = _write_document(
        tmp_path / "workflow.json",
        [{"action": "quota"}, {"action": "post_text", "text": "chosen text"}],
    )
    events: list[tuple[object, ...]] = []
    operation_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    mutation = _FakeMutationRuntime(events)
    mutation.failure = StandaloneMutationError("PUBLISH_OUTCOME_AMBIGUOUS", operation_id)

    with pytest.raises(StandaloneWorkflowError) as caught:
        await _runtime(_FakeApiRuntime(events), mutation).run(load_workflow(path))

    assert caught.value.code == "PUBLISH_OUTCOME_AMBIGUOUS"
    assert caught.value.step_index == 2
    assert caught.value.operation_id == operation_id
    assert events == [("quota", "alice"), ("post_text", "alice", "chosen text")]


@pytest.mark.asyncio
async def test_unrelated_base_exception_is_not_wrapped_or_retried(tmp_path: Path) -> None:
    path = _write_document(
        tmp_path / "workflow.json",
        [{"action": "quota"}, {"action": "media", "media_id": "media-1"}],
    )
    events: list[tuple[object, ...]] = []
    api = _FakeApiRuntime(events)
    api.failure_action = "quota"
    api.failure = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        await _runtime(api, _FakeMutationRuntime(events)).run(load_workflow(path))

    assert events == [("quota", "alice")]
