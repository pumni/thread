from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from threads_platform.domain.time import normalize_utc, utc_now


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REAUTHORIZATION_REQUIRED = "REAUTHORIZATION_REQUIRED"
    DISABLED = "DISABLED"


class CredentialStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REAUTHORIZATION_REQUIRED = "REAUTHORIZATION_REQUIRED"
    REVOKED = "REVOKED"


class AccountExecutionMode(StrEnum):
    API_ONLY = "API_ONLY"
    BROWSER_ONLY = "BROWSER_ONLY"
    HYBRID = "HYBRID"
    MANUAL = "MANUAL"


@dataclass(slots=True)
class ThreadsAccount:
    threads_user_id: str
    username: str
    id: UUID = field(default_factory=uuid4)
    display_name: str | None = None
    status: AccountStatus = AccountStatus.ACTIVE
    execution_mode: AccountExecutionMode = AccountExecutionMode.API_ONLY
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.threads_user_id.strip() or not self.username.strip():
            raise ValueError("Threads account identifiers must not be empty")
        self.created_at = normalize_utc(self.created_at)
        self.updated_at = normalize_utc(self.updated_at)


@dataclass(slots=True)
class OAuthCredentialMetadata:
    account_id: UUID
    credential_ref: str
    id: UUID = field(default_factory=uuid4)
    token_type: str = "Bearer"
    granted_scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None
    status: CredentialStatus = CredentialStatus.ACTIVE
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.credential_ref.strip():
            raise ValueError("credential_ref must not be empty")
        if self.expires_at is not None:
            self.expires_at = normalize_utc(self.expires_at)
        self.updated_at = normalize_utc(self.updated_at)
