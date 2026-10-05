from collections.abc import Mapping
from dataclasses import dataclass

import httpx2

from threads_platform.application.capability_router import CapabilityRouter
from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.commands.composition import compose_command_runtime
from threads_platform.application.commands.handlers import CommandHandler
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.ports.threads import (
    ThreadsAccessTokenProvider,
    ThreadsAPI,
    ThreadsCredentialSecretResolver,
)
from threads_platform.application.worker_jobs import WorkerJobService
from threads_platform.config.settings import Settings
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI
from threads_platform.infrastructure.threads_api.credentials import (
    PersistentThreadsAccessTokenProvider,
)
from threads_platform.infrastructure.threads_api.environment_credentials import (
    EnvironmentThreadsCredentialSecretResolver,
)


@dataclass(frozen=True, slots=True)
class ProcessCommandRuntimeComposition:
    command_runtime: CommandRuntime
    capability_router: CapabilityRouter
    command_handler_registry: Mapping[str, CommandHandler]
    threads_api_gateway: ThreadsAPI | None
    threads_access_token_provider: ThreadsAccessTokenProvider | None
    http_client: httpx2.AsyncClient | None


def compose_process_command_runtime(
    settings: Settings,
    unit_of_work_factory: UnitOfWorkFactory,
    worker_job_service: WorkerJobService,
    *,
    threads_api_gateway: ThreadsAPI | None = None,
    threads_access_token_provider: ThreadsAccessTokenProvider | None = None,
    secret_resolver: ThreadsCredentialSecretResolver | None = None,
    secret_environment: Mapping[str, str] | None = None,
    clock: Clock | None = None,
) -> ProcessCommandRuntimeComposition:
    resolved_clock = clock or SystemClock()
    if settings.threads_token_provider_mode == "environment":
        if settings.database_url is None:
            raise RuntimeError(
                "THREADS_PLATFORM_DATABASE_URL must be configured for environment token mode"
            )
        if threads_access_token_provider is None:
            resolver = secret_resolver or EnvironmentThreadsCredentialSecretResolver(
                secret_environment
            )
            threads_access_token_provider = PersistentThreadsAccessTokenProvider(
                unit_of_work_factory,
                resolver,
                resolved_clock,
            )

    http_client = None
    if threads_access_token_provider is not None and threads_api_gateway is None:
        http_client = httpx2.AsyncClient(
            base_url=str(settings.threads_api_base_url),
            timeout=httpx2.Timeout(15.0),
            follow_redirects=False,
            verify=True,
            trust_env=True,
        )
        threads_api_gateway = HttpThreadsAPI(http_client)

    composition = compose_command_runtime(
        unit_of_work_factory,
        worker_job_service,
        threads_api_gateway=threads_api_gateway,
        threads_access_token_provider=threads_access_token_provider,
        clock=resolved_clock,
    )
    return ProcessCommandRuntimeComposition(
        command_runtime=composition.command_runtime,
        capability_router=composition.capability_router,
        command_handler_registry=composition.command_handler_registry,
        threads_api_gateway=composition.threads_api_gateway,
        threads_access_token_provider=composition.threads_access_token_provider,
        http_client=http_client,
    )
