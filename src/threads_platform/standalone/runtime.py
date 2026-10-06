"""Manual browser lifecycle for standalone local accounts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from threads_platform.application.browser_capabilities import (
    BrowserFeedItemResultV1,
    BrowserFeedResultV1,
    BrowserTargetOpenResultV1,
)
from threads_platform.application.browser_read_semantics import (
    BROWSER_FEED_ANCESTOR_BOUND,
    BROWSER_FEED_ITERATION_BOUND,
    BROWSER_FEED_URL,
    BROWSER_READ_TARGET_ANCESTOR_BOUND,
    normalize_feed_candidates,
    normalize_profile_username,
    parse_thread_ref,
)
from threads_platform.application.ports.browser import (
    BROWSER_FEED_ORIGIN,
    BrowserAdapterError,
    BrowserContractError,
    BrowserEngine,
    BrowserEngineSession,
    BrowserFeedEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserProfileOpenEngineSession,
    BrowserThreadOpenEngineSession,
)
from threads_platform.application.ports.process_lock import ProcessAlreadyRunning
from threads_platform.infrastructure.browser.playwright_engine import PlaywrightBrowserEngine
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.standalone.accounts import LocalAccount, LocalAccountStore

_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_BROWSER_READ_TIMEOUT_SECONDS = 30
_THREADS_WEB_ORIGINS = frozenset({BROWSER_FEED_ORIGIN})
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

    async def open_profile(self, alias: str, username: str) -> BrowserTargetOpenResultV1:
        target_ref = normalize_profile_username(username)
        if target_ref is None:
            raise StandaloneRuntimeError("INVALID_PROFILE_TARGET")
        return await self._open_browser_target(alias, target_ref, target_kind="PROFILE")

    async def open_thread(self, alias: str, thread_ref: str) -> BrowserTargetOpenResultV1:
        parsed_target = parse_thread_ref(thread_ref)
        if parsed_target is None:
            raise StandaloneRuntimeError("INVALID_THREAD_TARGET")
        target_ref, author_username = parsed_target
        return await self._open_browser_target(
            alias,
            target_ref,
            target_kind="THREAD",
            author_username=author_username,
        )

    async def browse_feed(self, alias: str, max_items: int = 10) -> BrowserFeedResultV1:
        if type(max_items) is not int or not 1 <= max_items <= 20:
            raise StandaloneRuntimeError("INVALID_FEED_LIMIT")
        try:
            async with asyncio.timeout(_BROWSER_READ_TIMEOUT_SECONDS):
                account = await asyncio.to_thread(self._accounts.get, alias)
                lock = FilesystemProcessLock(self._lock_path(account))
                try:
                    lock.acquire()
                except ProcessAlreadyRunning:
                    raise StandaloneRuntimeError("ACCOUNT_BUSY") from None
                except OSError:
                    raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None

                session: BrowserEngineSession | None = None
                try:
                    profile_directory = self._profile_directory(account)
                    if not profile_directory.is_dir():
                        raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE")
                    request = BrowserLaunchRequest(
                        profile_directory=profile_directory,
                        network_route=BrowserNetworkRoute(
                            BrowserNetworkProtocol.DIRECT,
                            None,
                            None,
                        ),
                        proxy_credentials=None,
                        headless=False,
                    )
                    session = await self._browser_engine.open(request)
                    feed_session = cast(BrowserFeedEngineSession, session)
                    await feed_session.navigate(
                        BROWSER_FEED_URL,
                        allowed_origins=_THREADS_WEB_ORIGINS,
                    )

                    observations: list[BrowserFeedItemResultV1] = []
                    seen_refs: set[str] = set()
                    truncated = False
                    for iteration in range(BROWSER_FEED_ITERATION_BOUND):
                        if iteration:
                            await feed_session.scroll_feed()
                        candidates = await feed_session.collect_feed_candidates(
                            ancestor_bound=BROWSER_FEED_ANCESTOR_BOUND
                        )
                        if not candidates and not observations:
                            raise BrowserContractError()
                        batch = normalize_feed_candidates(
                            candidates,
                            max_items=max_items - len(observations),
                            ancestor_bound=BROWSER_FEED_ANCESTOR_BOUND,
                            existing_refs=frozenset(seen_refs),
                            position_start=len(observations),
                        )
                        for item in batch:
                            if item.thread_ref is None:
                                raise BrowserContractError()
                            seen_refs.add(item.thread_ref)
                            observations.append(item)
                        if len(observations) >= max_items:
                            truncated = True
                            break
                        if iteration and not batch:
                            truncated = True
                            break
                        if iteration == BROWSER_FEED_ITERATION_BOUND - 1:
                            truncated = True

                    return BrowserFeedResultV1(
                        observations=tuple(observations[:max_items]),
                        truncated=truncated,
                    )
                except BrowserAdapterError as error:
                    raise StandaloneRuntimeError(error.code) from None
                except StandaloneRuntimeError:
                    raise
                except Exception:
                    raise StandaloneRuntimeError("BROWSER_RUNTIME_UNAVAILABLE") from None
                finally:
                    try:
                        if session is not None:
                            await session.close()
                    except BrowserAdapterError as error:
                        raise StandaloneRuntimeError(error.code) from None
                    except Exception:
                        raise StandaloneRuntimeError("BROWSER_RUNTIME_UNAVAILABLE") from None
                    finally:
                        try:
                            lock.release()
                        except OSError:
                            raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None
        except TimeoutError:
            raise StandaloneRuntimeError("BROWSER_NAVIGATION_TIMEOUT") from None

    async def _open_browser_target(
        self,
        alias: str,
        target_ref: str,
        *,
        target_kind: Literal["PROFILE", "THREAD"],
        author_username: str | None = None,
    ) -> BrowserTargetOpenResultV1:
        try:
            async with asyncio.timeout(_BROWSER_READ_TIMEOUT_SECONDS):
                account = await asyncio.to_thread(self._accounts.get, alias)
                lock = FilesystemProcessLock(self._lock_path(account))
                try:
                    lock.acquire()
                except ProcessAlreadyRunning:
                    raise StandaloneRuntimeError("ACCOUNT_BUSY") from None
                except OSError:
                    raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None

                session: BrowserEngineSession | None = None
                try:
                    profile_directory = self._profile_directory(account)
                    if not profile_directory.is_dir():
                        raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE")
                    request = BrowserLaunchRequest(
                        profile_directory=profile_directory,
                        network_route=BrowserNetworkRoute(
                            BrowserNetworkProtocol.DIRECT,
                            None,
                            None,
                        ),
                        proxy_credentials=None,
                        headless=False,
                    )
                    session = await self._browser_engine.open(request)
                    await session.navigate(
                        f"{BROWSER_FEED_ORIGIN}{target_ref}",
                        allowed_origins=_THREADS_WEB_ORIGINS,
                    )
                    if target_kind == "PROFILE":
                        profile_session = cast(BrowserProfileOpenEngineSession, session)
                        await profile_session.verify_profile_target(
                            target_ref=target_ref,
                            ancestor_bound=BROWSER_READ_TARGET_ANCESTOR_BOUND,
                        )
                    else:
                        if author_username is None:
                            raise StandaloneRuntimeError("BROWSER_CONTRACT_MISMATCH")
                        thread_session = cast(BrowserThreadOpenEngineSession, session)
                        await thread_session.verify_thread_target(
                            target_ref=target_ref,
                            author_username=author_username,
                            ancestor_bound=BROWSER_READ_TARGET_ANCESTOR_BOUND,
                        )
                    return BrowserTargetOpenResultV1(
                        target_kind=target_kind,
                        target_ref=target_ref,
                        recognized=True,
                    )
                except BrowserAdapterError as error:
                    raise StandaloneRuntimeError(error.code) from None
                except StandaloneRuntimeError:
                    raise
                except Exception:
                    raise StandaloneRuntimeError("BROWSER_RUNTIME_UNAVAILABLE") from None
                finally:
                    try:
                        if session is not None:
                            await session.close()
                    except BrowserAdapterError as error:
                        raise StandaloneRuntimeError(error.code) from None
                    except Exception:
                        raise StandaloneRuntimeError("BROWSER_RUNTIME_UNAVAILABLE") from None
                    finally:
                        try:
                            lock.release()
                        except OSError:
                            raise StandaloneRuntimeError("LOCAL_PROFILE_UNAVAILABLE") from None
        except TimeoutError:
            raise StandaloneRuntimeError("BROWSER_NAVIGATION_TIMEOUT") from None

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
