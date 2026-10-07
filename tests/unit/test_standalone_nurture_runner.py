from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, cast
from uuid import UUID, uuid4

import pytest

import threads_platform.standalone.__main__ as cli_module
import threads_platform.standalone.app as app_module
import threads_platform.standalone.nurture_discovery as discovery_module
from threads_platform.application.ports.threads import DiscoveryPage, RemoteDiscoveryThread
from threads_platform.config.settings import Settings
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore
from threads_platform.standalone.api import LocalThreadsApiRuntime
from threads_platform.standalone.app import StandaloneAppContext
from threads_platform.standalone.nurture import NurturePresetV1, get_nurture_preset
from threads_platform.standalone.nurture_discovery import NurtureDiscoveryResult
from threads_platform.standalone.nurture_runner import (
    NurtureRunner,
    NurtureRunnerError,
    NurtureRunnerInterrupted,
)
from threads_platform.standalone.nurture_store import (
    NurtureRunV1,
    NurtureStateError,
    NurtureStore,
    NurtureTargetStateV1,
    NurtureTargetV1,
    fingerprint_remote_thread,
)

_NOW = datetime(2026, 10, 7, 1, 30, tzinfo=UTC)
_POST_TEXT = "REMOTE_POST_TEXT_SENTINEL career question?"
_TOKEN = "THREADS_TOKEN_SENTINEL_DO_NOT_LEAK"
_CREDENTIAL_REF = "env://THREADS_CREDENTIAL_SENTINEL_DO_NOT_LEAK"


def _preset(*, cooldown: int = 10, selected: int = 3) -> NurturePresetV1:
    return replace(
        get_nurture_preset("recruitment"),
        keyword_queries=("bounded query sentinel",),
        tag_queries=(),
        watched_public_usernames=(),
        include_terms=("career",),
        exclude_terms=(),
        max_selected_candidates=selected,
        max_browser_enrichments=min(5, selected),
        max_total_discovery_candidates=10,
        seen_cooldown_seconds=cooldown,
    )


def _thread(
    remote_id: str,
    *,
    text: str | None,
    timestamp: datetime | None,
    has_replies: bool | None = None,
) -> RemoteDiscoveryThread:
    return RemoteDiscoveryThread(
        remote_id,
        None,
        None,
        text,
        None,
        None,
        timestamp,
        None,
        has_replies,
    )


def _threads(now: datetime = _NOW) -> tuple[RemoteDiscoveryThread, ...]:
    return (
        _thread("thread-a", text=_POST_TEXT, timestamp=now, has_replies=False),
        _thread("thread-b", text="career role", timestamp=now - timedelta(days=10)),
    )


class _FakeApi:
    def __init__(
        self,
        threads: tuple[RemoteDiscoveryThread, ...] = (),
        *,
        failure: BaseException | None = None,
        on_first_call: Callable[[], None] | None = None,
    ) -> None:
        self.threads = threads
        self.failure = failure
        self.on_first_call = on_first_call
        self.calls: list[tuple[str, str | None, int, str | None]] = []

    async def mentions(self, _alias: str, *, after: str | None, limit: int) -> DiscoveryPage:
        return self._page("mentions", None, after, limit, self.threads)

    async def search(
        self,
        _alias: str,
        query: str,
        *,
        search_mode: DiscoverySearchMode,
        search_type: DiscoverySearchType,
        after: str | None,
        limit: int,
    ) -> DiscoveryPage:
        assert search_mode in {DiscoverySearchMode.KEYWORD, DiscoverySearchMode.TAG}
        assert search_type is DiscoverySearchType.RECENT
        return self._page("search", query, after, limit, ())

    async def profile_posts(
        self, _alias: str, username: str, *, after: str | None, limit: int
    ) -> DiscoveryPage:
        return self._page("profile_posts", username, after, limit, ())

    def _page(
        self,
        source: str,
        value: str | None,
        after: str | None,
        limit: int,
        threads: tuple[RemoteDiscoveryThread, ...],
    ) -> DiscoveryPage:
        self.calls.append((source, value, limit, after))
        if len(self.calls) == 1 and self.on_first_call is not None:
            self.on_first_call()
        if self.failure is not None:
            raise self.failure
        return DiscoveryPage(threads, "cursor-sentinel", True)


def _runner(api: _FakeApi, store: NurtureStore) -> NurtureRunner:
    return NurtureRunner(cast(LocalThreadsApiRuntime, api), store)


def _read_run(store: NurtureStore, account: LocalAccount, run_id: UUID) -> NurtureRunV1:
    with store.acquire_account_lock(account.id) as owner:
        return owner.get_run(run_id)


def _read_targets(
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
) -> tuple[NurtureTargetV1, ...]:
    with store.acquire_account_lock(account.id) as owner:
        return owner.get_targets(preset)


def _create_confirmed_target(
    store: NurtureStore,
    account: LocalAccount,
    preset: NurturePresetV1,
    remote_id: str,
    *,
    action_at: datetime,
    last_seen_at: datetime | None = None,
    operation_id: UUID | None = None,
) -> NurtureTargetV1:
    op_id = operation_id or uuid4()
    fingerprint = fingerprint_remote_thread(remote_id)
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=action_at)
        owner.reserve_target(preset, fingerprint, scope.receipt.id, "OBSERVE_ONLY", now=action_at)
        target = owner.complete_target_action(
            preset,
            fingerprint,
            scope.receipt.id,
            "CONFIRMED",
            op_id,
            now=action_at,
        )
        if last_seen_at is not None and last_seen_at != action_at:
            target = owner.observe_target(
                preset,
                fingerprint,
                scope.receipt.id,
                "OBSERVE_ONLY",
                now=last_seen_at,
            )
        scope.finish("SUCCESS")
        return target


def _all_nurture_json(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "nurture").rglob("*.json")
    )


def test_unknown_preset_and_account_fail_before_api_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    accounts.add("alice")
    api = _FakeApi()

    def forbidden_builder(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("API composition must not happen during preflight failures")

    monkeypatch.setattr(cli_module, "build_standalone_app", forbidden_builder)

    assert cli_module.main(["nurture", "run", "alice", "--preset", "unknown"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR PRESET_NOT_FOUND\n"
    assert api.calls == []

    assert cli_module.main(["nurture", "run", "missing", "--preset", "recruitment"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR ACCOUNT_NOT_FOUND\n"
    assert api.calls == []


def test_empty_discovery_prints_no_action_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    accounts.add("alice")
    api = _FakeApi()

    @asynccontextmanager
    async def fake_app(
        _root: Path,
        local_accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = True,
    ) -> AsyncGenerator[StandaloneAppContext]:
        assert include_api
        assert not include_mutations
        assert not include_browser
        yield StandaloneAppContext(local_accounts, cast(LocalThreadsApiRuntime, api), None, None)

    monkeypatch.setattr(cli_module, "build_standalone_app", fake_app)

    assert cli_module.main(["nurture", "run", "alice", "--preset", "recruitment"]) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert "outcome=SUCCESS discovered=0 selected=0 decision=NO_ACTION" in output.out


@pytest.mark.asyncio
async def test_busy_same_account_creates_no_receipt_or_api_call(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    accounts = LocalAccountStore(root)
    account = accounts.add("alice")
    store = NurtureStore(root)
    api = _FakeApi(_threads())
    held = store.acquire_account_lock(account.id).acquire()
    try:
        with pytest.raises(NurtureRunnerError) as error:
            await _runner(api, store).run(account, _preset(), now=_NOW)
    finally:
        held.release()

    assert error.value.code == "NURTURE_BUSY"
    assert error.value.run_id is None
    assert api.calls == []
    assert not (root / "nurture" / "runs" / str(account.id)).exists()


def test_runner_creates_running_receipt_before_discovery_and_cli_composes_api_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    account = accounts.add("alice")
    accounts.set_credential_ref("alice", _CREDENTIAL_REF)

    def assert_running_receipt_exists() -> None:
        active = tuple((root / "nurture" / "runs" / str(account.id) / "active").glob("*.json"))
        assert len(active) == 1
        receipt = json.loads(active[0].read_text(encoding="utf-8"))
        assert receipt["outcome"] == "RUNNING"
        assert receipt["selected_count"] == 0

    api = _FakeApi(_threads(), on_first_call=assert_running_receipt_exists)
    flags: dict[str, object] = {}
    original_builder = cli_module.build_standalone_app

    @asynccontextmanager
    async def tracked_builder(
        root_arg: Path,
        accounts_arg: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = True,
    ) -> AsyncGenerator[StandaloneAppContext]:
        flags.update(
            include_api=include_api,
            include_mutations=include_mutations,
            include_browser=include_browser,
        )
        async with original_builder(
            root_arg,
            accounts_arg,
            include_api=include_api,
            include_mutations=include_mutations,
            include_browser=include_browser,
        ) as app:
            assert app.api is api
            assert app.mutations is None
            assert app.browser is None
            yield app

    @asynccontextmanager
    async def fake_client(_settings: Settings) -> AsyncGenerator[object]:
        yield object()

    def fake_http_api(_client: object) -> HttpThreadsAPI:
        return cast(HttpThreadsAPI, object())

    def fake_api_runtime(
        _accounts: LocalAccountStore,
        _api: object,
        _resolver: object,
    ) -> LocalThreadsApiRuntime:
        return cast(LocalThreadsApiRuntime, api)

    def forbidden_runtime(*_args: object) -> NoReturn:
        pytest.fail("runtime must not be constructed")

    monkeypatch.setattr(cli_module, "build_standalone_app", tracked_builder)
    monkeypatch.setattr(app_module, "build_threads_http_client", fake_client)
    monkeypatch.setattr(app_module, "HttpThreadsAPI", fake_http_api)
    monkeypatch.setattr(
        app_module,
        "EnvironmentThreadsCredentialSecretResolver",
        lambda: object(),
    )
    monkeypatch.setattr(
        app_module,
        "LocalThreadsApiRuntime",
        fake_api_runtime,
    )
    monkeypatch.setattr(
        app_module,
        "LocalThreadsMutationRuntime",
        forbidden_runtime,
    )
    monkeypatch.setattr(
        app_module,
        "LocalRuntime",
        forbidden_runtime,
    )

    assert cli_module.main(["nurture", "run", "alice", "--preset", "recruitment"]) == 0
    output = capsys.readouterr()
    assert flags == {
        "include_api": True,
        "include_mutations": False,
        "include_browser": False,
    }
    assert output.err == ""
    assert "outcome=SUCCESS" in output.out
    assert "selected=1 decision=OBSERVE_ONLY" in output.out
    assert _POST_TEXT not in output.out
    assert _TOKEN not in output.out
    assert _CREDENTIAL_REF not in output.out
    assert api.calls[0][0] == "mentions"

    store = NurtureStore(root)
    targets = _read_targets(store, account, get_nurture_preset("recruitment"))
    assert len(targets) == 1
    assert targets[0].fingerprint == fingerprint_remote_thread("thread-a")
    assert targets[0].action_state == "NONE"
    assert targets[0].last_operation_id is None
    assert all(target.fingerprint != fingerprint_remote_thread("thread-b") for target in targets)
    assert _POST_TEXT not in _all_nurture_json(root)
    assert _TOKEN not in _all_nurture_json(root)
    assert _CREDENTIAL_REF not in _all_nurture_json(root)
    assert "tìm việc" not in _all_nurture_json(root)
    assert "cursor-sentinel" not in _all_nurture_json(root)
    assert "thread-a" not in _all_nurture_json(root)
    receipt_files = tuple(
        (root / "nurture" / "runs" / str(account.id) / "completed").glob("*.json")
    )
    assert len(receipt_files) == 1
    receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
    assert receipt["discovered_count"] == 2
    assert receipt["deduped_count"] == 2
    assert receipt["selected_count"] == 1
    assert receipt["skipped_count"] == 1
    assert receipt["enriched_count"] == receipt["replied_count"] == 0
    assert receipt["published_count"] == 0
    assert receipt["decision_codes"] == ["OBSERVE_ONLY"]
    assert receipt["operation_ids"] == []


@pytest.mark.asyncio
async def test_confirmed_target_uses_last_seen_surfacing_cooldown_without_losing_action_history(
    tmp_path: Path,
) -> None:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    preset = _preset(cooldown=10)
    store = NurtureStore(root)
    original_operation_id = uuid4()
    action_at = _NOW - timedelta(days=1)
    original = _create_confirmed_target(
        store,
        account,
        preset,
        "thread-a",
        action_at=action_at,
        operation_id=original_operation_id,
    )
    api = _FakeApi(_threads())
    runner = _runner(api, store)

    first = await runner.run(account, preset, now=_NOW)
    first_targets = _read_targets(store, account, preset)
    surfaced = next(
        target for target in first_targets if target.fingerprint == original.fingerprint
    )
    assert first.selected_count == 1
    assert first.decision_codes[0] == "OBSERVE_ONLY"
    assert surfaced.action_state == "CONFIRMED"
    assert surfaced.last_action_at == original.last_action_at == action_at
    assert surfaced.last_operation_id == original.last_operation_id == original_operation_id
    assert surfaced.last_seen_at == _NOW

    second_now = _NOW + timedelta(seconds=1)
    second = await runner.run(account, preset, now=second_now)
    second_targets = _read_targets(store, account, preset)
    surfaced_again = next(
        target for target in second_targets if target.fingerprint == original.fingerprint
    )
    next_target = next(
        target
        for target in second_targets
        if target.fingerprint == fingerprint_remote_thread("thread-b")
    )
    assert second.selected_count == 1
    assert second.decision_codes[0] == "OBSERVE_ONLY"
    assert surfaced_again.action_state == "CONFIRMED"
    assert surfaced_again.last_action_at == action_at
    assert surfaced_again.last_operation_id == original_operation_id
    assert surfaced_again.last_seen_at == _NOW
    assert next_target.action_state == "NONE"
    assert next_target.last_seen_at == second_now

    after_cooldown = _NOW + timedelta(seconds=11)
    third = await runner.run(account, preset, now=after_cooldown)
    final_targets = _read_targets(store, account, preset)
    surfaced_after_cooldown = next(
        target for target in final_targets if target.fingerprint == original.fingerprint
    )
    assert third.selected_count == 1
    assert surfaced_after_cooldown.action_state == "CONFIRMED"
    assert surfaced_after_cooldown.last_action_at == action_at
    assert surfaced_after_cooldown.last_operation_id == original_operation_id
    assert surfaced_after_cooldown.last_seen_at == after_cooldown


@pytest.mark.asyncio
async def test_recently_observed_none_target_is_not_shown_again_immediately(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    preset = _preset(cooldown=10)
    store = NurtureStore(root)
    runner = _runner(_FakeApi(_threads()[:1]), store)

    first = await runner.run(account, preset, now=_NOW)
    second = await runner.run(account, preset, now=_NOW + timedelta(seconds=1))

    assert first.selected_count == 1
    assert first.decision_codes[0] == "OBSERVE_ONLY"
    assert second.outcome == "SUCCESS"
    assert second.selected_count == 0
    assert second.decision_codes[0] == "NO_ACTION"


@pytest.mark.asyncio
async def test_all_candidates_in_bounded_shortlist_recently_surfaced_means_success_no_action(
    tmp_path: Path,
) -> None:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    preset = _preset(cooldown=10)
    store = NurtureStore(root)
    action_at = _NOW - timedelta(days=1)
    for remote_id in ("thread-a", "thread-b"):
        _create_confirmed_target(
            store,
            account,
            preset,
            remote_id,
            action_at=action_at,
            last_seen_at=_NOW - timedelta(seconds=1),
        )
    api = _FakeApi(_threads())

    run = await _runner(api, store).run(account, preset, now=_NOW)

    assert run.outcome == "SUCCESS"
    assert run.discovered_count == 2
    assert run.deduped_count == 2
    assert run.selected_count == 0
    assert run.skipped_count == 2
    assert run.decision_codes == ("NO_ACTION",)
    assert api.calls[0][0] == "mentions"


@pytest.mark.asyncio
async def test_future_last_seen_suppresses_confirmed_candidate_conservatively(
    tmp_path: Path,
) -> None:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    preset = _preset(cooldown=10)
    store = NurtureStore(root)
    original = _create_confirmed_target(
        store,
        account,
        preset,
        "thread-a",
        action_at=_NOW - timedelta(days=1),
        last_seen_at=_NOW + timedelta(minutes=1),
    )

    run = await _runner(_FakeApi((_threads()[0],)), store).run(account, preset, now=_NOW)
    current = next(
        target
        for target in _read_targets(store, account, preset)
        if target.fingerprint == original.fingerprint
    )

    assert run.selected_count == 0
    assert run.decision_codes == ("NO_ACTION",)
    assert current.last_seen_at == _NOW + timedelta(minutes=1)
    assert current.last_action_at == original.last_action_at
    assert current.last_operation_id == original.last_operation_id


@pytest.mark.asyncio
async def test_zero_seen_cooldown_has_no_added_surfacing_delay(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    preset = _preset(cooldown=0)
    store = NurtureStore(root)
    runner = _runner(_FakeApi(_threads()), store)

    first = await runner.run(account, preset, now=_NOW)
    second = await runner.run(account, preset, now=_NOW)

    assert first.selected_count == second.selected_count == 1
    assert first.decision_codes[0] == second.decision_codes[0] == "OBSERVE_ONLY"


@pytest.mark.asyncio
async def test_source_failure_creates_one_failed_receipt_without_retry(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    store = NurtureStore(root)
    api = _FakeApi(_threads(), failure=RuntimeError(_TOKEN + _POST_TEXT))

    with pytest.raises(NurtureRunnerError) as error:
        await _runner(api, store).run(account, _preset(), now=_NOW)

    assert error.value.code == "DISCOVERY_SOURCE_FAILED"
    assert error.value.run_id is not None
    assert [call[0] for call in api.calls] == ["mentions"]
    receipt = _read_run(store, account, error.value.run_id)
    assert receipt.outcome == "FAILED"
    assert receipt.error_code == "DISCOVERY_SOURCE_FAILED"
    assert receipt.failed_stage == "source_call"
    persisted = _all_nurture_json(root)
    assert _TOKEN not in persisted
    assert _POST_TEXT not in persisted
    assert _TOKEN not in repr(error.value)
    assert _POST_TEXT not in repr(error.value)


def test_cli_discovery_failure_emits_only_safe_code_and_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    accounts.add("alice")
    api = _FakeApi(failure=RuntimeError(_TOKEN + _CREDENTIAL_REF + _POST_TEXT))

    @asynccontextmanager
    async def fake_app(
        _root: Path,
        local_accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = True,
    ) -> AsyncGenerator[StandaloneAppContext]:
        assert include_api
        assert not include_mutations
        assert not include_browser
        yield StandaloneAppContext(local_accounts, cast(LocalThreadsApiRuntime, api), None, None)

    monkeypatch.setattr(cli_module, "build_standalone_app", fake_app)

    assert cli_module.main(["nurture", "run", "alice", "--preset", "recruitment"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("ERROR DISCOVERY_SOURCE_FAILED run=")
    assert all(
        secret not in output.out + output.err for secret in (_TOKEN, _CREDENTIAL_REF, _POST_TEXT)
    )
    assert [call[0] for call in api.calls] == ["mentions"]
    persisted = _all_nurture_json(root)
    assert all(secret not in persisted for secret in (_TOKEN, _CREDENTIAL_REF, _POST_TEXT))
    assert "tìm việc" not in persisted
    assert "cursor-sentinel" not in persisted


def test_state_write_failure_never_prints_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    accounts.add("alice")
    api = _FakeApi(_threads())

    @asynccontextmanager
    async def fake_app(
        _root: Path,
        accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = True,
    ) -> AsyncGenerator[StandaloneAppContext]:
        assert include_api
        assert not include_mutations
        assert not include_browser
        yield StandaloneAppContext(accounts, cast(LocalThreadsApiRuntime, api), None, None)

    def fail_target_write(*_args: object, **_kwargs: object) -> None:
        raise NurtureStateError("NURTURE_STATE_INVALID")

    monkeypatch.setattr(cli_module, "build_standalone_app", fake_app)
    monkeypatch.setattr(NurtureStore, "save_target_state", fail_target_write)

    assert cli_module.main(["nurture", "run", "alice", "--preset", "recruitment"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("ERROR NURTURE_STATE_INVALID run=")
    assert _POST_TEXT not in output.out + output.err
    active_or_completed = tuple((root / "nurture" / "runs").rglob("*.json"))
    assert len(active_or_completed) == 1
    receipt = json.loads(active_or_completed[0].read_text(encoding="utf-8"))
    assert receipt["outcome"] == "FAILED"
    assert receipt["failed_stage"] == "observe"


@pytest.mark.asyncio
async def test_keyboard_interrupt_persists_interrupted_and_reports_run_id(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    store = NurtureStore(root)
    api = _FakeApi(failure=KeyboardInterrupt())

    with pytest.raises(NurtureRunnerInterrupted) as error:
        await _runner(api, store).run(account, _preset(), now=_NOW)

    receipt = _read_run(store, account, error.value.run_id)
    assert receipt.outcome == "INTERRUPTED"
    assert receipt.error_code == "INTERRUPTED"
    assert receipt.failed_stage == "execution"
    assert [call[0] for call in api.calls] == ["mentions"]


def test_cli_keyboard_interrupt_reports_safe_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "standalone"
    monkeypatch.setenv("THREADS_LOCAL_DATA_ROOT", str(root))
    accounts = LocalAccountStore(root)
    account = accounts.add("alice")
    api = _FakeApi(failure=KeyboardInterrupt(_TOKEN + _POST_TEXT))

    @asynccontextmanager
    async def fake_app(
        _root: Path,
        local_accounts: LocalAccountStore,
        *,
        include_api: bool = True,
        include_mutations: bool = True,
        include_browser: bool = True,
    ) -> AsyncGenerator[StandaloneAppContext]:
        assert include_api
        assert not include_mutations
        assert not include_browser
        yield StandaloneAppContext(local_accounts, cast(LocalThreadsApiRuntime, api), None, None)

    monkeypatch.setattr(cli_module, "build_standalone_app", fake_app)

    assert cli_module.main(["nurture", "run", "alice", "--preset", "recruitment"]) == 130
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("ERROR INTERRUPTED run=")
    assert _TOKEN not in output.err
    assert _POST_TEXT not in output.err
    completed = tuple((root / "nurture" / "runs" / str(account.id) / "completed").glob("*.json"))
    assert len(completed) == 1
    receipt = json.loads(completed[0].read_text(encoding="utf-8"))
    assert receipt["outcome"] == "INTERRUPTED"


@pytest.mark.asyncio
async def test_async_cancellation_persists_interrupted_and_propagates(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    store = NurtureStore(root)
    api = _FakeApi(failure=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _runner(api, store).run(account, _preset(), now=_NOW)

    receipts = tuple((root / "nurture" / "runs" / str(account.id) / "completed").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["outcome"] == "INTERRUPTED"
    assert receipt["error_code"] == "INTERRUPTED"
    assert receipt["failed_stage"] == "execution"


@pytest.mark.asyncio
async def test_unexpected_pending_and_ambiguous_shortlist_entries_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "standalone"
    account = LocalAccountStore(root).add("alice")
    preset = _preset()
    store = NurtureStore(root)
    fingerprints = (
        fingerprint_remote_thread("thread-a"),
        fingerprint_remote_thread("thread-b"),
    )
    with store.acquire_account_lock(account.id) as owner:
        scope = owner.start_run(preset, now=_NOW - timedelta(hours=1))
        owner.reserve_target(preset, fingerprints[0], scope.receipt.id, "OBSERVE_ONLY", now=_NOW)
        owner.reserve_target(preset, fingerprints[1], scope.receipt.id, "OBSERVE_ONLY", now=_NOW)
        owner.complete_target_action(
            preset,
            fingerprints[1],
            scope.receipt.id,
            "AMBIGUOUS",
            uuid4(),
            now=_NOW,
        )
        scope.finish("SUCCESS")
        owner.get_targets(preset)

    api = _FakeApi(_threads())
    discovered = await discovery_module.discover_nurture_candidates(
        cast(LocalThreadsApiRuntime, api),
        account.alias,
        preset,
        NurtureTargetStateV1(1, account.id, preset.id, ()),
        _NOW,
    )
    assert len(discovered.selected_candidates) == 2
    forced_shortlist = replace(discovered)

    async def return_forced_shortlist(
        _api: LocalThreadsApiRuntime,
        _alias: str,
        _preset_arg: NurturePresetV1,
        _state: NurtureTargetStateV1,
        _now: datetime,
    ) -> NurtureDiscoveryResult:
        return forced_shortlist

    monkeypatch.setattr(
        "threads_platform.standalone.nurture_runner.discover_nurture_candidates",
        return_forced_shortlist,
    )
    run = await _runner(_FakeApi(), store).run(account, preset, now=_NOW)
    assert run.outcome == "SUCCESS"
    assert run.selected_count == 0
    assert run.decision_codes == ("NO_ACTION",)
    final_targets = _read_targets(store, account, preset)
    by_fingerprint = {target.fingerprint: target for target in final_targets}
    assert by_fingerprint[fingerprints[0]].action_state == "PENDING"
    assert by_fingerprint[fingerprints[1]].action_state == "AMBIGUOUS"
