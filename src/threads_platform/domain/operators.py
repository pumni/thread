from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class OperatorRole(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    user_id: UUID
    username: str
    role: OperatorRole
    enabled: bool
    must_change_password: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OperatorUser:
    id: UUID
    username: str
    role: OperatorRole
    enabled: bool
    must_change_password: bool
    created_at: datetime
