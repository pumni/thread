from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Iterator
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest

import threads_platform.infrastructure.browser.playwright_engine as playwright_engine
from threads_platform.application.ports.browser import (
    BrowserAdapterError,
    BrowserContractError,
    BrowserEngineSession,
    BrowserFeedEngineSession,
    BrowserLaunchRequest,
    BrowserMediaEngineSession,
    BrowserNetworkProtocol,
    BrowserNetworkRoute,
    BrowserNetworkRouteUnsupported,
    BrowserProcessCrashed,
    BrowserProfileOpenEngineSession,
    BrowserProxyCredentials,
    BrowserSurface,
    BrowserThreadOpenEngineSession,
    ChallengeDetected,
    FeedCandidateObservation,
    LocatorNotFound,
    MediaUploadFailed,
    NavigationTimeout,
    RemoteSessionStateUncertain,
    SessionExpired,
    UnsupportedUIState,
)
from threads_platform.application.ports.worker_agent import (
    LocalRecoveryEntry,
    LocalSessionState,
    WorkerAccountContext,
    WorkerControlClientError,
    WorkerJobCancelSnapshot,
    WorkerJobSnapshot,
)
from threads_platform.domain.worker_jobs import WorkerJobRetrySafety, WorkerJobStatus
from threads_platform.domain.workers import BrowserSessionState, NetworkProfile, NetworkProtocol
from threads_platform.infrastructure.browser.playwright_engine import (
    PlaywrightBrowserEngine,
)
from threads_platform.infrastructure.worker_agent.local_state import (
    LocalDataRoot,
    LocalProfileDirectoryResolver,
    WorkerLocalStateStore,
)
from threads_platform.workers.browser import (
    ActionOutcomeAmbiguous,
    BrowserAccountAffinityMismatch,
    BrowserNavigationPolicy,
    BrowserSurfaceState,
    ManagedPlaywrightBrowserSessionManager,
    PlaywrightBrowserAdapter,
    WorkerBrowserSession,
    WorkerJobExecution,
    WorkerJobLeaseLost,
    WorkerJobReconnectRecovery,
    WorkerJobRetrySafetyViolation,
    classify_browser_surface,
)
from threads_platform.workers.feed_browse import normalize_feed_candidates
from threads_platform.workers.sessions import (
    BrowserSessionOpenResult,
    LocalBrowserSessionManager,
    WorkerNetworkRoute,
)


@pytest.fixture
def synthetic_redirect_target_requests() -> list[str]:
    return []


@pytest.fixture
def synthetic_upload_count() -> list[int]:
    return []


def test_shared_browser_contract_shapes_are_topology_neutral() -> None:
    assert tuple(field.name for field in fields(BrowserLaunchRequest)) == (
        "profile_directory",
        "network_route",
        "proxy_credentials",
        "headless",
    )
    assert tuple(field.name for field in fields(BrowserNetworkRoute)) == (
        "protocol",
        "host",
        "port",
    )
    assert tuple(field.name for field in fields(BrowserProxyCredentials)) == (
        "username",
        "password",
    )
    assert not hasattr(BrowserEngineSession, "inspect_surface")


@pytest.fixture
def synthetic_origin(
    synthetic_redirect_target_requests: list[str], synthetic_upload_count: list[int]
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/slow":
                time.sleep(1.5)
            if path == "/redirect-out":
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://localhost:{server.server_address[1]}/redirect-target",
                )
                self.end_headers()
                return
            if path == "/redirect-target":
                synthetic_redirect_target_requests.append(path)
            if path == "/delayed-redirect":
                body = (
                    "<html><body><script>setTimeout(() => {"
                    f"window.location.href = 'http://localhost:{server.server_address[1]}"
                    "/redirect-target'; }, 20);</script></body></html>"
                ).encode()
            else:
                body = _SYNTHETIC_DOCUMENTS.get(
                    path,
                    _SYNTHETIC_DOCUMENTS.get(path.rstrip("/"), _document("AUTHENTICATED")),
                )
            if b"__OTHER_ORIGIN__" in body:
                other_origin = f"http://localhost:{server.server_address[1]}".encode()
                body = body.replace(b"__OTHER_ORIGIN__", other_origin)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                return

        def do_POST(self) -> None:
            synthetic_upload_count.append(1)
            time.sleep(0.25)
            status = 503 if urlsplit(self.path).path.endswith("_503") else 200
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"ok")
            self.close_connection = True

        def log_message(self, format: str, *args: object) -> None:
            _ = (format, args)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture
def short_managed_profile_tmp_path() -> Iterator[Path]:
    # Keep Chromium's nested profile/cache paths within Windows path limits.
    with tempfile.TemporaryDirectory(prefix="bp-") as directory:
        yield Path(directory)


def test_playwright_managed_profile_contract_navigation_and_cleanup(
    short_managed_profile_tmp_path: Path,
    synthetic_origin: str,
) -> None:
    async def scenario() -> None:
        worker_id = uuid4()
        account_id = uuid4()
        context, manager, store, resolver, opened = await _managed_session(
            short_managed_profile_tmp_path,
            worker_id,
            account_id,
            "logical-profile",
            open_session=False,
        )
        adapter = PlaywrightBrowserAdapter(
            worker_id,
            resolver,
            PlaywrightBrowserEngine(navigation_timeout_ms=1_000),
        )
        managed_manager = ManagedPlaywrightBrowserSessionManager(
            manager,
            adapter,
            headless=True,
        )
        profile_directory = resolver.resolve(worker_id, account_id, "logical-profile")
        policy = _local_policy(synthetic_origin)

        try:
            opened = await managed_manager.open(context)
            session = managed_manager.browser_session(account_id)
            assert session is not None
            assert opened.state.state is BrowserSessionState.LOGIN_REQUIRED

            await session.navigate(f"{synthetic_origin}/authenticated", policy)
            assert await session.inspect_contract() is BrowserSurfaceState.AUTHENTICATED
            assert profile_directory.is_dir()

            await session.navigate(f"{synthetic_origin}/unknown", policy)
            with pytest.raises(UnsupportedUIState) as unknown:
                await session.inspect_contract()
            assert str(unknown.value) == "UNSUPPORTED_UI_STATE"
            current = store.get_session(account_id)
            assert current is not None and current.state is BrowserSessionState.ERROR

            await session.navigate(f"{synthetic_origin}/missing", policy)
            with pytest.raises(LocatorNotFound):
                await session.inspect_contract()

            with pytest.raises(NavigationTimeout):
                await session.navigate(f"{synthetic_origin}/slow", policy)
        finally:
            current = store.get_session(account_id)
            if current is not None and current.state is not BrowserSessionState.STOPPED:
                await managed_manager.close(account_id)

        assert store.active_session_count() == 0
        current = store.get_session(account_id)
        assert current is not None and current.state is BrowserSessionState.STOPPED
        # Assert persisted profile data only after graceful context closure.
        assert profile_directory.is_dir()
        assert list(profile_directory.iterdir())

    asyncio.run(scenario())


def test_playwright_redirect_requests_intervention_without_following_target(
    tmp_path: Path,
    synthetic_origin: str,
    synthetic_redirect_target_requests: list[str],
) -> None:
    async def scenario() -> None:
        profile_directory = tmp_path / "redirect-profile"
        profile_directory.mkdir()
        session = await PlaywrightBrowserEngine(navigation_timeout_ms=5_000).open(
            BrowserLaunchRequest(
                profile_directory=profile_directory,
                network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                headless=True,
            )
        )
        try:
            with pytest.raises(RemoteSessionStateUncertain):
                await session.navigate(
                    f"{synthetic_origin}/redirect-out",
                    allowed_origins=frozenset({synthetic_origin}),
                )
        finally:
            await session.close()
        assert synthetic_redirect_target_requests == []

    asyncio.run(scenario())


def test_playwright_media_upload_waits_for_matching_response_and_same_dialog_preview(
    tmp_path: Path,
    synthetic_origin: str,
    synthetic_upload_count: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(playwright_engine, "BROWSER_FEED_ORIGIN", synthetic_origin)
        profile_directory = tmp_path / "media-profile"
        profile_directory.mkdir()
        file_path = tmp_path / "sample.png"
        file_path.write_bytes(b"synthetic-image")
        engine_session = cast(
            BrowserMediaEngineSession,
            await PlaywrightBrowserEngine(navigation_timeout_ms=2_000).open(
                BrowserLaunchRequest(
                    profile_directory=profile_directory,
                    network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                    headless=True,
                )
            ),
        )
        page = cast(Any, engine_session)._page  # pyright: ignore[reportPrivateUsage]
        try:
            await engine_session.navigate(
                f"{synthetic_origin}/media-composer",
                allowed_origins=frozenset({synthetic_origin}),
            )
            composer = await engine_session.prepare_media_composer()
            assert composer is not None
            stage = asyncio.create_task(engine_session.stage_local_media(composer, file_path))
            await page.wait_for_function(
                "document.querySelector('img')?.getAttribute('src')?.startsWith('blob:')",
                timeout=2_000,
            )
            assert not stage.done()
            started_at = asyncio.get_running_loop().time()
            await stage
            assert asyncio.get_running_loop().time() - started_at >= 0.1
            assert await page.evaluate("window.unsafeActionCount") == 0
        finally:
            await engine_session.close()

    asyncio.run(scenario())
    assert synthetic_upload_count == [1]


@pytest.mark.parametrize(
    "path",
    [
        "/media-composer-no-response",
        "/media-composer-duplicate",
        "/media-composer-non-200",
        "/media-composer-wrong-method",
        "/media-composer-off-origin",
        "/media-composer-extended-path",
    ],
)
def test_playwright_media_upload_requires_one_exact_successful_response(
    tmp_path: Path,
    synthetic_origin: str,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(playwright_engine, "BROWSER_FEED_ORIGIN", synthetic_origin)
        profile_directory = tmp_path / f"profile-{uuid4()}"
        profile_directory.mkdir()
        file_path = tmp_path / "sample.png"
        file_path.write_bytes(b"synthetic-image")
        engine_session = cast(
            BrowserMediaEngineSession,
            await PlaywrightBrowserEngine(navigation_timeout_ms=2_000).open(
                BrowserLaunchRequest(
                    profile_directory=profile_directory,
                    network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                    headless=True,
                )
            ),
        )
        try:
            await engine_session.navigate(
                f"{synthetic_origin}{path}",
                allowed_origins=frozenset({synthetic_origin}),
            )
            composer = await engine_session.prepare_media_composer()
            assert composer is not None
            with pytest.raises(MediaUploadFailed):
                await engine_session.stage_local_media(composer, file_path)
        finally:
            await engine_session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("path", "expected_missing"),
    [
        ("/media-no-composer", True),
        ("/media-multiple-dialogs", False),
        ("/media-file-input-outside", False),
        ("/media-unsupported-accept", False),
    ],
)
def test_playwright_media_composer_precondition_is_exact_and_bounded(
    tmp_path: Path,
    synthetic_origin: str,
    path: str,
    expected_missing: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(playwright_engine, "BROWSER_FEED_ORIGIN", synthetic_origin)
        profile_directory = tmp_path / f"profile-{uuid4()}"
        profile_directory.mkdir()
        engine_session = cast(
            BrowserMediaEngineSession,
            await PlaywrightBrowserEngine(navigation_timeout_ms=2_000).open(
                BrowserLaunchRequest(
                    profile_directory=profile_directory,
                    network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                    headless=True,
                )
            ),
        )
        page = cast(Any, engine_session)._page  # pyright: ignore[reportPrivateUsage]
        try:
            await engine_session.navigate(
                f"{synthetic_origin}{path}",
                allowed_origins=frozenset({synthetic_origin}),
            )
            if path == "/media-file-input-outside":
                assert await page.evaluate(
                    "[document.querySelectorAll('[role=dialog]').length, "
                    "document.querySelector('[role=dialog]')."
                    "querySelectorAll('input[type=file]').length, "
                    "document.querySelectorAll('input[type=file]').length]"
                ) == [1, 0, 1]
            if expected_missing:
                assert await engine_session.prepare_media_composer() is None
            else:
                with pytest.raises(BrowserContractError):
                    await engine_session.prepare_media_composer()
        finally:
            await engine_session.close()

    asyncio.run(scenario())


def test_page_initiated_off_origin_navigation_blocks_thread_inspection_after_load(
    tmp_path: Path,
    synthetic_origin: str,
    synthetic_redirect_target_requests: list[str],
) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        context, manager, _, resolver, _ = await _managed_session(
            tmp_path,
            worker_id,
            account_id,
            "delayed-redirect-profile",
            open_session=False,
        )
        reserved = await manager.open(context)
        await manager.transition(account_id, BrowserSessionState.STARTING)
        authenticated = await manager.transition(account_id, BrowserSessionState.AUTHENTICATED)
        opened = replace(reserved, state=authenticated)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(job, worker_id, client)
        adapter = PlaywrightBrowserAdapter(
            worker_id,
            resolver,
            PlaywrightBrowserEngine(navigation_timeout_ms=5_000),
        )
        session = await adapter.open_reserved_session(
            context,
            opened,
            transition=manager.transition,
            close_session=manager.close,
            job_execution=execution,
            headless=True,
        )
        try:
            await session.navigate(
                f"{synthetic_origin}/delayed-redirect",
                _local_policy(synthetic_origin),
            )
            await asyncio.sleep(0.3)
            with pytest.raises(RemoteSessionStateUncertain):
                await session.verify_thread_target(
                    target_ref="/@alice/post/post-1",
                    author_username="alice",
                    ancestor_bound=8,
                )
        finally:
            await session.close()
        assert client.snapshot.status is WorkerJobStatus.WAITING_INTERVENTION
        assert client.interventions == [("REMOTE_STATE_UNCERTAIN", "REMOTE_STATE_UNCERTAIN")]
        assert synthetic_redirect_target_requests == []

    asyncio.run(scenario())


def test_playwright_feed_without_reviewed_permalink_evidence_is_uncertain(
    tmp_path: Path,
    synthetic_origin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(playwright_engine, "BROWSER_FEED_ORIGIN", synthetic_origin)
        profile_directory = tmp_path / "session-transition-profile"
        profile_directory.mkdir()
        session = await PlaywrightBrowserEngine().open(
            BrowserLaunchRequest(
                profile_directory=profile_directory,
                network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                headless=True,
            )
        )
        try:
            await session.navigate(
                f"{synthetic_origin}/login",
                allowed_origins=frozenset({synthetic_origin}),
            )
            with pytest.raises(RemoteSessionStateUncertain):
                await cast(BrowserFeedEngineSession, session).collect_feed_candidates(
                    ancestor_bound=8
                )
        finally:
            await session.close()

    asyncio.run(scenario())


def test_playwright_feed_scan_uses_reviewed_semantic_markers_only(
    tmp_path: Path,
    synthetic_origin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(playwright_engine, "BROWSER_FEED_ORIGIN", synthetic_origin)
        profile_directory = tmp_path / "feed-profile"
        profile_directory.mkdir()
        session = await PlaywrightBrowserEngine().open(
            BrowserLaunchRequest(
                profile_directory=profile_directory,
                network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                headless=True,
            )
        )
        await session.navigate(
            f"{synthetic_origin}/feed", allowed_origins=frozenset({synthetic_origin})
        )
        candidates = await cast(BrowserFeedEngineSession, session).collect_feed_candidates(
            ancestor_bound=8
        )
        normalized = normalize_feed_candidates(candidates, max_items=5)
        await session.close()

        assert len(normalized) == 1
        assert normalized[0].thread_ref == "https://www.threads.com/@alice/post/post-1"
        assert normalized[0].text_excerpt == "Synthetic public text"
        assert candidates[0].ancestors[0].text_regions == ("Synthetic public text",)

    asyncio.run(scenario())


def test_playwright_thread_open_uses_exact_permalink_author_and_bounded_root(
    tmp_path: Path,
    synthetic_origin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def verify(
        path: str,
        target_ref: str,
        *,
        error: type[Exception] | None = None,
    ) -> None:
        monkeypatch.setattr(playwright_engine, "BROWSER_FEED_ORIGIN", synthetic_origin)
        profile_directory = tmp_path / f"thread-{uuid4()}"
        profile_directory.mkdir()
        session = await PlaywrightBrowserEngine(navigation_timeout_ms=1_000).open(
            BrowserLaunchRequest(
                profile_directory=profile_directory,
                network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                headless=True,
            )
        )
        try:
            await session.navigate(
                f"{synthetic_origin}{path}", allowed_origins=frozenset({synthetic_origin})
            )
            engine_session = cast(BrowserThreadOpenEngineSession, session)
            if error is None:
                assert (
                    await engine_session.verify_thread_target(
                        target_ref=target_ref,
                        author_username="alice",
                        ancestor_bound=8,
                    )
                    is None
                )
            else:
                with pytest.raises(error):
                    await engine_session.verify_thread_target(
                        target_ref=target_ref,
                        author_username="alice",
                        ancestor_bound=8,
                    )
        finally:
            await session.close()

    async def scenario() -> None:
        target = "/@alice/post/post-1"
        await verify(target, target)
        await verify("/@alice/post/post-duplicate/", "/@alice/post/post-duplicate")
        await verify(
            "/@alice/post/post-duplicate-author",
            "/@alice/post/post-duplicate-author",
            error=BrowserContractError,
        )
        await verify(
            "/@alice/post/post-competing",
            "/@alice/post/post-competing",
            error=BrowserContractError,
        )
        await verify(
            "/@alice/post/post-reply-cross",
            "/@alice/post/post-reply-cross",
            error=BrowserContractError,
        )
        await verify(
            "/@alice/post/post-over-bound",
            "/@alice/post/post-over-bound",
            error=BrowserContractError,
        )
        await verify(
            "/@alice/post/post-author-mismatch",
            "/@alice/post/post-author-mismatch",
            error=BrowserContractError,
        )
        await verify(
            "/@alice/post/post-no-anchor",
            "/@alice/post/post-no-anchor",
            error=RemoteSessionStateUncertain,
        )
        await verify(
            "/@alice/post/post-prefix-extra",
            "/@alice/post/post-prefix",
            error=RemoteSessionStateUncertain,
        )
        await verify(
            "/@alice/post/post-prefix-anchor",
            "/@alice/post/post-prefix-anchor",
            error=RemoteSessionStateUncertain,
        )

    asyncio.run(scenario())


def test_playwright_profile_open_uses_unique_bounded_header_association(
    tmp_path: Path,
    synthetic_origin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def verify(
        path: str,
        target_ref: str,
        *,
        error: type[Exception] | None = None,
        ancestor_bound: int = 8,
        expected_origin: str | None = None,
    ) -> None:
        monkeypatch.setattr(playwright_engine, "BROWSER_FEED_ORIGIN", synthetic_origin)
        profile_directory = tmp_path / f"profile-open-{uuid4()}"
        profile_directory.mkdir()
        session = await PlaywrightBrowserEngine(navigation_timeout_ms=1_000).open(
            BrowserLaunchRequest(
                profile_directory=profile_directory,
                network_route=BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
                headless=True,
            )
        )
        try:
            await session.navigate(
                f"{synthetic_origin}{path}", allowed_origins=frozenset({synthetic_origin})
            )
            if expected_origin is not None:
                monkeypatch.setattr(playwright_engine, "BROWSER_FEED_ORIGIN", expected_origin)
            engine_session = cast(BrowserProfileOpenEngineSession, session)
            if error is None:
                assert (
                    await engine_session.verify_profile_target(
                        target_ref=target_ref,
                        ancestor_bound=ancestor_bound,
                    )
                    is None
                )
            else:
                with pytest.raises(error):
                    await engine_session.verify_profile_target(
                        target_ref=target_ref,
                        ancestor_bound=ancestor_bound,
                    )
        finally:
            await session.close()

    async def scenario() -> None:
        target = "/@alice"
        await verify(target, target)
        await verify("/@duph1", "/@duph1")
        await verify("/@ambiguousheaders", "/@ambiguousheaders", error=BrowserContractError)
        await verify("/@headingoverflow", "/@headingoverflow", error=BrowserContractError)
        await verify(
            "/@missingh1",
            "/@missingh1",
            error=BrowserContractError,
        )
        await verify(
            "/@contaminated",
            "/@contaminated",
            error=BrowserContractError,
        )
        await verify(
            "/@postonly",
            "/@postonly",
            error=BrowserContractError,
        )
        await verify(
            "/@noevidence",
            "/@noevidence",
            error=RemoteSessionStateUncertain,
        )
        await verify(
            "/@outsideheader",
            "/@outsideheader",
            error=BrowserContractError,
        )
        await verify(
            "/@queryhref",
            "/@queryhref",
            error=RemoteSessionStateUncertain,
        )
        await verify(
            "/@alice",
            "/@alice",
            error=RemoteSessionStateUncertain,
            expected_origin="https://www.threads.com",
        )
        await verify(
            "/@overbound",
            "/@overbound",
            error=BrowserContractError,
        )
        await verify("/@alice/extended", target, error=RemoteSessionStateUncertain)

    asyncio.run(scenario())


def test_synthetic_session_states_report_durable_interventions(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_id = uuid4()
        account_id = uuid4()
        context, manager, store, resolver, _ = await _managed_session(
            tmp_path,
            worker_id,
            account_id,
            "session-profile",
            open_session=False,
        )
        engine = _MemoryBrowserEngine()
        adapter = PlaywrightBrowserAdapter(worker_id, resolver, engine)
        policy = _local_policy("http://127.0.0.1:41000")
        outcomes = (
            ("LOGIN_REQUIRED", BrowserSessionState.LOGIN_REQUIRED, None),
            ("SESSION_EXPIRED", BrowserSessionState.SESSION_EXPIRED, SessionExpired),
            ("CHALLENGE_REQUIRED", BrowserSessionState.CHALLENGE_REQUIRED, ChallengeDetected),
        )
        for surface_state, expected_state, expected_error in outcomes:
            opened = await manager.open(context)
            job = _running_job(worker_id, account_id)
            client = _MemoryWorkerJobControl(worker_id, account_id, job)
            execution = WorkerJobExecution(job, worker_id, client)
            engine.surface = _surface(surface_state)
            session = await adapter.open_reserved_session(
                context,
                opened,
                transition=manager.transition,
                close_session=manager.close,
                job_execution=execution,
                headless=True,
            )
            await session.navigate("http://127.0.0.1:41000/test", policy)
            if expected_error is None:
                assert await session.inspect_contract() is BrowserSurfaceState.LOGIN_REQUIRED
            else:
                with pytest.raises(expected_error):
                    await session.inspect_contract()
            state = store.get_session(account_id)
            assert state is not None and state.state is expected_state
            assert client.interventions[-1] == (expected_state.value, expected_state.value)
            await session.close()

    asyncio.run(scenario())


def test_session_transition_after_navigation_records_durable_intervention(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        context, manager, _, resolver, _ = await _managed_session(
            tmp_path,
            worker_id,
            account_id,
            "transition-profile",
            open_session=False,
        )
        reserved = await manager.open(context)
        await manager.transition(account_id, BrowserSessionState.STARTING)
        authenticated = await manager.transition(account_id, BrowserSessionState.AUTHENTICATED)
        opened = replace(reserved, state=authenticated)
        engine = _MemoryBrowserEngine(remote_uncertain_on_collect=True)
        adapter = PlaywrightBrowserAdapter(worker_id, resolver, engine)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(job, worker_id, client)
        session = await adapter.open_reserved_session(
            context,
            opened,
            transition=manager.transition,
            close_session=manager.close,
            job_execution=execution,
            headless=True,
        )

        await session.navigate(
            "http://127.0.0.1:41000/feed", _local_policy("http://127.0.0.1:41000")
        )
        with pytest.raises(RemoteSessionStateUncertain):
            await session.collect_feed_candidates(ancestor_bound=8)

        assert client.snapshot.status is WorkerJobStatus.WAITING_INTERVENTION
        assert client.interventions == [("REMOTE_STATE_UNCERTAIN", "REMOTE_STATE_UNCERTAIN")]
        assert engine.sessions[0].collect_count == 1
        assert engine.sessions[0].scroll_count == 0
        await session.close()

    asyncio.run(scenario())


def test_pending_cancellation_on_renew_stops_before_the_next_browser_action(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        context, manager, _, resolver, reserved = await _managed_session(
            tmp_path,
            worker_id,
            account_id,
            "cancel-profile",
            open_session=False,
        )
        reserved = await manager.open(context)
        await manager.transition(account_id, BrowserSessionState.STARTING)
        authenticated = await manager.transition(account_id, BrowserSessionState.AUTHENTICATED)
        opened = replace(reserved, state=authenticated)
        job = replace(
            _running_job(worker_id, account_id),
            capability_name="threads.browser.feed.browse",
        )
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(job, worker_id, client)
        engine = _MemoryBrowserEngine()
        adapter = PlaywrightBrowserAdapter(worker_id, resolver, engine)
        session = await adapter.open_reserved_session(
            context,
            opened,
            transition=manager.transition,
            close_session=manager.close,
            job_execution=execution,
            headless=True,
        )

        await execution.checkpoint({"phase": "BEFORE_NAVIGATION"})
        cancel_request = WorkerJobCancelSnapshot(
            request_id=uuid4(),
            generation=1,
            reason_code="OPERATOR_REQUESTED",
            requested_at=datetime.now(UTC),
        )
        client.pending_cancel_on_renew = cancel_request
        with pytest.raises(WorkerJobLeaseLost):
            await session.navigate(
                "https://www.threads.com/",
                BrowserNavigationPolicy(frozenset({"https://www.threads.com"})),
            )

        assert client.cancel_calls == [(cancel_request.request_id, 1, "BEFORE_NAVIGATION")]
        assert engine.sessions[0].navigate_count == 0
        assert client.snapshot.status is WorkerJobStatus.CANCELLED
        await session.close()

    asyncio.run(scenario())


def test_playwright_credentials_stay_in_memory_and_are_account_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_id = uuid4()
        first_account, second_account = uuid4(), uuid4()
        root, store, resolver = _managed_storage(tmp_path, worker_id)
        manager = LocalBrowserSessionManager(worker_id, 2, store, resolver)
        provider = _MemoryProxyCredentials(
            {
                "secret://first-account": BrowserProxyCredentials("first-user", "first-secret"),
                "secret://second-account": BrowserProxyCredentials("second-user", "second-secret"),
            }
        )
        engine = _MemoryBrowserEngine()
        adapter = PlaywrightBrowserAdapter(
            worker_id,
            resolver,
            engine,
            credential_provider=provider,
        )
        sessions: list[WorkerBrowserSession] = []
        for account_id, credential_ref, profile_ref in (
            (first_account, "secret://first-account", "first-profile"),
            (second_account, "secret://second-account", "second-profile"),
        ):
            network = NetworkProfile(
                account_id=account_id,
                name="account network",
                protocol=NetworkProtocol.HTTPS,
                host="proxy.example.test",
                port=8443,
                credential_ref=credential_ref,
            )
            context = WorkerAccountContext(account_id, worker_id, profile_ref, network)
            opened = await manager.open(context)
            assert opened.network_route.account_id == account_id
            assert opened.network_route.credential_ref == credential_ref
            sessions.append(
                await adapter.open_reserved_session(
                    context,
                    opened,
                    transition=manager.transition,
                    close_session=manager.close,
                    headless=True,
                )
            )
        assert provider.requested_refs == ["secret://first-account", "secret://second-account"]
        assert [request.network_route for request in engine.requests] == [
            BrowserNetworkRoute(BrowserNetworkProtocol.HTTPS, "proxy.example.test", 8443),
            BrowserNetworkRoute(BrowserNetworkProtocol.HTTPS, "proxy.example.test", 8443),
        ]
        assert engine.requests[0].proxy_credentials == BrowserProxyCredentials(
            "first-user", "first-secret"
        )
        assert engine.requests[1].proxy_credentials == BrowserProxyCredentials(
            "second-user", "second-secret"
        )
        diagnostic = repr(engine.requests)
        assert "first-secret" not in diagnostic
        assert "second-secret" not in diagnostic
        assert "secret://first-account" not in diagnostic
        assert "secret://second-account" not in diagnostic

        for session in sessions:
            await session.close()
        assert store.active_session_count() == 0
        assert root.path.is_dir()

    asyncio.run(scenario())


def test_credentialed_socks5_is_rejected_without_secret_in_error() -> None:
    request = BrowserLaunchRequest(
        Path("unused"),
        BrowserNetworkRoute(BrowserNetworkProtocol.SOCKS5, "proxy.example.test", 1080),
        proxy_credentials=BrowserProxyCredentials("proxy-user", "proxy-password"),
    )
    with pytest.raises(BrowserNetworkRouteUnsupported) as error:
        from threads_platform.infrastructure.browser.playwright_engine import (
            playwright_proxy_settings,
        )

        playwright_proxy_settings(request)
    assert "proxy-password" not in str(error.value)
    assert "proxy-user" not in repr(request)


def test_browser_adapter_rejects_profile_from_another_account(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_id = uuid4()
        first_account, second_account = uuid4(), uuid4()
        _, store, resolver = _managed_storage(tmp_path, worker_id)
        manager = LocalBrowserSessionManager(worker_id, 2, store, resolver)
        first_context = WorkerAccountContext(first_account, worker_id, "first-profile", None)
        second_context = WorkerAccountContext(second_account, worker_id, "second-profile", None)
        opened = await manager.open(first_context)
        engine = _MemoryBrowserEngine()
        adapter = PlaywrightBrowserAdapter(worker_id, resolver, engine)
        with pytest.raises(BrowserAccountAffinityMismatch):
            await adapter.open_reserved_session(
                second_context,
                opened,
                transition=manager.transition,
                close_session=manager.close,
                headless=True,
            )
        assert engine.requests == []
        assert store.active_session_count() == 0
        closed = store.get_session(first_account)
        assert closed is not None and closed.state is BrowserSessionState.STOPPED

    asyncio.run(scenario())


def test_browser_adapter_rejects_network_route_for_another_account(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        worker_id, account_id, other_account_id = uuid4(), uuid4(), uuid4()
        context, manager, store, resolver, _ = await _managed_session(
            tmp_path,
            worker_id,
            account_id,
            "network-affinity-profile",
            open_session=False,
        )
        opened = await manager.open(context)
        mismatched = replace(
            opened,
            network_route=WorkerNetworkRoute(
                other_account_id,
                opened.network_route.browser_route,
                opened.network_route.credential_ref,
            ),
        )
        engine = _MemoryBrowserEngine()
        adapter = PlaywrightBrowserAdapter(worker_id, resolver, engine)

        with pytest.raises(BrowserAccountAffinityMismatch):
            await adapter.open_reserved_session(
                context,
                mismatched,
                transition=manager.transition,
                close_session=manager.close,
                headless=True,
            )

        assert engine.requests == []
        closed = store.get_session(account_id)
        assert closed is not None and closed.state is BrowserSessionState.STOPPED

    asyncio.run(scenario())


def test_http_proxy_credentials_are_passed_only_to_engine_in_memory() -> None:
    from threads_platform.infrastructure.browser.playwright_engine import (
        playwright_proxy_settings,
    )

    request = BrowserLaunchRequest(
        Path("unused"),
        BrowserNetworkRoute(BrowserNetworkProtocol.HTTPS, "proxy.example.test", 8443),
        proxy_credentials=BrowserProxyCredentials("route-user", "route-password"),
        headless=True,
    )
    settings = playwright_proxy_settings(request)
    assert settings is not None
    assert settings == {
        "server": "https://proxy.example.test:8443",
        "username": "route-user",
        "password": "route-password",
    }
    assert "route-password" not in repr(request)


def test_unknown_ui_contracts_and_missing_markers_fail_closed() -> None:
    assert classify_browser_surface(_surface("AUTHENTICATED")) is BrowserSurfaceState.AUTHENTICATED
    with pytest.raises(UnsupportedUIState):
        classify_browser_surface(_surface("AUTHENTICATED", version="2"))
    with pytest.raises(UnsupportedUIState):
        classify_browser_surface(
            BrowserSurface("unrecognized.contract", "1", "AUTHENTICATED", True)
        )
    with pytest.raises(LocatorNotFound):
        classify_browser_surface(_surface("AUTHENTICATED", include_root=False))
    with pytest.raises(BrowserAdapterError) as missing_contract:
        classify_browser_surface(BrowserSurface(None, None, None, True))
    assert str(missing_contract.value) == "BROWSER_CONTRACT_MISMATCH"


def test_navigation_policy_allows_only_allowlisted_loopback_http() -> None:
    policy = _local_policy("http://127.0.0.1:41000")
    policy.validate("http://127.0.0.1:41000/synthetic")
    BrowserNavigationPolicy(frozenset({"https://www.threads.com"})).validate(
        "https://www.threads.com/"
    )
    with pytest.raises(UnsupportedUIState):
        BrowserNavigationPolicy(frozenset({"https://www.threads.com"})).validate(
            "https://threads.net/"
        )
    with pytest.raises(UnsupportedUIState):
        policy.validate("http://example.test:41000/synthetic")


def test_lease_loss_blocks_mutation_and_ambiguous_outcomes_do_not_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        _, store, _ = _managed_storage(tmp_path, worker_id)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(
            job,
            worker_id,
            client,
            local_state=store,
            profile_ref="safe-profile",
        )
        action_count = 0

        async def increment() -> None:
            nonlocal action_count
            action_count += 1

        client.lose_lease = True
        with pytest.raises(WorkerJobLeaseLost):
            await execution.execute_irreversible_boundary(increment)
        assert action_count == 0
        entry = store.recovery_entries()[0]
        assert entry.phase == "MUTATION_PENDING"

        store.clear_recovery_entry(execution.snapshot.job_id)
        client.lose_lease = False

        async def uncertain_action() -> None:
            nonlocal action_count
            action_count += 1
            raise TimeoutError("synthetic remote timeout")

        with pytest.raises(ActionOutcomeAmbiguous) as ambiguous:
            await execution.execute_irreversible_boundary(uncertain_action)
        assert ambiguous.value.intervention_recorded
        assert action_count == 1
        assert client.interventions == [
            ("AMBIGUOUS_OUTCOME", "ACTION_OUTCOME_REQUIRES_RECONCILIATION")
        ]
        assert store.recovery_entries() == []

    asyncio.run(scenario())


def test_post_action_lease_loss_is_ambiguous_and_keeps_recovery_journal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        _, store, _ = _managed_storage(tmp_path, worker_id)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        client.lose_lease_on_checkpoint_phase = "MUTATION_CONFIRMED"
        execution = WorkerJobExecution(
            job,
            worker_id,
            client,
            local_state=store,
            profile_ref="safe-profile",
        )
        action_count = 0

        async def increment() -> None:
            nonlocal action_count
            action_count += 1

        with pytest.raises(ActionOutcomeAmbiguous) as ambiguous:
            await execution.execute_irreversible_boundary(increment)

        assert not ambiguous.value.intervention_recorded
        assert action_count == 1
        assert client.interventions == []
        assert store.recovery_entries()[0].phase == "MUTATION_CONFIRMED"

        recovery = WorkerJobReconnectRecovery(worker_id, client, store)
        await recovery.reconcile((job,))
        assert store.recovery_entries()[0].phase == "MUTATION_CONFIRMED"

        client.lose_lease = False
        await recovery.reconcile((job,))
        assert client.interventions == [
            ("AMBIGUOUS_OUTCOME", "RESTART_REQUIRES_OUTCOME_RECONCILIATION")
        ]
        assert store.recovery_entries() == []
        assert action_count == 1

    asyncio.run(scenario())


def test_safe_to_retry_job_cannot_enter_irreversible_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        _, store, _ = _managed_storage(tmp_path, worker_id)
        job = replace(
            _running_job(worker_id, account_id),
            retry_safety=WorkerJobRetrySafety.SAFE_TO_RETRY,
        )
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(
            job,
            worker_id,
            client,
            local_state=store,
            profile_ref="safe-profile",
        )
        action_count = 0

        async def increment() -> None:
            nonlocal action_count
            action_count += 1

        with pytest.raises(WorkerJobRetrySafetyViolation):
            await execution.execute_irreversible_boundary(increment)

        assert action_count == 0
        assert client.snapshot.checkpoint is None
        assert store.recovery_entries() == []

    asyncio.run(scenario())


def test_reconnect_reconciliation_turns_local_uncertainty_into_intervention(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        _, store, _ = _managed_storage(tmp_path, worker_id)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        store.save_recovery_entry(
            _recovery_entry(job.job_id, account_id, "recover-profile", "MUTATION_STARTED")
        )
        await WorkerJobReconnectRecovery(worker_id, client, store).reconcile((job,))
        assert client.interventions == [
            ("AMBIGUOUS_OUTCOME", "RESTART_REQUIRES_OUTCOME_RECONCILIATION")
        ]
        assert store.recovery_entries() == []

    asyncio.run(scenario())


def test_browser_crash_retries_only_before_mutation_and_reconciles_after(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker_id, account_id = uuid4(), uuid4()
        _, store, resolver = _managed_storage(tmp_path, worker_id)
        manager = LocalBrowserSessionManager(worker_id, 1, store, resolver)
        context = WorkerAccountContext(account_id, worker_id, "crash-profile", None)
        opened = await manager.open(context)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(
            job,
            worker_id,
            client,
            local_state=store,
            profile_ref=context.profile_ref,
        )
        session = WorkerBrowserSession(
            account_id,
            opened,
            _MemoryBrowserSession(crash_on_navigation=True),
            manager.transition,
            manager.close,
            execution,
            lambda _: None,
        )
        with pytest.raises(BrowserProcessCrashed):
            await session.navigate(
                "http://127.0.0.1:41000/test", _local_policy("http://127.0.0.1:41000")
            )
        assert client.snapshot.status is WorkerJobStatus.FAILED_RETRYABLE
        current = store.get_session(account_id)
        assert current is not None and current.state is BrowserSessionState.ERROR
        await session.close()

        account_id = uuid4()
        context = WorkerAccountContext(account_id, worker_id, "ambiguous-profile", None)
        opened = await manager.open(context)
        job = _running_job(worker_id, account_id)
        client = _MemoryWorkerJobControl(worker_id, account_id, job)
        execution = WorkerJobExecution(
            job,
            worker_id,
            client,
            local_state=store,
            profile_ref=context.profile_ref,
        )

        async def synthetic_mutation() -> str:
            return "visible"

        await execution.execute_irreversible_boundary(synthetic_mutation)
        session = WorkerBrowserSession(
            account_id,
            opened,
            _MemoryBrowserSession(crash_on_navigation=True),
            manager.transition,
            manager.close,
            execution,
            lambda _: None,
        )
        with pytest.raises(ActionOutcomeAmbiguous) as ambiguous:
            await session.navigate(
                "http://127.0.0.1:41000/test",
                _local_policy("http://127.0.0.1:41000"),
            )
        assert ambiguous.value.intervention_recorded
        assert client.interventions[-1][0] == "AMBIGUOUS_OUTCOME"
        await session.close()

    asyncio.run(scenario())


def test_required_error_messages_are_bounded_codes() -> None:
    errors = (
        BrowserContractError(),
        LocatorNotFound(),
        SessionExpired(),
        ChallengeDetected(),
        RemoteSessionStateUncertain(),
        NavigationTimeout(),
        ActionOutcomeAmbiguous(intervention_recorded=False),
        MediaUploadFailed(),
        UnsupportedUIState(),
    )
    assert all(error.code.isupper() and str(error) == error.code for error in errors)


async def _managed_session(
    tmp_path: Path,
    worker_id: UUID,
    account_id: UUID,
    profile_ref: str,
    *,
    open_session: bool = True,
) -> tuple[
    WorkerAccountContext,
    LocalBrowserSessionManager,
    WorkerLocalStateStore,
    LocalProfileDirectoryResolver,
    BrowserSessionOpenResult,
]:
    root, store, resolver = _managed_storage(tmp_path, worker_id)
    _ = root
    manager = LocalBrowserSessionManager(worker_id, 2, store, resolver)
    context = WorkerAccountContext(account_id, worker_id, profile_ref, None)
    if open_session:
        opened = await manager.open(context)
    else:
        opened = BrowserSessionOpenResult(
            LocalSessionState(
                account_id,
                profile_ref,
                uuid4(),
                BrowserSessionState.LOGIN_REQUIRED,
                1,
                datetime.now(UTC),
            ),
            WorkerNetworkRoute(
                account_id,
                BrowserNetworkRoute(BrowserNetworkProtocol.DIRECT, None, None),
            ),
        )
    return context, manager, store, resolver, opened


def _managed_storage(
    tmp_path: Path,
    worker_id: UUID,
) -> tuple[LocalDataRoot, WorkerLocalStateStore, LocalProfileDirectoryResolver]:
    root = LocalDataRoot(tmp_path / f"worker-{worker_id}")
    root.prepare()
    store = WorkerLocalStateStore(root, worker_id)
    resolver = LocalProfileDirectoryResolver(root, store)
    return root, store, resolver


def _local_policy(origin: str) -> BrowserNavigationPolicy:
    return BrowserNavigationPolicy(frozenset({origin}))


def _document(
    state: str,
    *,
    version: str = "1",
    include_root: bool = True,
) -> bytes:
    root = "<main data-worker-ui-root></main>" if include_root else ""
    return (
        "<!doctype html><html><head>"
        '<meta name="worker-ui-contract" content="worker.synthetic">'
        f'<meta name="worker-ui-version" content="{version}">'
        f'<meta name="worker-session-state" content="{state}">'
        f"</head><body>{root}</body></html>"
    ).encode()


def _surface(
    state: str,
    *,
    version: str = "1",
    include_root: bool = True,
) -> BrowserSurface:
    return BrowserSurface(
        "worker.synthetic",
        version,
        state,
        include_root,
    )


def _thread_document(body: bytes) -> bytes:
    return (
        b'<!doctype html><html><head><link rel="canonical" '
        b'href="/@canonical/post/unrelated"></head><body>' + body + b"</body></html>"
    )


def _profile_document(body: bytes) -> bytes:
    return b"<!doctype html><html><body>" + body + b"</body></html>"


def _media_composer_document(upload_script: bytes) -> bytes:
    return (
        b'<!doctype html><html><body><div role="dialog">'
        b'<div role="textbox" contenteditable="true"></div>'
        b'<input type="file" accept="image/jpeg,image/png,image/webp">'
        b'<img><button type="button" onclick="window.unsafeActionCount++">Post</button>'
        b'<button type="button" onclick="window.unsafeActionCount++">Remove</button>'
        b"</div><script>window.unsafeActionCount = 0;"
        b"document.querySelector('input[type=file]').addEventListener('change', (event) => {"
        b"document.querySelector('img').src = URL.createObjectURL(event.target.files[0]);"
        + upload_script
        + b"});</script></body></html>"
    )


_SYNTHETIC_DOCUMENTS: dict[str, bytes] = {
    "/authenticated": _document("AUTHENTICATED"),
    "/login": _document("LOGIN_REQUIRED"),
    "/expired": _document("SESSION_EXPIRED"),
    "/challenge": _document("CHALLENGE_REQUIRED"),
    "/unknown": _document("AUTHENTICATED", version="17"),
    "/missing": _document("AUTHENTICATED", include_root=False),
    "/no-contract": b"<html><body><main data-worker-ui-root></main></body></html>",
    "/feed": (
        b"<!doctype html><html><body><section><div>"
        b'<a href="/@alice/post/post-1">permalink</a>'
        b'<a href="/@alice/">author</a>'
        b'<div dir="auto">Synthetic   public text</div>'
        b"</div></section></body></html>"
    ),
    "/@alice/post/post-1": _thread_document(
        b'<section class="css-hash-919"><a href="/@alice/post/post-1">open</a>'
        b'<a href="/@alice/">author</a><span dir="auto">Root text</span>'
        b"</section><button>Open</button>"
    ),
    "/@alice/post/post-duplicate": _thread_document(
        b'<section class="generated-42"><a href="/@alice/post/post-duplicate">one</a>'
        b'<a href="/@alice/post/post-duplicate/">two</a><a href="/@alice/">author</a>'
        b'<span dir="auto">Root text</span></section>'
    ),
    "/@alice/post/post-duplicate-author": _thread_document(
        b'<section><a href="/@alice/post/post-duplicate-author">target</a>'
        b'<a href="/@alice/">author one</a><a href="/@alice/">author two</a>'
        b'<span dir="auto">Root text</span></section>'
    ),
    "/@alice/post/post-competing": _thread_document(
        b'<section><div><a href="/@alice/post/post-competing">first</a>'
        b'<a href="/@alice/">author</a><span dir="auto">First root</span></div></section>'
        b'<section><div><a href="/@alice/post/post-competing">second</a>'
        b'<a href="/@alice/">author</a><span dir="auto">Second root</span></div></section>'
    ),
    "/@alice/post/post-reply-cross": _thread_document(
        b'<section><div><a href="/@alice/post/post-reply-cross">target</a>'
        b'<a href="/@alice/">target author</a><span dir="auto">Target text</span>'
        b'<div><a href="/@bob/post/reply-1">reply</a><a href="/@bob/">reply author</a>'
        b'<span dir="auto">Reply text</span></div></div></section>'
    ),
    "/@alice/post/post-over-bound": _thread_document(
        b'<div><a href="/@alice/">author</a><span dir="auto">Root text</span>'
        + b"<div>" * 9
        + b'<a href="/@alice/post/post-over-bound">target</a>'
        + b"</div>" * 9
        + b"</div>"
    ),
    "/@alice/post/post-author-mismatch": _thread_document(
        b'<section><a href="/@alice/post/post-author-mismatch">target</a>'
        b'<a href="/@Alice/">wrong case</a><span dir="auto">Root text</span></section>'
    ),
    "/@alice/post/post-no-anchor": _thread_document(
        b'<section><a href="/@alice/post/post-no-anchor-extra">similar permalink</a>'
        b'<a href="/@alice/">author</a><span dir="auto">Root text</span></section>'
    ),
    "/@alice/post/post-prefix-anchor": _thread_document(
        b'<section><a href="/@alice/post/post-prefix-anchor-extra">similar permalink</a>'
        b'<a href="/@alice/">author</a><span dir="auto">Root text</span></section>'
    ),
    "/@alice": _profile_document(
        b"<header><div><h1>Public profile</h1>"
        + b"".join(b'<a href="/@alice/">profile identity</a>' for _ in range(9))
        + b'</div></header><section><a href="/@alice/post/post-1">post</a></section>'
    ),
    "/@duph1": _profile_document(
        b"<header><div><h1>First profile</h1><h1>Second profile</h1>"
        b'<a href="/@duph1">profile identity</a></div></header>'
    ),
    "/@ambiguousheaders": _profile_document(
        b"<section><div><h1>First profile</h1>"
        b'<a href="/@ambiguousheaders">profile identity</a></div>'
        b"<div><h1>Second profile</h1>"
        b'<a href="/@ambiguousheaders">profile identity</a></div></section>'
    ),
    "/@headingoverflow": _profile_document(
        b"<header><div>"
        + b"".join(b"<h1>Profile heading</h1>" for _ in range(9))
        + b'<a href="/@headingoverflow">profile identity</a></div></header>'
    ),
    "/@missingh1": _profile_document(
        b'<header><div><a href="/@missingh1">profile identity</a></div></header>'
    ),
    "/@contaminated": _profile_document(
        b'<header><div><h1>Public profile</h1><a href="/@contaminated">profile identity</a>'
        b'<a href="/@alice/post/post-1">post permalink</a></div></header>'
    ),
    "/@postonly": _profile_document(
        b"<header><h1>Public profile</h1></header><section>"
        b'<a href="/@postonly">post content profile link</a>'
        b'<a href="/@alice/post/post-1">post permalink</a></section>'
    ),
    "/@noevidence": _profile_document(
        b'<header><h1>Public profile</h1><a href="/@someoneelse">other profile</a></header>'
    ),
    "/@outsideheader": _profile_document(
        b"<div><header><div><h1>Public profile</h1></div></header>"
        b'<footer><a href="/@outsideheader">profile elsewhere</a></footer></div>'
    ),
    "/@queryhref": _profile_document(
        b"<header><h1>Public profile</h1>"
        b'<a href="/@queryhref?source=profile">profile identity</a></header>'
    ),
    "/@overbound": _profile_document(
        b'<div><a href="/@overbound">profile identity</a>'
        + b"<span>" * 8
        + b"<h1>Public profile</h1>"
        + b"</span>" * 8
        + b"</div>"
    ),
    "/media-composer": _media_composer_document(
        b"fetch('/rupload_igphoto/fb_uploader_123', {method: 'POST'})"
        b".then((response) => response.arrayBuffer());"
    ),
    "/media-composer-no-response": _media_composer_document(b""),
    "/media-composer-duplicate": _media_composer_document(
        b"fetch('/rupload_igphoto/fb_uploader_123', {method: 'POST'})"
        b".then((response) => response.arrayBuffer());"
        b"fetch('/rupload_igphoto/fb_uploader_123', {method: 'POST'})"
        b".then((response) => response.arrayBuffer());"
    ),
    "/media-composer-non-200": _media_composer_document(
        b"fetch('/rupload_igphoto/fb_uploader_503', {method: 'POST'})"
        b".then((response) => response.arrayBuffer());"
    ),
    "/media-composer-wrong-method": _media_composer_document(
        b"fetch('/rupload_igphoto/fb_uploader_123').then((response) => response.arrayBuffer());"
    ),
    "/media-composer-off-origin": _media_composer_document(
        b"fetch('__OTHER_ORIGIN__/rupload_igphoto/fb_uploader_123', {method: 'POST'})"
        b".then((response) => response.arrayBuffer());"
    ),
    "/media-composer-extended-path": _media_composer_document(
        b"fetch('/rupload_igphoto/fb_uploader_123/extended', {method: 'POST'})"
        b".then((response) => response.arrayBuffer());"
    ),
    "/media-no-composer": b"<!doctype html><html><body></body></html>",
    "/media-multiple-dialogs": (
        b'<!doctype html><html><body><div role="dialog"><div role="textbox"></div>'
        b'<input type="file" accept="image/*"></div><div role="dialog">'
        b'<div role="textbox"></div><input type="file" accept="image/*"></div>'
        b"</body></html>"
    ),
    "/media-file-input-outside": (
        b'<!doctype html><html><body><div role="dialog" style="min-height:20px">'
        b'<div role="textbox" contenteditable="true" style="min-height:20px"></div>'
        b'</div><input type="file" accept="image/*"></body></html>'
    ),
    "/media-unsupported-accept": (
        b'<!doctype html><html><body><div role="dialog"><div role="textbox"></div>'
        b'<input type="file" accept="video/*"></div></body></html>'
    ),
}


def _running_job(worker_id: UUID, account_id: UUID) -> WorkerJobSnapshot:
    return WorkerJobSnapshot(
        job_id=uuid4(),
        capability_name="synthetic.adapter.test",
        capability_version=1,
        status=WorkerJobStatus.RUNNING,
        account_id=account_id,
        assigned_worker_id=worker_id,
        lease_worker_id=worker_id,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        retry_safety=WorkerJobRetrySafety.RECONCILIATION_REQUIRED,
        checkpoint=None,
    )


def _recovery_entry(
    job_id: UUID,
    account_id: UUID,
    profile_ref: str,
    phase: str,
) -> LocalRecoveryEntry:
    return LocalRecoveryEntry(job_id, account_id, profile_ref, phase, datetime.now(UTC))


class _MemoryBrowserSession:
    def __init__(
        self,
        surface: BrowserSurface | None = None,
        *,
        crash_on_navigation: bool = False,
        remote_uncertain_on_collect: bool = False,
    ) -> None:
        self.surface = surface or _surface("AUTHENTICATED")
        self.crash_on_navigation = crash_on_navigation
        self.remote_uncertain_on_collect = remote_uncertain_on_collect
        self.collect_count = 0
        self.scroll_count = 0
        self.navigate_count = 0
        self.closed = False

    async def navigate(self, url: str, *, allowed_origins: frozenset[str]) -> None:
        _ = (url, allowed_origins)
        self.navigate_count += 1
        if self.crash_on_navigation:
            raise BrowserProcessCrashed()

    async def inspect_surface(self) -> BrowserSurface:
        return self.surface

    async def collect_feed_candidates(
        self, *, ancestor_bound: int
    ) -> tuple[FeedCandidateObservation, ...]:
        _ = ancestor_bound
        self.collect_count += 1
        if self.remote_uncertain_on_collect:
            raise RemoteSessionStateUncertain()
        return ()

    async def scroll_feed(self) -> None:
        self.scroll_count += 1

    async def close(self) -> None:
        self.closed = True


class _MemoryBrowserEngine:
    def __init__(self, *, remote_uncertain_on_collect: bool = False) -> None:
        self.requests: list[BrowserLaunchRequest] = []
        self.surface = _surface("AUTHENTICATED")
        self.remote_uncertain_on_collect = remote_uncertain_on_collect
        self.sessions: list[_MemoryBrowserSession] = []

    async def open(self, request: BrowserLaunchRequest) -> _MemoryBrowserSession:
        self.requests.append(request)
        session = _MemoryBrowserSession(
            self.surface,
            remote_uncertain_on_collect=self.remote_uncertain_on_collect,
        )
        self.sessions.append(session)
        return session


class _MemoryProxyCredentials:
    def __init__(self, credentials: dict[str, BrowserProxyCredentials]) -> None:
        self._credentials = credentials
        self.requested_refs: list[str] = []

    async def credentials_for(self, credential_ref: str) -> BrowserProxyCredentials:
        self.requested_refs.append(credential_ref)
        return self._credentials[credential_ref]


class _MemoryWorkerJobControl:
    def __init__(
        self,
        worker_id: UUID,
        account_id: UUID,
        snapshot: WorkerJobSnapshot | None = None,
    ) -> None:
        self.snapshot = snapshot or _running_job(worker_id, account_id)
        self.lose_lease = False
        self.lose_lease_on_checkpoint_phase: str | None = None
        self.interventions: list[tuple[str, str]] = []
        self.pending_cancel_on_renew: WorkerJobCancelSnapshot | None = None
        self.cancel_calls: list[tuple[UUID, int, str]] = []

    async def renew_job(self, job_id: UUID, lease_token: UUID) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        if self.pending_cancel_on_renew is not None:
            self.snapshot = replace(self.snapshot, pending_cancel=self.pending_cancel_on_renew)
            self.pending_cancel_on_renew = None
        return self.snapshot

    async def checkpoint_job(
        self, job_id: UUID, lease_token: UUID, checkpoint: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        if checkpoint.get("phase") == self.lose_lease_on_checkpoint_phase:
            self.lose_lease = True
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")
        self.snapshot = replace(self.snapshot, checkpoint=checkpoint)
        return self.snapshot

    async def cancel_job(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        cancel_request_id: UUID,
        generation: int,
        checkpoint_phase: str,
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.cancel_calls.append((cancel_request_id, generation, checkpoint_phase))
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.CANCELLED,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
            pending_cancel=None,
        )
        return self.snapshot

    async def complete_job(
        self, job_id: UUID, lease_token: UUID, result: dict[str, object]
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        _ = result
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.SUCCEEDED,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    async def fail_job(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        error_code: str,
        retryable: bool,
        outcome_ambiguous: bool = False,
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        _ = (error_code, outcome_ambiguous)
        self.snapshot = replace(
            self.snapshot,
            status=(
                WorkerJobStatus.FAILED_RETRYABLE if retryable else WorkerJobStatus.FAILED_FINAL
            ),
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    async def request_intervention(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        intervention_type: str,
        detail_code: str,
    ) -> WorkerJobSnapshot:
        self._verify(job_id, lease_token)
        self.interventions.append((intervention_type, detail_code))
        self.snapshot = replace(
            self.snapshot,
            status=WorkerJobStatus.WAITING_INTERVENTION,
            lease_worker_id=None,
            lease_token=None,
            lease_expires_at=None,
        )
        return self.snapshot

    def _verify(self, job_id: UUID, lease_token: UUID) -> None:
        if (
            self.lose_lease
            or job_id != self.snapshot.job_id
            or lease_token != self.snapshot.lease_token
        ):
            raise WorkerControlClientError("WORKER_JOB_LEASE_LOST")
