"""Composition root for one standalone command invocation."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from threads_platform.config.settings import Settings
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI
from threads_platform.infrastructure.threads_api.environment_credentials import (
    EnvironmentThreadsCredentialSecretResolver,
)
from threads_platform.standalone.accounts import LocalAccountStore
from threads_platform.standalone.api import (
    LocalThreadsApiRuntime,
    build_threads_http_client,
)
from threads_platform.standalone.mutations import (
    LocalOperationStore,
    LocalThreadsMutationRuntime,
)
from threads_platform.standalone.runtime import LocalRuntime


@dataclass(frozen=True, slots=True)
class StandaloneAppContext:
    accounts: LocalAccountStore
    api: LocalThreadsApiRuntime | None
    mutations: LocalThreadsMutationRuntime | None
    browser: LocalRuntime | None


@asynccontextmanager
async def build_standalone_app(
    root: Path,
    accounts: LocalAccountStore | None = None,
    *,
    include_api: bool = True,
    include_mutations: bool = True,
    include_browser: bool = True,
) -> AsyncGenerator[StandaloneAppContext]:
    """Build the requested local runtimes and own their one-command lifetimes."""

    if include_mutations and not include_api:
        raise ValueError("standalone mutations require API access")
    if not include_api and not include_browser:
        raise ValueError("standalone app requires at least one runtime")

    local_accounts = accounts if accounts is not None else LocalAccountStore(root)

    if not include_api:
        yield StandaloneAppContext(
            accounts=local_accounts,
            api=None,
            mutations=None,
            browser=LocalRuntime(root, local_accounts),
        )
        return

    settings = Settings()
    async with build_threads_http_client(settings) as client:
        api = HttpThreadsAPI(client)
        secret_resolver = EnvironmentThreadsCredentialSecretResolver()
        api_runtime = LocalThreadsApiRuntime(local_accounts, api, secret_resolver)
        mutation_runtime = None
        if include_mutations:
            operations = LocalOperationStore(root)
            mutation_runtime = LocalThreadsMutationRuntime(
                root,
                local_accounts,
                api,
                secret_resolver,
                operations,
            )
        browser_runtime = LocalRuntime(root, local_accounts) if include_browser else None
        yield StandaloneAppContext(
            accounts=local_accounts,
            api=api_runtime,
            mutations=mutation_runtime,
            browser=browser_runtime,
        )
