from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from threads_platform.application.capability_router import (
    BUSINESS_CAPABILITIES,
    CapabilityEvidence,
    CapabilityRouter,
)
from threads_platform.application.crm_protocol_v1 import COMMAND_ENVELOPE_ADAPTER
from threads_platform.domain.accounts import AccountExecutionMode
from threads_platform.domain.capabilities import (
    CapabilityExecutionClass,
    CapabilityExecutor,
    OperationClass,
    RouteTarget,
)
from threads_platform.domain.discovery import (
    DiscoveryEvidenceSource,
    DiscoveryRunCursor,
    DiscoverySourceEvidence,
    LeadCandidate,
    LeadCandidateStatus,
)


def search_command_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "campaign_id": str(uuid4()),
        "query": "public discussion",
        "search_mode": "KEYWORD",
        "search_type": "TOP",
        "max_pages": 3,
    }
    payload.update(changes)
    return {
        "protocol_version": 1,
        "command_id": "c4-search-command",
        "correlation_id": "c4-search-correlation",
        "account_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "command_type": "threads.discovery.search",
        "payload": payload,
    }


def test_discovery_command_schema_is_explicit_bounded_and_rejects_extra_fields() -> None:
    command = COMMAND_ENVELOPE_ADAPTER.validate_python(search_command_payload())
    assert command.command_type == "threads.discovery.search"
    assert command.payload.max_pages == 3

    with pytest.raises(ValidationError):
        COMMAND_ENVELOPE_ADAPTER.validate_python(search_command_payload(max_pages=6))
    with pytest.raises(ValidationError):
        COMMAND_ENVELOPE_ADAPTER.validate_python(search_command_payload(arbitrary_query={}))


def test_discovery_router_policies_are_api_only_and_do_not_advertise_worker_fallback() -> None:
    router = CapabilityRouter()
    for command_type in (
        "threads.discovery.search",
        "threads.discovery.profile",
        "threads.discovery.mentions",
        "threads.discovery.conversation",
        "threads.discovery.resume",
    ):
        policy = BUSINESS_CAPABILITIES[command_type]
        assert policy.execution_class is CapabilityExecutionClass.NATIVE_API
        assert policy.operation_class is OperationClass.READ
        assert policy.preferred_executor is CapabilityExecutor.API
        assert policy.fallback_executor is None
        assert policy.worker_capability_name is None
        decision = router.decide(
            command_type,
            uuid4(),
            command_type,
            AccountExecutionMode.API_ONLY,
            CapabilityEvidence(True, True, True, True),
        )
        assert decision.target is RouteTarget.LOCAL_API
        assert decision.executor is CapabilityExecutor.API

    browser_only = router.decide(
        "browser-only-c4",
        uuid4(),
        "threads.discovery.search",
        AccountExecutionMode.BROWSER_ONLY,
        CapabilityEvidence(True, True, True, True),
    )
    assert browser_only.target is RouteTarget.UNSUPPORTED
    assert browser_only.executor is None


def test_source_evidence_requires_one_canonical_entity_and_cursor_hash_is_bounded() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        DiscoverySourceEvidence(
            run_id=uuid4(),
            source=DiscoveryEvidenceSource.MENTIONS,
            page_number=1,
        )
    with pytest.raises(ValueError):
        DiscoveryRunCursor(run_id=uuid4(), cursor_digest="short", page_number=1)


def test_lead_candidate_lifecycle_records_valid_transitions_and_fails_closed() -> None:
    candidate = LeadCandidate(account_id=uuid4(), author_id=uuid4())
    first = candidate.transition(
        LeadCandidateStatus.CANDIDATE,
        command_id="lead-candidate",
        reason_code="OPERATOR_REVIEWED",
        occurred_at=datetime.now(UTC),
    )
    assert first.previous_status is LeadCandidateStatus.ENRICHMENT_PENDING
    assert candidate.status is LeadCandidateStatus.CANDIDATE
    candidate.transition(
        LeadCandidateStatus.READY,
        command_id="lead-ready",
        reason_code="PROFILE_REVIEWED",
        occurred_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="not allowed"):
        candidate.transition(
            LeadCandidateStatus.ENRICHMENT_PENDING,
            command_id="lead-reopen",
            reason_code="REOPEN",
            occurred_at=datetime.now(UTC),
        )
