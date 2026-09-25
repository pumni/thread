from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from threads_platform.domain.accounts import AccountExecutionMode, AccountStatus
from threads_platform.domain.capabilities import (
    BusinessCapabilityPolicy,
    CapabilityExecutionClass,
    CapabilityExecutor,
    CapabilityRouteDecision,
    FallbackSafety,
    OperationClass,
    RouteTarget,
)
from threads_platform.domain.worker_jobs import WorkerJob, WorkerJobRetrySafety, WorkerJobStatus


def _policy(
    command_type: str,
    execution_class: CapabilityExecutionClass,
    operation_class: OperationClass,
    *,
    worker_capability: bool = False,
    fallback_executor: CapabilityExecutor | None = None,
) -> BusinessCapabilityPolicy:
    version = 1
    return BusinessCapabilityPolicy(
        command_type=command_type,
        capability_name=command_type,
        capability_version=version,
        execution_class=execution_class,
        operation_class=operation_class,
        preferred_executor=CapabilityExecutor.API,
        fallback_executor=fallback_executor,
        fallback_safety=(
            FallbackSafety.BEFORE_FIRST_ATTEMPT
            if fallback_executor is not None
            else FallbackSafety.NEVER
        ),
        worker_capability_name=command_type if worker_capability else None,
        worker_capability_version=version if worker_capability else None,
    )


_POLICIES = (
    _policy(
        "threads.publish_text",
        CapabilityExecutionClass.HYBRID,
        OperationClass.MUTATION,
        worker_capability=True,
        fallback_executor=CapabilityExecutor.WORKER,
    ),
    _policy(
        "threads.publish_image",
        CapabilityExecutionClass.HYBRID,
        OperationClass.MUTATION,
        worker_capability=True,
        fallback_executor=CapabilityExecutor.WORKER,
    ),
    _policy(
        "threads.publish_video",
        CapabilityExecutionClass.HYBRID,
        OperationClass.MUTATION,
        worker_capability=True,
        fallback_executor=CapabilityExecutor.WORKER,
    ),
    _policy(
        "threads.publish_carousel",
        CapabilityExecutionClass.HYBRID,
        OperationClass.MUTATION,
        worker_capability=True,
        fallback_executor=CapabilityExecutor.WORKER,
    ),
    _policy(
        "threads.create_reply",
        CapabilityExecutionClass.HYBRID,
        OperationClass.MUTATION,
        worker_capability=True,
        fallback_executor=CapabilityExecutor.WORKER,
    ),
    _policy(
        "threads.sync_conversation",
        CapabilityExecutionClass.NATIVE_API,
        OperationClass.READ,
    ),
    _policy(
        "threads.moderate_reply",
        CapabilityExecutionClass.NATIVE_API,
        OperationClass.MUTATION,
    ),
)

BUSINESS_CAPABILITIES: Mapping[str, BusinessCapabilityPolicy] = {
    policy.command_type: policy for policy in _POLICIES
}
KNOWN_COMMAND_TYPES = frozenset(BUSINESS_CAPABILITIES)


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    api_handler_available: bool
    worker_assigned: bool
    worker_online: bool
    worker_advertises_capability: bool
    account_mutation_busy: bool = False
    account_status: AccountStatus = AccountStatus.ACTIVE
    attempt_count: int = 0
    previous_executor: CapabilityExecutor | None = None
    existing_worker_job: WorkerJob | None = None


class CapabilityRouter:
    def __init__(
        self,
        policies: Mapping[str, BusinessCapabilityPolicy] = BUSINESS_CAPABILITIES,
    ) -> None:
        self._policies = dict(policies)

    def policy_for(self, command_type: str) -> BusinessCapabilityPolicy | None:
        return self._policies.get(command_type)

    def worker_execution_allowed(
        self, command_type: str, account_mode: AccountExecutionMode
    ) -> bool:
        policy = self.policy_for(command_type)
        worker_allowed = policy is not None and CapabilityExecutor.WORKER in {
            policy.preferred_executor,
            policy.fallback_executor,
        }
        return (
            policy is not None
            and policy.execution_class
            not in {
                CapabilityExecutionClass.UNSUPPORTED,
                CapabilityExecutionClass.HUMAN_ASSISTED,
            }
            and worker_allowed
            and policy.worker_capability_name is not None
            and account_mode in {AccountExecutionMode.BROWSER_ONLY, AccountExecutionMode.HYBRID}
        )

    def decide(
        self,
        command_id: str,
        account_id: UUID,
        command_type: str,
        account_mode: AccountExecutionMode,
        evidence: CapabilityEvidence,
    ) -> CapabilityRouteDecision:
        policy = self.policy_for(command_type)
        if policy is None:
            return CapabilityRouteDecision(
                command_id,
                account_id,
                command_type,
                1,
                CapabilityExecutionClass.UNSUPPORTED,
                OperationClass.READ,
                account_mode,
                RouteTarget.UNSUPPORTED,
                None,
                "UNSUPPORTED_CAPABILITY",
            )

        if evidence.existing_worker_job is not None:
            job = evidence.existing_worker_job
            requires_intervention = (
                job.status is WorkerJobStatus.WAITING_INTERVENTION
                or job.retry_safety is WorkerJobRetrySafety.RECONCILIATION_REQUIRED
            )
            return self._decision(
                command_id,
                account_id,
                policy,
                account_mode,
                RouteTarget.WAITING_INTERVENTION
                if requires_intervention
                else RouteTarget.WAITING_EXECUTION,
                None,
                "WORKER_JOB_REQUIRES_INTERVENTION"
                if requires_intervention
                else "EXISTING_WORKER_JOB",
            )

        if policy.execution_class is CapabilityExecutionClass.UNSUPPORTED:
            return self._decision(
                command_id,
                account_id,
                policy,
                account_mode,
                RouteTarget.UNSUPPORTED,
                None,
                "UNSUPPORTED_CAPABILITY",
            )
        if evidence.account_status is not AccountStatus.ACTIVE:
            return self._decision(
                command_id,
                account_id,
                policy,
                account_mode,
                RouteTarget.WAITING_INTERVENTION,
                None,
                "ACCOUNT_NOT_READY_FOR_EXECUTION",
            )
        if account_mode is AccountExecutionMode.MANUAL:
            return self._decision(
                command_id,
                account_id,
                policy,
                account_mode,
                RouteTarget.WAITING_INTERVENTION,
                None,
                "ACCOUNT_REQUIRES_MANUAL_EXECUTION",
            )
        if (
            policy.operation_class.requires_exclusive_account_coordination
            and evidence.account_mutation_busy
        ):
            return self._decision(
                command_id,
                account_id,
                policy,
                account_mode,
                RouteTarget.WAITING_EXECUTION,
                None,
                "ACCOUNT_EXECUTION_ALREADY_OWNED",
            )
        if policy.execution_class is CapabilityExecutionClass.HUMAN_ASSISTED:
            return self._decision(
                command_id,
                account_id,
                policy,
                account_mode,
                RouteTarget.WAITING_INTERVENTION,
                None,
                "HUMAN_ASSISTANCE_REQUIRED",
            )

        api_policy_allowed = CapabilityExecutor.API in {
            policy.preferred_executor,
            policy.fallback_executor,
        }
        worker_policy_allowed = CapabilityExecutor.WORKER in {
            policy.preferred_executor,
            policy.fallback_executor,
        }
        worker_supported = worker_policy_allowed and policy.worker_capability_name is not None
        worker_ready = (
            evidence.worker_assigned
            and evidence.worker_online
            and evidence.worker_advertises_capability
        )
        if account_mode is AccountExecutionMode.API_ONLY:
            if not api_policy_allowed:
                target, executor, reason = (
                    RouteTarget.UNSUPPORTED,
                    None,
                    "CAPABILITY_HAS_NO_API_EXECUTOR",
                )
            else:
                target, executor, reason = (
                    (RouteTarget.LOCAL_API, CapabilityExecutor.API, "API_ONLY_LOCAL_API")
                    if evidence.api_handler_available
                    else (RouteTarget.WAITING_EXECUTION, None, "API_EXECUTOR_UNAVAILABLE")
                )
        elif account_mode is AccountExecutionMode.BROWSER_ONLY:
            if not worker_supported:
                target, executor, reason = (
                    RouteTarget.UNSUPPORTED,
                    None,
                    "CAPABILITY_HAS_NO_WORKER_EXECUTOR",
                )
            elif evidence.worker_assigned and evidence.worker_advertises_capability:
                target, executor, reason = (
                    RouteTarget.WORKER_JOB,
                    CapabilityExecutor.WORKER,
                    "BROWSER_ONLY_ASSIGNED_WORKER",
                )
            else:
                target, executor, reason = (
                    RouteTarget.WAITING_EXECUTION,
                    None,
                    "ACTIVE_WORKER_CAPABILITY_REQUIRED"
                    if evidence.worker_assigned
                    else "ACTIVE_WORKER_ASSIGNMENT_REQUIRED",
                )
        elif policy.preferred_executor is CapabilityExecutor.API:
            if evidence.api_handler_available:
                target, executor, reason = (
                    RouteTarget.LOCAL_API,
                    CapabilityExecutor.API,
                    "PREFERRED_API_AVAILABLE",
                )
            elif (
                policy.fallback_executor is CapabilityExecutor.WORKER
                and policy.fallback_safety is FallbackSafety.BEFORE_FIRST_ATTEMPT
                and evidence.attempt_count == 0
                and worker_supported
                and evidence.worker_assigned
                and evidence.worker_advertises_capability
            ):
                target, executor, reason = (
                    RouteTarget.WORKER_JOB,
                    CapabilityExecutor.WORKER,
                    "API_UNAVAILABLE_SAFE_WORKER_FALLBACK",
                )
            else:
                target, executor, reason = (
                    RouteTarget.WAITING_EXECUTION,
                    None,
                    "NO_SAFE_EXECUTOR_AVAILABLE",
                )
        elif worker_ready:
            target, executor, reason = (
                RouteTarget.WORKER_JOB,
                CapabilityExecutor.WORKER,
                "PREFERRED_WORKER_AVAILABLE",
            )
        elif (
            policy.fallback_executor is CapabilityExecutor.API
            and policy.fallback_safety is FallbackSafety.BEFORE_FIRST_ATTEMPT
            and evidence.attempt_count == 0
            and evidence.api_handler_available
        ):
            target, executor, reason = (
                RouteTarget.LOCAL_API,
                CapabilityExecutor.API,
                "WORKER_UNAVAILABLE_SAFE_API_FALLBACK",
            )
        elif worker_supported and evidence.worker_assigned:
            target, executor, reason = (
                RouteTarget.WORKER_JOB,
                CapabilityExecutor.WORKER,
                "WORKER_UNAVAILABLE_WAITING",
            )
        else:
            target, executor, reason = (
                RouteTarget.WAITING_EXECUTION,
                None,
                "WORKER_EXECUTOR_UNAVAILABLE",
            )

        if evidence.attempt_count > 0 and target in {
            RouteTarget.LOCAL_API,
            RouteTarget.WORKER_JOB,
        }:
            if evidence.previous_executor is None or executor is not evidence.previous_executor:
                target, executor, reason = (
                    RouteTarget.WAITING_INTERVENTION,
                    None,
                    "EXECUTOR_SWITCH_REQUIRES_RECONCILIATION",
                )
        return self._decision(
            command_id,
            account_id,
            policy,
            account_mode,
            target,
            executor,
            reason,
        )

    @staticmethod
    def _decision(
        command_id: str,
        account_id: UUID,
        policy: BusinessCapabilityPolicy,
        account_mode: AccountExecutionMode,
        target: RouteTarget,
        executor: CapabilityExecutor | None,
        reason_code: str,
    ) -> CapabilityRouteDecision:
        return CapabilityRouteDecision(
            command_id=command_id,
            account_id=account_id,
            capability_name=policy.capability_name,
            capability_version=policy.capability_version,
            execution_class=policy.execution_class,
            operation_class=policy.operation_class,
            account_mode=account_mode,
            target=target,
            executor=executor,
            reason_code=reason_code,
        )
