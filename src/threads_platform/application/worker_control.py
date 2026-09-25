import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from threads_platform.application.clock import Clock, SystemClock
from threads_platform.application.ports.repositories import UnitOfWorkFactory
from threads_platform.domain.time import normalize_utc
from threads_platform.domain.workers import (
    WorkerAuditEvent,
    WorkerAuthChallenge,
    WorkerCapability,
    WorkerEnrollment,
    WorkerNode,
    WorkerSession,
    WorkerStatus,
)
from threads_platform.infrastructure.security.worker_auth import (
    challenge_message,
    verify_worker_signature,
)

SUPPORTED_WORKER_PROTOCOL_VERSION = 1
SUPPORTED_CAPABILITY_SCHEMA_VERSION = 1


class WorkerControlError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EnrollmentIssued:
    code: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EnrolledWorker:
    worker_id: UUID


@dataclass(frozen=True, slots=True)
class AuthChallenge:
    challenge_id: UUID
    nonce: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerAccessSession:
    access_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerPresence:
    worker_id: UUID
    status: WorkerStatus
    last_heartbeat_at: datetime
    presence_expires_at: datetime
    protocol_compatible: bool


class WorkerControlService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        clock: Clock | None = None,
        enrollment_ttl: timedelta = timedelta(minutes=10),
        challenge_ttl: timedelta = timedelta(minutes=1),
        session_ttl: timedelta = timedelta(minutes=15),
        presence_ttl: timedelta = timedelta(seconds=90),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or SystemClock()
        self._enrollment_ttl = enrollment_ttl
        self._challenge_ttl = challenge_ttl
        self._session_ttl = session_ttl
        self._presence_ttl = presence_ttl

    async def create_enrollment(self, created_by: str = "operator") -> EnrollmentIssued:
        now = normalize_utc(self._clock.now())
        code = secrets.token_urlsafe(32)
        enrollment = WorkerEnrollment(
            token_digest=self._digest(code),
            created_at=now,
            expires_at=now + self._enrollment_ttl,
            created_by=created_by,
        )
        async with self._unit_of_work_factory() as unit_of_work:
            await unit_of_work.worker_security.add_enrollment(enrollment)
            await unit_of_work.worker_security.add_audit_event(
                WorkerAuditEvent(
                    event_type="worker.enrollment.issued",
                    enrollment_id=enrollment.id,
                    created_at=now,
                )
            )
        return EnrollmentIssued(code=code, expires_at=enrollment.expires_at)

    async def enroll(
        self,
        code: str,
        *,
        worker_id: UUID,
        display_name: str,
        hostname: str,
        platform: str,
        public_key: bytes,
        max_concurrent_jobs: int = 1,
    ) -> EnrolledWorker:
        now = normalize_utc(self._clock.now())
        enrollment: WorkerEnrollment | None = None
        valid = False
        async with self._unit_of_work_factory() as unit_of_work:
            enrollment = await unit_of_work.worker_security.get_enrollment_for_update(
                self._digest(code)
            )
            if enrollment is not None:
                valid = enrollment.consumed_at is None and enrollment.expires_at > now
                enrollment.consumed_at = now
                await unit_of_work.worker_security.update_enrollment(enrollment)
            if valid:
                worker = WorkerNode(
                    worker_id=worker_id,
                    display_name=display_name,
                    hostname=hostname,
                    platform=platform,
                    public_key=public_key,
                    max_concurrent_jobs=max_concurrent_jobs,
                    status=WorkerStatus.REGISTERING,
                    created_at=now,
                    updated_at=now,
                )
                await unit_of_work.workers.add(worker)
            await unit_of_work.worker_security.add_audit_event(
                WorkerAuditEvent(
                    event_type=("worker.enrolled" if valid else "worker.enrollment.rejected"),
                    worker_id=worker_id if valid else None,
                    enrollment_id=enrollment.id if enrollment is not None else None,
                    detail_code=None if valid else "INVALID_OR_EXPIRED",
                    created_at=now,
                )
            )
        if not valid:
            raise WorkerControlError("ENROLLMENT_INVALID_OR_EXPIRED")
        return EnrolledWorker(worker_id=worker_id)

    async def create_challenge(self, worker_id: UUID) -> AuthChallenge:
        now = normalize_utc(self._clock.now())
        challenge = WorkerAuthChallenge(
            worker_id=worker_id,
            nonce=secrets.token_urlsafe(32),
            issued_at=now,
            expires_at=now + self._challenge_ttl,
        )
        async with self._unit_of_work_factory() as unit_of_work:
            worker = await unit_of_work.workers.get(worker_id)
            if (
                worker is None
                or worker.public_key is None
                or worker.status is WorkerStatus.DISABLED
            ):
                raise WorkerControlError("WORKER_NOT_AUTHENTICATABLE")
            await unit_of_work.worker_security.add_challenge(challenge)
            await unit_of_work.worker_security.add_audit_event(
                WorkerAuditEvent(
                    event_type="worker.auth.challenge_issued",
                    worker_id=worker_id,
                    created_at=now,
                )
            )
        return AuthChallenge(challenge.id, challenge.nonce, challenge.expires_at)

    async def exchange_challenge(self, challenge_id: UUID, signature: bytes) -> WorkerAccessSession:
        now = normalize_utc(self._clock.now())
        access_token: str | None = None
        expires_at: datetime | None = None
        error_code: str | None = None
        async with self._unit_of_work_factory() as unit_of_work:
            challenge = await unit_of_work.worker_security.get_challenge_for_update(challenge_id)
            worker = (
                await unit_of_work.workers.get(challenge.worker_id)
                if challenge is not None
                else None
            )
            if challenge is None:
                error_code = "AUTH_CHALLENGE_INVALID"
            else:
                valid_window = challenge.used_at is None and challenge.expires_at > now
                challenge.used_at = now
                await unit_of_work.worker_security.update_challenge(challenge)
                signature_valid = (
                    worker is not None
                    and worker.public_key is not None
                    and verify_worker_signature(
                        worker.public_key,
                        challenge_message(challenge.id, challenge.nonce),
                        signature,
                    )
                )
                if valid_window and signature_valid and worker is not None:
                    access_token = secrets.token_urlsafe(32)
                    expires_at = now + self._session_ttl
                    session = WorkerSession(
                        worker_id=worker.worker_id,
                        token_digest=self._digest(access_token),
                        issued_at=now,
                        expires_at=expires_at,
                    )
                    await unit_of_work.worker_security.add_session(session)
                    await unit_of_work.worker_security.add_audit_event(
                        WorkerAuditEvent(
                            event_type="worker.auth.session_issued",
                            worker_id=worker.worker_id,
                            created_at=now,
                        )
                    )
                else:
                    error_code = (
                        "AUTH_CHALLENGE_EXPIRED_OR_REPLAYED"
                        if not valid_window
                        else "AUTH_SIGNATURE_INVALID"
                    )
                    await unit_of_work.worker_security.add_audit_event(
                        WorkerAuditEvent(
                            event_type="worker.auth.rejected",
                            worker_id=challenge.worker_id,
                            detail_code=error_code,
                            created_at=now,
                        )
                    )
        if error_code is not None or access_token is None or expires_at is None:
            raise WorkerControlError(error_code or "AUTH_CHALLENGE_INVALID")
        return WorkerAccessSession(access_token, expires_at)

    async def authenticate(self, access_token: str) -> UUID | None:
        if not access_token:
            return None
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            session = await unit_of_work.worker_security.get_active_session(
                self._digest(access_token), now
            )
            if session is None:
                return None
            worker = await unit_of_work.workers.get(session.worker_id)
            if worker is None or worker.status is WorkerStatus.DISABLED:
                return None
            return worker.worker_id

    async def hello(
        self,
        worker_id: UUID,
        *,
        protocol_version: int,
        agent_version: str,
        capabilities_schema_version: int,
        capabilities: list[WorkerCapability],
        display_name: str | None = None,
        hostname: str | None = None,
        platform: str | None = None,
        max_concurrent_jobs: int | None = None,
        healthy: bool = True,
    ) -> WorkerPresence:
        now = normalize_utc(self._clock.now())
        compatible = (
            protocol_version == SUPPORTED_WORKER_PROTOCOL_VERSION
            and capabilities_schema_version == SUPPORTED_CAPABILITY_SCHEMA_VERSION
        )
        async with self._unit_of_work_factory() as unit_of_work:
            worker = await unit_of_work.workers.get_for_update(worker_id)
            if worker is None:
                raise WorkerControlError("WORKER_NOT_FOUND")
            if display_name is not None:
                worker.display_name = display_name
            if hostname is not None:
                worker.hostname = hostname
            if platform is not None:
                worker.platform = platform
            if max_concurrent_jobs is not None:
                worker.max_concurrent_jobs = max_concurrent_jobs
            worker.protocol_version = protocol_version
            worker.agent_version = agent_version
            worker.capabilities_schema_version = capabilities_schema_version
            worker.last_heartbeat_at = now
            worker.presence_expires_at = now + self._presence_ttl
            if not compatible:
                worker.status = WorkerStatus.UPGRADE_REQUIRED
            elif worker.status not in {WorkerStatus.DISABLED, WorkerStatus.DRAINING}:
                worker.status = WorkerStatus.ONLINE if healthy else WorkerStatus.DEGRADED
            worker.updated_at = now
            await unit_of_work.workers.update(worker)
            if capabilities_schema_version == SUPPORTED_CAPABILITY_SCHEMA_VERSION:
                await unit_of_work.worker_capabilities.replace_for_worker(worker_id, capabilities)
            await unit_of_work.worker_security.add_audit_event(
                WorkerAuditEvent(
                    event_type="worker.presence.hello",
                    worker_id=worker_id,
                    detail_code=None if compatible else "VERSION_INCOMPATIBLE",
                    created_at=now,
                )
            )
        return WorkerPresence(
            worker_id=worker_id,
            status=worker.status,
            last_heartbeat_at=now,
            presence_expires_at=worker.presence_expires_at,
            protocol_compatible=compatible,
        )

    async def heartbeat(self, worker_id: UUID, *, healthy: bool = True) -> WorkerPresence:
        now = normalize_utc(self._clock.now())
        async with self._unit_of_work_factory() as unit_of_work:
            worker = await unit_of_work.workers.get_for_update(worker_id)
            if worker is None:
                raise WorkerControlError("WORKER_NOT_FOUND")
            worker.last_heartbeat_at = now
            worker.presence_expires_at = now + self._presence_ttl
            compatible = (
                worker.protocol_version == SUPPORTED_WORKER_PROTOCOL_VERSION
                and worker.capabilities_schema_version == SUPPORTED_CAPABILITY_SCHEMA_VERSION
            )
            if not compatible:
                worker.status = WorkerStatus.UPGRADE_REQUIRED
            elif worker.status not in {WorkerStatus.DISABLED, WorkerStatus.DRAINING}:
                worker.status = WorkerStatus.ONLINE if healthy else WorkerStatus.DEGRADED
            worker.updated_at = now
            await unit_of_work.workers.update(worker)
            await unit_of_work.worker_security.add_audit_event(
                WorkerAuditEvent(
                    event_type="worker.presence.heartbeat",
                    worker_id=worker_id,
                    detail_code=None if healthy else "DEGRADED",
                    created_at=now,
                )
            )
        return WorkerPresence(
            worker_id=worker_id,
            status=worker.status,
            last_heartbeat_at=now,
            presence_expires_at=worker.presence_expires_at,
            protocol_compatible=compatible,
        )

    async def expire_presence(self) -> int:
        now = normalize_utc(self._clock.now())
        expired_count = 0
        async with self._unit_of_work_factory() as unit_of_work:
            expired = await unit_of_work.workers.list_expired_presence(now)
            for snapshot in expired:
                worker = await unit_of_work.workers.get_for_update(snapshot.worker_id)
                if (
                    worker is not None
                    and worker.status in {WorkerStatus.ONLINE, WorkerStatus.DEGRADED}
                    and worker.presence_expires_at is not None
                    and worker.presence_expires_at <= now
                ):
                    worker.status = WorkerStatus.OFFLINE
                    worker.updated_at = now
                    await unit_of_work.workers.update(worker)
                    await unit_of_work.worker_security.add_audit_event(
                        WorkerAuditEvent(
                            event_type="worker.presence.expired",
                            worker_id=worker.worker_id,
                            created_at=now,
                        )
                    )
                    expired_count += 1
        return expired_count

    async def audit_events(self, worker_id: UUID) -> list[WorkerAuditEvent]:
        async with self._unit_of_work_factory() as unit_of_work:
            return await unit_of_work.worker_security.list_audit_events(worker_id)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
