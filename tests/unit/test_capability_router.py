from uuid import uuid4

from threads_platform.application.capability_router import CapabilityEvidence, CapabilityRouter
from threads_platform.domain.accounts import AccountExecutionMode
from threads_platform.domain.capabilities import (
    BusinessCapabilityPolicy,
    CapabilityExecutionClass,
    CapabilityExecutor,
    FallbackSafety,
    OperationClass,
    RouteTarget,
)
from threads_platform.domain.worker_jobs import WorkerJob, WorkerJobRetrySafety, WorkerJobStatus


def evidence(**changes: object) -> CapabilityEvidence:
    values: dict[str, object] = {
        "api_handler_available": True,
        "worker_assigned": True,
        "worker_online": True,
        "worker_advertises_capability": True,
    }
    values.update(changes)
    return CapabilityEvidence(**values)  # type: ignore[arg-type]


def test_api_only_always_uses_existing_api_handler() -> None:
    decision = CapabilityRouter().decide(
        "command-api-only",
        uuid4(),
        "threads.publish_text",
        AccountExecutionMode.API_ONLY,
        evidence(),
    )

    assert decision.target is RouteTarget.LOCAL_API
    assert decision.executor is CapabilityExecutor.API


def test_browser_only_never_falls_back_to_api() -> None:
    router = CapabilityRouter()
    remote = router.decide(
        "command-browser-only",
        uuid4(),
        "threads.publish_text",
        AccountExecutionMode.BROWSER_ONLY,
        evidence(api_handler_available=True),
    )
    unassigned = router.decide(
        "command-browser-only-unassigned",
        uuid4(),
        "threads.publish_text",
        AccountExecutionMode.BROWSER_ONLY,
        evidence(worker_assigned=False),
    )

    assert remote.target is RouteTarget.WORKER_JOB
    assert remote.executor is CapabilityExecutor.WORKER
    assert unassigned.target is RouteTarget.WAITING_EXECUTION
    assert unassigned.executor is None


def test_hybrid_prefers_api_and_allows_only_pre_attempt_safe_fallback() -> None:
    router = CapabilityRouter()
    policy = BusinessCapabilityPolicy(
        command_type="threads.example",
        capability_name="example",
        capability_version=1,
        execution_class=CapabilityExecutionClass.HYBRID,
        operation_class=OperationClass.MUTATION,
        preferred_executor=CapabilityExecutor.WORKER,
        fallback_executor=CapabilityExecutor.API,
        fallback_safety=FallbackSafety.BEFORE_FIRST_ATTEMPT,
        worker_capability_name="example",
        worker_capability_version=1,
    )
    router = CapabilityRouter({"threads.example": policy})

    preferred = router.decide(
        "command-preferred",
        uuid4(),
        "threads.example",
        AccountExecutionMode.HYBRID,
        evidence(),
    )
    fallback = router.decide(
        "command-safe-fallback",
        uuid4(),
        "threads.example",
        AccountExecutionMode.HYBRID,
        evidence(worker_online=False),
    )
    after_attempt = router.decide(
        "command-no-fallback",
        uuid4(),
        "threads.example",
        AccountExecutionMode.HYBRID,
        evidence(worker_online=False, attempt_count=1, previous_executor=CapabilityExecutor.API),
    )

    assert preferred.target is RouteTarget.WORKER_JOB
    assert fallback.target is RouteTarget.LOCAL_API
    assert after_attempt.target is RouteTarget.WAITING_INTERVENTION


def test_manual_busy_and_unsupported_routes_are_explicit() -> None:
    router = CapabilityRouter()
    manual = router.decide(
        "command-manual",
        uuid4(),
        "threads.publish_text",
        AccountExecutionMode.MANUAL,
        evidence(),
    )
    busy = router.decide(
        "command-busy",
        uuid4(),
        "threads.publish_text",
        AccountExecutionMode.HYBRID,
        evidence(account_mutation_busy=True),
    )
    unsupported = router.decide(
        "command-unsupported",
        uuid4(),
        "threads.not-supported",
        AccountExecutionMode.HYBRID,
        evidence(),
    )

    assert manual.target is RouteTarget.WAITING_INTERVENTION
    assert busy.target is RouteTarget.WAITING_EXECUTION
    assert unsupported.target is RouteTarget.UNSUPPORTED


def test_reconciliation_required_worker_job_never_routes_to_another_executor() -> None:
    job = WorkerJob(
        "threads.publish_text",
        1,
        command_id="command-ambiguous",
        account_id=uuid4(),
        status=WorkerJobStatus.WAITING_INTERVENTION,
        retry_safety=WorkerJobRetrySafety.RECONCILIATION_REQUIRED,
    )
    account_id = job.account_id
    assert account_id is not None

    decision = CapabilityRouter().decide(
        "command-ambiguous",
        account_id,
        "threads.publish_text",
        AccountExecutionMode.HYBRID,
        evidence(existing_worker_job=job),
    )

    assert decision.target is RouteTarget.WAITING_INTERVENTION
    assert decision.executor is None
