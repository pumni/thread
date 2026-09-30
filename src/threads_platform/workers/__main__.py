import asyncio
import importlib.metadata
import os
import signal
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from threads_platform.infrastructure.browser.playwright_engine import PlaywrightBrowserEngine
from threads_platform.infrastructure.worker_agent.enrollment_bootstrap import (
    EnrollmentBootstrapFile,
)
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
from threads_platform.workers.package_check import run_package_check
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
from threads_platform.workers.windows_service import LocalStopSignal

_SERVICE_DATA_DIRECTORY = "ThreadsOperations"


async def _run() -> None:
    if os.name != "nt":
        raise RuntimeError("the persistent Worker Agent entrypoint requires Windows DPAPI")
    agent = _create_agent(service_mode=False)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError, RuntimeError:
            pass
    await agent.run(stop_event)


def _create_agent(*, service_mode: bool) -> WorkerAgent:
    control_plane_url = _required_environment("THREADS_WORKER_CONTROL_PLANE_URL")
    if service_mode:
        data_root = production_service_data_root()
    else:
        configured_root = os.environ.get("THREADS_WORKER_DATA_ROOT")
        data_root = (
            LocalDataRoot(Path(configured_root))
            if configured_root is not None
            else LocalDataRoot.from_environment()
        )
    data_root.prepare()
    identity_store = WorkerIdentityFileStore(data_root)
    worker_id = identity_store.load_or_create()
    enabled_capabilities = enabled_browser_capabilities()
    config = WorkerAgentConfig(
        control_plane_url=control_plane_url,
        display_name=os.environ.get("THREADS_WORKER_DISPLAY_NAME", "Windows Worker"),
        agent_version=os.environ.get("THREADS_WORKER_AGENT_VERSION", "0.1.0"),
        enrollment_code=(
            None if service_mode else os.environ.get("THREADS_WORKER_ENROLLMENT_CODE")
        ),
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
    return WorkerAgent(
        config,
        identity_store,
        DPAPIWorkerKeyStore(data_root),
        state_store,
        WorkerProcessLock(data_root.child("worker", "agent.lock")),
        client,
        job_handler=job_handler,
        enrollment_bootstrap=EnrollmentBootstrapFile(data_root) if service_mode else None,
    )


async def _run_windows_service(
    stop_event: asyncio.Event,
    stop_signal: LocalStopSignal,
    report_running: Callable[[], None],
) -> None:
    agent = _create_agent(service_mode=True)
    try:
        await agent.initialize()
    except Exception:
        await agent.close()
        raise
    report_running()
    await agent.run(
        stop_event,
        drain_on_stop=True,
        local_stop_requested=stop_signal.is_set,
    )


async def _run_windows_service_check(
    stop_event: asyncio.Event,
    _stop_signal: LocalStopSignal,
    report_running: Callable[[], None],
) -> None:
    data_root = service_check_data_root()
    data_root.prepare()
    lock = WorkerProcessLock(data_root.child("worker", "agent.lock"))
    lock.acquire()
    try:
        identity_store = WorkerIdentityFileStore(data_root)
        worker_id = identity_store.load_or_create()
        DPAPIWorkerKeyStore(data_root).load_or_create(worker_id)
        WorkerLocalStateStore(data_root, worker_id)
        report_running()
        await stop_event.wait()
    finally:
        lock.release()


def _windows_service(test_mode: bool) -> int:
    runner = _run_windows_service_check if test_mode else _run_windows_service
    if os.name != "nt":
        print("THREADS_WORKER_WINDOWS_SERVICE_UNSUPPORTED", file=sys.stderr)
        return 2
    from threads_platform.workers.windows_service import run_windows_service

    return run_windows_service(runner)


def production_service_data_root() -> LocalDataRoot:
    program_data = _required_environment("ProgramData")
    expected = Path(program_data) / _SERVICE_DATA_DIRECTORY
    try:
        metadata = expected.lstat()
    except FileNotFoundError:
        pass
    else:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)
        ):
            raise RuntimeError("service data directory must be a regular directory")
    configured = os.environ.get("THREADS_WORKER_DATA_ROOT")
    if configured is not None:
        actual_path = Path(configured)
        if not actual_path.is_absolute() or _normalized_windows_path(actual_path) != (
            _normalized_windows_path(expected)
        ):
            raise RuntimeError("THREADS_WORKER_DATA_ROOT must use the service data directory")
    return LocalDataRoot(expected)


def service_check_data_root() -> LocalDataRoot:
    program_data = Path(_required_environment("ProgramData")).resolve()
    configured = os.environ.get("THREADS_WORKER_SERVICE_CHECK_ROOT")
    if configured is None:
        raise RuntimeError("THREADS_WORKER_SERVICE_CHECK_ROOT must be configured")
    candidate = Path(configured)
    if not candidate.is_absolute():
        raise RuntimeError("THREADS_WORKER_SERVICE_CHECK_ROOT must be absolute")
    resolved = candidate.resolve()
    if resolved == program_data or not resolved.is_relative_to(program_data):
        raise RuntimeError("THREADS_WORKER_SERVICE_CHECK_ROOT must be isolated under ProgramData")
    return LocalDataRoot(resolved)


def _normalized_windows_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


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
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer") from error
    if parsed < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


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


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print(importlib.metadata.version("threads-platform"))
        return 0
    if arguments == ["--package-check"]:
        return run_package_check()
    if arguments == ["--windows-service"]:
        return _windows_service(test_mode=False)
    if arguments == ["--windows-service-check"]:
        return _windows_service(test_mode=True)
    if arguments:
        print("THREADS_WORKER_INVALID_ARGUMENTS", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run())
    except RuntimeError as error:
        if str(error) == "THREADS_WORKER_CONTROL_PLANE_URL must be configured":
            print("THREADS_WORKER_CONFIGURATION_INVALID", file=sys.stderr)
            return 2
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
