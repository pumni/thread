import asyncio
import os
import sys
import tempfile
from pathlib import Path


def run_package_check() -> int:
    if not _is_windows():
        print("WORKER_PACKAGE_CHECK_UNSUPPORTED_PLATFORM", file=sys.stderr)
        return 2
    try:
        asyncio.run(_check_packaged_runtime())
    except Exception:
        print("WORKER_PACKAGE_CHECK_FAILED", file=sys.stderr)
        return 1
    print("WORKER_PACKAGE_CHECK_OK")
    return 0


def _is_windows() -> bool:
    return os.name == "nt"


async def _check_packaged_runtime() -> None:
    import ssl

    import httpx2
    from playwright.async_api import Route, async_playwright

    from threads_platform.infrastructure.worker_agent.identity import WorkerIdentityFileStore
    from threads_platform.infrastructure.worker_agent.local_state import (
        LocalDataRoot,
        WorkerLocalStateStore,
    )
    from threads_platform.infrastructure.worker_agent.windows_keys import DPAPIWorkerKeyStore
    from threads_platform.workers.control_client import HttpWorkerControlClient

    ssl_context = httpx2.create_ssl_context(verify=True, trust_env=True)
    if (
        not type(ssl_context).__module__.startswith("truststore.")
        or ssl_context.verify_mode != ssl.CERT_REQUIRED
        or not ssl_context.check_hostname
    ):
        raise RuntimeError("packaged Worker HTTP client trust configuration is invalid")

    control_client = HttpWorkerControlClient("https://control.test")
    await control_client.aclose()

    with tempfile.TemporaryDirectory(
        prefix="threads-worker-package-check-", ignore_cleanup_errors=True
    ) as temporary_root:
        data_root = LocalDataRoot(Path(temporary_root) / "worker-data")
        data_root.prepare()

        identity_store = WorkerIdentityFileStore(data_root)
        worker_id = identity_store.load_or_create()
        if identity_store.load_or_create() != worker_id:
            raise RuntimeError("temporary worker identity did not persist")

        key_store = DPAPIWorkerKeyStore(data_root)
        first_identity = key_store.load_or_create(worker_id)
        second_identity = key_store.load_or_create(worker_id)
        if first_identity.public_key_bytes != second_identity.public_key_bytes:
            raise RuntimeError("temporary DPAPI identity did not round-trip")

        WorkerLocalStateStore(data_root, worker_id)
        if not data_root.journal_path.is_file():
            raise RuntimeError("temporary worker journal was not initialized")

        external_requests: list[str] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--no-first-run",
                ],
            )
            try:
                page = await browser.new_page()

                async def reject_network(route: Route) -> None:
                    external_requests.append("blocked")
                    await route.abort()

                await page.route("**/*", reject_network)
                await page.goto("about:blank")
                if page.url != "about:blank" or external_requests:
                    raise RuntimeError("browser self-check left the inert page")
            finally:
                await browser.close()
