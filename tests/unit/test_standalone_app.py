from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import httpx2
import pytest

import threads_platform.standalone.app as app_module
from threads_platform.config.settings import Settings
from threads_platform.standalone.app import StandaloneAppContext, build_standalone_app


class _FakeAsyncClientContext:
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> _FakeAsyncClientContext:
        self.enter_count += 1
        return self

    async def __aexit__(self, *_: object) -> None:
        self.exit_count += 1


class _ClientLedger:
    def __init__(self) -> None:
        self.clients: list[_FakeAsyncClientContext] = []

    def build(self, _settings: Settings) -> httpx2.AsyncClient:
        client = _FakeAsyncClientContext()
        self.clients.append(client)
        return cast(httpx2.AsyncClient, client)


def _install_fake_clients(monkeypatch: pytest.MonkeyPatch) -> _ClientLedger:
    ledger = _ClientLedger()
    monkeypatch.setattr(app_module, "build_threads_http_client", ledger.build)
    return ledger


def test_each_context_owns_one_api_client_and_has_no_shared_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _install_fake_clients(monkeypatch)
    contexts: list[StandaloneAppContext] = []

    async def build_two_contexts() -> None:
        async with build_standalone_app(tmp_path / "first") as first:
            contexts.append(first)
            assert first.api is not None
            assert first.mutations is not None
            assert first.browser is not None
        async with build_standalone_app(tmp_path / "second") as second:
            contexts.append(second)
            assert second.api is not None
            assert second.mutations is not None
            assert second.browser is not None

    asyncio.run(build_two_contexts())

    assert len(ledger.clients) == 2
    assert all(client.enter_count == 1 and client.exit_count == 1 for client in ledger.clients)
    assert contexts[0].accounts is not contexts[1].accounts
    assert contexts[0].api is not contexts[1].api
    assert contexts[0].mutations is not contexts[1].mutations
    assert contexts[0].browser is not contexts[1].browser


@pytest.mark.parametrize("failure", [RuntimeError("failed"), asyncio.CancelledError()])
def test_api_client_closes_after_context_failure_or_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    ledger = _install_fake_clients(monkeypatch)

    async def fail_in_context() -> None:
        async with build_standalone_app(
            tmp_path,
            include_mutations=False,
            include_browser=False,
        ):
            raise failure

    with pytest.raises(type(failure)):
        asyncio.run(fail_in_context())

    assert len(ledger.clients) == 1
    assert ledger.clients[0].enter_count == 1
    assert ledger.clients[0].exit_count == 1


def test_api_client_closes_if_mutation_runtime_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _install_fake_clients(monkeypatch)

    def fail_operation_store(_root: Path) -> object:
        raise RuntimeError("setup failed")

    monkeypatch.setattr(app_module, "LocalOperationStore", fail_operation_store)

    async def build_context() -> None:
        async with build_standalone_app(tmp_path, include_browser=False):
            raise AssertionError("context setup should fail")

    with pytest.raises(RuntimeError, match="setup failed"):
        asyncio.run(build_context())

    assert len(ledger.clients) == 1
    assert ledger.clients[0].enter_count == 1
    assert ledger.clients[0].exit_count == 1


def test_browser_only_context_does_not_create_an_http_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _install_fake_clients(monkeypatch)

    async def build_context() -> None:
        async with build_standalone_app(
            tmp_path,
            include_api=False,
            include_mutations=False,
        ) as app:
            assert app.api is None
            assert app.mutations is None
            assert app.browser is not None

    asyncio.run(build_context())

    assert ledger.clients == []
