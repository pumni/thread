import asyncio
import os
import signal

from threads_platform.infrastructure.browser.playwright_engine import PlaywrightBrowserEngine
from threads_platform.infrastructure.worker_agent.identity import WorkerIdentityFileStore
from threads_platform.infrastructure.worker_agent.local_media import LocalMediaFileResolver
from threads_platform.infrastructure.worker_agent.local_state import (
    LocalDataRoot,
    LocalProfileDirectoryResolver,
    WorkerLocalStateStore,
)
from threads_platform.infrastructure.worker_agent.process_lock import WorkerProcessLock
from threads_platform.infrastructure.worker_agent.windows_keys import DPAPIWorkerKeyStore
from threads_platform.workers.browser import (
    ManagedPlaywrightBrowserSessionManager,
    PlaywrightBrowserAdapter,
)
from threads_platform.workers.browser_capability_dispatch import (
    BrowserCapabilityHandler,
    BrowserCapabilityJobDispatcher,
)
from threads_platform.workers.control_client import HttpWorkerControlClient
from threads_platform.workers.feed_browse import (
    FEED_CAPABILITY_NAME,
    FEED_CAPABILITY_VERSION,
    BrowserFeedBrowseWorker,
)
from threads_platform.workers.media_local_upload import (
    MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME,
    MEDIA_LOCAL_UPLOAD_CAPABILITY_VERSION,
    BrowserLocalMediaUploadWorker,
)
from threads_platform.workers.profile_open import (
    PROFILE_OPEN_CAPABILITY_NAME,
    PROFILE_OPEN_CAPABILITY_VERSION,
    BrowserProfileOpenWorker,
)
from threads_platform.workers.runtime import WorkerAgent, WorkerAgentConfig
from threads_platform.workers.sessions import LocalBrowserSessionManager
from threads_platform.workers.thread_open import (
    THREAD_OPEN_CAPABILITY_NAME,
    THREAD_OPEN_CAPABILITY_VERSION,
    BrowserThreadOpenWorker,
)


async def _run() -> None:
    from pathlib import Path

    configured_root = os.environ.get("THREADS_WORKER_DATA_ROOT")
    data_root = LocalDataRoot.from_environment(
        Path(configured_root) if configured_root is not None else None
    )
    data_root.prepare()
    identity_store = WorkerIdentityFileStore(data_root)
    worker_id = identity_store.load_or_create()
    enabled_capabilities = enabled_browser_capabilities()
    config = WorkerAgentConfig(
        control_plane_url=_required_environment("THREADS_WORKER_CONTROL_PLANE_URL"),
        display_name=os.environ.get("THREADS_WORKER_DISPLAY_NAME", "Windows Worker"),
        agent_version=os.environ.get("THREADS_WORKER_AGENT_VERSION", "0.1.0"),
        enrollment_code=os.environ.get("THREADS_WORKER_ENROLLMENT_CODE"),
        max_concurrent_jobs=_integer_environment("THREADS_WORKER_MAX_CONCURRENT_JOBS", 1),
        max_browser_sessions=_integer_environment("THREADS_WORKER_MAX_BROWSER_SESSIONS", 1),
        capabilities=enabled_capabilities,
    )
    state_store = WorkerLocalStateStore(data_root, worker_id)
    client = HttpWorkerControlClient(config.control_plane_url)
    job_handler = None
    if enabled_capabilities:
        profile_resolver = LocalProfileDirectoryResolver(data_root, state_store)
        local_sessions = LocalBrowserSessionManager(
            worker_id,
            config.max_browser_sessions,
            state_store,
            profile_resolver,
        )
        browser_adapter = PlaywrightBrowserAdapter(
            worker_id,
            profile_resolver,
            PlaywrightBrowserEngine(),
        )
        browser_sessions = ManagedPlaywrightBrowserSessionManager(
            local_sessions,
            browser_adapter,
        )
        handlers: dict[str, BrowserCapabilityHandler] = {}
        if (FEED_CAPABILITY_NAME, FEED_CAPABILITY_VERSION) in enabled_capabilities:
            handlers[FEED_CAPABILITY_NAME] = BrowserFeedBrowseWorker(
                worker_id, client, browser_sessions
            )
        if (THREAD_OPEN_CAPABILITY_NAME, THREAD_OPEN_CAPABILITY_VERSION) in enabled_capabilities:
            handlers[THREAD_OPEN_CAPABILITY_NAME] = BrowserThreadOpenWorker(
                worker_id, client, browser_sessions
            )
        if (PROFILE_OPEN_CAPABILITY_NAME, PROFILE_OPEN_CAPABILITY_VERSION) in enabled_capabilities:
            handlers[PROFILE_OPEN_CAPABILITY_NAME] = BrowserProfileOpenWorker(
                worker_id, client, browser_sessions
            )
        if (
            MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME,
            MEDIA_LOCAL_UPLOAD_CAPABILITY_VERSION,
        ) in enabled_capabilities:
            handlers[MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME] = BrowserLocalMediaUploadWorker(
                worker_id,
                client,
                browser_sessions,
                LocalMediaFileResolver(data_root),
                state_store,
            )
        job_handler = BrowserCapabilityJobDispatcher(worker_id, client, handlers)
    agent = WorkerAgent(
        config,
        identity_store,
        DPAPIWorkerKeyStore(data_root),
        state_store,
        WorkerProcessLock(data_root.child("worker", "agent.lock")),
        client,
        job_handler=job_handler,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError, RuntimeError:
            pass
    await agent.run(stop_event)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} must be configured")
    return value


def _integer_environment(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer") from error


def _boolean_environment(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


def enabled_browser_capabilities() -> tuple[tuple[str, int], ...]:
    return tuple(
        capability
        for enabled, capability in (
            (
                _boolean_environment("THREADS_WORKER_FEED_BROWSE_ENABLED", False),
                (FEED_CAPABILITY_NAME, FEED_CAPABILITY_VERSION),
            ),
            (
                _boolean_environment("THREADS_WORKER_THREAD_OPEN_ENABLED", False),
                (THREAD_OPEN_CAPABILITY_NAME, THREAD_OPEN_CAPABILITY_VERSION),
            ),
            (
                _boolean_environment("THREADS_WORKER_PROFILE_OPEN_ENABLED", False),
                (PROFILE_OPEN_CAPABILITY_NAME, PROFILE_OPEN_CAPABILITY_VERSION),
            ),
            (
                _boolean_environment("THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED", False),
                (MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME, MEDIA_LOCAL_UPLOAD_CAPABILITY_VERSION),
            ),
        )
        if enabled
    )


def main() -> None:
    if os.name != "nt":
        raise RuntimeError("the persistent Worker Agent entrypoint requires Windows DPAPI")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
