from __future__ import annotations

from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    ProxySettings,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from threads_platform.domain.workers import NetworkProtocol
from threads_platform.workers.browser import (
    BrowserContractError,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkRouteUnsupported,
    BrowserProcessCrashed,
    BrowserRuntimeUnavailable,
    BrowserSurface,
    LocatorNotFound,
    NavigationTimeout,
)


class PlaywrightBrowserEngine:
    """Pinned Playwright/Chromium lifecycle implementation for worker profiles."""

    def __init__(self, *, navigation_timeout_ms: int = 30_000) -> None:
        if navigation_timeout_ms < 1:
            raise ValueError("navigation timeout must be positive")
        self._navigation_timeout_ms = navigation_timeout_ms

    async def open(self, request: BrowserLaunchRequest) -> BrowserEngineSession:
        profile_directory = request.profile_directory.resolve(strict=True)
        if not profile_directory.is_dir():
            raise BrowserContractError()
        proxy: ProxySettings | None = playwright_proxy_settings(request)
        manager = async_playwright()
        try:
            playwright = await manager.start()
        except PlaywrightError:
            raise BrowserRuntimeUnavailable("BROWSER_RUNTIME_UNAVAILABLE") from None
        executable = Path(playwright.chromium.executable_path)
        if not executable.is_file():
            await playwright.stop()
            raise BrowserRuntimeUnavailable("BROWSER_BINARY_MISSING")
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_directory),
                headless=request.headless,
                proxy=proxy,
                accept_downloads=False,
            )
        except PlaywrightError:
            await playwright.stop()
            raise BrowserRuntimeUnavailable("BROWSER_START_FAILED") from None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
        except PlaywrightError:
            await context.close()
            await playwright.stop()
            raise BrowserRuntimeUnavailable("BROWSER_PAGE_CREATION_FAILED") from None
        return _PlaywrightBrowserSession(
            playwright,
            context,
            page,
            navigation_timeout_ms=self._navigation_timeout_ms,
        )


class _PlaywrightBrowserSession:
    def __init__(
        self,
        playwright: Playwright,
        context: BrowserContext,
        page: Page,
        *,
        navigation_timeout_ms: int,
    ) -> None:
        self._playwright = playwright
        self._context = context
        self._page = page
        self._browser: Browser | None = context.browser
        self._navigation_timeout_ms = navigation_timeout_ms
        self._crashed = False
        self._closed = False
        page.on("crash", self._on_page_crash)
        context.on("close", self._on_context_close)

    async def navigate(self, url: str) -> None:
        self._ensure_alive()
        try:
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._navigation_timeout_ms,
            )
        except PlaywrightTimeoutError:
            raise NavigationTimeout() from None
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_NAVIGATION_FAILED") from None

    async def inspect_surface(self) -> BrowserSurface:
        self._ensure_alive()
        try:
            contract_id = await self._required_meta("worker-ui-contract")
            contract_version = await self._required_meta("worker-ui-version")
            session_state = await self._required_meta("worker-session-state")
            root = self._page.locator("[data-worker-ui-root]")
            root_count = await root.count()
            if root_count == 0:
                raise LocatorNotFound()
            if root_count != 1:
                raise BrowserContractError()
        except BrowserContractError, LocatorNotFound:
            raise
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_SURFACE_QUERY_FAILED") from None
        return BrowserSurface(
            contract_id=contract_id,
            contract_version=contract_version,
            session_state=session_state,
            required_root_present=True,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        was_dead = (
            self._crashed
            or self._page.is_closed()
            or (self._browser is not None and not self._browser.is_connected())
        )
        close_error = False
        try:
            await self._context.close()
        except PlaywrightError:
            close_error = not was_dead
        finally:
            try:
                await self._playwright.stop()
            except PlaywrightError:
                close_error = close_error or not was_dead
        if close_error:
            raise BrowserProcessCrashed() from None

    async def _required_meta(self, name: str) -> str:
        locator = self._page.locator(f'meta[name="{name}"]')
        count = await locator.count()
        if count == 0:
            raise LocatorNotFound()
        if count != 1:
            raise BrowserContractError()
        value = await locator.get_attribute("content")
        if value is None or not value:
            raise BrowserContractError()
        return value

    def _ensure_alive(self) -> None:
        if (
            self._closed
            or self._crashed
            or self._page.is_closed()
            or (self._browser is not None and not self._browser.is_connected())
        ):
            raise BrowserProcessCrashed()

    def _on_page_crash(self, _: Page) -> None:
        self._crashed = True

    def _on_context_close(self, _: BrowserContext) -> None:
        if not self._closed:
            self._crashed = True


def playwright_proxy_settings(request: BrowserLaunchRequest) -> ProxySettings | None:
    route = request.network_route
    credentials = request.proxy_credentials
    if route.account_id != request.account_id:
        raise BrowserNetworkRouteUnsupported()
    if route.protocol is NetworkProtocol.DIRECT:
        if route.host is not None or route.port is not None or credentials is not None:
            raise BrowserNetworkRouteUnsupported()
        return None
    if not route.host or route.port is None:
        raise BrowserNetworkRouteUnsupported()
    scheme = {
        NetworkProtocol.HTTP: "http",
        NetworkProtocol.HTTPS: "https",
        NetworkProtocol.SOCKS5: "socks5",
    }.get(route.protocol)
    if scheme is None:
        raise BrowserNetworkRouteUnsupported()
    if (
        route.protocol is NetworkProtocol.SOCKS5
        and credentials is not None
        and (credentials.username is not None or credentials.password is not None)
    ):
        raise BrowserNetworkRouteUnsupported()
    server_host = (
        f"[{route.host}]" if ":" in route.host and not route.host.startswith("[") else route.host
    )
    settings: ProxySettings = {"server": f"{scheme}://{server_host}:{route.port}"}
    if credentials is not None:
        if credentials.username is not None:
            settings["username"] = credentials.username
        if credentials.password is not None:
            settings["password"] = credentials.password
    return settings
