from collections.abc import Mapping
from dataclasses import dataclass

from threads_platform.application.capability_router import CapabilityRouter
from threads_platform.application.clock import Clock
from threads_platform.application.commands.handlers import CommandHandler
from threads_platform.application.commands.runtime import CommandRuntime
from threads_platform.application.commands.threads_handlers import (
    create_threads_command_handlers,
)
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.ports.threads import ThreadsAccessTokenProvider, ThreadsAPI
from threads_platform.application.worker_jobs import WorkerJobService


@dataclass(frozen=True, slots=True)
class CommandRuntimeComposition:
    unit_of_work_factory: UnitOfWorkFactory
    worker_job_service: WorkerJobService
    capability_router: CapabilityRouter
    threads_api_gateway: ThreadsAPI | None
    threads_access_token_provider: ThreadsAccessTokenProvider | None
    command_handler_registry: Mapping[str, CommandHandler]
    command_runtime: CommandRuntime


def compose_command_runtime(
    unit_of_work_factory: UnitOfWorkFactory,
    worker_job_service: WorkerJobService,
    *,
    threads_api_gateway: ThreadsAPI | None,
    threads_access_token_provider: ThreadsAccessTokenProvider | None,
    clock: Clock | None = None,
) -> CommandRuntimeComposition:
    if (threads_api_gateway is None) != (threads_access_token_provider is None):
        raise ValueError(
            "Threads API gateway and access token provider must be configured together"
        )

    command_handler_registry: Mapping[str, CommandHandler] = (
        create_threads_command_handlers(
            threads_api_gateway,
            threads_access_token_provider,
            unit_of_work_factory,
        )
        if threads_api_gateway is not None and threads_access_token_provider is not None
        else {}
    )
    capability_router = CapabilityRouter()
    command_runtime = CommandRuntime(
        unit_of_work_factory,
        command_handler_registry,
        clock=clock,
        capability_router=capability_router,
        worker_job_service=worker_job_service,
    )
    return CommandRuntimeComposition(
        unit_of_work_factory=unit_of_work_factory,
        worker_job_service=worker_job_service,
        capability_router=capability_router,
        threads_api_gateway=threads_api_gateway,
        threads_access_token_provider=threads_access_token_provider,
        command_handler_registry=command_handler_registry,
        command_runtime=command_runtime,
    )
