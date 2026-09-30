from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog

from threads_platform.application.ports.worker_agent import WorkerJobSnapshot
from threads_platform.application.worker_control import (
    AuthChallenge as ServiceAuthChallenge,
)
from threads_platform.application.worker_control import (
    EnrollmentIssued,
    WorkerAccessSession,
)
from threads_platform.domain.capabilities import OperationClass
from threads_platform.domain.commands import Command, CommandStatus
from threads_platform.domain.worker_jobs import (
    WorkerJob,
    WorkerJobAttempt,
    WorkerJobRetrySafety,
    WorkerJobStatus,
)
from threads_platform.domain.workers import (
    NetworkProfile,
    NetworkProtocol,
    WorkerAuthChallenge,
    WorkerEnrollment,
    WorkerSession,
)
from threads_platform.observability.logging import sanitize_log_event
from threads_platform.transport.http.workers import (
    CreateChallengeResponse,
    CreateEnrollmentResponse,
    EnrollWorkerRequest,
    ExchangeChallengeRequest,
    ExchangeChallengeResponse,
    WorkerAccountContextResponse,
    WorkerJobCheckpointRequest,
    WorkerJobCompleteRequest,
    WorkerJobResponse,
    WorkerNetworkProfileResponse,
)

_NOW = datetime(2026, 9, 30, tzinfo=UTC)
_ENROLLMENT_SECRET = "SYNTHETIC_ENROLLMENT_SECRET_SENTINEL"
_CHALLENGE_SECRET = "SYNTHETIC_CHALLENGE_NONCE_SENTINEL"
_SESSION_SECRET = "SYNTHETIC_WORKER_SESSION_SENTINEL"
_DOCUMENT_SECRET = "SYNTHETIC_DOCUMENT_SECRET_SENTINEL"
_SIGNATURE_SECRET = "SYNTHETIC_SIGNATURE_SECRET_SENTINEL"
_CREDENTIAL_REF = "secret-store://SYNTHETIC_CREDENTIAL_REF_SENTINEL"


def test_command_repr_hides_documents_and_execution_lease_but_keeps_safe_fields() -> None:
    lease_token = uuid4()
    command = Command(
        command_id="cmd-synthetic-70",
        correlation_id="corr-synthetic-70",
        account_id=uuid4(),
        command_type="conversation.sync",
        payload={"message": _DOCUMENT_SECRET},
        status=CommandStatus.PROCESSING,
        result={"response": _DOCUMENT_SECRET},
        execution_lease_token=lease_token,
        execution_lease_expires_at=_NOW + timedelta(minutes=1),
        checkpoint={"cursor": _DOCUMENT_SECRET},
    )

    rendered = repr(command)

    assert _DOCUMENT_SECRET not in rendered
    assert str(lease_token) not in rendered
    assert command.command_id in rendered
    assert command.correlation_id in rendered
    assert command.status.value in rendered


def test_worker_job_repr_hides_documents_and_lease_but_keeps_safe_fields() -> None:
    lease_token = uuid4()
    worker_id = uuid4()
    job = WorkerJob(
        capability_name="synthetic.echo",
        capability_version=1,
        operation_class=OperationClass.READ,
        input_data={"payload": _DOCUMENT_SECRET},
        status=WorkerJobStatus.RUNNING,
        lease_worker_id=worker_id,
        lease_token=lease_token,
        lease_expires_at=_NOW + timedelta(minutes=1),
        checkpoint={"step": _DOCUMENT_SECRET},
        result={"output": _DOCUMENT_SECRET},
    )

    rendered = repr(job)

    assert _DOCUMENT_SECRET not in rendered
    assert str(lease_token) not in rendered
    assert str(job.id) in rendered
    assert job.status.value in rendered
    assert job.capability_name in rendered


def test_worker_job_attempt_and_agent_snapshot_hide_lease_and_documents() -> None:
    worker_job_id = uuid4()
    worker_id = uuid4()
    lease_token = uuid4()
    attempt = WorkerJobAttempt(worker_job_id, 1, worker_id, lease_token)
    snapshot = WorkerJobSnapshot(
        job_id=worker_job_id,
        capability_name="synthetic.echo",
        capability_version=1,
        status=WorkerJobStatus.RUNNING,
        account_id=None,
        assigned_worker_id=worker_id,
        lease_worker_id=worker_id,
        lease_token=lease_token,
        lease_expires_at=_NOW + timedelta(minutes=1),
        retry_safety=WorkerJobRetrySafety.SAFE_TO_RETRY,
        checkpoint={"step": _DOCUMENT_SECRET},
        input_data={"payload": _DOCUMENT_SECRET},
    )

    assert str(lease_token) not in repr(attempt)
    assert str(lease_token) not in repr(snapshot)
    assert _DOCUMENT_SECRET not in repr(snapshot)
    assert snapshot.capability_name in repr(snapshot)


def test_worker_auth_domain_objects_hide_digest_nonce_and_credential_reference() -> None:
    worker_id = uuid4()
    enrollment = WorkerEnrollment("d" * 64, _NOW + timedelta(minutes=10))
    challenge = WorkerAuthChallenge(worker_id, _CHALLENGE_SECRET, _NOW + timedelta(minutes=1))
    session = WorkerSession(worker_id, "e" * 64, _NOW + timedelta(minutes=15))
    profile = NetworkProfile(
        account_id=uuid4(),
        name="synthetic proxy",
        protocol=NetworkProtocol.SOCKS5,
        host="proxy.invalid",
        port=1080,
        credential_ref=_CREDENTIAL_REF,
    )

    assert "d" * 64 not in repr(enrollment)
    assert _CHALLENGE_SECRET not in repr(challenge)
    assert "e" * 64 not in repr(session)
    assert _CREDENTIAL_REF not in repr(profile)
    assert str(worker_id) in repr(challenge)


def test_worker_control_return_objects_hide_transient_auth_material() -> None:
    challenge_id = uuid4()
    code = EnrollmentIssued(_ENROLLMENT_SECRET, _NOW + timedelta(minutes=10))
    challenge = ServiceAuthChallenge(challenge_id, _CHALLENGE_SECRET, _NOW + timedelta(minutes=1))
    session = WorkerAccessSession(_SESSION_SECRET, _NOW + timedelta(minutes=15))

    assert _ENROLLMENT_SECRET not in repr(code)
    assert _CHALLENGE_SECRET not in repr(challenge)
    assert str(challenge_id) in repr(challenge)
    assert _SESSION_SECRET not in repr(session)


def test_worker_http_model_repr_hides_secrets_without_changing_model_dump() -> None:
    worker_id = uuid4()
    challenge_id = uuid4()
    account_id = uuid4()
    profile_id = uuid4()
    signature = _SIGNATURE_SECRET + "X" * 55

    enroll_request = EnrollWorkerRequest(
        enrollment_code=_ENROLLMENT_SECRET,
        worker_id=worker_id,
        display_name="Synthetic worker",
        hostname="SYNTHETIC-HOST",
        platform="windows",
        public_key="A" * 44,
    )
    enrollment_response = CreateEnrollmentResponse(
        enrollment_code=_ENROLLMENT_SECRET,
        expires_at=_NOW,
    )
    challenge_response = CreateChallengeResponse(
        challenge_id=challenge_id,
        nonce=_CHALLENGE_SECRET,
        expires_at=_NOW,
    )
    challenge_request = ExchangeChallengeRequest(
        challenge_id=challenge_id,
        signature=signature,
    )
    session_response = ExchangeChallengeResponse(
        access_token=_SESSION_SECRET,
        expires_at=_NOW,
    )
    account_context = WorkerAccountContextResponse(
        account_id=account_id,
        worker_id=worker_id,
        profile_ref="synthetic-profile",
        network_profile=WorkerNetworkProfileResponse(
            id=profile_id,
            protocol=NetworkProtocol.SOCKS5,
            host="proxy.invalid",
            port=1080,
            credential_ref=_CREDENTIAL_REF,
        ),
    )

    assert _ENROLLMENT_SECRET not in repr(enroll_request)
    assert _ENROLLMENT_SECRET not in repr(enrollment_response)
    assert _CHALLENGE_SECRET not in repr(challenge_response)
    assert _SIGNATURE_SECRET not in repr(challenge_request)
    assert _SESSION_SECRET not in repr(session_response)
    assert _CREDENTIAL_REF not in repr(account_context)

    assert enroll_request.model_dump() == {
        "enrollment_code": _ENROLLMENT_SECRET,
        "worker_id": worker_id,
        "display_name": "Synthetic worker",
        "hostname": "SYNTHETIC-HOST",
        "platform": "windows",
        "public_key": "A" * 44,
        "max_concurrent_jobs": 1,
        "max_browser_sessions": 1,
    }
    assert enrollment_response.model_dump() == {
        "enrollment_code": _ENROLLMENT_SECRET,
        "expires_at": _NOW,
    }
    assert challenge_response.model_dump() == {
        "challenge_id": challenge_id,
        "nonce": _CHALLENGE_SECRET,
        "expires_at": _NOW,
    }
    assert challenge_request.model_dump()["signature"] == signature
    assert session_response.model_dump()["access_token"] == _SESSION_SECRET
    assert account_context.model_dump()["network_profile"]["credential_ref"] == _CREDENTIAL_REF


def test_worker_job_http_model_repr_hides_documents_and_lease_without_changing_dump() -> None:
    lease_token = uuid4()
    response = WorkerJobResponse(
        id=uuid4(),
        command_id="cmd-synthetic-75",
        account_id=None,
        assigned_worker_id=None,
        capability_name="synthetic.echo",
        capability_version=1,
        status=WorkerJobStatus.QUEUED,
        priority=0,
        preemptible=True,
        scheduled_at=_NOW,
        deadline_at=None,
        attempt_count=0,
        max_attempts=3,
        retry_safety=WorkerJobRetrySafety.SAFE_TO_RETRY,
        input_data={"payload": _DOCUMENT_SECRET},
        lease_worker_id=None,
        lease_token=lease_token,
        lease_expires_at=None,
        checkpoint={"step": _DOCUMENT_SECRET},
        result={"value": _DOCUMENT_SECRET},
        error_code=None,
    )
    checkpoint_request = WorkerJobCheckpointRequest(
        lease_token=lease_token,
        checkpoint={"step": _DOCUMENT_SECRET},
    )
    complete_request = WorkerJobCompleteRequest(
        lease_token=lease_token,
        result={"value": _DOCUMENT_SECRET},
    )

    for model in (response, checkpoint_request, complete_request):
        assert _DOCUMENT_SECRET not in repr(model)
        assert str(lease_token) not in repr(model)
    assert response.model_dump()["input_data"] == {"payload": _DOCUMENT_SECRET}
    assert response.model_dump()["lease_token"] == lease_token
    assert response.model_dump()["checkpoint"] == {"step": _DOCUMENT_SECRET}
    assert response.model_dump()["result"] == {"value": _DOCUMENT_SECRET}
    assert checkpoint_request.model_dump()["lease_token"] == lease_token
    assert checkpoint_request.model_dump()["checkpoint"] == {"step": _DOCUMENT_SECRET}
    assert complete_request.model_dump()["lease_token"] == lease_token
    assert complete_request.model_dump()["result"] == {"value": _DOCUMENT_SECRET}


def test_safe_auth_repr_strings_remain_safe_through_logging_pipeline() -> None:
    worker_id = uuid4()
    challenge = WorkerAuthChallenge(worker_id, _CHALLENGE_SECRET, _NOW + timedelta(minutes=1))
    session = WorkerAccessSession(_SESSION_SECRET, _NOW + timedelta(minutes=15))
    response = CreateEnrollmentResponse(
        enrollment_code=_ENROLLMENT_SECRET,
        expires_at=_NOW,
    )
    profile = NetworkProfile(
        account_id=uuid4(),
        name="synthetic proxy",
        protocol=NetworkProtocol.SOCKS5,
        host="proxy.invalid",
        port=1080,
        credential_ref=_CREDENTIAL_REF,
    )
    source = {
        "event": "safe_repr_regression",
        "object_repr": [repr(challenge), repr(session), repr(response), repr(profile)],
        "authorization": "Bearer SYNTHETIC_BEARER_SENTINEL",
    }

    sanitized = sanitize_log_event(None, "info", source)
    rendered_value = structlog.processors.JSONRenderer()(None, "info", sanitized)
    rendered = rendered_value.decode() if isinstance(rendered_value, bytes) else rendered_value

    for sentinel in (
        _CHALLENGE_SECRET,
        _SESSION_SECRET,
        _ENROLLMENT_SECRET,
        _CREDENTIAL_REF,
        "SYNTHETIC_BEARER_SENTINEL",
    ):
        assert sentinel not in rendered
    assert "worker_id" in rendered
    assert "[REDACTED]" in rendered
