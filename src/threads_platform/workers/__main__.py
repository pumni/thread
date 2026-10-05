import asyncio
import importlib.metadata
import os
import signal
import sys
from pathlib import Path

from threads_platform.infrastructure.browser.playwright_engine import PlaywrightBrowserEngine
from threads_platform.infrastructure.local.process_lock import FilesystemProcessLock
from threads_platform.infrastructure.worker_agent.identity import WorkerIdentityFileStore
from threads_platform.infrastructure.worker_agent.local_media import LocalMediaFileResolver
from threads_platform.infrastructure.worker_agent.local_state import (
    LocalDataRoot,
    LocalProfileDirectoryResolver,
    WorkerLocalStateStore,
)
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
from threads_platform.workers.host_config import (
    WorkerHostConfig,
    WorkerHostConfigError,
    read_worker_host_config,
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


async def _run(host_config: WorkerHostConfig | None = None) -> None:
    control_plane_url = configured_value(
        host_config.control_plane_url if host_config is not None else None,
        "THREADS_WORKER_CONTROL_PLANE_URL",
    )
    configured_root = configured_optional_value(
        host_config.data_root if host_config is not None else None,
        "THREADS_WORKER_DATA_ROOT",
    )
    data_root = LocalDataRoot.from_environment(
        Path(configured_root) if configured_root is not None else None
    )
    data_root.prepare()
    identity_store = WorkerIdentityFileStore(data_root)
    worker_id = identity_store.load_or_create()
    enabled_capabilities = enabled_browser_capabilities(host_config)
    config = WorkerAgentConfig(
        control_plane_url=control_plane_url,
        display_name=configured_value(
            host_config.display_name if host_config is not None else None,
            "THREADS_WORKER_DISPLAY_NAME",
            "Windows Worker",
        ),
        agent_version=configured_value(
            host_config.agent_version if host_config is not None else None,
            "THREADS_WORKER_AGENT_VERSION",
            "0.1.0",
        ),
        enrollment_code=os.environ.get("THREADS_WORKER_ENROLLMENT_CODE"),
        max_concurrent_jobs=configured_integer(
            host_config.max_concurrent_jobs if host_config is not None else None,
            "THREADS_WORKER_MAX_CONCURRENT_JOBS",
        ),
        max_browser_sessions=configured_integer(
            host_config.max_browser_sessions if host_config is not None else None,
            "THREADS_WORKER_MAX_BROWSER_SESSIONS",
        ),
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
        FilesystemProcessLock(data_root.child("worker", "agent.lock")),
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


def configured_value(config_value: str | None, name: str, default: str | None = None) -> str:
    if config_value is not None:
        return config_value
    if default is not None and name not in os.environ:
        return default
    return _required_environment(name)


def configured_optional_value(config_value: str | None, name: str) -> str | None:
    return config_value if config_value is not None else os.environ.get(name)


def configured_integer(config_value: int | None, name: str, default: int = 1) -> int:
    return config_value if config_value is not None else _integer_environment(name, default)


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


def enabled_browser_capabilities(
    host_config: WorkerHostConfig | None = None,
) -> tuple[tuple[str, int], ...]:
    capability_overrides = {
        "THREADS_WORKER_FEED_BROWSE_ENABLED": (
            host_config.feed_browse_enabled if host_config is not None else None
        ),
        "THREADS_WORKER_THREAD_OPEN_ENABLED": (
            host_config.thread_open_enabled if host_config is not None else None
        ),
        "THREADS_WORKER_PROFILE_OPEN_ENABLED": (
            host_config.profile_open_enabled if host_config is not None else None
        ),
        "THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED": (
            host_config.media_local_upload_enabled if host_config is not None else None
        ),
    }
    return tuple(
        capability
        for enabled, capability in (
            (
                _configured_boolean(
                    capability_overrides["THREADS_WORKER_FEED_BROWSE_ENABLED"],
                    "THREADS_WORKER_FEED_BROWSE_ENABLED",
                ),
                (FEED_CAPABILITY_NAME, FEED_CAPABILITY_VERSION),
            ),
            (
                _configured_boolean(
                    capability_overrides["THREADS_WORKER_THREAD_OPEN_ENABLED"],
                    "THREADS_WORKER_THREAD_OPEN_ENABLED",
                ),
                (THREAD_OPEN_CAPABILITY_NAME, THREAD_OPEN_CAPABILITY_VERSION),
            ),
            (
                _configured_boolean(
                    capability_overrides["THREADS_WORKER_PROFILE_OPEN_ENABLED"],
                    "THREADS_WORKER_PROFILE_OPEN_ENABLED",
                ),
                (PROFILE_OPEN_CAPABILITY_NAME, PROFILE_OPEN_CAPABILITY_VERSION),
            ),
            (
                _configured_boolean(
                    capability_overrides["THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED"],
                    "THREADS_WORKER_MEDIA_LOCAL_UPLOAD_ENABLED",
                ),
                (MEDIA_LOCAL_UPLOAD_CAPABILITY_NAME, MEDIA_LOCAL_UPLOAD_CAPABILITY_VERSION),
            ),
        )
        if enabled
    )


def _configured_boolean(config_value: bool | None, name: str) -> bool:
    return config_value if config_value is not None else _boolean_environment(name, False)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print(importlib.metadata.version("threads-platform"))
        return 0
    if arguments == ["--package-check"]:
        return run_package_check()
    if len(arguments) == 2 and arguments[0] == "--validate-host-config":
        try:
            read_worker_host_config(Path(arguments[1]))
        except WorkerHostConfigError:
            print("THREADS_WORKER_HOST_CONFIG_INVALID", file=sys.stderr)
            return 2
        print("THREADS_WORKER_HOST_CONFIG_VALID")
        return 0
    host_config: WorkerHostConfig | None = None
    if len(arguments) == 2 and arguments[0] == "--host-config":
        try:
            host_config = read_worker_host_config(Path(arguments[1]))
        except WorkerHostConfigError:
            print("THREADS_WORKER_HOST_CONFIG_INVALID", file=sys.stderr)
            return 2
    elif arguments:
        print("THREADS_WORKER_INVALID_ARGUMENTS", file=sys.stderr)
        return 2
    if os.name != "nt":
        raise RuntimeError("the persistent Worker Agent entrypoint requires Windows DPAPI")
    try:
        asyncio.run(_run(host_config))
    except RuntimeError as error:
        if str(error) == "THREADS_WORKER_CONTROL_PLANE_URL must be configured":
            print("THREADS_WORKER_CONFIGURATION_INVALID", file=sys.stderr)
            return 2
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
