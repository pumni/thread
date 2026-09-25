from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from threads_platform.application.worker_control import (
    WorkerControlError,
    WorkerControlService,
)
from threads_platform.domain.workers import WorkerCapability, WorkerStatus
from threads_platform.infrastructure.persistence.models import (
    WorkerAuditEventRecord,
    WorkerEnrollmentRecord,
    WorkerNodeRecord,
    WorkerSessionRecord,
)
from threads_platform.infrastructure.security.worker_auth import challenge_message
from threads_platform.workers.key_store import WorkerDeviceIdentity

pytestmark = pytest.mark.integration


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 25, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, duration: timedelta) -> None:
        self.current += duration


async def test_worker_enrollment_authentication_protocol_and_presence(
    unit_of_work_factory: object,
    db_session: AsyncSession,
) -> None:
    clock = MutableClock()
    service = WorkerControlService(
        unit_of_work_factory,  # type: ignore[arg-type]
        clock=clock,
        enrollment_ttl=timedelta(seconds=5),
        challenge_ttl=timedelta(seconds=5),
        session_ttl=timedelta(seconds=20),
        presence_ttl=timedelta(seconds=10),
    )
    issued = await service.create_enrollment()
    identity = WorkerDeviceIdentity.generate()
    worker_id = uuid4()
    enrolled = await service.enroll(
        issued.code,
        worker_id=worker_id,
        display_name="Primary worker",
        hostname="OPS-01",
        platform="windows",
        public_key=identity.public_key_bytes,
        max_concurrent_jobs=3,
    )
    assert enrolled.worker_id == worker_id
    with pytest.raises(WorkerControlError, match="ENROLLMENT_INVALID_OR_EXPIRED"):
        await service.enroll(
            issued.code,
            worker_id=uuid4(),
            display_name="Replay",
            hostname="OPS-02",
            platform="windows",
            public_key=identity.public_key_bytes,
        )

    expiring = await service.create_enrollment()
    clock.advance(timedelta(seconds=6))
    with pytest.raises(WorkerControlError, match="ENROLLMENT_INVALID_OR_EXPIRED"):
        await service.enroll(
            expiring.code,
            worker_id=uuid4(),
            display_name="Expired",
            hostname="OPS-03",
            platform="windows",
            public_key=identity.public_key_bytes,
        )

    challenge = await service.create_challenge(worker_id)
    signature = identity.sign(challenge_message(challenge.challenge_id, challenge.nonce))
    session = await service.exchange_challenge(challenge.challenge_id, signature)
    assert await service.authenticate(session.access_token) == worker_id
    with pytest.raises(WorkerControlError, match="AUTH_CHALLENGE_EXPIRED_OR_REPLAYED"):
        await service.exchange_challenge(challenge.challenge_id, signature)

    invalid_signature_challenge = await service.create_challenge(worker_id)
    with pytest.raises(WorkerControlError, match="AUTH_SIGNATURE_INVALID"):
        await service.exchange_challenge(invalid_signature_challenge.challenge_id, b"x" * 64)

    expired_challenge = await service.create_challenge(worker_id)
    expired_signature = identity.sign(
        challenge_message(expired_challenge.challenge_id, expired_challenge.nonce)
    )
    clock.advance(timedelta(seconds=6))
    with pytest.raises(WorkerControlError, match="AUTH_CHALLENGE_EXPIRED_OR_REPLAYED"):
        await service.exchange_challenge(expired_challenge.challenge_id, expired_signature)

    incompatible = await service.hello(
        worker_id,
        protocol_version=2,
        agent_version="2.0.0",
        capabilities_schema_version=1,
        capabilities=[WorkerCapability(worker_id, "synthetic.echo", 1)],
    )
    assert incompatible.status is WorkerStatus.UPGRADE_REQUIRED
    assert not incompatible.protocol_compatible

    compatible = await service.hello(
        worker_id,
        protocol_version=1,
        agent_version="1.0.0",
        capabilities_schema_version=1,
        capabilities=[WorkerCapability(worker_id, "synthetic.echo", 1)],
    )
    assert compatible.status is WorkerStatus.ONLINE
    degraded = await service.heartbeat(worker_id, healthy=False)
    assert degraded.status is WorkerStatus.DEGRADED

    clock.advance(timedelta(seconds=11))
    assert await service.expire_presence() == 1
    recovered = await service.heartbeat(worker_id)
    assert recovered.status is WorkerStatus.ONLINE
    clock.advance(timedelta(seconds=9))
    assert await service.authenticate(session.access_token) is None

    stored_worker = await db_session.get(WorkerNodeRecord, worker_id)
    stored_enrollment = await db_session.scalar(
        select(WorkerEnrollmentRecord).where(WorkerEnrollmentRecord.token_digest.is_not(None))
    )
    stored_session = await db_session.scalar(select(WorkerSessionRecord))
    assert stored_worker is not None
    assert stored_worker.public_key == identity.public_key_bytes
    assert "private_key" not in WorkerNodeRecord.__table__.columns
    assert stored_enrollment is not None
    assert stored_enrollment.token_digest != issued.code
    assert stored_session is not None
    assert stored_session.token_digest != session.access_token

    audit_events = await db_session.scalars(
        select(WorkerAuditEventRecord).where(WorkerAuditEventRecord.worker_id == worker_id)
    )
    audit_text = repr(list(audit_events))
    assert issued.code not in audit_text
    assert session.access_token not in audit_text
    assert signature.hex() not in audit_text


async def test_worker_session_rejects_unknown_token(
    unit_of_work_factory: object,
) -> None:
    service = WorkerControlService(unit_of_work_factory)  # type: ignore[arg-type]
    assert await service.authenticate("not-a-worker-token") is None
