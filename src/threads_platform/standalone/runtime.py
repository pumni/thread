"""Manual browser lifecycle for standalone local accounts."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import cast

from threads_platform.application.ports.browser import (
    BrowserAdapterError,
    BrowserEngine,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
)
from threads_platform.application.ports.process_lock import ProcessAlreadyRunning
from threads_platform.infrastructure.browser.playwright_engine import PlaywrightBrowserEngine
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore

_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_DEFAULT_OPERATOR_WAITER = cast(Callable[[str], None], input)


class StandaloneRuntimeError(Exception):
    code: str

    def __init__(self, code: str) -> None:
        if _SAFE_ERROR_CODE.fullmatch(code) is None:
            raise ValueError("standalone runtime error code must be bounded and safe")
        self.code = code
        super().__init__(code)


class LocalRuntime:
    def __init__(
        self,
        data_root: Path,
        accounts: LocalAccountStore,
        browser_engine: BrowserEngine | None = None,
    ) -> None:
        self._data_root = data_root
        self._accounts = accounts
        self._browser_engine = (
            browser_engine if browser_engine is not None else PlaywrightBrowserEngine()
        )

    async def login(
        self,
        alias: str,
        *,
        wait_for_operator: Callable[[str], None] = _DEFAULT_OPERATOR_WAITER,
    ) -> None:
        account = self._accounts.get(alias)
        lock = FilesystemProcessLock(self._lock_path(account))
        try:
            try:
                lock.acquire()
            except ProcessAlreadyRunning:
                raise StandaloneRuntimeError("ACCOUNT_BUSY") from None
            except OSError:
                raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None

            profile_directory = self._profile_directory(account)
            try:
                profile_directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None
            profile_directory = self._profile_directory(account)

            request = BrowserLaunchRequest(
                profile_directory=profile_directory,
                network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                proxy_credentials=None,
                headless=False,
            )

            session: BrowserEngineSession | None = None
            try:
                session = await self._browser_engine.open(request)
                prompt = (
                    f"Browser opened for {alias}. Log in to Threads manually, complete any normal "
                    "challenge, then press Enter here to close the browser: "
                )
                wait_for_operator(prompt)
            finally:
                if session is not None:
                    await session.close()
        except BrowserAdapterError as error:
            raise StandaloneRuntimeError(error.code) from None
        finally:
            lock.release()

    def _profile_directory(self, account: LocalAccount) -> Path:
        return self._resolve_managed_path("profiles", str(account.id))

    def _lock_path(self, account: LocalAccount) -> Path:
        return self._resolve_managed_path("locks", f"{account.id}.lock")

    def _resolve_managed_path(self, *parts: str) -> Path:
        try:
            root = self._data_root.resolve(strict=False)
            path = root.joinpath(*parts).resolve(strict=False)
            path.relative_to(root)
        except OSError, RuntimeError, ValueError:
            raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None
        return path
