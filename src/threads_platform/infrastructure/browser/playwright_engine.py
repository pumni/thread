from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    ProxySettings,
    Request,
    Response,
    Route,
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
    BROWSER_FEED_CANDIDATE_BOUND,
    BROWSER_FEED_ORIGIN,
    BrowserContractError,
    BrowserEngineSession,
    BrowserLaunchRequest,
    BrowserNetworkRouteUnsupported,
    BrowserProcessCrashed,
    BrowserRuntimeUnavailable,
    BrowserSurface,
    FeedAncestorObservation,
    FeedCandidateObservation,
    LocatorNotFound,
    MediaUploadFailed,
    NavigationTimeout,
    PreparedMediaComposer,
    RemoteSessionStateUncertain,
    UnsupportedUIState,
)

_FEED_SCAN_SCRIPT = r"""
({allowedOrigin, ancestorBound, candidateBound}) => {
  if (window.location.origin !== allowedOrigin) {
    return {ok: false, reason: 'origin_mismatch'};
  }
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  if (anchors.length > 1000) return {ok: false};
  const parsePath = (href) => {
    try {
      const url = new URL(href, window.location.href);
      if (url.origin !== allowedOrigin || url.search || url.hash) return null;
      if (/^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}\/?$/.test(url.pathname)) {
        return 'post';
      }
      if (/^\/@[A-Za-z0-9._]{1,30}\/?$/.test(url.pathname)) return 'profile';
    } catch (_) {
      return null;
    }
    return null;
  };
  const candidates = anchors.filter((anchor) => parsePath(anchor.getAttribute('href')) === 'post');
  if (candidates.length > candidateBound) return {ok: false};
  const candidateEvidence = candidates.map((anchor) => {
    const ancestors = [];
    let node = anchor.parentElement;
    while (node && ancestors.length < ancestorBound) {
      const descendantAnchors = Array.from(node.querySelectorAll('a[href]'));
      const semanticHrefs = descendantAnchors
        .map((link) => link.getAttribute('href'))
        .filter((href) => href !== null && parsePath(href) !== null);
      const textNodes = Array.from(
        node.querySelectorAll('span[dir="auto"], div[dir="auto"]')
      );
      ancestors.push({
        hrefs: semanticHrefs.slice(0, 64),
        linksTruncated: semanticHrefs.length > 64,
        textRegions: textNodes.slice(0, 20).map((element) =>
          (element.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 501)
        ),
        textRegionsTruncated: textNodes.length > 20,
      });
      node = node.parentElement;
    }
    return {permalinkHref: anchor.getAttribute('href'), ancestors};
  });
  return {ok: true, candidates: candidateEvidence};
}
"""

_MEDIA_COMPOSER_IS_ACTIVE_SCRIPT = r"""
(dialog) => {
  const activeDialogs = Array.from(document.querySelectorAll('[role="dialog"]'))
    .filter((node) => node.getClientRects().length > 0
      && getComputedStyle(node).visibility !== 'hidden'
      && node.getAttribute('aria-hidden') !== 'true');
  return dialog.isConnected
    && dialog.getAttribute('role') === 'dialog'
    && activeDialogs.length === 1
    && activeDialogs[0] === dialog;
}
"""

_MEDIA_UPLOAD_PATH = re.compile(r"^/rupload_igphoto/fb_uploader_[0-9]+$")
_MEDIA_UPLOAD_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_MEDIA_UPLOAD_DUPLICATE_SETTLE_SECONDS = 0.15


@dataclass(frozen=True, slots=True)
class _PreparedMediaComposerElements:
    dialog: ElementHandle
    file_input: ElementHandle


_THREAD_OPEN_SCRIPT = r"""
({allowedOrigin, targetRef, targetAuthor, ancestorBound}) => {
  const normalizePath = (path) => path.endsWith('/') ? path.slice(0, -1) : path;
  const semanticPath = (href) => {
    if (typeof href !== 'string' || !href.startsWith('/') || href.startsWith('//')) return null;
    if (href.includes('?') || href.includes('#')) return null;
    return normalizePath(href);
  };
  if (window.location.origin !== allowedOrigin) return {outcome: 'uncertain'};
  if (normalizePath(window.location.pathname) !== targetRef) return {outcome: 'uncertain'};

  const anchors = Array.from(document.querySelectorAll('a[href]'));
  if (anchors.length > 1000) return {outcome: 'invalid'};
  const targetAnchors = anchors.filter(
    (anchor) => semanticPath(anchor.getAttribute('href')) === targetRef
  );
  if (targetAnchors.length === 0) return {outcome: 'uncertain'};
  if (targetAnchors.length > 64) return {outcome: 'invalid'};

  const authorPath = `/@${targetAuthor}`;
  const roots = [];
  for (const targetAnchor of targetAnchors) {
    let node = targetAnchor.parentElement;
    let associatedRoot = null;
    for (let depth = 1; node && depth <= ancestorBound; depth += 1) {
      const links = Array.from(node.querySelectorAll('a[href]'));
      if (links.length > 64) return {outcome: 'invalid'};
      let targetLinkCount = 0;
      let hasCompetingPost = false;
      let targetAuthorCount = 0;
      let hasCompetingAuthor = false;
      for (const link of links) {
        const path = semanticPath(link.getAttribute('href'));
        if (path === null) continue;
        if (/^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}\/?$/.test(path)) {
          if (path === targetRef) targetLinkCount += 1;
          else hasCompetingPost = true;
        } else if (/^\/@[A-Za-z0-9._]{1,30}\/?$/.test(path)) {
          if (path === authorPath) targetAuthorCount += 1;
          else hasCompetingAuthor = true;
        }
      }
      if ((hasCompetingPost || hasCompetingAuthor) && targetLinkCount > 0) {
        return {outcome: 'invalid'};
      }
      const textRegions = Array.from(node.querySelectorAll('span[dir="auto"]'));
      if (textRegions.length > 32) return {outcome: 'invalid'};
      const hasBoundedText = textRegions.some((region) => {
        const text = (region.textContent || '').replace(/\s+/g, ' ').trim();
        return text.length > 0 && text.length <= 1000;
      });
      if (targetLinkCount > 0 && targetAuthorCount === 1 && hasBoundedText) {
        associatedRoot = node;
        break;
      }
      node = node.parentElement;
    }
    if (associatedRoot === null) return {outcome: 'invalid'};
    if (roots.length > 0 && roots[0] !== associatedRoot) return {outcome: 'invalid'};
    roots.push(associatedRoot);
  }
  return {outcome: 'recognized'};
}
"""

_PROFILE_OPEN_SCRIPT = r"""
({allowedOrigin, targetRef, ancestorBound}) => {
  const normalizePath = (path) => path.endsWith('/') ? path.slice(0, -1) : path;
  const anchorPath = (href) => {
    if (typeof href !== 'string' || !href.startsWith('/') || href.startsWith('//') ||
        href.includes('?') || href.includes('#')) {
      return null;
    }
    return normalizePath(href);
  };
  const isPostPermalink = (href) => {
    if (typeof href !== 'string' || !href.startsWith('/') || href.startsWith('//')) {
      return false;
    }
    const pathname = href.split(/[?#]/, 1)[0];
    return /^\/@[A-Za-z0-9._]{1,30}\/post\/[A-Za-z0-9_-]{1,120}\/?$/.test(pathname);
  };
  if (window.location.origin !== allowedOrigin) return {outcome: 'uncertain'};
  if (normalizePath(window.location.pathname) !== targetRef) return {outcome: 'uncertain'};

  const anchors = Array.from(document.querySelectorAll('a[href]'));
  if (anchors.length > 1000) return {outcome: 'invalid'};
  const headings = Array.from(document.querySelectorAll('h1'));
  if (headings.length !== 1 || !(headings[0].textContent || '').replace(/\s+/g, ' ').trim()) {
    return {outcome: 'invalid'};
  }
  const exactTargetAnchors = anchors.filter(
    (anchor) => anchorPath(anchor.getAttribute('href')) === targetRef
  );
  if (exactTargetAnchors.length === 0) return {outcome: 'uncertain'};

  let node = headings[0].parentElement;
  let profileHeader = null;
  for (let depth = 1; node && depth <= ancestorBound; depth += 1) {
    if (node.tagName === 'DIV') {
      profileHeader = node;
      break;
    }
    node = node.parentElement;
  }
  if (profileHeader === null) return {outcome: 'invalid'};

  const headerLinks = Array.from(profileHeader.querySelectorAll('a[href]'));
  if (headerLinks.some((link) => isPostPermalink(link.getAttribute('href')))) {
    return {outcome: 'invalid'};
  }
  if (!headerLinks.some((link) => anchorPath(link.getAttribute('href')) === targetRef)) {
    return {outcome: 'invalid'};
  }
  return {outcome: 'recognized'};
}
"""


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
                # The route guard must see every top-level navigation request.
                service_workers="block",
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
        self._allowed_navigation_origins: frozenset[str] = frozenset()
        self._navigation_guard_installed = False
        self._navigation_guard_lock = asyncio.Lock()
        self._blocked_navigation = False
        self._approved_document_loaded = False
        self._remote_state_uncertain = False
        self._navigation_timed_out = False
        self._prepared_media_composers: dict[UUID, _PreparedMediaComposerElements] = {}
        page.on("crash", self._on_page_crash)
        context.on("close", self._on_context_close)

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        self._ensure_alive()
        normalized_allowed_origins = frozenset(
            origin
            for allowed_origin in allowed_origins
            if (origin := _normalized_browser_origin(allowed_origin)) is not None
        )
        if not normalized_allowed_origins:
            raise UnsupportedUIState()
        if self._allowed_navigation_origins and (
            normalized_allowed_origins != self._allowed_navigation_origins
        ):
            raise UnsupportedUIState()
        self._allowed_navigation_origins = normalized_allowed_origins

        try:
            if not self._navigation_guard_installed:
                await self._page.route("**/*", self._guard_navigation)
                self._navigation_guard_installed = True
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._navigation_timeout_ms,
            )
            await self._raise_navigation_policy_error()
        except PlaywrightTimeoutError:
            await self._raise_guard_rejection()
            raise NavigationTimeout() from None
        except PlaywrightError:
            self._ensure_alive()
            await self._raise_guard_rejection()
            raise BrowserRuntimeUnavailable("BROWSER_NAVIGATION_FAILED") from None

    async def _guard_navigation(self, route: Route) -> None:
        request = route.request
        try:
            is_main_navigation = (
                request.is_navigation_request() and request.frame == self._page.main_frame
            )
        except PlaywrightError:
            is_main_navigation = True
        if not is_main_navigation:
            await route.continue_()
            return
        async with self._navigation_guard_lock:
            if _normalized_browser_origin(request.url) not in self._allowed_navigation_origins:
                if self._approved_document_loaded:
                    self._remote_state_uncertain = True
                else:
                    self._blocked_navigation = True
                await route.abort("blockedbyclient")
                return
            try:
                response = await route.fetch(
                    max_redirects=0,
                    timeout=max(1, int(self._navigation_timeout_ms * 0.9)),
                )
            except PlaywrightTimeoutError:
                self._navigation_timed_out = True
                try:
                    await route.abort()
                except PlaywrightError:
                    pass
                return
            except PlaywrightError:
                try:
                    await route.abort()
                except PlaywrightError:
                    pass
                return
            if 300 <= response.status < 400:
                self._remote_state_uncertain = True
                await route.abort("blockedbyclient")
                return
            self._approved_document_loaded = True
            await route.fulfill(response=response)

    async def _raise_navigation_policy_error(self) -> None:
        await self._raise_guard_rejection()
        async with self._navigation_guard_lock:
            if (
                self._allowed_navigation_origins
                and _normalized_browser_origin(self._page.url)
                not in self._allowed_navigation_origins
            ):
                raise RemoteSessionStateUncertain()

    async def _raise_guard_rejection(self) -> None:
        async with self._navigation_guard_lock:
            if self._blocked_navigation:
                raise UnsupportedUIState()
            if self._remote_state_uncertain:
                raise RemoteSessionStateUncertain()
            if self._navigation_timed_out:
                raise NavigationTimeout()

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

    async def collect_feed_candidates(
        self, *, ancestor_bound: int
    ) -> tuple[FeedCandidateObservation, ...]:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        if not 1 <= ancestor_bound <= 12:
            raise BrowserContractError()
        try:
            payload = await self._page.evaluate(
                _FEED_SCAN_SCRIPT,
                {
                    "allowedOrigin": BROWSER_FEED_ORIGIN,
                    "ancestorBound": ancestor_bound,
                    "candidateBound": BROWSER_FEED_CANDIDATE_BOUND,
                },
            )
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_FEED_INSPECTION_FAILED") from None
        return _feed_candidate_observations(payload)

    async def verify_thread_target(
        self, *, target_ref: str, author_username: str, ancestor_bound: int
    ) -> None:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        target_match = re.fullmatch(
            r"/@([A-Za-z0-9._]{1,30})/post/[A-Za-z0-9_-]{1,120}", target_ref
        )
        if (
            not 1 <= ancestor_bound <= 8
            or target_match is None
            or target_match.group(1) != author_username
        ):
            raise BrowserContractError()
        try:
            payload = await self._page.evaluate(
                _THREAD_OPEN_SCRIPT,
                {
                    "allowedOrigin": BROWSER_FEED_ORIGIN,
                    "targetRef": target_ref,
                    "targetAuthor": author_username,
                    "ancestorBound": ancestor_bound,
                },
            )
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_THREAD_INSPECTION_FAILED") from None
        _verify_thread_target_result(payload)

    async def verify_profile_target(self, *, target_ref: str, ancestor_bound: int) -> None:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        if (
            not 1 <= ancestor_bound <= 8
            or re.fullmatch(r"/@[A-Za-z0-9._]{1,30}", target_ref) is None
        ):
            raise BrowserContractError()
        try:
            payload = await self._page.evaluate(
                _PROFILE_OPEN_SCRIPT,
                {
                    "allowedOrigin": BROWSER_FEED_ORIGIN,
                    "targetRef": target_ref,
                    "ancestorBound": ancestor_bound,
                },
            )
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_PROFILE_INSPECTION_FAILED") from None
        _verify_profile_target_result(payload)

    async def prepare_media_composer(self) -> PreparedMediaComposer | None:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        if _normalized_browser_origin(self._page.url) != BROWSER_FEED_ORIGIN:
            raise RemoteSessionStateUncertain()
        try:
            dialogs = self._page.locator('[role="dialog"]').filter(visible=True)
            dialog_count = await dialogs.count()
            if dialog_count == 0:
                return None
            if dialog_count != 1:
                raise BrowserContractError()
            dialog = dialogs
            if await dialog.locator('[role="textbox"]').count() != 1:
                raise BrowserContractError()
            file_inputs = self._page.locator('input[type="file"]')
            if await file_inputs.count() != 1:
                raise BrowserContractError()
            file_input = dialog.locator('input[type="file"]')
            if await file_input.count() != 1:
                raise BrowserContractError()
            accept = await file_input.get_attribute("accept")
            if not _accepts_reviewed_images(accept):
                raise BrowserContractError()
            dialog_handle = await dialog.element_handle()
            file_input_handle = await file_input.element_handle()
        except BrowserContractError:
            raise
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_MEDIA_COMPOSER_INSPECTION_FAILED") from None

        token = uuid4()
        self._prepared_media_composers[token] = _PreparedMediaComposerElements(
            dialog_handle,
            file_input_handle,
        )
        return PreparedMediaComposer(token)

    async def stage_local_media(self, composer: PreparedMediaComposer, file_path: Path) -> None:
        elements = self._prepared_media_composers.pop(composer.token, None)
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        if elements is None:
            raise BrowserContractError()
        try:
            current_origin = _normalized_browser_origin(self._page.url)
            if current_origin != BROWSER_FEED_ORIGIN:
                raise RemoteSessionStateUncertain()
            if not await elements.dialog.evaluate(_MEDIA_COMPOSER_IS_ACTIVE_SCRIPT):
                raise BrowserContractError()
            file_input_connected = await elements.file_input.evaluate(
                "(node) => node.isConnected && node.matches('input[type=\"file\"]')"
            )
            if not file_input_connected:
                raise BrowserContractError()

            matching_requests: list[Request] = []
            matching_responses: list[Response] = []
            finished_requests: list[Request] = []
            request_finished = asyncio.Event()
            selection_started = False

            def on_request(request: Request) -> None:
                if selection_started and _media_upload_endpoint(request.url) is not None:
                    matching_requests.append(request)

            def on_response(response: Response) -> None:
                if selection_started and _media_upload_endpoint(response.url) is not None:
                    matching_responses.append(response)

            def on_request_finished(request: Request) -> None:
                if selection_started and _media_upload_endpoint(request.url) is not None:
                    finished_requests.append(request)
                    request_finished.set()

            self._page.on("request", on_request)
            self._page.on("response", on_response)
            self._page.on("requestfinished", on_request_finished)
            try:
                selection_started = True
                await elements.file_input.set_input_files(
                    str(file_path), timeout=self._navigation_timeout_ms
                )
                try:
                    await asyncio.wait_for(
                        request_finished.wait(), timeout=self._navigation_timeout_ms / 1000
                    )
                except TimeoutError:
                    raise MediaUploadFailed() from None

                try:
                    await asyncio.sleep(_MEDIA_UPLOAD_DUPLICATE_SETTLE_SECONDS)
                except PlaywrightTimeoutError:
                    raise MediaUploadFailed() from None

                await self._raise_navigation_policy_error()
                if (
                    len(matching_requests) != 1
                    or len(matching_responses) != 1
                    or len(finished_requests) != 1
                ):
                    raise MediaUploadFailed()
                request = matching_requests[0]
                finished_request = finished_requests[0]
                response = matching_responses[0]
                request_endpoint = _media_upload_endpoint(request.url)
                response_endpoint = _media_upload_endpoint(response.url)
                response_request_endpoint = _media_upload_endpoint(response.request.url)
                finished_endpoint = _media_upload_endpoint(finished_request.url)
                if (
                    request.method != "POST"
                    or request_endpoint is None
                    or request_endpoint[0] != current_origin
                    or finished_request.method != request.method
                    or finished_endpoint != request_endpoint
                    or response_endpoint != request_endpoint
                    or response_request_endpoint != request_endpoint
                    or response.request.method != request.method
                    or response.request.url != finished_request.url
                    or response.request.method != "POST"
                    or response.status != 200
                ):
                    raise MediaUploadFailed()

                same_dialog = await elements.dialog.evaluate(_MEDIA_COMPOSER_IS_ACTIVE_SCRIPT)
                preview_count = await elements.dialog.evaluate(
                    "(dialog) => dialog.querySelectorAll('img[src^=\"blob:\"]').length"
                )
                if not same_dialog or not isinstance(preview_count, int) or preview_count < 1:
                    raise MediaUploadFailed()
            finally:
                self._page.remove_listener("request", on_request)
                self._page.remove_listener("response", on_response)
                self._page.remove_listener("requestfinished", on_request_finished)
        except BrowserContractError, MediaUploadFailed, RemoteSessionStateUncertain:
            raise
        except PlaywrightTimeoutError:
            self._ensure_alive()
            raise MediaUploadFailed() from None
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_MEDIA_UPLOAD_FAILED") from None

    async def discard_media_composer(self, composer: PreparedMediaComposer) -> None:
        elements = self._prepared_media_composers.pop(composer.token, None)
        if elements is None:
            return
        for handle in (elements.file_input, elements.dialog):
            try:
                await handle.dispose()
            except PlaywrightError:
                pass

    async def scroll_feed(self) -> None:
        self._ensure_alive()
        await self._raise_navigation_policy_error()
        try:
            await self._page.evaluate("() => window.scrollBy(0, Math.max(window.innerHeight, 1))")
        except PlaywrightError:
            self._ensure_alive()
            raise BrowserRuntimeUnavailable("BROWSER_FEED_SCROLL_FAILED") from None

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


def _feed_candidate_observations(payload: object) -> tuple[FeedCandidateObservation, ...]:
    if not isinstance(payload, dict):
        raise BrowserContractError()
    data = cast(dict[str, object], payload)
    if data.get("reason") == "origin_mismatch":
        raise RemoteSessionStateUncertain()
    candidate_value = data.get("candidates")
    if data.get("ok") is not True or not isinstance(candidate_value, list):
        raise BrowserContractError()
    raw_candidates = cast(list[object], candidate_value)
    if not raw_candidates:
        raise RemoteSessionStateUncertain()

    candidates: list[FeedCandidateObservation] = []
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            raise BrowserContractError()
        candidate = cast(dict[str, object], raw_candidate)
        permalink_href = candidate.get("permalinkHref")
        ancestor_value = candidate.get("ancestors")
        if not isinstance(permalink_href, str) or not isinstance(ancestor_value, list):
            raise BrowserContractError()
        raw_ancestors = cast(list[object], ancestor_value)
        ancestors: list[FeedAncestorObservation] = []
        for raw_ancestor in raw_ancestors:
            if not isinstance(raw_ancestor, dict):
                raise BrowserContractError()
            ancestor = cast(dict[str, object], raw_ancestor)
            href_values = ancestor.get("hrefs")
            text_values = ancestor.get("textRegions")
            links_truncated = ancestor.get("linksTruncated")
            text_regions_truncated = ancestor.get("textRegionsTruncated")
            if (
                not isinstance(href_values, list)
                or not isinstance(text_values, list)
                or not isinstance(links_truncated, bool)
                or not isinstance(text_regions_truncated, bool)
            ):
                raise BrowserContractError()
            hrefs = cast(list[object], href_values)
            text_regions = cast(list[object], text_values)
            if not all(isinstance(href, str) for href in hrefs) or not all(
                isinstance(text, str) for text in text_regions
            ):
                raise BrowserContractError()
            ancestors.append(
                FeedAncestorObservation(
                    hrefs=tuple(cast(str, href) for href in hrefs),
                    text_regions=tuple(cast(str, text) for text in text_regions),
                    links_truncated=links_truncated,
                    text_regions_truncated=text_regions_truncated,
                )
            )
        candidates.append(
            FeedCandidateObservation(permalink_href=permalink_href, ancestors=tuple(ancestors))
        )
    return tuple(candidates)


def _verify_thread_target_result(payload: object) -> None:
    if not isinstance(payload, dict):
        raise BrowserContractError()
    data = cast(dict[str, object], payload)
    if set(data) != {"outcome"}:
        raise BrowserContractError()
    outcome = data.get("outcome")
    if outcome == "uncertain":
        raise RemoteSessionStateUncertain()
    if outcome != "recognized":
        raise BrowserContractError()


def _verify_profile_target_result(payload: object) -> None:
    if not isinstance(payload, dict):
        raise BrowserContractError()
    data = cast(dict[str, object], payload)
    if set(data) != {"outcome"}:
        raise BrowserContractError()
    outcome = data.get("outcome")
    if outcome == "uncertain":
        raise RemoteSessionStateUncertain()
    if outcome != "recognized":
        raise BrowserContractError()


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


def _normalized_browser_origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    hostname = hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return f"{scheme}://{hostname}"
    return f"{scheme}://{hostname}:{port}"


def _accepts_reviewed_images(value: str | None) -> bool:
    if value is None:
        return False
    accepted = {item.strip().casefold() for item in value.split(",") if item.strip()}
    return "image/*" in accepted or _MEDIA_UPLOAD_MIME_TYPES.issubset(accepted)


def _media_upload_endpoint(url: str) -> tuple[str | None, str] | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if _MEDIA_UPLOAD_PATH.fullmatch(parsed.path) is None:
        return None
    return _normalized_browser_origin(url), parsed.path
