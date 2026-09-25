import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from threads_platform.domain.accounts import AccountExecutionMode
from threads_platform.domain.time import normalize_utc, utc_now


class CapabilityExecutionClass(StrEnum):
    NATIVE_API = "NATIVE_API"
    HYBRID = "HYBRID"
    BROWSER_ASSISTED = "BROWSER_ASSISTED"
    HUMAN_ASSISTED = "HUMAN_ASSISTED"
    UNSUPPORTED = "UNSUPPORTED"


class OperationClass(StrEnum):
    READ = "READ"
    MUTATION = "MUTATION"
    SESSION = "SESSION"
    BACKGROUND = "BACKGROUND"

    @property
    def requires_exclusive_account_coordination(self) -> bool:
        return self is not OperationClass.READ


class CapabilityExecutor(StrEnum):
    API = "API"
    WORKER = "WORKER"


class RouteTarget(StrEnum):
    LOCAL_API = "LOCAL_API"
    WORKER_JOB = "WORKER_JOB"
    WAITING_EXECUTION = "WAITING_EXECUTION"
    WAITING_INTERVENTION = "WAITING_INTERVENTION"
    UNSUPPORTED = "UNSUPPORTED"


class FallbackSafety(StrEnum):
    NEVER = "NEVER"
    BEFORE_FIRST_ATTEMPT = "BEFORE_FIRST_ATTEMPT"


@dataclass(frozen=True, slots=True)
class BusinessCapabilityPolicy:
    command_type: str
    capability_name: str
    capability_version: int
    execution_class: CapabilityExecutionClass
    operation_class: OperationClass
    preferred_executor: CapabilityExecutor
    fallback_executor: CapabilityExecutor | None = None
    fallback_safety: FallbackSafety = FallbackSafety.NEVER
    worker_capability_name: str | None = None
    worker_capability_version: int | None = None
    blocked_reason_code: str | None = None

    def __post_init__(self) -> None:
        if not self.command_type.strip() or not self.capability_name.strip():
            raise ValueError("business capability requires a command and capability name")
        if self.capability_version < 1:
            raise ValueError("business capability version must be positive")
        if (self.worker_capability_name is None) != (self.worker_capability_version is None):
            raise ValueError("worker capability name and version must be set together")
        if self.worker_capability_version is not None and self.worker_capability_version < 1:
            raise ValueError("worker capability version must be positive")
        if (
            self.blocked_reason_code is not None
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", self.blocked_reason_code) is None
        ):
            raise ValueError("blocked capability reason must be a bounded code")
        if self.fallback_executor is self.preferred_executor:
            raise ValueError("fallback executor must differ from the preferred executor")
        if self.fallback_executor is not None and self.fallback_safety is FallbackSafety.NEVER:
            raise ValueError("an allowed fallback requires an explicit safety condition")
        if self.fallback_executor is None and self.fallback_safety is not FallbackSafety.NEVER:
            raise ValueError("fallback safety requires a fallback executor")
        if (
            CapabilityExecutor.WORKER in {self.preferred_executor, self.fallback_executor}
            and self.worker_capability_name is None
        ):
            raise ValueError("worker execution requires an advertised worker capability")


@dataclass(frozen=True, slots=True)
class CapabilityRouteDecision:
    command_id: str
    account_id: UUID
    capability_name: str
    capability_version: int
    execution_class: CapabilityExecutionClass
    operation_class: OperationClass
    account_mode: AccountExecutionMode
    target: RouteTarget
    executor: CapabilityExecutor | None
    reason_code: str
    attempt_count: int = 0
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.command_id.strip() or not self.capability_name.strip():
            raise ValueError("route decision requires a command and capability")
        if self.capability_version < 1 or self.attempt_count < 0 or not self.reason_code.strip():
            raise ValueError("route decision version and reason are required")
        if self.target is RouteTarget.LOCAL_API and self.executor is not CapabilityExecutor.API:
            raise ValueError("local API route requires the API executor")
        if self.target is RouteTarget.WORKER_JOB and self.executor is not CapabilityExecutor.WORKER:
            raise ValueError("WorkerJob route requires the worker executor")
        if (
            self.target
            in {
                RouteTarget.WAITING_EXECUTION,
                RouteTarget.WAITING_INTERVENTION,
                RouteTarget.UNSUPPORTED,
            }
            and self.executor is not None
        ):
            raise ValueError("non-execution route cannot select an executor")
        object.__setattr__(self, "created_at", normalize_utc(self.created_at))

    @property
    def requires_account_coordination(self) -> bool:
        return self.operation_class.requires_exclusive_account_coordination
