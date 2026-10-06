from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event

import pytest

import threads_platform.standalone.runtime as runtime_module
from threads_platform.application.browser_capabilities import (
    BrowserFeedResultV1,
    BrowserTargetOpenResultV1,
)
from threads_platform.application.browser_read_semantics import (
    BROWSER_FEED_ANCESTOR_BOUND,
    BROWSER_FEED_ITERATION_BOUND,
    BROWSER_READ_TARGET_ANCESTOR_BOUND,
    normalize_profile_ref,
    normalize_profile_username,
    parse_thread_ref,
)
from threads_platform.application.ports.browser import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    FeedAncestorObservation,
    FeedCandidateObservation,
)
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError


class _FakeSession:
    def __init__(
        self,
        *,
        navigation_error: Exception | None = None,
        profile_error: Exception | None = None,
        thread_error: Exception | None = None,
        navigation_gate: asyncio.Event | None = None,
        navigation_started: asyncio.Event | None = None,
        feed_batches: list[tuple[FeedCandidateObservation, ...]] | None = None,
        feed_error: Exception | None = None,
    ) -> None:
        self.close_calls = 0
        self.navigate_calls = 0
        self.navigations: list[tuple[str, frozenset[str]]] = []
        self.profile_verifications: list[tuple[str, int]] = []
        self.thread_verifications: list[tuple[str, str, int]] = []
        self.feed_batches = feed_batches
        self.feed_error = feed_error
        self.collect_calls = 0
        self.scroll_calls = 0
        self.navigation_error = navigation_error
        self.profile_error = profile_error
        self.thread_error = thread_error
        self.navigation_gate = navigation_gate
        self.navigation_started = navigation_started

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        self.navigate_calls += 1
        self.navigations.append((url, allowed_origins))
        if self.navigation_started is not None:
            self.navigation_started.set()
        if self.navigation_gate is not None:
            await self.navigation_gate.wait()
        if self.navigation_error is not None:
            raise self.navigation_error

    async def verify_profile_target(self, *, target_ref: str, ancestor_bound: int) -> None:
        self.profile_verifications.append((target_ref, ancestor_bound))
        if self.profile_error is not None:
            raise self.profile_error

    async def verify_thread_target(
        self, *, target_ref: str, author_username: str, ancestor_bound: int
    ) -> None:
        self.thread_verifications.append((target_ref, author_username, ancestor_bound))
        if self.thread_error is not None:
            raise self.thread_error

    async def collect_feed_candidates(
        self, *, ancestor_bound: int
    ) -> tuple[FeedCandidateObservation, ...]:
        assert ancestor_bound == BROWSER_FEED_ANCESTOR_BOUND
        self.collect_calls += 1
        if self.feed_error is not None:
            raise self.feed_error
        batches = self.feed_batches or []
        if not batches:
            return ()
        return batches[min(self.collect_calls - 1, len(batches) - 1)]

    async def scroll_feed(self) -> None:
        self.scroll_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


class _FakeEngine:
    def __init__(
        self,
        open_error: Exception | None = None,
        session: _FakeSession | None = None,
    ) -> None:
        self.open_error = open_error
        self.requests: list[BrowserLaunchRequest] = []
        self.sessions: list[_FakeSession] = []
        self.next_session = session

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        self.requests.append(request)
        if self.open_error is not None:
            raise self.open_error
        session = self.next_session or _FakeSession()
        self.sessions.append(session)
        return session


def _lock_path(root: Path, account: LocalAccount) -> Path:
    return root / "locks" / f"{account.id}.lock"


def _assert_lock_can_be_acquired(root: Path, account: LocalAccount) -> None:
    lock = FilesystemProcessLock(_lock_path(root, account))
    lock.acquire()
    lock.release()


def _feed_candidate(post_id: str, text: str = "bounded feed excerpt") -> FeedCandidateObservation:
    permalink = f"/@alice/post/{post_id}"
    return FeedCandidateObservation(
        permalink_href=permalink,
        ancestors=(
            FeedAncestorObservation(
                hrefs=(permalink, "/@alice/"),
                text_regions=(text,),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_login_uses_headed_direct_persistent_profile_and_manual_waiter(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    store = LocalAccountStore(root)
    account = store.add("alice")
    account_file = root / "accounts" / "alice.json"
    account_before = account_file.read_bytes()
    engine = _FakeEngine()
    runtime = LocalRuntime(root, store, engine)
    prompts: list[str] = []

    await runtime.login("alice", wait_for_operator=prompts.append)
    await runtime.login("alice", wait_for_operator=prompts.append)

    expected_prompt = (
        "Browser opened for alice. Log in to Threads manually, complete any normal challenge, "
        "then press Enter here to close the browser: "
    )
    assert prompts == [expected_prompt, expected_prompt]
    assert len(engine.requests) == 2
    first_request, second_request = engine.requests
    expected_profile = root / "profiles" / str(account.id)
    assert first_request.headless is False
    assert first_request.network_route == BrowserNetworkRoute(
        BrowserNetworkProtocol.DIRECT, None, None
    )
    assert first_request.profile_directory == expected_profile
    assert second_request.profile_directory == expected_profile
    assert first_request.profile_directory.is_dir()
    assert first_request.proxy_credentials is None
    assert not hasattr(first_request, "worker_id")
    assert not hasattr(first_request, "account_id")
    assert not hasattr(first_request, "profile_ref")
    assert [session.close_calls for session in engine.sessions] == [1, 1]
    assert all(session.navigate_calls == 0 for session in engine.sessions)
    assert account_file.read_bytes() == account_before
    _assert_lock_can_be_acquired(root, account)


@pytest.mark.asyncio
async def test_login_closes_session_and_releases_lock_when_waiter_raises(
    tmp_path: Path,
) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, store, engine)

    def interrupt_wait(_: str) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await runtime.login("alice", wait_for_operator=interrupt_wait)

    assert engine.sessions[0].close_calls == 1
    _assert_lock_can_be_acquired(tmp_path, account)


@pytest.mark.asyncio
async def test_same_account_lock_rejects_second_login_without_opening_engine(
    tmp_path: Path,
) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    held_lock = FilesystemProcessLock(_lock_path(tmp_path, account))
    held_lock.acquire()
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, store, engine)

    try:
        with pytest.raises(StandaloneRuntimeError, match="^ACCOUNT_BUSY$") as error:
            await runtime.login("alice", wait_for_operator=lambda _: None)
        assert error.value.code == "ACCOUNT_BUSY"
        assert engine.requests == []
    finally:
        held_lock.release()


@pytest.mark.asyncio
async def test_different_account_lock_does_not_block_login(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    first = store.add("alice")
    second = store.add("bob")
    held_lock = FilesystemProcessLock(_lock_path(tmp_path, first))
    held_lock.acquire()
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, store, engine)

    try:
        await runtime.login("bob", wait_for_operator=lambda _: None)
        assert len(engine.requests) == 1
        assert engine.requests[0].profile_directory == (tmp_path / "profiles" / str(second.id))
        assert not hasattr(engine.requests[0], "account_id")
        assert not hasattr(engine.requests[0], "worker_id")
        assert not hasattr(engine.requests[0], "profile_ref")
        assert engine.sessions[0].close_calls == 1
    finally:
        held_lock.release()


@pytest.mark.asyncio
async def test_browser_open_error_maps_and_releases_lock(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    engine = _FakeEngine(BrowserAdapterError("BROWSER_START_FAILED"))
    runtime = LocalRuntime(tmp_path, store, engine)

    with pytest.raises(StandaloneRuntimeError, match="^BROWSER_START_FAILED$") as error:
        await runtime.login("alice", wait_for_operator=lambda _: None)

    assert error.value.code == "BROWSER_START_FAILED"
    _assert_lock_can_be_acquired(tmp_path, account)


@pytest.mark.asyncio
async def test_profile_directory_failure_maps_and_releases_lock(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles").write_text("not a directory", encoding="utf-8")
    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, store, engine)

    with pytest.raises(StandaloneRuntimeError, match="^LOCAL_PROFILE_UNAVAILABLE$") as error:
        await runtime.login("alice", wait_for_operator=lambda _: None)

    assert error.value.code == "LOCAL_PROFILE_UNAVAILABLE"
    assert engine.requests == []
    _assert_lock_can_be_acquired(tmp_path, account)


def test_shared_browser_read_target_semantics_are_bounded_and_normalized() -> None:
    assert normalize_profile_username("alice_1.test") == "/@alice_1.test"
    assert normalize_profile_ref("/@alice_1.test/") == "/@alice_1.test"
    assert parse_thread_ref("/@alice_1.test/post/a_B-9/") == (
        "/@alice_1.test/post/a_B-9",
        "alice_1.test",
    )
    assert BROWSER_READ_TARGET_ANCESTOR_BOUND == 8


@pytest.mark.parametrize(
    "username",
    (
        "@alice",
        "https://www.threads.com/@alice",
        "alice/path",
        "alice?tab=posts",
        "alice#posts",
        "",
        "x" * 31,
        "alice!",
    ),
)
@pytest.mark.asyncio
async def test_invalid_profile_target_fails_before_account_lookup_or_browser_open(
    tmp_path: Path,
    username: str,
) -> None:
    class NoLookupStore:
        def get(self, _alias: str) -> LocalAccount:
            raise AssertionError("invalid target must be rejected before account lookup")

    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, NoLookupStore(), engine)  # type: ignore[arg-type]

    with pytest.raises(StandaloneRuntimeError, match="^INVALID_PROFILE_TARGET$"):
        await runtime.open_profile("alice", username)

    assert engine.requests == []


@pytest.mark.parametrize(
    "thread_ref",
    (
        "https://www.threads.com/@alice/post/post-1",
        "//@alice/post/post-1",
        "/@alice/post/post-1?x=1",
        "/@alice/post/post-1#fragment",
        "/@alice/post/post-1/extra",
        "/@alice/post/",
        "/@alice/post/" + "x" * 121,
    ),
)
@pytest.mark.asyncio
async def test_invalid_thread_target_fails_before_account_lookup_or_browser_open(
    tmp_path: Path,
    thread_ref: str,
) -> None:
    class NoLookupStore:
        def get(self, _alias: str) -> LocalAccount:
            raise AssertionError("invalid target must be rejected before account lookup")

    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, NoLookupStore(), engine)  # type: ignore[arg-type]

    with pytest.raises(StandaloneRuntimeError, match="^INVALID_THREAD_TARGET$"):
        await runtime.open_thread("alice", thread_ref)

    assert engine.requests == []


@pytest.mark.asyncio
async def test_open_profile_uses_shared_engine_and_returns_normalized_result(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    store = LocalAccountStore(root)
    account = store.add("alice")
    profile_directory = root / "profiles" / str(account.id)
    profile_directory.mkdir(parents=True)
    session = _FakeSession()
    engine = _FakeEngine(session=session)

    result = await LocalRuntime(root, store, engine).open_profile("alice", "alice_1")

    assert isinstance(result, BrowserTargetOpenResultV1)
    assert result.model_dump(mode="json") == {
        "result_version": 1,
        "target_kind": "PROFILE",
        "target_ref": "/@alice_1",
        "recognized": True,
    }
    assert len(engine.requests) == 1
    request = engine.requests[0]
    assert request.profile_directory == profile_directory
    assert request.network_route == BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None)
    assert request.proxy_credentials is None
    assert request.headless is False
    assert session.navigations == [
        (f"{BROWSER_FEED_ORIGIN}/@alice_1", frozenset({BROWSER_FEED_ORIGIN}))
    ]
    assert session.profile_verifications == [("/@alice_1", 8)]
    assert session.thread_verifications == []
    assert session.close_calls == 1
    _assert_lock_can_be_acquired(root, account)


@pytest.mark.asyncio
async def test_open_thread_normalizes_trailing_slash_and_verifies_author_and_bound(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    store = LocalAccountStore(root)
    account = store.add("alice")
    (root / "profiles" / str(account.id)).mkdir(parents=True)
    session = _FakeSession()
    engine = _FakeEngine(session=session)

    result = await LocalRuntime(root, store, engine).open_thread("alice", "/@alice/post/post-ID_9/")

    assert isinstance(result, BrowserTargetOpenResultV1)
    assert result.model_dump(mode="json") == {
        "result_version": 1,
        "target_kind": "THREAD",
        "target_ref": "/@alice/post/post-ID_9",
        "recognized": True,
    }
    assert session.navigations == [
        (
            f"{BROWSER_FEED_ORIGIN}/@alice/post/post-ID_9",
            frozenset({BROWSER_FEED_ORIGIN}),
        )
    ]
    assert session.thread_verifications == [("/@alice/post/post-ID_9", "alice", 8)]
    assert session.profile_verifications == []
    assert session.close_calls == 1
    _assert_lock_can_be_acquired(root, account)


@pytest.mark.parametrize("max_items", (0, 21, True, "3"))
@pytest.mark.asyncio
async def test_invalid_feed_limit_is_rejected_before_account_lookup_or_browser_open(
    tmp_path: Path,
    max_items: object,
) -> None:
    class NoLookupStore:
        def get(self, _alias: str) -> LocalAccount:
            raise AssertionError("invalid feed limit must be rejected before account lookup")

    engine = _FakeEngine()
    runtime = LocalRuntime(tmp_path, NoLookupStore(), engine)  # type: ignore[arg-type]

    with pytest.raises(StandaloneRuntimeError, match="^INVALID_FEED_LIMIT$"):
        await runtime.browse_feed("alice", max_items)  # type: ignore[arg-type]

    assert engine.requests == []


@pytest.mark.asyncio
async def test_browse_feed_reuses_headed_direct_account_profile_and_returns_bounded_result(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    store = LocalAccountStore(root)
    account = store.add("alice")
    profile_directory = root / "profiles" / str(account.id)
    profile_directory.mkdir(parents=True)
    session = _FakeSession(feed_batches=[(_feed_candidate("post-1", "  First   excerpt  "),)])
    engine = _FakeEngine(session=session)

    result = await LocalRuntime(root, store, engine).browse_feed("alice", 1)

    assert isinstance(result, BrowserFeedResultV1)
    assert result.model_dump(mode="json") == {
        "result_version": 1,
        "observations": [
            {
                "thread_ref": f"{BROWSER_FEED_ORIGIN}/@alice/post/post-1",
                "author_username": "alice",
                "text_excerpt": "First excerpt",
                "position": 0,
            }
        ],
        "truncated": True,
    }
    assert len(engine.requests) == 1
    request = engine.requests[0]
    assert request.profile_directory == profile_directory
    assert request.profile_directory.is_dir()
    assert request.headless is False
    assert request.network_route == BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None)
    assert request.proxy_credentials is None
    assert session.navigations == [(f"{BROWSER_FEED_ORIGIN}/", frozenset({BROWSER_FEED_ORIGIN}))]
    assert session.collect_calls == 1
    assert session.scroll_calls == 0
    assert session.close_calls == 1
    assert not hasattr(request, "worker_id")
    assert not hasattr(request, "account_id")
    assert not hasattr(request, "profile_ref")
    assert str(profile_directory) not in result.model_dump_json()
    assert "<html" not in result.model_dump_json()
    _assert_lock_can_be_acquired(root, account)


@pytest.mark.asyncio
async def test_browse_feed_lock_conflict_does_not_launch_browser(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    held_lock = FilesystemProcessLock(_lock_path(tmp_path, account))
    held_lock.acquire()
    engine = _FakeEngine()

    try:
        with pytest.raises(StandaloneRuntimeError, match="^ACCOUNT_BUSY$"):
            await LocalRuntime(tmp_path, store, engine).browse_feed("alice", 3)
        assert engine.requests == []
    finally:
        held_lock.release()


@pytest.mark.asyncio
async def test_browse_feed_deduplicates_across_iterations_and_enforces_limit(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    store = LocalAccountStore(root)
    account = store.add("alice")
    (root / "profiles" / str(account.id)).mkdir(parents=True)
    repeated = _feed_candidate("post-1")
    second = _feed_candidate("post-2", "second excerpt")
    session = _FakeSession(
        feed_batches=[(repeated,), (repeated, second), (_feed_candidate("post-3"),)]
    )
    engine = _FakeEngine(session=session)

    result = await LocalRuntime(root, store, engine).browse_feed("alice", 2)

    assert len(result.observations) == 2
    assert [item.thread_ref for item in result.observations] == [
        f"{BROWSER_FEED_ORIGIN}/@alice/post/post-1",
        f"{BROWSER_FEED_ORIGIN}/@alice/post/post-2",
    ]
    assert [item.position for item in result.observations] == [0, 1]
    assert session.collect_calls == 2
    assert session.scroll_calls == 1
    assert len(result.observations) <= 2
    assert session.close_calls == 1
    _assert_lock_can_be_acquired(root, account)


@pytest.mark.asyncio
async def test_browse_feed_stops_after_five_collections_and_four_scrolls(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    store = LocalAccountStore(root)
    account = store.add("alice")
    (root / "profiles" / str(account.id)).mkdir(parents=True)
    batches = [(_feed_candidate(f"post-{index}"),) for index in range(BROWSER_FEED_ITERATION_BOUND)]
    session = _FakeSession(feed_batches=batches)
    engine = _FakeEngine(session=session)

    result = await LocalRuntime(root, store, engine).browse_feed("alice", 20)

    assert session.collect_calls == BROWSER_FEED_ITERATION_BOUND == 5
    assert session.scroll_calls == BROWSER_FEED_ITERATION_BOUND - 1 == 4
    assert len(result.observations) == 5
    assert result.truncated is True
    assert session.close_calls == 1
    _assert_lock_can_be_acquired(root, account)


@pytest.mark.asyncio
async def test_browse_feed_adapter_failure_closes_session_and_releases_lock(
    tmp_path: Path,
) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    session = _FakeSession(feed_error=BrowserAdapterError("REMOTE_STATE_UNCERTAIN"))
    engine = _FakeEngine(session=session)

    with pytest.raises(StandaloneRuntimeError, match="^REMOTE_STATE_UNCERTAIN$"):
        await LocalRuntime(tmp_path, store, engine).browse_feed("alice", 3)

    assert session.close_calls == 1
    _assert_lock_can_be_acquired(tmp_path, account)


@pytest.mark.asyncio
async def test_browse_feed_timeout_closes_session_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_BROWSER_READ_TIMEOUT_SECONDS", 0.05)
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    started = asyncio.Event()
    session = _FakeSession(navigation_gate=asyncio.Event(), navigation_started=started)
    engine = _FakeEngine(session=session)
    task = asyncio.create_task(LocalRuntime(tmp_path, store, engine).browse_feed("alice", 3))
    await started.wait()

    with pytest.raises(StandaloneRuntimeError, match="^BROWSER_NAVIGATION_TIMEOUT$"):
        await task

    assert session.close_calls == 1
    _assert_lock_can_be_acquired(tmp_path, account)


@pytest.mark.asyncio
async def test_browse_feed_cancellation_closes_session_and_releases_lock(
    tmp_path: Path,
) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    started = asyncio.Event()
    session = _FakeSession(navigation_gate=asyncio.Event(), navigation_started=started)
    engine = _FakeEngine(session=session)
    task = asyncio.create_task(LocalRuntime(tmp_path, store, engine).browse_feed("alice", 3))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.close_calls == 1
    _assert_lock_can_be_acquired(tmp_path, account)


@pytest.mark.asyncio
async def test_target_open_lock_conflict_does_not_launch_browser(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    held_lock = FilesystemProcessLock(_lock_path(tmp_path, account))
    held_lock.acquire()
    engine = _FakeEngine()

    try:
        with pytest.raises(StandaloneRuntimeError, match="^ACCOUNT_BUSY$"):
            await LocalRuntime(tmp_path, store, engine).open_profile("alice", "alice")
        assert engine.requests == []
    finally:
        held_lock.release()


@pytest.mark.asyncio
async def test_browser_open_failure_releases_target_lock(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    engine = _FakeEngine(BrowserAdapterError("BROWSER_START_FAILED"))

    with pytest.raises(StandaloneRuntimeError, match="^BROWSER_START_FAILED$"):
        await LocalRuntime(tmp_path, store, engine).open_thread("alice", "/@alice/post/p1")

    _assert_lock_can_be_acquired(tmp_path, account)


@pytest.mark.asyncio
async def test_adapter_failure_closes_session_and_releases_target_lock(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    session = _FakeSession(profile_error=BrowserAdapterError("REMOTE_STATE_UNCERTAIN"))
    engine = _FakeEngine(session=session)

    with pytest.raises(StandaloneRuntimeError, match="^REMOTE_STATE_UNCERTAIN$"):
        await LocalRuntime(tmp_path, store, engine).open_profile("alice", "alice")

    assert session.close_calls == 1
    _assert_lock_can_be_acquired(tmp_path, account)


@pytest.mark.asyncio
async def test_target_read_timeout_closes_session_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_BROWSER_READ_TIMEOUT_SECONDS", 0.01)
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    session = _FakeSession(navigation_gate=asyncio.Event())
    engine = _FakeEngine(session=session)

    with pytest.raises(StandaloneRuntimeError, match="^BROWSER_NAVIGATION_TIMEOUT$"):
        await LocalRuntime(tmp_path, store, engine).open_profile("alice", "alice")

    assert session.close_calls == 1
    _assert_lock_can_be_acquired(tmp_path, account)


@pytest.mark.asyncio
async def test_local_account_lookup_is_inside_the_bounded_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    store = LocalAccountStore(root)
    account = store.add("alice")
    (root / "profiles" / str(account.id)).mkdir(parents=True)
    lookup_started = Event()
    release_lookup = Event()
    original_get = store.get

    def slow_get(alias: str) -> LocalAccount:
        lookup_started.set()
        release_lookup.wait(timeout=2)
        return original_get(alias)

    monkeypatch.setattr(runtime_module, "_BROWSER_READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(store, "get", slow_get)
    engine = _FakeEngine()
    try:
        with pytest.raises(StandaloneRuntimeError, match="^BROWSER_NAVIGATION_TIMEOUT$"):
            await LocalRuntime(root, store, engine).open_profile("alice", "alice")
    finally:
        release_lookup.set()

    assert lookup_started.is_set()
    assert engine.requests == []


@pytest.mark.asyncio
async def test_target_read_cancellation_closes_session_and_releases_lock(
    tmp_path: Path,
) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    started = asyncio.Event()
    session = _FakeSession(navigation_gate=asyncio.Event(), navigation_started=started)
    engine = _FakeEngine(session=session)
    task = asyncio.create_task(LocalRuntime(tmp_path, store, engine).open_profile("alice", "alice"))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.close_calls == 1
    _assert_lock_can_be_acquired(tmp_path, account)


@pytest.mark.asyncio
async def test_unexpected_browser_error_is_bounded_and_cleanup_still_runs(tmp_path: Path) -> None:
    store = LocalAccountStore(tmp_path)
    account = store.add("alice")
    (tmp_path / "profiles" / str(account.id)).mkdir(parents=True)
    session = _FakeSession(profile_error=RuntimeError("<html>private page</html>"))
    engine = _FakeEngine(session=session)

    with pytest.raises(StandaloneRuntimeError, match="^BROWSER_RUNTIME_UNAVAILABLE$"):
        await LocalRuntime(tmp_path, store, engine).open_profile("alice", "alice")

    assert session.close_calls == 1
    _assert_lock_can_be_acquired(tmp_path, account)
