import asyncio
import os
import signal

from threads_platform.infrastructure.worker_agent.identity import WorkerIdentityFileStore
from threads_platform.infrastructure.worker_agent.local_state import (
    LocalDataRoot,
    WorkerLocalStateStore,
)
from threads_platform.infrastructure.worker_agent.process_lock import WorkerProcessLock
from threads_platform.infrastructure.worker_agent.windows_keys import DPAPIWorkerKeyStore
from threads_platform.workers.control_client import HttpWorkerControlClient
from threads_platform.workers.runtime import WorkerAgent, WorkerAgentConfig


async def _run() -> None:
    if os.name != "nt":
        raise RuntimeError("the persistent Worker Agent entrypoint requires Windows DPAPI")
    from pathlib import Path

    configured_root = os.environ.get("THREADS_WORKER_DATA_ROOT")
    data_root = LocalDataRoot.from_environment(
        Path(configured_root) if configured_root is not None else None
    )
    data_root.prepare()
    identity_store = WorkerIdentityFileStore(data_root)
    worker_id = identity_store.load_or_create()
    config = WorkerAgentConfig(
        control_plane_url=_required_environment("THREADS_WORKER_CONTROL_PLANE_URL"),
        display_name=os.environ.get("THREADS_WORKER_DISPLAY_NAME", "Windows Worker"),
        agent_version=os.environ.get("THREADS_WORKER_AGENT_VERSION", "0.1.0"),
        enrollment_code=os.environ.get("THREADS_WORKER_ENROLLMENT_CODE"),
        max_concurrent_jobs=_integer_environment("THREADS_WORKER_MAX_CONCURRENT_JOBS", 1),
        max_browser_sessions=_integer_environment("THREADS_WORKER_MAX_BROWSER_SESSIONS", 1),
    )
    state_store = WorkerLocalStateStore(data_root, worker_id)
    client = HttpWorkerControlClient(config.control_plane_url)
    agent = WorkerAgent(
        config,
        identity_store,
        DPAPIWorkerKeyStore(data_root),
        state_store,
        WorkerProcessLock(data_root.child("worker", "agent.lock")),
        client,
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


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
