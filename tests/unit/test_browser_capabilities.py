from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from threads_platform.application.browser_capabilities import (
    BROWSER_CAPABILITY_CONTRACTS,
    COMMON_FAILURES,
    BrowserCapabilityContract,
    BrowserCapabilityStatus,
    BrowserFeedResultV1,
    BrowserMediaStageResultV1,
    BrowserTargetOpenResultV1,
)
from threads_platform.application.capability_router import CapabilityEvidence, CapabilityRouter
from threads_platform.application.commands.runtime import KNOWN_COMMAND_TYPES
from threads_platform.application.crm_protocol_v1 import (
    COMMAND_ENVELOPE_ADAPTER,
    BrowserProfileOpenCommandV1,
    BrowserThreadOpenCommandV1,
)
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.application.worker_control import WorkerControlError, WorkerControlService
from threads_platform.domain.accounts import AccountExecutionMode
from threads_platform.domain.browser_media import BrowserMediaKind
from threads_platform.domain.capabilities import RouteTarget
from threads_platform.domain.workers import WorkerCapability


def test_c5_contracts_are_typed_versioned_with_evidence_backed_capabilities_available() -> None:
    expected = {
        "threads.browser.feed.browse": ("READ", True, "BrowserFeedResultV1"),
        "threads.browser.thread.open": ("READ", True, "BrowserTargetOpenResultV1"),
        "threads.browser.profile.open": ("READ", True, "BrowserTargetOpenResultV1"),
        "threads.browser.media.local_upload": ("MUTATION", False, "BrowserMediaStageResultV1"),
    }
    assert {contract.name for contract in BROWSER_CAPABILITY_CONTRACTS} == set(expected)

    router = CapabilityRouter()
    evidence = CapabilityEvidence(
        api_handler_available=True,
        worker_assigned=True,
        worker_online=True,
        worker_advertises_capability=True,
    )
    for contract in BROWSER_CAPABILITY_CONTRACTS:
        operation, preemptible, result_schema = expected[contract.name]
        assert contract.version == 1
        assert contract.operation_class.value == operation
        assert contract.preemptible is preemptible
        assert contract.result_schema == result_schema
        assert contract.result_schema_version == 1
        assert contract.status is BrowserCapabilityStatus.AVAILABLE
        assert contract.blocked_reason_code is None
        assert contract.required_session_state.value == "AUTHENTICATED"
        assert contract.irreversible_boundary is (
            contract.name == "threads.browser.media.local_upload"
        )
        assert "REMOTE_STATE_UNCERTAIN" in contract.intervention_types
        if contract.name == "threads.browser.thread.open":
            assert contract.safe_checkpoints == ("BEFORE_NAVIGATION", "THREAD_READY")
            assert contract.max_duration_seconds == 30
        if contract.name == "threads.browser.profile.open":
            assert contract.safe_checkpoints == (
                "BEFORE_NAVIGATION",
                "BEFORE_PROFILE_INSPECTION",
                "PROFILE_READY",
            )
        assert contract.name in KNOWN_COMMAND_TYPES
        policy = router.policy_for(contract.name)
        assert policy is not None
        assert policy.worker_capability_name == contract.name
        assert policy.blocked_reason_code == contract.blocked_reason_code
        assert (
            router.worker_execution_allowed(contract.name, AccountExecutionMode.BROWSER_ONLY)
            is True
        )
        for mode in AccountExecutionMode:
            decision = router.decide(
                f"cmd-{contract.name.rsplit('.', 1)[-1]}",
                uuid4(),
                contract.name,
                mode,
                evidence,
            )
            if mode in {AccountExecutionMode.BROWSER_ONLY, AccountExecutionMode.HYBRID}:
                assert decision.target is RouteTarget.WORKER_JOB
            elif mode is AccountExecutionMode.MANUAL:
                assert decision.target is RouteTarget.WAITING_INTERVENTION
            else:
                assert decision.target is RouteTarget.UNSUPPORTED

    contracts = {contract.name: contract for contract in BROWSER_CAPABILITY_CONTRACTS}
    assert contracts["threads.browser.profile.open"].allowed_failure_codes == (
        COMMON_FAILURES
        | {
            "BROWSER_SESSION_UNAVAILABLE",
            "BROWSER_NETWORK_ROUTE_UNSUPPORTED",
            "UNSUPPORTED_BROWSER_CAPABILITY",
            "WORKER_JOB_INPUT_INVALID",
            "WORKER_JOB_RETRY_SAFETY_MISMATCH",
        }
    )
    assert contracts["threads.browser.media.local_upload"].allowed_failure_codes == (
        COMMON_FAILURES
        | {
            "BROWSER_SESSION_UNAVAILABLE",
            "BROWSER_NETWORK_ROUTE_UNSUPPORTED",
            "UNSUPPORTED_BROWSER_CAPABILITY",
            "WORKER_JOB_INPUT_INVALID",
            "WORKER_JOB_RETRY_SAFETY_MISMATCH",
            "MEDIA_FILE_UNAVAILABLE",
            "MEDIA_FILE_REJECTED",
            "MEDIA_FILE_TOO_LARGE",
        }
    )
    media = contracts["threads.browser.media.local_upload"]
    assert media.allowed_upload_extensions == frozenset({".jpg", ".jpeg", ".png", ".webp"})
    assert media.intervention_types == frozenset(
        {
            "LOGIN_REQUIRED",
            "SESSION_EXPIRED",
            "CHALLENGE_REQUIRED",
            "REMOTE_STATE_UNCERTAIN",
            "AMBIGUOUS_OUTCOME",
            "OPERATOR_CONFIRMATION_REQUIRED",
        }
    )


@pytest.mark.parametrize(
    ("command_type", "payload"),
    [
        ("threads.browser.feed.browse", {"max_items": 20}),
        ("threads.browser.thread.open", {"thread_ref": "/@alice/post/remote_thread-1"}),
        ("threads.browser.profile.open", {"profile_ref": "/@alice/"}),
        ("threads.browser.media.local_upload", {"media_ref": "asset-1.jpg"}),
    ],
)
def test_c5_command_payloads_are_strict_and_bounded(
    command_type: str, payload: dict[str, object]
) -> None:
    command = COMMAND_ENVELOPE_ADAPTER.validate_python(
        {
            "protocol_version": 1,
            "command_id": "browser-capability-1",
            "correlation_id": "browser-capability-1",
            "account_id": uuid4(),
            "created_at": datetime.now(UTC),
            "command_type": command_type,
            "payload": payload,
        }
    )

    assert command.command_type == command_type
    expected_payload = (
        {"profile_ref": "/@alice"} if command_type == "threads.browser.profile.open" else payload
    )
    assert command.payload.model_dump(mode="python") == expected_payload


def test_c5_commands_reject_arbitrary_urls_unbounded_feed_and_local_paths() -> None:
    header = {
        "protocol_version": 1,
        "command_id": "browser-capability-invalid",
        "correlation_id": "browser-capability-invalid",
        "account_id": uuid4(),
        "created_at": datetime.now(UTC),
    }
    invalid = (
        ("threads.browser.feed.browse", {"max_items": 21}),
        ("threads.browser.thread.open", {"thread_ref": "thread-1"}),
        ("threads.browser.thread.open", {"thread_ref": "https://threads.net/t/123"}),
        ("threads.browser.thread.open", {"thread_ref": "/@alice/post/post-1?source=feed"}),
        ("threads.browser.thread.open", {"thread_ref": "/@alice/post/post-1#reply"}),
        ("threads.browser.thread.open", {"thread_ref": "/@alice/post/../post-1"}),
        ("threads.browser.profile.open", {"profile_ref": "https://threads.net/@user"}),
        ("threads.browser.profile.open", {"profile_ref": "/@alice?source=profile"}),
        ("threads.browser.profile.open", {"profile_ref": "/@alice#posts"}),
        ("threads.browser.profile.open", {"profile_ref": "/@alice/post/post-1"}),
        ("threads.browser.media.local_upload", {"media_ref": "..\\secret.jpg"}),
        ("threads.browser.media.local_upload", {"media_ref": "CON.txt"}),
        ("threads.browser.media.local_upload", {"media_ref": "asset.jpg", "path": "C:\\x"}),
    )
    for command_type, payload in invalid:
        with pytest.raises(ValidationError):
            COMMAND_ENVELOPE_ADAPTER.validate_python(
                {**header, "command_type": command_type, "payload": payload}
            )

    trailing_slash = COMMAND_ENVELOPE_ADAPTER.validate_python(
        {
            **header,
            "command_type": "threads.browser.thread.open",
            "payload": {"thread_ref": "/@alice/post/post-1/"},
        }
    )
    assert isinstance(trailing_slash, BrowserThreadOpenCommandV1)
    assert trailing_slash.payload.thread_ref == "/@alice/post/post-1"

    trailing_profile_slash = COMMAND_ENVELOPE_ADAPTER.validate_python(
        {
            **header,
            "command_type": "threads.browser.profile.open",
            "payload": {"profile_ref": "/@alice/"},
        }
    )
    assert isinstance(trailing_profile_slash, BrowserProfileOpenCommandV1)
    assert trailing_profile_slash.payload.profile_ref == "/@alice"


def test_c5_result_schemas_exclude_dom_and_worker_local_paths() -> None:
    feed = BrowserFeedResultV1.model_validate(
        {"observations": [{"thread_ref": "thread-1", "position": 0}], "truncated": False}
    )
    target = BrowserTargetOpenResultV1.model_validate(
        {
            "target_kind": "THREAD",
            "target_ref": "/@alice/post/thread-1",
            "recognized": True,
        }
    )
    profile_target = BrowserTargetOpenResultV1.model_validate(
        {
            "target_kind": "PROFILE",
            "target_ref": "/@alice",
            "recognized": True,
        }
    )
    upload = BrowserMediaStageResultV1.model_validate(
        {"media_kind": BrowserMediaKind.IMAGE, "byte_size": 1024, "staged": True}
    )
    assert (
        feed.result_version
        == target.result_version
        == profile_target.result_version
        == upload.result_version
        == 1
    )
    assert target.recognized is profile_target.recognized is True
    with pytest.raises(ValidationError):
        BrowserTargetOpenResultV1.model_validate(
            {"target_kind": "THREAD", "target_ref": "thread-1", "recognized": False}
        )
    with pytest.raises(ValidationError):
        BrowserFeedResultV1.model_validate(
            {"observations": [], "truncated": False, "html": "<body>"}
        )
    with pytest.raises(ValidationError):
        BrowserMediaStageResultV1.model_validate(
            {
                "media_kind": "IMAGE",
                "byte_size": 1024,
                "staged": True,
                "path": "C:\\worker\\media\\asset.jpg",
            }
        )


@pytest.mark.parametrize(
    "contract",
    [
        contract
        for contract in BROWSER_CAPABILITY_CONTRACTS
        if contract.status is BrowserCapabilityStatus.BLOCKED_UI_EVIDENCE
    ],
)
async def test_workers_cannot_advertise_ui_evidence_blocked_capabilities(
    contract: BrowserCapabilityContract,
) -> None:
    worker_id = uuid4()
    service = WorkerControlService(cast(UnitOfWorkFactory, lambda: None))

    with pytest.raises(WorkerControlError, match="WORKER_ADVERTISED_BLOCKED_CAPABILITY"):
        await service.hello(
            worker_id,
            protocol_version=1,
            agent_version="1.0.0",
            capabilities_schema_version=1,
            capabilities=[WorkerCapability(worker_id, contract.name, contract.version)],
        )
