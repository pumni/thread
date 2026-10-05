from __future__ import annotations

from pathlib import Path

import pytest

from threads_platform.application.ports.browser import (
    BrowserAdapterError,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
)
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.runtime import LocalRuntime, StandaloneRuntimeError


class _FakeSession:
    def __init__(self) -> None:
        self.close_calls = 0
        self.navigate_calls = 0

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        self.navigate_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


class _FakeEngine:
    def __init__(self, open_error: BrowserAdapterError | None = None) -> None:
        self.open_error = open_error
        self.requests: list[BrowserLaunchRequest] = []
        self.sessions: list[_FakeSession] = []

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        self.requests.append(request)
        if self.open_error is not None:
            raise self.open_error
        session = _FakeSession()
        self.sessions.append(session)
        return session


def _lock_path(root: Path, account: LocalAccount) -> Path:
    return root / "locks" / f"{account.id}.lock"


def _assert_lock_can_be_acquired(root: Path, account: LocalAccount) -> None:
    lock = FilesystemProcessLock(_lock_path(root, account))
    lock.acquire()
    lock.release()


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
