from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now


def _empty_metadata() -> dict[str, object]:
    return {}


class WorkerStatus(StrEnum):
    REGISTERING = "REGISTERING"
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"
    UPGRADE_REQUIRED = "UPGRADE_REQUIRED"


class NetworkProtocol(StrEnum):
    DIRECT = "DIRECT"
    HTTP = "HTTP"
    HTTPS = "HTTPS"
    SOCKS5 = "SOCKS5"


class BrowserSessionState(StrEnum):
    UNINITIALIZED = "UNINITIALIZED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    STARTING = "STARTING"
    AUTHENTICATED = "AUTHENTICATED"
    BUSY = "BUSY"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
    ERROR = "ERROR"
    STOPPED = "STOPPED"

    @property
    def requires_intervention(self) -> bool:
        return self in {
            BrowserSessionState.LOGIN_REQUIRED,
            BrowserSessionState.SESSION_EXPIRED,
            BrowserSessionState.CHALLENGE_REQUIRED,
        }


@dataclass(slots=True)
class WorkerNode:
    worker_id: UUID
    display_name: str
    hostname: str
    platform: str = "unknown"
    agent_version: str | None = None
    protocol_version: int | None = None
    capabilities_schema_version: int | None = None
    public_key: bytes | None = None
    status: WorkerStatus = WorkerStatus.REGISTERING
    max_concurrent_jobs: int = 1
    max_browser_sessions: int = 1
    active_browser_sessions: int = 0
    last_heartbeat_at: datetime | None = None
    presence_expires_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.display_name.strip() or not self.hostname.strip() or not self.platform.strip():
            raise ValueError("worker display name, hostname, and platform must not be empty")
        if self.max_concurrent_jobs < 1:
            raise ValueError("worker capacity must be positive")
        if self.max_browser_sessions < 1 or self.active_browser_sessions < 0:
            raise ValueError(
                "browser session capacity must be positive and active count nonnegative"
            )
        if self.active_browser_sessions > self.max_browser_sessions:
            raise ValueError("active browser sessions cannot exceed browser session capacity")
        if self.protocol_version is not None and self.protocol_version < 1:
            raise ValueError("protocol_version must be positive")
        if self.capabilities_schema_version is not None and self.capabilities_schema_version < 1:
            raise ValueError("capabilities_schema_version must be positive")
        if self.public_key is not None and len(self.public_key) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)
        if self.last_heartbeat_at is not None:
            self.last_heartbeat_at = normalize_utc(self.last_heartbeat_at)
        if self.presence_expires_at is not None:
            self.presence_expires_at = normalize_utc(self.presence_expires_at)


@dataclass(slots=True)
class WorkerCapability:
    worker_id: UUID
    name: str
    version: int
    id: UUID = field(default_factory=uuid4)
    metadata: dict[str, object] = field(default_factory=_empty_metadata)
    advertised_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.name.strip() or self.version < 1:
            raise ValueError("capability name and positive version are required")
        self.advertised_at = normalize_utc(self.advertised_at)


@dataclass(slots=True)
class BrowserProfile:
    worker_id: UUID
    profile_ref: str
    id: UUID = field(default_factory=uuid4)
    display_name: str | None = None
    metadata: dict[str, object] = field(default_factory=_empty_metadata)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if (
            not self.profile_ref.strip()
            or "/" in self.profile_ref
            or "\\" in self.profile_ref
            or ":" in self.profile_ref
            or self.profile_ref in {".", ".."}
        ):
            raise ValueError("profile_ref must be a logical reference, not a filesystem path")
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)


@dataclass(slots=True)
class NetworkProfile:
    account_id: UUID
    name: str
    protocol: NetworkProtocol
    host: str | None
    port: int | None
    id: UUID = field(default_factory=uuid4)
    credential_ref: str | None = field(default=None, repr=False)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("network profile name must not be empty")
        if self.protocol is NetworkProtocol.DIRECT:
            if self.host is not None or self.port is not None or self.credential_ref is not None:
                raise ValueError("direct network profile cannot have proxy configuration")
        elif not self.host or self.port is None or not 1 <= self.port <= 65535:
            raise ValueError("proxy network profile requires a host and valid port")
        if self.credential_ref is not None and not self.credential_ref.strip():
            raise ValueError("credential_ref must not be empty")
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)


@dataclass(slots=True)
class AccountWorkerAssignment:
    account_id: UUID
    worker_id: UUID
    profile_ref: str
    id: UUID = field(default_factory=uuid4)
    network_profile_id: UUID | None = None
    is_active: bool = True
    assigned_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.profile_ref.strip() or "/" in self.profile_ref or "\\" in self.profile_ref:
            raise ValueError("assignment profile_ref must be a logical reference")
        self.assigned_at = normalize_utc(self.assigned_at)
        self.ended_at = normalize_utc(self.ended_at) if self.ended_at is not None else None
        if self.is_active != (self.ended_at is None):
            raise ValueError("active assignment must not have ended_at")


@dataclass(slots=True)
class WorkerEnrollment:
    token_digest: str
    expires_at: datetime
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    consumed_at: datetime | None = None
    created_by: str | None = None

    def __post_init__(self) -> None:
        if len(self.token_digest) != 64:
            raise ValueError("enrollment token digest must be SHA-256 hex")
        self.created_at = normalize_utc(self.created_at)
        self.expires_at = normalize_utc(self.expires_at)
        self.consumed_at = normalize_utc(self.consumed_at) if self.consumed_at else None


@dataclass(slots=True)
class WorkerAuthChallenge:
    worker_id: UUID
    nonce: str
    expires_at: datetime
    id: UUID = field(default_factory=uuid4)
    issued_at: datetime = field(default_factory=utc_now)
    used_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.nonce.strip():
            raise ValueError("challenge nonce must not be empty")
        self.issued_at = normalize_utc(self.issued_at)
        self.expires_at = normalize_utc(self.expires_at)
        self.used_at = normalize_utc(self.used_at) if self.used_at else None


@dataclass(slots=True)
class WorkerSession:
    worker_id: UUID
    token_digest: str
    expires_at: datetime
    id: UUID = field(default_factory=uuid4)
    issued_at: datetime = field(default_factory=utc_now)
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if len(self.token_digest) != 64:
            raise ValueError("worker session digest must be SHA-256 hex")
        self.issued_at = normalize_utc(self.issued_at)
        self.expires_at = normalize_utc(self.expires_at)
        self.revoked_at = normalize_utc(self.revoked_at) if self.revoked_at else None


@dataclass(slots=True)
class WorkerAccountSession:
    account_id: UUID
    worker_id: UUID
    profile_ref: str
    session_id: UUID
    state: BrowserSessionState
    revision: int
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.profile_ref.strip() or "/" in self.profile_ref or "\\" in self.profile_ref:
            raise ValueError("session profile_ref must be a logical reference")
        if self.revision < 1:
            raise ValueError("session revision must be positive")
        self.updated_at = normalize_utc(self.updated_at)

    @property
    def requires_intervention(self) -> bool:
        return self.state.requires_intervention


@dataclass(slots=True)
class WorkerAuditEvent:
    event_type: str
    id: UUID = field(default_factory=uuid4)
    worker_id: UUID | None = None
    enrollment_id: UUID | None = None
    detail_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("event_type must not be empty")
        self.created_at = normalize_utc(self.created_at)
