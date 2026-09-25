from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from threads_platform.domain.capabilities import OperationClass
from threads_platform.domain.time import normalize_utc


class AccountExecutionOwnerType(StrEnum):
    COMMAND = "COMMAND"
    WORKER_JOB = "WORKER_JOB"


@dataclass(frozen=True, slots=True)
class AccountExecutionLease:
    account_id: UUID
    owner_type: AccountExecutionOwnerType
    owner_id: str
    operation_class: OperationClass
    fencing_generation: int
    lease_expires_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.owner_id.strip() or self.fencing_generation < 1:
            raise ValueError("account execution lease requires an owner and positive fence")
        if self.operation_class is OperationClass.READ:
            raise ValueError("READ operations do not acquire exclusive account leases")
        object.__setattr__(self, "lease_expires_at", normalize_utc(self.lease_expires_at))
        object.__setattr__(self, "updated_at", normalize_utc(self.updated_at))
